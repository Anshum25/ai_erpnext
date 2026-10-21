"""Document direction and image-to-Draft for every registered document type.

1. The direction policy (detection/direction.py) as a pure function: who issued a document and who it is for decides
   what it becomes for our company; contradictory, unknown and unsupported documents never get a target.
2. End to end per type: a Prompt-B3-shaped extraction (the provider is never called) goes through the real
   classification, mapping, validation and ERPNext insert(); the persisted Draft is checked field by field, and a second
   confirmation never creates a second document.
3. Discounts, returns linked to their original invoice, duplicates, and the chat's clarification questions.

Run: bench --site <site> run-tests --module ai_erpnext.tests.test_document_direction
"""

import json
from unittest import mock

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_days, nowdate

from ai_erpnext.api import chat
from ai_erpnext.api.creation import create_document
from ai_erpnext.api.mapping import apply_corrections, remap
from ai_erpnext.detection import direction as D
from ai_erpnext.mapping.mapper import map_document
from ai_erpnext.pipeline.review_service import apply_mapping
from ai_erpnext.tests.fixtures import test_file
from ai_erpnext.validation.validator import validate_outcome

US = "AIDIR Own Co Pvt Ltd"  # synthetic company rows for the pure policy tests
SISTER = "AIDIR Sister Co"
CUST, SUPP = "AIDIR Customer", "AIDIR Supplier"


def rows(*names):
	return [frappe._dict(name=n, company_name=n, abbr="".join(w[0] for w in n.split())[:5]) for n in names]


def classify(doc, usable=(US,), default=US, rows_=None, **kw):
	with mock.patch.object(D, "company_rows", return_value=rows_ or rows(*usable)):
		return D.classify(doc, list(usable), default_company=default, **kw)


def doc(kind, issuer=None, recipient=None, seller=None, buyer=None, **extra):
	return {
		"document_type": kind,
		"issuer_name": issuer,
		"recipient_name": recipient,
		"supplier_name": seller,
		"customer_name": buyer,
		**extra,
	}


