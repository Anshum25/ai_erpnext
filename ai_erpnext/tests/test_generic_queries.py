"""Generic structured queries: filters, pagination, aggregates (currency-aware), child-table rows,
one-hop linked records and the query-only DocTypes, under company isolation and user permissions.

Two test companies with their own invoices and other records; users whose User Permissions allow
company A, B, both, or neither. Everything is created inside the test transaction and rolled back.

Run: bench --site <site> run-tests --module ai_erpnext.tests.test_generic_queries
"""

from unittest import mock

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_days, nowdate

from ai_erpnext.chat import queries

COMPANIES = {"A": "AIQ Alpha Co", "B": "AIQ Beta Co", "C": "AIQ Gamma Co"}
ROLES = ["Accounts User", "Sales User", "Stock User", "Projects User", "Support Team", "Purchase User"]
USERS = {"A": ["A"], "B": ["B"], "AB": ["A", "B"], "NONE": ["C"]}


def run(plan):
	return queries.execute(queries.validate(plan))


def rows(result):
	return (result.get("card") or {}).get("rows", [])


class TestGenericQueries(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		currency = cls.currency = (
			frappe.db.get_value(
				"Company",
				frappe.db.get_single_value("Global Defaults", "default_company"),
				"default_currency",
			)
			or "INR"
		)
		country = frappe.db.get_single_value("System Settings", "country") or "India"
		for key, name in COMPANIES.items():
			if not frappe.db.exists("Company", name):
				frappe.get_doc(
					{
						"doctype": "Company",
						"company_name": name,
						"abbr": f"AIQ{key}",
						"default_currency": currency,
						"country": country,
						"create_chart_of_accounts_based_on": "Standard Template",
						"chart_of_accounts": "Standard",
					}
				).insert()
		group = frappe.db.get_value("Customer Group", {"is_group": 0}, "name")
		cls.group = group
		territory = frappe.db.get_value("Territory", {"is_group": 0}, "name")
		cls.customer = (
			frappe.get_doc(
				{
					"doctype": "Customer",
					"customer_name": "AIQ Shared Buyer",
					"customer_group": group,
					"territory": territory,
				}
			)
			.insert()
			.name
		)
		ig = frappe.db.get_value("Item Group", {"is_group": 0}, "name")
		for code, stock in (("AIQ-ITEM", 0), ("AIQ-OTHER", 0), ("AIQ-STOCK", 1)):
			frappe.get_doc(
				{
					"doctype": "Item",
					"item_code": code,
					"item_name": code.title(),
					"item_group": ig,
					"stock_uom": "Nos",
					"is_stock_item": stock,
					"is_sales_item": 1,
					"is_purchase_item": 1,
				}
			).insert()
		cls.invoices = {"A": [], "B": []}
		for key, specs in (
			("A", [(100, "AIQ-ITEM", 3), (250, "AIQ-OTHER", 1)]),
			("B", [(70, "AIQ-ITEM", 9)]),
		):
			for rate, item, qty in specs:
				cls.invoices[key].append(cls.make_si(key, rate, item, qty))
		# one invoice in another currency (set directly: only the grouping of amounts is under test)
		cls.usd = cls.make_si("A", 40, "AIQ-ITEM", 1)
		frappe.db.set_value("Sales Invoice", cls.usd, "currency", "USD", update_modified=False)
		cls.make_records()
		cls.users = {}
		for label, allowed in USERS.items():
			cls.users[label] = cls.make_user(
				f"aiq-{label.lower()}@example.com", ROLES, [COMPANIES[k] for k in allowed]
			)
		cls.users["NOROLES"] = cls.make_user("aiq-noroles@example.com", ["Accounts User"], [])
		frappe.cache.delete_value("user_permissions")

	@classmethod
	def make_user(cls, email, roles, companies):
		if not frappe.db.exists("User", email):
			frappe.get_doc(
				{
					"doctype": "User",
					"email": email,
					"first_name": email.split("@")[0],
					"send_welcome_email": 0,
					"roles": [{"role": r} for r in roles],
				}
			).insert(ignore_permissions=True)
		for company in companies:
			frappe.get_doc(
				{
					"doctype": "User Permission",
					"user": email,
					"allow": "Company",
					"for_value": company,
					"apply_to_all_doctypes": 1,
				}
			).insert(ignore_permissions=True)
		return email

	@classmethod
	def make_si(cls, key, rate, item, qty):
		posting = add_days(nowdate(), -5)
		return (
			frappe.get_doc(
				{
					"doctype": "Sales Invoice",
					"customer": cls.customer,
					"company": COMPANIES[key],
					"posting_date": posting,
					"set_posting_time": 1,
					"due_date": posting,
					"items": [{"item_code": item, "qty": qty, "rate": rate}],
				}
			)
			.insert()
			.name
		)

	@classmethod
	def make_records(cls):
		cls.records = {}
		for key in ("A", "B"):
			cls.records[("Lead", key)] = (
				frappe.get_doc(
					{"doctype": "Lead", "first_name": f"AIQ Lead {key}", "company": COMPANIES[key]}
				)
				.insert()
				.name
			)
			cls.records[("Project", key)] = (
				frappe.get_doc(
					{"doctype": "Project", "project_name": f"AIQ Project {key}", "company": COMPANIES[key]}
				)
				.insert()
				.name
			)
			cls.records[("Issue", key)] = (
				frappe.get_doc({"doctype": "Issue", "subject": f"AIQ Issue {key}", "company": COMPANIES[key]})
				.insert()
				.name
			)
			cls.records[("Task", key)] = (
				frappe.get_doc({"doctype": "Task", "subject": f"AIQ Task {key}", "company": COMPANIES[key]})
				.insert()
				.name
			)
			warehouse = frappe.db.get_value("Warehouse", {"company": COMPANIES[key], "is_group": 0}, "name")
			cls.records[("Warehouse", key)] = warehouse
			cls.records[("Bin", key)] = (
				frappe.get_doc(
					{
						"doctype": "Bin",
						"item_code": "AIQ-STOCK",
						"warehouse": warehouse,
						"actual_qty": 5 if key == "A" else 8,
					}
				)
				.insert()
				.name
			)
		# a Task without a company is not tied to any company
		cls.records[("Task", "none")] = (
			frappe.get_doc({"doctype": "Task", "subject": "AIQ Task NoCompany"}).insert().name
		)
		frappe.db.set_value(
			"Task", cls.records[("Task", "none")], "company", None
		)  # ERPNext defaults it on insert
		cls.records[("Quotation", "A")] = (
			frappe.get_doc(
				{
					"doctype": "Quotation",
					"quotation_to": "Customer",
					"party_name": cls.customer,
					"company": COMPANIES["A"],
					"transaction_date": nowdate(),
					"items": [{"item_code": "AIQ-ITEM", "qty": 2, "rate": 10}],
				}
			)
			.insert()
			.name
		)

	def tearDown(self):
		frappe.set_user("Administrator")

	def mine(self, names, key=None):
		ours = {n for v in self.invoices.values() for n in v} | {self.usd}
		return {n for n in names if n in ours}

	def plan(self, **extra):
		return {
			"doctype": "Sales Invoice",
			"operation": "list",
			"filters": {"party": "AIQ Shared"},
			"limit": 20,
			**extra,
		}

	# ---- where filters --------------------------------------------------------------------------
	def test_filters_narrow_the_results(self):
		frappe.set_user(self.users["AB"])
		# grand totals: A 300 and 250, B 630, the USD invoice 40
		big = run(self.plan(where=[{"field": "grand_total", "op": ">", "value": 260}]))
		self.assertEqual(self.mine(big["names"]), {self.invoices["A"][0], self.invoices["B"][0]})
		small = run(self.plan(where=[{"field": "grand_total", "op": "<=", "value": 250}]))
		self.assertEqual(self.mine(small["names"]), {self.invoices["A"][1], self.usd})
		between = run(self.plan(where=[{"field": "grand_total", "op": "between", "value": [400, 100]}]))
		self.assertEqual(self.mine(between["names"]), {self.invoices["A"][0], self.invoices["A"][1]})
		both = run(
			self.plan(
				where=[
					{"field": "grand_total", "op": ">", "value": 60},
					{"field": "company", "op": "=", "value": COMPANIES["B"]},
				]
			)
		)
		self.assertEqual(self.mine(both["names"]), {self.invoices["B"][0]})
		found = run(self.plan(where=[{"field": "customer", "op": "in", "value": [self.customer, "Nobody"]}]))
		self.assertEqual(len(self.mine(found["names"])), 4)
		self.assertIn(
			"grand total",
			found.get("card", {}).get("note", "")
			+ run(self.plan(where=[{"field": "grand_total", "op": ">", "value": 1}]))["card"]["note"],
		)

	def test_invalid_filters_never_widen_the_query(self):
		frappe.set_user(self.users["AB"])
		for where in (
			[{"field": "debit_to", "op": "=", "value": "x"}],
			[{"field": "grand_total", "op": "like", "value": "1"}],
			[{"field": "grand_total", "op": "=", "value": "abc"}],
			[{"field": "grand_total", "op": ">", "value": 1}, {"field": "bogus", "op": "=", "value": 1}],
		):
			with self.assertRaises(queries.QueryError):
				queries.validate(self.plan(where=where))
		with self.assertRaises(queries.QueryError):
			queries.validate(
				{
					"operation": "details",
					"doctype": "auto",
					"where": [{"field": "name", "op": "=", "value": "x"}],
				}
			)  # no DocType to apply it to

	def test_count_applies_filters_and_company_scope(self):
		for label, expected in (("A", 3), ("B", 1), ("AB", 4), ("NONE", 0)):
			frappe.set_user(self.users[label])
			text = run(self.plan(operation="count", where=[{"field": "grand_total", "op": ">", "value": 0}]))[
				"text"
			]
			self.assertIn(f" {expected} Sales Invoice", text, label)

	def test_stored_plans_are_revalidated(self):
		frappe.set_user(self.users["A"])
		q = queries.validate(self.plan(where=[{"field": "grand_total", "op": ">", "value": 90}]))
		q["where"] = [["debit_to", "=", "x"]]  # a tampered plan coming back from conversation state
		with self.assertRaises(queries.QueryError):
			queries.execute(q)
		q = queries.validate(self.plan(operation="list"))
		q["operation"] = "submit"
		with self.assertRaises(queries.QueryError):
			queries.execute(q)

	# ---- pagination ----------------------------------------------------------------------------
	def test_pagination_is_bounded(self):
		frappe.set_user(self.users["AB"])
		q = queries.validate(self.plan(limit=1000, offset=10**9))
		self.assertEqual((q["limit"], q["offset"]), (queries.MAX_PAGE, queries.MAX_OFFSET))
		q = queries.validate(self.plan(limit=-5, offset=-5))
		self.assertEqual((q["limit"], q["offset"]), (1, 0))
		first = run(
			self.plan(
				limit=2, where=[{"field": "customer", "op": "=", "value": self.customer}], sort="oldest"
			)
		)
		self.assertTrue(first["card"]["has_more"])
		self.assertEqual(len(rows(first)), 2)
		second = run(
			self.plan(
				limit=2,
				offset=2,
				where=[{"field": "customer", "op": "=", "value": self.customer}],
				sort="oldest",
			)
		)
		self.assertFalse(set(first["names"]) & set(second["names"]))

	# ---- aggregates ----------------------------------------------------------------------------
	def agg(self, fn, field=None, group_by=None, **extra):
		return run(
			self.plan(
				operation="aggregate", aggregate={"fn": fn, "field": field, "group_by": group_by}, **extra
			)
		)

	def test_sum_is_grouped_by_currency_never_mixed(self):
		frappe.set_user(self.users["A"])
		result = self.agg("sum", "grand_total")
		by_currency = {r["currency"]: r["value"] for r in rows(result)}
		self.assertEqual(set(by_currency) & {"USD", self.currency}, {"USD", self.currency})
		self.assertEqual(by_currency["USD"], 40.0)
		self.assertEqual(
			by_currency[self.currency], 550.0
		)  # 300 + 250 of company A only; company B's 630 is not included
		self.assertIn("never added together", result["card"]["note"])

	def test_aggregates_respect_company_isolation(self):
		expected = {"A": 550.0, "B": 630.0, "AB": 1180.0}
		for label, total in expected.items():
			frappe.set_user(self.users[label])
			by_currency = {r["currency"]: r["value"] for r in rows(self.agg("sum", "grand_total"))}
			self.assertEqual(by_currency.get(self.currency), total, label)
		frappe.set_user(self.users["NONE"])
		self.assertEqual(rows(self.agg("sum", "grand_total")), [])
		self.assertIn("found no", self.agg("count")["text"])

	def test_count_avg_min_max_and_groups(self):
		frappe.set_user(self.users["AB"])
		per_company = {r["company"]: r["value"] for r in rows(self.agg("count", group_by="company"))}
		self.assertEqual((per_company[COMPANIES["A"]], per_company[COMPANIES["B"]]), (3, 1))
		avg = {
			r["currency"]: r["value"]
			for r in rows(
				self.agg("avg", "grand_total", filters={"party": "AIQ Shared", "company": COMPANIES["A"]})
			)
		}
		self.assertEqual(avg[self.currency], 275.0)
		lo = {r["currency"]: r["value"] for r in rows(self.agg("min", "grand_total"))}
		hi = {r["currency"]: r["value"] for r in rows(self.agg("max", "grand_total"))}
		self.assertEqual((lo[self.currency], hi[self.currency]), (250.0, 630.0))
		by_customer = rows(self.agg("sum", "grand_total", group_by="customer"))
		self.assertTrue(all(r["customer"] == self.customer for r in by_customer))
		newest = rows(self.agg("max", "posting_date"))
		self.assertEqual(newest[0]["value"], str(add_days(nowdate(), -5)))

	def test_aggregate_with_filters_and_month_buckets(self):
		frappe.set_user(self.users["AB"])
		filtered = rows(
			self.agg("sum", "grand_total", where=[{"field": "grand_total", "op": ">", "value": 260}])
		)
		self.assertEqual({r["currency"]: r["value"] for r in filtered}, {self.currency: 930.0})
		with self.assertRaises(queries.QueryClarify):
			queries.validate(self.plan(operation="aggregate", aggregate={"fn": "count", "group_by": "month"}))
		monthly = self.agg("count", group_by="month", date={"preset": "last_year"})
		self.assertIn("found no", monthly["text"])
		this_year = self.agg("count", group_by="month", date={"preset": "last_30_days"})
		self.assertEqual(sum(r["value"] for r in rows(this_year)), 4)
		self.assertTrue(all(len(r["bucket"]) == 7 for r in rows(this_year)))

	def test_aggregate_plan_shapes_are_validated(self):
		frappe.set_user(self.users["AB"])
		for aggregate in (
			{"fn": "sum", "field": "customer"},
			{"fn": "sum", "field": "debit_to"},
			{"fn": "median", "field": "grand_total"},
			{"fn": "sum", "field": "grand_total", "group_by": "owner"},
		):
			with self.assertRaises(queries.QueryError, msg=str(aggregate)):
				queries.validate(self.plan(operation="aggregate", aggregate=aggregate))
		with self.assertRaises(queries.QueryError):
			queries.validate(self.plan(operation="details", aggregate={"fn": "count"}, filters={"name": "x"}))
		# an aggregate on a list request makes it an aggregate; the operation is never silently dropped
		q = queries.validate(self.plan(aggregate={"fn": "count"}))
		self.assertEqual(q["operation"], "aggregate")

	def test_existing_summary_handlers_still_work(self):
		frappe.set_user(self.users["AB"])
		summary = run(
			{"doctype": "Sales Invoice", "operation": "sales_summary", "date": {"preset": "last_30_days"}}
		)
		self.assertTrue(rows(summary) or "found no" in summary["text"])
		outstanding = run({"doctype": "Sales Invoice", "operation": "outstanding_by_customer"})
		self.assertIn("text", outstanding)

	# ---- child tables -------------------------------------------------------------------------
	def test_parents_can_be_found_by_a_child_row_condition(self):
		frappe.set_user(self.users["A"])
		child = {"table": "items", "where": [{"field": "item_code", "op": "=", "value": "AIQ-ITEM"}]}
		found = run(self.plan(child=child))
		self.assertEqual(self.mine(found["names"]), {self.invoices["A"][0], self.usd})
		self.assertIn("having items", found["card"]["note"])
		big = run(self.plan(child={"table": "items", "where": [{"field": "qty", "op": ">=", "value": 9}]}))
		self.assertEqual(self.mine(big["names"]), set())  # the matching row belongs to company B's invoice
		count = run(self.plan(operation="count", child=child))["text"]
		self.assertIn(" 2 Sales Invoice", count)

	def test_child_rows_only_come_from_accessible_parents(self):
		child = {
			"table": "items",
			"fields": ["item_code", "qty", "rate"],
			"where": [{"field": "item_code", "op": "=", "value": "AIQ-ITEM"}],
		}
		expect = {
			"A": {self.invoices["A"][0], self.usd},
			"B": {self.invoices["B"][0]},
			"AB": {self.invoices["A"][0], self.usd, self.invoices["B"][0]},
		}
		for label, parents in expect.items():
			frappe.set_user(self.users[label])
			result = run(self.plan(operation="child_rows", child=child))
			self.assertEqual(
				{r["parent"] for r in rows(result)} & self.mine({r["parent"] for r in rows(result)}),
				parents,
				label,
			)
			self.assertEqual(
				set(result["card"]["rows"][0]) - {"_name", "_n"}, {"parent", "item_code", "qty", "rate"}
			)
		frappe.set_user(self.users["NONE"])
		self.assertEqual(rows(run(self.plan(operation="child_rows", child=child))), [])
		frappe.set_user(self.users["A"])
		qty = {
			r["parent"]: r["qty"]
			for r in rows(run(self.plan(operation="child_rows", child={"table": "items", "fields": ["qty"]})))
		}
		self.assertEqual(qty[self.invoices["A"][0]], 3.0)
		self.assertNotIn(self.invoices["B"][0], qty)  # never a row of an inaccessible parent

	def test_child_requests_outside_the_policy_are_rejected(self):
		frappe.set_user(self.users["A"])
		for child in (
			{"table": "payment_schedule"},
			{"table": "items", "fields": ["cost_center"]},
			{"table": "items", "where": [{"field": "owner", "op": "=", "value": "x"}]},
			{"table": "items", "where": [{"field": "qty", "op": "like", "value": "1"}]},
		):
			with self.assertRaises(queries.QueryError, msg=str(child)):
				queries.validate(self.plan(operation="child_rows", child=child))
		with self.assertRaises(queries.QueryError):
			queries.validate({"doctype": "Customer", "operation": "child_rows", "child": {"table": "items"}})
		with self.assertRaises(queries.QueryError):
			queries.validate(self.plan(operation="child_rows"))  # no table named

	def test_restricted_child_fields_are_not_shown(self):
		frappe.set_user(self.users["A"])
		child = {"table": "items", "fields": ["item_code", "rate"]}
		with mock.patch(
			"ai_erpnext.capability.service.get_permitted_fields",
			side_effect=lambda dt, **kw: ["item_code", "name", "parent"],
		):
			with self.assertRaises(queries.QueryError):  # rate is not readable for this user any more
				queries.validate(self.plan(operation="child_rows", child=child))

	# ---- linked records ------------------------------------------------------------------------
	def test_linked_record_fields_are_added_next_to_the_list(self):
		frappe.set_user(self.users["A"])
		result = run(self.plan(link={"field": "customer", "fields": ["customer_group", "territory"]}))
		card = result["card"]
		self.assertIn("customer.customer_group", {c["key"] for c in card["columns"]})
		row = next(r for r in card["rows"] if r["_name"] == self.invoices["A"][0])
		self.assertEqual(row["customer.customer_group"], self.group)

	def test_linked_record_is_not_revealed_when_unreadable(self):
		frappe.set_user(self.users["A"])
		real = frappe.has_permission
		with mock.patch(
			"frappe.has_permission",
			side_effect=lambda dt=None, *a, **k: False if dt == "Customer" else real(dt, *a, **k),
		):
			result = run(self.plan(link={"field": "customer", "fields": ["customer_group"]}))
		self.assertTrue(rows(result))
		self.assertTrue(
			all(r.get("customer.customer_group") is None for r in rows(result))
		)  # blank: same as a missing record

	def test_link_requests_outside_the_policy_are_rejected(self):
		frappe.set_user(self.users["A"])
		for link in (
			{"field": "company"},
			{"field": "debit_to"},
			{"field": "customer", "fields": ["tax_id"]},
			{"field": "customer.customer_group"},
		):
			with self.assertRaises(queries.QueryError, msg=str(link)):
				queries.validate(self.plan(link=link))
		with self.assertRaises(queries.QueryError):
			queries.validate(self.plan(operation="count", link={"field": "customer"}))
		with self.assertRaises(queries.QueryError):
			queries.validate(
				self.plan(operation="aggregate", aggregate={"fn": "count"}, link={"field": "customer"})
			)

	# ---- query-only DocTypes -----------------------------------------------------------------
	def names(self, doctype, **extra):
		return set(run({"doctype": doctype, "operation": "list", "limit": 20, **extra})["names"])

	def test_new_doctypes_are_queryable_with_company_isolation(self):
		for doctype in ("Lead", "Project", "Issue", "Task"):
			a, b = self.records[(doctype, "A")], self.records[(doctype, "B")]
			for label, expected in (("A", {a}), ("B", {b}), ("AB", {a, b}), ("NONE", set())):
				frappe.set_user(self.users[label])
				self.assertEqual(self.names(doctype) & {a, b}, expected, f"{doctype} {label}")

	def test_records_without_a_company_stay_visible_only_where_the_doctype_allows_it(self):
		none_task = self.records[("Task", "none")]
		for label in ("A", "NONE"):
			frappe.set_user(self.users[label])
			self.assertIn(
				none_task, self.names("Task"), label
			)  # Task.company is optional: not tied to any company
		self.assertEqual(queries.REGISTRY["Task"].optional_company, True)
		self.assertEqual(
			queries.REGISTRY["Project"].optional_company, False
		)  # a required company is always enforced

	def test_quotation_is_queryable_and_still_creatable(self):
		from ai_erpnext.chat import forms

		self.assertIn("Quotation", forms.CREATABLE)
		quote = self.records[("Quotation", "A")]
		frappe.set_user(self.users["A"])
		self.assertIn(quote, self.names("Quotation"))
		details = run({"doctype": "Quotation", "operation": "details", "filters": {"name": quote}})
		self.assertEqual(details["card"]["name"], quote)
		self.assertTrue(details["card"]["items"]["rows"])
		for label in ("B", "NONE"):
			frappe.set_user(self.users[label])
			self.assertNotIn(quote, self.names("Quotation"), label)
			self.assertIn(
				"couldn't find",
				run({"doctype": "Quotation", "operation": "details", "filters": {"name": quote}})["text"],
				label,
			)
		self.assertNotIn("edit", queries.REGISTRY["Quotation"].operations)

	def test_doctype_without_read_permission_is_refused(self):
		frappe.set_user(self.users["NOROLES"])
		for doctype in ("Lead", "Issue", "Project", "Bin", "Quotation"):
			with self.assertRaises(queries.QueryError, msg=doctype) as ctx:
				queries.validate({"doctype": doctype, "operation": "list"})
			self.assertIn("permission", str(ctx.exception))

	def test_bin_respects_company_and_warehouse_permissions(self):
		a, b = self.records[("Bin", "A")], self.records[("Bin", "B")]
		for label, expected in (("A", {a}), ("B", {b}), ("AB", {a, b}), ("NONE", set())):
			frappe.set_user(self.users[label])
			self.assertEqual(self.names("Bin") & {a, b}, expected, label)
		# a warehouse User Permission narrows it further, even inside the permitted company
		email = self.make_user("aiq-wh@example.com", ROLES, [COMPANIES["A"], COMPANIES["B"]])
		frappe.get_doc(
			{
				"doctype": "User Permission",
				"user": email,
				"allow": "Warehouse",
				"for_value": self.records[("Warehouse", "B")],
			}
		).insert(ignore_permissions=True)
		frappe.cache.delete_value("user_permissions")
		frappe.set_user(email)
		self.assertEqual(self.names("Bin") & {a, b}, {b})
		total = rows(
			run(
				{
					"doctype": "Bin",
					"operation": "aggregate",
					"aggregate": {"fn": "sum", "field": "actual_qty", "group_by": "item_code"},
					"where": [{"field": "item_code", "op": "=", "value": "AIQ-STOCK"}],
				}
			)
		)
		self.assertEqual(total[0]["value"], 8.0)
		with self.assertRaises(queries.QueryError):
			queries.validate({"doctype": "Bin", "operation": "details", "filters": {"name": b}})
		with self.assertRaises(queries.QueryError):
			queries.validate({"doctype": "Bin", "operation": "list", "fields": ["valuation_rate"]})
		with self.assertRaises(queries.QueryError):
			queries.validate(
				{
					"doctype": "Bin",
					"operation": "aggregate",
					"aggregate": {"fn": "avg", "field": "valuation_rate"},
				}
			)

	def test_company_scope_comes_from_company_user_permissions_not_from_listing_companies(self):
		"""Since ERPNext v16 a Company links to its default warehouse, so a Warehouse User Permission can hide Company records.
		The chat's company scope must still be the user's Company User Permissions (as ERPNext applies them), not "none"."""
		real = frappe.get_list

		def hidden_companies(doctype, *args, **kwargs):
			# only permission-checked listings are hidden (frappe.get_all goes through get_list with ignore_permissions)
			return (
				[]
				if doctype == "Company" and not kwargs.get("ignore_permissions")
				else real(doctype, *args, **kwargs)
			)

		frappe.set_user(self.users["A"])
		with mock.patch("frappe.get_list", side_effect=hidden_companies):
			self.assertEqual(queries.permitted_companies("Sales Invoice"), [COMPANIES["A"]])
			self.assertEqual(self.mine(run(self.plan())["names"]), set(self.invoices["A"]) | {self.usd})
			self.assertEqual(
				self.names("Bin") & {self.records[("Bin", "A")], self.records[("Bin", "B")]},
				{self.records[("Bin", "A")]},
			)
			self.assertEqual(
				queries.validate(self.plan(filters={"party": "AIQ Shared", "company": "AIQA"}))["company"],
				COMPANIES["A"],
			)
			with self.assertRaises(queries.QueryError):
				queries.validate(
					self.plan(filters={"company": COMPANIES["B"]})
				)  # still not a company of this user
		frappe.set_user("Administrator")
		self.assertIsNone(
			queries.permitted_companies("Sales Invoice")
		)  # no restriction: get_list decides, as in ERPNext

	def test_company_permissions_for_some_doctypes_only(self):
		email = self.make_user("aiq-applicable@example.com", ROLES, [])
		frappe.get_doc(
			{
				"doctype": "User Permission",
				"user": email,
				"allow": "Company",
				"for_value": COMPANIES["A"],
				"apply_to_all_doctypes": 0,
				"applicable_for": "Sales Invoice",
			}
		).insert(ignore_permissions=True)
		frappe.cache.delete_value("user_permissions")
		frappe.set_user(email)
		self.assertEqual(queries.permitted_companies("Sales Invoice"), [COMPANIES["A"]])
		self.assertIsNone(queries.permitted_companies("Bin"))  # the permission doesn't apply there
		self.assertEqual(self.mine(run(self.plan())["names"]), set(self.invoices["A"]) | {self.usd})

	def test_new_doctype_aggregates_are_company_scoped(self):
		frappe.set_user(self.users["A"])
		by_status = {
			r["status"]: r["value"]
			for r in rows(
				run(
					{
						"doctype": "Task",
						"operation": "aggregate",
						"aggregate": {"fn": "count", "group_by": "status"},
					}
				)
			)
		}
		self.assertTrue(by_status.get("Open", 0) >= 2)  # A's task and the company-less one; never B's
		frappe.set_user(self.users["B"])
		self.assertEqual(self.names("Task") & {self.records[("Task", "A")]}, set())
