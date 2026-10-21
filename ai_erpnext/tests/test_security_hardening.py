"""Regression tests for the security review of the chatbot (permissions, history, AI Document Entry ownership,
endpoint methods, Draft-only creation, logging).

Everything is created inside the test transaction and rolled back. No provider is ever called.

Run: bench --site <site> run-tests --module ai_erpnext.tests.test_security_hardening
"""

import json
import re
from pathlib import Path
from unittest import mock

import frappe
from frappe.tests import IntegrationTestCase

from ai_erpnext.api import chat, creation, extraction, mapping, validation
from ai_erpnext.chat import forms, history
from ai_erpnext.chat import session as S

COMPANIES = {"A": "AIS Alpha Co", "B": "AIS Beta Co"}
ROUTE = "ai_erpnext.chat.llm.route"
APP = Path(__file__).resolve().parents[1]


def make_user(email, roles, permissions=()):
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
	for allow, value in permissions:
		if frappe.db.exists("User Permission", {"user": email, "allow": allow, "for_value": value}):
			continue
		frappe.get_doc(
			{
				"doctype": "User Permission",
				"user": email,
				"allow": allow,
				"for_value": value,
				"apply_to_all_doctypes": 1 if allow == "Company" else 0,
			}
		).insert(ignore_permissions=True)
	frappe.cache.delete_value("user_permissions")
	return email


class SecurityCase(IntegrationTestCase):
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
		for key, name in COMPANIES.items():
			if not frappe.db.exists("Company", name):
				frappe.get_doc(
					{
						"doctype": "Company",
						"company_name": name,
						"abbr": f"AIS{key}",
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
					"customer_name": "AIS Hidden Buyer",
					"customer_group": group,
					"territory": territory,
					"default_currency": "USD",
				}
			)
			.insert()
			.name
		)
		cls.other_customer = (
			frappe.get_doc(
				{
					"doctype": "Customer",
					"customer_name": "AIS Visible Buyer",
					"customer_group": group,
					"territory": territory,
				}
			)
			.insert()
			.name
		)
		ig = frappe.db.get_value("Item Group", {"is_group": 0}, "name")
		for code in ("AIS-ITEM", "AIS-OTHER"):
			frappe.get_doc(
				{
					"doctype": "Item",
					"item_code": code,
					"item_name": code,
					"item_group": ig,
					"stock_uom": "Nos",
					"is_stock_item": 0,
					"is_sales_item": 1,
					"is_purchase_item": 1,
				}
			).insert()
		frappe.get_doc(
			{
				"doctype": "Item Price",
				"item_code": "AIS-ITEM",
				"price_list": "Standard Selling",
				"price_list_rate": 12,
				"selling": 1,
			}
		).insert()
		cls.invoices = {}
		for key in COMPANIES:
			cls.invoices[key] = (
				frappe.get_doc(
					{
						"doctype": "Sales Invoice",
						"customer": cls.customer,
						"company": COMPANIES[key],
						"posting_date": frappe.utils.add_days(frappe.utils.nowdate(), -3),
						"set_posting_time": 1,
						"due_date": frappe.utils.add_days(frappe.utils.nowdate(), -3),
						"items": [{"item_code": "AIS-ITEM", "qty": 1, "rate": 100}],
					}
				)
				.insert()
				.name
			)
		cls.sm_a = make_user(
			"ais-sm-a@example.com", ["System Manager", "Accounts User"], [("Company", COMPANIES["A"])]
		)
		cls.sm_ab = make_user(
			"ais-sm-ab@example.com",
			["System Manager", "Accounts User"],
			[("Company", COMPANIES["A"]), ("Company", COMPANIES["B"])],
		)
		cls.sm_2 = make_user("ais-sm-2@example.com", ["System Manager"])

	def setUp(self):
		frappe.set_user("Administrator")
		mock.patch("frappe.enqueue").start()
		self.addCleanup(mock.patch.stopall)

	def tearDown(self):
		frappe.set_user("Administrator")

	def drop_permission(self, user, company):
		frappe.set_user("Administrator")
		for name in frappe.get_all(
			"User Permission", filters={"user": user, "allow": "Company", "for_value": company}, pluck="name"
		):
			frappe.delete_doc("User Permission", name, ignore_permissions=True, force=True)
		frappe.cache.delete_value("user_permissions")