class TestDirectionPolicy(IntegrationTestCase):
	"""The business-role table, case by case. The title alone never decides."""

	def target(self, *args, **kw):
		c = classify(doc(*args, **kw))
		return c.target_key if c.ok else c.status

	def test_the_perspective_table(self):
		cases = [
			# (source, issuer, recipient, seller, buyer) -> target
			(("Purchase Order", CUST, US, US, CUST), "Sales Order"),  # a customer's PO to us
			(
				("Purchase Order", SUPP, US, US, SUPP),
				"Sales Order",
			),  # a supplier buying from us: the relationship on the document decides
			(("Purchase Order", US, SUPP, SUPP, US), "Purchase Order"),  # our own PO
			(("Sales Order", SUPP, US, SUPP, US), "Purchase Order"),  # a supplier's order confirmation to us
			(("Invoice", SUPP, US, SUPP, US), "Purchase Invoice"),  # a supplier bills us
			(("Invoice", CUST, US, CUST, US), "Purchase Invoice"),  # a customer bills us for what it supplied
			(
				("Sales Invoice", US, CUST, US, CUST),
				"Sales Invoice",
			),  # our invoice (or a copy of it sent back to us)
			(("Delivery Note", SUPP, US, SUPP, US), "Purchase Receipt"),  # a supplier delivered to us
			(("Delivery Note", US, CUST, US, CUST), "Delivery Note"),  # we delivered
			(("Goods Receipt Note", US, SUPP, SUPP, US), "Purchase Receipt"),
			(("Quotation", US, CUST, US, CUST), "Quotation"),
			(("Credit Note", US, CUST, US, CUST), "Sales Invoice Return"),  # our credit note
			(
				("Credit Note", SUPP, US, SUPP, US),
				"Purchase Invoice Return",
			),  # a supplier credits us: our Debit Note
			(
				("Debit Note", US, SUPP, SUPP, US),
				"Purchase Invoice Return",
			),  # we debit the supplier for returned goods
			(("Debit Note", CUST, US, US, CUST), "Sales Invoice Return"),  # a customer returns goods to us
		]
		for args, expected in cases:
			self.assertEqual(self.target(*args), expected, args)

	def test_a_title_says_purchase_invoice_but_we_issued_it(self):
		# "Purchase Invoice" printed by our company to a customer is our sales invoice: the title is ignored
		self.assertEqual(self.target("Purchase Invoice", US, CUST, US, CUST), "Sales Invoice")
		self.assertEqual(self.target("Sales Invoice", SUPP, US, SUPP, US), "Purchase Invoice")

	def test_a_bill_between_two_other_parties_is_never_created(self):
		c = classify(doc("Sales Invoice", SUPP, CUST, SUPP, CUST))
		self.assertEqual((c.status, c.ask, c.target_key), (D.NEEDS_ROLE, "role", None))
		self.assertIn(SUPP, c.message)

	def test_contradictory_parties_are_asked_about(self):
		# a "Sales Order" issued by a customer that names us as the seller: the title and the parties disagree
		c = classify(doc("Sales Order", CUST, US, US, CUST))
		self.assertEqual((c.status, c.ask, c.target_key), (D.CONFLICT, "role", None))
		# the user confirms the relationship the document states: then it is our Sales Order
		c = classify(doc("Sales Order", CUST, US, US, CUST), selected_role="seller")
		self.assertEqual((c.status, c.target_key, c.counterparty), (D.OK, "Sales Order", CUST))
		# our own purchase order whose roles were read the wrong way round is not turned into a Sales Order
		self.assertEqual(classify(doc("Purchase Order", US, SUPP, US, SUPP)).status, D.CONFLICT)

	def test_a_selected_role_never_overrides_what_the_document_proves(self):
		c = classify(doc("Invoice", SUPP, US, SUPP, US), selected_role="seller")
		self.assertEqual((c.status, c.target_key), (D.CONFLICT, None))
		c = classify(
			doc("Invoice", SUPP, CUST, SUPP, CUST), selected_role="buyer"
		)  # neither party matched: the user decides
		self.assertEqual(
			(c.target_key, c.counterparty, c.role_source), ("Purchase Invoice", SUPP, "selected")
		)

	def test_both_sides_our_company_or_the_other_side_our_company(self):
		self.assertEqual(classify(doc("Invoice", US, US, US, US)).status, D.CONFLICT)
		c = classify(doc("Invoice", US, SISTER, US, SISTER), usable=(US, SISTER))
		self.assertEqual(
			(c.status, c.ask, c.target_key), (D.CONFLICT, "company", None)
		)  # an intercompany document needs a person
		c = classify(doc("Invoice", US, SISTER, US, SISTER), usable=(US, SISTER), selected_company=US)
		self.assertEqual(c.target_key, "Sales Invoice")  # once the user chose whose books it belongs in

	def test_unknown_unsupported_and_unconfident_documents(self):
		self.assertEqual(classify(doc("Proforma Invoice", US, CUST, US, CUST)).status, D.UNKNOWN_TYPE)
		self.assertEqual(classify(doc("Spaceship Manifest")).status, D.UNKNOWN_TYPE)
		self.assertEqual(classify(doc("Invoice", US, CUST, US, CUST, confidence=0.4)).status, D.UNKNOWN_TYPE)
		self.assertEqual(
			classify(doc("Quotation", SUPP, US, SUPP, US)).status, D.UNSUPPORTED
		)  # a Supplier Quotation for us
		self.assertEqual(
			classify(doc("Goods Receipt Note", CUST, US, US, CUST)).status, D.UNSUPPORTED
		)  # the customer's own record
		# a debit memo without an issuer: a return or a further charge, nobody can tell
		self.assertEqual(
			classify({"document_type": "Debit Memo", "supplier_name": US, "customer_name": CUST}).status,
			D.UNSUPPORTED,
		)

	def test_internal_documents(self):
		self.assertEqual(self.target("Material Request", US), "Material Request")
		self.assertEqual(self.target("Bill of Materials"), "BOM")
		c = classify(doc("Material Request", SISTER), usable=(US, SISTER))
		self.assertEqual(
			(c.status, c.ask), (D.NEEDS_COMPANY, "company")
		)  # names another of the user's companies

	def test_company_names_match_only_the_same_organisation(self):
		self.assertEqual(
			self.target("Invoice", "AIDIR Own Co", CUST, "AIDIR Own Co", CUST), "Sales Invoice"
		)  # legal form words
		self.assertEqual(
			self.target("Invoice", "AOCPL", CUST, "AOCPL", CUST), "Sales Invoice"
		)  # exact abbreviation
		self.assertEqual(
			classify(doc("Invoice", "AIDIR Own Cmpany", CUST, "AIDIR Own Cmpany", CUST)).status, D.NEEDS_ROLE
		)  # a typo is not us

	def test_our_company_comes_from_the_user_not_from_a_name(self):
		usable = (US, SISTER)
		c = classify(doc("Invoice", SISTER, CUST, SISTER, CUST), usable=usable, default=US)
		self.assertEqual(
			(c.status, c.ask, c.company_candidates), (D.NEEDS_COMPANY, "company", [SISTER])
		)  # named, but not the default
		c = classify(
			doc("Invoice", SISTER, CUST, SISTER, CUST), usable=usable, default=US, selected_company=SISTER
		)
		self.assertEqual((c.company, c.company_source, c.target_key), (SISTER, "selected", "Sales Invoice"))
		c = classify(
			doc("Invoice", SISTER, CUST, SISTER, CUST), usable=(US,), default=US, selected_company=SISTER
		)
		self.assertEqual(c.company, US)  # a company the user can't use is never selected
		c = classify(doc("Invoice", CUST, "Someone", CUST, "Someone"), usable=usable, default=None)
		self.assertEqual(c.status, D.NEEDS_COMPANY)

	def test_prompt_b2_extractions_still_classify(self):
		# B2 had no issuer/recipient: supplier_name was the seller and customer_name the buyer
		self.assertEqual(
			classify(
				{"document_type": "Sales Invoice", "supplier_name": US, "customer_name": CUST}
			).target_key,
			"Sales Invoice",
		)
		self.assertEqual(
			classify(
				{"document_type": "Sales Invoice", "supplier_name": SUPP, "customer_name": US}
			).target_key,
			"Purchase Invoice",
		)


