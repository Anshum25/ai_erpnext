"""Duplicate detection for Sales Invoices created from AI documents.

Sales Invoice has no field for an external invoice number, so drafts created by this app
carry `AI Source Invoice No: <no>` in remarks. Checks (permission-aware via get_list):
1. A Sales Invoice whose name equals the document's invoice number.
2. A non-cancelled Sales Invoice for the same customer/company tagged with the same source number.
3. Possible duplicate: same customer, company, posting date and grand total.
"""

import re

import frappe
from frappe.utils import flt

SOURCE_TAG = "AI Source Invoice No:"
_TAG_RE = re.compile(re.escape(SOURCE_TAG) + r"[ \t]*(\S[^\n]*?)[ \t]*$", re.MULTILINE)


def source_remark(invoice_number):
	return f"{SOURCE_TAG} {invoice_number}" if invoice_number else ""


def find_duplicate_sales_invoices(customer, invoice_number=None, company=None, posting_date=None, grand_total=None):
	found = {}
	base = {"docstatus": ["<", 2]}
	if company:
		base["company"] = company

	if invoice_number:
		for row in frappe.get_list("Sales Invoice", filters={**base, "name": invoice_number}, fields=["name", "customer", "docstatus"]):
			found[row.name] = dict(row, reason=f"Sales Invoice named '{invoice_number}' already exists")

	if customer and invoice_number:
		rows = frappe.get_list(
			"Sales Invoice",
			filters={**base, "customer": customer, "remarks": ["like", f"%{SOURCE_TAG}%"]},
			fields=["name", "customer", "docstatus", "remarks"],
			limit_page_length=500,
		)
		wanted = invoice_number.strip().lower()
		for row in rows:
			refs = {m.strip().lower() for m in _TAG_RE.findall(row.remarks or "")}
			if wanted in refs:
				found.setdefault(row.name, {"name": row.name, "customer": row.customer, "docstatus": row.docstatus,
					"reason": f"Already created from source invoice '{invoice_number}'"})

	if customer and posting_date and grand_total:
		rows = frappe.get_list(
			"Sales Invoice",
			filters={**base, "customer": customer, "posting_date": posting_date},
			fields=["name", "customer", "docstatus", "grand_total"],
			limit_page_length=100,
		)
		for row in rows:
			if abs(flt(row.grand_total) - flt(grand_total)) <= 0.01:
				found.setdefault(row.name, {"name": row.name, "customer": row.customer, "docstatus": row.docstatus,
					"reason": "Same customer, date and grand total"})

	return list(found.values())


def check_duplicate_sales_invoice(mapped, canonical):
	"""Backward-compatible helper: True if a duplicate exists."""
	return bool(
		find_duplicate_sales_invoices(
			mapped.get("customer"),
			canonical.get("invoice_number"),
			mapped.get("company"),
			mapped.get("posting_date"),
			canonical.get("total_amount"),
		)
	)
