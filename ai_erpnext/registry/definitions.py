"""Built-in AI document definitions: the document types an uploaded image may become.

Which definition a document gets is decided by detection/direction.py (kind of document x our company's role), never by
the printed title. All types share one adapter and the transaction mapper/validator/builder; the fields below are the
per-type semantics those components read. Every builder only copies the fields listed here.
"""

from ai_erpnext.registry.document_registry import DocumentDefinition, register

ADAPTER = "ai_erpnext.mapping.adapters.DocumentAdapter"
MAPPER = "ai_erpnext.mapping.transaction.TransactionMapper"
VALIDATOR = "ai_erpnext.validation.transaction.TransactionValidator"
BUILDER = "ai_erpnext.creation.transaction.TransactionBuilder"

SCHEMA = (
	"document_type",
	"issuer_name",
	"recipient_name",
	"supplier_name (seller)",
	"customer_name (buyer)",
	"invoice_number",
	"date",
	"due_date",
	"delivery_date",
	"valid_till",
	"reference_number",
	"original_invoice_number",
	"currency",
	"warehouse",
	"material_request_type",
	"bom_item_code",
	"bom_item_name",
	"bom_quantity",
	"bom_uom",
	"items[item_code,item_name,description,qty,uom,rate,amount]",
	"subtotal",
	"discount_amount",
	"taxes[name,rate,amount]",
	"tax_rate",
	"tax_amount",
	"total_amount",
)
SELLING_ITEM_FIELDS = ("item_code", "description", "qty", "uom", "rate", "warehouse")
MASTERS = {
	"customer": "Customer",
	"supplier": "Supplier",
	"company": "Company",
	"currency": "Currency",
	"warehouse": "Warehouse",
	"item": "Item",
	"uom": "UOM",
}


def _txn(key, target, label, party_field, party_doctype, date_field, **kw):
	party_key = party_doctype.lower() if party_doctype else None
	fields = [
		"company",
		"currency",
		date_field,
		"set_warehouse",
		"taxes_and_charges",
		"discount_amount",
		"apply_discount_on",
	]
	fields += [kw.get("second_date_field")] if kw.get("second_date_field") else []
	fields += [party_field] if party_field else []
	fields += [kw.get("reference_field")] if kw.get("reference_field") else []
	fields += ["is_return", "return_against"] if kw.get("is_return") else []
	required = ["company", "currency", date_field] + ([party_key] if party_key else [])
	item_fields = list(SELLING_ITEM_FIELDS) + ([kw["row_date_field"]] if kw.get("row_date_field") else [])
	return register(
		DocumentDefinition(
			key=key,
			target_doctype=target,
			label=label,
			adapter=ADAPTER,
			mapper=MAPPER,
			validator=VALIDATOR,
			builder=BUILDER,
			extraction_schema=SCHEMA,
			supported_fields=tuple(f for f in dict.fromkeys(fields) if f),
			supported_item_fields=tuple(item_fields),
			required_fields=tuple(required),
			required_item_fields=("item_code", "qty", "rate", "uom"),
			masters=MASTERS,
			party_field=party_field,
			party_doctype=party_doctype,
			date_field=date_field,
			supports_discount=True,
			**kw,
		)
	)


SALES = {"is_sales_item": 1}
PURCHASE = {"is_purchase_item": 1}
SALES_TAX, PURCHASE_TAX = "Sales Taxes and Charges Template", "Purchase Taxes and Charges Template"

