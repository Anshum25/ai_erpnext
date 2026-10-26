"""HRMS in the chat: employee look-ups, leave balances, attendance and Leave Application drafts.

Real HRMS records are created inside the test transaction (rolled back): two companies with a holiday list, a leave type
with an allocation of 10 days, approved / open / rejected leave applications, and submitted and draft attendance.
Users: an employee linked to their own Employee record (HRMS gives them a User Permission for it), another employee,
their leave approver, an HR User restricted to Company A, and a user with no Employee record. The router model is
mocked; the phrasings below are routed without it.

Skipped on a site without HRMS. Run: bench --site <site> run-tests --module ai_erpnext.tests.test_chat_hr
"""

import json
import unittest
from unittest import mock

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_days, getdate, nowdate

from ai_erpnext.api import chat
from ai_erpnext.chat import capabilities, forms, hr, queries, shortcuts

ROUTE = "ai_erpnext.chat.llm.route"
CO = {"A": "AIH Alpha Co", "B": "AIH Beta Co"}
ANNUAL = "AIH Annual Leave"
# The chat itself (AI Chat Session) is for System Managers; HR data access comes from the HR roles and User Permissions.
USERS = {
	"emp": ("aih-emp@example.com", ["System Manager", "Employee"]),
	"other": ("aih-other@example.com", ["System Manager", "Employee"]),
	"approver": ("aih-approver@example.com", ["System Manager", "Employee", "Leave Approver"]),
	"hr": ("aih-hr@example.com", ["System Manager", "HR User"]),
	"nolink": ("aih-nolink@example.com", ["System Manager", "Employee"]),
}


def run(plan):
	return queries.execute(queries.validate(plan))


def days(n):
	return str(add_days(nowdate(), n))


