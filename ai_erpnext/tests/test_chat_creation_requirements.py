"""Mandatory fields and warehouses when creating documents from the chat.

Reproduces "ERPNext didn't accept the Purchase Order: Row #1: Warehouse is mandatory for stock
Item Bottle" and checks every supported transaction type against ERPNext's real validation.
Test companies, items and users are created in the test transaction and rolled back; the
router model is mocked, no AI provider is called, and Document.submit fails the test if used.

Run: bench --site <site> run-tests --module ai_erpnext.tests.test_chat_creation_requirements
"""

import json
from unittest import mock

import frappe
from frappe.model.document import Document
from frappe.tests import IntegrationTestCase
from frappe.utils import add_days, nowdate

from ai_erpnext.api import chat
from ai_erpnext.chat import requirements
from ai_erpnext.mapping.warehouse import configured_default_warehouse

ROUTE = "ai_erpnext.chat.llm.route"
PLAIN, OTHER = "AIW Plain Co", "AIW Other Co"  # companies without an applicable default warehouse


def no_submit(*args, **kwargs):
	raise AssertionError("submit() must never be called by the assistant")


class TestChatCreationRequirements(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		currency = frappe.db.get_value("Company", frappe.db.get_single_value("Global Defaults", "default_company"), "default_currency") or "INR"
		country = frappe.db.get_single_value("System Settings", "country") or "India"
		for name, abbr in ((PLAIN, "AIWP"), (OTHER, "AIWO")):
			if not frappe.db.exists("Company", name):
				frappe.get_doc({"doctype": "Company", "company_name": name, "abbr": abbr, "default_currency": currency, "country": country,
					"create_chart_of_accounts_based_on": "Standard Template", "chart_of_accounts": "Standard"}).insert()
		cls.stores, cls.goods = f"Stores - AIWP", f"Finished Goods - AIWP"
		cls.other_stores = "Stores - AIWO"
		for wh in (cls.stores, cls.goods, cls.other_stores):
			assert frappe.db.exists("Warehouse", wh), wh  # created with the company by ERPNext
		# These companies must have no default warehouse. Since ERPNext v16 moved the default to Company,
		# a new company gets its "Stores" warehouse as default; clear it (older sites keep the default in
		# Stock Settings, which belongs to another company).
		if frappe.get_meta("Company").has_field("default_warehouse"):
			for name in (PLAIN, OTHER):
				frappe.db.set_value("Company", name, "default_warehouse", None)
				frappe.clear_document_cache("Company", name)
		for name in (PLAIN, OTHER):
			default = configured_default_warehouse(name)
			assert not default or frappe.db.get_value("Warehouse", default, "company") != name, default
		ig = frappe.db.get_value("Item Group", {"is_group": 0}, "name")
		item = {"doctype": "Item", "item_group": ig, "stock_uom": "Nos", "is_sales_item": 1, "is_purchase_item": 1}
		frappe.get_doc({**item, "item_code": "AIW-BOTTLE", "item_name": "AIW Bottle", "is_stock_item": 1}).insert()
		frappe.get_doc({**item, "item_code": "AIW-JAR", "item_name": "AIW Jar", "is_stock_item": 1}).insert()
		frappe.get_doc({**item, "item_code": "AIW-SVC", "item_name": "AIW Service", "is_stock_item": 0}).insert()
		frappe.get_doc({**item, "item_code": "AIW-DEF", "item_name": "AIW Defaulted", "is_stock_item": 1,
			"item_defaults": [{"company": PLAIN, "default_warehouse": cls.goods}]}).insert()
		sg = frappe.db.get_value("Supplier Group", {"is_group": 0}, "name")
		cls.supplier = frappe.get_doc({"doctype": "Supplier", "supplier_name": "AIW Supplier", "supplier_group": sg}).insert().name
		cg = frappe.db.get_value("Customer Group", {"is_group": 0}, "name")
		territory = frappe.db.get_value("Territory", {"is_group": 0}, "name")
		cls.customer = frappe.get_doc({"doctype": "Customer", "customer_name": "AIW Customer", "customer_group": cg,
			"territory": territory}).insert().name

	def setUp(self):
		frappe.set_user("Administrator")
		self.route = mock.patch(ROUTE).start()
		mock.patch("frappe.enqueue").start()
		for target in ("submit", "_submit"):
			mock.patch.object(Document, target, no_submit).start()
		self.addCleanup(mock.patch.stopall)
		self.s = chat.new_conversation()["session"]

	# helpers ----------------------------------------------------------------------------------
	def start(self, doctype, items, company=PLAIN, **values):
		party = {"Purchase Order": "supplier", "Purchase Invoice": "supplier", "Purchase Receipt": "supplier"}.get(doctype, "customer")
		values = {party if doctype != "Quotation" else "party_name": self.supplier if party == "supplier" else self.customer,
			"company": company, **values}
		if doctype == "Purchase Order":
			values.setdefault("schedule_date", add_days(nowdate(), 7))
		if doctype == "Sales Order":
			values.setdefault("delivery_date", add_days(nowdate(), 7))
		self.route.return_value = {"mode": "create", "doctype": doctype, "values": values,
			"items": [{"item": code, "qty": qty, "rate": 10} for code, qty in items]}
		chat.send_message(session=self.s, text=f"create a {doctype.lower()} with some items, details as given")
		return self.walk()

	def last(self):
		return next(m for m in reversed(chat.get_session(self.s)["messages"]) if m.get("question"))

	def pending(self):
		return json.loads(frappe.db.get_value("AI Chat Session", self.s, "state"))["pending"]

	def walk(self):
		"""Answer the routine questions (more items? supplier invoice no.?) until something else is asked."""
		for _ in range(10):
			m = self.last()
			q, key = m["question"], (self.pending() or {}).get("key")
			if q["kind"] == "form_more_items":
				self.click("__done__")
			elif q["kind"] == "form_field" and key == "bill_no":
				self.click("__skip__")
			else:
				return m
		self.fail("stuck in routine questions")

	def click(self, value):
		q = self.last()["question"]
		return chat.send_message(session=self.s, action=json.dumps({"question_id": q["id"], "value": value, "label": value}))

	def say(self, text):
		return chat.send_message(session=self.s, text=text)

	def preview(self):
		m = self.last()
		self.assertEqual(m["question"]["kind"], "form_confirm", m.get("text"))
		return m["card"]

	def create(self):
		r = self.click("__create__")
		card = next((m["card"] for m in r["messages"] if (m.get("card") or {}).get("type") == "created"), None)
		self.assertIsNotNone(card, json.dumps([[m["role"], m.get("text"), (m.get("question") or {}).get("kind")] for m in chat.get_session(self.s)["messages"][-6:]], indent=0))
		doc = frappe.get_doc(card["doctype"], card["name"])
		self.assertEqual(doc.docstatus, 0)
		return doc

	# ---- the reported failure -------------------------------------------------------------------
	def test_warehouse_is_mandatory_for_stock_item_reproduced_in_erpnext(self):
		"""The exact ERPNext failure the chat used to run into (no warehouse, no applicable default)."""
		with self.assertRaises(frappe.ValidationError) as ctx:
			frappe.get_doc({"doctype": "Purchase Order", "supplier": self.supplier, "company": PLAIN, "transaction_date": nowdate(),
				"schedule_date": add_days(nowdate(), 7), "items": [{"item_code": "AIW-BOTTLE", "qty": 3, "rate": 10,
				"schedule_date": add_days(nowdate(), 7)}]}).insert()
		self.assertIn("Warehouse is mandatory for stock Item", str(ctx.exception))
		frappe.local.message_log = []

	def test_po_without_default_asks_for_the_warehouse_before_the_preview(self):
		m = self.start("Purchase Order", [("AIW Bottle", 3)])
		self.assertEqual(m["question"]["kind"], "form_warehouse")
		self.assertIn("received into", m["text"])
		self.assertIn("row 1 (AIW Bottle)", m["text"])
		options = [o["value"] for o in m["question"]["options"]]
		self.assertIn(self.stores, options)
		self.assertTrue(all(frappe.db.get_value("Warehouse", o, "company") == PLAIN for o in options))  # never another company's
		self.click(self.stores)
		card = self.preview()
		self.assertEqual(card["items"][0]["warehouse"], self.stores)
		self.assertIsNone(card["items"][0]["warehouse_note"])
		before = frappe.db.count("Purchase Order")
		po = self.create()
		self.assertEqual(po.items[0].warehouse, self.stores)
		self.assertEqual(frappe.db.count("Purchase Order"), before + 1)

	def test_warehouse_answer_in_plain_words(self):
		self.start("Purchase Order", [("AIW Bottle", 3)])
		self.say("use Finished Goods warehouse")  # the warehouse's own name, phrased naturally
		self.assertEqual(self.preview()["items"][0]["warehouse"], self.goods)
		self.say("set target warehouse to Stores")  # a correction to every stock row, same company only
		self.assertEqual(self.preview()["items"][0]["warehouse"], self.stores)

	def test_invalid_and_other_company_warehouses_are_refused(self):
		self.start("Purchase Order", [("AIW Bottle", 3)])
		for wrong in ("No Such Warehouse", self.other_stores):
			r = self.say(wrong)
			self.assertIn("couldn't find a warehouse of AIW Plain Co", " ".join(m.get("text") or "" for m in r["messages"]))
			self.assertEqual(self.last()["question"]["kind"], "form_warehouse")
		self.say(self.stores)
		self.assertEqual(self.preview()["items"][0]["warehouse"], self.stores)
		self.say(f"row 1 warehouse {self.other_stores}")  # a correction is checked the same way
		self.assertEqual(self.preview()["items"][0]["warehouse"], self.stores)

	def test_configured_default_is_used_and_shown(self):
		m = self.start("Purchase Order", [("AIW Defaulted", 2)])
		self.assertEqual(m["question"]["kind"], "form_confirm", m["text"])  # nothing to ask
		item = self.preview()["items"][0]
		self.assertEqual((item["warehouse"], item["warehouse_note"]), (self.goods, "ERPNext default"))
		self.assertEqual(self.create().items[0].warehouse, self.goods)

	def test_rows_can_have_different_warehouses(self):
		m = self.start("Purchase Order", [("AIW Bottle", 1), ("AIW Service", 1), ("AIW Jar", 2)])
		self.assertIn("rows 1, 3 (AIW Bottle, AIW Jar)", m["text"])  # the service row needs none
		self.click(self.stores)
		self.say(f"row 3 warehouse {self.goods}")
		items = self.preview()["items"]
		self.assertEqual([i.get("warehouse") for i in items], [self.stores, None, self.goods])
		po = self.create()
		self.assertEqual([r.warehouse for r in po.items], [self.stores, None, self.goods])  # no warehouse forced on the service row

	def test_non_stock_items_get_no_warehouse_question(self):
		m = self.start("Purchase Order", [("AIW Service", 1)])
		self.assertEqual(m["question"]["kind"], "form_confirm")
		self.assertNotIn("warehouse", self.preview()["items"][0])
		r = self.say(f"row 1 warehouse {self.stores}")
		self.assertIn("doesn't need a warehouse", " ".join(m.get("text") or "" for m in r["messages"]))

	def test_header_warehouse_and_company_change(self):
		self.start("Purchase Order", [("AIW Bottle", 1), ("AIW Jar", 1)])
		self.click(self.stores)
		self.say(f"change warehouse to {self.goods}")  # one warehouse for all stock rows
		self.assertEqual([i["warehouse"] for i in self.preview()["items"]], [self.goods, self.goods])
		default_company = frappe.db.get_single_value("Global Defaults", "default_company")
		self.say(f"change company to {default_company}")  # warehouses belong to a company: chosen again for the new one
		m = self.last()
		if m["question"]["kind"] == "form_warehouse":
			self.assertTrue(all(frappe.db.get_value("Warehouse", o["value"], "company") == default_company for o in m["question"]["options"]))
		else:
			wh = [i["warehouse"] for i in self.preview()["items"]]
			self.assertTrue(all(frappe.db.get_value("Warehouse", w, "company") == default_company for w in wh))

	def test_each_supported_doctype_follows_its_own_warehouse_rule(self):
		expected = {"Purchase Order": "received into", "Purchase Receipt": "received into", "Sales Order": "delivered from",
			"Delivery Note": "delivered from", "Sales Invoice": None, "Purchase Invoice": None, "Quotation": None}
		for doctype, wording in expected.items():
			self.s = chat.new_conversation()["session"]
			m = self.start(doctype, [("AIW Bottle", 2)])
			if wording:
				self.assertEqual(m["question"]["kind"], "form_warehouse", doctype)
				self.assertIn(wording, m["text"], doctype)
				self.click(self.stores)
			else:  # invoices without update_stock and quotations never need one
				self.assertEqual(m["question"]["kind"], "form_confirm", f"{doctype}: {m['text']}")
			doc = self.create()
			self.assertEqual(doc.doctype, doctype)
			if wording:
				self.assertEqual(doc.items[0].warehouse, self.stores, doctype)
		# Material Request (a rate-less internal document) has its own rule, tested in test_coverage_phase_d
		self.assertEqual(set(requirements.WAREHOUSE_RULES), {d for d, w in expected.items() if w} | {"Material Request"})

	def test_sales_order_bundle_with_stock_items_needs_a_warehouse(self):
		ig = frappe.db.get_value("Item Group", {"is_group": 0}, "name")
		frappe.get_doc({"doctype": "Item", "item_code": "AIW-KIT", "item_name": "AIW Kit", "item_group": ig, "stock_uom": "Nos",
			"is_stock_item": 0, "is_sales_item": 1}).insert()
		frappe.get_doc({"doctype": "Product Bundle", "new_item_code": "AIW-KIT", "items": [{"item_code": "AIW-BOTTLE", "qty": 2}]}).insert()
		self.assertTrue(requirements.needs_warehouse("Sales Order", "AIW-KIT"))
		self.assertFalse(requirements.needs_warehouse("Quotation", "AIW-KIT"))
		m = self.start("Sales Order", [("AIW Kit", 1)])
		self.assertEqual(m["question"]["kind"], "form_warehouse")
		self.click(self.stores)
		self.assertEqual(self.create().items[0].warehouse, self.stores)

	# ---- mandatory fields -------------------------------------------------------------------
	def _make_mandatory(self, doctype, *fieldnames):
		"""A site customization (Property Setter, no schema change), undone after the test."""
		for fieldname in fieldnames:
			frappe.make_property_setter({"doctype": doctype, "fieldname": fieldname, "property": "reqd", "value": "1",
				"property_type": "Check"}, validate_fields_for_doctype=False)
			name = frappe.db.get_value("Property Setter", {"doc_type": doctype, "field_name": fieldname, "property": "reqd"})
			self.addCleanup(frappe.delete_doc, "Property Setter", name, force=True, ignore_permissions=True)
		frappe.clear_cache(doctype="Purchase Order")
		self.addCleanup(frappe.clear_cache, doctype="Purchase Order")

	def test_site_mandatory_parent_fields_are_asked_after_erpnext_refuses(self):
		self._make_mandatory("Purchase Order", "order_confirmation_no", "order_confirmation_date")
		self.start("Purchase Order", [("AIW Defaulted", 1)])
		before = frappe.db.count("Purchase Order")
		r = self.click("__create__")
		texts = " ".join(m.get("text") or "" for m in r["messages"])
		self.assertIn("ERPNext needs a few more details: Order Confirmation No, Order Confirmation Date", texts)
		self.assertEqual(frappe.db.count("Purchase Order"), before)  # nothing half-created
		self.say("OC-7781")
		self.say(nowdate())
		card = self.preview()
		self.assertIn({"label": "Order Confirmation No", "value": "OC-7781"}, card["rows"])  # kept and shown
		po = self.create()
		self.assertEqual(po.order_confirmation_no, "OC-7781")

	def test_custom_mandatory_field_is_asked_before_the_preview(self):
		meta_field = frappe.get_meta("Purchase Order").get_field("order_confirmation_no")
		with mock.patch.object(requirements, "inspect", return_value={"defaults": {}, "missing": ["order_confirmation_no"]}), \
			mock.patch.object(meta_field, "is_custom_field", 1, create=True):
			m = self.start("Purchase Order", [("AIW Service", 1)])
		self.assertEqual((m["question"]["kind"], self.pending()["key"]), ("form_field", "order_confirmation_no"))
		self.assertIn("ERPNext requires “Order Confirmation No”", m["text"])

	def test_row_field_the_chat_cannot_collect_is_explained_with_its_row(self):
		self._make_mandatory("Purchase Order Item", "supplier_part_no")
		self.start("Purchase Order", [("AIW Defaulted", 1)])
		before = (frappe.db.count("Purchase Order"), frappe.db.count("Purchase Order Item"))
		r = self.click("__create__")
		texts = " ".join(m.get("text") or "" for m in r["messages"])
		self.assertIn("Row #1", texts)
		self.assertIn("Supplier Part Number", texts)
		self.assertIn("can't be set from the chat", texts)
		self.assertNotIn("Traceback", texts)
		self.assertEqual((frappe.db.count("Purchase Order"), frappe.db.count("Purchase Order Item")), before)
		for _ in range(3):  # retrying the same payload fails the same way and never leaves anything behind
			self.click("__retry__")
		self.assertEqual((frappe.db.count("Purchase Order"), frappe.db.count("Purchase Order Item")), before)
		self.assertEqual(self.last()["question"]["kind"], "form_confirm")  # Retry / Cancel still offered

	def test_controller_validation_error_then_correction_and_retry(self):
		self.start("Purchase Order", [("AIW Defaulted", 1)], schedule_date=add_days(nowdate(), -3))
		r = self.click("__create__")
		texts = " ".join(m.get("text") or "" for m in r["messages"])
		self.assertIn("ERPNext didn't accept the Purchase Order", texts)
		self.assertIn("cannot be before", texts)
		later = add_days(nowdate(), 5)
		self.say(f"change required by to {later}")
		self.assertIn({"label": "Required by", "value": later}, self.preview()["rows"])  # the corrected value is in the next preview
		before = frappe.db.count("Purchase Order")
		po = self.create()
		self.assertEqual(str(po.schedule_date), later)
		self.assertEqual(frappe.db.count("Purchase Order"), before + 1)
		r = chat.send_message(session=self.s, action=json.dumps({"question_id": "stale", "value": "__create__", "label": "Create"}))
		self.assertEqual(frappe.db.count("Purchase Order"), before + 1)  # a repeated click creates nothing

	def test_warehouse_disabled_after_the_preview_is_caught_before_insert(self):
		self.start("Purchase Order", [("AIW Bottle", 1)])
		self.click(self.stores)
		self.preview()
		frappe.db.set_value("Warehouse", self.stores, "disabled", 1)
		self.addCleanup(frappe.db.set_value, "Warehouse", self.stores, "disabled", 0)
		before = frappe.db.count("Purchase Order")
		r = self.click("__create__")
		texts = " ".join(m.get("text") or "" for m in r["messages"])
		self.assertIn(f"“{self.stores}” isn't a warehouse of AIW Plain Co that you can use", texts)
		self.assertEqual(self.last()["question"]["kind"], "form_warehouse")
		self.assertNotIn(self.stores, [o["value"] for o in self.last()["question"]["options"]])
		self.assertEqual(frappe.db.count("Purchase Order"), before)

	def test_erpnext_warehouse_rejection_is_turned_into_a_question(self):
		"""If ERPNext still refuses for a missing warehouse (e.g. a rule we don't model), the chat asks for it."""
		self.start("Purchase Order", [("AIW Service", 1)])
		with mock.patch.object(requirements, "needs_warehouse", return_value=False):
			original = frappe.get_doc

			def refusing(*args, **kwargs):
				doc = original(*args, **kwargs)
				if getattr(doc, "doctype", None) == "Purchase Order":
					doc.validate = lambda: frappe.throw("Row #1: Warehouse is mandatory for stock Item <strong>AIW-SVC</strong>")
				return doc

			with mock.patch("frappe.get_doc", refusing):
				r = self.click("__create__")
		self.assertIn("Warehouse is mandatory", " ".join(m.get("text") or "" for m in r["messages"]))
		self.assertEqual(self.last()["question"]["kind"], "form_warehouse")
		self.assertEqual(self.pending()["rows"], [1])

	# ---- permissions and company isolation ------------------------------------------------------
	def test_restricted_user_only_sees_and_uses_their_companys_warehouses(self):
		email = "aiw-restricted@example.com"
		frappe.get_doc({"doctype": "User", "email": email, "first_name": "AIW", "send_welcome_email": 0,
			"roles": [{"role": "System Manager"}, {"role": "Purchase User"}, {"role": "Stock User"}]}).insert(ignore_permissions=True)
		frappe.get_doc({"doctype": "User Permission", "user": email, "allow": "Company", "for_value": PLAIN,
			"apply_to_all_doctypes": 1}).insert(ignore_permissions=True)
		frappe.set_user(email)
		self.s = chat.new_conversation()["session"]
		m = self.start("Purchase Order", [("AIW Bottle", 1)])
		self.assertNotIn(self.other_stores, [o["value"] for o in m["question"]["options"]])
		self.say(self.other_stores)
		self.assertEqual(self.last()["question"]["kind"], "form_warehouse")
		self.say(self.stores)
		r = self.say(f"change company to {OTHER}")  # not a company this user may use
		self.assertIn("couldn't find a company called", " ".join(m.get("text") or "" for m in r["messages"]))
		po = self.create()
		self.assertEqual((po.company, po.items[0].warehouse, po.owner), (PLAIN, self.stores, email))
