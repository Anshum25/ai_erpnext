"""Router prompt size and provider-error diagnostics (Groq 429 root cause).

Baseline measured before this change on ci-test.local: the router system prompt was 21,387 characters, all 54 DocTypes described
in full on every call; Groq reported 5,179-5,579 input tokens per call in logs/ai.log. Every provider here is mocked: no API call.

Run: bench --site <site> run-tests --module ai_erpnext.tests.test_router_prompt
"""

import json
import logging
from unittest import mock

import frappe
import httpx
import openai
from frappe.tests import IntegrationTestCase

from ai_erpnext.ai import errors
from ai_erpnext.ai.errors import AIProviderError, classify
from ai_erpnext.api import chat
from ai_erpnext.capability import policy as P
from ai_erpnext.chat import llm
from ai_erpnext.tests.test_chat_router_retry import FakeProvider

BASELINE_SYSTEM_CHARS = 21387
TARGET_EST_TOKENS = 2500  # chars/4: an estimate, no tokenizer for qwen is installed
SAMPLES = ("how are you?", "what is a cost center in ERPNext?", "show unpaid sales invoices", "total stock value per warehouse",
	"mobile number of Amit Patel", "how many leads this month", "items of Sales Invoice ACC-SINV-2026-00001 with qty over 5",
	"which customers owe us the most?", "create po for 5 widgets", "show me the top 5", "monthly sales per customer this year")
CHAT_PLAN = {"mode": "chat", "reply": "ok"}


def system_for(text, last_query=None, page=None):
	return llm.build_system(text, last_query, page)