class TestChatHR(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		frappe.set_user("Administrator")
		if not hr.installed():
			raise unittest.SkipTest("HRMS is not installed on this site")
		super().setUpClass()
		for key, value in {
			"leave_approver_mandatory_in_leave_application": 0,
			"restrict_backdated_leave_application": 0,
			"prevent_self_leave_approval": 0,
			"send_leave_notification": 0,
		}.items():
			frappe.db.set_single_value("HR Settings", key, value)
		today = getdate(nowdate())
		cls.start, cls.end = getdate(f"{today.year - 1}-01-01"), getdate(f"{today.year + 1}-12-31")
		currency = frappe.db.get_value("Company", {}, "default_currency") or "INR"
		country = frappe.db.get_single_value("System Settings", "country") or "India"
		for key, name in CO.items():
			if not frappe.db.exists("Company", name):
				frappe.get_doc(
					{
						"doctype": "Company",
						"company_name": name,
						"abbr": f"AIH{key}",
						"default_currency": currency,
						"country": country,
						"create_chart_of_accounts_based_on": "Standard Template",
						"chart_of_accounts": "Standard",
					}
				).insert()
		holidays = frappe.get_doc(
			{
				"doctype": "Holiday List",
				"holiday_list_name": "AIH Holidays",
				"from_date": cls.start,
				"to_date": cls.end,
			}
		).insert()
		for company in CO.values():
			frappe.get_doc(
				{
					"doctype": "Holiday List Assignment",
					"applicable_for": "Company",
					"assigned_to": company,
					"holiday_list": holidays.name,
					"from_date": cls.start,
				}
			).insert().submit()
		cls.users = {}
		for key, (email, roles) in USERS.items():
			if not frappe.db.exists("User", email):
				frappe.get_doc(
					{
						"doctype": "User",
						"email": email,
						"first_name": f"AIH {key}",
						"send_welcome_email": 0,
						"roles": [{"role": r} for r in roles],
					}
				).insert(ignore_permissions=True)
			cls.users[key] = email
		frappe.get_doc(
			{
				"doctype": "User Permission",
				"user": cls.users["hr"],
				"allow": "Company",
				"for_value": CO["A"],
				"apply_to_all_doctypes": 1,
			}
		).insert(ignore_permissions=True)
		frappe.get_doc({"doctype": "Leave Type", "leave_type_name": ANNUAL, "max_leaves_allowed": 0}).insert()

		cls.e1 = cls.employee("AIH Anshum", "Sharma", CO["A"], cls.users["emp"], cls.users["approver"])
		cls.e2 = cls.employee("AIH Anshum", "Verma", CO["A"])
		cls.e3 = cls.employee("AIH Priya", "Nair", CO["B"])
		cls.e4 = cls.employee("AIH Ravi", "Kumar", CO["A"], cls.users["other"])
		cls.e_approver = cls.employee("AIH Approver", "One", CO["A"], cls.users["approver"])
		for emp in (cls.e1, cls.e3):
			frappe.get_doc(
				{
					"doctype": "Leave Allocation",
					"employee": emp,
					"leave_type": ANNUAL,
					"from_date": cls.start,
					"to_date": cls.end,
					"new_leaves_allocated": 10,
				}
			).insert().submit()
		cls.leave(cls.e1, days(-60), days(-59), "Approved", submit=True)  # 2 days taken
		cls.leave(cls.e1, days(-40), days(-38), "Open", submit=False)  # 3 days pending: not deducted
		cls.leave(cls.e1, days(-20), days(-20), "Rejected", submit=True)  # never counts
		cls.attendance(cls.e1, days(-10), "Absent")
		cls.attendance(cls.e1, days(-9), "Present")
		cls.attendance(cls.e1, days(-8), "Present", submit=False)  # a draft: not counted
		cls.attendance(cls.e1, nowdate(), "Present")
		cls.attendance(cls.e3, nowdate(), "Present")
		frappe.cache.delete_value("user_permissions")

	@classmethod
	def employee(cls, first, last, company, user=None, approver=None):
		return (
			frappe.get_doc(
				{
					"doctype": "Employee",
					"first_name": first,
					"last_name": last,
					"gender": "Male",
					"date_of_birth": "1990-01-01",
					"date_of_joining": add_days(cls.start, -365),
					"company": company,
					"status": "Active",
					"user_id": user,
					"leave_approver": approver,
				}
			)
			.insert()
			.name
		)

	@classmethod
	def leave(cls, employee, from_date, to_date, status, submit):
		doc = frappe.get_doc(
			{
				"doctype": "Leave Application",
				"employee": employee,
				"leave_type": ANNUAL,
				"from_date": from_date,
				"to_date": to_date,
				"status": status,
			}
		).insert()
		if submit:
			doc.submit()
		return doc.name

	@classmethod
	def attendance(cls, employee, date, status, submit=True):
		doc = frappe.get_doc(
			{"doctype": "Attendance", "employee": employee, "attendance_date": date, "status": status}
		).insert()
		if submit:
			doc.submit()

	def setUp(self):
		# each test starts from the class fixtures: what a test creates (drafts, sessions) is rolled back after it
		frappe.set_user("Administrator")
		frappe.db.savepoint("aih_test")
		self.addCleanup(frappe.db.rollback, save_point="aih_test")
		self.as_user("Administrator")
		self.route = mock.patch(ROUTE, return_value={"mode": "chat", "reply": "ok"}).start()
		mock.patch("frappe.enqueue").start()
		self.addCleanup(mock.patch.stopall)
		self.addCleanup(frappe.set_user, "Administrator")

	# helpers ------------------------------------------------------------------------------------
	def as_user(self, key):
		frappe.set_user(self.users[key] if key in getattr(self, "users", {}) else key)
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

	@staticmethod
	def texts(r):
		return " ".join(m.get("text") or "" for m in r["messages"] if m["role"] == "assistant")

	@staticmethod
	def card(r, kind):
		return next((m["card"] for m in r["messages"] if (m.get("card") or {}).get("type") == kind), None)

	def balance_row(self, r, leave_type=ANNUAL):
		return next(row for row in self.card(r, "table")["rows"] if row["leave_type"] == leave_type)

	def leave_count(self):
		return frappe.db.count("Leave Application", {"employee": ["in", [self.e1, self.e2, self.e4]]})

	# 1. routing --------------------------------------------------------------------------------
	def test_hr_phrasings_route_to_read_only_operations(self):
		cases = {
			"leave balance for anshum named employee": ("leave_balance", {"search": "anshum"}),
			"Show my leave balance": ("leave_balance", {"self": True}),
			"Leave balance for Anshum": ("leave_balance", {"search": "Anshum"}),
			"How many annual leaves are remaining?": (
				"leave_balance",
				{"self": True, "leave_type": "annual"},
			),
			"Show my attendance for this month": ("attendance", {"self": True}),
			"Show attendance for Anshum for October": ("attendance", {"search": "Anshum"}),
			"Show my employee details": ("details", {"self": True}),
			"Find employee Anshum": ("details", {"search": "Anshum"}),
			"Show employee details for Anshum": ("details", {"search": "Anshum"}),
			"Who is employee EMP-0001?": ("details", {"search": "EMP-0001"}),
		}
		for text, (operation, filters) in cases.items():
			plan = shortcuts.match(text)
			self.assertEqual(plan["mode"], "query", text)
			self.assertEqual(
				(plan["query"]["doctype"], plan["query"]["operation"], plan["query"]["filters"]),
				("Employee", operation, filters),
				text,
			)
		self.assertEqual(
			shortcuts.match("Show my leave balance for this year")["query"]["date"], {"preset": "this_year"}
		)
		october = shortcuts.match("Show attendance for Anshum for October")["query"]["date"]
		self.assertEqual((october["from"][5:], october["to"][5:]), ("10-01", "10-31"))

	def test_the_reported_refusals_are_gone(self):
		self.as_user("hr")
		r = self.say("leave balance for AIH Anshum Sharma named employee")
		self.route.assert_not_called()
		self.assertNotIn("I can't show", self.texts(r))
		self.assertEqual(self.balance_row(r)["remaining"], 8)
		r = self.say("create leave application")
		self.route.assert_not_called()
		self.assertNotIn("I can't create", self.texts(r))
		self.assertEqual((self.pending()["kind"], self.pending()["key"]), ("form_field", "employee"))

	def test_a_model_plan_asking_for_a_leave_balance_field_becomes_a_leave_balance(self):
		# the plan the router produced before this change ("leave balance" as an Employee field)
		self.as_user("hr")
		r = self.say(
			"what's the leave situation of AIH Anshum Sharma",
			{
				"mode": "query",
				"query": {
					"doctype": "Employee",
					"operation": "details",
					"filters": {"search": "AIH Anshum Sharma"},
					"fields": ["leave balance"],
				},
			},
		)
		self.assertEqual(self.balance_row(r)["remaining"], 8)

	# 2. employees ------------------------------------------------------------------------------
	def test_my_employee_details_and_lookup_by_id(self):
		self.as_user("emp")
		r = self.say("Show my employee details")
		card = self.card(r, "record")
		self.assertEqual((card["doctype"], card["name"]), ("Employee", self.e1))
		self.assertNotIn("date_of_birth", json.dumps(card))  # organisational fields only
		r = self.say(f"Who is employee {self.e1}?")
		self.assertEqual(self.card(r, "record")["name"], self.e1)
		self.as_user("hr")  # may read employees, but no Employee record is linked to this user
		r = self.say("show my leave balance")
		self.assertIn("isn't linked to an Employee record", self.texts(r))
		self.as_user("nolink")  # ERPNext removes the Employee role from users without an Employee record
		r = self.say("show my employee details")
		self.assertIn("You don't have permission to view Employee records", self.texts(r))

	def test_an_ambiguous_employee_name_is_asked_never_picked(self):
		self.as_user("hr")
		r = self.say("find employee AIH Anshum")
		self.assertIsNone(self.card(r, "record"))
		q = self.pending()
		self.assertEqual(q["kind"], "query_pick")
		self.assertEqual({o["value"] for o in q["options"]}, {f"Employee::{self.e1}", f"Employee::{self.e2}"})
		r = self.click(f"Employee::{self.e2}")
		self.assertEqual(self.card(r, "record")["name"], self.e2)
		# the same for a leave balance: the chosen employee's figures, nothing before the choice
		r = self.say("leave balance for AIH Anshum")
		self.assertIsNone(self.card(r, "table"))
		self.assertEqual(self.pending()["kind"], "query_pick")
		r = self.click(f"Employee::{self.e1}")
		self.assertEqual(self.balance_row(r)["remaining"], 8)

	# 3-6. leave balance ------------------------------------------------------------------------
	def test_leave_balance_follows_hrms_and_excludes_pending_and_rejected(self):
		from hrms.hr.doctype.leave_application.leave_application import get_leave_details

		expected = get_leave_details(self.e1, nowdate())["leave_allocation"][ANNUAL]
		self.as_user("emp")
		r = self.say("show my leave balance")
		row = self.balance_row(r)
		self.assertEqual((row["allocated"], row["taken"], row["pending"], row["remaining"]), (10, 2, 3, 8))
		self.assertEqual(row["remaining"], expected["remaining_leaves"])  # HRMS's own figures
		self.assertEqual(row["period"], f"{self.start} to {self.end}")
		self.assertIn("not deducted", self.card(r, "table")["note"])

	def test_leave_type_and_period(self):
		self.as_user("emp")
		r = self.say("How many annual leaves are remaining?")
		self.assertIn(f"8 day(s) of {ANNUAL} remaining as on {nowdate()}", self.texts(r))
		self.assertIn("3 more day(s) are pending approval", self.texts(r))
		self.assertEqual([row["leave_type"] for row in self.card(r, "table")["rows"]], [ANNUAL])
		r = self.say("leave balance for last year")
		self.assertIn(f"as on {getdate(nowdate()).year - 1}-12-31", self.texts(r))
		r = self.say("unicorn leave balance")
		self.assertIn("There's no leave type matching “unicorn”", self.texts(r))
		with self.assertRaises(queries.QueryError):
			run(
				{
					"doctype": "Employee",
					"operation": "leave_balance",
					"date": {"from": "2026-01-01", "to": "2028-01-01"},
				}
			)

	def test_no_applicable_allocation(self):
		self.as_user("emp")
		r = self.say("sick leave balance")
		self.assertIn("has no Sick Leave allocation covering", self.texts(r))
		self.assertIsNone(self.card(r, "table"))
		two_years_ago = getdate(nowdate()).year - 2
		result = run(
			{
				"doctype": "Employee",
				"operation": "leave_balance",
				"filters": {"self": True},
				"date": {"from": f"{two_years_ago}-03-01", "to": f"{two_years_ago}-03-31"},
			}
		)
		self.assertIn("has no leave allocations covering", result["text"])
		self.as_user("hr")
		r = self.say("leave balance for AIH Anshum Verma")
		self.assertIn("has no leave allocations covering", self.texts(r))

	# 12-14. permissions and attendance ---------------------------------------------------------
	def test_another_employee_cannot_see_leave_or_attendance(self):
		self.as_user("other")
		r = self.say("leave balance for AIH Anshum Sharma")
		self.assertIn("couldn't find an employee matching", self.texts(r))
		self.assertIsNone(self.card(r, "table"))
		r = self.say("attendance of AIH Anshum Sharma for this month")
		self.assertIn("couldn't find an employee matching", self.texts(r))
		# forged picks: an employee the user can't read is not found; a record of another DocType is refused
		plan = queries.validate({"doctype": "Employee", "operation": "leave_balance"})
		self.assertTrue(hr.leave_balance(dict(plan, pick=["Employee", self.e1])).get("not_found"))
		with self.assertRaises(queries.QueryError):
			hr.leave_balance(dict(plan, pick=["Customer", self.e1]))
		r = self.say("show my leave balance")  # their own employee has no allocation: no one else's figures
		self.assertIn("AIH Ravi Kumar", self.texts(r))
		self.assertNotIn("Anshum", self.texts(r))

	def test_company_restrictions_apply(self):
		self.as_user("hr")  # HR User limited to Company A
		for text in (
			"leave balance for AIH Priya Nair",
			"attendance of AIH Priya Nair for this month",
			"find employee AIH Priya Nair",
		):
			r = self.say(text)
			self.assertIn("couldn't find an employee matching", self.texts(r), text)
		self.as_user("Administrator")
		r = self.say("leave balance for AIH Priya Nair")
		self.assertEqual(self.balance_row(r)["remaining"], 10)

	def test_attendance_period_and_drafts(self):
		self.as_user("emp")
		result = run(
			{
				"doctype": "Employee",
				"operation": "attendance",
				"filters": {"self": True},
				"date": {"from": days(-10), "to": days(-8)},
			}
		)
		self.assertIn("1 Absent, 1 Present", result["text"])
		self.assertIn("1 draft attendance record(s)", result["text"])
		self.assertEqual([row["attendance_date"] for row in result["card"]["rows"]], [days(-10), days(-9)])
		r = self.say("Show my attendance for this month")
		self.assertIn(nowdate(), [row["attendance_date"] for row in self.card(r, "table")["rows"]])
		self.assertTrue(
			all(row["attendance_date"][:7] == nowdate()[:7] for row in self.card(r, "table")["rows"])
		)
		r = self.say("attendance of AIH Priya Nair for this month")  # another company's employee
		self.assertIn("couldn't find an employee matching", self.texts(r))

	# 7-11. Leave Application drafts ------------------------------------------------------------
	def test_guided_leave_application_is_a_draft_created_only_after_confirmation(self):
		self.as_user("emp")
		before = self.leave_count()
		self.say("create leave application")
		q = self.pending()
		self.assertEqual(
			(q["kind"], q["key"]), ("form_field", "leave_type")
		)  # the employee is the user's own
		self.say(ANNUAL)
		self.assertEqual(self.pending()["key"], "from_date")
		self.say(days(7))
		q = self.pending()
		self.assertEqual(q["key"], "to_date")
		self.assertIn(days(7), [o["value"] for o in q["options"]])
		self.say(days(8))
		self.assertEqual(self.pending()["key"], "description")
		r = self.click("__skip__")
		self.assertEqual(self.pending()["kind"], "form_confirm")
		preview = {row["label"]: row["value"] for row in self.card(r, "form_preview")["rows"]}
		self.assertEqual(preview["Employee"], f"AIH Anshum Sharma ({self.e1})")
		self.assertEqual(
			(preview["Leave days (HRMS)"], preview["Balance before this application"]), ("2", "8")
		)
		self.assertIn("not be submitted or approved", self.texts(r))
		self.assertEqual(self.leave_count(), before)  # nothing before the confirmation
		r = self.click("__create__")
		card = self.card(r, "created")
		self.assertEqual(card["doctype"], "Leave Application")
		doc = frappe.get_doc("Leave Application", card["name"])
		self.assertEqual((doc.docstatus, doc.status, doc.employee), (0, "Open", self.e1))
		self.assertEqual((doc.from_date, doc.to_date), (getdate(days(7)), getdate(days(8))))
		self.assertEqual((doc.owner, doc.leave_approver), (self.users["emp"], self.users["approver"]))
		self.assertEqual(self.leave_count(), before + 1)
		self.assertIn("hasn't been submitted or approved", self.texts(r))

	def test_prefilled_request_never_submits_or_approves(self):
		self.as_user("hr")
		r = self.say(
			"Create leave application for AIH Anshum Sharma from next week",
			{
				"mode": "create",
				"doctype": "Leave Application",
				"values": {
					"employee": "AIH Anshum Sharma",
					"leave_type": ANNUAL,
					"from_date": days(14),
					"to_date": days(15),
					"status": "Approved",
					"docstatus": 1,
					"description": "family event",
				},
			},
		)
		self.assertEqual(self.pending()["kind"], "form_confirm")
		r = self.click("__create__")
		doc = frappe.get_doc("Leave Application", self.card(r, "created")["name"])
		self.assertEqual((doc.docstatus, doc.status, doc.description), (0, "Open", "family event"))

	def test_missing_fields_are_asked_and_a_duration_is_offered(self):
		self.as_user("emp")
		self.say(
			"apply for two days of annual leave",
			{"mode": "create", "doctype": "Leave Application", "values": {"leave_type": "annual", "days": 2}},
		)
		q = self.pending()
		self.assertEqual(q["key"], "leave_type")  # "annual" is only a partial name: offered, not assumed
		self.assertEqual([o["value"] for o in q["options"]], [ANNUAL])
		self.click(ANNUAL)
		self.assertEqual(self.pending()["key"], "from_date")
		self.say(days(30))
		q = self.pending()
		self.assertEqual(q["options"][0]["value"], days(31))
		self.assertIn("2 calendar days", q["options"][0]["label"])

	def test_hrms_validation_errors_are_explained_and_correctable(self):
		self.as_user("emp")
		before = self.leave_count()
		plan = {
			"mode": "create",
			"doctype": "Leave Application",
			"values": {
				"leave_type": ANNUAL,
				"from_date": days(20),
				"to_date": days(40),
				"description": "trip",
			},
		}
		r = self.say("apply for annual leave for three weeks", plan)
		self.assertIn("HRMS didn't accept the Leave Application", self.texts(r))
		self.assertIn("Insufficient leave balance", self.texts(r))
		self.assertEqual(self.pending()["kind"], "form_confirm")
		r = self.say(f"change to date to {days(21)}")
		self.assertEqual(self.pending()["kind"], "form_confirm")
		self.assertIsNotNone(self.card(r, "form_preview"))
		self.assertEqual(self.leave_count(), before)
		# overlapping the pending application
		self.say("cancel")
		plan["values"].update(from_date=days(-39), to_date=days(-39))
		r = self.say("apply for annual leave", plan)
		self.assertIn("HRMS didn't accept", self.texts(r))
		self.assertIn("already applied", self.texts(r))
		self.assertEqual(self.leave_count(), before)

	def test_ambiguous_or_unauthorised_employee_in_the_form(self):
		self.as_user("hr")
		self.say(
			"create leave application for AIH Anshum",
			{"mode": "create", "doctype": "Leave Application", "values": {"employee": "AIH Anshum"}},
		)
		q = self.pending()
		self.assertEqual(q["key"], "employee")
		self.assertEqual({o["value"] for o in q["options"]}, {self.e1, self.e2})
		self.as_user("other")  # an employee can't apply for someone else, by name or by a forged button value
		self.say(
			"create leave application for AIH Anshum Sharma",
			{"mode": "create", "doctype": "Leave Application", "values": {"employee": "AIH Anshum Sharma"}},
		)
		q = self.pending()
		self.assertEqual(q["key"], "employee")
		self.assertNotIn(self.e1, [o["value"] for o in q["options"]])
		self.click(self.e1)
		self.assertEqual(self.pending()["key"], "employee")

	# 15. regressions and HRMS-less sites ---------------------------------------------------------
	def test_existing_workflows_and_capabilities_are_kept(self):
		r = self.say("create purchase invoice")
		self.assertEqual(self.pending()["key"], "supplier")
		self.assertIn("Purchase Invoice", self.texts(r))
		self.say("cancel")
		summary = capabilities.capability_summary()
		for dt in ("Sales Invoice", "Purchase Order", "Customer", "Item", "Leave Application"):
			self.assertIn(dt, summary)
		self.assertIn("leave balances and attendance", summary)
		self.assertIn("create a Draft Leave Application", capabilities.answer("what is a leave application"))
		r = self.say("help")
		self.assertIn("apply for leave", self.texts(r))

	def test_without_hrms_the_chat_says_so(self):
		with mock.patch.object(hr, "installed", return_value=False):
			self.assertNotIn("Leave Application", forms.creatable())
			with self.assertRaises(queries.QueryError) as e:
				run({"doctype": "Employee", "operation": "leave_balance"})
			self.assertIn("HRMS", str(e.exception))
			r = self.say("create leave application")
			self.assertIn("isn't installed", self.texts(r))
			self.assertIsNone(self.pending())
			self.assertNotIn("Leave Application", capabilities.capability_summary())

	# 16. manual QA 2026-10-26: "how many leave balance i have?" -------------------------------------
	def test_reported_phrasing_resolves_the_logged_in_users_own_employee(self):
		self.assertEqual(
			shortcuts.match("how many leave balance i have?")["query"],
			{"doctype": "Employee", "operation": "leave_balance", "filters": {"self": True}},
		)
		self.as_user("emp")
		r = self.say("how many leave balance i have?")
		self.route.assert_not_called()  # deterministic: no AI call decides whose balance this is
		self.assertEqual(self.balance_row(r)["remaining"], 8)
		self.assertIn(self.e1, json.dumps(r["messages"]))

	def test_missing_employee_mapping_explains_the_fix_and_never_falls_back(self):
		from hrms.hr.doctype.leave_application import leave_application

		self.as_user("hr")  # may read employees, but no Employee record has this user in User ID
		with mock.patch.object(leave_application, "get_leave_details") as details:
			r = self.say("how many leave balance i have?")
		details.assert_not_called()  # no balance of any other employee is looked up
		text = self.texts(r)
		self.assertIn(f"Your user ({self.users['hr']}) isn't linked to an Employee record", text)
		self.assertIn("“User ID” field of your Employee record", text)
		self.assertIn("leave balance for <employee name or ID>", text)  # the follow-up this user can use
		self.assertIsNone(self.card(r, "table"))
		r = self.say(
			f"leave balance for {self.e1}"
		)  # the explicit follow-up works with the user's own permissions
		self.assertEqual(self.balance_row(r)["remaining"], 8)

	def test_linked_but_unreadable_employee_is_reported_without_bypassing_permissions(self):
		from hrms.hr.doctype.leave_application import leave_application

		self.as_user("emp")  # linked to e1; the permission-aware lookup is made to find nothing
		with (
			mock.patch.object(hr, "my_employees", return_value=[]),
			mock.patch.object(leave_application, "get_leave_details") as details,
		):
			r = self.say("show my leave balance")
		details.assert_not_called()
		self.assertIn("linked to an Employee record, but you don't have permission to read it", self.texts(r))
		self.assertIsNone(self.card(r, "table"))
		self.assertNotIn(self.e1, json.dumps(r["messages"]))  # not even the ID of the unreadable record

	def test_missing_allocation_or_leave_types_is_not_a_zero_balance(self):
		self.as_user("other")  # e4: an employee without any Leave Allocation
		text = self.texts(self.say("how many leave balance i have?"))
		self.assertIn("has no leave allocations covering", text)
		self.assertNotIn("0 day(s)", text)
		with mock.patch.object(hr, "_leave_types", return_value=[]):
			text = self.texts(self.say("show my leave balance"))
		self.assertIn("HRMS has no Leave Types", text)
		self.assertNotIn("0 day(s)", text)

	def test_without_hrms_my_leave_balance_says_so(self):
		with mock.patch.object(hr, "installed", return_value=False):
			r = self.say("how many leave balance i have?")
		self.assertIn("isn't installed", self.texts(r))
		self.assertIsNone(self.card(r, "table"))
