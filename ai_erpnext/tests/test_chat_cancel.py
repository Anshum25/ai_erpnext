"""Cancelling chat workflows (manual QA 2026-10-26, Part 2) and choosing a customer while creating a document (Issue 5).

Covers the typed and button Cancel of guided forms and document images at every stage (questions, record choices, records
created on the way, validation failures, the preview, after the Draft exists), stale buttons, retries, reloads, repeated
cancels, a new workflow afterwards, and the background extraction job. The router model is mocked and frappe.enqueue is
patched: no AI call, no background job and no email; everything else runs in a rolled-back transaction.

Run: bench --site <site> run-tests --module ai_erpnext.tests.test_chat_cancel
"""

import json
import shutil
import subprocess
from pathlib import Path
from unittest import mock

import frappe
from frappe.tests import IntegrationTestCase

from ai_erpnext.api import chat
from ai_erpnext.chat import engine, workflow
from ai_erpnext.pipeline.review_service import apply_mapping
from ai_erpnext.tests.fixtures import test_file
from ai_erpnext.tests.test_chat_agent import extraction

ROUTE = "ai_erpnext.chat.llm.route"


def create_plan(doctype, values=None, items=None):
	return {"mode": "create", "doctype": doctype, "values": values or {}, "items": items or []}


class TestChatCancel(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		ig = frappe.db.get_value("Item Group", {"is_group": 0}, "name")
		frappe.get_doc(
			{
				"doctype": "Item",
				"item_code": "AIX-ITEM",
				"item_name": "AIX Widget",
				"item_group": ig,
				"stock_uom": "Nos",
				"is_stock_item": 0,
				"is_sales_item": 1,
				"is_purchase_item": 1,
			}
		).insert()
		sgroup = frappe.db.get_value("Supplier Group", {"is_group": 0}, "name")
		cls.supplier = (
			frappe.get_doc({"doctype": "Supplier", "supplier_name": "AIX Supplies", "supplier_group": sgroup})
			.insert()
			.name
		)
		cgroup = frappe.db.get_value("Customer Group", {"is_group": 0}, "name")
		territory = frappe.db.get_value("Territory", {"is_group": 0}, "name")
		cls.customers = {}
		for key, name in (
			("exact", "AIX Exact Buyer"),
			("twin1", "AIX Twin Traders"),
			("twin2", "AIX Twin Traders"),
			("stale", "AIX Soon Disabled"),
		):
			cls.customers[key] = (
				frappe.get_doc(
					{
						"doctype": "Customer",
						"customer_name": name,
						"customer_group": cgroup,
						"territory": territory,
					}
				)
				.insert()
				.name
			)
		cls.image_url = test_file("png")

	def setUp(self):
		frappe.set_user("Administrator")
		self.route = mock.patch(ROUTE, return_value={"mode": "chat", "reply": "ok"}).start()
		self.enqueue = mock.patch("frappe.enqueue").start()
		self.addCleanup(mock.patch.stopall)
		self.s = chat.new_conversation()["session"]

	# helpers ------------------------------------------------------------------------------------
	def say(self, text, plan=None, client_id=None):
		if plan is not None:
			self.route.return_value = plan
		return chat.send_message(session=self.s, text=text, client_id=client_id)

	def state(self):
		return json.loads(frappe.db.get_value("AI Chat Session", self.s, "state") or "{}")

	def pending(self):
		return self.state().get("pending")

	def click(self, value, question=None, client_id=None):
		q = question or self.pending()
		return chat.send_message(
			session=self.s,
			action=json.dumps({"question_id": q["id"], "value": value, "label": value}),
			client_id=client_id,
		)

	def cancel_button(self, workflow_id):
		return chat.send_message(
			session=self.s, action=json.dumps({"command": "cancel", "workflow_id": workflow_id})
		)

	@staticmethod
	def texts(r):
		return " ".join(m.get("text") or "" for m in r["messages"] if m["role"] == "assistant")

	def history(self):
		return chat.get_session(self.s)["messages"]

	def counts(self, *doctypes):
		return {dt: frappe.db.count(dt) for dt in doctypes}

	def to_purchase_invoice_preview(self):
		r = self.say("create purchase invoice", create_plan("Purchase Invoice"))
		self.say("AIX Supplies")
		self.click("__skip__")  # supplier invoice number
		self.say("AIX-ITEM")
		self.say("2")
		self.say("150")
		self.click("__done__")
		self.assertEqual(self.pending()["kind"], "form_confirm")
		return r

	def assert_idle(self):
		st = self.state()
		self.assertEqual((st.get("forms"), st.get("pending"), st.get("workflow")), ([], None, None))
		payload = chat.get_session(self.s)
		self.assertIsNone(payload["pending_id"])
		self.assertIsNone(payload["workflow"])

	# 1. typed and button Cancel ---------------------------------------------------------------------
	def test_cancel_before_any_required_field_is_collected(self):
		before = self.counts("Sales Order")
		r = self.say("create sales order", create_plan("Sales Order"))
		self.assertEqual(r["workflow"]["label"], "Creating Sales Order (Draft)")
		q = next(m["question"] for m in reversed(r["messages"]) if m.get("question"))
		self.assertEqual(
			q["cancel"], {"label": "Cancel", "value": "__cancel_all__"}
		)  # in the question's choice area
		r = self.say("cancel")
		self.assertIn("Cancelled the Sales Order. Nothing was created by this workflow.", self.texts(r))
		self.assertIsNone(r["workflow"])
		self.assert_idle()
		self.assertEqual(self.counts("Sales Order"), before)

	def test_cancel_phrases_are_handled_without_the_model(self):
		for phrase in (
			"cancel",
			"Cancel this",
			"never mind",
			"stop creating it",
			"I changed my mind",
			"cancel the sales order",
			"cancle",
			"ok, cancel please",
		):
			self.say("create sales order", create_plan("Sales Order"))
			self.route.reset_mock()
			r = self.say(phrase)
			self.route.assert_not_called()
			self.assertIn("Cancelled the Sales Order", self.texts(r), phrase)
			self.assert_idle()

	def test_document_cancellation_requests_are_not_workflow_cancels(self):
		self.assertFalse(engine.CANCEL_RE.match("cancel sales invoice ACC-SINV-2026-00001"))
		self.assertFalse(engine.BARE_CANCEL_RE.match("cancel the sales invoice"))
		# with nothing in progress, "cancel the sales invoice" is a request about a document: the router answers it
		self.say(
			"cancel the sales invoice",
			{"mode": "unsupported", "reply": "I can't cancel documents."},
		)
		self.route.assert_called_once()
		self.assertIn("I never submit, cancel", self.history()[-1]["text"])

	def test_cancel_during_customer_selection_with_the_question_button(self):
		before = self.counts("Sales Invoice", "Customer")
		self.say(
			"Create a Sales Invoice for Rahul",
			create_plan("Sales Invoice", values={"party": "AIX Nobody Rahul"}),
		)
		q = self.pending()
		self.assertEqual((q["kind"], q["key"]), ("form_field", "customer"))
		values = [o["value"] for o in q["options"]]
		self.assertIn("__create__", values)
		self.assertNotIn("__cancel_all__", values)  # Cancel is separate from the answers ...
		self.assertEqual(q["cancel"]["value"], "__cancel_all__")  # ... but shown with them
		r = self.click("__cancel_all__")
		self.assertIn("Nothing was created by this workflow", self.texts(r))
		self.assert_idle()
		# the old "Create new customer" button can't resume the cancelled workflow
		r = self.click("__create__", question=q)
		self.assertIn("That question is no longer active", self.texts(r))
		self.assertEqual(self.counts("Sales Invoice", "Customer"), before)
		self.assert_idle()

	def test_cancel_after_a_customer_was_created_on_the_way_keeps_and_names_it(self):
		before = self.counts("Sales Invoice")
		self.say("create sales invoice", create_plan("Sales Invoice"))
		self.say("AIX Brand New Buyer")
		self.click("__create__")  # explicit choice: a nested Customer form, nothing created yet
		self.assertEqual(self.pending()["kind"], "form_confirm")
		self.assertFalse(frappe.db.exists("Customer", {"customer_name": "AIX Brand New Buyer"}))
		nested = self.pending()
		self.assertEqual(
			[o["label"] for o in nested["options"]], ["Create Customer", "Back to the Sales Invoice"]
		)
		self.assertEqual(nested["cancel"]["value"], "__cancel_all__")  # Cancel = the whole Sales Invoice
		self.click("__create__")
		customer = frappe.db.get_value("Customer", {"customer_name": "AIX Brand New Buyer"})
		self.assertTrue(customer)
		wf = chat.get_session(self.s)["workflow"]
		r = self.cancel_button(wf["id"])
		text = self.texts(r)
		self.assertIn("No Sales Invoice was created", text)
		self.assertIn(f"Customer {customer}", text)  # it exists, and the reply says so
		self.assertNotIn("Nothing was created", text)
		self.assertIn(
			{"label": f"Open Customer {customer}", "doctype": "Customer", "name": customer},
			r["messages"][-1]["links"],
		)
		self.assertTrue(frappe.db.exists("Customer", customer))  # never deleted
		self.assertEqual(self.counts("Sales Invoice"), before)
		self.assert_idle()

	def test_cancel_after_a_validation_failure(self):
		before = self.counts("Purchase Invoice")
		self.to_purchase_invoice_preview()
		with mock.patch(
			"frappe.model.document.Document.insert", side_effect=frappe.ValidationError("AIX refused")
		):
			r = self.click("__create__")
		self.assertIn("AIX refused", self.texts(r))
		self.assertEqual([o["value"] for o in self.pending()["options"]], ["__retry__", "__cancel__"])
		r = self.say("cancel")
		self.assertIn("Nothing was created by this workflow", self.texts(r))
		self.assert_idle()
		self.assertEqual(self.counts("Purchase Invoice"), before)

	def test_cancel_on_the_preview_with_the_cancel_bar_and_stale_confirmation(self):
		before = self.counts("Purchase Invoice")
		self.to_purchase_invoice_preview()
		preview = self.pending()
		wf = chat.get_session(self.s)["workflow"]
		self.assertEqual((wf["kind"], wf["label"]), ("form", "Creating Purchase Invoice (Draft)"))
		r = self.cancel_button(wf["id"])
		self.assertEqual(r["messages"][0]["text"], "Cancel")  # the user's press, shown in the conversation
		self.assertIn("Cancelled the Purchase Invoice", self.texts(r))
		self.assert_idle()
		r = self.click("__create__", question=preview)  # the old Create button of the preview
		self.assertIn("That question is no longer active", self.texts(r))
		self.assertEqual(self.counts("Purchase Invoice"), before)

	# 2. after the Draft exists -----------------------------------------------------------------------
	def test_cancel_after_the_draft_was_created_reports_it_and_never_deletes_or_submits(self):
		self.to_purchase_invoice_preview()
		r = self.click("__create__")
		name = next(
			m["card"]["name"] for m in r["messages"] if (m.get("card") or {}).get("type") == "created"
		)
		self.assertIsNone(r["workflow"])  # finished: nothing left to cancel
		r = self.say("cancel")
		text = self.texts(r)
		self.assertIn(f"Purchase Invoice {name} was already created as a Draft and stays in ERPNext", text)
		self.assertIn("doesn't delete it", text)
		self.assertEqual(r["messages"][-1]["links"][0]["name"], name)
		self.assertEqual(
			frappe.db.get_value("Purchase Invoice", name, "docstatus"), 0
		)  # not submitted, not deleted
		# a later "cancel" (after another request) no longer refers to it
		self.say("hi")
		r = self.say("cancel")
		self.assertEqual(self.texts(r), "There's nothing in progress to cancel.")

	def test_cancel_after_the_document_was_submitted_never_reverses_it(self):
		self.to_purchase_invoice_preview()
		r = self.click("__create__")
		name = next(
			m["card"]["name"] for m in r["messages"] if (m.get("card") or {}).get("type") == "created"
		)
		frappe.db.set_value(
			"Purchase Invoice", name, "docstatus", 1
		)  # the user submitted it in ERPNext meanwhile
		r = self.say("cancel")
		text = self.texts(r)
		self.assertIn(f"Purchase Invoice {name} is already submitted", text)
		self.assertIn("I never cancel documents", text)
		self.assertEqual(frappe.db.get_value("Purchase Invoice", name, "docstatus"), 1)

	# 3. idempotence, retries, reloads, a new workflow --------------------------------------------------
	def test_repeated_cancel_retry_reload_and_a_new_workflow(self):
		self.say("hello there")
		self.say("create sales order", create_plan("Sales Order"))
		old_wf = chat.get_session(self.s)["workflow"]["id"]
		first = self.pending()
		self.say("cancel", client_id="c-1")
		seq = self.state()["seq"]
		again = self.say("cancel", client_id="c-1")  # the same message delivered twice (network retry)
		self.assertEqual(self.state()["seq"], seq)  # not processed again
		self.assertIn("Cancelled the Sales Order", self.texts(again))  # the original reply is returned
		r = self.say("cancel")  # a second, new Cancel: safe
		self.assertEqual(self.texts(r), "There's nothing in progress to cancel.")
		r = self.cancel_button(old_wf)  # the Cancel bar of the ended workflow
		self.assertEqual(self.texts(r), "There's nothing in progress to cancel.")
		payload = chat.get_session(self.s)  # a reload shows no active question and no workflow
		self.assertIsNone(payload["pending_id"])
		self.assertIsNone(payload["workflow"])
		self.assertIn("That question is no longer active", self.texts(self.click("__more__", question=first)))
		# history is kept
		users = [m.get("text") for m in payload["messages"] if m["role"] == "user"]
		self.assertEqual(users[:3], ["hello there", "create sales order", "cancel"])
		# a new workflow starts normally, with a new id; the old Cancel can't touch it
		r = self.say("create purchase order", create_plan("Purchase Order"))
		new_wf = r["workflow"]["id"]
		self.assertNotEqual(new_wf, old_wf)
		self.assertEqual(self.pending()["key"], "supplier")
		r = self.cancel_button(old_wf)
		self.assertIn("belonged to an earlier task", self.texts(r))
		self.assertEqual(self.state()["forms"][0]["doctype"], "Purchase Order")
		self.assertEqual(self.pending()["key"], "supplier")
		self.assertIn("Cancelled the Purchase Order", self.texts(self.cancel_button(new_wf)))

	def test_late_and_unknown_actions_never_resume_a_cancelled_workflow(self):
		self.say("create purchase invoice", create_plan("Purchase Invoice"))
		q = self.pending()
		self.say("AIX Supplies")  # answered: a new question is pending now
		self.say("never mind")
		for value in ("__create__", "__skip__", "__cancel_all__", self.supplier):
			r = self.click(value, question=q)
			self.assertIn("That question is no longer active", self.texts(r))
		self.assert_idle()

	# 4. document images and the background job -------------------------------------------------------
	def entry(self):
		return frappe.get_doc(
			"AI Document Entry", frappe.db.get_value("AI Chat Session", self.s, "active_entry")
		)

	def test_cancel_while_the_image_is_being_read_stops_the_job(self):
		from ai_erpnext.pipeline.controller import run_ai_pipeline

		r = chat.send_message(session=self.s, text="create invoice from this", file_url=self.image_url)
		self.assertTrue(r["busy"])
		self.assertEqual(r["workflow"]["label"], "Reading the attached document")
		r = self.cancel_button(r["workflow"]["id"])
		self.assertIn("Cancelled. Nothing was created by this workflow.", self.texts(r))
		self.assertFalse(r["busy"])
		entry = self.entry()
		self.assertEqual((entry.status, entry.error_log), ("Failed", engine.CANCELLED_EXTRACTION))
		# the queued job finds the entry no longer Pending: no AI call, nothing stored
		with mock.patch("ai_erpnext.ai.providers.run") as provider:
			run_ai_pipeline(entry.name)
		provider.assert_not_called()
		self.assertFalse(self.entry().extracted_data)
		# polling never advances it, and "retry" doesn't restart it
		self.assertEqual(chat.poll(session=self.s, since=self.state()["seq"])["messages"], [])
		self.assertIn("nothing to retry", self.texts(self.say("retry")))
		self.assertEqual(self.entry().status, "Failed")

	def test_a_job_already_running_when_cancelled_does_not_store_its_result(self):
		from ai_erpnext.pipeline.controller import run_ai_pipeline

		chat.send_message(session=self.s, text="create invoice from this", file_url=self.image_url)
		name = self.entry().name

		def provider_call(*args, **kwargs):
			# the user cancels in the chat while the provider is still working
			self.cancel_button(chat.get_session(self.s)["workflow"]["id"])
			return extraction()

		with (
			mock.patch("ai_erpnext.ai.providers.get_settings"),
			mock.patch("ai_erpnext.ai.providers.primary"),
			mock.patch("ai_erpnext.ai.providers.run", side_effect=provider_call),
			mock.patch("frappe.db.commit"),
		):
			run_ai_pipeline(name)
		entry = frappe.get_doc("AI Document Entry", name)
		self.assertEqual(entry.status, "Failed")
		self.assertFalse(entry.extracted_data)  # the late result was discarded
		self.assertEqual(self.state()["phase"], "cancelled")

	def test_cancel_while_choosing_a_customer_for_an_image_names_records_created_on_the_way(self):
		before = self.counts("Sales Invoice")
		chat.send_message(session=self.s, text="create invoice from this", file_url=self.image_url)
		entry = self.entry()
		entry.extracted_data = json.dumps(extraction(customer="AIX Image Buyer Unknown"))
		apply_mapping(entry)
		entry.save()
		chat.poll(session=self.s, since=0)
		for _ in range(6):  # past company/tax/duplicate questions to the customer
			q = self.pending()
			if q["kind"] == "customer":
				break
			self.click(
				{"taxes": "__none__", "duplicate": "__ack__", "data_errors": "__ack__"}.get(
					q["kind"], q["options"][0]["value"]
				)
			)
		q = self.pending()
		self.assertEqual(q["kind"], "customer")
		self.assertEqual(q["cancel"]["value"], "__cancel_all__")
		r = self.say("no")  # not a customer name: nothing is searched, nothing offered for creation
		self.assertIn("OK, none of these", self.texts(r))
		self.assertNotIn("“no”", json.dumps(r["messages"]))
		self.click("__create__")  # create the customer on the way (its own confirmation)
		for _ in range(4):
			q = self.pending()
			if q["kind"] == "master_confirm":
				break
			self.click(q["options"][0]["value"])
		self.click("__create__")
		customer = frappe.db.get_value("Customer", {"customer_name": "AIX Image Buyer Unknown"})
		self.assertTrue(customer)
		r = self.say("I changed my mind")
		text = self.texts(r)
		self.assertIn(f"Customer {customer}", text)
		self.assertIn("No document was created", text)
		self.assertTrue(frappe.db.exists("Customer", customer))
		self.assertEqual(self.counts("Sales Invoice"), before)
		self.assertEqual(self.state()["phase"], "cancelled")
		self.assertIsNone(chat.get_session(self.s)["workflow"])

	# 5. choosing the customer (Issue 5) ------------------------------------------------------------------
	def test_exact_customer_name_is_used_and_the_invoice_details_are_kept(self):
		self.say(
			"Create a Sales Invoice for AIX Exact Buyer",
			create_plan(
				"Sales Invoice", values={"party": "AIX Exact Buyer"}, items=[{"item": "AIX-ITEM", "qty": 3}]
			),
		)
		form = self.state()["forms"][0]
		self.assertEqual(form["values"]["customer"], self.customers["exact"])
		self.assertNotEqual(self.pending()["key"], "customer")

	def test_duplicate_and_partial_names_are_asked_never_picked(self):
		self.say(
			"create sales invoice for AIX Twin Traders",
			create_plan("Sales Invoice", values={"party": "AIX Twin Traders"}),
		)
		q = self.pending()
		self.assertEqual(q["key"], "customer")
		self.assertNotIn("customer", self.state()["forms"][0]["values"])
		offered = {o["value"] for o in q["options"]}
		self.assertTrue({self.customers["twin1"], self.customers["twin2"]} <= offered)
		self.say("cancel")
		self.say("create sales invoice", create_plan("Sales Invoice"))
		self.say("AIX Twin")  # partial: a choice, not a guess
		self.assertNotIn("customer", self.state()["forms"][0]["values"])
		self.assertEqual(self.pending()["key"], "customer")
		self.click(self.customers["twin2"])  # the button carries the exact record
		self.assertEqual(self.state()["forms"][0]["values"]["customer"], self.customers["twin2"])

	def test_no_match_offers_existing_customers_separately_from_create_new(self):
		before = self.counts("Customer")
		self.say(
			"Create a Sales Invoice for Rahul",
			create_plan(
				"Sales Invoice", values={"party": "AIX Rahul Nomatch"}, items=[{"item": "AIX-ITEM", "qty": 2}]
			),
		)
		q = self.pending()
		text = self.history()[-1]["text"]
		self.assertIn("I couldn't find a customer called “AIX Rahul Nomatch”", text)
		self.assertIn("Choose an existing customer below", text)
		self.assertEqual(text.count("You can also type a name"), 0)  # no repeated guidance
		create = [o for o in q["options"] if o["value"] == "__create__"]
		self.assertEqual(create[0]["label"], "Create new customer “AIX Rahul Nomatch”")
		self.assertEqual(
			self.state()["forms"][0]["queued_items"][0]["text"], "AIX-ITEM"
		)  # details kept while choosing
		r = self.say("no")
		self.assertIn("OK, none of these", self.texts(r))
		self.assertNotIn("Create new customer “no”", json.dumps(r["messages"]))
		self.assertEqual(self.pending()["key"], "customer")
		self.assertEqual(self.counts("Customer"), before)  # nothing created without the explicit choice
		self.say(
			"create new customer"
		)  # the explicit choice, typed: a Customer form to confirm, still nothing created
		form = self.state()["forms"][-1]
		self.assertEqual(
			(form["doctype"], form["values"].get("customer_name")), ("Customer", "AIX Rahul Nomatch")
		)
		self.assertEqual(self.counts("Customer"), before)
		self.assertEqual(self.state()["forms"][0]["queued_items"][0]["text"], "AIX-ITEM")

	def test_forged_or_stale_customer_selection_is_rejected_on_the_server(self):
		self.say("create sales invoice", create_plan("Sales Invoice"))
		self.click("AIX Customer That Does Not Exist")  # a button value never offered
		self.assertNotIn("customer", self.state()["forms"][0]["values"])
		self.assertEqual(self.pending()["key"], "customer")
		self.click(self.customers["stale"])
		self.assertEqual(self.state()["forms"][0]["values"]["customer"], self.customers["stale"])
		frappe.db.set_value("Customer", self.customers["stale"], "disabled", 1)  # changed before the preview
		self.say("AIX-ITEM")
		self.say("1")
		self.say("10")
		self.click("__done__")
		pending = self.pending()
		while pending["kind"] == "form_field" and pending["key"] != "customer":
			self.click(pending["options"][0]["value"]) if pending["options"] else self.say("2026-12-31")
			pending = self.pending()
		self.assertEqual(pending["key"], "customer")  # re-checked: asked again instead of using it
		self.assertNotIn("customer", self.state()["forms"][0]["values"])
		self.assertIn("can't be used", " ".join(m.get("text") or "" for m in self.history()[-3:]))


class TestCancelWidget(IntegrationTestCase):
	def test_cancel_control_and_ordering_helpers(self):
		node = shutil.which("node")
		if not node:
			self.skipTest("node is not available")
		script = Path(__file__).with_name("chat_state_check.mjs")
		result = subprocess.run([node, str(script)], capture_output=True, text=True, timeout=60)
		self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
		self.assertIn("ok", result.stdout)

	def test_widget_renders_the_cancel_bar_and_question_cancel_through_the_server(self):
		source = (Path(__file__).resolve().parents[1] / "public" / "js" / "ai_bot_v2.js").read_text()
		self.assertIn(
			'import { cancelControl, insertBeforeId, isStale, questionChoices } from "./ai_chat_state.js";',
			source,
		)
		self.assertIn('data-act="cancel-workflow"', source)
		self.assertIn("send({ action: JSON.stringify(control.action), client_id: uid() });", source)
		self.assertIn("const choices = questionChoices(q);", source)
		self.assertIn("S.workflow = r.workflow || null;", source)
		self.assertIn("isStale(seq, S.appliedSeq)", source)
		# the Cancel bar is not blocked by a running request (only the answer buttons are)
		handler = source[source.index("'[data-act=\"cancel-workflow\"]', function") :]
		self.assertNotIn("S.sending", handler[: handler.index("});")])
		self.assertEqual(
			workflow.cancelled_text("Sales Invoice", []),
			"Cancelled the Sales Invoice. Nothing was created by this workflow.",
		)
