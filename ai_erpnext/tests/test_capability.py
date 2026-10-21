"""The shared capability layer (capability/): metadata describes, policy allows, service intersects.

Run: bench --site <site> run-tests --module ai_erpnext.tests.test_capability
"""

import frappe
from frappe.tests import IntegrationTestCase

from ai_erpnext.capability import metadata as M
from ai_erpnext.capability import policy as P
from ai_erpnext.capability import service as C
from ai_erpnext.chat import forms, queries

NEW_QUERY_ONLY = (
	"Quotation",
	"Supplier Quotation",
	"Material Request",
	"Lead",
	"Opportunity",
	"Journal Entry",
	"Bin",
	"Project",
	"Task",
	"Issue",
)


def info(fieldtype, fieldname="x", **kw):
	return M.FieldInfo(fieldname, fieldtype, fieldname, kw.get("options"), False, False, False, 0)


BATCH_2 = (
	"Account",
	"Cost Center",
	"GL Entry",
	"Payment Request",
	"Dunning",
	"Asset",
	"Stock Ledger Entry",
	"Stock Reconciliation",
	"Serial No",
	"Batch",
	"Pick List",
	"Item Group",
	"Brand",
	"Price List",
	"Item Price",
	"Customer Group",
	"Territory",
	"Supplier Group",
	"Prospect",
	"Request for Quotation",
	"BOM",
	"Work Order",
	"Job Card",
	"Production Plan",
	"Quality Inspection",
	"Asset Category",
	"Subcontracting Order",
	"Subcontracting Receipt",
	"Timesheet",
	"Warranty Claim",
)