# ------------------------------------------------------------------------------------------------
# End to end: real mapping, validation and insert()
# ------------------------------------------------------------------------------------------------
class DocumentCase(IntegrationTestCase):
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
		cg = frappe.db.get_value("Customer Group", {"is_group": 0}, "name")
		te = frappe.db.get_value("Territory", {"is_group": 0}, "name")
		sg = frappe.db.get_value("Supplier Group", {"is_group": 0}, "name")
		ig = frappe.db.get_value("Item Group", {"is_group": 0}, "name")
		cls.customer = (
			frappe.get_doc(
				{
					"doctype": "Customer",
					"customer_name": "AIDIR Customer",
					"customer_group": cg,
					"territory": te,
				}
			)
			.insert()
			.name
		)
		cls.supplier = (
			frappe.get_doc({"doctype": "Supplier", "supplier_name": "AIDIR Supplier", "supplier_group": sg})
			.insert()
			.name
		)
		for code, stock in (("AIDIR-SVC", 0), ("AIDIR-STK", 1), ("AIDIR-RAW", 1), ("AIDIR-FG", 1)):
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
					"valuation_rate": 4,
				}
			).insert()
		cls.warehouse = (
			frappe.get_doc({"doctype": "Warehouse", "warehouse_name": "AIDIR Stores", "company": cls.company})
			.insert()
			.name
		)

	def setUp(self):
		frappe.set_user("Administrator")
		self.enqueue = mock.patch("frappe.enqueue").start()
		self.addCleanup(mock.patch.stopall)

	def extraction(self, kind, issuer, recipient, seller, buyer, items=None, **extra):
		import random

		# fixtures persist across the tests of a class: a random rate keeps "same party, date and total" from matching
		rate = extra.pop("rate", random.randint(101, 9999))
		data = {
			"document_type": kind,
			"issuer_name": issuer,
			"recipient_name": recipient,
			"supplier_name": seller,
			"customer_name": buyer,
			"invoice_number": "AIDIR-" + frappe.generate_hash(length=8),
			"date": nowdate(),
			"due_date": None,
			"delivery_date": None,
			"currency": None,
			"items": items
			or [
				{
					"item_code": "AIDIR-SVC",
					"item_name": "AIDIR-SVC name",
					"description": None,
					"qty": 3,
					"uom": "Nos",
					"rate": rate,
					"amount": 3 * rate,
				}
			],
			"subtotal": 3 * rate,
			"discount_amount": None,
			"taxes": [],
			"tax_rate": None,
			"tax_amount": None,
			"total_amount": 3 * rate,
		}
		data.update(extra)
		return data

	def entry(self, data):
		e = frappe.get_doc(
			{
				"doctype": "AI Document Entry",
				"status": "Processed",
				"file": test_file("png"),
				"extracted_data": json.dumps(data),
			}
		).insert()
		apply_mapping(e)
		e.save()
		return e

	def create(self, data, **kw):
		e = self.entry(data)
		result = create_document(e.name, **kw)
		self.assertTrue(
			result["created"],
			(result.get("message"), (result.get("review") or {}).get("validation", {}).get("errors")),
		)
		created = frappe.get_doc(result["doctype"], result["name"])
		self.assertEqual(created.docstatus, 0)  # always a Draft
		with self.assertRaises(
			frappe.ValidationError
		):  # a second confirmation never creates a second document
			create_document(e.name)
		self.assertEqual(frappe.db.get_value("AI Document Entry", e.name, "created_document"), created.name)
		return created, result

	def stock_row(self, code="AIDIR-STK", qty=2, rate=7):
		return [
			{
				"item_code": code,
				"item_name": f"{code} name",
				"description": None,
				"qty": qty,
				"uom": "Nos",
				"rate": rate,
				"amount": qty * rate,
			}
		]


