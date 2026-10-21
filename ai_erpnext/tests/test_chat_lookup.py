"""Field questions about real records, record resolution and the lookup clarification loop.

"What is the mobile number of Amit Patel?" must resolve the record and answer the field, report
an empty field plainly, offer a choice when several records match and complete the lookup with
the user's next reply instead of asking the same question again. The router model is mocked
(and must not be called for messages the deterministic layer handles); queries run against the
site database inside the rolled-back test transaction.

Run: bench --site <site> run-tests --module ai_erpnext.tests.test_chat_lookup
"""

import json
from unittest import mock

import frappe
from frappe.tests import IntegrationTestCase

from ai_erpnext.ai.errors import AIProviderError
from ai_erpnext.api import chat
from ai_erpnext.chat import llm

ROUTE = "ai_erpnext.chat.llm.route"
NO_MODEL = AssertionError("this message must not reach the AI model")


class TestChatLookup(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		cls.company = frappe.db.get_single_value("Global Defaults", "default_company")
		cls.currency = frappe.get_cached_value("Company", cls.company, "default_currency")
		group = frappe.db.get_value("Customer Group", {"is_group": 0}, "name")
		territory = frappe.db.get_value("Territory", {"is_group": 0}, "name")
		sgroup = frappe.db.get_value("Supplier Group", {"is_group": 0}, "name")

		def customer(name, **extra):
			return (
				frappe.get_doc(
					{
						"doctype": "Customer",
						"customer_name": name,
						"customer_group": group,
						"territory": territory,
						**extra,
					}
				)
				.insert()
				.name
			)

		cls.amit = customer("Amit Patel", mobile_no="9876500001", email_id="amit.patel@example.com")
		cls.palmer = customer("Palmer Productions Ltd.")
		cls.ravis = [customer("Ravizz Kumar Traders"), customer("Ravizz Kumar Retail")]
		cls.dual_customer = customer("AIL Dual Name")
		cls.dual_supplier = (
			frappe.get_doc(
				{"doctype": "Supplier", "supplier_name": "AIL Dual Name", "supplier_group": sgroup}
			)
			.insert()
			.name
		)
		cls.supplier = (
			frappe.get_doc(
				{"doctype": "Supplier", "supplier_name": "AIL Northwind Supply", "supplier_group": sgroup}
			)
			.insert()
			.name
		)
		ig = frappe.db.get_value("Item Group", {"is_group": 0}, "name")
		frappe.get_doc(
			{
				"doctype": "Item",
				"item_code": "AIL-BOTTLE",
				"item_name": "AIL Bottle",
				"item_group": ig,
				"stock_uom": "Nos",
				"is_stock_item": 0,
				"is_sales_item": 1,
				"is_purchase_item": 1,
			}
		).insert()
		cls.tax_account = cls._tax_account()
		cls.si = frappe.get_doc(
			{
				"doctype": "Sales Invoice",
				"customer": cls.amit,
				"company": cls.company,
				"currency": cls.currency,
				"items": [{"item_code": "AIL-BOTTLE", "qty": 4, "rate": 25}],
				"taxes": [
					{
						"charge_type": "On Net Total",
						"account_head": cls.tax_account,
						"description": "AIL VAT",
						"rate": 10,
					}
				],
			}
		).insert()
		cls.si.submit()
		cls.draft_si = (
			frappe.get_doc(
				{
					"doctype": "Sales Invoice",
					"customer": cls.palmer,
					"company": cls.company,
					"currency": cls.currency,
					"items": [{"item_code": "AIL-BOTTLE", "qty": 1, "rate": 10}],
				}
			)
			.insert()
			.name
		)

	@classmethod
	def _tax_account(cls):
		parent = frappe.db.get_value(
			"Account",
			{
				"company": cls.company,
				"is_group": 1,
				"root_type": "Liability",
				"account_name": ["like", "%Duties and Taxes%"],
			},
		) or frappe.db.get_value("Account", {"company": cls.company, "is_group": 1, "root_type": "Liability"})
		return (
			frappe.get_doc(
				{
					"doctype": "Account",
					"account_name": "AIL VAT",
					"parent_account": parent,
					"company": cls.company,
					"account_type": "Tax",
					"tax_rate": 10,
				}
			)
			.insert()
			.name
		)

	def setUp(self):
		frappe.set_user("Administrator")
		self.route = mock.patch(ROUTE, side_effect=NO_MODEL).start()
		mock.patch("frappe.enqueue").start()
		self.addCleanup(mock.patch.stopall)
		self.s = chat.new_conversation()["session"]

	# helpers ----------------------------------------------------------------------------------
	def say(self, text, context=None, client_id=None):
		return chat.send_message(
			session=self.s, text=text, context=json.dumps(context) if context else None, client_id=client_id
		)

	def state(self):
		return json.loads(frappe.db.get_value("AI Chat Session", self.s, "state") or "{}")

	def click(self, value):
		q = self.state()["pending"]
		return chat.send_message(
			session=self.s, action=json.dumps({"question_id": q["id"], "value": value, "label": value})
		)

	def text(self, r):
		return "\n".join(m.get("text") or "" for m in r["messages"] if m["role"] == "assistant")

	# ---- specific fields -----------------------------------------------------------------------
	def test_mobile_number_of_a_named_customer(self):
		r = self.say("What is the mobile number of Amit Patel?")
		last = r["messages"][-1]
		self.assertEqual(last["source"], "erpnext")
		self.assertEqual(last["text"], "The mobile number of customer Amit Patel is 9876500001.")
		self.assertEqual([row["label"] for row in last["card"]["rows"]], ["Mobile No"])  # only what was asked
		self.assertNotIn("amit.patel@example.com", json.dumps(r))
		self.route.assert_not_called()

	def test_name_matching_ignores_case_spacing_and_word_endings(self):
		self.assertIn("9876500001", self.text(self.say("mobile of   amit   PATEL")))
		r = self.say("What is the email of amit patel")
		self.assertIn("amit.patel@example.com", self.text(r))
		r = self.say(
			"mobile of palmer production Ltd."
		)  # “Productions” and the final dot as typed in the screenshot
		self.assertIn(
			"I found one customer matching “palmer production Ltd”: Palmer Productions Ltd.", self.text(r)
		)
		self.assertIn("was found, but no mobile number is recorded", self.text(r))
		self.assertNotIn("None", self.text(r))

	def test_empty_field_is_reported_not_invented(self):
		r = self.say("Palmer Productions Ltd.'s email")
		self.assertEqual(
			r["messages"][-1]["text"],
			"Customer Palmer Productions Ltd. was found, but no email address is recorded.",
		)

	def test_several_matches_offer_a_choice_and_the_choice_completes_the_question(self):
		r = self.say("mobile of ravizz kumar")
		pending = self.state()["pending"]
		self.assertEqual(pending["kind"], "query_pick")
		self.assertEqual(
			sorted(o["value"] for o in pending["options"]), sorted(f"Customer::{c}" for c in self.ravis)
		)
		self.assertNotIn("Ravizz Kumar Retail", r["messages"][-1]["text"])  # record names only in the buttons
		r = self.click(f"Customer::{self.ravis[1]}")
		self.assertEqual(
			r["messages"][-1]["text"],
			f"Customer {self.ravis[1]} was found, but no mobile number is recorded.",
		)
		self.assertIsNone(self.state()["pending"])
		# typed ordinals work too, and a pick never reaches the model
		self.say("mobile of ravizz kumar")
		options = self.state()["pending"]["options"]
		r = self.say("no, use the second one")
		self.assertIn(options[1]["value"].split("::")[1], r["messages"][-1]["text"])
		self.route.assert_not_called()

	def test_same_name_as_customer_and_supplier_asks_which(self):
		self.say("email of AIL Dual Name")
		pending = self.state()["pending"]
		self.assertEqual(pending["kind"], "query_pick")
		self.assertEqual(
			{o["value"] for o in pending["options"]},
			{f"Customer::{self.dual_customer}", f"Supplier::{self.dual_supplier}"},
		)
		self.assertTrue(all(o["label"].startswith(("Customer:", "Supplier:")) for o in pending["options"]))
		r = self.click(f"Supplier::{self.dual_supplier}")
		self.assertEqual(
			r["messages"][-1]["text"], "Supplier AIL Dual Name was found, but no email address is recorded."
		)

	def test_supplier_lookup_by_exact_name(self):
		r = self.say("email of supplier AIL Northwind Supply")
		self.assertEqual(
			r["messages"][-1]["text"],
			"Supplier AIL Northwind Supply was found, but no email address is recorded.",
		)

	def test_customer_details_card(self):
		r = self.say("Show Amit Patel's customer details.")
		card = r["messages"][-1]["card"]
		rows = {row["label"]: row["value"] for row in card["rows"]}
		self.assertEqual((card["type"], card["name"]), ("record", self.amit))
		self.assertEqual(rows["Mobile No"], "9876500001")
		self.assertEqual(
			rows["Outstanding (submitted Sales Invoices)"], f"110.0 {self.currency} in 1 invoice(s)"
		)

	def test_not_found_is_clear_and_the_next_reply_completes_the_lookup(self):
		r = self.say("What is the mobile number of Amitt Pattel?")
		self.assertIn(
			"I couldn't find a customer, supplier or contact matching “Amitt Pattel” that you can access.",
			self.text(r),
		)
		self.assertEqual(self.state()["pending"]["kind"], "query_target")
		r = self.say("Amit Patel")  # the corrected name answers the same question (mobile), no model call
		self.assertEqual(r["messages"][-1]["text"], "The mobile number of customer Amit Patel is 9876500001.")
		self.assertIsNone(self.state()["pending"])
		self.route.assert_not_called()

	def test_failed_lookups_stop_after_a_bounded_number_of_tries(self):
		self.say("mobile of Zzq Nobody")
		self.say("Zzq Nobody Two")
		r = self.say("Zzq Nobody Three")
		self.assertIn("I've stopped searching for it", self.text(r))
		self.assertIsNone(self.state()["pending"])

	def test_a_new_request_replaces_an_open_lookup_question(self):
		self.say("mobile of Zzq Nobody")
		self.route.side_effect = None
		self.route.return_value = {"mode": "chat", "reply": "A Sales Order is a confirmed order."}
		r = self.say("What is a cost center?")
		self.assertEqual(self.route.call_count, 1)
		self.assertEqual(r["messages"][-1]["source"], "general")
		self.assertIsNone(self.state()["pending"])

	# ---- the screenshot loop: model plans that used to dead-end ------------------------------------
	def test_model_plan_with_party_name_resolves_instead_of_asking(self):
		self.route.side_effect = None
		self.route.return_value = {
			"mode": "query",
			"query": {
				"doctype": "Customer",
				"operation": "details",
				"filters": {"party": "amit patel"},
				"fields": ["mobile_no"],
			},
		}
		r = self.say("tell me amit patel mobile please")
		self.assertEqual(r["messages"][-1]["text"], "The mobile number of customer Amit Patel is 9876500001.")

	def test_which_customer_question_is_answered_by_the_next_message(self):
		self.route.side_effect = None
		self.route.return_value = {
			"mode": "query",
			"query": {"doctype": "Customer", "operation": "details", "fields": ["mobile"]},
		}
		r = self.say("customer mobile number please")
		self.assertEqual(r["messages"][-1]["text"], "Which Customer do you mean? Type its name or ID.")
		self.assertEqual(self.state()["pending"]["kind"], "query_target")
		self.route.side_effect = NO_MODEL
		r = self.say("amit patel")
		self.assertEqual(r["messages"][-1]["text"], "The mobile number of customer Amit Patel is 9876500001.")
		self.assertEqual(
			self.route.call_count, 1
		)  # the reply completed the lookup; no second model call, no repeat

	def test_model_repeating_the_same_clarification_is_not_repeated(self):
		self.route.side_effect = None
		self.route.return_value = {"mode": "clarify", "question": "Which customer should I show?"}
		r = self.say("I need that buyer from yesterday")
		self.assertEqual(r["messages"][-1]["text"], "Which customer should I show?")
		r = self.say("the big one")
		self.assertNotEqual(r["messages"][-1]["text"], "Which customer should I show?")
		self.assertIn("won't ask the same question again", r["messages"][-1]["text"])

	# ---- documents ----------------------------------------------------------------------------
	def test_items_taxes_status_and_outstanding_of_an_invoice(self):
		r = self.say(f"Which items are in Sales Invoice {self.si.name}?")
		self.assertIn(f"Sales Invoice {self.si.name} has 1 item row:", self.text(r))
		self.assertIn("4.0 Nos × AIL Bottle (AIL-BOTTLE) at 25.0 = 100.0", self.text(r))
		r = self.say(f"Show me the tax breakdown of {self.si.name}")
		self.assertIn("AIL VAT (10.0%): 10.0", self.text(r))
		self.assertIn(f"Total taxes and charges: 10.0 {self.currency}; grand total 110.0", self.text(r))
		r = self.say(f"status of invoice {self.si.name}")
		self.assertIn(f"The status of Sales Invoice {self.si.name} is Unpaid.", self.text(r))
		r = self.say(f"outstanding amount of {self.si.name}")
		self.assertIn(f"is 110.0 {self.currency}", self.text(r))
		r = self.say(f"outstanding amount of {self.draft_si}")
		self.assertIn("is a Draft, so nothing is outstanding until it is submitted", self.text(r))
		self.route.assert_not_called()

	def test_outstanding_of_a_customer(self):
		self.assertIn(
			f"Outstanding for customer Amit Patel: 110.0 {self.currency} in 1 invoice(s)",
			self.text(self.say("What is the outstanding amount for Amit Patel?")),
		)
		r = self.say("What is the outstanding amount for Palmer Productions Ltd.?")
		self.assertIn("has no outstanding amount on submitted Sales Invoices you can access", self.text(r))

	def test_this_document_uses_the_open_form_or_the_last_result(self):
		r = self.say(
			"What is the status of this Sales Invoice?",
			context={"route": ["Form", "Sales Invoice", self.si.name]},
		)
		self.assertIn(f"The status of Sales Invoice {self.si.name} is Unpaid.", self.text(r))
		self.say(f"Which items are in Sales Invoice {self.draft_si}?")  # now the last single result
		r = self.say("show me the tax breakdown of this invoice")
		self.assertIn(f"Sales Invoice {self.draft_si} has no tax rows", self.text(r))
		other = chat.new_conversation()["session"]
		r = chat.send_message(session=other, text="What is the delivery date of this Sales Order?")
		self.assertEqual(r["messages"][-1]["text"], "Which Sales Order do you mean? Type its name or ID.")

	def test_open_form_context_is_checked_against_permissions(self):
		r = self.say(
			"status of this sales invoice", context={"route": ["Form", "Sales Invoice", "NOPE-0001"]}
		)
		self.assertIn("Which Sales Invoice do you mean?", self.text(r))
		r = self.say("status of this sales invoice", context={"route": ["Form", "User", "Administrator"]})
		self.assertNotIn("Administrator", self.text(r))

	def test_count_of_outstanding_invoices(self):
		r = self.say("How many submitted Sales Invoices are outstanding?")
		n = frappe.db.count("Sales Invoice", {"docstatus": 1, "outstanding_amount": [">", 0], "is_return": 0})
		self.assertIn(f"There {'is' if n == 1 else 'are'} {n} Sales Invoice record", self.text(r))
		self.assertIn("with an outstanding amount", self.text(r))

	def test_unsupported_field_is_said_plainly(self):
		r = self.say("phone of item AIL-BOTTLE")
		self.assertEqual(r["messages"][-1]["text"], "I can't show phone for Item records.")
		self.route.side_effect = None
		self.route.return_value = {
			"mode": "query",
			"query": {
				"doctype": "Item",
				"operation": "details",
				"filters": {"search": "AIL-BOTTLE"},
				"fields": ["mobile"],
			},
		}
		r = self.say("need the mobile for item AIL-BOTTLE")
		self.assertEqual(r["messages"][-1]["text"], "I can't show mobile for Item records.")

	# ---- permissions and data minimisation ------------------------------------------------------
	def test_lookups_only_use_doctypes_the_user_may_read(self):
		from ai_erpnext.chat import queries

		email = "ail-buyer@example.com"
		if not frappe.db.exists("User", email):
			frappe.get_doc(
				{
					"doctype": "User",
					"email": email,
					"first_name": "AIL Buyer",
					"send_welcome_email": 0,
					"roles": [{"role": "Purchase User"}],
				}
			).insert(ignore_permissions=True)
		frappe.set_user(email)
		try:
			self.assertFalse(frappe.has_permission("Customer", "read"))
			plan = {
				"doctype": "Customer",
				"operation": "details",
				"filters": {"search": "Amit Patel"},
				"fields": ["mobile"],
			}
			with self.assertRaises(queries.QueryError) as ctx:
				queries.execute(queries.validate(plan))
			self.assertEqual(str(ctx.exception), "You don't have permission to view Customer records.")
			try:
				result = queries.execute(queries.validate(dict(plan, doctype="auto")))
			except queries.QueryError:
				result = {"text": "", "result_doctype": None}
			self.assertNotEqual(result.get("result_doctype"), "Customer")  # never the customer record itself
			if result.get("result_doctype") != "Contact" or not frappe.has_permission("Contact", "read"):
				self.assertNotIn("9876500001", result["text"])
		finally:
			frappe.set_user("Administrator")

	def test_stored_plans_are_rechecked_against_the_allowlist(self):
		from ai_erpnext.chat import queries

		tampered = queries.validate(
			{"doctype": "Customer", "operation": "list", "filters": {"search": "Amit Patel"}}
		)
		tampered["fields"] = ["name", "customer_name", "credit_limits", "owner", "modified_by"]
		result = queries.execute(tampered)
		self.assertEqual([c["key"] for c in result["card"]["columns"]], ["name", "customer_name"])
		with self.assertRaises(queries.QueryError):
			queries.execute(dict(tampered, doctype="User"))
		with self.assertRaises(queries.QueryError):
			queries.execute(dict(tampered, operation="delete"))

	def test_record_data_never_reaches_the_model_history(self):
		self.say("What is the mobile number of Amit Patel?")
		self.say(f"Which items are in Sales Invoice {self.si.name}?")
		msgs = json.loads(frappe.db.get_value("AI Chat Session", self.s, "messages"))
		history = json.dumps(llm.router_history(msgs))
		for secret in ("9876500001", "AIL Bottle", "110.0"):
			self.assertNotIn(secret, history)

	def test_duplicate_delivery_is_not_processed_twice(self):
		first = self.say("mobile of amit patel", client_id="dup-1")
		seq = self.state()["seq"]
		again = self.say("mobile of amit patel", client_id="dup-1")
		self.assertEqual(again["messages"], first["messages"])
		self.assertEqual(self.state()["seq"], seq)

	def test_known_questions_get_built_in_guidance_when_the_ai_is_down(self):
		self.route.side_effect = AIProviderError("quota_exhausted")
		r = self.say("How do I create a Purchase Order?")
		last = r["messages"][-1]
		self.assertEqual(last["source"], "guide")
		self.assertIn("To create a Purchase Order in ERPNext:", last["text"])
		self.assertIn("create a Draft Purchase Order step by step", last["text"])
		self.assertIn("usage limit has been reached", last["text"])  # and the user is told why
		r = self.say("What does the Stock module do?")
		self.assertIn("The Stock module tracks items", r["messages"][-1]["text"])
		self.assertIn("I can look up", r["messages"][-1]["text"])
		r = self.say("What is the difference between a Purchase Receipt and a Purchase Invoice?")
		self.assertIn("records the goods you received", r["messages"][-1]["text"])
