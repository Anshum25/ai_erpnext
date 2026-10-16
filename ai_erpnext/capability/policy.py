"""The single, explicit chatbot capability policy: what may be queried and what may be created.

Metadata (frappe.get_meta) says what ERPNext *supports*; it never grants anything. A DocType or field
is usable only when it is listed here AND present in the metadata AND permitted for the current user
(DocType, record, company and field/permlevel permissions). capability.service answers the questions;
chat.queries.REGISTRY is this REGISTRY (same object), so existing callers keep working.

Query capability (REGISTRY) and creation capability (chat.forms.SPECS / CREATABLE) are separate: a
queryable DocType is not creatable, and nothing here can edit, submit, cancel, delete or pay.
Add a DocType by adding a Spec with explicit fields and a test (tests/test_capability.py checks every
listed field against the installed metadata).
"""

from dataclasses import dataclass, field


@dataclass
class Spec:
	title: str  # field shown as the record's human title
	list_fields: tuple
	detail_fields: tuple
	date_field: str | None = None
	party_field: str | None = None
	party_title_field: str | None = None
	amount_field: str | None = None
	status_field: str | None = "status"
	search_fields: tuple = ()
	child: tuple = ()  # (table fieldname, fields)
	operations: tuple = ("list", "count", "details", "aggregate", "child_rows")
	labels: dict = field(default_factory=dict)
	taxes: bool = False  # has a "taxes" table (Sales/Purchase Taxes and Charges)
	# --- generic query plans (filters, aggregates, child tables, linked records); all optional ---
	optional_company: bool = False  # the company field may be empty (Lead, Task...): such records stay visible
	currency_field: str | None = None  # field holding the document currency of its Currency-type fields
	currency_map: dict = field(default_factory=dict)  # fieldname -> currency fieldname, or "@company" (company currency)
	extra_children: dict = field(default_factory=dict)  # more allowlisted child tables: table fieldname -> fields
	# DocTypes without a company field are only queryable when a reviewer marked them shared: master data (customers, items,
	# price lists, groups ...) that ERPNext itself shares between companies and protects with roles and User Permissions.
	shared: bool = False
	# Filters always applied unless the plan sets the same field itself (e.g. cancelled ledger entries are not data).
	default_filters: tuple = ()


TXN_ITEMS = ("items", ("item_code", "item_name", "qty", "uom", "rate", "amount"))
TAX_FIELDS = ("charge_type", "account_head", "description", "rate", "tax_amount", "total")