class TestDocumentTypes(DocumentCase):
	def test_sales_invoice(self):
		si, result = self.create(
			self.extraction(
				"Invoice", self.company, "AIDIR Customer", self.company, "AIDIR Customer", rate=11
			)
		)
		self.assertEqual(
			(si.doctype, si.customer, si.company, si.is_return),
			("Sales Invoice", self.customer, self.company, 0),
		)
		self.assertEqual(
			[(r.item_code, r.qty, r.rate, r.uom) for r in si.items], [("AIDIR-SVC", 3, 11, "Nos")]
		)
		self.assertEqual((si.grand_total, str(si.posting_date)), (33, nowdate()))
		self.assertEqual(result["label"], "Sales Invoice")

	def test_purchase_invoice_from_a_supplier_bill(self):
		data = self.extraction(
			"Sales Invoice",
			"AIDIR Supplier",
			self.company,
			"AIDIR Supplier",
			self.company,
			rate=10,
			due_date=add_days(nowdate(), 30),
		)
		pi, _ = self.create(data)
		self.assertEqual(
			(pi.doctype, pi.supplier, pi.company, pi.bill_no),
			("Purchase Invoice", self.supplier, self.company, data["invoice_number"]),
		)
		self.assertEqual(
			(str(pi.due_date), pi.grand_total, pi.items[0].item_code, pi.items[0].qty),
			(add_days(nowdate(), 30), 30, "AIDIR-SVC", 3),
		)
		self.assertFalse(
			frappe.db.exists("Sales Invoice", {"remarks": ["like", f"%{data['invoice_number']}%"]})
		)  # never a Sales Invoice

	def test_sales_order_from_a_customer_purchase_order(self):
		data = self.extraction(
			"Purchase Order",
			"AIDIR Customer",
			self.company,
			self.company,
			"AIDIR Customer",
			items=self.stock_row(),
			delivery_date=add_days(nowdate(), 10),
			warehouse="AIDIR Stores",
			subtotal=14,
			total_amount=14,
		)
		so, _ = self.create(data)
		self.assertEqual(
			(so.doctype, so.customer, so.po_no, str(so.delivery_date)),
			("Sales Order", self.customer, data["invoice_number"], add_days(nowdate(), 10)),
		)
		self.assertEqual(
			[(r.item_code, r.qty, r.rate, r.warehouse, str(r.delivery_date)) for r in so.items],
			[("AIDIR-STK", 2, 7, self.warehouse, add_days(nowdate(), 10))],
		)

	def test_purchase_order_ours_and_from_a_supplier_order_confirmation(self):
		ours = self.extraction(
			"Purchase Order",
			self.company,
			"AIDIR Supplier",
			"AIDIR Supplier",
			self.company,
			items=self.stock_row(),
			delivery_date=add_days(nowdate(), 5),
			warehouse="AIDIR Stores",
			subtotal=14,
			total_amount=14,
		)
		po, _ = self.create(ours)
		self.assertEqual(
			(po.doctype, po.supplier, str(po.schedule_date), po.items[0].warehouse),
			("Purchase Order", self.supplier, add_days(nowdate(), 5), self.warehouse),
		)
		confirmation = self.extraction(
			"Sales Order",
			"AIDIR Supplier",
			self.company,
			"AIDIR Supplier",
			self.company,
			items=self.stock_row(qty=4),
			delivery_date=add_days(nowdate(), 6),
			warehouse="AIDIR Stores",
			subtotal=28,
			total_amount=28,
		)
		po2, _ = self.create(confirmation)
		self.assertEqual((po2.doctype, po2.supplier, po2.items[0].qty), ("Purchase Order", self.supplier, 4))

	def test_an_order_without_its_required_date_asks_for_it(self):
		outcome = map_document(
			self.extraction(
				"Purchase Order",
				"AIDIR Customer",
				self.company,
				self.company,
				"AIDIR Customer",
				items=self.stock_row(),
				warehouse="AIDIR Stores",
			)
		)
		self.assertEqual(outcome.target_doctype, "Sales Order")
		self.assertIn("delivery_date", {i.field for i in validate_outcome(outcome).blocking})

	def test_quotation(self):
		qtn, _ = self.create(
			self.extraction(
				"Quotation",
				self.company,
				"AIDIR Customer",
				self.company,
				"AIDIR Customer",
				valid_till=add_days(nowdate(), 15),
			)
		)
		self.assertEqual(
			(qtn.doctype, qtn.quotation_to, qtn.party_name, str(qtn.valid_till)),
			("Quotation", "Customer", self.customer, add_days(nowdate(), 15)),
		)

	def test_delivery_note_and_purchase_receipt(self):
		dn, _ = self.create(
			self.extraction(
				"Delivery Note",
				self.company,
				"AIDIR Customer",
				self.company,
				"AIDIR Customer",
				items=self.stock_row(),
				warehouse="AIDIR Stores",
				subtotal=14,
				total_amount=14,
			)
		)
		self.assertEqual(
			(dn.doctype, dn.customer, dn.items[0].warehouse, dn.items[0].qty),
			("Delivery Note", self.customer, self.warehouse, 2),
		)
		data = self.extraction(
			"Delivery Challan",
			"AIDIR Supplier",
			self.company,
			"AIDIR Supplier",
			self.company,
			items=self.stock_row(qty=5),
			warehouse="AIDIR Stores",
			subtotal=35,
			total_amount=35,
		)
		pr, _ = self.create(data)
		self.assertEqual(
			(pr.doctype, pr.supplier, pr.supplier_delivery_note, pr.items[0].warehouse, pr.items[0].qty),
			("Purchase Receipt", self.supplier, data["invoice_number"], self.warehouse, 5),
		)

	def test_material_request(self):
		data = self.extraction(
			"Material Requisition",
			self.company,
			None,
			None,
			None,
			items=[
				{
					"item_code": "AIDIR-STK",
					"item_name": "AIDIR-STK name",
					"qty": 8,
					"uom": "Nos",
					"rate": None,
					"amount": None,
				}
			],
			subtotal=None,
			total_amount=None,
			warehouse="AIDIR Stores",
			delivery_date=add_days(nowdate(), 3),
			material_request_type="Purchase",
		)
		mr, _ = self.create(data)
		self.assertEqual(
			(mr.material_request_type, str(mr.schedule_date)), ("Purchase", add_days(nowdate(), 3))
		)
		self.assertEqual(
			[(r.item_code, r.qty, r.warehouse, str(r.schedule_date)) for r in mr.items],
			[("AIDIR-STK", 8, self.warehouse, add_days(nowdate(), 3))],
		)

	def test_material_request_without_a_purpose_asks_for_it(self):
		outcome = map_document(
			self.extraction(
				"Material Request",
				self.company,
				None,
				None,
				None,
				items=self.stock_row(),
				warehouse="AIDIR Stores",
				delivery_date=add_days(nowdate(), 3),
			)
		)
		report = validate_outcome(outcome)
		self.assertIn("material_request_type", {i.field for i in report.blocking})
		self.assertIn("Purchase", [c["name"] for c in outcome.results["material_request_type"].candidates])

	def test_bom_is_a_draft_that_is_neither_active_nor_default(self):
		data = self.extraction(
			"Bill of Materials",
			self.company,
			None,
			None,
			None,
			bom_item_code="AIDIR-FG",
			bom_quantity=2,
			items=[
				{
					"item_code": "AIDIR-RAW",
					"item_name": "AIDIR-RAW name",
					"qty": 6,
					"uom": "Nos",
					"rate": None,
					"amount": None,
				}
			],
			subtotal=None,
			total_amount=None,
		)
		bom, _ = self.create(data)
		self.assertEqual(
			(bom.item, bom.quantity, bom.company, bom.is_active, bom.is_default),
			("AIDIR-FG", 2, self.company, 0, 0),
		)
		self.assertEqual([(r.item_code, r.qty) for r in bom.items], [("AIDIR-RAW", 6)])
		# the same BOM again is reported as a possible duplicate
		report = validate_outcome(map_document(data))
		self.assertTrue(report.duplicate)

	def test_bom_component_cannot_be_the_finished_item(self):
		data = self.extraction(
			"BOM",
			self.company,
			None,
			None,
			None,
			bom_item_code="AIDIR-FG",
			items=[
				{
					"item_code": "AIDIR-FG",
					"item_name": "AIDIR-FG name",
					"qty": 1,
					"uom": "Nos",
					"rate": None,
					"amount": None,
				}
			],
			subtotal=None,
			total_amount=None,
		)
		self.assertIn("bom_recursion", {i.code for i in validate_outcome(map_document(data)).errors})

	def test_credit_note_is_a_sales_invoice_return_linked_to_the_original(self):
		original = frappe.get_doc(
			{
				"doctype": "Sales Invoice",
				"customer": self.customer,
				"company": self.company,
				"posting_date": nowdate(),
				"items": [{"item_code": "AIDIR-SVC", "qty": 3, "rate": 10}],
			}
		).insert()
		original.submit()  # test data: the assistant itself never submits
		data = self.extraction(
			"Credit Note",
			self.company,
			"AIDIR Customer",
			self.company,
			"AIDIR Customer",
			original_invoice_number=original.name,
			items=[
				{
					"item_code": "AIDIR-SVC",
					"item_name": "AIDIR-SVC name",
					"qty": 1,
					"uom": "Nos",
					"rate": 10,
					"amount": 10,
				}
			],
			subtotal=10,
			total_amount=10,
		)
		ret, result = self.create(data)
		self.assertEqual(
			(ret.doctype, ret.is_return, ret.return_against, ret.customer),
			("Sales Invoice", 1, original.name, self.customer),
		)
		self.assertEqual((ret.items[0].qty, ret.grand_total), (-1, -10))
		self.assertEqual(result["label"], "Credit Note (Sales Invoice return)")

	def test_a_return_against_an_unknown_invoice_must_be_resolved(self):
		data = self.extraction(
			"Credit Note",
			self.company,
			"AIDIR Customer",
			self.company,
			"AIDIR Customer",
			original_invoice_number="NO-SUCH-INV-77",
		)
		e = self.entry(data)
		self.assertIn("return_against", {x["field"] for x in json.loads(e.validation_results)["errors"]})
		self.assertFalse(create_document(e.name)["created"])
		apply_corrections(
			e.name, {"return_against": "__none__"}
		)  # the reviewer decides: an unlinked credit note
		result = create_document(e.name)
		self.assertTrue(result["created"])
		self.assertEqual(
			frappe.db.get_value("Sales Invoice", result["name"], ["is_return", "return_against"]), (1, None)
		)

	def test_supplier_credit_note_is_a_purchase_invoice_return(self):
		ret, result = self.create(
			self.extraction("Credit Note", "AIDIR Supplier", self.company, "AIDIR Supplier", self.company)
		)
		self.assertEqual(
			(ret.doctype, ret.is_return, ret.supplier, ret.items[0].qty),
			("Purchase Invoice", 1, self.supplier, -3),
		)
		self.assertEqual(result["label"], "Debit Note (Purchase Invoice return)")

	def test_supplier_invoice_number_is_a_duplicate_check(self):
		data = self.extraction(
			"Invoice",
			"AIDIR Supplier",
			self.company,
			"AIDIR Supplier",
			self.company,
			invoice_number="AIDIR-BILL-0042",
			rate=13,
		)
		self.create(data)
		again = self.entry(dict(data, rate=99))
		validation = json.loads(again.validation_results)
		self.assertTrue(validation["duplicate"])
		self.assertIn("AIDIR-BILL-0042", " ".join(d["reason"] for d in validation["duplicates"]))
		self.assertFalse(create_document(again.name)["created"])


