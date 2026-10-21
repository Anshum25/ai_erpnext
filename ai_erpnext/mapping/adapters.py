"""Adapters: raw AI extraction JSON -> canonical, sanitised dict.

The extraction prompt is free-form, so key names vary between runs/models
(e.g. `unit` vs `uom`, `date` vs `invoice_date`). Adapters absorb that variation so
extraction never has to change. Extracted values are treated as untrusted input.

The canonical dict is perspective-free (issuer/recipient, seller/buyer); which side is our
company and which DocType the document becomes is decided by detection/direction.py.
"""

from ai_erpnext.mapping.normalize import clean_text, parse_number

MAX_ITEMS = 500


def first(data, *keys):
	for key in keys:
		value = data.get(key)
		if value not in (None, "", [], {}):
			return value
	return None


class BaseAdapter:
	def __init__(self, definition):
		self.definition = definition

	def adapt(self, extracted: dict) -> dict:
		raise NotImplementedError


class DocumentAdapter(BaseAdapter):
	def adapt(self, extracted: dict) -> dict:
		data = extracted if isinstance(extracted, dict) else {}
		customer = data.get("customer") if isinstance(data.get("customer"), dict) else {}
		items_raw = first(data, "items", "line_items", "lines")
		items_raw = items_raw if isinstance(items_raw, list) else []

		canonical = {
			"document_type": clean_text(data.get("document_type")),
			"issuer_name": clean_text(first(data, "issuer_name", "issued_by")),
			"recipient_name": clean_text(first(data, "recipient_name", "addressed_to")),
			# the buyer (Prompt B2/B3 customer_name) and the seller (supplier_name)
			"customer_name": clean_text(
				first(data, "customer_name", "buyer_name", "bill_to", "client_name") or customer.get("name")
			),
			"supplier_name": clean_text(first(data, "supplier_name", "seller_name", "vendor_name")),
			"customer_address": clean_text(first(data, "customer_address", "billing_address")),
			# Kept for older callers: names that may be our own company on a sales invoice.
			"company_names": [
				clean_text(v)
				for v in (
					first(data, "company", "company_name"),
					first(data, "seller_name", "issuer_name", "vendor_name"),
					data.get("supplier_name"),
				)
				if clean_text(v)
			],
			"invoice_number": clean_text(
				first(data, "invoice_number", "document_number", "invoice_no", "bill_no", "number"), 140
			),
			"invoice_date": clean_text(
				first(data, "invoice_date", "document_date", "posting_date", "date"), 64
			),
			"due_date": clean_text(data.get("due_date"), 64),
			"delivery_date": clean_text(
				first(data, "delivery_date", "required_by_date", "required_by", "schedule_date"), 64
			),
			"valid_till": clean_text(first(data, "valid_till", "valid_until"), 64),
			"reference_number": clean_text(
				first(data, "reference_number", "customer_po_number", "po_number"), 140
			),
			"original_invoice_number": clean_text(
				first(data, "original_invoice_number", "against_invoice", "return_against"), 140
			),
			"terms": clean_text(first(data, "terms", "payment_terms"), 140),
			"currency": clean_text(first(data, "currency", "currency_code"), 60),
			"warehouse": clean_text(first(data, "warehouse", "set_warehouse"), 140),
			"material_request_type": clean_text(first(data, "material_request_type", "purpose"), 60),
			"bom_item": {
				"item_code": clean_text(data.get("bom_item_code"), 140),
				"item_name": clean_text(first(data, "bom_item_name", "finished_item")),
				"qty": parse_number(data.get("bom_quantity")),
				"uom": clean_text(data.get("bom_uom"), 140),
			},
			"subtotal": parse_number(first(data, "subtotal", "sub_total", "net_total")),
			"discount_amount": parse_number(first(data, "discount_amount", "discount")),
			"tax_rate": parse_number(data.get("tax_rate")),
			"tax_amount": parse_number(first(data, "tax_amount", "total_tax", "tax")),
			"total_amount": parse_number(first(data, "total_amount", "grand_total", "total")),
			"items": [],
		}

		# Prompt B2 also lists tax components (e.g. CGST + SGST) under "taxes"; use their
		# sum only when the document-level tax_amount is missing.
		taxes = data.get("taxes")
		if canonical["tax_amount"] is None and isinstance(taxes, list):
			amounts = [parse_number(t.get("amount")) for t in taxes if isinstance(t, dict)]
			amounts = [a for a in amounts if a is not None]
			if amounts:
				canonical["tax_amount"] = sum(amounts)

		for idx, row in enumerate(items_raw[:MAX_ITEMS], start=1):
			if not isinstance(row, dict):
				continue
			canonical["items"].append(
				{
					"idx": idx,
					"item_code": clean_text(first(row, "item_code", "sku", "code", "product_code"), 140),
					"item_name": clean_text(first(row, "item_name", "name", "product", "item")),
					"description": clean_text(row.get("description")),
					"qty": parse_number(first(row, "qty", "quantity")),
					"uom": clean_text(first(row, "uom", "unit", "unit_of_measure", "units"), 140),
					"rate": parse_number(first(row, "rate", "unit_price", "price")),
					"amount": parse_number(first(row, "amount", "line_total", "total")),
					"warehouse": clean_text(row.get("warehouse"), 140),
				}
			)
		return canonical


# The Sales Invoice adapter was the first one; every type now shares the same canonical shape.
SalesInvoiceAdapter = DocumentAdapter
