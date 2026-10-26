"""Manual QA findings of 2026-10-26: general conversation, “my company”, records of no named kind and result limits.

- “Tell me a joke.” is general conversation: the model's reply, labelled as a general answer, with no lookup or workflow.
- “Show me my company name.” is answered from the user's own usable companies; several without a default are a question.
- “Show 100000 records.” asks which records instead of re-running the previous query, and limits are explained, never
  silently replaced; a page of a longer list says how many records match in all.

The router model is mocked; everything else runs in a rolled-back transaction.
Run: bench --site <site> run-tests --module ai_erpnext.tests.test_chat_qa_routing
"""

import json
from unittest import mock

import frappe
from frappe.tests import IntegrationTestCase

from ai_erpnext.api import chat
from ai_erpnext.chat import company, engine, llm, queries, shortcuts

ROUTE = "ai_erpnext.chat.llm.route"
CO = {"A": "AIQ Alpha Co", "B": "AIQ Beta Co"}


class TestChatQARouting(IntegrationTestCase):
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
						"abbr": f"AIQ{key}",
						"default_currency": currency,
						"country": country,
						"create_chart_of_accounts_based_on": "Standard Template",
						"chart_of_accounts": "Standard",
					}
				).insert()
		cls.users = {}
		for key, allowed, default in (
			("one", ["A"], None),
			("both", ["A", "B"], None),
			("dflt", ["A", "B"], "B"),
		):
			email = f"aiq-{key}@example.com"
			if not frappe.db.exists("User", email):
				frappe.get_doc(
					{
						"doctype": "User",
						"email": email,
						"first_name": f"AIQ {key}",
						"send_welcome_email": 0,
						"roles": [{"role": "System Manager"}],
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
						"is_default": 1 if k == default else 0,
					}
				).insert(ignore_permissions=True)
			cls.users[key] = email
		group = frappe.db.get_value("Customer Group", {"is_group": 0}, "name")
		territory = frappe.db.get_value("Territory", {"is_group": 0}, "name")
		for i in range(1, 26):
			frappe.get_doc(
				{
					"doctype": "Customer",
					"customer_name": f"AIQ Cap {i:02d}",
					"customer_group": group,
					"territory": territory,
				}
			).insert()
		frappe.cache.delete_value("user_permissions")

	def setUp(self):
		self.as_user("Administrator")
		self.route = mock.patch(ROUTE, return_value={"mode": "chat", "reply": "ok"}).start()
		mock.patch("frappe.enqueue").start()
		self.addCleanup(mock.patch.stopall)
		self.addCleanup(frappe.set_user, "Administrator")

	def as_user(self, user):
		frappe.set_user(self.users.get(user, user) if hasattr(self, "users") else user)
		self.s = chat.new_conversation()["session"]

	def say(self, text, plan=None):
		if plan is not None:
			self.route.return_value = plan
		return chat.send_message(session=self.s, text=text)

	def state(self):
		return json.loads(frappe.db.get_value("AI Chat Session", self.s, "state") or "{}")

	@staticmethod
	def texts(r):
		return " ".join(m.get("text") or "" for m in r["messages"] if m["role"] == "assistant")

	@staticmethod
	def last(r):
		return r["messages"][-1]

	# Issue 1: general conversation ------------------------------------------------------------------
	def test_a_joke_is_general_conversation_without_lookups_or_actions(self):
		self.assertIsNone(shortcuts.match("Tell me a joke."))  # no deterministic data/workflow rule claims it
		joke = "Why did the scarecrow win an award? Because he was outstanding in his field."
		with (
			mock.patch.object(queries, "execute") as execute,
			mock.patch("ai_erpnext.chat.forms.start") as start,
		):
			r = self.say("Tell me a joke.", {"mode": "chat", "reply": joke})
			r2 = self.say("Tell me a joke.", {"mode": "chat", "reply": "A different joke."})
		execute.assert_not_called()
		start.assert_not_called()
		self.assertEqual((self.last(r)["text"], self.last(r)["source"]), (joke, "general"))
		self.assertNotIn("card", self.last(r))
		self.assertEqual(self.last(r2)["text"], "A different joke.")  # wording may differ each time
		st = self.state()
		self.assertEqual((st.get("pending"), st.get("forms") or [], st.get("workflow")), (None, [], None))
		self.assertIsNone(st.get("last_query"))

	def test_router_prompt_allows_general_topics_and_keeps_data_out_of_chat(self):
		system, _ = llm.build_system("Tell me a joke.")
		self.assertIn("a short work-appropriate joke, ERPNext and business topics", system)
		self.assertIn("off_topic: anything else (cooking, general programming", system)
		self.assertIn("Never state this company's data in a reply", system)

	def test_off_topic_questions_get_a_fixed_redirect_never_the_models_answer(self):
		recipe = "1. Boil fresh water. 2. Steep the tea for 3-5 minutes."
		for text, plan in (
			("how to make a tea?", {"mode": "off_topic"}),
			("how to make a tea?", {"mode": "off_topic", "reply": recipe}),  # a reply sent anyway is dropped
			# the model answered a general coding question in chat mode: still refused (code without an ERP topic)
			(
				"give me python code for find highest integer in array",
				{"mode": "chat", "reply": "Use max():\n```python\nprint(max([3, 1, 9]))\n```"},
			),
		):
			r = self.say(text, plan)
			msg = self.last(r)
			self.assertEqual(msg["text"], engine.OFF_TOPIC, text)
			self.assertNotIn("source", msg)  # not labelled as an answer
			self.assertNotIn("max(", json.dumps(r))
			self.assertNotIn("Boil", json.dumps(r))
		st = self.state()
		self.assertEqual((st.get("pending"), st.get("forms") or []), (None, []))

	def test_erpnext_customisation_code_and_jokes_are_still_answered(self):
		code = "In a Server Script:\n```python\ndoc.remarks = 'Checked'\n```"
		r = self.say("how do I set remarks in a frappe server script?", {"mode": "chat", "reply": code})
		self.assertEqual((self.last(r)["text"], self.last(r)["source"]), (code, "general"))
		r = self.say(
			"tell me a joke?", {"mode": "chat", "reply": "Why did the ledger stay calm? It was balanced."}
		)
		self.assertEqual(self.last(r)["source"], "general")

	def test_a_joke_during_a_form_leaves_the_form_untouched(self):
		self.say(
			"create purchase order",
			{"mode": "create", "doctype": "Purchase Order", "values": {}, "items": []},
		)
		before = self.state()["pending"]["key"]
		r = self.say("Tell me a joke.", {"mode": "chat", "reply": "Knock knock."})
		self.assertIn("Knock knock.", self.texts(r))
		self.assertIn("Back to the Purchase Order", self.texts(r))
		self.assertEqual(self.state()["pending"]["key"], before)
		self.assertEqual(self.state()["forms"][0]["doctype"], "Purchase Order")

	# Issue 2: “my company” ---------------------------------------------------------------------------
	def test_company_phrasings_are_recognised(self):
		for text in (
			"Show me my company name.",
			"what is my company?",
			"which company am I in",
			"What's our company name",
			"list my companies",
		):
			self.assertEqual(shortcuts.match(text), {"mode": "company"}, text)
		self.assertNotEqual(
			(shortcuts.match("show companies with overdue invoices") or {}).get("mode"), "company"
		)

	def test_one_usable_company_is_the_users_company(self):
		self.as_user("one")
		r = self.say("Show me my company name.")
		self.route.assert_not_called()
		self.assertEqual(self.last(r)["text"], f"Your company is {CO['A']}.")
		self.assertNotIn(CO["B"], json.dumps(r))  # a company the user may not use is never named

	def test_several_companies_with_a_default_name_the_default(self):
		self.as_user("dflt")
		r = self.say("what is my company?")
		self.assertEqual(
			self.last(r)["text"], f"Your default company is {CO['B']}. You can also use: {CO['A']}."
		)

	def test_several_companies_without_a_default_are_a_question_not_a_guess(self):
		self.as_user("both")
		r = self.say("Show me my company name.")
		text = self.last(r)["text"]
		self.assertIn(f"You have access to 2 companies: {CO['A']}, {CO['B']}", text)
		self.assertIn("Which company do you mean?", text)
		self.assertNotIn("Your company is", text)
		with mock.patch("frappe.defaults.get_global_default", return_value=CO["A"]):
			text = company.answer()["text"]
		self.assertIn(f"new documents default to {CO['A']}, the site-wide default company", text)
		with mock.patch("frappe.defaults.get_global_default", return_value="AIQ Elsewhere Co"):
			self.assertNotIn(
				"AIQ Elsewhere Co", company.answer()["text"]
			)  # not a usable company: not mentioned

	def test_no_usable_company_and_a_model_planned_company_query(self):
		with mock.patch.object(queries, "usable_companies", return_value=[]):
			r = self.say("Show me my company name.")
		self.assertIn("I can't see any company you have access to", self.last(r)["text"])
		self.as_user("one")
		r = self.say(
			"name of the organisation I work for",
			{"mode": "query", "query": {"doctype": "Company", "operation": "list"}},
		)
		self.assertEqual(self.last(r)["text"], f"Your company is {CO['A']}.")
		self.assertNotIn("can't look up", self.texts(r))

	def test_unsupported_lookup_message_is_short_and_accurate(self):
		r = self.say(
			"show spaceships", {"mode": "query", "query": {"doctype": "Spaceship", "operation": "list"}}
		)
		text = self.last(r)["text"]
		self.assertIn("I can't look up “Spaceship” records.", text)
		self.assertIn("Sales Invoice", text)
		self.assertIn("other kinds of records", text)
		self.assertLess(len(text), 400)
		self.assertNotIn("Stock Ledger Entry", text)  # no full dump of every DocType

	# Issue 4: records of no named kind, stale context and limits ----------------------------------------
	def test_show_100000_records_asks_which_and_never_reuses_the_previous_query(self):
		r = self.say("show unpaid purchase invoices")
		self.assertEqual(self.state()["last_query"]["plan"]["doctype"], "Purchase Invoice")
		with mock.patch.object(queries, "execute") as execute:
			r = self.say("Show 100000 records.")
		execute.assert_not_called()
		self.route.assert_not_called()
		msg = self.last(r)
		self.assertTrue(msg["text"].startswith("Which records would you like to see"))
		self.assertIn("at most 20 records per message (you asked for 100000)", msg["text"])
		self.assertNotIn("card", msg)
		self.assertNotIn("Purchase Invoice", msg["text"])
		self.assertIsNone(self.state()["last_query"])  # the next reply starts fresh
		self.say("customers", {"mode": "query", "query": {"doctype": "Customer", "operation": "list"}})
		self.assertIsNone(self.route.call_args.args[2])  # the router got no previous query to inherit from

	def test_explicit_follow_ups_keep_the_previous_query_for_the_router(self):
		self.say("show unpaid purchase invoices")
		self.say(
			"only the overdue ones",
			{"mode": "query", "query": {"doctype": "Purchase Invoice", "operation": "overdue"}},
		)
		previous = self.route.call_args.args[2]
		self.assertEqual(
			(previous["plan"]["doctype"], previous["plan"]["operation"]), ("Purchase Invoice", "unpaid")
		)
		context = llm.router_context(previous)
		self.assertEqual(context["previous_query"]["doctype"], "Purchase Invoice")
		system, _ = llm.build_system("only the overdue ones", previous)
		self.assertIn("any other message starts fresh, without its DocType or filters", system)
		self.assertIn("Records of no named kind", system)

	def test_a_new_topic_does_not_inherit_filters(self):
		self.say("show unpaid purchase invoices")
		r = self.say("show the last 5 customers")
		card = self.last(r)["card"]
		self.assertEqual(card["doctype"], "Customer")
		self.assertNotIn("outstanding", card["note"])
		self.assertEqual(len(card["rows"]), 5)

	def test_large_limits_are_capped_explained_and_paged(self):
		plan = {
			"mode": "query",
			"query": {
				"doctype": "Customer",
				"operation": "list",
				"filters": {"search": "AIQ Cap"},
				"limit": 100000,
			},
		}
		r = self.say("show 100000 AIQ Cap customers", plan)
		msg = self.last(r)
		self.assertEqual(len(msg["card"]["rows"]), queries.MAX_PAGE)
		self.assertTrue(msg["card"]["has_more"])
		self.assertIn(f"25 Customer records match; here are the first {queries.MAX_PAGE}", msg["text"])
		self.assertIn("You asked for 100000, but I show at most 20 per message", msg["text"])
		self.assertIn("Showing 20 of 25 matching records.", msg["card"]["note"])
		r = self.say("show more")
		msg = self.last(r)
		self.assertEqual(msg["text"], "Showing Customer records 21–25 of 25.")  # the cap note isn't repeated
		self.assertEqual(len(msg["card"]["rows"]), 5)
		self.assertFalse(msg["card"]["has_more"])
		# the deterministic list phrasing caps the same way
		plan = shortcuts.match("show 100000 customers")["query"]
		self.assertEqual(plan["limit"], 100000)
		q = queries.validate(plan)
		self.assertEqual((q["limit"], q["requested_limit"]), (queries.MAX_PAGE, 100000))

	def test_a_complete_small_result_is_reported_as_complete(self):
		r = self.say(
			"AIQ Cap 0 customers",
			{
				"mode": "query",
				"query": {"doctype": "Customer", "operation": "list", "filters": {"search": "AIQ Cap 0"}},
			},
		)
		msg = self.last(r)
		self.assertEqual(msg["text"], "I found 9 Customer records.")
		self.assertFalse(msg["card"]["has_more"])
		self.assertNotIn("matching records", msg["card"]["note"])
		r = self.say(
			"show 100000 AIQ Cap 0 customers",
			{
				"mode": "query",
				"query": {
					"doctype": "Customer",
					"operation": "list",
					"filters": {"search": "AIQ Cap 0"},
					"limit": 100000,
				},
			},
		)
		# a large limit with fewer matches: complete, and said so (not "at most 20", which would suggest more exist)
		self.assertEqual(
			self.last(r)["text"],
			"I found 9 Customer records, which is all of them (you asked for up to 100000).",
		)

	def test_open_in_erpnext_route_only_when_the_list_view_can_show_the_same_filters(self):
		self.assertEqual(
			queries._list_route("Sales Invoice", [["docstatus", "=", 1], ["company", "in", [CO["A"]]]], []),
			{"doctype": "Sales Invoice", "filters": {"docstatus": 1, "company": ["in", [CO["A"]]]}},
		)
		self.assertIsNone(queries._list_route("Customer", [], [["customer_name", "like", "%x%"]]))
		self.assertIsNone(queries._list_route("Timesheet", [["employee", "in", ["HR-EMP-1"]]], []))
		self.assertIsNone(
			queries._list_route(
				"Sales Invoice", [["due_date", "<", "2026-01-01"], ["due_date", "<", "2025-01-01"]], []
			)
		)
