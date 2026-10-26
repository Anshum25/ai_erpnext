"""Unpaid/overdue Sales Invoices and the company scope (production report 2026-10-26).

“show me unpaid and overdue si” was answered with “Which company's Sales Invoices should I look at?” (a router-model
clarification), and the reply “nivasync infotech” with “I couldn't find a company …” (only an exact, whole-name match was
accepted). A query without a company covers every company the user may use, so no company is asked for; a named company
is matched among the user's own companies (case, spacing and partial names), an ambiguous one is asked about with the
original query kept, and nothing is revealed about other companies.

Fixtures (rolled back): three companies, two of them with similar names, users limited to one, several or another company,
and invoices that are overdue, not yet due, paid, draft and cancelled. The router model is mocked.
Run: bench --site <site> run-tests --module ai_erpnext.tests.test_chat_company_scope
"""

import json
from unittest import mock

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_days, nowdate

from ai_erpnext.api import chat
from ai_erpnext.chat import llm, queries, shortcuts

ROUTE = "ai_erpnext.chat.llm.route"
CO = {"A": "AIO Nivasync Infotech Pvt Ltd", "B": "AIO Beta Traders", "C": "AIO Nivasync Labs"}
USERS = {"one": ["A"], "multi": ["A", "B", "C"], "restricted": ["B"]}


