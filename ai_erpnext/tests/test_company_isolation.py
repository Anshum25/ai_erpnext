"""Company isolation of the chat's read-only data queries.

Three test companies with their own invoices, and users whose User Permissions allow
Company A, Company B, both, or neither (only an unrelated Company C). Every query
operation must return exactly the records of the permitted companies, a company the user
names must be one of them, and a record of another company must not be revealed.
Everything is created inside the test transaction and rolled back.

Run: bench --site <site> run-tests --module ai_erpnext.tests.test_company_isolation
"""

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_days, nowdate

from ai_erpnext.chat import queries

COMPANIES = {"A": "AIC Alpha Co", "B": "AIC Beta Co", "C": "AIC Gamma Co"}
USERS = {"A": ["A"], "B": ["B"], "AB": ["A", "B"], "NONE": ["C"]}


def run(plan):
	return queries.execute(queries.validate(plan))


class TestCompanyIsolation(IntegrationTestCase):
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
						"abbr": f"AIC{key}",
						"default_currency": currency,
						"country": country,
						"create_chart_of_accounts_based_on": "Standard Template",
						"chart_of_accounts": "Standard",
					}
				).insert()
		group = frappe.db.get_value("Customer Group", {"is_group": 0}, "name")
		territory = frappe.db.get_value("Territory", {"is_group": 0}, "name")
		cls.customer = (
			frappe.get_doc(
				{
					"doctype": "Customer",
					"customer_name": "AIC Shared Buyer",
					"customer_group": group,
					"territory": territory,
				}
			)
			.insert()
			.name
		)
		ig = frappe.db.get_value("Item Group", {"is_group": 0}, "name")
		frappe.get_doc(
			{
				"doctype": "Item",
				"item_code": "AIC-ITEM",
				"item_name": "AIC Gadget",
				"item_group": ig,
				"stock_uom": "Nos",
				"is_stock_item": 0,
				"is_sales_item": 1,
			}
		).insert()
		today = nowdate()
		cls.invoices = {}
		for key in ("A", "B"):
			cls.invoices[key] = [
				cls.make_si(key, 100, submit=True, due=add_days(today, -3)),
				cls.make_si(key, 70, submit=False),
			]
		cls.users = {}
		for label, allowed in USERS.items():
			email = f"aic-{label.lower()}@example.com"
			if not frappe.db.exists("User", email):
				frappe.get_doc(
					{
						"doctype": "User",
						"email": email,
						"first_name": f"AIC {label}",
						"send_welcome_email": 0,
						"roles": [{"role": "Accounts User"}],
					}
				).insert(ignore_permissions=True)
			for key in allowed:
				frappe.get_doc(
					{
						"doctype": "User Permission",
						"user": email,
						"allow": "Company",
						"for_value": COMPANIES[key],
						"apply_to_all_doctypes": 1,
					}
				).insert(ignore_permissions=True)
			cls.users[label] = email
		frappe.cache.delete_value("user_permissions")

	@classmethod
	def make_si(cls, key, rate, submit, due=None):
		posting = add_days(nowdate(), -5)
		si = frappe.get_doc(
			{
				"doctype": "Sales Invoice",
				"customer": cls.customer,
				"company": COMPANIES[key],
				"posting_date": posting,
				"set_posting_time": 1,
				"due_date": due or posting,
				"items": [{"item_code": "AIC-ITEM", "qty": 1, "rate": rate}],
			}
		).insert()
		if submit:
			si.submit()
		return si.name

	def tearDown(self):
		frappe.set_user("Administrator")

	def ours(self, names):
		"""Only the test invoices (the site may hold other data)."""
		mine = {n for v in self.invoices.values() for n in v}
		return {n for n in names if n in mine}

	def expect(self, *keys):
		return {n for k in keys for n in self.invoices[k]}

	def list_names(self, **extra):
		plan = {
			"doctype": "Sales Invoice",
			"operation": "list",
			"filters": {"party": "AIC Shared"},
			"limit": 20,
			**extra,
		}
		return self.ours(run(plan)["names"])

	# ---------------------------------------------------------------------------------------
	def test_each_user_sees_only_permitted_companies(self):
		for label, keys in (("A", ["A"]), ("B", ["B"]), ("AB", ["A", "B"]), ("NONE", [])):
			frappe.set_user(self.users[label])
			self.assertEqual(self.list_names(), self.expect(*keys), label)
			submitted = self.ours(
				run(
					{
						"doctype": "Sales Invoice",
						"operation": "unpaid",
						"filters": {"party": "AIC Shared"},
						"limit": 20,
					}
				)["names"]
			)
			self.assertEqual(submitted, {self.invoices[k][0] for k in keys}, label)
			count = run(
				{"doctype": "Sales Invoice", "operation": "count", "filters": {"party": "AIC Shared"}}
			)["text"]
			self.assertIn(f" {len(keys) * 2} Sales Invoice", count, label)

	def test_totals_and_outstanding_are_isolated(self):
		for label, keys in (("A", ["A"]), ("AB", ["A", "B"]), ("NONE", [])):
			frappe.set_user(self.users[label])
			summary = run(
				{"doctype": "Sales Invoice", "operation": "sales_summary", "date": {"preset": "last_30_days"}}
			)
			companies = {row["company"] for row in (summary.get("card") or {}).get("rows", [])}
			self.assertEqual(companies & set(COMPANIES.values()), {COMPANIES[k] for k in keys}, label)
			outstanding = run(
				{"doctype": "Sales Invoice", "operation": "outstanding_by_customer", "limit": 20}
			)
			row = next(
				(r for r in (outstanding.get("card") or {}).get("rows", []) if r["_name"] == self.customer),
				None,
			)
			self.assertEqual(row["total"] if row else 0, 100 * len(keys), label)
			details = run({"doctype": "Customer", "operation": "details", "filters": {"name": self.customer}})
			value = {r["label"]: r["value"] for r in details["card"]["rows"]}[
				"Outstanding (submitted Sales Invoices)"
			]
			self.assertEqual(
				value,
				f"{100.0 * len(keys)} {self.currency} in {len(keys)} invoice(s)"
				if keys
				else "Nothing outstanding",
				label,
			)
			# the field answer ("outstanding amount of <customer>") is limited the same way
			answer = run(
				{
					"doctype": "Customer",
					"operation": "details",
					"filters": {"search": "AIC Shared Buyer"},
					"fields": ["outstanding"],
				}
			)["text"]
			if keys:
				self.assertIn(f"{100.0 * len(keys)} {self.currency} in {len(keys)} invoice(s)", answer, label)
			else:
				self.assertIn(
					"has no outstanding amount on submitted Sales Invoices you can access", answer, label
				)

	def test_other_companys_record_is_not_revealed(self):
		frappe.set_user(self.users["A"])
		other = self.invoices["B"][0]
		r = run({"doctype": "Sales Invoice", "operation": "details", "filters": {"name": other}})
		self.assertIn("couldn't find", r["text"])
		self.assertNotIn("card", r)
		mine = run(
			{"doctype": "Sales Invoice", "operation": "details", "filters": {"name": self.invoices["A"][0]}}
		)
		self.assertEqual(mine["card"]["name"], self.invoices["A"][0])

	def test_named_company_must_be_permitted(self):
		frappe.set_user(self.users["AB"])
		self.assertEqual(
			self.list_names(filters={"party": "AIC Shared", "company": COMPANIES["B"]}), self.expect("B")
		)
		self.assertEqual(
			self.list_names(filters={"party": "AIC Shared", "company": "AICA"}), self.expect("A")
		)  # by abbreviation
		for label, company in (
			("A", COMPANIES["B"]),
			("AB", COMPANIES["C"]),
			("NONE", COMPANIES["A"]),
			("A", "No Such Company Ltd"),
		):
			frappe.set_user(self.users[label])
			with self.assertRaises(queries.QueryError) as ctx:
				queries.validate(
					{"doctype": "Sales Invoice", "operation": "list", "filters": {"company": company}}
				)
			self.assertIn(
				"that you have access to", str(ctx.exception)
			)  # same answer: existence is not revealed

	def test_unrestricted_user_sees_all_companies(self):
		email = "aic-all@example.com"
		if not frappe.db.exists("User", email):
			frappe.get_doc(
				{
					"doctype": "User",
					"email": email,
					"first_name": "AIC All",
					"send_welcome_email": 0,
					"roles": [{"role": "Accounts User"}],
				}
			).insert(ignore_permissions=True)
		frappe.set_user(email)  # no User Permissions: role permissions decide, as in ERPNext itself
		self.assertEqual(self.list_names(), self.expect("A", "B"))

	def test_chat_end_to_end_uses_the_session_user(self):
		from unittest import mock

		from ai_erpnext.api import chat

		# The assistant is for System Managers (role of the chat DocTypes); User Permissions still restrict them.
		email = "aic-sm-b@example.com"
		if not frappe.db.exists("User", email):
			frappe.get_doc(
				{
					"doctype": "User",
					"email": email,
					"first_name": "AIC SM B",
					"send_welcome_email": 0,
					"roles": [{"role": "System Manager"}, {"role": "Accounts User"}],
				}
			).insert(ignore_permissions=True)
			frappe.get_doc(
				{
					"doctype": "User Permission",
					"user": email,
					"allow": "Company",
					"for_value": COMPANIES["B"],
					"apply_to_all_doctypes": 1,
				}
			).insert(ignore_permissions=True)
		frappe.set_user(email)
		session = chat.new_conversation()["session"]
		plan = {
			"mode": "query",
			"query": {
				"doctype": "Sales Invoice",
				"operation": "list",
				"filters": {"party": "AIC Shared"},
				"limit": 20,
			},
		}
		with mock.patch("ai_erpnext.chat.llm.route", return_value=plan):
			r = chat.send_message(session=session, text="Show Sales Invoices of AIC Shared Buyer")
		card = r["messages"][-1]["card"]
		self.assertEqual(self.ours(row["_name"] for row in card["rows"]), self.expect("B"))
