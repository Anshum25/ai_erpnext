"""Integration tests for the AI Sales Invoice registry / mapping / validation / creation flow.

Run: bench --site <site> run-tests --module ai_erpnext.tests.test_sales_invoice_mapping
All fixtures are created inside the test transaction and rolled back afterwards.
"""

import json

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import nowdate

from ai_erpnext.constants import DocumentStatus, FieldStatus, MappingStatus
from ai_erpnext.mapping.company import CompanyMatcher
from ai_erpnext.mapping.customer import match_customer
from ai_erpnext.mapping.item import match_item
from ai_erpnext.mapping.mapper import map_document
from ai_erpnext.mapping.normalize import parse_date, parse_number
from ai_erpnext.mapping.uom import match_uom
from ai_erpnext.registry.document_registry import get_definition, supported_doctypes
from ai_erpnext.validation.base import DATA, ValidationReport
from ai_erpnext.validation.totals import validate_line, validate_totals
from ai_erpnext.validation.validator import validate_outcome

P = "AITEST"
INVOICE_NO = "AITEST-INV-000002"
TODAY = nowdate()  # must fall inside an active Fiscal Year


def make_customer(name):
	group = frappe.db.get_value("Customer Group", {"is_group": 0}, "name")
	territory = frappe.db.get_value("Territory", {"is_group": 0}, "name")
	return frappe.get_doc(
		{"doctype": "Customer", "customer_name": name, "customer_group": group, "territory": territory}
	).insert()


def make_item(code, item_name, uom="Nos", **extra):
	group = frappe.db.get_value("Item Group", {"is_group": 0}, "name")
	return frappe.get_doc(
		{
			"doctype": "Item",
			"item_code": code,
			"item_name": item_name,
			"item_group": group,
			"stock_uom": uom,
			"is_stock_item": 0,
			"is_sales_item": 1,
			**extra,
		}
	).insert()


def our_company():
	return frappe.defaults.get_user_default("Company") or frappe.db.get_single_value("Global Defaults", "default_company")


def extracted_invoice(customer=f"{P} Exact Customer", item=f"{P} Exact Item", **overrides):
	# Issued by our own company to the customer: a Sales Invoice for us (detection/direction.py).
	data = {
		"document_type": "Sales Invoice",
		"supplier_name": our_company(),
		"customer_name": customer,
		"invoice_number": INVOICE_NO,
		"invoice_date": TODAY,
		"due_date": TODAY,
		"items": [
			{"item_name": item, "description": "Blue", "qty": 2, "unit": "Nos", "rate": 10.0, "amount": 20.0},
		],
		"subtotal": 20.0,
		"total_amount": 20.0,
	}
	data.update(overrides)
	return data


def make_entry(extracted):
	# status "Processed" so the before_save hook does not enqueue AI extraction.
	return frappe.get_doc(
		{"doctype": "AI Document Entry", "status": "Processed", "extracted_data": json.dumps(extracted)}
	).insert()


def validate_totals_report(canonical):
	report = ValidationReport()
	validate_totals(report, canonical)
	return report


