"""Shared Draft builder for every registered AI document type.

Only the definition's supported fields are copied (never docstatus, owner, naming, accounts ...); per-type rules below
set what ERPNext needs for the document to mean what the source document means: the source's own dates and rates, the
discount on the base the document used, a return with negative quantities linked to its original invoice, a BOM that is
neither active nor default until someone reviews it. insert() then applies ERPNext's own permissions and validation.
"""

from frappe.utils import flt

from ai_erpnext.creation.document_builder import BaseDocumentBuilder
from ai_erpnext.validation.duplicate import source_remark


class TransactionBuilder(BaseDocumentBuilder):
	def build(self, mapped, context):
		doc = super().build(mapped, context)
		if self.definition.is_return:
			for row in doc.get(self.definition.items_table) or []:
				row.qty = -abs(flt(row.qty))
				if row.get("rate") is not None:
					row.rate = abs(flt(row.rate))
		return doc

	def prepare(self, doc, mapped, context):
		d, meta = self.definition, doc.meta
		if d.date_field == "posting_date" and mapped.get("posting_date"):
			doc.set_posting_time = 1  # keep the document's own date instead of ERPNext resetting it to today
		if meta.has_field("ignore_pricing_rule"):
			doc.ignore_pricing_rule = 1  # rates come from the source document, not from pricing rules
		if meta.has_field("update_stock"):
			doc.update_stock = 0  # stock moves through Delivery Notes / Purchase Receipts, never as a side effect
		if d.target_doctype == "Quotation":
			doc.quotation_to = "Customer"
		if d.target_doctype == "BOM":
			doc.is_active = 0  # a reviewed BOM is activated by a person, never by the assistant
			doc.is_default = 0
		if d.is_return:
			doc.is_return = 1
		if mapped.get("discount_amount") and mapped.get("apply_discount_on"):
			doc.apply_discount_on = mapped["apply_discount_on"]
			doc.discount_amount = -abs(flt(mapped["discount_amount"])) if d.is_return else abs(flt(mapped["discount_amount"]))
		if doc.get("taxes_and_charges") and not doc.get("taxes"):
			doc.set_taxes()  # standard ERPNext: copy rows from the chosen template
		if meta.has_field("remarks"):
			lines = [f"Created from AI Document Entry {context.get('entry')}" if context.get("entry") else "Created by AI Document Entry"]
			number = (context.get("canonical") or {}).get("invoice_number")
			if number:
				lines.append(source_remark(number))
			doc.remarks = "\n".join(lines)


def totals_note(doc, canonical, definition):
	"""A sentence when the draft's grand total differs from the document's printed total (e.g. taxes left out), else None."""
	printed = canonical.get("total_amount")
	if printed is None or not doc.meta.has_field("grand_total"):
		return None
	drafted, printed = abs(flt(doc.grand_total)), abs(flt(printed))
	if abs(drafted - printed) <= max(0.05, printed * 0.001):
		return None
	return (f"The draft's grand total is {flt(drafted, 2)} {doc.get('currency') or ''}, but the document shows {flt(printed, 2)}. "
		"Review the taxes and charges before submitting.").replace("  ", " ")