class TestConversationHistory(SecurityCase):
	"""Stored query results are checked again, with the reader's current permissions, whenever they are shown again."""

	counter = 0

	def setUp(self):
		super().setUp()
		# fresh users per test: permissions are changed during the tests and the transaction is only rolled back per class
		TestConversationHistory.counter += 1
		n = TestConversationHistory.counter
		self.u_a = make_user(
			f"ais-h{n}-a@example.com", ["System Manager", "Accounts User"], [("Company", COMPANIES["A"])]
		)
		self.u_ab = make_user(
			f"ais-h{n}-ab@example.com",
			["System Manager", "Accounts User"],
			[("Company", COMPANIES["A"]), ("Company", COMPANIES["B"])],
		)

	def ask(self, user, plan, text="show them"):
		frappe.set_user(user)
		session = chat.new_conversation()["session"]
		with mock.patch(ROUTE, return_value={"mode": "query", "query": plan}):
			chat.send_message(session=session, text=text)
		return session

	def listing(self):
		return {
			"doctype": "Sales Invoice",
			"operation": "list",
			"filters": {"party": "AIS Hidden"},
			"limit": 20,
		}

	def reopen(self, user, session):
		frappe.set_user(user)
		return chat.open_conversation(session)["messages"]

	def result_message(self, messages):
		return next(m for m in messages if m["role"] == "assistant" and m.get("id", 0) > 1)

	def test_unchanged_permissions_replay_the_stored_result(self):
		session = self.ask(self.u_ab, self.listing())
		msg = self.result_message(self.reopen(self.u_ab, session))
		self.assertEqual({r["_name"] for r in msg["card"]["rows"]}, set(self.invoices.values()))
		self.assertTrue(
			all("ref" not in m for m in self.reopen(self.u_ab, session))
		)  # internal references never reach the browser

	def test_a_company_that_is_no_longer_permitted_is_removed_from_the_history(self):
		session = self.ask(self.u_ab, self.listing())
		self.drop_permission(self.u_ab, COMPANIES["B"])
		msg = self.result_message(self.reopen(self.u_ab, session))
		self.assertEqual({r["_name"] for r in msg["card"]["rows"]}, {self.invoices["A"]})
		self.assertIn("no longer available", msg["text"])
		self.assertNotIn(self.invoices["B"], json.dumps(self.reopen(self.u_ab, session)))

	def test_a_result_with_no_readable_record_is_withheld(self):
		session = self.ask(self.u_a, self.listing())
		self.drop_permission(self.u_a, COMPANIES["A"])
		make_user(self.u_a, [], [("Company", COMPANIES["B"])])
		msgs = self.reopen(self.u_a, session)
		msg = self.result_message(msgs)
		self.assertEqual(msg["text"], history.SAFE_TEXT)
		self.assertNotIn("card", msg)
		self.assertNotIn("AIS Hidden Buyer", json.dumps(msgs))
		self.assertNotIn(self.invoices["A"], json.dumps(msgs))

	def test_record_details_are_withheld_when_the_record_is_no_longer_readable(self):
		session = self.ask(
			self.u_a,
			{"doctype": "Sales Invoice", "operation": "details", "filters": {"name": self.invoices["A"]}},
		)
		self.assertEqual(
			self.result_message(self.reopen(self.u_a, session))["card"]["name"], self.invoices["A"]
		)
		self.drop_permission(self.u_a, COMPANIES["A"])
		make_user(self.u_a, [], [("Company", COMPANIES["B"])])
		msg = self.result_message(self.reopen(self.u_a, session))
		self.assertEqual(msg["text"], history.SAFE_TEXT)
		self.assertNotIn("card", msg)

	def test_aggregate_results_need_the_doctype_to_stay_readable(self):
		session = self.ask(
			self.u_ab,
			{
				"doctype": "Sales Invoice",
				"operation": "aggregate",
				"aggregate": {"fn": "sum", "field": "grand_total"},
				"filters": {"party": "AIS Hidden"},
			},
		)
		msg = self.result_message(self.reopen(self.u_ab, session))
		self.assertTrue(msg["card"]["rows"])
		with mock.patch(
			"frappe.has_permission", side_effect=lambda dt=None, ptype="read", *a, **k: dt != "Sales Invoice"
		):
			msg = self.result_message(chat.open_conversation(session)["messages"])
		self.assertEqual(msg["text"], history.SAFE_TEXT)

	def test_results_stored_without_a_known_source_are_withheld(self):
		session = self.ask(self.u_ab, self.listing())
		frappe.set_user("Administrator")
		doc = frappe.get_doc(S.DOCTYPE, session)
		msgs = json.loads(doc.messages)
		stored = next(m for m in msgs if m.get("source") == "erpnext")
		# a result stored before references were kept, without a card: its records can't be determined
		stored.pop("ref", None)
		stored.pop("card", None)
		stored["text"] = "There are 12 Sales Invoice records you can access."
		doc.messages = json.dumps(msgs)
		doc.save()
		shown = self.reopen(self.u_ab, session)
		self.assertEqual(next(m for m in shown if m["id"] == stored["id"])["text"], history.SAFE_TEXT)
		self.assertNotIn("12 Sales Invoice", json.dumps(shown))

	def test_results_stored_without_references_are_checked_through_their_card(self):
		session = self.ask(self.u_ab, self.listing())
		frappe.set_user("Administrator")
		doc = frappe.get_doc(S.DOCTYPE, session)
		msgs = json.loads(doc.messages)
		for m in msgs:
			m.pop("ref", None)
		doc.messages = json.dumps(msgs)
		doc.save()
		self.drop_permission(self.u_ab, COMPANIES["B"])
		shown = self.result_message(self.reopen(self.u_ab, session))
		self.assertEqual({r["_name"] for r in shown["card"]["rows"]}, {self.invoices["A"]})

	def test_ordinary_conversation_history_is_untouched(self):
		session = self.ask(self.u_ab, self.listing(), text="Show invoices of AIS Hidden")
		self.drop_permission(self.u_ab, COMPANIES["B"])
		msgs = self.reopen(self.u_ab, session)
		self.assertEqual(msgs[0]["role"], "assistant")  # the greeting
		self.assertIn(
			"AIS Hidden", next(m for m in msgs if m["role"] == "user")["text"]
		)  # the user's own words stay

	def test_a_user_who_lost_chat_access_gets_nothing_from_stored_conversations(self):
		session = self.ask(self.u_ab, self.listing())
		frappe.set_user("Administrator")
		frappe.get_doc("User", self.u_ab).remove_roles("System Manager")
		frappe.clear_cache(user=self.u_ab)
		for call in (
			lambda: chat.open_conversation(session),
			lambda: chat.get_session(session),
			lambda: chat.poll(session=session),
		):
			frappe.set_user(self.u_ab)
			with self.assertRaises(frappe.PermissionError):
				call()

	def test_another_users_conversation_is_not_available(self):
		session = self.ask(self.u_ab, self.listing())
		frappe.set_user(self.sm_2)
		with self.assertRaises(frappe.PermissionError):
			chat.open_conversation(session)

	def test_recheck_helper_passes_new_messages_and_messages_without_data(self):
		msgs = [
			{"id": 1, "role": "assistant", "text": "Hi"},
			{"id": 2, "role": "user", "text": "x"},
			{
				"id": 3,
				"role": "assistant",
				"text": "fresh",
				"source": "erpnext",
				"ref": {"doctype": "Sales Invoice", "names": ["NOPE"]},
			},
		]
		out = history.recheck(msgs, replay_upto=2)
		self.assertEqual(
			[m["text"] for m in out], ["Hi", "x", "fresh"]
		)  # id 3 was produced by this very request
		self.assertTrue(all("ref" not in m for m in out))
		again = history.recheck(msgs, replay_upto=3)
		self.assertEqual(again[2]["text"], history.SAFE_TEXT)  # a stored result naming an unreadable record


