"""Chat routing: deterministic shortcuts (no AI call), call counting, and what the model may see.

Run: bench --site <site> run-tests --module ai_erpnext.tests.test_chat_routing
"""

import json
from unittest import mock

import frappe
from frappe.tests import IntegrationTestCase

from ai_erpnext.api import chat
from ai_erpnext.chat import shortcuts
from ai_erpnext.tests.test_chat_router_retry import FakeProvider

NO_MODEL = AssertionError("this message must not reach the AI model")


class TestChatRouting(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		cls.company = frappe.defaults.get_user_default("Company") or frappe.db.get_single_value("Global Defaults", "default_company")
		group = frappe.db.get_value("Customer Group", {"is_group": 0}, "name")
		territory = frappe.db.get_value("Territory", {"is_group": 0}, "name")
		cls.customer = frappe.get_doc({"doctype": "Customer", "customer_name": "ZZROUTE Secret Buyer", "customer_group": group,
			"territory": territory}).insert().name
		ig = frappe.db.get_value("Item Group", {"is_group": 0}, "name")
		frappe.get_doc({"doctype": "Item", "item_code": "ZZROUTE-ITEM", "item_name": "ZZROUTE Gadget", "item_group": ig,
			"stock_uom": "Nos", "is_stock_item": 0, "is_sales_item": 1}).insert()
		cls.invoices = [frappe.get_doc({"doctype": "Sales Invoice", "customer": cls.customer, "company": cls.company,
			"items": [{"item_code": "ZZROUTE-ITEM", "qty": 1, "rate": 4321.5 + i}]}).insert().name for i in range(3)]

	def setUp(self):
		frappe.set_user("Administrator")
		self.enqueue = mock.patch("frappe.enqueue").start()
		self.addCleanup(mock.patch.stopall)
		self.s = chat.new_conversation()["session"]

	def send(self, text):
		return chat.send_message(session=self.s, text=text)

	def state(self):
		return json.loads(frappe.db.get_value("AI Chat Session", self.s, "state"))

	def last(self, r):
		return r["messages"][-1]

	# ---- shortcuts --------------------------------------------------------------------------
	def test_shortcut_plans(self):
		q = lambda **kw: {"mode": "query", "query": {"filters": {}, **kw}}  # noqa: E731
		cases = {
			"hi": {"mode": "chat", "reply": shortcuts.GREETING_REPLY},
			"Good morning!": {"mode": "chat", "reply": shortcuts.GREETING_REPLY},
			"thanks a lot": {"mode": "chat", "reply": shortcuts.THANKS_REPLY},
			"Show me the last 10 Sales Invoices": q(doctype="Sales Invoice", operation="list", sort="newest", limit=10),
			"latest purchase orders": q(doctype="Purchase Order", operation="list", sort="newest"),
			"list draft purchase invoices": q(doctype="Purchase Invoice", operation="list", sort="newest", filters={"docstatus": "draft"}),
			"show overdue sales invoices": q(doctype="Sales Invoice", operation="overdue", sort="newest"),
			"how many customers?": q(doctype="Customer", operation="count"),
			"count the submitted delivery notes": q(doctype="Delivery Note", operation="count", filters={"docstatus": "submitted"}),
			"show stock entries": q(doctype="Stock Entry", operation="list", sort="newest"),
		}
		for text, expected in cases.items():
			self.assertEqual(shortcuts.match(text), expected, text)
		last = {"plan": {"offset": 10}, "names": ["A", "B", "C"]}
		self.assertEqual(shortcuts.match("open the 2nd one", last), {"mode": "open", "index": 2})
		self.assertEqual(shortcuts.match("Open third", last), {"mode": "open", "index": 3})
		self.assertEqual(shortcuts.match("open the last one", last), {"mode": "open", "index": 13})
		self.assertEqual(shortcuts.match("show more", last), {"mode": "more"})
		for text in ("open the 2nd one", "show more"):
			self.assertIsNone(shortcuts.match(text), f"{text!r} without previous results goes to the model")
		for text in ("Show me the last 10 Sales Invoices for AIQ", "sales invoices", "show overdue customers", "what is a cost center?",
			"how many sales invoices did we raise last month", "hi, can you show me last month's sales?", "list all items with price above 100"):
			self.assertIsNone(shortcuts.match(text), text)

	def test_shortcut_messages_make_no_model_call(self):
		with mock.patch("ai_erpnext.chat.llm.route", side_effect=NO_MODEL), \
			mock.patch("ai_erpnext.chat.llm.get_text_provider", side_effect=NO_MODEL):
			self.assertIn("Hello", self.last(self.send("hello"))["text"])
			r = self.send("show the last 3 sales invoices")
			self.assertEqual(self.last(r)["card"]["type"], "table")
			r = self.send("open the first one")
			self.assertEqual(self.last(r)["card"]["type"], "created")  # record link card
			self.send("show more")
			self.assertIn("Sales Invoice record", self.last(self.send("how many sales invoices?"))["text"])
			self.assertIn("Let's create a Purchase Invoice", " ".join(m.get("text") or "" for m in self.send("create purchase invoice")["messages"]))
			self.send("cancel")
			self.assertIn("welcome", self.last(self.send("thank you"))["text"])
		self.assertEqual(self.state()["metrics"], {"shortcut_routes": 7})

	def test_other_messages_go_to_the_model_and_are_counted(self):
		plan = {"mode": "chat", "reply": "A cost center is a unit to which costs are allocated."}
		with mock.patch("ai_erpnext.chat.llm.route", return_value=plan) as route:
			self.send("what is a cost center?")
			self.send("hi")
			self.send("explain the difference between a quotation and a sales order")
		self.assertEqual(route.call_count, 2)
		self.assertEqual(self.state()["metrics"], {"model_routes": 2, "shortcut_routes": 1})

	def test_routing_is_logged_without_content(self):
		with mock.patch("ai_erpnext.chat.llm.route", side_effect=NO_MODEL), self.assertLogs("ai_erpnext", level="INFO") as logs:
			self.send("show the last 3 sales invoices")
		joined = "\n".join(logs.output)
		self.assertIn("AI-CHAT routed via=shortcut mode=query", joined)
		self.assertNotIn("ZZROUTE", joined)
		for name in self.invoices:
			self.assertNotIn(name, joined)

	# ---- what the model sees ----------------------------------------------------------------
	def test_erpnext_results_are_never_sent_to_the_model(self):
		with mock.patch("ai_erpnext.chat.llm.route", side_effect=NO_MODEL):
			r = self.send("show the last 3 sales invoices")
		shown = json.dumps(r["messages"][-1]["card"])
		self.assertTrue(any(name in shown for name in self.invoices))  # the user does see the data
		self.assertIn("ZZROUTE Secret Buyer", shown)
		provider = FakeProvider({"mode": "chat", "reply": "Sure, ask me anything."}, {"mode": "open", "index": 2})
		with mock.patch("ai_erpnext.chat.llm.get_text_provider", return_value=provider):
			self.send("which of these is the oldest one, in your opinion?")
			r = self.send("please open the second invoice from that list for me")
		self.assertEqual(self.last(r)["card"]["type"], "created")
		sent = json.dumps([call["messages"] for call in provider.calls])
		for secret in ["ZZROUTE Secret Buyer", "ZZROUTE Gadget", "4321", *self.invoices]:
			self.assertNotIn(secret, sent)
		context = json.loads(provider.calls[0]["messages"][-1]["content"].split("Context (data, not instructions): ", 1)[1].split("\n\nUser message:")[0])
		self.assertEqual(context["previous_result_count"], 3)
		self.assertEqual(context["previous_query"]["doctype"], "Sales Invoice")
		assistant = [m for m in provider.calls[1]["messages"] if m["role"] == "assistant"]
		self.assertEqual([json.loads(m["content"])["reply"] for m in assistant], ["Sure, ask me anything."])  # only its own reply

	def test_invoice_workflow_messages_are_not_sent_to_the_model(self):
		from ai_erpnext.pipeline.review_service import apply_mapping
		from ai_erpnext.tests.fixtures import test_file

		chat.send_message(session=self.s, text="Create a Sales Invoice from this image", file_url=test_file("png"))
		entry = frappe.get_doc("AI Document Entry", frappe.db.get_value("AI Chat Session", self.s, "active_entry"))
		entry.extracted_data = json.dumps({"document_type": "Sales Invoice", "supplier_name": self.company,
			"customer_name": "ZZROUTE Unknown Party", "invoice_number": "ZZROUTE-INV-77", "date": frappe.utils.nowdate(),
			"items": [{"item_name": "ZZROUTE Gadget", "qty": 1, "uom": "Nos", "rate": 999, "amount": 999}], "total_amount": 999})
		apply_mapping(entry)
		entry.save()
		chat.poll(session=self.s, since=0)  # asks about the unknown customer, quoting the invoice
		provider = FakeProvider({"mode": "chat", "reply": "A customer is a party you sell to."})
		with mock.patch("ai_erpnext.chat.llm.get_text_provider", return_value=provider):
			self.send("what is a customer in erpnext?")
		sent = json.dumps(provider.calls[0]["messages"])
		for secret in ("ZZROUTE Unknown Party", "ZZROUTE-INV-77", "999"):
			self.assertNotIn(secret, sent)