REGISTRY = {
	"Sales Invoice": Spec("customer_name", ("name", "customer_name", "posting_date", "grand_total", "currency", "status"),
		("name", "customer", "customer_name", "company", "posting_date", "due_date", "currency", "net_total", "total_taxes_and_charges",
			"grand_total", "outstanding_amount", "status"), "posting_date", "customer", "customer_name", "grand_total", search_fields=("name", "customer", "customer_name"),
		child=TXN_ITEMS, operations=("list", "count", "details", "aggregate", "child_rows", "sales_summary", "overdue", "unpaid", "outstanding_by_customer"), taxes=True,
		currency_field="currency"),
	"Purchase Invoice": Spec("supplier_name", ("name", "supplier_name", "posting_date", "grand_total", "currency", "status"),
		("name", "supplier", "supplier_name", "company", "posting_date", "due_date", "bill_no", "currency", "net_total", "total_taxes_and_charges",
			"grand_total", "outstanding_amount", "status"), "posting_date", "supplier", "supplier_name", "grand_total", search_fields=("name", "supplier", "supplier_name", "bill_no"),
		child=TXN_ITEMS, operations=("list", "count", "details", "aggregate", "child_rows", "overdue", "unpaid"), taxes=True, currency_field="currency"),
	"Sales Order": Spec("customer_name", ("name", "customer_name", "transaction_date", "grand_total", "currency", "status"),
		("name", "customer", "customer_name", "company", "transaction_date", "delivery_date", "set_warehouse", "currency", "net_total",
			"total_taxes_and_charges", "grand_total", "per_delivered", "per_billed", "status"),
		"transaction_date", "customer", "customer_name", "grand_total", search_fields=("name", "customer", "customer_name"), child=TXN_ITEMS, taxes=True, currency_field="currency"),
	"Purchase Order": Spec("supplier_name", ("name", "supplier_name", "transaction_date", "grand_total", "currency", "status"),
		("name", "supplier", "supplier_name", "company", "transaction_date", "schedule_date", "set_warehouse", "currency", "net_total",
			"total_taxes_and_charges", "grand_total", "per_received", "per_billed", "status"),
		"transaction_date", "supplier", "supplier_name", "grand_total", search_fields=("name", "supplier", "supplier_name"), child=TXN_ITEMS, taxes=True, currency_field="currency"),
	"Delivery Note": Spec("customer_name", ("name", "customer_name", "posting_date", "grand_total", "currency", "status"),
		("name", "customer", "customer_name", "company", "posting_date", "set_warehouse", "currency", "net_total", "total_taxes_and_charges",
			"grand_total", "per_billed", "status"),
		"posting_date", "customer", "customer_name", "grand_total", search_fields=("name", "customer", "customer_name"), child=TXN_ITEMS, taxes=True, currency_field="currency"),
	"Purchase Receipt": Spec("supplier_name", ("name", "supplier_name", "posting_date", "grand_total", "currency", "status"),
		("name", "supplier", "supplier_name", "company", "posting_date", "set_warehouse", "currency", "net_total", "total_taxes_and_charges",
			"grand_total", "per_billed", "status"),
		"posting_date", "supplier", "supplier_name", "grand_total", search_fields=("name", "supplier", "supplier_name"), child=TXN_ITEMS, taxes=True, currency_field="currency"),
	"Stock Entry": Spec("stock_entry_type", ("name", "stock_entry_type", "posting_date", "total_outgoing_value", "total_incoming_value", "docstatus"),
		("name", "stock_entry_type", "purpose", "company", "posting_date", "from_warehouse", "to_warehouse", "total_outgoing_value", "total_incoming_value", "docstatus"),
		"posting_date", amount_field="total_outgoing_value", status_field=None, search_fields=("name", "stock_entry_type"),
		child=("items", ("item_code", "item_name", "qty", "uom", "s_warehouse", "t_warehouse")),
		currency_map={"total_outgoing_value": "@company", "total_incoming_value": "@company"}),
	"Payment Entry": Spec("party_name", ("name", "payment_type", "party_name", "posting_date", "paid_amount", "paid_from_account_currency", "status"),
		("name", "payment_type", "party_type", "party", "party_name", "company", "posting_date", "mode_of_payment", "paid_amount",
			"paid_from_account_currency", "received_amount", "paid_to_account_currency", "reference_no", "status"),
		"posting_date", "party", "party_name", "paid_amount", search_fields=("name", "party", "party_name", "reference_no"),
		currency_map={"paid_amount": "paid_from_account_currency", "received_amount": "paid_to_account_currency"}),
	"Customer": Spec("customer_name", ("name", "customer_name", "customer_group", "territory", "customer_type"),
		("name", "customer_name", "customer_type", "customer_group", "territory", "default_currency", "mobile_no", "email_id", "tax_id",
			"customer_primary_contact", "primary_address", "disabled"),
		status_field=None, search_fields=("name", "customer_name", "mobile_no", "email_id"), operations=("list", "count", "details", "aggregate", "summary"), shared=True),
	"Supplier": Spec("supplier_name", ("name", "supplier_name", "supplier_group", "country", "supplier_type"),
		("name", "supplier_name", "supplier_type", "supplier_group", "country", "default_currency", "mobile_no", "email_id", "tax_id",
			"supplier_primary_contact", "primary_address", "disabled"),
		status_field=None, search_fields=("name", "supplier_name", "mobile_no", "email_id"), operations=("list", "count", "details", "aggregate", "summary"), shared=True),
	"Contact": Spec("full_name", ("name", "full_name", "company_name", "email_id", "mobile_no"),
		("name", "full_name", "company_name", "designation", "email_id", "mobile_no", "phone", "status"),
		status_field=None, search_fields=("name", "full_name", "first_name", "last_name", "email_id", "mobile_no", "phone", "company_name"), shared=True),
	"Item": Spec("item_name", ("name", "item_name", "item_group", "stock_uom", "is_stock_item"),
		("name", "item_name", "item_group", "stock_uom", "is_stock_item", "is_sales_item", "is_purchase_item", "description", "disabled"),
		status_field=None, search_fields=("name", "item_name"), shared=True),
	"Warehouse": Spec("warehouse_name", ("name", "warehouse_name", "company", "warehouse_type", "is_group"),
		("name", "warehouse_name", "company", "parent_warehouse", "warehouse_type", "is_group", "disabled"),
		status_field=None, search_fields=("name", "warehouse_name")),
	# HR: only non-sensitive organisational fields; salary, bank and personal data are never exposed.
	"Employee": Spec("employee_name", ("name", "employee_name", "department", "designation", "status"),
		("name", "employee_name", "department", "designation", "company", "branch", "status"), status_field="status",
		search_fields=("name", "employee_name")),
	# --- query-only DocTypes (no creation or edit capability); each verified against the installed metadata ---
	"Quotation": Spec("customer_name", ("name", "customer_name", "transaction_date", "valid_till", "grand_total", "currency", "status"),
		("name", "quotation_to", "party_name", "customer_name", "company", "transaction_date", "valid_till", "order_type", "currency", "net_total",
			"total_taxes_and_charges", "grand_total", "status"),
		"transaction_date", "party_name", "customer_name", "grand_total", search_fields=("name", "party_name", "customer_name"),
		child=TXN_ITEMS, taxes=True, currency_field="currency"),
	"Supplier Quotation": Spec("supplier_name", ("name", "supplier_name", "transaction_date", "valid_till", "grand_total", "currency", "status"),
		("name", "supplier", "supplier_name", "company", "transaction_date", "valid_till", "currency", "net_total", "total_taxes_and_charges",
			"grand_total", "status"),
		"transaction_date", "supplier", "supplier_name", "grand_total", search_fields=("name", "supplier", "supplier_name"),
		child=TXN_ITEMS, taxes=True, currency_field="currency"),
	"Material Request": Spec("title", ("name", "material_request_type", "transaction_date", "schedule_date", "status", "per_ordered", "per_received"),
		("name", "title", "material_request_type", "company", "transaction_date", "schedule_date", "set_warehouse", "set_from_warehouse", "status",
			"per_ordered", "per_received"),
		"transaction_date", search_fields=("name", "title", "material_request_type"),
		child=("items", ("item_code", "item_name", "qty", "uom", "warehouse", "schedule_date"))),
	"Lead": Spec("lead_name", ("name", "lead_name", "company_name", "status", "territory", "qualification_status"),
		("name", "lead_name", "company_name", "job_title", "status", "type", "territory", "industry", "email_id", "mobile_no", "phone", "city",
			"country", "lead_owner", "qualification_status", "company", "customer"),
		search_fields=("name", "lead_name", "company_name", "email_id", "mobile_no"), optional_company=True),
	"Opportunity": Spec("customer_name", ("name", "customer_name", "opportunity_from", "transaction_date", "expected_closing", "opportunity_amount", "currency", "status"),
		("name", "opportunity_from", "party_name", "customer_name", "company", "transaction_date", "expected_closing", "opportunity_type", "sales_stage",
			"probability", "opportunity_amount", "currency", "opportunity_owner", "territory", "status"),
		"transaction_date", "party_name", "customer_name", "opportunity_amount", search_fields=("name", "party_name", "customer_name", "title"),
		child=("items", ("item_code", "item_name", "qty", "uom", "rate", "amount")), currency_field="currency"),
	"Journal Entry": Spec("title", ("name", "title", "voucher_type", "posting_date", "total_debit", "total_credit"),
		("name", "title", "voucher_type", "company", "posting_date", "total_debit", "total_credit", "difference", "is_opening"),
		"posting_date", amount_field="total_debit", status_field=None, search_fields=("name", "title", "voucher_type"),
		extra_children={"accounts": ("account", "party_type", "party", "debit_in_account_currency", "credit_in_account_currency", "account_currency")},
		currency_map={"total_debit": "@company", "total_credit": "@company", "difference": "@company"}),
	# Stock balance per item and warehouse. Rows are filtered by Frappe's own role and User Permission checks (item, warehouse,
	# company); valuation fields are deliberately not exposed, and a Bin has no title or details view.
	"Bin": Spec("item_code", ("name", "item_code", "warehouse", "actual_qty", "reserved_qty", "projected_qty", "stock_uom"),
		("name", "item_code", "warehouse", "actual_qty", "reserved_qty", "projected_qty", "ordered_qty", "indented_qty", "stock_value", "stock_uom"),
		amount_field="actual_qty", status_field=None, search_fields=("item_code", "warehouse"), operations=("list", "count", "aggregate"),
		optional_company=True, currency_map={"stock_value": "@company"}),  # stock_value is what the Stock workspace's "Total Stock Value" card sums; valuation_rate stays out
	"Project": Spec("project_name", ("name", "project_name", "status", "priority", "percent_complete", "expected_end_date", "customer"),
		("name", "project_name", "status", "priority", "percent_complete", "company", "project_type", "customer", "department", "expected_start_date",
			"expected_end_date", "actual_start_date", "actual_end_date", "estimated_costing", "total_sales_amount", "total_billed_amount", "is_active"),
		party_field="customer", search_fields=("name", "project_name", "customer"),
		currency_map={"estimated_costing": "@company", "total_sales_amount": "@company", "total_billed_amount": "@company"}),
	"Task": Spec("subject", ("name", "subject", "project", "status", "priority", "exp_end_date", "progress"),
		("name", "subject", "project", "company", "type", "status", "priority", "parent_task", "exp_start_date", "exp_end_date", "act_start_date",
			"act_end_date", "progress", "expected_time", "actual_time", "department", "is_group"),
		"exp_end_date", search_fields=("name", "subject", "project"), optional_company=True),
	"Issue": Spec("subject", ("name", "subject", "customer", "status", "priority", "issue_type", "opening_date"),
		("name", "subject", "customer", "customer_name", "company", "project", "status", "priority", "issue_type", "opening_date", "resolution_time"),
		"opening_date", "customer", "customer_name", search_fields=("name", "subject", "customer", "customer_name"), optional_company=True),
	# --- Batch 2: read-only coverage across modules. Company DocTypes are limited to the user's companies; "shared" ones are master data. ---
	# Accounting
	"Account": Spec("account_name", ("name", "account_name", "account_number", "root_type", "account_type", "company", "is_group", "disabled"),
		("name", "account_name", "account_number", "root_type", "report_type", "account_type", "account_currency", "parent_account", "company",
			"is_group", "disabled", "freeze_account"), status_field=None, search_fields=("name", "account_name", "account_number")),
	"Cost Center": Spec("cost_center_name", ("name", "cost_center_name", "cost_center_number", "company", "parent_cost_center", "is_group", "disabled"),
		("name", "cost_center_name", "cost_center_number", "company", "parent_cost_center", "is_group", "disabled"), status_field=None,
		search_fields=("name", "cost_center_name", "cost_center_number")),
	"GL Entry": Spec("voucher_no", ("name", "posting_date", "account", "party_type", "party", "debit", "credit", "voucher_type", "voucher_no"),
		("name", "posting_date", "account", "party_type", "party", "debit", "credit", "voucher_type", "voucher_no", "against_voucher_type", "against_voucher",
			"cost_center", "project", "company", "fiscal_year", "is_opening"), "posting_date", amount_field="debit", status_field=None,
		search_fields=("name", "account", "party", "voucher_no"), operations=("list", "count", "aggregate"),
		currency_map={"debit": "@company", "credit": "@company"}, default_filters=(("is_cancelled", "=", 0),)),
	"Payment Request": Spec("party_name", ("name", "payment_request_type", "party_name", "transaction_date", "grand_total", "currency", "status"),
		("name", "payment_request_type", "party_type", "party", "party_name", "company", "transaction_date", "reference_doctype", "reference_name",
			"mode_of_payment", "grand_total", "outstanding_amount", "currency", "status"), "transaction_date", "party", "party_name", "grand_total",
		search_fields=("name", "party", "party_name", "reference_name"), currency_field="currency"),
	"Dunning": Spec("customer_name", ("name", "customer_name", "posting_date", "dunning_type", "dunning_amount", "grand_total", "currency", "status"),
		("name", "customer", "customer_name", "company", "posting_date", "dunning_type", "total_outstanding", "total_interest", "dunning_fee",
			"dunning_amount", "grand_total", "currency", "status"), "posting_date", "customer", "customer_name", "grand_total",
		search_fields=("name", "customer", "customer_name"), currency_field="currency"),
	"Asset": Spec("asset_name", ("name", "asset_name", "item_code", "asset_category", "location", "status", "purchase_date", "total_asset_cost"),
		("name", "asset_name", "item_code", "asset_category", "location", "company", "status", "purchase_date", "available_for_use_date", "disposal_date",
			"purchase_amount", "total_asset_cost", "value_after_depreciation", "calculate_depreciation", "is_fully_depreciated", "custodian", "department",
			"cost_center"), "purchase_date", amount_field="total_asset_cost", search_fields=("name", "asset_name", "item_code"),
		currency_map={"purchase_amount": "@company", "total_asset_cost": "@company", "value_after_depreciation": "@company"}),
	# Stock
	"Stock Ledger Entry": Spec("item_code", ("name", "posting_date", "item_code", "warehouse", "actual_qty", "qty_after_transaction", "voucher_type", "voucher_no"),
		("name", "posting_date", "item_code", "warehouse", "actual_qty", "qty_after_transaction", "voucher_type", "voucher_no", "stock_uom", "company"),
		"posting_date", amount_field="actual_qty", status_field=None, search_fields=("item_code", "warehouse", "voucher_no"),
		operations=("list", "count", "aggregate"), default_filters=(("is_cancelled", "=", 0),)),  # valuation fields are not exposed
	"Stock Reconciliation": Spec("name", ("name", "purpose", "posting_date", "company", "docstatus"),
		("name", "purpose", "posting_date", "company", "set_warehouse", "docstatus"), "posting_date", status_field=None, search_fields=("name", "purpose"),
		child=("items", ("item_code", "item_name", "warehouse", "qty", "current_qty", "quantity_difference"))),
	"Serial No": Spec("serial_no", ("name", "serial_no", "item_code", "warehouse", "status", "company", "warranty_expiry_date"),
		("name", "serial_no", "item_code", "item_name", "warehouse", "batch_no", "status", "company", "customer", "warranty_expiry_date", "amc_expiry_date",
			"maintenance_status"), search_fields=("name", "serial_no", "item_code")),
	"Batch": Spec("batch_id", ("name", "batch_id", "item", "manufacturing_date", "expiry_date", "batch_qty", "disabled"),
		("name", "batch_id", "item", "item_name", "manufacturing_date", "expiry_date", "batch_qty", "stock_uom", "supplier", "disabled"), "expiry_date",
		status_field=None, search_fields=("name", "batch_id", "item"), shared=True),
	"Pick List": Spec("name", ("name", "purpose", "customer_name", "status", "per_delivered", "company"),
		("name", "purpose", "customer", "customer_name", "company", "parent_warehouse", "work_order", "material_request", "status", "delivery_status",
			"per_delivered"), search_fields=("name", "customer_name", "purpose"),
		child=("locations", ("item_code", "item_name", "warehouse", "qty", "picked_qty", "uom", "sales_order"))),
	"Item Group": Spec("item_group_name", ("name", "item_group_name", "parent_item_group", "is_group"), ("name", "item_group_name", "parent_item_group", "is_group"),
		status_field=None, search_fields=("name", "item_group_name"), shared=True),
	"Brand": Spec("brand", ("name", "brand"), ("name", "brand"), status_field=None, search_fields=("name", "brand"), shared=True),
	"Price List": Spec("price_list_name", ("name", "price_list_name", "currency", "enabled", "buying", "selling"),
		("name", "price_list_name", "currency", "enabled", "buying", "selling"), status_field=None, search_fields=("name", "price_list_name"), shared=True),
	"Item Price": Spec("item_name", ("name", "item_code", "item_name", "price_list", "price_list_rate", "currency", "uom"),
		("name", "item_code", "item_name", "price_list", "price_list_rate", "currency", "uom", "customer", "supplier", "valid_from", "valid_upto", "buying", "selling"),
		"valid_from", amount_field="price_list_rate", status_field=None, search_fields=("name", "item_code", "item_name", "price_list"),
		currency_field="currency", shared=True),
	# Selling / CRM / Buying
	"Customer Group": Spec("customer_group_name", ("name", "customer_group_name", "parent_customer_group", "is_group"),
		("name", "customer_group_name", "parent_customer_group", "is_group"), status_field=None, search_fields=("name", "customer_group_name"), shared=True),
	"Territory": Spec("territory_name", ("name", "territory_name", "parent_territory", "is_group"), ("name", "territory_name", "parent_territory", "is_group"),
		status_field=None, search_fields=("name", "territory_name"), shared=True),
	"Supplier Group": Spec("supplier_group_name", ("name", "supplier_group_name", "parent_supplier_group", "is_group"),
		("name", "supplier_group_name", "parent_supplier_group", "is_group"), status_field=None, search_fields=("name", "supplier_group_name"), shared=True),
	"Prospect": Spec("company_name", ("name", "company_name", "customer_group", "territory", "industry", "prospect_owner"),
		("name", "company_name", "customer_group", "territory", "industry", "market_segment", "no_of_employees", "website", "prospect_owner", "company"),
		status_field=None, search_fields=("name", "company_name")),
	"Request for Quotation": Spec("name", ("name", "transaction_date", "schedule_date", "status", "company"),
		("name", "transaction_date", "schedule_date", "status", "company", "opportunity"), "transaction_date", search_fields=("name",),
		child=("items", ("item_code", "item_name", "qty", "uom", "warehouse", "schedule_date"))),
	# Manufacturing / Quality / Subcontracting
	"BOM": Spec("item_name", ("name", "item", "item_name", "quantity", "uom", "is_active", "is_default", "company"),
		("name", "item", "item_name", "quantity", "uom", "company", "is_active", "is_default", "with_operations", "currency", "total_cost", "docstatus"),
		status_field=None, search_fields=("name", "item", "item_name"), child=("items", ("item_code", "item_name", "qty", "uom", "rate", "amount", "source_warehouse")),
		currency_field="currency"),
	"Work Order": Spec("production_item", ("name", "production_item", "item_name", "qty", "produced_qty", "status", "planned_start_date", "company"),
		("name", "production_item", "item_name", "bom_no", "qty", "produced_qty", "material_transferred_for_manufacturing", "status", "sales_order",
			"production_plan", "project", "source_warehouse", "wip_warehouse", "fg_warehouse", "planned_start_date", "planned_end_date",
			"actual_start_date", "actual_end_date", "company"), "planned_start_date", search_fields=("name", "production_item", "item_name", "sales_order"),
		child=("required_items", ("item_code", "item_name", "source_warehouse", "required_qty", "transferred_qty", "consumed_qty"))),
	"Job Card": Spec("operation", ("name", "work_order", "production_item", "operation", "workstation", "for_quantity", "total_completed_qty", "status"),
		("name", "work_order", "production_item", "operation", "workstation", "for_quantity", "total_completed_qty", "status", "posting_date", "expected_start_date",
			"expected_end_date", "actual_start_date", "actual_end_date", "project", "company"), "posting_date",
		search_fields=("name", "work_order", "production_item", "operation")),
	"Production Plan": Spec("name", ("name", "posting_date", "status", "total_planned_qty", "total_produced_qty", "company"),
		("name", "posting_date", "status", "get_items_from", "from_date", "to_date", "total_planned_qty", "total_produced_qty", "company"), "posting_date",
		search_fields=("name",), child=("po_items", ("item_code", "bom_no", "planned_qty", "produced_qty", "pending_qty", "warehouse", "planned_start_date"))),
	"Quality Inspection": Spec("item_name", ("name", "report_date", "inspection_type", "item_code", "item_name", "reference_type", "reference_name", "status"),
		("name", "report_date", "inspection_type", "item_code", "item_name", "reference_type", "reference_name", "batch_no", "sample_size", "manual_inspection",
			"inspected_by", "status", "company"), "report_date", search_fields=("name", "item_code", "item_name", "reference_name"),
		child=("readings", ("specification", "status", "reading_value", "min_value", "max_value"))),
	"Asset Category": Spec("asset_category_name", ("name", "asset_category_name", "enable_cwip_accounting"), ("name", "asset_category_name", "enable_cwip_accounting",
		"non_depreciable_category"), status_field=None, search_fields=("name", "asset_category_name"), shared=True),
	"Subcontracting Order": Spec("supplier_name", ("name", "supplier_name", "transaction_date", "schedule_date", "status", "per_received"),
		("name", "supplier", "supplier_name", "purchase_order", "company", "transaction_date", "schedule_date", "supplier_warehouse", "total_qty", "status", "per_received"),
		"transaction_date", party_field="supplier", party_title_field="supplier_name", search_fields=("name", "supplier", "supplier_name"),
		child=("items", ("item_code", "item_name", "qty", "received_qty", "warehouse", "schedule_date"))),
	"Subcontracting Receipt": Spec("supplier_name", ("name", "supplier_name", "posting_date", "status", "total_qty"),
		("name", "supplier", "supplier_name", "supplier_delivery_note", "company", "posting_date", "set_warehouse", "total_qty", "is_return", "status"),
		"posting_date", party_field="supplier", party_title_field="supplier_name", search_fields=("name", "supplier", "supplier_name"),
		child=("items", ("item_code", "item_name", "qty", "received_qty", "rejected_qty", "warehouse"))),
	# Projects / Support
	"Timesheet": Spec("title", ("name", "title", "employee_name", "start_date", "end_date", "total_hours", "status"),
		("name", "title", "employee_name", "company", "start_date", "end_date", "parent_project", "customer", "total_hours", "total_billable_hours", "per_billed", "status"),
		"start_date", search_fields=("name", "title", "employee_name"),
		child=("time_logs", ("activity_type", "from_time", "to_time", "hours", "project", "task", "is_billable"))),  # rates and costing are not exposed
	"Warranty Claim": Spec("customer_name", ("name", "customer_name", "complaint_date", "item_code", "serial_no", "status"),
		("name", "customer", "customer_name", "company", "complaint_date", "item_code", "serial_no", "warranty_amc_status", "warranty_expiry_date",
			"resolution_date", "territory", "status"), "complaint_date", party_field="customer", party_title_field="customer_name",
		search_fields=("name", "customer", "customer_name", "serial_no")),
}


