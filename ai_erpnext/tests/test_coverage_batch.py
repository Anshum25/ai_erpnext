"""Batch 2 of the read-only coverage (accounting, stock, manufacturing, quality, assets, subcontracting, CRM, projects,
support, shared masters), the period/group aggregates, overdue-days filter, customer summary, and guided creation of
Lead and Issue.

Two test companies, users bound to them, real submitted invoices (so ledger entries exist), everything rolled back.

Run: bench --site <site> run-tests --module ai_erpnext.tests.test_coverage_batch
"""

import json
from unittest import mock

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_days, add_months, getdate, nowdate

from ai_erpnext.api import chat
from ai_erpnext.capability import policy as P
from ai_erpnext.chat import queries, shortcuts

COMPANIES = {"A": "AIB Alpha Co", "B": "AIB Beta Co"}
ROLES = ["Accounts User", "Accounts Manager", "Sales User", "Stock User", "Projects User", "Support Team", "Purchase User", "Manufacturing User",
	"Quality Manager", "Item Manager"]
ROUTE = "ai_erpnext.chat.llm.route"


def run(plan):
	return queries.execute(queries.validate(plan))


def rows(result):
	return (result.get("card") or {}).get("rows", [])


class TestCoverageBatch(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		currency = cls.currency = frappe.db.get_value("Company", frappe.db.get_single_value("Global Defaults", "default_company"), "default_currency") or "INR"
		country = frappe.db.get_single_value("System Settings", "country") or "India"
		for key, name in COMPANIES.items():
			if not frappe.db.exists("Company", name):
				frappe.get_doc({"doctype": "Company", "company_name": name, "abbr": f"AIB{key}", "default_currency": currency, "country": country,
					"create_chart_of_accounts_based_on": "Standard Template", "chart_of_accounts": "Standard"}).insert()
		group = frappe.db.get_value("Customer Group", {"is_group": 0}, "name")
		territory = frappe.db.get_value("Territory", {"is_group": 0}, "name")
		cls.customers = {n: frappe.get_doc({"doctype": "Customer", "customer_name": n, "customer_group": group, "territory": territory}).insert().name
			for n in ("AIB Buyer One", "AIB Buyer Two")}
		ig = frappe.db.get_value("Item Group", {"is_group": 0}, "name")
		frappe.get_doc({"doctype": "Item", "item_code": "AIB-ITEM", "item_name": "AIB Item", "item_group": ig, "stock_uom": "Nos", "is_stock_item": 0,
			"is_sales_item": 1}).insert()
		cls.invoices = {}
		today = nowdate()
		for key, customer, rate, due_days_ago in (("A", "AIB Buyer One", 100, 45), ("A", "AIB Buyer Two", 200, 10), ("B", "AIB Buyer One", 300, 50)):
			posting = add_days(today, -(due_days_ago + 3))
			due = add_days(today, -due_days_ago)
			si = frappe.get_doc({"doctype": "Sales Invoice", "customer": cls.customers[customer], "company": COMPANIES[key], "posting_date": posting,
				"set_posting_time": 1, "due_date": due, "items": [{"item_code": "AIB-ITEM", "qty": 1, "rate": rate}]}).insert()
			si.submit()  # test data only: the chat itself never submits
			cls.invoices[(key, customer)] = si.name
		cls.order = frappe.get_doc({"doctype": "Sales Order", "customer": cls.customers["AIB Buyer One"], "company": COMPANIES["A"],
			"transaction_date": today, "delivery_date": add_days(today, 5), "items": [{"item_code": "AIB-ITEM", "qty": 2, "rate": 50, "delivery_date": add_days(today, 5)}]}).insert().name
		cls.users = {}
		elsewhere = frappe.db.get_single_value("Global Defaults", "default_company")  # "NONE": permitted only for an unrelated company
		for label, allowed in {"A": ["A"], "B": ["B"], "AB": ["A", "B"], "NONE": []}.items():
			cls.users[label] = cls.make_user(f"aib-{label.lower()}@example.com", ROLES, [COMPANIES[k] for k in allowed] or [elsewhere], system_manager=True)
		cls.users["NOROLES"] = cls.make_user("aib-noroles@example.com", ["Accounts User"], [])
		frappe.cache.delete_value("user_permissions")

	@classmethod
	def make_user(cls, email, roles, companies, system_manager=False):
		if not frappe.db.exists("User", email):
			frappe.get_doc({"doctype": "User", "email": email, "first_name": email.split("@")[0], "send_welcome_email": 0,
				"roles": [{"role": r} for r in (roles + (["System Manager"] if system_manager else [])) if frappe.db.exists("Role", r)]}).insert(ignore_permissions=True)
		for company in companies:
			frappe.get_doc({"doctype": "User Permission", "user": email, "allow": "Company", "for_value": company, "apply_to_all_doctypes": 1}).insert(ignore_permissions=True)
		return email

	def setUp(self):
		frappe.set_user("Administrator")
		mock.patch("frappe.enqueue").start()
		self.addCleanup(mock.patch.stopall)

	def tearDown(self):
		frappe.set_user("Administrator")

	# ---- every registered DocType runs safely -----------------------------------------------------
	def test_every_registered_doctype_lists_counts_and_aggregates_without_error(self):
		frappe.set_user(self.users["AB"])
		ran = 0
		for dt, spec in P.REGISTRY.items():
			try:
				queries.validate({"doctype": dt, "operation": "list"})
			except queries.QueryError as e:
				self.assertIn("permission", str(e), dt)  # a DocType this test user's roles can't read (e.g. Employee without HR roles)
				continue
			ran += 1
			result = run({"doctype": dt, "operation": "list", "limit": 3})
			self.assertTrue(result["text"], dt)
			self.assertRegex(run({"doctype": dt, "operation": "count"})["text"], r"There (is|are) \d+ ", dt)
			if "aggregate" in spec.operations:
				self.assertTrue(run({"doctype": dt, "operation": "aggregate", "aggregate": {"fn": "count"}})["text"], dt)
			tables = queries.capability.child_tables(dt)
			if tables and "child_rows" in spec.operations:
				table = next(iter(tables))
				self.assertTrue(run({"doctype": dt, "operation": "child_rows", "child": {"table": table}})["text"], dt)

	def test_the_smoke_test_covers_nearly_all_doctypes(self):
		frappe.set_user(self.users["AB"])
		readable = [dt for dt in P.REGISTRY if frappe.has_permission(dt, "read")]
		self.assertGreaterEqual(len(readable), len(P.REGISTRY) - 4)

	def test_a_user_without_access_gets_a_clear_refusal_for_every_doctype(self):
		frappe.set_user(self.users["NOROLES"])
		refused = 0
		for dt in P.REGISTRY:
			try:
				result = run({"doctype": dt, "operation": "count"})
			except queries.QueryError as e:
				self.assertIn("permission", str(e), dt)
				refused += 1
			else:
				self.assertRegex(result["text"], r"There (is|are) \d+ ", dt)  # a DocType this role may read
		self.assertGreater(refused, 10)

	def test_company_scoping_of_new_company_doctypes(self):
		"""Accounts and cost centers are created with each company: every user sees exactly the accounts of their own companies."""
		for doctype in ("Account", "Cost Center", "Warehouse"):
			ours = {k: set(frappe.get_all(doctype, filters={"company": COMPANIES[k]}, pluck="name")) for k in COMPANIES}
			for label, keys in (("A", ["A"]), ("B", ["B"]), ("AB", ["A", "B"]), ("NONE", [])):
				frappe.set_user(self.users[label])
				seen = set()
				for key in COMPANIES:
					seen |= {r["_name"] for r in rows(run({"doctype": doctype, "operation": "list", "limit": 20, "filters": {"company": COMPANIES[key]}}))} if key in keys else set()
				found = {r["_name"] for r in rows(run({"doctype": doctype, "operation": "list", "limit": 20}))}
				expected = set().union(*(ours[k] for k in keys)) if keys else set()
				self.assertTrue(found & set().union(*ours.values()) <= expected, f"{doctype} {label} sees another company's records")
				if keys:
					self.assertTrue(found & expected, f"{doctype} {label} sees nothing")
				frappe.set_user("Administrator")

	def test_shared_masters_follow_user_permissions(self):
		territories = frappe.get_all("Territory", filters={"is_group": 0}, pluck="name", limit=2)
		if len(territories) < 2:
			frappe.get_doc({"doctype": "Territory", "territory_name": "AIB Territory", "parent_territory": "All Territories", "is_group": 0}).insert()
			territories = frappe.get_all("Territory", filters={"is_group": 0}, pluck="name", limit=2)
		email = self.make_user("aib-territory@example.com", ROLES, [], system_manager=True)
		frappe.get_doc({"doctype": "User Permission", "user": email, "allow": "Territory", "for_value": territories[0]}).insert(ignore_permissions=True)
		frappe.cache.delete_value("user_permissions")
		frappe.set_user(email)
		names = {r["_name"] for r in rows(run({"doctype": "Territory", "operation": "list", "limit": 20}))}
		self.assertIn(territories[0], names)
		self.assertNotIn(territories[1], names)

	# ---- accounting: ledger entries ---------------------------------------------------------------
	def gl(self, **extra):
		return {"doctype": "GL Entry", "operation": "aggregate", "aggregate": {"fn": "sum", "field": "debit", "group_by": "company"}, **extra}

	def test_gl_entry_totals_are_company_scoped_and_use_company_currency(self):
		for label, keys in (("A", ["A"]), ("B", ["B"]), ("AB", ["A", "B"]), ("NONE", [])):
			frappe.set_user(self.users[label])
			result = run(self.gl(where=[{"field": "voucher_type", "op": "=", "value": "Sales Invoice"}]))
			by_company = {r["company"]: r for r in rows(result)}
			ours = {c: r for c, r in by_company.items() if c in COMPANIES.values()}
			self.assertEqual(set(ours), {COMPANIES[k] for k in keys}, label)
			for r in ours.values():
				self.assertEqual(r["currency"], self.currency)  # company-currency amounts are labelled and grouped by company
		frappe.set_user(self.users["AB"])
		self.assertIn("never added together", run(self.gl())["card"]["note"])

	def test_gl_entries_balance_and_cancelled_entries_are_not_counted(self):
		frappe.set_user("Administrator")
		voucher = self.invoices[("A", "AIB Buyer One")]
		debit = rows(run({"doctype": "GL Entry", "operation": "aggregate", "filters": {"search": voucher}, "aggregate": {"fn": "sum", "field": "debit", "group_by": "company"}}))
		credit = rows(run({"doctype": "GL Entry", "operation": "aggregate", "filters": {"search": voucher}, "aggregate": {"fn": "sum", "field": "credit", "group_by": "company"}}))
		self.assertTrue(debit and debit[0]["value"] == credit[0]["value"] > 0)
		self.assertNotIn("valuation", json.dumps(run({"doctype": "GL Entry", "operation": "list", "limit": 3})).lower())
		cancelled = frappe.get_doc("Sales Invoice", self.invoices[("B", "AIB Buyer One")])
		self.assertTrue(frappe.db.count("GL Entry", {"voucher_no": cancelled.name, "is_cancelled": 0}))
		frappe.db.set_value("GL Entry", {"voucher_no": cancelled.name}, "is_cancelled", 1)  # what cancelling does to its ledger rows
		try:
			visible = run({"doctype": "GL Entry", "operation": "list", "filters": {"search": cancelled.name}, "limit": 20})
			self.assertEqual(rows(visible), [])
			self.assertIn("cancelled", frappe.get_meta("GL Entry").get_field("is_cancelled").label.lower())
		finally:
			frappe.db.set_value("GL Entry", {"voucher_no": cancelled.name}, "is_cancelled", 0)  # the fixture is shared by the other tests
		self.assertTrue(rows(run({"doctype": "GL Entry", "operation": "list", "filters": {"search": cancelled.name}, "limit": 20})))

	# ---- period + group aggregates, overdue days, summary -----------------------------------------
	def test_monthly_sales_per_customer(self):
		frappe.set_user(self.users["AB"])
		result = run({"doctype": "Sales Invoice", "operation": "aggregate", "date": {"preset": "last_30_days"}, "filters": {"party": "AIB Buyer"},
			"aggregate": {"fn": "sum", "field": "grand_total", "group_by": "customer", "period": "month"}})
		self.assertTrue(rows(result))
		self.assertTrue(all({"bucket", "customer", "currency", "value"} <= set(r) for r in rows(result)))
		self.assertIn("customer and month", result["card"]["title"])
		with self.assertRaises(queries.QueryClarify):
			queries.validate({"doctype": "Sales Invoice", "operation": "aggregate", "aggregate": {"fn": "sum", "field": "grand_total", "group_by": "customer", "period": "month"}})
		per_customer = {}
		for r in rows(run({"doctype": "Sales Invoice", "operation": "aggregate", "date": {"from": str(add_months(nowdate(), -3)), "to": nowdate()}, "filters": {"party": "AIB Buyer"},
			"aggregate": {"fn": "sum", "field": "grand_total", "group_by": "customer", "period": "month"}})):
			per_customer[r["customer"]] = per_customer.get(r["customer"], 0) + r["value"]
		self.assertEqual(per_customer[self.customers["AIB Buyer Two"]], 200.0)  # tax-free test invoices: grand total equals the rate

	def test_overdue_by_more_than_n_days(self):
		frappe.set_user(self.users["AB"])
		result = run({"doctype": "Sales Invoice", "operation": "list", "filters": {"party": "AIB Buyer"}, "overdue_days": 30, "limit": 20})
		names = {r["_name"] for r in rows(result)}
		self.assertEqual(names & set(self.invoices.values()), {self.invoices[("A", "AIB Buyer One")], self.invoices[("B", "AIB Buyer One")]})
		self.assertIn("overdue by more than 30 days", result["card"]["note"])
		count = run({"doctype": "Sales Invoice", "operation": "count", "filters": {"party": "AIB Buyer"}, "overdue_days": 30})["text"]
		self.assertIn(" 2 Sales Invoice", count)
		frappe.set_user(self.users["A"])
		names = {r["_name"] for r in rows(run({"doctype": "Sales Invoice", "operation": "list", "filters": {"party": "AIB Buyer"}, "overdue_days": 30, "limit": 20}))}
		self.assertEqual(names & set(self.invoices.values()), {self.invoices[("A", "AIB Buyer One")]})
		for bad in (-3, 5000, "x", [30], {"a": 1}, True):
			with self.assertRaises(queries.QueryError, msg=str(bad)):
				queries.validate({"doctype": "Sales Invoice", "operation": "list", "overdue_days": bad})
		with self.assertRaises(queries.QueryError):
			queries.validate({"doctype": "Customer", "operation": "list", "overdue_days": 30})

	def summary(self, user, name="AIB Buyer One"):
		frappe.set_user(user)
		return run({"doctype": "Customer", "operation": "summary", "filters": {"search": name}})

	def test_customer_summary_is_company_scoped_and_currency_aware(self):
		both = rows(self.summary(self.users["AB"]))
		invoices = [r for r in both if r["document"] == "Sales Invoice" and r["state"] == "Submitted"]
		self.assertEqual(sum(r["total"] for r in invoices if r["currency"] == self.currency), 400.0)  # 100 (company A) + 300 (company B)
		self.assertEqual(sum(r["outstanding"] for r in invoices if r["currency"] == self.currency), 400.0)
		self.assertTrue([r for r in both if r["document"] == "Sales Order" and r["state"] == "Draft"])
		only_a = rows(self.summary(self.users["A"]))
		self.assertEqual(sum(r["total"] for r in only_a if r["document"] == "Sales Invoice" and r["currency"] == self.currency), 100.0)
		only_b = rows(self.summary(self.users["B"]))
		self.assertFalse([r for r in only_b if r["document"] == "Sales Order"])  # the order belongs to company A

	def test_summary_names_what_it_could_not_include(self):
		real = frappe.has_permission
		frappe.set_user(self.users["A"])
		with mock.patch("frappe.has_permission", side_effect=lambda dt=None, *a, **k: False if dt == "Sales Order" else real(dt, *a, **k)):
			result = run({"doctype": "Customer", "operation": "summary", "filters": {"search": "AIB Buyer One"}})
		self.assertIn("Not included (no permission): Sales Order", result["card"]["note"])
		self.assertFalse([r for r in rows(result) if r["document"] == "Sales Order"])
		with mock.patch("frappe.has_permission", side_effect=lambda dt=None, *a, **k: False if dt in ("Sales Order", "Sales Invoice") else real(dt, *a, **k)):
			with self.assertRaises(queries.QueryError):
				run({"doctype": "Customer", "operation": "summary", "filters": {"search": "AIB Buyer One"}})

	def test_summary_for_an_unknown_customer_invents_nothing(self):
		result = self.summary(self.users["A"], "No Such Buyer Anywhere")
		self.assertTrue(result.get("not_found"))
		self.assertNotIn("card", result)
		with self.assertRaises(queries.QueryClarify):
			queries.validate({"doctype": "Customer", "operation": "summary"})
		with self.assertRaises(queries.QueryError):
			queries.validate({"doctype": "Item", "operation": "summary", "filters": {"search": "x"}})

	def test_history_of_a_summary_needs_the_underlying_documents(self):
		from ai_erpnext.chat import history

		msg = {"id": 3, "role": "assistant", "source": "erpnext", "text": "Here is the summary", "card": {"type": "table", "doctype": None, "rows": [{"x": 1}]},
			"ref": {"doctype": "Customer", "names": [self.customers["AIB Buyer One"]], "requires": ["Sales Order", "Sales Invoice"]}}
		frappe.set_user(self.users["A"])
		self.assertEqual(history.recheck([dict(msg)], 5)[0].get("card", {}).get("type"), "table")
		real = frappe.has_permission
		with mock.patch("frappe.has_permission", side_effect=lambda dt=None, *a, **k: False if dt == "Sales Invoice" else real(dt, *a, **k)):
			self.assertEqual(history.recheck([dict(msg)], 5)[0]["text"], history.SAFE_TEXT)

	# ---- stock: ledger entries ------------------------------------------------------------------
	def test_stock_ledger_rows_never_expose_valuation(self):
		frappe.set_user(self.users["AB"])
		result = run({"doctype": "Stock Ledger Entry", "operation": "list", "limit": 3})
		self.assertFalse({"valuation_rate", "stock_value", "incoming_rate", "outgoing_rate"} & {c["key"] for c in (result.get("card") or {}).get("columns", [])})
		for field in ("valuation_rate", "stock_value", "incoming_rate"):
			with self.assertRaises(queries.QueryError):
				queries.validate({"doctype": "Stock Ledger Entry", "operation": "list", "where": [{"field": field, "op": ">", "value": 0}]})
			with self.assertRaises(queries.QueryError):
				queries.validate({"doctype": "Stock Ledger Entry", "operation": "aggregate", "aggregate": {"fn": "sum", "field": field}})

	# ---- shortcuts ------------------------------------------------------------------------------
	def test_shortcuts_for_new_doctypes_and_names_that_start_like_a_doctype(self):
		plan = shortcuts.match("show the last 5 territories")
		self.assertEqual((plan["query"]["doctype"], plan["query"]["operation"]), ("Territory", "list"))
		self.assertEqual(shortcuts.match("how many batches")["query"]["doctype"], "Batch")
		self.assertEqual(shortcuts.match("list opportunities")["query"]["doctype"], "Opportunity")
		self.assertEqual(shortcuts.match("show gl entries")["query"]["doctype"], "GL Entry")
		self.assertEqual(shortcuts.match("count stock ledger entries")["query"]["doctype"], "Stock Ledger Entry")
		summary = shortcuts.match("Give me a summary of customer AIB Buyer One")
		self.assertEqual((summary["query"]["doctype"], summary["query"]["operation"], summary["query"]["filters"]["search"]), ("Customer", "summary", "AIB Buyer One"))
		self.assertEqual(shortcuts.match("summarize supplier Acme Ltd")["query"]["doctype"], "Supplier")
		# a customer called "Project Hub Ltd" is still found as a party, not read as "project Hub Ltd"
		for name in ("Project Hub Ltd", "Account Managers Co", "Asset Light Traders", "Task Force Inc", "Batch Brothers"):
			plan = shortcuts.match(f"what is the mobile number of {name}")
			self.assertEqual(plan["query"]["doctype"], "auto", name)
			self.assertEqual(plan["query"]["filters"]["search"], name, name)
		self.assertEqual(shortcuts.match("mobile number of customer Amit Patel")["query"]["doctype"], "Customer")  # real prefixes still work
		self.assertEqual(shortcuts.match("status of task TASK-2026-00001")["query"]["doctype"], "Task")  # an ID makes the DocType word count

	def test_router_prompt_lists_new_capabilities_only(self):
		from ai_erpnext.chat import llm

		schema = "\n" + llm._schema()
		for dt in ("GL Entry", "Work Order", "Timesheet", "Item Price"):
			self.assertIn(f"\n{dt}:", schema)
		self.assertNotIn("\nBank Account:", schema)
		self.assertNotIn("\nSalary Slip:", schema)
		self.assertNotIn("valuation_rate", schema)
		self.assertLess(len(schema), 20000)

	# ---- guided creation of Lead and Issue --------------------------------------------------------
	def start_chat(self, user):
		frappe.set_user(user)
		self.session = chat.new_conversation()["session"]
		self.route = mock.patch(ROUTE, return_value={"mode": "chat", "reply": "ok"}).start()

	def say(self, text, plan=None):
		if plan is not None:
			self.route.return_value = plan
		return chat.send_message(session=self.session, text=text)

	def pending(self):
		return json.loads(frappe.db.get_value("AI Chat Session", self.session, "state") or "{}").get("pending")

	def click(self, value):
		return chat.send_message(session=self.session, action=json.dumps({"question_id": self.pending()["id"], "value": value, "label": value}))

	def test_lead_is_created_after_confirmation_and_is_described_as_saved_immediately(self):
		self.start_chat(self.users["A"])
		before = frappe.db.count("Lead")
		self.say("create lead", {"mode": "create", "doctype": "Lead", "values": {}, "items": []})
		self.assertEqual(self.pending()["key"], "first_name")
		self.say("Priya Sharma")
		self.assertEqual(self.pending()["key"], "company_name")
		self.say("AIB Test Industries")
		self.click("__skip__")  # email
		self.click("__skip__")  # mobile
		q = self.pending()
		self.assertEqual(q["kind"], "form_confirm")
		text = chat.get_session(self.session)["messages"][-1]
		self.assertIn("saved immediately", text["text"])
		self.assertNotIn("Draft", text["text"].replace("no Draft state", ""))
		self.assertEqual(frappe.db.count("Lead"), before)  # nothing exists before the confirmation
		r = self.click("__create__")
		card = next(m["card"] for m in r["messages"] if (m.get("card") or {}).get("type") == "created")
		self.assertEqual(card["doctype"], "Lead")
		lead = frappe.get_doc("Lead", card["name"])
		self.assertEqual((lead.first_name, lead.company_name, lead.docstatus), ("Priya Sharma", "AIB Test Industries", 0))
		self.assertEqual(frappe.db.count("Lead"), before + 1)
		# a second confirmation of the same form creates nothing more
		r = chat.send_message(session=self.session, action=json.dumps({"question_id": "stale", "value": "__create__"}))
		self.assertEqual(frappe.db.count("Lead"), before + 1)

	def test_issue_is_created_with_an_optional_customer(self):
		self.start_chat(self.users["A"])
		self.say("please raise a support ticket: the printer is on fire", {"mode": "create", "doctype": "Issue", "values": {"subject": "Printer is on fire"}, "items": []})
		self.assertEqual(self.pending()["key"], "customer")
		self.say("AIB Buyer One")
		self.click("__skip__")  # description
		r = self.click("__create__")
		card = next(m["card"] for m in r["messages"] if (m.get("card") or {}).get("type") == "created")
		issue = frappe.get_doc("Issue", card["name"])
		self.assertEqual((issue.subject, issue.customer), ("Printer is on fire", self.customers["AIB Buyer One"]))

	def test_model_supplied_lifecycle_values_are_ignored_when_creating_a_lead(self):
		self.start_chat(self.users["A"])
		# not a bare "create lead" (that is answered without the model): the model's plan is what is being checked
		self.say("create a lead called Evil Lead and mark it converted", {"mode": "create", "doctype": "Lead", "values": {"first_name": "Evil Lead", "docstatus": 1, "owner": "evil@example.com",
			"status": "Converted", "workflow_state": "Approved", "name": "FAKE-LEAD"}, "items": []})
		state = json.loads(frappe.db.get_value("AI Chat Session", self.session, "state"))
		self.assertEqual(set(state["forms"][0]["values"]), {"first_name"})

	def test_creation_requires_permission_and_names_the_available_types(self):
		frappe.set_user(self.users["NOROLES"])
		ctx = mock.MagicMock()
		ctx.st = {}
		from ai_erpnext.chat import forms

		forms.start(ctx, "Lead")
		self.assertIn("permission", str(ctx.say.call_args))
		self.assertEqual(frappe.db.count("Lead", {"first_name": "Evil Lead"}), 0)

	def test_lead_and_issue_are_in_the_creatable_list_and_help(self):
		from ai_erpnext.chat import capabilities, forms

		self.assertIn("Lead", forms.CREATABLE)
		self.assertIn("Issue", forms.CREATABLE)
		summary = capabilities.capability_summary()
		self.assertIn("Lead", summary)
		self.assertIn("look up (read-only)", summary)
		self.assertIn("never mixing currencies", summary)
		self.assertIn("create a new Lead", capabilities.footer("Lead"))

	# ---- page context (what the server accepts) ---------------------------------------------------
	def test_page_context_is_only_a_hint_and_rechecked(self):
		from ai_erpnext.chat import engine

		frappe.set_user(self.users["A"])
		own = self.invoices[("A", "AIB Buyer One")]
		other = self.invoices[("B", "AIB Buyer One")]
		self.assertEqual(engine._view_context({"route": ["Form", "Sales Invoice", own]}), ("Sales Invoice", own))
		self.assertIsNone(engine._view_context({"route": ["Form", "Sales Invoice", other]}))  # other company: ignored, not revealed
		self.assertIsNone(engine._view_context({"route": ["Form", "User", "Administrator"]}))  # not a queryable DocType
		self.assertIsNone(engine._view_context({"route": ["Form", "Sales Invoice", "new-sales-invoice-1"]}))
		self.assertIsNone(engine._view_context({"route": ["List", "Sales Invoice"]}))
		self.assertIsNone(engine._view_context("{not json"))
