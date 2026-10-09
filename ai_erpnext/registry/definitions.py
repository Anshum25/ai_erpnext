"""Built-in AI document definitions. Only Sales Invoice is implemented in this milestone."""

from ai_erpnext.registry.document_registry import DocumentDefinition, register

SALES_INVOICE = register(
	DocumentDefinition(
		key="Sales Invoice",
		target_doctype="Sales Invoice",
		aliases=("sales invoice", "tax invoice", "invoice (sales)", "customer invoice"),
		adapter="ai_erpnext.mapping.adapters.SalesInvoiceAdapter",
		mapper="ai_erpnext.mapping.sales_invoice.SalesInvoiceMapper",
		validator="ai_erpnext.validation.sales_invoice.SalesInvoiceValidator",
		builder="ai_erpnext.creation.sales_invoice.SalesInvoiceBuilder",
		extraction_schema=(
			"document_type",
			"customer_name",
			"customer_address",
			"company_name",
			"invoice_number",
			"invoice_date",
			"due_date",
			"terms",
			"currency",
			"warehouse",
			"items[item_code,item_name,description,qty,uom,rate,amount,warehouse]",
			"subtotal",
			"tax_rate",
			"tax_amount",
			"total_amount",
		),
		supported_fields=(
			"customer",
			"company",
			"posting_date",
			"due_date",
			"currency",
			"set_warehouse",
			"taxes_and_charges",
		),
		supported_item_fields=(
			"item_code",
			"description",
			"qty",
			"uom",
			"rate",
			"warehouse",
		),
		required_fields=("customer", "company", "posting_date", "currency"),
		required_item_fields=("item_code", "qty", "rate", "uom"),
		masters={
			"customer": "Customer",
			"company": "Company",
			"currency": "Currency",
			"warehouse": "Warehouse",
			"item": "Item",
			"uom": "UOM",
		},
	)
)