# Fields that are never exposed, filtered, grouped or created, whatever a Spec lists (defence in depth).
PROHIBITED_FIELDTYPES = frozenset({"Password", "Attach", "Attach Image", "Signature"})
PROHIBITED_NAME_TOKENS = frozenset({"password", "passwd", "secret", "token", "otp", "salary", "iban", "ctc", "aadhaar", "aadhar", "passport",
	"credential", "credentials"})  # whole "_"-separated words of a fieldname
PROHIBITED_NAME_PARTS = ("api_key", "apikey", "private_key", "bank_account", "secret_key")

# Set by Frappe or by workflow/lifecycle: never taken from the chat (model- or user-supplied) on creation.
PROTECTED_CREATE_FIELDS = frozenset({"docstatus", "owner", "creation", "modified", "modified_by", "name", "parent", "parenttype", "parentfield",
	"idx", "amended_from", "workflow_state", "doctype", "__islocal", "__unsaved", "_user_tags", "_comments", "_assign", "_liked_by"})

# Lifecycle and raw-access verbs a query plan may never name as its operation.
PROHIBITED_QUERY_OPERATIONS = frozenset({"submit", "cancel", "amend", "delete", "remove", "pay", "update", "edit", "write", "insert", "create",
	"post", "approve", "reject", "reopen", "close", "set", "sql", "raw", "run", "execute"})


