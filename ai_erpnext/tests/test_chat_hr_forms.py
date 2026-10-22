"""HR self-service through the opt-in "AI Assistant User" role (HRMS sites): Expense Claim Drafts, and the employee scope
of HR look-ups (capability.service.employee_scope).

The users here are NOT System Managers: an employee (role Employee), an expense approver and an HR User, each also given
the assistant role. Without an HR role a user only sees and files their own employee's records (plus, for an approver, the
records of the employees they approve), whatever ERPNext's role permissions would show. The class skips itself where HRMS
isn't installed; test_without_hrms simulates such a site. Everything is rolled back.

Run: bench --site <site> run-tests --module ai_erpnext.tests.test_chat_hr_forms
"""

import json
import unittest
from unittest import mock

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import nowdate

from ai_erpnext.api import chat
from ai_erpnext.capability import service as C
from ai_erpnext.chat import capabilities, forms, hr, queries

ROUTE = "ai_erpnext.chat.llm.route"
ROLE = "AI Assistant User"
USERS = {
	"emp": ("aihf-emp@example.com", [ROLE, "Employee"]),
	"other": ("aihf-other@example.com", [ROLE, "Employee"]),
	"approver": ("aihf-approver@example.com", [ROLE, "Employee", "Expense Approver"]),
	"hr": ("aihf-hr@example.com", [ROLE, "HR User"]),
}


def run(plan):
	return queries.execute(queries.validate(plan))