class TestCapabilityPolicy(IntegrationTestCase):
	def setUp(self):
		frappe.set_user("Administrator")

	# ---- compatibility ---------------------------------------------------------------------
	def test_queries_registry_is_the_shared_policy(self):
		self.assertIs(queries.REGISTRY, P.REGISTRY)
		self.assertIs(queries.Spec, P.Spec)
		self.assertIs(queries.QueryError, C.CapabilityError)
		for dt in (
			"Sales Invoice",
			"Purchase Invoice",
			"Sales Order",
			"Purchase Order",
			"Delivery Note",
			"Purchase Receipt",
			"Stock Entry",
			"Payment Entry",
			"Customer",
			"Supplier",
			"Contact",
			"Item",
			"Warehouse",
			"Employee",
		):
			self.assertIn(dt, P.REGISTRY)  # nothing that was queryable before was dropped

	def test_existing_operations_are_unchanged(self):
		spec = P.REGISTRY["Sales Invoice"]
		for op in (
			"list",
			"count",
			"details",
			"sales_summary",
			"overdue",
			"unpaid",
			"outstanding_by_customer",
		):
			self.assertIn(op, spec.operations)
		self.assertEqual(
			P.REGISTRY["Purchase Invoice"].list_fields[:3], ("name", "supplier_name", "posting_date")
		)

	# ---- policy matches the installed metadata ---------------------------------------------
	def test_every_listed_field_exists_in_the_installed_metadata(self):
		for dt, spec in P.REGISTRY.items():
			self.assertTrue(M.doctype_exists(dt), dt)
			for f in set(spec.list_fields) | set(spec.detail_fields):
				self.assertIsNotNone(
					M.field_info(dt, f), f"{dt}.{f} is in the policy but not in the metadata"
				)
			for f in (spec.status_field, spec.date_field, spec.party_field, spec.amount_field, spec.title):
				if f:
					self.assertIsNotNone(
						M.field_info(dt, f),
						f"{dt}.{f} (status/date/party/amount/title) is not in the metadata",
					)
			for f, source in spec.currency_map.items():
				self.assertIn(f, set(spec.list_fields) | set(spec.detail_fields), f"{dt}.{f}")
				self.assertTrue(source == "@company" or M.field_info(dt, source), f"{dt}.{f} -> {source}")
			declared = {**({spec.child[0]: spec.child[1]} if spec.child else {}), **spec.extra_children}
			for table, fields in declared.items():
				child = M.child_doctype(dt, table)
				self.assertIsNotNone(child, f"{dt}.{table} is not a child table")
				for f in fields:
					self.assertIsNotNone(
						M.field_info(child, f), f"{child}.{f} is declared but not in the metadata"
					)
			for table, fields in C.child_tables(dt).items():
				child = M.child_doctype(dt, table)
				self.assertIsNotNone(child, f"{dt}.{table}")
				self.assertTrue(fields, f"{dt}.{table} has no readable fields")
				for f in fields:
					self.assertIsNotNone(M.field_info(child, f), f"{child}.{f}")

	def test_no_listed_field_is_a_secret(self):
		for dt in P.REGISTRY:
			for f in C.allowed_fields(dt, permitted_only=False):
				self.assertFalse(C.is_prohibited_field(M.field_info(dt, f)), f"{dt}.{f}")

	def test_prohibited_fields(self):
		for t in ("Password", "Attach", "Attach Image", "Signature"):
			self.assertTrue(C.is_prohibited_field(info(t)))
		for name in (
			"api_key",
			"secret_key",
			"access_token",
			"otp",
			"bank_account_no",
			"salary_structure",
			"passport_number",
			"private_key",
		):
			self.assertTrue(C.is_prohibited_field(info("Data", name)), name)
		for name in ("footprint", "contact_person", "grand_total", "customer_name", "operation_type"):
			self.assertFalse(C.is_prohibited_field(info("Data", name)), name)
		self.assertTrue(C.is_prohibited_field(None))

	# ---- metadata alone grants nothing ------------------------------------------------------
	def test_unlisted_doctypes_are_not_queryable(self):
		for dt in (
			"User",
			"Salary Slip",
			"DocType",
			"AI Provider Settings",
			"AI Document Entry",
			"Company",
			"Error Log",
			"Bank Account",
			"Bank Transaction",
			"Role",
			"Communication",
			"File",
			"Payment Gateway Account",
			"Currency",
			"Fiscal Year",
			"Sales Taxes and Charges Template",
		):
			self.assertFalse(C.is_queryable(dt), dt)
			with self.assertRaises(C.CapabilityError):
				C.query_spec(dt)
			with self.assertRaises(queries.QueryError):
				queries.validate({"doctype": dt, "operation": "list"})
		self.assertFalse(C.is_queryable(None) or C.is_queryable(["Customer"]) or C.is_queryable({"x": 1}))

	def test_metadata_fields_outside_the_policy_are_rejected(self):
		self.assertIsNotNone(M.field_info("Sales Invoice", "debit_to"))
		self.assertNotIn("debit_to", C.allowed_fields("Sales Invoice"))
		with self.assertRaises(C.CapabilityError):
			C.normalize_where("Sales Invoice", [{"field": "debit_to", "op": "=", "value": "x"}])
		with self.assertRaises(C.CapabilityError):
			C.normalize_where("Customer", [{"field": "image", "op": "=", "value": "x"}])
		with self.assertRaises(C.CapabilityError):
			C.normalize_where("Sales Invoice", [{"field": "owner", "op": "=", "value": "x"}])
		with self.assertRaises(C.CapabilityError):
			C.normalize_where(
				"Sales Invoice", [{"field": "docstatus", "op": "=", "value": 1}]
			)  # use the dedicated docstatus filter

	def test_query_capability_is_separate_from_creation(self):
		for dt in NEW_QUERY_ONLY:
			self.assertTrue(C.is_queryable(dt), dt)
			# Lead and Issue became creatable in batch 2; Material Request (a Draft) in the 2026-10-21 phase
			if dt not in ("Quotation", "Lead", "Issue", "Material Request"):
				self.assertFalse(C.is_creatable(dt), f"{dt} must stay query-only")
		self.assertTrue(
			C.is_creatable("Quotation") and C.is_creatable("Sales Invoice") and C.is_creatable("Item")
		)
		self.assertTrue(
			C.is_creatable("Material Request") and C.is_creatable("Stock Entry")
		)  # Draft-only stock documents
		self.assertEqual(set(C.creatable_doctypes()), set(forms.CREATABLE))
		for dt in (
			"Payment Entry",
			"Journal Entry",
			"Contact",
			"Warehouse",
			"Employee",
		):  # payments excluded, journals deferred
			self.assertTrue(C.is_queryable(dt) and not C.is_creatable(dt), dt)
		with self.assertRaises(C.CapabilityError):
			C.creation_fields("Bin")

	def test_new_doctypes_have_no_edit_or_lifecycle_operations(self):
		for dt in NEW_QUERY_ONLY:
			self.assertFalse(set(P.REGISTRY[dt].operations) & P.PROHIBITED_QUERY_OPERATIONS, dt)

	def test_bin_does_not_expose_valuation_or_details(self):
		fields = set(C.allowed_fields("Bin", permitted_only=False))
		self.assertNotIn(
			"valuation_rate", fields
		)  # per-unit valuation stays out; stock_value is the total the Stock workspace shows
		self.assertIn("stock_value", fields)
		self.assertEqual(
			C.currency_source("Bin", "stock_value"), ("company", None)
		)  # a Float that is money: grouped by company, never mixed
		self.assertNotIn("details", P.REGISTRY["Bin"].operations)

	# ---- fail closed: operations --------------------------------------------------------------
	def test_lifecycle_and_raw_operations_fail_closed(self):
		for op in (
			"submit",
			"cancel",
			"amend",
			"delete",
			"pay",
			"update",
			"edit",
			"insert",
			"sql",
			"raw",
			"execute",
			"approve",
		):
			with self.assertRaises(queries.QueryError, msg=op) as ctx:
				queries.validate({"doctype": "Sales Invoice", "operation": op})
			self.assertIn("only read", str(ctx.exception))
			with self.assertRaises(C.CapabilityError):
				C.check_operation("Sales Invoice", op)
		with self.assertRaises(C.CapabilityError):
			C.check_operation("Customer", "outstanding_by_customer")  # an operation of another DocType
		with self.assertRaises(C.CapabilityError):
			C.check_operation("Bin", "details")

	# ---- operators and values --------------------------------------------------------------
	def test_operators_depend_on_the_field_type(self):
		self.assertIn(">", C.field_operators("Sales Invoice", "grand_total"))
		self.assertNotIn("like", C.field_operators("Sales Invoice", "grand_total"))
		self.assertIn("like", C.field_operators("Sales Invoice", "customer"))
		self.assertNotIn(">", C.field_operators("Sales Invoice", "customer"))
		self.assertIn("between", C.field_operators("Sales Invoice", "posting_date"))
		self.assertEqual(C.field_operators("Item", "disabled"), ("=", "!="))
		self.assertEqual(C.field_operators("Sales Invoice", "docstatus"), ())
		self.assertEqual(C.field_operators("Sales Invoice", "no_such_field"), ())

	def test_where_validation(self):
		ok = C.normalize_where(
			"Sales Invoice",
			[
				{"field": "grand_total", "op": "gt", "value": "1,000"},
				{"field": "posting_date", "op": "between", "value": ["2026-03-31", "2026-03-01"]},
				{"field": "status", "op": "in", "value": ["paid", "Unpaid"]},
				{"field": "customer_name", "op": "contains", "value": "Am%it"},
			],
		)
		self.assertEqual(ok[0], ["grand_total", ">", 1000.0])
		self.assertEqual(ok[1], ["posting_date", "between", ["2026-03-01", "2026-03-31"]])  # ordered
		self.assertEqual(ok[2], ["status", "in", ["Paid", "Unpaid"]])  # canonical option spelling
		self.assertEqual(ok[3], ["customer_name", "like", "Amit"])  # wildcard characters removed
		self.assertEqual(
			C.normalize_where("Sales Invoice", ok),
			ok,
			"normalisation must be idempotent (plans are re-validated)",
		)
		self.assertEqual(C.normalize_where("Sales Invoice", None), [])

	def test_invalid_conditions_are_rejected_never_dropped(self):
		bad = [
			[{"field": "grand_total", "op": "like", "value": "5"}],  # operator not valid for a number
			[{"field": "grand_total", "op": "=", "value": "lots"}],
			[{"field": "grand_total", "op": "; DROP TABLE x", "value": 1}],
			[{"field": "status", "op": "=", "value": "Nonexistent"}],
			[{"field": "posting_date", "op": "=", "value": "31/13/2026"}],
			[{"field": "customer", "op": "=", "value": {"$ne": 1}}],
			[{"field": "customer", "op": "=", "value": None}],
			[{"field": "customer", "op": "in", "value": []}],
			[{"field": "customer", "op": "in", "value": [str(i) for i in range(C.MAX_IN_VALUES + 1)]}],
			[{"field": "posting_date", "op": "between", "value": ["2026-01-01"]}],
			[{"field": "grand_total; select 1", "op": "=", "value": 1}],
			[{"op": "=", "value": 1}],
			["grand_total > 5"],
			"grand_total > 5",
			[{"field": "customer", "op": "=", "value": "x"}] * (C.MAX_WHERE + 1),
		]
		for where in bad:
			with self.assertRaises(C.CapabilityError, msg=str(where)[:80]):
				C.normalize_where("Sales Invoice", where)
		# a bad condition next to a good one rejects the whole plan instead of widening it
		with self.assertRaises(C.CapabilityError):
			C.normalize_where(
				"Sales Invoice",
				[{"field": "customer", "op": "=", "value": "x"}, {"field": "nope", "op": "=", "value": 1}],
			)

	def test_aggregate_validation(self):
		self.assertEqual(
			C.normalize_aggregate(
				"Sales Invoice", {"fn": "total", "field": "grand_total", "group_by": "customer"}
			),
			{"fn": "sum", "field": "grand_total", "group_by": "customer", "period": None},
		)
		month = C.normalize_aggregate("Sales Invoice", {"fn": "count", "group_by": "month"})
		self.assertEqual((month["group_by"], month["period"]), (None, "month"))
		both = C.normalize_aggregate(
			"Sales Invoice", {"fn": "sum", "field": "grand_total", "group_by": "customer", "period": "month"}
		)
		self.assertEqual((both["group_by"], both["period"]), ("customer", "month"))
		with self.assertRaises(C.CapabilityError):
			C.normalize_aggregate("Sales Invoice", {"fn": "count", "period": "week"})
		with self.assertRaises(C.CapabilityError):
			C.normalize_aggregate("Sales Invoice", {"fn": "count", "group_by": "month", "period": "year"})
		self.assertEqual(
			C.normalize_aggregate("Sales Invoice", {"fn": "max", "field": "posting_date"})["field"],
			"posting_date",
		)
		for agg in (
			{"fn": "sum", "field": "customer"},  # not numeric
			{"fn": "sum", "field": "debit_to"},
			{"fn": "sum"},
			{"fn": "median", "field": "grand_total"},
			{"fn": "sum", "field": "grand_total", "group_by": "customer_name"},  # free text: no grouping
			{"fn": "sum", "field": "grand_total", "group_by": "owner"},
			{"fn": "avg", "field": "posting_date"},
			"sum(grand_total)",
		):
			with self.assertRaises(C.CapabilityError, msg=str(agg)):
				C.normalize_aggregate("Sales Invoice", agg)
		with self.assertRaises(C.CapabilityError):
			C.normalize_aggregate(
				"Bin", {"fn": "sum", "field": "actual_qty", "group_by": "month"}
			)  # Bin has no date to bucket by
		with self.assertRaises(C.CapabilityError):
			C.normalize_aggregate("Customer", {"fn": "sum", "field": "customer_name"})

	def test_currency_amounts_always_have_a_currency_source(self):
		self.assertEqual(C.currency_source("Sales Invoice", "grand_total"), ("field", "currency"))
		self.assertEqual(
			C.currency_source("Payment Entry", "received_amount"), ("field", "paid_to_account_currency")
		)
		self.assertEqual(C.currency_source("Journal Entry", "total_debit"), ("company", None))
		self.assertEqual(C.currency_source("Stock Entry", "total_outgoing_value"), ("company", None))
		self.assertIsNone(C.currency_source("Bin", "actual_qty"))
		for dt in P.REGISTRY:
			for f in C.measure_fields(dt):
				if M.field_info(dt, f).fieldtype == "Currency":
					self.assertNotEqual(
						C.currency_source(dt, f)[0], "unknown", f"{dt}.{f} would be summed without a currency"
					)

	# ---- child tables and links --------------------------------------------------------------
	def test_child_tables_are_allowlisted_per_parent(self):
		self.assertEqual(set(C.child_tables("Sales Invoice")), {"items", "taxes"})
		self.assertEqual(C.child_tables("Customer"), {})
		self.assertIn("accounts", C.child_tables("Journal Entry"))
		self.assertNotIn(
			"payment_schedule", C.child_tables("Sales Invoice")
		)  # exists in the metadata, not in the policy
		with self.assertRaises(C.CapabilityError):
			C.child_fields("Sales Invoice", "payment_schedule")
		with self.assertRaises(C.CapabilityError):
			C.child_fields("Customer", "items")
		child = C.normalize_child(
			"Sales Invoice",
			{
				"table": "items",
				"where": [{"field": "qty", "op": ">", "value": 5}],
				"fields": ["item_code", "qty"],
			},
		)
		self.assertEqual(
			child, {"table": "items", "where": [["qty", ">", 5.0]], "fields": ["item_code", "qty"]}
		)
		self.assertEqual(C.normalize_child("Sales Invoice", child), child)
		for bad in (
			{"table": "items", "fields": ["warehouse_x"]},
			{"table": "items", "fields": ["cost_center"]},
			{"table": "items", "where": [{"field": "cost_center", "op": "=", "value": "x"}]},
			{"table": "packed_items"},
			{"table": None},
			"items",
		):
			with self.assertRaises(C.CapabilityError, msg=str(bad)):
				C.normalize_child("Sales Invoice", bad)

	def test_links_are_one_hop_to_queryable_targets(self):
		links = C.link_fields("Sales Invoice")
		self.assertEqual(links.get("customer"), "Customer")
		self.assertNotIn("company", links)  # Company is not queryable
		self.assertNotIn("currency", links)
		link = C.normalize_link("Sales Invoice", {"field": "customer", "fields": ["customer_group"]})
		self.assertEqual(
			(link["field"], link["target"], link["fields"]), ("customer", "Customer", ["customer_group"])
		)
		for bad in (
			{"field": "company"},
			{"field": "debit_to"},
			{"field": "customer", "fields": ["mobile_no"]},  # not a list-level field
			{"field": "customer", "fields": ["tax_id"]},
			{"field": "customer.customer_group"},
			{},
			"customer",
		):
			with self.assertRaises(C.CapabilityError, msg=str(bad)):
				C.normalize_link("Sales Invoice", bad)

	def test_company_modes(self):
		self.assertEqual(C.company_mode("Sales Invoice"), "required")
		self.assertEqual(C.company_mode("Customer"), "shared")
		self.assertEqual(C.company_mode("Contact"), "shared")
		self.assertEqual(C.company_mode("User"), "none")  # not listed in the policy
		for dt in ("Lead", "Task", "Issue", "Bin"):
			self.assertEqual(C.company_mode(dt), "optional", dt)

	# ---- creation guard ----------------------------------------------------------------------
	def test_creation_guard_rejects_lifecycle_and_unknown_fields(self):
		ok = {
			"doctype": "Sales Order",
			"customer": "C",
			"company": "X",
			"transaction_date": "2026-01-01",
			"delivery_date": "2026-02-01",
			"items": [
				{"item_code": "I", "qty": 1, "rate": 2, "warehouse": "W", "delivery_date": "2026-02-01"}
			],
		}
		self.assertTrue(C.assert_creation_safe("Sales Order", ok))
		for key, value in (
			("docstatus", 1),
			("owner", "x@example.com"),
			("workflow_state", "Approved"),
			("amended_from", "SO-1"),
			("creation", "2020-01-01"),
			("name", "SO-FAKE"),
			("parent", "x"),
			("grand_total", 5),
			("naming_series", "X-"),
		):
			with self.assertRaises(C.CapabilityError, msg=key):
				C.assert_creation_safe("Sales Order", {**ok, key: value})
		with self.assertRaises(C.CapabilityError):
			C.assert_creation_safe(
				"Sales Order", {**ok, "items": [{"item_code": "I", "qty": 1, "docstatus": 1}]}
			)
		with self.assertRaises(C.CapabilityError):
			C.assert_creation_safe("Lead", {"doctype": "Lead", "lead_name": "x"})  # queryable, not creatable
		with self.assertRaises(C.CapabilityError):
			C.assert_creation_safe("Sales Order", {**ok, "doctype": "Sales Invoice"})
		# fields ERPNext made mandatory and the form asked for are allowed, but only by name and never protected ones
		self.assertTrue(C.assert_creation_safe("Sales Order", {**ok, "po_no": "P1"}, extra=["po_no"]))
		with self.assertRaises(C.CapabilityError):
			C.assert_creation_safe("Sales Order", {**ok, "docstatus": 1}, extra=["docstatus"])
		with self.assertRaises(C.CapabilityError):
			C.reject_protected({"doctype": "Customer", "owner": "x"})

	def test_creation_fields_match_the_guided_forms(self):
		self.assertEqual(
			set(C.creation_fields("Sales Order")),
			{"customer", "company", "transaction_date", "delivery_date", "set_warehouse"},
		)
		self.assertIn("quotation_to", C.creation_fields("Quotation"))  # fixed values are part of the spec
		self.assertNotIn("docstatus", C.creation_fields("Sales Invoice"))

	def test_every_queryable_doctype_has_a_deliberate_company_policy(self):
		for dt, spec in P.REGISTRY.items():
			mode = C.company_mode(dt)
			self.assertIn(mode, ("required", "optional", "shared"), dt)
			if not M.has_company_field(dt):
				self.assertTrue(
					spec.shared, f"{dt} has no company field and is not reviewed as shared master data"
				)
			else:
				self.assertFalse(spec.shared, f"{dt} has a company field, so company scoping must apply")
			self.assertTrue(C.is_queryable(dt), dt)

	def test_a_doctype_without_company_field_is_not_queryable_unless_marked_shared(self):
		original = P.REGISTRY["Batch"]
		try:
			P.REGISTRY["Batch"] = P.Spec(**{**original.__dict__, "shared": False})
			self.assertFalse(C.is_queryable("Batch"))
			with self.assertRaises(C.CapabilityError):
				queries.validate({"doctype": "Batch", "operation": "list"})
		finally:
			P.REGISTRY["Batch"] = original
		self.assertTrue(C.is_queryable("Batch"))

	def test_batch_2_doctypes_are_read_only_and_not_creatable(self):
		for dt in BATCH_2:
			self.assertIn(dt, P.REGISTRY, dt)
			self.assertFalse(C.is_creatable(dt), dt)
			self.assertFalse(set(P.REGISTRY[dt].operations) & P.PROHIBITED_QUERY_OPERATIONS, dt)

	def test_ledger_style_doctypes_exclude_cancelled_rows_and_valuation(self):
		for dt in ("GL Entry", "Stock Ledger Entry"):
			self.assertEqual(P.REGISTRY[dt].default_filters, (("is_cancelled", "=", 0),))
		sle = set(C.allowed_fields("Stock Ledger Entry", permitted_only=False))
		self.assertFalse(
			{"valuation_rate", "stock_value", "incoming_rate", "outgoing_rate", "stock_value_difference"}
			& sle
		)
		self.assertFalse({"purchase_rate"} & set(C.allowed_fields("Serial No", permitted_only=False)))
		self.assertFalse(
			{"billing_rate", "costing_rate", "billing_amount"} & set(C.child_tables("Timesheet")["time_logs"])
		)
		self.assertFalse({"policy_number", "insurer"} & set(C.allowed_fields("Asset", permitted_only=False)))

	def test_lead_and_issue_are_creatable_and_identified_as_saved_immediately(self):
		from ai_erpnext.chat import forms

		for dt in ("Lead", "Issue"):
			self.assertTrue(C.is_creatable(dt), dt)
			self.assertIn(dt, forms.MASTERS)
		self.assertEqual(
			set(C.creation_fields("Lead")), {"first_name", "company_name", "email_id", "mobile_no"}
		)
		self.assertEqual(set(C.creation_fields("Issue")), {"subject", "customer", "description"})