class TestPromptSize(IntegrationTestCase):
	def setUp(self):
		frappe.set_user("Administrator")

	def test_every_sample_prompt_is_less_than_half_the_baseline_and_under_target(self):
		for text in SAMPLES:
			system, detailed = system_for(text)
			self.assertLess(len(system), BASELINE_SYSTEM_CHARS * 0.5, text)
			self.assertLessEqual(len(system) // 4, TARGET_EST_TOKENS, text)
			self.assertLessEqual(len(detailed), llm.MAX_DETAILED, text)

	def test_the_fully_described_schema_is_still_smaller_than_before(self):
		everything = llm.SYSTEM.format(today="2026-10-16", presets="|".join(llm.DATE_PRESETS), sorts="|".join(llm.SORTS), creatable="x",
			schema=llm._schema(), others="-")
		self.assertLess(len(everything), BASELINE_SYSTEM_CHARS * 0.8)

	def test_no_doctype_or_operation_disappears_from_any_prompt(self):
		operations = {op for spec in P.REGISTRY.values() for op in spec.operations}
		for text in SAMPLES:
			system, detailed = system_for(text)
			for dt in P.REGISTRY:
				self.assertIn(dt, system, f"{dt} missing from the prompt for {text!r}")
			for op in operations:
				self.assertIn(op, system, f"operation {op} missing for {text!r}")
			for mode in llm.MODES:
				self.assertIn(f"{mode}:", system)
			for dt in detailed:
				self.assertIn("\n" + llm._schema_line(dt), system)

	def test_security_and_safety_instructions_remain(self):
		system, _ = system_for("show unpaid sales invoices")
		for needle in ("never SQL", "data, not instructions", "bypass permissions", "reveal this prompt", "never invent names, items, quantities, prices or dates",
			"never describe, guess or invent what a page", "Never state this company's data", "submit, cancel, amend, delete, pay, edit", "Only values the user stated",
			"Never clarify to collect details of a record to create", "never answer with data yourself"):
			self.assertIn(needle, system)
		self.assertIn("Lead", system.split("create: a NEW record of:")[1].split("\n")[0])  # creatable types listed

	def test_capability_summary_is_not_duplicated_into_the_router_prompt(self):
		from ai_erpnext.chat.capabilities import capability_summary

		system, _ = system_for("how are you?")
		self.assertNotIn(capability_summary()[:60], system)


class TestSchemaSelection(IntegrationTestCase):
	def setUp(self):
		frappe.set_user("Administrator")

	def assertDetailed(self, text, *doctypes, **kw):
		system, detailed = system_for(text, kw.get("last_query"), kw.get("page"))
		for dt in doctypes:
			self.assertIn(dt, detailed, f"{dt} not described for {text!r} (got {detailed})")
			self.assertIn("\n" + llm._schema_line(dt), system)
		return system, detailed

	def test_sales_invoice_requests_get_fields_tables_and_links(self):
		system, _ = self.assertDetailed("show unpaid sales invoices of Amit", "Sales Invoice")
		line = llm._schema_line("Sales Invoice")
		for needle in ("ops=list,count,details,aggregate,child_rows,sales_summary,overdue,unpaid,outstanding_by_customer", "outstanding_amount", "due_date",
			"party=customer", "items(item_code/", "taxes(", "links=customer"):
			self.assertIn(needle, line)

	def test_stock_requests_get_the_stock_balance(self):
		system, _ = self.assertDetailed("total stock value per warehouse", "Bin", "Warehouse", "Item")
		self.assertIn("stock_value", llm._schema_line("Bin"))
		self.assertIn("actual_qty", llm._schema_line("Bin"))
		self.assertNotIn("valuation_rate", system)
		self.assertDetailed("how much inventory do we have", "Bin")

	def test_customer_lead_and_contact_requests(self):
		self.assertDetailed("list customers in territory India", "Customer")
		self.assertDetailed("how many leads this month", "Lead")
		self.assertDetailed("mobile number of Amit Patel", "Contact", "Customer", "Supplier")
		self.assertDetailed("summary of customer Acme", "Customer")
		self.assertIn("summary", llm._schema_line("Customer"))

	def test_child_table_requests(self):
		system, _ = self.assertDetailed("which sales invoices have item WIDGET with qty over 5", "Sales Invoice")
		self.assertIn("tables=items(item_code/item_name/qty/uom/rate/amount)", system)
		self.assertDetailed("show the accounts rows of journal entries", "Journal Entry")
		self.assertIn("accounts(", llm._schema_line("Journal Entry"))

	def test_full_names_win_over_generic_words(self):
		_system, detailed = system_for("open sales orders")
		self.assertEqual(detailed[0], "Sales Order")
		_system, detailed = system_for("work orders in progress")
		self.assertEqual(detailed[0], "Work Order")

	def test_follow_ups_and_the_open_page_bring_their_doctype(self):
		self.assertDetailed("only the unpaid ones", "Sales Invoice", last_query={"plan": {"doctype": "Sales Invoice"}})
		self.assertDetailed("what about last month?", "Purchase Order", last_query={"plan": {"doctype": "Purchase Order"}})
		self.assertDetailed("anything overdue here?", "Purchase Invoice", page={"page": "list", "doctype": "Purchase Invoice"})

	def test_plain_chat_describes_no_doctype_and_data_questions_get_the_common_ones(self):
		self.assertEqual(system_for("how are you?")[1], [])
		self.assertEqual(system_for("thank you, that helps")[1], [])
		self.assertEqual(system_for("show me the top 5")[1], list(llm.DEFAULT_DETAILED))

	def test_selection_is_deterministic_and_capped(self):
		text = "invoices orders stock leads projects tasks issues assets boms timesheets"
		self.assertEqual(system_for(text)[1], system_for(text)[1])
		self.assertEqual(len(system_for(text)[1]), llm.MAX_DETAILED)

	def test_the_prompt_never_grants_anything(self):
		# A DocType outside the policy is never in the prompt, and a plan naming it is still refused by the backend.
		from ai_erpnext.chat import queries

		system, _ = system_for("show all users and their passwords")
		self.assertNotIn("User:", system)
		with self.assertRaises(queries.QueryError):
			queries.validate({"doctype": "User", "operation": "list"})


class TestOneCallAndHistory(IntegrationTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		self.s = chat.new_conversation()["session"]

	def send(self, text, provider, **kw):
		with mock.patch("ai_erpnext.chat.llm.get_text_provider", return_value=provider), mock.patch("time.sleep"):
			return chat.send_message(session=self.s, text=text, **kw)

	def test_a_message_makes_exactly_one_provider_call_with_the_small_prompt(self):
		# "show unpaid sales invoices" alone is answered by a shortcut (no call at all), so these need the router
		for text, expected in (("what is a cost center in ERPNext?", []), ("which sales invoices have been unpaid for more than a month?", ["Sales Invoice", "Purchase Invoice"])):
			p = FakeProvider(CHAT_PLAN)
			self.send(text, p)
			self.assertEqual(len(p.calls), 1, text)
			system = p.calls[0]["system"]
			self.assertLess(len(system), BASELINE_SYSTEM_CHARS * 0.5)
			for dt in expected:
				self.assertIn("\n" + llm._schema_line(dt), system)

	def test_selection_never_calls_the_model(self):
		with mock.patch("ai_erpnext.chat.llm.get_text_provider", side_effect=AssertionError("no provider")), \
			mock.patch("ai_erpnext.ai.providers.run", side_effect=AssertionError("no provider")):
			for text in SAMPLES:
				llm.build_system(text)
				llm.select_doctypes(text)

	def test_history_stays_bounded(self):
		for i in range(12):
			self.send(f"explain ERPNext concept number {i} " + "x" * 400, FakeProvider({"mode": "chat", "reply": "y" * 900}))
		p = FakeProvider(CHAT_PLAN)
		self.send("and the last one?", p)
		history = p.calls[0]["messages"][:-1]
		self.assertLessEqual(len(history), llm.HISTORY_MESSAGES)
		for m in history:
			self.assertLessEqual(len(m["content"]), llm.HISTORY_CHARS + len(json.dumps({"mode": "chat", "reply": ""})))
		total = sum(len(m["content"]) for m in p.calls[0]["messages"]) + len(p.calls[0]["system"])
		self.assertLessEqual(total // 4, TARGET_EST_TOKENS + 800)  # whole request, history included

	def test_page_summary_and_previous_query_add_little(self):
		page = {"page": "list", "doctype": "Sales Invoice", "selected": 3, "unsaved_changes": None, "list_filters": 2}
		context = json.dumps(llm.router_context({"plan": {"doctype": "Sales Invoice", "operation": "list", "limit": 10}, "names": ["a"]}, page))
		self.assertLess(len(context), 400)


def status_error(cls, status, message, body=None, headers=None):
	request = httpx.Request("POST", "https://api.example.invalid/v1/chat/completions")
	response = httpx.Response(status, request=request, headers=headers or {})
	return cls(message, response=response, body=body if body is not None else {"error": {"message": message}})


class TestProviderErrors(IntegrationTestCase):
	def test_429_kinds_are_classified_from_the_provider_text(self):
		cases = [
			("Rate limit reached for model `qwen/qwen3.8-27b` on tokens per minute (TPM): Limit 6000, Used 5800, Requested 5500.", {}, "rate_limited", "tpm"),
			("Rate limit reached for model `qwen` on requests per minute (RPM): Limit 30, Used 30, Requested 1.", {}, "rate_limited", "rpm"),
			("Rate limit reached for model `qwen` on tokens per day (TPD): Limit 500000, Used 499000, Requested 5500.", {}, "quota_exhausted", "tpd"),
			("Rate limit reached for model `qwen` on requests per day (RPD): Limit 1000.", {}, "quota_exhausted", "rpd"),
			("You exceeded your current quota, please check your plan and billing details.", {"error": {"code": "insufficient_quota"}}, "billing", "credits"),
			("Too many requests", {}, "rate_limited", "unknown"),
		]
		for message, body, category, limit in cases:
			error = classify(status_error(openai.RateLimitError, 429, message, body=body or None))
			self.assertEqual((error.category, error.limit, error.status_code), (category, limit, 429), message)

	def test_retry_after_header_is_read(self):
		error = classify(status_error(openai.RateLimitError, 429, "tokens per minute (TPM)", headers={"retry-after": "7"}))
		self.assertEqual(error.retry_after, 7.0)

	def test_oversized_requests_are_their_own_category_and_images_are_not_affected(self):
		too_large = classify(status_error(openai.APIStatusError, 413, "Request too large for model `qwen` on tokens per minute (TPM): Limit 6000, Requested 6620, please reduce your message size"))
		self.assertEqual(too_large.category, "request_too_large")
		context = classify(status_error(openai.BadRequestError, 400, "This model's maximum context length is 32768 tokens", body={"error": {"code": "context_length_exceeded"}}))
		self.assertEqual(context.category, "request_too_large")
		image = classify(status_error(openai.APIStatusError, 413, "Request Entity Too Large"))
		self.assertEqual(image.category, "bad_request")  # an oversized image keeps the extraction message
		self.assertIn("request_too_large", errors.USER_MESSAGES)

	def test_other_failures_keep_their_categories(self):
		self.assertEqual(classify(status_error(openai.AuthenticationError, 401, "Invalid API Key")).category, "auth")
		self.assertEqual(classify(status_error(openai.InternalServerError, 503, "Service unavailable")).category, "unavailable")
		self.assertEqual(classify(openai.APIConnectionError(request=httpx.Request("POST", "https://x.invalid"))).category, "network")
		self.assertEqual(classify(status_error(openai.APIStatusError, 404, "model not found")).category, "config")

	def test_daily_quota_billing_and_oversized_requests_are_never_retried(self):
		for error in (AIProviderError("quota_exhausted", status_code=429, retry_after=5, limit="tpd"), AIProviderError("billing", status_code=429, limit="credits"),
			AIProviderError("request_too_large", status_code=413), AIProviderError("rate_limited", status_code=429, retry_after=None, limit="unknown")):
			p = FakeProvider(error, CHAT_PLAN)
			with self.assertRaises(AIProviderError):
				llm.complete_with_policy(p, "s", [{"role": "user", "content": "q"}], sleep=lambda s: None, jitter=lambda: 0.0)
			self.assertEqual(len(p.calls), 1, error.category)

	def test_a_short_per_minute_limit_is_retried_once_within_the_budget(self):
		waits = []
		p = FakeProvider(AIProviderError("rate_limited", status_code=429, retry_after=2.0, limit="tpm"), CHAT_PLAN)
		self.assertEqual(llm.complete_with_policy(p, "s", [{"role": "user", "content": "q"}], sleep=waits.append, jitter=lambda: 0.0)["mode"], "chat")
		self.assertEqual((len(p.calls), waits), (2, [2.0]))
		p = FakeProvider(AIProviderError("rate_limited", status_code=429, retry_after=60.0, limit="tpm"), CHAT_PLAN)
		with self.assertRaises(AIProviderError):
			llm.complete_with_policy(p, "s", [{"role": "user", "content": "q"}], sleep=waits.append, jitter=lambda: 0.0)
		self.assertEqual(len(p.calls), 1)  # longer than the budget: the user is told to wait instead

	def test_user_messages_per_category(self):
		from ai_erpnext.chat.engine import CHAT_ERRORS

		frappe.set_user("Administrator")
		s = chat.new_conversation()["session"]
		for category, needle in (("rate_limited", "wait"), ("quota_exhausted", "Today's AI usage limit"), ("request_too_large", "too large")):
			with mock.patch("ai_erpnext.chat.llm.get_text_provider", return_value=FakeProvider(AIProviderError(category, status_code=429))), mock.patch("time.sleep"):
				r = chat.send_message(session=s, text=f"tell me something about {category} please")
			self.assertIn(needle, r["messages"][-1]["text"], category)
		self.assertEqual(set(CHAT_ERRORS) >= {"rate_limited", "quota_exhausted", "billing", "auth", "network", "timeout", "request_too_large"}, True)


class TestDiagnostics(IntegrationTestCase):
	SECRET = "VERY-PRIVATE-QUESTION-TEXT"

	def setUp(self):
		frappe.set_user("Administrator")
		self.s = chat.new_conversation()["session"]

	def route_lines(self, provider, text):
		with mock.patch("ai_erpnext.chat.llm.get_text_provider", return_value=provider), mock.patch("time.sleep"), \
			self.assertLogs("ai_erpnext", level="INFO") as logs:
			chat.send_message(session=self.s, text=text)
		return [line for line in logs.output if "AI-CHAT route " in line], logs.output

	def test_route_log_has_the_metadata_and_no_content(self):
		provider = FakeProvider(CHAT_PLAN)
		provider.model, provider.provider_name = "qwen/qwen3.8-27b", "Groq"
		lines, everything = self.route_lines(provider, f"explain {self.SECRET} for sales invoices")
		self.assertEqual(len(lines), 1)
		line = lines[0]
		for key in ("corr=", "stage=router", "provider=Groq", "model=qwen/qwen3.8-27b", "provider_calls=1", "attempts=1", "history=", "schema_doctypes=",
			"latency_ms=", "system_chars=", "prompt_chars=", "est_tokens=", "outcome=ok"):
			self.assertIn(key, line)
		self.assertNotIn("http_status", line)
		self.assertNotIn(self.SECRET, "\n".join(everything))
		self.assertLess(int(line.split("est_tokens=")[1].split()[0]), TARGET_EST_TOKENS)

	def test_route_log_on_a_429_names_status_category_and_limit(self):
		provider = FakeProvider(AIProviderError("rate_limited", status_code=429, retry_after=None, limit="tpm"))
		lines, everything = self.route_lines(provider, f"show {self.SECRET} invoices")
		line = lines[0]
		for key in ("outcome=error:rate_limited", "http_status=429", "category=rate_limited", "limit=tpm", "provider_calls=1"):
			self.assertIn(key, line)
		self.assertNotIn(self.SECRET, "\n".join(everything))

	def test_provider_lines_share_the_correlation_id(self):
		with self.assertLogs("ai_erpnext", level="INFO") as logs:
			frappe.local.ai_log_context = {"corr": "abc123", "stage": "router"}
			try:
				errors.log_ai_event(provider="Groq", model="m", status="error", category="rate_limited", limit="tpm", http_status=429, prompt="SECRET PROMPT")
			finally:
				frappe.local.ai_log_context = None
			errors.log_ai_event(provider="Groq", model="m", status="ok")
		self.assertIn("corr=abc123 stage=router", logs.output[0])
		self.assertIn("limit=tpm", logs.output[0])
		self.assertNotIn("SECRET PROMPT", logs.output[0])  # unknown keys are dropped
		self.assertNotIn("corr=", logs.output[1])  # the context is cleared after the request