class TestHRSelfService(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		frappe.set_user("Administrator")
		if not hr.installed():
			raise unittest.SkipTest("HRMS is not installed on this site")
		super().setUpClass()
		cls.company = frappe.db.get_single_value("Global Defaults", "default_company")
		cls.currency = frappe.get_cached_value("Company", cls.company, "default_currency")
		account = frappe.get_all(
			"Account",
			filters={
				"company": cls.company,
				"is_group": 0,
				"root_type": "Expense",
				"account_currency": cls.currency,
			},
			pluck="name",
			limit=1,
		)[0]
		for claim_type in ("Travel", "Food"):
			doc = frappe.get_doc("Expense Claim Type", claim_type)
			if not any(a.company == cls.company for a in doc.accounts):
				doc.append("accounts", {"company": cls.company, "default_account": account})
				doc.save()
		cls.users = {}
		for key, (email, roles) in USERS.items():
			if not frappe.db.exists("User", email):
				frappe.get_doc(
					{
						"doctype": "User",
						"email": email,
						"first_name": f"AIHF {key}",
						"send_welcome_email": 0,
						"roles": [{"role": r} for r in roles],
					}
				).insert(ignore_permissions=True)
			cls.users[key] = email
		cls.e_self = cls.employee("AIHF Asha", "Menon", cls.users["emp"])
		cls.e_other = cls.employee("AIHF Rohan", "Das", cls.users["other"])
		cls.e_report = cls.employee("AIHF Vikram", "Shah", None, expense_approver=cls.users["approver"])
		cls.claims = {
			e: cls.claim(e, amount) for e, amount in ((cls.e_self, 40), (cls.e_other, 50), (cls.e_report, 60))
		}
		frappe.cache.delete_value("user_permissions")

	@classmethod
	def employee(cls, first, last, user, expense_approver=None):
		return (
			frappe.get_doc(
				{
					"doctype": "Employee",
					"first_name": first,
					"last_name": last,
					"gender": "Female",
					"date_of_birth": "1991-06-01",
					"date_of_joining": "2023-01-01",
					"company": cls.company,
					"status": "Active",
					"user_id": user,
					"expense_approver": expense_approver,
				}
			)
			.insert()
			.name
		)

	@classmethod
	def claim(cls, employee, amount):
		return (
			frappe.get_doc(
				{
					"doctype": "Expense Claim",
					"employee": employee,
					"company": cls.company,
					"posting_date": nowdate(),
					"expenses": [{"expense_date": nowdate(), "expense_type": "Travel", "amount": amount}],
				}
			)
			.insert()
			.name
		)

	def setUp(self):
		# IntegrationTestCase rolls back per class: each test also gets its own savepoint (what it creates is undone after it)
		frappe.set_user("Administrator")
		frappe.db.savepoint("aihf_test")
		self.addCleanup(frappe.db.rollback, save_point="aihf_test")
		self.route = mock.patch(ROUTE, return_value={"mode": "chat", "reply": "ok"}).start()
		mock.patch("frappe.enqueue").start()
		self.addCleanup(mock.patch.stopall)
		self.addCleanup(frappe.set_user, "Administrator")

	def as_user(self, key):
		frappe.set_user(self.users[key])
		self.s = chat.new_conversation()["session"]

	def say(self, text, plan=None):
		if plan is not None:
			self.route.return_value = plan
		return chat.send_message(session=self.s, text=text)

	def pending(self):
		return json.loads(frappe.db.get_value("AI Chat Session", self.s, "state") or "{}").get("pending")

	def click(self, value):
		q = self.pending()
		return chat.send_message(
			session=self.s, action=json.dumps({"question_id": q["id"], "value": value, "label": value})
		)

	def texts(self, n=4):
		return " ".join(m.get("text") or "" for m in chat.get_session(self.s)["messages"][-n:])

	def listed(self, doctype):
		return set(run({"doctype": doctype, "operation": "list", "limit": 20})["names"])

	# --- Expense Claim Drafts ----------------------------------------------------------------------
	def test_an_employee_drafts_their_own_expense_claim(self):
		self.as_user("emp")
		self.assertFalse("System Manager" in frappe.get_roles())
		before = frappe.db.count("Expense Claim")
		self.say("create expense claim", {"mode": "create", "doctype": "Expense Claim"})
		self.assertEqual(
			self.pending()["key"], "row__expense_date"
		)  # employee (their own) and date are defaults
		for value in ("today", "Travel", "250", "Taxi to the client"):
			self.say(value)
		self.click("__more__")
		for value in ("today", "Food", "-5"):
			self.say(value)
		self.assertIn("must be greater than 0", self.texts())
		self.say("80")
		self.click("__skip__")
		self.click("__done__")
		self.assertEqual(self.pending()["kind"], "form_confirm")
		self.assertEqual(frappe.db.count("Expense Claim"), before)
		card = next(m["card"] for m in reversed(chat.get_session(self.s)["messages"]) if m.get("card"))
		labels = {r["label"]: r["value"] for r in card["rows"]}
		self.assertEqual(labels["Total claimed"], f"330.0 {self.currency}")
		self.assertIn("Taxi to the client", labels["Expense 1"])
		r = self.click("__create__")
		created = next(m["card"] for m in r["messages"] if (m.get("card") or {}).get("type") == "created")
		doc = frappe.get_doc("Expense Claim", created["name"])
		self.assertEqual(
			(doc.docstatus, doc.employee, doc.approval_status, doc.total_claimed_amount, doc.company),
			(0, self.e_self, "Draft", 330, self.company),
		)
		self.assertEqual([(e.expense_type, e.amount) for e in doc.expenses], [("Travel", 250), ("Food", 80)])
		self.assertFalse(any(e.sanctioned_amount for e in doc.expenses))  # sanctioning is the approver's

	def test_without_an_hr_role_a_claim_can_only_be_for_oneself(self):
		self.as_user("emp")
		self.say("create expense claim", {"mode": "create", "doctype": "Expense Claim"})
		self.say("change employee to AIHF Rohan Das")
		self.assertIn("couldn't find", self.texts())
		state = json.loads(frappe.db.get_value("AI Chat Session", self.s, "state"))
		self.assertEqual(state["forms"][-1]["values"]["employee"], self.e_self)

	def test_an_hr_user_may_file_for_another_employee(self):
		self.as_user("hr")
		self.say("create expense claim", {"mode": "create", "doctype": "Expense Claim"})
		self.assertEqual(self.pending()["key"], "employee")  # no own employee: asked
		self.say("AIHF Rohan Das")
		self.assertEqual(self.pending()["key"], "row__expense_date")

	# --- employee scope of HR look-ups ---------------------------------------------------------------
	def test_an_employee_lists_only_their_own_claims(self):
		frappe.set_user(self.users["emp"])
		ours = set(self.claims.values())
		self.assertEqual(self.listed("Expense Claim") & ours, {self.claims[self.e_self]})
		count = run({"doctype": "Expense Claim", "operation": "count"})["text"]
		self.assertIn("There is 1 ", count)
		other = run(
			{
				"doctype": "Expense Claim",
				"operation": "details",
				"filters": {"name": self.claims[self.e_other]},
			}
		)
		missing = run(
			{"doctype": "Expense Claim", "operation": "details", "filters": {"name": "HR-EXP-0000-99999"}}
		)
		self.assertTrue(
			other["not_found"]
		)  # the same answer as a record that doesn't exist: nothing is revealed
		self.assertEqual(
			other["text"].replace(self.claims[self.e_other], "X"),
			missing["text"].replace("HR-EXP-0000-99999", "X"),
		)
		self.assertNotIn("Rohan", json.dumps(other))

	def test_an_approver_also_sees_the_claims_they_approve(self):
		frappe.set_user(self.users["approver"])
		ours = set(self.claims.values())
		self.assertEqual(self.listed("Expense Claim") & ours, {self.claims[self.e_report]})

	def test_an_hr_user_sees_every_permitted_claim(self):
		frappe.set_user(self.users["hr"])
		self.assertEqual(self.listed("Expense Claim") & set(self.claims.values()), set(self.claims.values()))

	def test_the_scope_applies_to_aggregates_too(self):
		frappe.set_user(self.users["emp"])
		total = run(
			{
				"doctype": "Expense Claim",
				"operation": "aggregate",
				"aggregate": {"fn": "sum", "field": "total_claimed_amount"},
			}
		)
		values = [str(c) for row in total["card"]["rows"] for c in row.values()]
		self.assertIn("40.0", values, total)  # only the employee's own claim (40), not all three (150)
		self.assertNotIn("150.0", values)

	# --- sites without HRMS ----------------------------------------------------------------------------
	def test_without_hrms_the_new_hr_capabilities_say_so(self):
		with mock.patch.object(hr, "installed", return_value=False):
			for dt in ("Expense Claim", "Leave Application", "Attendance"):
				self.assertFalse(C.is_queryable(dt), dt)
				with self.assertRaises(queries.QueryError) as e:
					queries.validate({"doctype": dt, "operation": "list"})
				self.assertIn("HRMS app, which isn't installed", str(e.exception))
				self.assertNotIn(dt, capabilities.capability_summary())
			self.assertNotIn("Expense Claim", forms.creatable())
			described = capabilities.describe("Expense Claim")
			self.assertIsNone(described["query"])
			self.assertIsNone(described["create"])
			self.assertIn("HRMS", described["unavailable"])
