"""Guided record creation from the chat (no image): ask required fields one by one, confirm,
create a Draft. The router model is mocked; everything else runs against the site database in a
rolled-back transaction.

Run: bench --site <site> run-tests --module ai_erpnext.tests.test_chat_forms
"""

import json
from unittest import mock

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_days, nowdate

from ai_erpnext.ai.errors import AIProviderError
from ai_erpnext.api import chat

ROUTE = "ai_erpnext.chat.llm.route"


def create_plan(doctype, values=None, items=None):
	return {"mode": "create", "doctype": doctype, "values": values or {}, "items": items or []}


class TestChatForms(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		ig = frappe.db.get_value("Item Group", {"is_group": 0}, "name")
		frappe.get_doc({"doctype": "Item", "item_code": "AIF-ITEM", "item_name": "AIF Widget", "item_group": ig, "stock_uom": "Nos",
			"is_stock_item": 0, "is_sales_item": 1, "is_purchase_item": 1}).insert()
		group = frappe.db.get_value("Supplier Group", {"is_group": 0}, "name")
		cls.supplier = frappe.get_doc({"doctype": "Supplier", "supplier_name": "AIF Summit Supplies", "supplier_group": group}).insert().name
		cgroup = frappe.db.get_value("Customer Group", {"is_group": 0}, "name")
		territory = frappe.db.get_value("Territory", {"is_group": 0}, "name")
		cls.customer = frappe.get_doc({"doctype": "Customer", "customer_name": "AIF Existing Buyer", "customer_group": cgroup, "territory": territory}).insert().name

	def setUp(self):
		frappe.set_user("Administrator")
		self.route = mock.patch(ROUTE, return_value={"mode": "chat", "reply": "ok"}).start()
		mock.patch("frappe.enqueue").start()
		self.addCleanup(mock.patch.stopall)
		self.s = chat.new_conversation()["session"]

	# helpers ------------------------------------------------------------------------------------
	def say(self, text, plan=None):
		if plan is not None:
			self.route.return_value = plan
		return chat.send_message(session=self.s, text=text)

	def pending(self):
		# server-side pending question (includes the internal field key the browser never sees)
		return json.loads(frappe.db.get_value("AI Chat Session", self.s, "state") or "{}").get("pending")

	def click(self, value, client_id=None):
		q = self.pending()
		return chat.send_message(session=self.s, action=json.dumps({"question_id": q["id"], "value": value, "label": value}), client_id=client_id)

	def last_text(self):
		return chat.get_session(self.s)["messages"][-1].get("text") or ""

	def created(self, r):
		return next((m["card"] for m in r["messages"] if (m.get("card") or {}).get("type") == "created"), None)

	# tests --------------------------------------------------------------------------------------
	def test_purchase_invoice_step_by_step(self):
		before = frappe.db.count("Purchase Invoice")
		self.say("create purchase invoice", create_plan("Purchase Invoice"))
		q = self.pending()
		self.assertEqual((q["kind"], q["key"]), ("form_field", "supplier"))
		self.assertIn("__create__", [o["value"] for o in q["options"]])
		self.say("AIF Summit Supplies")
		self.assertEqual(self.pending()["key"], "bill_no")
		self.click("__skip__")
		q = self.pending()
		self.assertEqual((q["kind"], q["key"]), ("form_item", "item_code"))
		self.say("AIF-ITEM")
		self.assertEqual(self.pending()["key"], "qty")
		self.say("abc")
		self.assertIn("greater than 0", " ".join(m.get("text") or "" for m in chat.get_session(self.s)["messages"][-3:]))
		self.say("-2")
		self.assertEqual(self.pending()["key"], "qty")
		self.say("3")
		self.assertEqual(self.pending()["key"], "rate")
		self.say("400")
		self.assertEqual(self.pending()["kind"], "form_more_items")
		self.click("__done__")
		q = self.pending()
		self.assertEqual(q["kind"], "form_confirm")
		self.assertEqual(frappe.db.count("Purchase Invoice"), before)  # nothing created before confirmation
		r = self.click("__create__", client_id="pi-1")
		card = self.created(r)
		pi = frappe.get_doc("Purchase Invoice", card["name"])
		self.assertEqual((pi.docstatus, pi.supplier, pi.items[0].item_code, pi.items[0].qty, pi.items[0].rate), (0, self.supplier, "AIF-ITEM", 3, 400))
		self.assertIn({"label": "Status", "value": "Draft"}, card["rows"])
		again = chat.send_message(session=self.s, action=json.dumps({"question_id": q["id"], "value": "__create__"}), client_id="pi-1")
		self.assertEqual(again["messages"], [])
		self.assertEqual(frappe.db.count("Purchase Invoice"), before + 1)

	def test_prefilled_sales_order_asks_only_missing_fields(self):
		plan = create_plan("Sales Order", {"party": "AIF Existing Buyer"}, [{"item": "AIF-ITEM", "qty": 2, "rate": 1000}])
		self.say("Create a sales order for AIF Existing Buyer for 2 AIF-ITEM at 1000", plan)
		q = self.pending()
		self.assertEqual(q["key"], "delivery_date")
		self.assertEqual(q["input"], "date")
		self.say(add_days(nowdate(), 10))
		self.assertEqual(self.pending()["kind"], "form_more_items")
		self.click("__done__")
		r = self.click("__create__")
		so = frappe.get_doc("Sales Order", self.created(r)["name"])
		self.assertEqual((so.docstatus, so.customer, so.items[0].qty, so.items[0].rate, str(so.items[0].delivery_date)),
			(0, self.customer, 2, 1000, add_days(nowdate(), 10)))

	def test_missing_customer_created_inside_sales_invoice_after_confirmation(self):
		self.say("new sales invoice", create_plan("Sales Invoice"))
		before = frappe.db.count("Customer")
		self.say("AIF Brand New Buyer")
		q = self.pending()
		self.assertIn("couldn't find", self.last_text())
		self.click("__create__")
		q = self.pending()
		self.assertEqual(q["kind"], "form_confirm")  # name came from the user's answer; type has a default
		self.assertEqual(frappe.db.count("Customer"), before)
		self.say("change customer type to Individual")
		self.click("__create__")
		self.assertEqual(frappe.db.count("Customer"), before + 1)
		new = frappe.db.get_value("Customer", {"customer_name": "AIF Brand New Buyer"}, ["name", "customer_type"], as_dict=True)
		self.assertEqual(new.customer_type, "Individual")
		self.assertEqual(self.pending()["kind"], "form_item")  # back in the Sales Invoice with the customer set
		self.say("AIF Widget")
		self.say("1")
		self.say("25")
		self.click("__done__")
		r = self.click("__create__")
		si = frappe.get_doc("Sales Invoice", self.created(r)["name"])
		self.assertEqual((si.docstatus, si.customer), (0, new.name))

	def test_partial_name_is_only_a_suggestion(self):
		self.say("create purchase order", create_plan("Purchase Order"))
		self.say("Summit")
		q = self.pending()
		self.assertEqual(q["key"], "supplier")
		self.assertIn("Several", self.last_text())
		self.assertIn(self.supplier, [o["value"] for o in q["options"]])
		self.click(self.supplier)
		self.assertEqual(self.pending()["key"], "schedule_date")

	def test_corrections_and_remove_row(self):
		plan = create_plan("Purchase Invoice", {"party": "AIF Summit Supplies", "bill_no": "B-1"}, [{"item": "AIF-ITEM", "qty": 1, "rate": 10}])
		self.say("create purchase invoice from AIF Summit Supplies bill B-1 1 AIF-ITEM at 10", plan)
		self.click("__done__")
		self.assertEqual(self.pending()["kind"], "form_confirm")
		self.say("row 1 qty 5")
		card = next(m["card"] for m in reversed(chat.get_session(self.s)["messages"]) if (m.get("card") or {}).get("type") == "form_preview")
		self.assertEqual(card["items"][0]["qty"], 5)
		self.say("change supplier invoice number to B-2")
		card = next(m["card"] for m in reversed(chat.get_session(self.s)["messages"]) if (m.get("card") or {}).get("type") == "form_preview")
		self.assertIn({"label": "Supplier invoice number", "value": "B-2"}, card["rows"])
		self.say("remove row 1")
		self.assertEqual(self.pending()["key"], "item_code")  # a document needs at least one item

	def test_cancel_top_level_and_nested(self):
		before = {dt: frappe.db.count(dt) for dt in ("Sales Invoice", "Customer")}
		self.say("create sales invoice", create_plan("Sales Invoice"))
		self.say("AIF Another New Buyer")
		self.click("__create__")
		self.assertEqual(self.pending()["kind"], "form_confirm")
		self.click("__cancel__")  # cancels only the nested customer
		self.assertIn("won't create the customer", " ".join(m.get("text") or "" for m in chat.get_session(self.s)["messages"][-3:]))
		self.assertEqual(self.pending()["key"], "customer")
		self.say("cancel")
		self.assertIn("Nothing was created", self.last_text())
		self.assertEqual({dt: frappe.db.count(dt) for dt in before}, before)

	def test_permission_is_checked(self):
		email = "aif-limited@example.com"
		frappe.get_doc({"doctype": "User", "email": email, "first_name": "Lim", "send_welcome_email": 0,
			"roles": [{"role": "System Manager"}]}).insert(ignore_permissions=True)
		from ai_erpnext.chat import engine, forms

		frappe.set_user(email)
		ctx = engine.Ctx(chat.S.get_session())
		with mock.patch("frappe.has_permission", side_effect=lambda dt, *a, **k: dt != "Purchase Order"):
			forms.start(ctx, "Purchase Order")
		self.assertIn("don't have permission", ctx.msgs[-1]["text"])
		self.assertFalse(ctx.st.get("forms"))

	def test_works_without_ai_service_and_refuses_unsupported(self):
		self.route.side_effect = AIProviderError("rate_limited")
		self.say("create sales order")
		self.assertEqual(self.pending()["key"], "customer")
		self.say("cancel")
		self.route.side_effect = None
		self.say("create a payment entry", create_plan("Payment Entry"))
		self.assertIn("can't create that", self.last_text())
		self.say("submit all drafts", {"mode": "unsupported", "reply": "I can't submit documents."})
		self.assertIn("never submit", self.last_text())

	def test_duplicate_master_names_are_refused(self):
		self.say("create customer AIF Existing Buyer", create_plan("Customer", {"customer_name": "AIF Existing Buyer"}))
		self.assertIn("already exists", " ".join(m.get("text") or "" for m in chat.get_session(self.s)["messages"][-3:]))
		self.assertEqual(self.pending()["key"], "customer_name")

	def test_item_master_creation(self):
		before = frappe.db.count("Item")
		self.say("create item", create_plan("Item"))
		self.assertEqual(self.pending()["key"], "item_code")
		self.say("AIF-NEW-1")
		self.assertEqual(self.pending()["key"], "item_group")
		self.say("Services")
		q = self.pending()
		self.assertEqual([o["value"] for o in q["options"]], ["1", "0"])
		self.click("0")
		self.assertEqual(self.pending()["kind"], "form_confirm")
		self.assertEqual(frappe.db.count("Item"), before)
		self.click("__create__")
		item = frappe.get_doc("Item", "AIF-NEW-1")
		self.assertEqual((item.item_group, item.is_stock_item, item.item_name), ("Services", 0, "AIF-NEW-1"))

	def test_erpnext_notices_go_to_chat_not_popups(self):
		from ai_erpnext.chat import forms

		frappe.local.message_log = [json.dumps({"message": "<b>Row 1:</b> Expense Head changed", "title": "Expense Head Changed"})]
		self.assertEqual(forms.erpnext_notes(), ["Row 1: Expense Head changed"])
		self.assertEqual(frappe.local.message_log, [])