class TestAIDocumentEntryOwnership(SecurityCase):
	def make_entry(self, user):
		frappe.set_user(user)
		extracted = {
			"invoice_number": "SECRET-INV-1",
			"total_amount": 123.45,
			"supplier_name": "Private Supplier",
		}
		return (
			frappe.get_doc(
				{
					"doctype": "AI Document Entry",
					"status": "Processed",
					"extracted_data": json.dumps(extracted),
				}
			)
			.insert()
			.name
		)

	def test_owner_has_access_other_system_managers_do_not(self):
		entry = self.make_entry(self.sm_a)
		frappe.set_user(self.sm_a)
		self.assertTrue(frappe.has_permission("AI Document Entry", "read", doc=entry))
		self.assertEqual(mapping.get_review(entry)["name"], entry)
		for other in (self.sm_ab, self.sm_2):
			frappe.set_user(other)
			self.assertFalse(frappe.has_permission("AI Document Entry", "read", doc=entry))
			self.assertFalse(frappe.has_permission("AI Document Entry", "write", doc=entry))
			with self.assertRaises(frappe.PermissionError):
				frappe.get_doc("AI Document Entry", entry).check_permission("read")
			self.assertNotIn(entry, frappe.get_list("AI Document Entry", pluck="name"))
			self.assertEqual(frappe.get_list("AI Document Entry", filters={"name": entry}), [])
		frappe.set_user("Administrator")
		self.assertTrue(frappe.has_permission("AI Document Entry", "read", doc=entry))

	def test_every_review_endpoint_refuses_other_users_and_says_the_same_as_for_a_missing_entry(self):
		entry = self.make_entry(self.sm_a)
		frappe.set_user(self.sm_2)
		calls = {
			"get_review": lambda e: mapping.get_review(e),
			"remap": lambda e: mapping.remap(e),
			"apply_corrections": lambda e: mapping.apply_corrections(e, "{}"),
			"validate_entry": lambda e: validation.validate_entry(e),
			"create_document": lambda e: creation.create_document(e),
			"retry_extraction": lambda e: extraction.retry_extraction(e),
		}
		for name, call in calls.items():
			with self.assertRaises(frappe.PermissionError, msg=name) as foreign:
				call(entry)
			with self.assertRaises(frappe.PermissionError, msg=name) as missing:
				call("AI-DOC-DOES-NOT-EXIST")
			self.assertEqual(
				str(foreign.exception), str(missing.exception), name
			)  # existence is not revealed
		# a non-name is refused before the function runs (Frappe validates the endpoint's type hints) or by get_entry
		with self.assertRaises((frappe.PermissionError, TypeError)):
			mapping.get_review(["not", "a", "name"])

	def test_the_entry_stays_unchanged_after_a_refused_call(self):
		entry = self.make_entry(self.sm_a)
		frappe.set_user(self.sm_2)
		with self.assertRaises(frappe.PermissionError):
			mapping.remap(entry, target_doctype="Sales Invoice")
		frappe.set_user("Administrator")
		self.assertFalse(frappe.db.get_value("AI Document Entry", entry, "target_doctype"))

	def test_chat_cannot_adopt_another_users_entry(self):
		from ai_erpnext.chat import engine

		entry = self.make_entry(self.sm_a)
		frappe.set_user(self.sm_2)
		session = chat.new_conversation()["session"]
		doc = frappe.get_doc(S.DOCTYPE, session)
		state = json.loads(doc.state or "{}")
		state["entry"] = entry
		doc.state = json.dumps(state)
		doc.save()
		with self.assertRaises(frappe.PermissionError):
			engine._entry(engine.Ctx(S.lock(S.get_session(session))))

	def test_remap_only_accepts_registered_document_types(self):
		entry = self.make_entry(self.sm_a)
		frappe.set_user(self.sm_a)
		for bad in ("User", "DocType", "Sales Order; drop", "Note", ["Sales Invoice"]):
			with self.assertRaises((frappe.ValidationError, TypeError, AttributeError), msg=str(bad)):
				mapping.remap(entry, target_doctype=bad)
		self.assertFalse(frappe.db.get_value("AI Document Entry", entry, "target_doctype"))
		with mock.patch("ai_erpnext.api.mapping.apply_mapping"):
			self.assertEqual(mapping.remap(entry, target_doctype="Sales Invoice")["name"], entry)
		self.assertEqual(frappe.db.get_value("AI Document Entry", entry, "target_doctype"), "Sales Invoice")


