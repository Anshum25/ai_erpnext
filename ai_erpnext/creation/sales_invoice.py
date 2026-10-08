from ai_erpnext.creation.document_builder import BaseDocumentBuilder
from ai_erpnext.validation.duplicate import source_remark


class SalesInvoiceBuilder(BaseDocumentBuilder):
	def prepare(self, doc, mapped, context):
		if mapped.get("posting_date"):
			# Keep the invoice's own date instead of ERPNext resetting it to today.
			doc.set_posting_time = 1
		# Rates come from the source document, not from pricing rules.
		doc.ignore_pricing_rule = 1
		doc.update_stock = 0
		lines = [f"Created from AI Document Entry {context.get('entry')}" if context.get("entry") else "Created by AI Document Entry"]
		invoice_number = (context.get("canonical") or {}).get("invoice_number")
		if invoice_number:
			lines.append(source_remark(invoice_number))
		doc.remarks = "\n".join(lines)