SALES_INVOICE = _txn(
	"Sales Invoice",
	"Sales Invoice",
	"Sales Invoice",
	"customer",
	"Customer",
	"posting_date",
	aliases=("sales invoice", "tax invoice", "invoice (sales)", "customer invoice"),
	second_date_field="due_date",
	item_filters=SALES,
	tax_template_doctype=SALES_TAX,
	warehouse="header",
	party_currency_check=True,
	remarks_tag=True,
	duplicate_check="sales_invoice",
)
PURCHASE_INVOICE = _txn(
	"Purchase Invoice",
	"Purchase Invoice",
	"Purchase Invoice",
	"supplier",
	"Supplier",
	"posting_date",
	second_date_field="due_date",
	item_filters=PURCHASE,
	tax_template_doctype=PURCHASE_TAX,
	warehouse="header",
	party_currency_check=True,
	reference_field="bill_no",
	remarks_tag=True,
	duplicate_check="reference",
)
SALES_ORDER = _txn(
	"Sales Order",
	"Sales Order",
	"Sales Order",
	"customer",
	"Customer",
	"transaction_date",
	second_date_field="delivery_date",
	second_date_required=True,
	row_date_field="delivery_date",
	item_filters=SALES,
	tax_template_doctype=SALES_TAX,
	warehouse="header",
	reference_field="po_no",
	duplicate_check="reference",
)
PURCHASE_ORDER = _txn(
	"Purchase Order",
	"Purchase Order",
	"Purchase Order",
	"supplier",
	"Supplier",
	"transaction_date",
	second_date_field="schedule_date",
	second_date_required=True,
	row_date_field="schedule_date",
	item_filters=PURCHASE,
	tax_template_doctype=PURCHASE_TAX,
	warehouse="header",
	duplicate_check="party_date_total",
)
QUOTATION = _txn(
	"Quotation",
	"Quotation",
	"Quotation",
	"party_name",
	"Customer",
	"transaction_date",
	second_date_field="valid_till",
	item_filters=SALES,
	tax_template_doctype=SALES_TAX,
	duplicate_check="party_date_total",
)
DELIVERY_NOTE = _txn(
	"Delivery Note",
	"Delivery Note",
	"Delivery Note",
	"customer",
	"Customer",
	"posting_date",
	item_filters=SALES,
	tax_template_doctype=SALES_TAX,
	warehouse="header",
	reference_field="po_no",
	duplicate_check="party_date_total",
)
PURCHASE_RECEIPT = _txn(
	"Purchase Receipt",
	"Purchase Receipt",
	"Purchase Receipt",
	"supplier",
	"Supplier",
	"posting_date",
	item_filters=PURCHASE,
	tax_template_doctype=PURCHASE_TAX,
	warehouse="header",
	reference_field="supplier_delivery_note",
	remarks_tag=True,
	duplicate_check="reference",
)
SALES_INVOICE_RETURN = _txn(
	"Sales Invoice Return",
	"Sales Invoice",
	"Credit Note (Sales Invoice return)",
	"customer",
	"Customer",
	"posting_date",
	item_filters=SALES,
	tax_template_doctype=SALES_TAX,
	is_return=True,
	party_currency_check=True,
	remarks_tag=True,
	duplicate_check="sales_invoice",
)
PURCHASE_INVOICE_RETURN = _txn(
	"Purchase Invoice Return",
	"Purchase Invoice",
	"Debit Note (Purchase Invoice return)",
	"supplier",
	"Supplier",
	"posting_date",
	item_filters=PURCHASE,
	tax_template_doctype=PURCHASE_TAX,
	is_return=True,
	party_currency_check=True,
	reference_field="bill_no",
	remarks_tag=True,
	duplicate_check="reference",
)

MATERIAL_REQUEST = register(
	DocumentDefinition(
		key="Material Request",
		target_doctype="Material Request",
		label="Material Request",
		adapter=ADAPTER,
		mapper=MAPPER,
		validator=VALIDATOR,
		builder=BUILDER,
		extraction_schema=SCHEMA,
		supported_fields=(
			"company",
			"transaction_date",
			"schedule_date",
			"material_request_type",
			"set_warehouse",
		),
		supported_item_fields=("item_code", "description", "qty", "uom", "warehouse", "schedule_date"),
		required_fields=("company", "transaction_date", "material_request_type"),
		required_item_fields=("item_code", "qty", "uom"),
		masters=MASTERS,
		date_field="transaction_date",
		second_date_field="schedule_date",
		second_date_required=True,
		row_date_field="schedule_date",
		warehouse="rows",
		currency=False,
		rate_required=False,
		duplicate_check=None,
	)
)

BOM = register(
	DocumentDefinition(
		key="BOM",
		target_doctype="BOM",
		label="Bill of Materials (BOM)",
		adapter=ADAPTER,
		mapper=MAPPER,
		validator=VALIDATOR,
		builder=BUILDER,
		extraction_schema=SCHEMA,
		supported_fields=("company", "item", "quantity", "currency"),
		supported_item_fields=("item_code", "qty", "uom", "rate"),
		required_fields=("company", "bom_item"),
		required_item_fields=("item_code", "qty", "uom"),
		masters=MASTERS,
		rate_required=False,
		duplicate_check="bom",
	)
)