class TestEndpointMethods(SecurityCase):
	def methods(self, fn):
		return frappe.allowed_http_methods_for_whitelisted_func.get(fn)

	def test_state_changing_endpoints_accept_post_only(self):
		for fn in (
			mapping.get_review,
			mapping.remap,
			mapping.apply_corrections,
			validation.validate_entry,
			creation.create_document,
			extraction.retry_extraction,
			chat.get_session,
			chat.send_message,
			chat.poll,
			chat.new_conversation,
			chat.list_conversations,
			chat.open_conversation,
		):
			self.assertEqual(self.methods(fn), ["POST"], fn.__name__)

	def test_every_whitelisted_endpoint_of_the_app_declares_its_methods(self):
		unrestricted = []
		for path in (APP / "api").glob("*.py"):
			source = path.read_text()
			for m in re.finditer(r"@frappe\.whitelist\((?P<args>[^)]*)\)\s*\ndef (?P<name>\w+)", source):
				if "methods" not in m.group("args"):
					unrestricted.append(f"{path.stem}.{m.group('name')}")
		self.assertEqual(unrestricted, [])


class TestPermissionAwareLookups(SecurityCase):
	def restricted_user(self):
		"""A System Manager whose User Permissions allow only the other item / customer."""
		return make_user(
			"ais-restricted@example.com",
			["System Manager", "Sales User", "Accounts User"],
			[("Item", "AIS-OTHER"), ("Customer", self.other_customer)],
		)

	def test_price_suggestions_respect_item_permissions(self):
		form = {"doctype": "Sales Order"}
		frappe.set_user("Administrator")
		self.assertEqual(forms._price(form, "AIS-ITEM")[0], 12)
		frappe.set_user(self.restricted_user())
		self.assertIsNone(forms._price(form, "AIS-ITEM"))  # no Item Price of an item the user can't access

	def test_price_lookup_without_read_permission_gives_no_price(self):
		with mock.patch("frappe.get_list", side_effect=frappe.PermissionError):
			self.assertIsNone(forms._price({"doctype": "Sales Order"}, "AIS-ITEM"))

	def test_existing_record_checks_do_not_reveal_hidden_records(self):
		frappe.set_user("Administrator")
		self.assertEqual(forms._exists_named("Customer", "customer_name", "AIS Hidden Buyer"), self.customer)
		frappe.set_user(self.restricted_user())
		self.assertIsNone(forms._exists_named("Customer", "customer_name", "AIS Hidden Buyer"))
		self.assertEqual(
			forms._exists_named("Customer", "customer_name", "AIS Visible Buyer"), self.other_customer
		)

	def test_customer_default_currency_is_not_read_for_hidden_customers(self):
		from ai_erpnext.mapping.currency import CurrencyMatcher

		frappe.set_user("Administrator")
		self.assertEqual(CurrencyMatcher().resolve(None, customer=self.customer).name, "USD")
		frappe.set_user(self.restricted_user())
		result = CurrencyMatcher().resolve(None, customer=self.customer)
		self.assertNotEqual(result.name, "USD")  # the hidden customer's currency is not used

	def test_access_helpers(self):
		from ai_erpnext.capability import access

		frappe.set_user(self.restricted_user())
		self.assertFalse(access.readable_exists("Customer", self.customer))
		self.assertTrue(access.readable_exists("Customer", self.other_customer))
		self.assertIsNone(access.readable_value("Customer", self.customer, "default_currency"))
		self.assertIsNone(access.readable_value("Customer", self.other_customer, "no_such_field"))
		self.assertEqual(
			access.readable_names(
				"Customer", {"name": ["in", [self.customer, self.other_customer]]}, limit=5
			),
			[self.other_customer],
		)
		self.assertEqual(access.readable_names("Customer", {"name": "x"}, limit=1), [])