class TestChatCompanyScope(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		currency = (
			frappe.db.get_value(
				"Company",
				frappe.db.get_single_value("Global Defaults", "default_company"),
				"default_currency",
			)
			or "INR"
		)
		country = frappe.db.get_single_value("System Settings", "country") or "India"
		for key, name in CO.items():
			if not frappe.db.exists("Company", name):
				frappe.get_doc(
					{
						"doctype": "Company",
						"company_name": name,
						"abbr": f"AIO{key}",
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
					"customer_name": "AIO Buyer",
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
				"item_code": "AIO-ITEM",
				"item_name": "AIO Service",
				"item_group": ig,
				"stock_uom": "Nos",
				"is_stock_item": 0,
				"is_sales_item": 1,
			}
		).insert()
		cls.si = {
			"A_overdue": cls.make_si("A", due=add_days(nowdate(), -3)),
			"A_not_due": cls.make_si("A", due=add_days(nowdate(), 10)),
			"A_paid": cls.make_si("A", due=add_days(nowdate(), -3)),
			"A_draft": cls.make_si("A", due=add_days(nowdate(), -3), submit=False),
			"A_cancelled": cls.make_si("A", due=add_days(nowdate(), -3)),
			"B_overdue": cls.make_si("B", due=add_days(nowdate(), -3)),
			"C_overdue": cls.make_si("C", due=add_days(nowdate(), -3)),
		}
		frappe.db.set_value("Sales Invoice", cls.si["A_paid"], {"outstanding_amount": 0, "status": "Paid"})
		frappe.get_doc("Sales Invoice", cls.si["A_cancelled"]).cancel()
		cls.users = {}
		for key, allowed in USERS.items():
			email = f"aio-{key}@example.com"
			if not frappe.db.exists("User", email):
				frappe.get_doc(
					{
						"doctype": "User",
						"email": email,
						"first_name": f"AIO {key}",
						"send_welcome_email": 0,
						"roles": [{"role": "System Manager"}, {"role": "Accounts User"}],
					}
				).insert(ignore_permissions=True)
			for k in allowed:
				frappe.get_doc(
					{
						"doctype": "User Permission",
						"user": email,
						"allow": "Company",
						"for_value": CO[k],
						"apply_to_all_doctypes": 1,
					}
				).insert(ignore_permissions=True)
			cls.users[key] = email
		frappe.cache.delete_value("user_permissions")

	@classmethod
	def make_si(cls, key, due, submit=True):
		posting = add_days(nowdate(), -5)
		si = frappe.get_doc(
			{
				"doctype": "Sales Invoice",
				"customer": cls.customer,
				"company": CO[key],
				"posting_date": posting,
				"set_posting_time": 1,
				"due_date": due,
				"items": [{"item_code": "AIO-ITEM", "qty": 1, "rate": 100}],
			}
		).insert()
		if submit:
			si.submit()
		return si.name

	def setUp(self):
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

	@staticmethod
	def last(r):
		return r["messages"][-1]

	def shown(self, r):
		card = self.last(r).get("card") or {}
		return {row["_name"] for row in card.get("rows") or []}

	def names(self, *keys):
		return {self.si[k] for k in keys}

	# 1-4: no company named: every company the user may use, never a company question -------------------
	def test_unpaid_and_overdue_without_a_company_covers_all_permitted_companies(self):
		self.as_user("multi")
		r = self.say("show me unpaid and overdue si")
		self.route.assert_not_called()  # deterministic: the model can't ask "which company?"
		self.assertIsNone(self.pending())
		self.assertEqual(self.shown(r), self.names("A_overdue", "A_not_due", "B_overdue", "C_overdue"))
		text = self.last(r)["text"]
		self.assertIn("I found 4 unpaid Sales Invoices; 3 of them are overdue", text)
		self.assertNotIn("Which company", text)
		marks = {row["_name"]: row["_overdue"] for row in self.last(r)["card"]["rows"]}
		self.assertEqual(marks[self.si["A_not_due"]], "")
		self.assertEqual(marks[self.si["A_overdue"]], "Yes")
		self.assertIn("Overdue", [c["label"] for c in self.last(r)["card"]["columns"]])

	def test_a_user_with_one_company_sees_only_that_company(self):
		self.as_user("one")
		r = self.say("show me unpaid and overdue si")
		self.assertEqual(self.shown(r), self.names("A_overdue", "A_not_due"))
		self.assertIn("1 of them is overdue", self.last(r)["text"])

	def test_a_restricted_user_sees_only_their_company(self):
		self.as_user("restricted")
		r = self.say("show me unpaid and overdue sales invoices")
		self.assertEqual(self.shown(r), self.names("B_overdue"))

	# 10-12: what unpaid and overdue mean --------------------------------------------------------------------
	def test_paid_draft_cancelled_and_not_yet_due_invoices(self):
		self.as_user("one")
		unpaid = self.shown(self.say("show unpaid si"))
		self.assertEqual(unpaid, self.names("A_overdue", "A_not_due"))
		self.assertFalse(unpaid & self.names("A_paid", "A_draft", "A_cancelled"))
		overdue = self.shown(self.say("show overdue si"))
		self.assertEqual(overdue, self.names("A_overdue"))  # not yet due: unpaid, but not overdue

	# 5-8: a named company ------------------------------------------------------------------------------------
	def test_a_named_company_is_matched_ignoring_case_spacing_and_extra_words(self):
		self.as_user("multi")
		r = self.say("show overdue si for   aio BETA   traders")
		self.route.assert_not_called()
		self.assertEqual(self.shown(r), self.names("B_overdue"))
		r = self.say("show overdue si for nivasync infotech")  # the start of each word is enough
		self.assertEqual(self.shown(r), self.names("A_overdue"))
		self.assertIn(f"company {CO['A']}", self.last(r)["card"]["note"])

	def test_the_reported_conversation_now_resolves_the_company(self):
		# the production path: the model plans the query with the company as the user typed it
		self.as_user("multi")
		plan = {
			"mode": "query",
			"query": {
				"doctype": "Sales Invoice",
				"operation": "overdue",
				"filters": {"company": "nivasync infotech"},
			},
		}
		r = self.say("nivasync infotech", plan)
		self.assertEqual(self.shown(r), self.names("A_overdue"))
		self.assertNotIn("couldn't find", self.last(r)["text"])

	def test_an_unknown_or_unpermitted_company_reveals_nothing(self):
		self.as_user("restricted")
		plan = {
			"mode": "query",
			"query": {
				"doctype": "Sales Invoice",
				"operation": "overdue",
				"filters": {"company": "Nivasync Infotech"},
			},
		}
		r = self.say("overdue invoices of nivasync infotech", plan)
		text = self.last(r)["text"]
		self.assertIn("I couldn't find a company “Nivasync Infotech” that you have access to.", text)
		self.assertIn(f"Your companies: {CO['B']}.", text)  # only the user's own companies are named
		self.assertNotIn(CO["A"], json.dumps(r))
		self.assertNotIn("card", self.last(r))
		# the deterministic phrasing doesn't treat an unknown name as a company: the router reads it (e.g. a customer)
		self.assertIsNone(shortcuts.match("show overdue si for nivasync infotech"))

	def test_an_ambiguous_company_is_asked_and_the_original_query_resumes(self):
		self.as_user("multi")
		r = self.say("show overdue si for nivasync")
		self.assertNotIn("card", self.last(r))
		q = self.pending()
		self.assertEqual(q["kind"], "query_company")
		self.assertEqual({o["value"] for o in q["options"]}, {CO["A"], CO["C"]})  # B doesn't match
		self.assertEqual((q["plan"]["doctype"], q["plan"]["operation"]), ("Sales Invoice", "overdue"))
		r = self.say("labs")  # a typed answer to the company question, not a new request
		self.assertEqual(self.shown(r), self.names("C_overdue"))  # still overdue Sales Invoices
		self.assertIsNone(self.pending())
		self.say("show overdue si for nivasync")
		r = self.click(CO["A"])
		self.assertEqual(self.shown(r), self.names("A_overdue"))
		self.assertIsNone(self.pending())
		self.say("show unpaid si for nivasync")
		r = self.say("cancel")
		self.assertIn("stopped that lookup", self.last(r)["text"])
		self.assertIsNone(self.pending())

	def test_router_prompt_never_asks_for_a_company(self):
		system, _ = llm.build_system("show me unpaid and overdue si")
		self.assertIn(
			"never clarify which company: without it a query covers all the user's companies", system
		)
		with self.assertRaises(queries.QueryClarify) as c:
			frappe.set_user(self.users["multi"])
			queries.validate(
				{"doctype": "Sales Invoice", "operation": "unpaid", "filters": {"company": "Nivasync"}}
			)
		self.assertEqual(c.exception.kind, "company")
		self.assertEqual(c.exception.plan["operation"], "unpaid")