class TestAISalesInvoiceMapping(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		cls.exact_customer = make_customer(f"{P} Exact Customer").name
		make_customer(f"{P} Twin Customer")
		make_customer(f"{P} Twin Customer")  # same customer_name, different ID -> ambiguous
		make_customer(f"{P} Fuzzy Customer Alpha")
		make_customer(f"{P} Fuzzy Customer Alphb")

		cls.exact_item = make_item(f"{P}-EXACT", f"{P} Exact Item").name
		make_item(f"{P}-TWIN-1", f"{P} Twin Item")
		make_item(f"{P}-TWIN-2", f"{P} Twin Item")
		cls.sqft_item = make_item(f"{P}-SQFT", f"{P} Floor Tile", uom="Square Foot").name
		cls.company = CompanyMatcher().default_company()

	# 1-4 Customer ------------------------------------------------------------------
	def test_01_exact_customer_match(self):
		res = match_customer(f"{P} Exact Customer")
		self.assertTrue(res.matched)
		self.assertEqual(res.name, self.exact_customer)
		self.assertEqual(res.match_type, "exact")
		self.assertEqual(res.status, FieldStatus.MATCHED)

	def test_02_case_insensitive_customer_match(self):
		res = match_customer(f"{P.lower()} exact CUSTOMER")
		self.assertTrue(res.matched)
		self.assertEqual(res.name, self.exact_customer)
		self.assertEqual(res.match_type, "case_insensitive")

	def test_02b_normalized_customer_match(self):
		res = match_customer(f"{P}  exact, customer.")
		self.assertTrue(res.matched)
		self.assertEqual(res.match_type, "normalized")

	def test_03_customer_not_found(self):
		res = match_customer("Completely Unknown Buyer Zqx")
		self.assertFalse(res.matched)
		self.assertEqual(res.status, FieldStatus.NOT_FOUND)
		self.assertTrue(res.requires_review)

	def test_04_ambiguous_customer(self):
		res = match_customer(f"{P} Twin Customer")
		self.assertFalse(res.matched)
		self.assertEqual(res.status, FieldStatus.AMBIGUOUS)
		self.assertTrue(res.requires_review)
		self.assertGreaterEqual(len(res.candidates), 2)
		self.assertIsNone(res.name)

	def test_04b_fuzzy_customer_is_never_auto_selected(self):
		res = match_customer(f"{P} Fuzzy Customer Alph")
		self.assertFalse(res.matched)
		self.assertEqual(res.match_type, "fuzzy_suggestion")
		self.assertTrue(res.candidates)

	# 5-7 Item ----------------------------------------------------------------------
	def test_05_exact_item_match(self):
		res = match_item({"item_name": f"{P} Exact Item"})
		self.assertTrue(res.matched)
		self.assertEqual(res.name, self.exact_item)
		self.assertEqual(res.match_type, "exact")
		by_code = match_item({"item_code": self.exact_item, "item_name": "whatever"})
		self.assertEqual(by_code.match_type, "exact_code")

	def test_06_item_not_found(self):
		res = match_item({"item_name": "Nonexistent Gadget Zqx", "description": "nothing like it"})
		self.assertFalse(res.matched)
		self.assertEqual(res.status, FieldStatus.NOT_FOUND)

	def test_07_ambiguous_item(self):
		res = match_item({"item_name": f"{P} Twin Item"})
		self.assertFalse(res.matched)
		self.assertEqual(res.status, FieldStatus.AMBIGUOUS)
		self.assertEqual({c["name"] for c in res.candidates}, {f"{P}-TWIN-1", f"{P}-TWIN-2"})

	# 8-9 UOM -----------------------------------------------------------------------
	def test_08_uom_match(self):
		self.assertEqual(match_uom("Nos").match_type, "exact")
		res = match_uom("Square feet")
		self.assertTrue(res.matched)
		self.assertEqual(res.name, "Square Foot")
		self.assertEqual(match_uom("pcs").name, "Nos")

	def test_09_uom_not_found(self):
		before = frappe.db.count("UOM")
		res = match_uom("Zorkmid Bundles")
		self.assertFalse(res.matched)
		self.assertIn(res.status, (FieldStatus.NOT_FOUND, FieldStatus.AMBIGUOUS))
		self.assertEqual(frappe.db.count("UOM"), before, "UOM must never be auto-created")

	# 10-12 line validation ---------------------------------------------------------
	def test_10_quantity_validation(self):
		report = ValidationReport()
		validate_line(report, 1, 0, 10, 0, "item_1")
		self.assertIn("qty_not_positive", [i.code for i in report.errors])

	def test_11_rate_validation(self):
		report = ValidationReport()
		validate_line(report, 1, 1, -5, -5, "item_1")
		self.assertIn("rate_negative", [i.code for i in report.errors])

	def test_12_amount_validation(self):
		report = ValidationReport()
		validate_line(report, 1, 10, 1000, 10000, "item_1")
		self.assertFalse(report.issues)
		validate_line(report, 2, 2, 10, 100, "item_2")
		self.assertEqual([i.code for i in report.errors], ["amount_mismatch"])
		self.assertEqual(report.errors[0].category, DATA)

	def test_12b_document_totals(self):
		canonical = {
			"items": [{"qty": 1, "rate": 24.99, "amount": 24.99}, {"qty": 1, "rate": 16.99, "amount": 16.99}, {"qty": 1, "rate": 19.99, "amount": 19.99}],
			"subtotal": 61.97, "tax_rate": 0.05, "tax_amount": 3.10, "total_amount": 65.06, "discount_amount": None,
		}
		report = ValidationReport()
		validate_totals(report, canonical)
		self.assertFalse(report.errors)
		self.assertEqual([i.code for i in report.warnings], ["total_mismatch"])  # 1-cent rounding
		# only a percentage rate (seen in real extraction output): tax is derived
		only_rate = dict(canonical, tax_rate=5.0, tax_amount=None)
		report = validate_totals_report(only_rate)
		self.assertFalse(report.errors, [i.message for i in report.errors])
		canonical["total_amount"] = 80
		report = ValidationReport()
		validate_totals(report, canonical)
		self.assertIn("total_mismatch", [i.code for i in report.errors])

	# 13 duplicate ------------------------------------------------------------------
	def test_13_duplicate_invoice_detection(self):
		entry = make_entry(extracted_invoice())
		from ai_erpnext.api.creation import create_document

		first = create_document(entry.name)
		self.assertTrue(first["created"], first.get("message"))

		second = make_entry(extracted_invoice())
		from ai_erpnext.api.mapping import remap

		review = remap(second.name)
		self.assertTrue(review["validation"]["duplicate"])
		self.assertEqual(review["status"], DocumentStatus.NEEDS_REVIEW)
		self.assertIn(first["name"], [d["name"] for d in review["validation"]["duplicates"]])

		blocked = create_document(second.name)
		self.assertFalse(blocked["created"])
		count = frappe.db.count("Sales Invoice", {"customer": self.exact_customer})
		allowed = create_document(second.name, acknowledge_duplicate=1)
		self.assertTrue(allowed["created"])
		self.assertEqual(frappe.db.count("Sales Invoice", {"customer": self.exact_customer}), count + 1)

	# 14-19 document level ----------------------------------------------------------
	def test_14_complete_sales_invoice_mapping(self):
		outcome = map_document(extracted_invoice())
		self.assertEqual(outcome.target_doctype, "Sales Invoice")
		self.assertEqual(outcome.status, MappingStatus.MAPPED)
		m = outcome.mapped
		self.assertEqual(m["customer"], self.exact_customer)
		self.assertEqual(m["company"], self.company)
		self.assertEqual(m["posting_date"], TODAY)
		self.assertTrue(frappe.db.exists("Currency", m["currency"]))
		self.assertEqual(m["items"][0]["item_code"], self.exact_item)
		self.assertEqual(m["items"][0]["uom"], "Nos")
		self.assertEqual(outcome.results["item_1"].match_type, "exact")
		# extracted values are preserved separately from mapped identifiers
		self.assertEqual(outcome.results["customer"].input, f"{P} Exact Customer")

	def test_15_missing_customer(self):
		outcome = map_document(extracted_invoice(customer="Unknown Buyer Zqx"))
		self.assertIsNone(outcome.mapped["customer"])
		report = validate_outcome(outcome)
		self.assertIn("customer_not_found", [i.code for i in report.blocking])
		self.assertEqual(report.document_status, DocumentStatus.NEEDS_REVIEW)

	def test_16_missing_item(self):
		outcome = map_document(extracted_invoice(item="Nonexistent Gadget Zqx"))
		self.assertIsNone(outcome.mapped["items"][0]["item_code"])
		report = validate_outcome(outcome)
		self.assertTrue(any(i.field == "item_1" for i in report.blocking))

	def test_17_invalid_document_type(self):
		outcome = map_document({"document_type": "Spaceship Manifest", "items": []})
		self.assertIsNone(outcome.target_doctype)
		self.assertIsNone(outcome.definition)
		self.assertEqual(outcome.status, MappingStatus.FAILED)
		entry = make_entry({"document_type": "Spaceship Manifest"})
		from ai_erpnext.api.mapping import remap

		review = remap(entry.name)
		self.assertEqual(review["status"], DocumentStatus.NEEDS_REVIEW)
		self.assertFalse(review["target_doctype"])

	def test_18_successful_mapped_document(self):
		# distinct total so the "same customer/date/total" heuristic does not hit test_13's invoices
		entry = make_entry(extracted_invoice(
			invoice_number="AITEST-UNIQUE-18",
			items=[{"item_name": f"{P} Exact Item", "qty": 7, "unit": "Nos", "rate": 3.0, "amount": 21.0}],
			subtotal=21.0, total_amount=21.0,
		))
		from ai_erpnext.api.mapping import remap

		review = remap(entry.name)
		entry.reload()
		self.assertFalse(review["validation"]["duplicates"], review["validation"]["duplicates"])
		self.assertEqual(entry.target_doctype, "Sales Invoice")
		self.assertIn(entry.status, (DocumentStatus.READY_TO_CREATE, DocumentStatus.READY_FOR_REVIEW))
		self.assertFalse(review["validation"]["errors"], review["validation"]["errors"])
		self.assertTrue(review["validation"]["can_create"])
		self.assertEqual(json.loads(entry.extracted_data)["customer_name"], f"{P} Exact Customer")

	def test_19_needs_review_document_and_correction(self):
		entry = make_entry(extracted_invoice(customer=f"{P} Twin Customer", invoice_number="AITEST-UNIQUE-19"))
		from ai_erpnext.api.creation import create_document
		from ai_erpnext.api.mapping import apply_corrections, remap

		review = remap(entry.name)
		self.assertEqual(review["status"], DocumentStatus.NEEDS_REVIEW)
		self.assertTrue(review["validation"]["errors"])
		blocked = create_document(entry.name)
		self.assertFalse(blocked["created"])

		chosen = review["results"]["customer"]["candidates"][0]["name"]
		corrected = apply_corrections(entry.name, {"customer": chosen})
		self.assertEqual(corrected["mapped"]["customer"], chosen)
		self.assertEqual(corrected["results"]["customer"]["match_type"], "manual")
		self.assertNotEqual(corrected["status"], DocumentStatus.NEEDS_REVIEW)

		with self.assertRaises(frappe.ValidationError):
			apply_corrections(entry.name, {"not_a_field": "x"})

	def test_19b_uom_without_conversion_needs_review(self):
		outcome = map_document(extracted_invoice(items=[{"item_name": f"{P} Exact Item", "qty": 1, "unit": "Square feet", "rate": 5, "amount": 5}]))
		self.assertEqual(outcome.mapped["items"][0]["uom"], "Square Foot")
		report = validate_outcome(outcome)
		self.assertIn("uom_no_conversion", [i.code for i in report.blocking])

	def test_19d_posting_date_outside_fiscal_year(self):
		outcome = map_document(extracted_invoice(invoice_date="1999-01-01", due_date="1999-01-01"))
		report = validate_outcome(outcome)
		self.assertIn("no_fiscal_year", [i.code for i in report.blocking])

	def test_19e_due_date_before_posting(self):
		outcome = map_document(extracted_invoice(due_date="2000-01-01"))
		self.assertIn("due_before_posting", [i.code for i in validate_outcome(outcome).blocking])

	def test_19c_low_confidence_document_type(self):
		outcome = map_document({**extracted_invoice(), "confidence": 0.4})
		self.assertIsNone(outcome.target_doctype)
		self.assertFalse(outcome.detection["confident"])

	# 20 draft creation -------------------------------------------------------------
	def test_20_erpnext_draft_creation(self):
		extracted = extracted_invoice(
			invoice_number="AITEST-UNIQUE-20",
			items=[
				{"item_name": f"{P} Exact Item", "description": "Blue", "qty": 2, "unit": "Nos", "rate": 10.0, "amount": 20.0},
				{"item_name": f"{P} Floor Tile", "qty": 3, "unit": "Square feet", "rate": 4.5, "amount": 13.5},
			],
			subtotal=33.5,
			total_amount=33.5,
		)
		entry = make_entry(extracted)
		from ai_erpnext.api.creation import create_document

		res = create_document(entry.name)
		self.assertTrue(res["created"], res.get("message"))
		si = frappe.get_doc("Sales Invoice", res["name"])
		self.assertEqual(si.docstatus, 0, "must be Draft, never submitted")
		self.assertEqual(si.customer, self.exact_customer)
		self.assertEqual(si.company, self.company)
		self.assertEqual(str(si.posting_date), TODAY)
		self.assertEqual([r.item_code for r in si.items], [self.exact_item, self.sqft_item])
		self.assertEqual([r.uom for r in si.items], ["Nos", "Square Foot"])
		self.assertEqual([r.qty for r in si.items], [2, 3])
		self.assertEqual([r.rate for r in si.items], [10.0, 4.5])
		self.assertAlmostEqual(si.net_total, 33.5)
		self.assertIn("AI Source Invoice No: AITEST-UNIQUE-20", si.remarks)

		entry.reload()
		self.assertEqual(entry.status, DocumentStatus.CREATED)
		self.assertEqual(entry.created_document, si.name)
		# cannot create twice from the same entry
		with self.assertRaises(frappe.ValidationError):
			create_document(entry.name)

	# misc ---------------------------------------------------------------------------
	def test_registry(self):
		self.assertEqual(supported_doctypes(), ["Sales Invoice", "Purchase Invoice", "Sales Order", "Purchase Order", "Quotation",
			"Delivery Note", "Purchase Receipt", "Material Request", "BOM"])
		d = get_definition("sales invoice")
		self.assertEqual(d.target_doctype, "Sales Invoice")
		self.assertIn("customer", d.required_fields)
		self.assertEqual(get_definition("Purchase Invoice").party_doctype, "Supplier")
		self.assertTrue(get_definition("Sales Invoice Return").is_return)
		self.assertIsNone(get_definition("Supplier Quotation"))  # never a target from an image

	def test_untrusted_input_sanitising(self):
		self.assertEqual(parse_number("$ 1,234.50"), 1234.5)
		self.assertIsNone(parse_number({"evil": 1}))
		self.assertTrue(parse_date("05/08/2024").ambiguous)
		self.assertEqual(parse_date("Aug 5, 2024").value, "2024-08-05")
		outcome = map_document(extracted_invoice(customer={"$ne": 1}, items="not a list"))
		self.assertEqual(outcome.mapped["items"], [])
		self.assertIsNone(outcome.mapped["customer"])