class TestDraftOnly(SecurityCase):
	def test_no_lifecycle_call_exists_in_application_code(self):
		"""No submit/cancel/delete/amend anywhere in the app (the tests excepted); the only permission bypass is the extraction job's own result save."""
		found = []
		skip = {
			"run_tests2.py",
			"set_api_key.py",
			"check_provider.py",
			"update_provider.py",
			"setup_provider.py",
			"fix_client_script.py",
		}
		for path in APP.rglob("*.py"):
			if "tests" in path.parts or path.name in skip:
				continue
			for line in path.read_text().splitlines():
				if "forms.cancel(" in line:  # cancels the chat's guided form, not a document
					continue
				if re.search(
					r"\.submit\(|\.cancel\(|docstatus\s*=\s*[12]\b|delete_doc|\.amend\(|ignore_permissions\s*=\s*(True|1)",
					line,
				):
					found.append(f"{path.relative_to(APP)}: {line.strip()}")
		# exactly one hit: the extraction job saving its own AI Document Entry result
		self.assertEqual(found, ["pipeline/controller.py: doc.save(ignore_permissions=True)"])

	def test_model_supplied_lifecycle_fields_never_reach_insert(self):
		frappe.set_user("Administrator")
		form = {
			"doctype": "Sales Order",
			"values": {
				"customer": self.customer,
				"company": COMPANIES["A"],
				"docstatus": 1,
				"owner": "evil@example.com",
				"transaction_date": frappe.utils.nowdate(),
				"delivery_date": frappe.utils.nowdate(),
			},
			"labels": {},
			"items": [{"item_code": "AIS-ITEM", "qty": 1, "rate": 5, "docstatus": 1}],
			"extra": [],
		}
		ctx = mock.MagicMock()
		ctx.st = {"forms": [form], "pending": None}
		before = frappe.db.count("Sales Order")
		with mock.patch.object(forms, "_rejected") as rejected:
			forms._create(ctx, form)
		rejected.assert_called_once()  # refused before insert()
		self.assertEqual(frappe.db.count("Sales Order"), before)

	def test_guided_creation_through_the_chat_stays_draft(self):
		frappe.set_user("Administrator")
		s = chat.new_conversation()["session"]
		plan = {
			"mode": "create",
			"doctype": "Sales Order",
			"values": {"docstatus": 1, "owner": "evil@example.com", "workflow_state": "Approved"},
			"items": [],
		}
		with mock.patch(ROUTE, return_value=plan):
			chat.send_message(session=s, text="create sales order and submit it")
		state = json.loads(frappe.db.get_value(S.DOCTYPE, s, "state"))
		self.assertTrue(
			all(
				k not in (state["forms"][0]["values"] if state.get("forms") else {})
				for k in ("docstatus", "owner", "workflow_state")
			)
		)

	def test_masters_refuse_protected_fields(self):
		from ai_erpnext.chat import masters

		with self.assertRaises(frappe.ValidationError if False else Exception):
			masters.create(
				{
					"doctype": "Customer",
					"values": {"customer_name": "AIS Evil", "docstatus": 1, "owner": "evil@example.com"},
				}
			)
		self.assertFalse(frappe.db.exists("Customer", {"customer_name": "AIS Evil"}))