# Standard reports the assistant may *describe* when the user has one open. Only the report name and its validated filters are
# used: the report is never executed and its result rows are never read (REPORT_RESULTS_ENABLED stays False until a report's
# own query is proven to apply the user's role, User Permission and company restrictions).
# ref_doctype: the DocType the report is built on (the user must be able to read it). filters: the filter names we understand
# and their type ("company", "date" or "bool"); any other filter is reported as not understood, never guessed.
REPORT_RESULTS_ENABLED = False
REPORTS = {
	"Accounts Receivable": {"ref_doctype": "Sales Invoice", "filters": {"company": "company", "report_date": "date", "ageing_based_on": None},
		"about": "lists unpaid Sales Invoices per customer, split into ageing buckets by how long they are overdue."},
	"Accounts Payable": {"ref_doctype": "Purchase Invoice", "filters": {"company": "company", "report_date": "date", "ageing_based_on": None},
		"about": "lists unpaid Purchase Invoices per supplier, split into ageing buckets by how long they are overdue."},
	"General Ledger": {"ref_doctype": "GL Entry", "filters": {"company": "company", "from_date": "date", "to_date": "date", "group_by": None},
		"about": "lists the ledger entries (debits and credits) of accounts, parties or vouchers for a period."},
	"Trial Balance": {"ref_doctype": "GL Entry", "filters": {"company": "company", "from_date": "date", "to_date": "date", "fiscal_year": None},
		"about": "shows opening balance, debit, credit and closing balance of every account for a period."},
	"Balance Sheet": {"ref_doctype": "GL Entry", "filters": {"company": "company", "period_start_date": "date", "period_end_date": "date", "filter_based_on": None},
		"about": "shows assets, liabilities and equity as at the end of each period."},
	"Profit and Loss Statement": {"ref_doctype": "GL Entry", "filters": {"company": "company", "period_start_date": "date", "period_end_date": "date",
		"filter_based_on": None}, "about": "shows income and expenses, and the profit or loss, for each period."},
	"Stock Balance": {"ref_doctype": "Stock Ledger Entry", "filters": {"company": "company", "from_date": "date", "to_date": "date"},
		"about": "shows the quantity (and value) of each item per warehouse at the end of a period, with the movements within it."},
	"Stock Ledger": {"ref_doctype": "Stock Ledger Entry", "filters": {"company": "company", "from_date": "date", "to_date": "date"},
		"about": "lists every stock movement (receipts, issues, transfers) with the running quantity after each."},
	"Sales Register": {"ref_doctype": "Sales Invoice", "filters": {"company": "company", "from_date": "date", "to_date": "date"},
		"about": "lists submitted Sales Invoices for a period with their net totals and taxes."},
	"Purchase Register": {"ref_doctype": "Purchase Invoice", "filters": {"company": "company", "from_date": "date", "to_date": "date"},
		"about": "lists submitted Purchase Invoices for a period with their net totals and taxes."},
	"Gross Profit": {"ref_doctype": "Sales Invoice", "filters": {"company": "company", "from_date": "date", "to_date": "date"},
		"about": "compares what items were sold for with what they cost, per invoice, item or customer."},
}