class TestDiscounts(DocumentCase):
	def test_discount_without_tax_is_persisted_and_totals_match(self):
		si, result = self.create(
			self.extraction(
				"Invoice",
				self.company,
				"AIDIR Customer",
				self.company,
				"AIDIR Customer",
				rate=10,
				discount_amount=5,
				total_amount=25,
			)
		)
		self.assertEqual(
			(si.discount_amount, si.apply_discount_on, si.net_total, si.grand_total), (5, "Net Total", 25, 25)
		)
		self.assertIsNone(result["totals_note"])

	def test_discount_base_is_read_from_the_documents_numbers(self):
		from ai_erpnext.mapping.transaction import discount_base

		before = {
			"discount_amount": 10,
			"subtotal": 100,
			"tax_rate": 18,
			"tax_amount": 16.2,
			"total_amount": 106.2,
		}
		after = {
			"discount_amount": 10,
			"subtotal": 100,
			"tax_rate": 18,
			"tax_amount": 18,
			"total_amount": 108,
		}
		self.assertEqual(discount_base(before)[0], "Net Total")
		self.assertEqual(discount_base(after)[0], "Grand Total")
		self.assertIsNone(
			discount_base({"discount_amount": 10, "subtotal": 100, "tax_amount": 18})[0]
		)  # no rate: undecidable
		self.assertEqual(
			discount_base(
				{"discount_amount": 10, "subtotal": 100, "tax_amount": 16.2, "tax_component_rate": 18}
			)[0],
			"Net Total",
		)
		self.assertEqual(discount_base({"discount_amount": 0})[0], None)

	def test_an_undecidable_discount_base_blocks_until_answered(self):
		data = self.extraction(
			"Invoice",
			self.company,
			"AIDIR Customer",
			self.company,
			"AIDIR Customer",
			rate=10,
			discount_amount=3,
			tax_amount=5,
			total_amount=32,
		)
		e = self.entry(data)
		self.assertIn(
			"apply_discount_on",
			{x["field"] for x in json.loads(e.validation_results)["errors"] if x["category"] == "mapping"},
		)
		self.assertFalse(create_document(e.name)["created"])
		with self.assertRaises(frappe.ValidationError):
			apply_corrections(e.name, {"apply_discount_on": "Somewhere"})
		apply_corrections(e.name, {"apply_discount_on": "Grand Total"})
		result = create_document(e.name, acknowledge_errors=1)
		si = frappe.get_doc("Sales Invoice", result["name"])
		self.assertEqual((si.discount_amount, si.apply_discount_on), (3, "Grand Total"))
		# the draft has no tax rows (no template chosen), so the result says its total differs from the document
		self.assertIn("32", result["totals_note"])

	def test_discount_larger_than_the_subtotal_is_a_data_error(self):
		outcome = map_document(
			self.extraction(
				"Invoice",
				self.company,
				"AIDIR Customer",
				self.company,
				"AIDIR Customer",
				rate=10,
				discount_amount=500,
				total_amount=-470,
			)
		)
		self.assertIn("discount_too_large", {i.code for i in validate_outcome(outcome).data_errors})