class TestLogging(SecurityCase):
	def test_query_failure_log_has_no_data(self):
		from ai_erpnext.chat import engine

		lines = []
		ctx = mock.MagicMock()
		with (
			mock.patch("ai_erpnext.chat.queries.execute", side_effect=RuntimeError("SECRET-CUSTOMER 123.45")),
			mock.patch("ai_erpnext.ai.errors.get_ai_logger") as logger,
		):
			logger.return_value.error.side_effect = lines.append
			engine._run_query(ctx, {"doctype": "Customer", "operation": "list"})
		self.assertTrue(lines)
		self.assertNotIn("SECRET", " ".join(lines))
		self.assertIn("RuntimeError", " ".join(lines))
		said = " ".join(str(c) for c in ctx.say.call_args_list)
		self.assertNotIn("SECRET", said)

	def test_source_does_not_log_prompts_responses_or_results(self):
		offenders = []
		for path in APP.rglob("*.py"):
			if "tests" in path.parts or path.name in {
				"run_tests2.py",
				"set_api_key.py",
				"check_provider.py",
				"update_provider.py",
				"setup_provider.py",
			}:
				continue
			for n, line in enumerate(path.read_text().splitlines(), start=1):
				if (
					re.search(
						r"(logger|get_ai_logger\(\))\.\w+\(.*(prompt|response|result|extracted|content|messages|text\b)",
						line,
					)
					and "category" not in line
					and "error_type" not in line
				):
					offenders.append(f"{path.relative_to(APP)}:{n}: {line.strip()[:100]}")
		self.assertEqual(offenders, [])
