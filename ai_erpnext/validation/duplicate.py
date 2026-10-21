"""Duplicate detection for documents created from AI document images (permission-aware via get_list).

Sales Invoice has no field for an external invoice number, so drafts created by this app carry
`AI Source Invoice No: <no>` in remarks. Per definition (registry `duplicate_check`):
- "sales_invoice": an invoice named like the document number, one tagged with the same source number, or the same
  customer, company, posting date and grand total;
- "reference": the same party with the same reference (supplier invoice number, customer PO number, supplier delivery note);
- "party_date_total": the same party, company, date and grand total;
- "bom": a BOM of the same finished item, company and quantity.
Cancelled documents never count.
"""

import re

import frappe
from frappe.utils import flt

SOURCE_TAG = "AI Source Invoice No:"
_TAG_RE = re.compile(re.escape(SOURCE_TAG) + r"[ \t]*(\S[^\n]*?)[ \t]*$", re.MULTILINE)


def source_remark(invoice_number):
	return f"{SOURCE_TAG} {invoice_number}" if invoice_number else ""


def find_duplicate_sales_invoices(
	customer, invoice_number=None, company=None, posting_date=None, grand_total=None, is_return=None
):
	found = {}
	base = {"docstatus": ["<", 2]}
	if company:
		base["company"] = company
	if is_return is not None:
		base["is_return"] = 1 if is_return else 0

	if invoice_number:
		for row in frappe.get_list(
			"Sales Invoice",
			filters={**base, "name": invoice_number},
			fields=["name", "customer", "docstatus"],
		):
			found[row.name] = dict(row, reason=f"Sales Invoice named '{invoice_number}' already exists")

	if customer and invoice_number:
		found.update(
			{
				k: v
				for k, v in _tagged(
					"Sales Invoice", {**base, "customer": customer}, invoice_number, "customer"
				).items()
				if k not in found
			}
		)

	if customer and posting_date and grand_total:
		rows = frappe.get_list(
			"Sales Invoice",
			filters={**base, "customer": customer, "posting_date": posting_date},
			fields=["name", "customer", "docstatus", "grand_total"],
			limit=100,
		)
		for row in rows:
			if abs(abs(flt(row.grand_total)) - abs(flt(grand_total))) <= 0.01:
				found.setdefault(
					row.name,
					{
						"name": row.name,
						"customer": row.customer,
						"docstatus": row.docstatus,
						"reason": "Same customer, date and grand total",
					},
				)

	return list(found.values())


def _tagged(doctype, filters, number, party_field):
	"""Documents whose remarks carry the AI source tag for `number`."""
	out = {}
	rows = frappe.get_list(
		doctype,
		filters={**filters, "remarks": ["like", f"%{SOURCE_TAG}%"]},
		fields=["name", party_field, "docstatus", "remarks"],
		limit=500,
	)
	wanted = number.strip().lower()
	for row in rows:
		refs = {m.strip().lower() for m in _TAG_RE.findall(row.remarks or "")}
		if wanted in refs:
			out[row.name] = {
				"name": row.name,
				"party": row.get(party_field),
				"docstatus": row.docstatus,
				"reason": f"Already created from source document '{number}'",
			}
	return out


def find_duplicates(definition, mapped, canonical):
	"""Possible duplicates of the document about to be created, as [{"name", "reason", ...}]."""
	strategy, d = definition.duplicate_check, definition
	if not strategy:
		return []
	company = mapped.get("company")
	total = canonical.get("total_amount")
	if strategy == "sales_invoice":
		if not mapped.get("customer"):
			return []
		return find_duplicate_sales_invoices(
			mapped["customer"],
			canonical.get("invoice_number"),
			company,
			mapped.get("posting_date"),
			total,
			is_return=d.is_return,
		)
	base = {"docstatus": ["<", 2]}
	if company:
		base["company"] = company
	if d.is_return:
		base["is_return"] = 1
	elif frappe.get_meta(d.target_doctype).has_field("is_return"):
		base["is_return"] = 0
	found = {}
	party = mapped.get(d.party_field) if d.party_field else None
	if strategy == "bom":
		if not mapped.get("item"):
			return []
		for row in frappe.get_list(
			"BOM",
			filters={**base, "item": mapped["item"], "quantity": flt(mapped.get("quantity") or 1)},
			fields=["name", "docstatus", "is_active"],
			limit=20,
		):
			found[row.name] = {
				"name": row.name,
				"docstatus": row.docstatus,
				"reason": "A BOM for the same item and quantity already exists",
			}
		return list(found.values())
	if not party:
		return []
	base[d.party_field] = party
	ref = mapped.get(d.reference_field) if d.reference_field else None
	if strategy == "reference" and ref:
		for row in frappe.get_list(
			d.target_doctype, filters={**base, d.reference_field: ref}, fields=["name", "docstatus"], limit=20
		):
			found[row.name] = {
				"name": row.name,
				"docstatus": row.docstatus,
				"reason": f"Same {d.party_doctype.lower()} and reference '{ref}'",
			}
	if d.remarks_tag and canonical.get("invoice_number"):
		for name, row in _tagged(d.target_doctype, base, canonical["invoice_number"], d.party_field).items():
			found.setdefault(name, row)
	date = mapped.get(d.date_field) if d.date_field else None
	if date and total and frappe.get_meta(d.target_doctype).has_field("grand_total"):
		for row in frappe.get_list(
			d.target_doctype,
			filters={**base, d.date_field: date},
			fields=["name", "docstatus", "grand_total"],
			limit=100,
		):
			if abs(abs(flt(row.grand_total)) - abs(flt(total))) <= 0.01:
				found.setdefault(
					row.name,
					{
						"name": row.name,
						"docstatus": row.docstatus,
						"reason": f"Same {d.party_doctype.lower()}, date and grand total",
					},
				)
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