class TestNoTypeOverride(DocumentCase):
	def test_classification_cannot_be_changed_from_the_browser_or_the_form(self):
		data = self.extraction("Invoice", "AIDIR Supplier", self.company, "AIDIR Supplier", self.company)
		e = self.entry(data)
		self.assertEqual(e.target_doctype, "Purchase Invoice")
		with self.assertRaises(frappe.ValidationError):
			remap(e.name, target_doctype="Sales Invoice")
		self.assertEqual(
			frappe.db.get_value("AI Document Entry", e.name, "target_doctype"), "Purchase Invoice"
		)
		e.reload()
		e.target_doctype = "Sales Invoice"  # a Desk edit of the field
		apply_mapping(e)
		self.assertEqual(e.target_doctype, "Purchase Invoice")
		self.assertEqual(
			remap(e.name, target_doctype="Purchase Invoice")["target_doctype"], "Purchase Invoice"
		)  # confirming is fine

	def test_an_unclassifiable_document_creates_nothing(self):
		e = self.entry(
			self.extraction("Invoice", "AIDIR Supplier", "AIDIR Customer", "AIDIR Supplier", "AIDIR Customer")
		)
		self.assertFalse(e.target_doctype)
		before = (frappe.db.count("Sales Invoice"), frappe.db.count("Purchase Invoice"))
		result = create_document(e.name)
		self.assertFalse(result["created"])
		self.assertEqual((frappe.db.count("Sales Invoice"), frappe.db.count("Purchase Invoice")), before)
		apply_corrections(e.name, {"document_role": "buyer"})  # the user says which side they are on
		self.assertEqual(
			frappe.db.get_value("AI Document Entry", e.name, "target_doctype"), "Purchase Invoice"
		)
		with self.assertRaises(frappe.ValidationError):
			apply_corrections(e.name, {"document_role": "both"})


