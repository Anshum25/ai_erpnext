"""Data-backed coverage for query features that only had smoke tests, the new guided Draft flows (Material Request, Stock
Entry), the image content check and the retention of extracted data.

Every query test inserts its own fixtures and asserts the exact records and values returned; creation tests assert the
persisted Draft. The provider is never called.

Run: bench --site <site> run-tests --module ai_erpnext.tests.test_coverage_phase_d
"""

import json
from unittest import mock

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_days, flt, nowdate

from ai_erpnext.api import chat
from ai_erpnext.chat import queries
from ai_erpnext.pipeline import controller, retention
from ai_erpnext.tests.fixtures import test_file


def run(plan):
	return queries.execute(queries.validate(plan))


def rows(result):
	return (result.get("card") or {}).get("rows") or []


class PhaseDCase(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		try:
			cls.make_fixtures()
		except Exception:
			frappe.db.rollback()  # a failed setup must not leave half its fixtures committed for the next run
			raise

	@classmethod
	def make_fixtures(cls):
		frappe.set_user("Administrator")
		cls.company = frappe.defaults.get_user_default("Company") or frappe.db.get_single_value(
			"Global Defaults", "default_company"
		)
		cls.currency = frappe.get_cached_value("Company", cls.company, "default_currency")
		cg = frappe.db.get_value("Customer Group", {"is_group": 0}, "name")
		te = frappe.db.get_value("Territory", {"is_group": 0}, "name")
		sg = frappe.db.get_value("Supplier Group", {"is_group": 0}, "name")
		ig = frappe.db.get_value("Item Group", {"is_group": 0}, "name")
		cls.customer = (
			frappe.get_doc(
				{
					"doctype": "Customer",
					"customer_name": "AIPD Customer",
					"customer_group": cg,
					"territory": te,
				}
			)
			.insert()
			.name
		)
		cls.supplier = (
			frappe.get_doc({"doctype": "Supplier", "supplier_name": "AIPD Supplier", "supplier_group": sg})
			.insert()
			.name
		)
		for code, stock in (("AIPD-SVC", 0), ("AIPD-RAW", 1), ("AIPD-FG", 1)):
			frappe.get_doc(
				{
					"doctype": "Item",
					"item_code": code,
					"item_name": f"{code} name",
					"item_group": ig,
					"stock_uom": "Nos",
					"is_stock_item": stock,
					"is_sales_item": 1,
					"is_purchase_item": 1,
					"valuation_rate": 6,
				}
			).insert()
		cls.wh = {
			k: frappe.get_doc({"doctype": "Warehouse", "warehouse_name": f"AIPD {k}", "company": cls.company})
			.insert()
			.name
			for k in ("Main", "Shop")
		}
		cls.receipt = frappe.get_doc(
			{
				"doctype": "Stock Entry",
				"stock_entry_type": "Material Receipt",
				"company": cls.company,
				"items": [
					{"item_code": "AIPD-RAW", "qty": 40, "t_warehouse": cls.wh["Main"], "basic_rate": 6}
				],
			}
		).insert()
		cls.receipt.submit()  # test data: the assistant never submits
		cls.transfer = frappe.get_doc(
			{
				"doctype": "Stock Entry",
				"stock_entry_type": "Material Transfer",
				"company": cls.company,
				"items": [
					{
						"item_code": "AIPD-RAW",
						"qty": 15,
						"s_warehouse": cls.wh["Main"],
						"t_warehouse": cls.wh["Shop"],
					}
				],
			}
		).insert()
		cls.transfer.submit()
		cls.pi = frappe.get_doc(
			{
				"doctype": "Purchase Invoice",
				"supplier": cls.supplier,
				"company": cls.company,
				"posting_date": add_days(nowdate(), -10),
				"set_posting_time": 1,
				"bill_no": "AIPD-BILL-1",
				"bill_date": add_days(nowdate(), -10),
				"due_date": add_days(nowdate(), -3),
				"items": [{"item_code": "AIPD-SVC", "qty": 4, "rate": 25}],
			}
		).insert()
		cls.pi.submit()
		cls.si = frappe.get_doc(
			{
				"doctype": "Sales Invoice",
				"customer": cls.customer,
				"company": cls.company,
				"posting_date": nowdate(),
				"items": [{"item_code": "AIPD-SVC", "qty": 2, "rate": 80}],
			}
		).insert()
		cls.si.submit()
		from erpnext.accounts.doctype.payment_entry.payment_entry import get_payment_entry

		pe = get_payment_entry("Sales Invoice", cls.si.name)
		pe.reference_no, pe.reference_date, pe.paid_amount, pe.received_amount = (
			"AIPD-PAY-1",
			nowdate(),
			60,
			60,
		)
		pe.references[0].allocated_amount = 60
		pe.insert()
		pe.submit()
		cls.pe = pe.name
		cash = frappe.db.get_value(
			"Account", {"company": cls.company, "account_type": "Cash", "is_group": 0}, "name"
		)
		expense = frappe.db.get_value(
			"Account", {"company": cls.company, "root_type": "Expense", "is_group": 0}, "name"
		)
		cls.je = (
			frappe.get_doc(
				{
					"doctype": "Journal Entry",
					"company": cls.company,
					"posting_date": nowdate(),
					"title": "AIPD Journal",
					"accounts": [
						{
							"account": expense,
							"debit_in_account_currency": 33,
							"cost_center": frappe.get_cached_value("Company", cls.company, "cost_center"),
						},
						{"account": cash, "credit_in_account_currency": 33},
					],
				}
			)
			.insert()
			.name
		)
		cls.bom = frappe.get_doc(
			{
				"doctype": "BOM",
				"item": "AIPD-FG",
				"company": cls.company,
				"quantity": 1,
				"rm_cost_as_per": "Valuation Rate",
				"items": [{"item_code": "AIPD-RAW", "qty": 3, "rate": 6}],
			}
		).insert()
		cls.bom.submit()
		cls.wo = (
			frappe.get_doc(
				{
					"doctype": "Work Order",
					"production_item": "AIPD-FG",
					"bom_no": cls.bom.name,
					"qty": 5,
					"company": cls.company,
					"wip_warehouse": cls.wh["Main"],
					"fg_warehouse": cls.wh["Shop"],
					"source_warehouse": cls.wh["Main"],
				}
			)
			.insert()
			.name
		)

	def setUp(self):
		frappe.set_user("Administrator")
		self.enqueue = mock.patch("frappe.enqueue").start()
		self.route = mock.patch(
			"ai_erpnext.chat.llm.route", return_value={"mode": "chat", "reply": "general answer"}
		).start()
		self.addCleanup(mock.patch.stopall)


class TestDataBackedQueries(PhaseDCase):
	def test_purchase_invoice_list_details_and_overdue(self):
		result = run(
			{"doctype": "Purchase Invoice", "operation": "list", "filters": {"party": "AIPD Supplier"}}
		)
		self.assertEqual(result["names"], [self.pi.name])
		self.assertEqual((rows(result)[0]["grand_total"], rows(result)[0]["currency"]), (100, self.currency))
		details = run(
			{
				"doctype": "Purchase Invoice",
				"operation": "details",
				"filters": {"name": self.pi.name},
				"fields": ["outstanding", "items"],
			}
		)
		self.assertIn(f"100.0 {self.currency}", details["text"])
		self.assertIn("4.0 Nos × AIPD-SVC name (AIPD-SVC) at 25.0 = 100.0", details["text"])
		overdue = run(
			{"doctype": "Purchase Invoice", "operation": "overdue", "filters": {"party": "AIPD Supplier"}}
		)
		self.assertEqual(overdue["names"], [self.pi.name])

	def test_payment_entry_values_and_effect_on_outstanding(self):
		result = run({"doctype": "Payment Entry", "operation": "list", "filters": {"party": "AIPD Customer"}})
		self.assertEqual(result["names"], [self.pe])
		row = rows(result)[0]
		self.assertEqual(
			(row["payment_type"], row["paid_amount"], row["paid_from_account_currency"]),
			("Receive", 60, self.currency),
		)
		total = run(
			{
				"doctype": "Payment Entry",
				"operation": "aggregate",
				"aggregate": {"fn": "sum", "field": "paid_amount"},
				"filters": {"party": "AIPD Customer"},
			}
		)
		self.assertEqual(rows(total)[0]["value"], 60)
		self.assertEqual(flt(frappe.db.get_value("Sales Invoice", self.si.name, "outstanding_amount")), 100)
		outstanding = run(
			{
				"doctype": "Customer",
				"operation": "details",
				"filters": {"name": self.customer},
				"fields": ["outstanding"],
			}
		)
		self.assertIn("100.0", outstanding["text"])

	def test_journal_entry_and_its_account_rows(self):
		result = run(
			{
				"doctype": "Journal Entry",
				"operation": "list",
				"where": [{"field": "title", "op": "=", "value": "AIPD Journal"}],
			}
		)
		self.assertEqual(result["names"], [self.je])
		self.assertEqual((rows(result)[0]["total_debit"], rows(result)[0]["total_credit"]), (33, 33))
		lines = run(
			{
				"doctype": "Journal Entry",
				"operation": "child_rows",
				"child": {
					"table": "accounts",
					"fields": ["account", "debit_in_account_currency", "credit_in_account_currency"],
				},
				"where": [{"field": "name", "op": "=", "value": self.je}],
			}
		)
		self.assertEqual(
			sorted((r["debit_in_account_currency"], r["credit_in_account_currency"]) for r in rows(lines)),
			[(0, 33), (33, 0)],
		)

	def test_stock_entries_and_their_rows(self):
		result = run(
			{
				"doctype": "Stock Entry",
				"operation": "list",
				"where": [{"field": "stock_entry_type", "op": "=", "value": "Material Transfer"}],
				"limit": 20,
			}
		)
		self.assertIn(self.transfer.name, result["names"])
		self.assertNotIn(self.receipt.name, result["names"])
		lines = run(
			{
				"doctype": "Stock Entry",
				"operation": "child_rows",
				"child": {"table": "items", "fields": ["item_code", "qty", "s_warehouse", "t_warehouse"]},
				"where": [{"field": "name", "op": "=", "value": self.transfer.name}],
			}
		)
		self.assertEqual(
			[(r["item_code"], r["qty"], r["s_warehouse"], r["t_warehouse"]) for r in rows(lines)],
			[("AIPD-RAW", 15, self.wh["Main"], self.wh["Shop"])],
		)

	def test_stock_ledger_rows_and_balances_are_real_movements(self):
		result = run(
			{
				"doctype": "Stock Ledger Entry",
				"operation": "list",
				"where": [{"field": "item_code", "op": "=", "value": "AIPD-RAW"}],
				"limit": 20,
			}
		)
		moves = sorted((r["warehouse"], r["actual_qty"]) for r in rows(result))
		self.assertEqual(
			moves, sorted([(self.wh["Main"], 40), (self.wh["Main"], -15), (self.wh["Shop"], 15)])
		)
		self.assertFalse({"valuation_rate", "stock_value"} & {c["key"] for c in result["card"]["columns"]})
		bins = run(
			{
				"doctype": "Bin",
				"operation": "aggregate",
				"aggregate": {"fn": "sum", "field": "actual_qty", "group_by": "warehouse"},
				"where": [{"field": "item_code", "op": "=", "value": "AIPD-RAW"}],
			}
		)
		self.assertEqual(
			{r["warehouse"]: r["value"] for r in rows(bins)}, {self.wh["Main"]: 25, self.wh["Shop"]: 15}
		)

	def test_bom_and_work_order(self):
		bom = run(
			{
				"doctype": "BOM",
				"operation": "details",
				"filters": {"name": self.bom.name},
				"fields": ["items"],
			}
		)
		self.assertIn("3.0 Nos × AIPD-RAW name (AIPD-RAW)", bom["text"])
		wo = run(
			{
				"doctype": "Work Order",
				"operation": "list",
				"where": [{"field": "production_item", "op": "=", "value": "AIPD-FG"}],
			}
		)
		self.assertEqual(wo["names"], [self.wo])
		self.assertEqual((rows(wo)[0]["qty"], rows(wo)[0]["produced_qty"]), (5, 0))
		required = run(
			{
				"doctype": "Work Order",
				"operation": "child_rows",
				"child": {"table": "required_items", "fields": ["item_code", "required_qty"]},
				"where": [{"field": "name", "op": "=", "value": self.wo}],
			}
		)
		self.assertEqual([(r["item_code"], r["required_qty"]) for r in rows(required)], [("AIPD-RAW", 15)])

	def test_employee_organisational_fields_only(self):
		emp = frappe.get_doc(
			{
				"doctype": "Employee",
				"first_name": "AIPD",
				"last_name": "Person",
				"gender": frappe.db.get_value("Gender", {}, "name") or "Male",
				"date_of_birth": "1990-01-01",
				"date_of_joining": nowdate(),
				"company": self.company,
			}
		).insert()
		result = run({"doctype": "Employee", "operation": "list", "filters": {"search": "AIPD Person"}})
		self.assertEqual(result["names"], [emp.name])
		self.assertEqual(rows(result)[0]["employee_name"], "AIPD Person")
		self.assertFalse(
			{"date_of_birth", "ctc", "bank_ac_no", "cell_number", "personal_email"}
			& {c["key"] for c in result["card"]["columns"]}
		)
		with self.assertRaises(queries.QueryError):
			queries.validate(
				{
					"doctype": "Employee",
					"operation": "details",
					"filters": {"name": emp.name},
					"fields": ["date_of_birth"],
				}
			)


class TestGuidedStockDocuments(PhaseDCase):
	def setUp(self):
		super().setUp()
		self.s = chat.new_conversation()["session"]

	def send(self, text=None, action=None):
		return chat.send_message(session=self.s, text=text, action=json.dumps(action) if action else None)

	def pending(self):
		return next(
			m["question"] for m in reversed(chat.get_session(self.s)["messages"]) if m.get("question")
		)

	def answer(self, value):
		q = self.pending()
		return self.send(action={"question_id": q["id"], "value": value, "label": value})

	def created(self, response):
		card = next(m["card"] for m in response["messages"] if (m.get("card") or {}).get("type") == "created")
		return frappe.get_doc(card["doctype"], card["name"])

	def test_material_request_is_a_draft_with_purpose_dates_and_warehouse(self):
		self.send(text="create material request")
		q = self.pending()
		self.assertEqual(q["kind"], "form_field")
		self.assertIn("Purchase", [o["value"] for o in q["options"]])  # the purpose is asked, never assumed
		self.answer("Purchase")
		self.send(text=add_days(nowdate(), 4))  # required by
		self.send(text="AIPD-RAW")
		self.send(text="7")  # quantity; no price is asked for a material request
		self.answer("__done__")
		if (
			self.pending()["kind"] == "form_warehouse"
		):  # no ERPNext default applies: the row's warehouse is asked
			self.answer(self.wh["Main"])
		self.send(text="use AIPD Main warehouse")  # one warehouse for every row, whatever the default was
		self.assertEqual(self.pending()["kind"], "form_confirm")
		mr = self.created(self.answer("__create__"))
		self.assertEqual(
			(mr.doctype, mr.docstatus, mr.material_request_type, str(mr.schedule_date)),
			("Material Request", 0, "Purchase", add_days(nowdate(), 4)),
		)
		self.assertEqual(
			[(r.item_code, r.qty, r.warehouse) for r in mr.items], [("AIPD-RAW", 7, self.wh["Main"])]
		)

	def test_stock_transfer_asks_both_warehouses_and_stays_a_draft(self):
		before = frappe.db.count("Stock Ledger Entry")
		self.send(text="create stock entry")
		q = self.pending()
		self.assertEqual(
			sorted(o["value"] for o in q["options"] if not o["value"].startswith("__")),
			["Material Issue", "Material Receipt", "Material Transfer"],
		)
		self.answer("Material Transfer")
		texts = []
		for _ in range(2):  # source, then target
			q = self.pending()
			texts.append(chat.get_session(self.s)["messages"][-1]["text"])
			self.answer(self.wh["Main"] if "leave" in texts[-1] else self.wh["Shop"])
		self.assertTrue(any("leave" in t for t in texts) and any("go into" in t for t in texts), texts)
		self.send(text="AIPD-RAW")
		self.send(text="5")
		self.answer("__done__")
		se = self.created(self.answer("__create__"))
		self.assertEqual(
			(se.doctype, se.docstatus, se.stock_entry_type), ("Stock Entry", 0, "Material Transfer")
		)
		self.assertEqual(
			[(r.item_code, r.qty, r.s_warehouse, r.t_warehouse) for r in se.items],
			[("AIPD-RAW", 5, self.wh["Main"], self.wh["Shop"])],
		)
		self.assertEqual(frappe.db.count("Stock Ledger Entry"), before)  # a Draft moves no stock

	def test_a_material_receipt_has_no_source_warehouse(self):
		self.send(text="create stock entry")
		self.answer("Material Receipt")
		self.assertIn("go into", chat.get_session(self.s)["messages"][-1]["text"])
		self.answer(self.wh["Main"])
		self.send(text="AIPD-RAW")
		self.send(text="2")
		self.answer("__done__")
		se = self.created(self.answer("__create__"))
		self.assertEqual([(r.s_warehouse, r.t_warehouse) for r in se.items], [(None, self.wh["Main"])])

	def test_only_simple_stock_entry_types_are_offered(self):
		self.send(text="create stock entry")
		r = self.send(text="Manufacture")
		self.assertEqual(self.pending()["kind"], "form_field")
		self.assertFalse(
			frappe.db.exists(
				"Stock Entry",
				{
					"stock_entry_type": "Manufacture",
					"owner": "Administrator",
					"docstatus": 0,
					"creation": [">", frappe.utils.add_to_date(frappe.utils.now_datetime(), minutes=-1)],
				},
			)
		)
		self.assertTrue(r["messages"])


class TestImageContentCheck(PhaseDCase):
	def fake_png(self):
		return (
			frappe.get_doc(
				{
					"doctype": "File",
					"file_name": "aipd-not-an-image.png",
					"content": b"%PDF-1.4 definitely not a png",
					"is_private": 1,
				}
			)
			.insert()
			.file_url
		)

	def test_a_renamed_non_image_is_refused_before_any_extraction(self):
		s = chat.new_conversation()["session"]
		r = chat.send_message(session=s, text="read this", file_url=self.fake_png())
		self.assertIn(
			"not a readable JPG, PNG or WEBP image", " ".join(m.get("text") or "" for m in r["messages"])
		)
		self.enqueue.assert_not_called()
		self.assertFalse(frappe.db.get_value("AI Chat Session", s, "active_entry"))

	def test_the_pipeline_never_sends_a_non_image_to_a_provider(self):
		entry = frappe.get_doc(
			{"doctype": "AI Document Entry", "status": "Pending", "file": self.fake_png()}
		).insert()
		# the job commits its own result; inside a test that would commit the class fixtures too
		with (
			mock.patch("ai_erpnext.ai.providers.primary", side_effect=AssertionError("no provider call")),
			mock.patch.object(frappe.db, "commit"),
		):
			controller.run_ai_pipeline(entry.name)
		entry.reload()
		self.assertEqual((entry.status, entry.error_log), ("Failed", controller.SAFE_IMAGE_ERROR))

	def test_real_images_pass_and_formats_must_match(self):
		content = frappe.get_doc("File", {"file_url": test_file("png")}).get_content(encodings=())
		self.assertIsNone(controller.image_problem(content, "png"))
		self.assertEqual(
			controller.image_problem(content, "jpg"), controller.SAFE_IMAGE_ERROR
		)  # a PNG named .jpg
		self.assertEqual(controller.image_problem(b"", "png"), controller.SAFE_IMAGE_ERROR)


class TestRetention(PhaseDCase):
	def entry(self, status, created=None, days_old=400):
		e = frappe.get_doc(
			{
				"doctype": "AI Document Entry",
				"status": "Processed",
				"extracted_data": json.dumps({"customer_name": "AIPD Secret Buyer"}),
				"mapping_results": json.dumps(
					{
						"document_type": {
							"classification": {
								"kind": "invoice",
								"target_key": "Sales Invoice",
								"company": self.company,
								"our_role": "seller",
								"status": "ok",
								"buyer": "AIPD Secret Buyer",
							}
						}
					}
				),
			}
		).insert()
		frappe.db.set_value(
			"AI Document Entry",
			e.name,
			{
				"status": status,
				"created_document": created,
				"modified": add_days(frappe.utils.now_datetime(), -days_old),
			},
			update_modified=False,
		)
		return e.name

	def test_only_old_resolved_entries_are_cleared(self):
		old_done = self.entry("Created", self.si.name)
		recent_done = self.entry("Created", self.si.name, days_old=10)
		unresolved = self.entry("Needs Review")
		failed = self.entry("Failed")
		in_chat = self.entry("Created", self.si.name)
		s = chat.new_conversation()["session"]
		frappe.db.set_value("AI Chat Session", s, "active_entry", in_chat)
		cleared = retention.purge_resolved_entries(days=180)
		self.assertIn(old_done, cleared)
		self.assertFalse({recent_done, unresolved, failed, in_chat} & set(cleared))
		row = frappe.db.get_value(
			"AI Document Entry",
			old_done,
			["extracted_data", "created_document", "status", "mapping_results"],
			as_dict=True,
		)
		self.assertEqual(
			(row.extracted_data, row.created_document, row.status), (None, self.si.name, "Created")
		)
		self.assertNotIn("Secret Buyer", row.mapping_results)
		self.assertEqual(
			json.loads(row.mapping_results)["document_type"]["classification"]["target_key"], "Sales Invoice"
		)
		for name in (recent_done, unresolved, failed, in_chat):
			self.assertIn("Secret Buyer", frappe.db.get_value("AI Document Entry", name, "extracted_data"))

	def test_zero_days_keeps_everything(self):
		name = self.entry("Created", self.si.name)
		self.assertEqual(retention.purge_resolved_entries(days=0), [])
		self.assertTrue(frappe.db.get_value("AI Document Entry", name, "extracted_data"))

	def test_the_job_is_scheduled_daily_and_configurable(self):
		self.assertIn(
			"ai_erpnext.pipeline.retention.purge_resolved_entries",
			frappe.get_hooks("scheduler_events")["daily"],
		)
		self.assertTrue(frappe.get_meta("AI Provider Settings").has_field("document_retention_days"))
		from ai_erpnext.setup_doctypes import ensure_schema

		ensure_schema()  # an upgraded site gets the documented default once
		self.assertEqual(frappe.db.get_single_value("AI Provider Settings", "document_retention_days"), 180)
		self.assertEqual(retention.retention_days(), 180)
		frappe.db.set_single_value("AI Provider Settings", "document_retention_days", 30)
		ensure_schema()  # an administrator's choice is never overwritten
		self.assertEqual(retention.retention_days(), 30)
