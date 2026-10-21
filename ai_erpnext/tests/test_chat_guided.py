"""Guided creation with existing-master selection, explicit "Create new", corrections and loop safety.

A Purchase Order is built step by step from existing records; new suppliers/customers are only
created through their own preview and confirmation; corrections, a change of document type,
"start over" and "cancel" act on the pending state; provider failures and repeated invalid
answers never lose it. The router model is mocked (and must not be called where the chat is
deterministic); Document.submit fails the test if used; everything is rolled back.

Run: bench --site <site> run-tests --module ai_erpnext.tests.test_chat_guided
"""

import json
from unittest import mock

import frappe
from frappe.model.document import Document
from frappe.tests import IntegrationTestCase
from frappe.utils import add_days, nowdate

from ai_erpnext.ai.errors import AIProviderError
from ai_erpnext.api import chat

ROUTE = "ai_erpnext.chat.llm.route"
NO_MODEL = AssertionError("this message must not reach the AI model")


def no_submit(*args, **kwargs):
	raise AssertionError("submit() must never be called by the assistant")


class TestChatGuided(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		cls.company = frappe.db.get_single_value("Global Defaults", "default_company")
		cls.abbr = frappe.get_cached_value("Company", cls.company, "abbr")
		ig = frappe.db.get_value("Item Group", {"is_group": 0}, "name")
		frappe.get_doc(
			{
				"doctype": "Item",
				"item_code": "AIG-BOTTLE",
				"item_name": "AIG Bottle",
				"item_group": ig,
				"stock_uom": "Nos",
				"is_stock_item": 1,
				"is_sales_item": 1,
				"is_purchase_item": 1,
			}
		).insert()
		sg = frappe.db.get_value("Supplier Group", {"is_group": 0}, "name")
		cls.supplier = (
			frappe.get_doc(
				{"doctype": "Supplier", "supplier_name": "AIG Harbor Supplies", "supplier_group": sg}
			)
			.insert()
			.name
		)
		cg = frappe.db.get_value("Customer Group", {"is_group": 0}, "name")
		territory = frappe.db.get_value("Territory", {"is_group": 0}, "name")
		for name in ("AIG Buyer One", "AIG Buyer Two"):
			frappe.get_doc(
				{"doctype": "Customer", "customer_name": name, "customer_group": cg, "territory": territory}
			).insert()
		cls.stores, cls.goods = f"Stores - {cls.abbr}", f"Finished Goods - {cls.abbr}"

	def setUp(self):
		frappe.set_user("Administrator")
		self.route = mock.patch(ROUTE, side_effect=NO_MODEL).start()
		mock.patch("frappe.enqueue").start()
		for target in ("submit", "_submit"):
			mock.patch.object(Document, target, no_submit).start()
		self.addCleanup(mock.patch.stopall)
		self.s = chat.new_conversation()["session"]

	# helpers ----------------------------------------------------------------------------------
	def say(self, text, client_id=None):
		return chat.send_message(session=self.s, text=text, client_id=client_id)

	def state(self):
		return json.loads(frappe.db.get_value("AI Chat Session", self.s, "state") or "{}")

	def pending(self):
		return self.state()["pending"]

	def form(self):
		return self.state()["forms"][-1]

	def click(self, value, client_id=None):
		q = self.pending()
		return chat.send_message(
			session=self.s,
			action=json.dumps({"question_id": q["id"], "value": value, "label": value}),
			client_id=client_id,
		)

	def last(self, r):
		return r["messages"][-1]

	def po_at_preview(self, qty_rate="5 at 20"):
		"""create purchase order -> existing supplier -> date -> existing item -> qty and rate -> preview."""
		self.say("create purchase order")
		q = self.pending()
		self.assertEqual((q["kind"], q["key"]), ("form_field", "supplier"))
		values = [o["value"] for o in q["options"]]
		self.assertIn("__create__", values)  # Create New is a separate choice
		self.assertTrue(
			set(values) - {"__create__", "__more__"} <= set(frappe.get_all("Supplier", pluck="name"))
		)
		self.say(self.supplier) if self.supplier not in values else self.click(self.supplier)
		self.assertEqual(self.form()["values"]["supplier"], self.supplier)
		self.assertEqual(self.pending()["key"], "schedule_date")
		self.say(add_days(nowdate(), 7))
		q = self.pending()
		self.assertEqual((q["kind"], q["key"]), ("form_item", "item_code"))
		self.say("AIG Bottle")
		self.assertEqual(self.pending()["key"], "qty")
		self.say(qty_rate)
		self.assertEqual(self.pending()["kind"], "form_more_items")
		return self.click("__done__")

	def preview(self, r):
		card = self.last(r).get("card") or {}
		self.assertEqual(card.get("type"), "form_preview", self.last(r).get("text"))
		return card

	# ---- existing masters, preview, corrections, creation ------------------------------------------
	def test_purchase_order_from_existing_records_with_corrections(self):
		r = self.po_at_preview()
		card = self.preview(r)
		rows = {row["label"]: row["value"] for row in card["rows"]}
		self.assertEqual((rows["Supplier"], rows["Company"]), ("AIG Harbor Supplies", self.company))
		self.assertEqual(
			[(i["code"], i["qty"], i["rate"]) for i in card["items"]], [("AIG-BOTTLE", 5, 20)]
		)  # “5 at 20” set both
		self.assertEqual(
			card["items"][0]["warehouse"], self.stores
		)  # the configured default, labelled as such
		self.assertEqual(card["items"][0]["warehouse_note"], "ERPNext default")

		r = self.say("Use Finished Goods warehouse")
		self.assertIn(
			f"set to {self.goods} for all stock rows", "\n".join(m.get("text") or "" for m in r["messages"])
		)
		self.assertEqual(self.preview(r)["items"][0]["warehouse"], self.goods)
		r = self.say("change quantity to 7")
		card = self.preview(r)
		self.assertEqual(
			[(i["qty"], i["rate"], i["warehouse"]) for i in card["items"]], [(7, 20, self.goods)]
		)  # only qty changed
		self.assertEqual(
			{row["label"]: row["value"] for row in card["rows"]}["Supplier"], "AIG Harbor Supplies"
		)

		before = frappe.db.count("Purchase Order")
		r = self.click("__create__", client_id="po-1")
		created = self.last(r)["card"]
		po = frappe.get_doc("Purchase Order", created["name"])
		self.assertEqual((po.docstatus, po.supplier, po.company), (0, self.supplier, self.company))
		self.assertEqual(
			[(i.item_code, i.qty, i.rate, i.warehouse) for i in po.items], [("AIG-BOTTLE", 7, 20, self.goods)]
		)
		self.assertEqual(frappe.db.count("Purchase Order"), before + 1)
		stale = chat.send_message(
			session=self.s, action=json.dumps({"question_id": "old", "value": "__create__"}), client_id="po-2"
		)
		self.assertIn("no longer active", self.last(stale)["text"])
		self.assertEqual(
			frappe.db.count("Purchase Order"), before + 1
		)  # a second click never creates a second PO
		self.route.assert_not_called()

	def test_create_po_shows_existing_suppliers_and_create_new_as_buttons(self):
		"""“create po”: the guided form asks, with existing suppliers as buttons and a separate “Create new”."""
		r = self.say("create po")
		self.route.assert_not_called()  # understood without the model (it used to ask in free text, with no buttons)
		q = self.pending()
		self.assertEqual(
			(self.form()["doctype"], q["kind"], q["key"]), ("Purchase Order", "form_field", "supplier")
		)
		last = self.last(r)
		self.assertIn("Choose one below or create a new supplier", last["text"])
		values = [o["value"] for o in last["question"]["options"]]
		self.assertIn(self.supplier, values)
		self.assertEqual(values[-1], "__create__")
		existing = frappe.db.count("Supplier", {"disabled": 0})
		self.assertEqual(len([v for v in values if not v.startswith("__")]), min(existing, 10))
		self.assertEqual("__more__" in values, existing > 10)
		for text, doctype in (
			("new so", "Sales Order"),
			("make a pi", "Purchase Invoice"),
			("create grn", "Purchase Receipt"),
			("create a new SI", "Sales Invoice"),
		):
			self.say("cancel")
			self.say(text)
			self.assertEqual(self.form()["doctype"], doctype, text)
		self.route.assert_not_called()

	def test_show_more_pages_through_existing_suppliers(self):
		sg = frappe.db.get_value("Supplier Group", {"is_group": 0}, "name")
		for i in range(11):
			frappe.get_doc(
				{"doctype": "Supplier", "supplier_name": f"AIG Paged Supplier {i:02d}", "supplier_group": sg}
			).insert()
		self.say("create purchase order")
		first = [o["value"] for o in self.pending()["options"]]
		self.assertIn("__more__", first)
		self.click("__more__")
		second = [o["value"] for o in self.pending()["options"]]
		self.assertEqual(self.pending()["key"], "supplier")
		self.assertFalse(set(first) - {"__more__", "__create__"} & set(second))  # a new page of records
		self.assertIn("__create__", second)

	def test_model_never_asks_for_creation_details_in_free_text(self):
		self.route.side_effect = None
		self.route.return_value = {
			"mode": "clarify",
			"question": "Which supplier should the Purchase Order be for?",
		}
		r = self.say("can you make a new po for our office please")
		self.assertEqual((self.form()["doctype"], self.pending()["key"]), ("Purchase Order", "supplier"))
		self.assertNotIn(
			"Which supplier should the Purchase Order be for?", [m.get("text") for m in r["messages"]]
		)
		self.assertIn("__create__", [o["value"] for o in self.pending()["options"]])

	def test_explicit_create_new_supplier_needs_its_own_confirmation(self):
		self.say("create purchase order")
		before = frappe.db.count("Supplier")
		self.click("__create__")
		self.assertEqual(self.form()["doctype"], "Supplier")
		self.assertEqual(self.pending()["key"], "supplier_name")
		r = self.say("AIG Fresh Vendor")
		self.assertEqual(self.preview(r)["title"], "New Supplier")
		self.assertEqual(frappe.db.count("Supplier"), before)  # nothing created before the confirmation
		r = self.click("__create__")
		self.assertEqual(frappe.db.count("Supplier"), before + 1)
		self.assertEqual(self.form()["doctype"], "Purchase Order")  # back to the order, supplier filled in
		new = frappe.db.get_value("Supplier", {"supplier_name": "AIG Fresh Vendor"})
		self.assertEqual(self.form()["values"]["supplier"], new)
		self.assertEqual(self.pending()["key"], "schedule_date")

	def test_explicit_create_new_customer_for_a_sales_order(self):
		self.say("create sales order")
		self.assertEqual(self.pending()["key"], "customer")
		before = frappe.db.count("Customer")
		self.click("__create__")
		r = self.say("AIG Brand New Buyer")
		self.assertEqual(self.preview(r)["title"], "New Customer")
		self.click("__create__")
		self.assertEqual(frappe.db.count("Customer"), before + 1)
		self.assertEqual(self.form()["doctype"], "Sales Order")
		self.assertEqual(self.form()["labels"]["customer"], "AIG Brand New Buyer")

	def test_unknown_name_is_never_created_automatically(self):
		self.say("create purchase order")
		before = frappe.db.count("Supplier")
		r = self.say("Zzq Unknown Vendor")
		q = self.pending()
		self.assertEqual((q["kind"], q["key"]), ("form_field", "supplier"))
		self.assertIn("I couldn't find a supplier called “Zzq Unknown Vendor”", self.last(r)["text"])
		self.assertIn("__create__", [o["value"] for o in q["options"]])
		self.assertTrue(len(q["options"]) > 1)  # existing suppliers are offered too
		self.assertEqual(frappe.db.count("Supplier"), before)

	def test_existing_customer_by_position_and_by_name(self):
		self.say("create sales order")
		options = [o for o in self.pending()["options"] if not o["value"].startswith("__")]
		self.say("no, use the second customer")
		self.assertEqual(self.form()["values"]["customer"], options[1]["value"])
		self.say("change customer to aig buyer one")  # case-insensitive name, keeps the rest
		self.assertEqual(self.form()["labels"]["customer"], "AIG Buyer One")

	def test_create_new_is_only_offered_when_the_user_may_create_that_master(self):
		email = "aig-purchaser@example.com"
		if not frappe.db.exists("User", email):
			frappe.get_doc(
				{
					"doctype": "User",
					"email": email,
					"first_name": "AIG Purchaser",
					"send_welcome_email": 0,
					"roles": [{"role": "System Manager"}, {"role": "Purchase User"}],
				}
			).insert(ignore_permissions=True)
		frappe.set_user(email)
		try:
			self.assertFalse(frappe.has_permission("Supplier", "create"))
			s = chat.new_conversation()["session"]
			chat.send_message(session=s, text="create purchase order")
			q = json.loads(frappe.db.get_value("AI Chat Session", s, "state"))["pending"]
			self.assertEqual(q["key"], "supplier")
			self.assertNotIn("__create__", [o["value"] for o in q["options"]])
			r = chat.send_message(session=s, text="Zzq Unknown Vendor")
			self.assertNotIn("create", r["messages"][-1]["text"].lower().split("couldn't find")[1])
		finally:
			frappe.set_user("Administrator")

	# ---- changing the request ------------------------------------------------------------------
	def test_switching_document_type_keeps_the_values(self):
		self.po_at_preview()
		r = self.say("I meant Purchase Receipt, not Purchase Order.")
		form = self.form()
		self.assertEqual(form["doctype"], "Purchase Receipt")
		self.assertEqual(form["values"]["supplier"], self.supplier)
		self.assertEqual(
			[(i["item_code"], i["qty"], i["rate"]) for i in form["items"]], [("AIG-BOTTLE", 5, 20)]
		)
		self.assertIn("Switched to a Purchase Receipt", "\n".join(m.get("text") or "" for m in r["messages"]))
		self.assertEqual(self.preview(r)["title"], "New Purchase Receipt (Draft)")

	def test_start_over_and_cancel(self):
		self.po_at_preview()
		self.say("start over")
		form = self.form()
		self.assertEqual(
			(form["doctype"], form["items"], form["values"].get("supplier")), ("Purchase Order", [], None)
		)
		self.assertEqual(self.pending()["key"], "supplier")
		before = frappe.db.count("Purchase Order")
		r = self.say("Cancel this.")
		self.assertIn("Nothing was created", self.last(r)["text"])
		self.assertEqual(self.state()["forms"], [])
		self.assertEqual(frappe.db.count("Purchase Order"), before)

	def test_fields_the_form_does_not_have_get_a_targeted_answer(self):
		self.say("create purchase invoice")
		self.click(self.supplier)
		r = self.say("set target warehouse to Stores")
		self.assertIn(
			"A Purchase Invoice created here has no warehouse",
			self.last(r)["text"] if not self.last(r).get("question") else r["messages"][-2]["text"],
		)
		r = self.say("set colour to red")
		self.assertIn(
			"I can't change “colour” on a Purchase Invoice here. You can change: Supplier",
			"\n".join(m.get("text") or "" for m in r["messages"]),
		)
		self.assertEqual(self.form()["values"]["supplier"], self.supplier)  # nothing else changed
		self.route.assert_not_called()

	# ---- state, failures and loops --------------------------------------------------------------
	def test_provider_failure_keeps_the_pending_document(self):
		self.say("create purchase order")
		self.click(self.supplier)
		before = self.pending()
		self.route.side_effect = AIProviderError("unavailable")
		r = self.say("what is a cost center?")
		texts = [m.get("text") for m in r["messages"]]
		self.assertIn("The AI service is temporarily unavailable. Please try again in a moment.", texts)
		self.assertIn("Back to the Purchase Order:", texts)
		after = self.pending()
		self.assertEqual((after["kind"], after["key"]), (before["kind"], before["key"]))
		self.assertEqual(self.form()["values"]["supplier"], self.supplier)

	def test_repeated_invalid_answers_trigger_a_recovery_message(self):
		self.say("create purchase order")
		self.click(self.supplier)
		self.say(add_days(nowdate(), 7))
		self.say("AIG Bottle")
		first = self.say("lots")
		self.assertNotIn("I still don't have a value", self.last(first)["text"])
		self.say("plenty")
		r = self.say("many")
		last = self.last(r)
		self.assertIn("I still don't have a value I can use here", last["text"])
		self.assertIn("How many of AIG Bottle?", last["text"])  # says what is still missing
		self.assertIn("__cancel__", [o["value"] for o in last["question"]["options"]])
		self.assertEqual(self.pending()["key"], "qty")  # nothing was discarded
		r = self.say("3")
		self.assertEqual(self.form()["draft_item"]["qty"], 3)

	def test_asking_the_first_question_again_after_cancel_is_not_a_loop(self):
		for _ in range(4):
			r = self.say("create purchase order")
			self.assertNotIn("I still don't have a value", self.last(r)["text"])
			self.say("cancel")
		for _ in range(3):
			r = self.say("start over") if self.state().get("forms") else self.say("create purchase order")
			self.assertNotIn("I still don't have a value", self.last(r)["text"])

	def test_use_name_as_field_is_an_answer_not_a_refusal(self):
		self.say("create purchase order")
		r = self.say(f"use {self.supplier} as supplier")
		self.assertNotIn("I can't change", "\n".join(m.get("text") or "" for m in r["messages"]))

	def test_pending_document_survives_reload(self):
		self.say("create purchase order")
		self.click(self.supplier)
		pending = self.pending()
		reloaded = chat.get_session(self.s)  # panel reopened / page reloaded
		self.assertEqual(reloaded["pending_id"], pending["id"])
		self.assertTrue(any(m.get("question", {}).get("id") == pending["id"] for m in reloaded["messages"]))
		self.say(add_days(nowdate(), 7))  # continues where it was
		self.assertEqual(self.pending()["key"], "item_code")
		self.assertEqual(self.form()["values"]["supplier"], self.supplier)