class TestImageChatFlows(DocumentCase):
	def setUp(self):
		super().setUp()
		self.route = mock.patch(
			"ai_erpnext.chat.llm.route", return_value={"mode": "chat", "reply": "general answer"}
		).start()
		self.s = chat.new_conversation()["session"]

	def send(self, text=None, file_url=None, action=None):
		return chat.send_message(
			session=self.s, text=text, file_url=file_url, action=json.dumps(action) if action else None
		)

	def pending(self):
		return next(
			m["question"] for m in reversed(chat.get_session(self.s)["messages"]) if m.get("question")
		)

	def answer(self, value):
		q = self.pending()
		return self.send(action={"question_id": q["id"], "value": value, "label": value})

	def chat_entry(self):
		return frappe.get_doc(
			"AI Document Entry", frappe.db.get_value("AI Chat Session", self.s, "active_entry")
		)

	def upload(self, data):
		self.send(text="please process this", file_url=test_file("png"))
		e = self.chat_entry()
		e.extracted_data = json.dumps(data)
		apply_mapping(e)
		e.save()
		return chat.poll(session=self.s, since=0)

	def texts(self, payload=None):
		return " ".join(m.get("text") or "" for m in (payload or chat.get_session(self.s))["messages"])

	def all_option_values(self):
		return [
			o.get("value")
			for m in chat.get_session(self.s)["messages"]
			if m.get("question")
			for o in m["question"].get("options") or []
		]

	def test_a_supplier_bill_becomes_a_purchase_invoice_never_a_sales_invoice(self):
		self.upload(
			self.extraction(
				"Sales Invoice", "AIDIR Supplier", self.company, "AIDIR Supplier", self.company, rate=17
			)
		)
		q = self.pending()
		self.assertEqual(q["kind"], "confirm")
		card = next(
			m["card"]
			for m in reversed(chat.get_session(self.s)["messages"])
			if (m.get("card") or {}).get("type") == "draft_preview"
		)
		values = {r["label"]: r["value"] for r in card["rows"]}
		self.assertEqual(
			(values["Will create"], values["Supplier"]), ("Draft Purchase Invoice", self.supplier)
		)
		self.assertIn("buyer", values["Your company"])
		r = self.answer("__create__")
		created = next(m["card"] for m in r["messages"] if (m.get("card") or {}).get("type") == "created")
		self.assertEqual(
			(created["doctype"], frappe.db.get_value("Purchase Invoice", created["name"], "docstatus")),
			("Purchase Invoice", 0),
		)
		self.assertIn("Purchase Invoice draft has been created", self.texts(r))

	def test_no_generic_yes_override_exists_for_unsupported_documents(self):
		before = frappe.db.count("Sales Invoice")
		self.upload(
			self.extraction(
				"Proforma Invoice", self.company, "AIDIR Customer", self.company, "AIDIR Customer"
			)
		)
		q = self.pending()
		self.assertEqual(q["kind"], "blocked")
		self.assertEqual([o["value"] for o in q["options"]], ["__cancel__"])
		self.assertNotIn("__yes__", self.all_option_values())
		r = self.send(text="yes, it's a sales invoice")
		self.assertEqual(frappe.db.count("Sales Invoice"), before)
		self.assertNotIn("draft has been created", self.texts(r))

	def test_a_supplier_quotation_is_refused_not_turned_into_a_quotation(self):
		self.upload(
			self.extraction("Quotation", "AIDIR Supplier", self.company, "AIDIR Supplier", self.company)
		)
		self.assertEqual(self.pending()["kind"], "blocked")
		self.assertIn("Supplier Quotation", self.texts())

	def test_the_user_says_which_side_they_are_on_when_neither_party_matches(self):
		self.upload(
			self.extraction(
				"Invoice", "AIDIR Supplier", "Someone Else Ltd", "AIDIR Supplier", "Someone Else Ltd"
			)
		)
		q = self.pending()
		self.assertEqual(q["kind"], "role")
		self.assertEqual([o["value"] for o in q["options"]][:2], ["seller", "buyer"])
		self.send(text="we are the buyer")
		self.assertEqual(self.chat_entry().target_doctype, "Purchase Invoice")
		self.assertEqual(self.pending()["kind"], "confirm")

	def test_contradictory_parties_are_asked_about_in_the_chat(self):
		self.upload(
			self.extraction(
				"Purchase Order",
				self.company,
				"AIDIR Supplier",
				self.company,
				"AIDIR Supplier",
				items=self.stock_row(),
			)
		)
		self.assertEqual(self.pending()["kind"], "role")
		self.assertIn("contradict", self.texts())
		self.assertFalse(self.chat_entry().target_doctype)

	def test_missing_supplier_is_created_only_after_confirmation(self):
		self.upload(
			self.extraction(
				"Invoice", "AIDIR Brand New Vendor", self.company, "AIDIR Brand New Vendor", self.company
			)
		)
		q = self.pending()
		self.assertEqual(q["kind"], "supplier")
		before = frappe.db.count("Supplier")
		self.answer("__create__")
		self.assertEqual(self.pending()["kind"], "master_confirm")
		self.send(text="set supplier type to Individual")  # a master-field correction in the image flow
		self.assertEqual(frappe.db.count("Supplier"), before)
		self.answer("__create__")
		created = frappe.get_doc("Supplier", {"supplier_name": "AIDIR Brand New Vendor"})
		self.assertEqual(created.supplier_type, "Individual")
		self.assertEqual(json.loads(self.chat_entry().review_overrides)["supplier"], created.name)
		self.assertEqual(self.pending()["kind"], "confirm")

	def test_customer_group_correction_while_creating_a_customer(self):
		group = (
			frappe.get_doc(
				{
					"doctype": "Customer Group",
					"customer_group_name": "AIDIR Group",
					"parent_customer_group": frappe.db.get_value("Customer Group", {"is_group": 1}, "name"),
				}
			)
			.insert()
			.name
		)
		self.upload(
			self.extraction("Invoice", self.company, "AIDIR Fresh Buyer", self.company, "AIDIR Fresh Buyer")
		)
		self.answer("__create__")
		r = self.send(text="set customer group to AIDIR Group")
		self.assertIn("Updated", self.texts(r))
		self.answer("__create__")
		self.assertEqual(
			frappe.db.get_value("Customer", {"customer_name": "AIDIR Fresh Buyer"}, "customer_group"), group
		)

	def test_start_over_keeps_the_extraction_and_drops_the_answers(self):
		self.upload(
			self.extraction(
				"Invoice", "AIDIR Supplier", "Someone Else Ltd", "AIDIR Supplier", "Someone Else Ltd"
			)
		)
		self.answer("buyer")
		self.assertEqual(self.chat_entry().target_doctype, "Purchase Invoice")
		r = self.send(text="start over")
		self.assertIn("earlier answers are cleared", self.texts(r))
		self.assertEqual(
			self.pending()["kind"], "role"
		)  # asked again: the role answer was one of the answers
		self.assertEqual(json.loads(self.chat_entry().extracted_data)["issuer_name"], "AIDIR Supplier")

	def test_why_lists_the_open_problems(self):
		self.upload(
			self.extraction("Invoice", self.company, "AIDIR Nobody Known", self.company, "AIDIR Nobody Known")
		)
		r = self.send(text="why?")
		self.assertIn("Open problems", self.texts(r))
		self.assertIn("AIDIR Nobody Known", self.texts(r))
		self.assertEqual(self.pending()["kind"], "customer")  # and the question comes back

	def test_discount_question_in_the_chat(self):
		self.upload(
			self.extraction(
				"Invoice",
				self.company,
				"AIDIR Customer",
				self.company,
				"AIDIR Customer",
				rate=10,
				discount_amount=3,
				tax_amount=5,
				total_amount=32,
			)
		)
		q = self.pending()
		self.assertEqual(q["kind"], "discount_base")
		self.answer("Net Total")
		self.assertNotEqual(self.pending()["kind"], "discount_base")
		self.assertEqual(json.loads(self.chat_entry().review_overrides)["apply_discount_on"], "Net Total")
