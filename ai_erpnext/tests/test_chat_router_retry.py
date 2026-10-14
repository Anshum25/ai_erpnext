"""Chat router: root-cause fix (JSON-framed history), plain-text salvage and the bounded retry policy.
No network: a fake provider (or a mocked HTTP transport) stands in for Groq.

Run: bench --site <site> run-tests --module ai_erpnext.tests.test_chat_router_retry
"""

import json
from unittest import mock

import frappe
import httpx
import openai
from frappe.tests import IntegrationTestCase

from ai_erpnext.ai.errors import AIProviderError, classify
from ai_erpnext.ai.openai_provider import OpenAIProvider
from ai_erpnext.api import chat
from ai_erpnext.chat import llm

GROQ_URL = "https://api.groq.com/openai/v1"
REQ = httpx.Request("POST", GROQ_URL + "/chat/completions")
CHAT_PLAN = {"mode": "chat", "reply": "A Sales Invoice is a bill sent to a customer."}
PROSE = "A Sales Invoice is a document you create when you bill a customer."


def err(category, **kw):
	return AIProviderError(category, **kw)


class FakeProvider:
	"""Returns/raises the scripted outcomes in order and records every call."""

	def __init__(self, *outcomes):
		self.outcomes, self.calls = list(outcomes), []

	def complete_json(self, system, messages, sdk_retries=None):
		self.calls.append({"messages": messages, "sdk_retries": sdk_retries})
		outcome = self.outcomes.pop(0)
		if isinstance(outcome, Exception):
			raise outcome
		return outcome


def run_policy(provider):
	sleeps = []
	result = llm.complete_with_policy(provider, "system", [{"role": "user", "content": "q"}], sleep=sleeps.append, jitter=lambda: 1.0)
	return result, sleeps


class TestRouterRetryPolicy(IntegrationTestCase):
	def test_valid_structured_output_single_call(self):
		p = FakeProvider(CHAT_PLAN)
		result, sleeps = run_policy(p)
		self.assertEqual((result, len(p.calls), sleeps), (CHAT_PLAN, 1, []))
		self.assertEqual(p.calls[0]["sdk_retries"], 0)  # the router owns retries: SDK retries disabled

	def test_plain_text_answer_gets_one_correction_then_is_accepted(self):
		p = FakeProvider(err("invalid_response", status_code=400, failed_generation=PROSE), CHAT_PLAN)
		result, _ = run_policy(p)
		self.assertEqual(result, CHAT_PLAN)
		self.assertEqual(p.calls[1]["messages"][-1]["content"], llm.CORRECTION)
		p = FakeProvider(*[err("invalid_response", status_code=400, failed_generation=PROSE)] * 3)
		result, _ = run_policy(p)
		self.assertEqual(result, {"mode": "chat", "reply": PROSE})
		self.assertEqual(len(p.calls), 2)  # prose twice: accepted, no third call

	def test_rejected_output_that_is_valid_json_is_used_directly(self):
		fg = json.dumps({"mode": "query", "query": {"doctype": "Customer", "operation": "count"}})
		p = FakeProvider(err("invalid_response", status_code=400, failed_generation="```json\n" + fg + "\n```"))
		result, _ = run_policy(p)
		self.assertEqual((result["mode"], len(p.calls)), ("query", 1))

	def test_empty_output_is_retried_with_correction(self):
		p = FakeProvider(err("invalid_response", status_code=200, failed_generation=""), CHAT_PLAN)
		result, sleeps = run_policy(p)
		self.assertEqual((result, len(p.calls), sleeps), (CHAT_PLAN, 2, []))
		self.assertEqual(p.calls[1]["messages"][-1]["content"], llm.CORRECTION)

	def test_malformed_output_respects_attempt_limit(self):
		bad = err("invalid_response", status_code=400, failed_generation='{"mode": "chat", "reply": "unterminated')
		p = FakeProvider(bad, bad, bad, CHAT_PLAN)
		with self.assertRaises(AIProviderError) as ctx:
			run_policy(p)
		self.assertEqual((ctx.exception.category, len(p.calls)), ("invalid_response", llm.MAX_ATTEMPTS))

	def test_timeout_retries_with_backoff(self):
		p = FakeProvider(err("timeout"), CHAT_PLAN)
		result, sleeps = run_policy(p)
		self.assertEqual((result, len(p.calls)), (CHAT_PLAN, 2))
		self.assertEqual(sleeps, [llm.BACKOFF_BASE_S])  # base * 2^0, full jitter factor 1.0

	def test_5xx_stops_at_attempt_limit_within_budget(self):
		p = FakeProvider(*[err("unavailable", status_code=503)] * 5)
		with self.assertRaises(AIProviderError) as ctx:
			_, sleeps = run_policy(p)
		self.assertEqual((ctx.exception.category, len(p.calls)), ("unavailable", 3))

	def test_429_waits_for_retry_after_only_within_budget(self):
		p = FakeProvider(err("rate_limited", status_code=429, retry_after=2.0), CHAT_PLAN)
		result, sleeps = run_policy(p)
		self.assertEqual((result, len(p.calls)), (CHAT_PLAN, 2))
		self.assertAlmostEqual(sleeps[0], 2.25)
		p = FakeProvider(err("rate_limited", status_code=429, retry_after=60.0), CHAT_PLAN)
		with self.assertRaises(AIProviderError) as ctx:
			run_policy(p)
		self.assertEqual((ctx.exception.category, len(p.calls)), ("rate_limited", 1))
		p = FakeProvider(err("rate_limited", status_code=429), CHAT_PLAN)  # no retry-after: don't guess
		with self.assertRaises(AIProviderError):
			run_policy(p)
		self.assertEqual(len(p.calls), 1)

	def test_quota_and_permanent_errors_are_never_retried(self):
		for category, status in (("quota_exhausted", 429), ("auth", 401), ("config", 404), ("bad_request", 400), ("billing", 402)):
			p = FakeProvider(err(category, status_code=status, retry_after=1.0), CHAT_PLAN)
			with self.assertRaises(AIProviderError) as ctx:
				run_policy(p)
			self.assertEqual((ctx.exception.category, len(p.calls)), (category, 1), category)


class TestProviderErrorDetails(IntegrationTestCase):
	def _status_error(self, cls, status, message, headers=None, body=None):
		return cls(message, response=httpx.Response(status, request=REQ, headers=headers or {}), body=body or {"error": {"message": message}})

	def test_classify_reads_retry_after_and_failed_generation(self):
		e = classify(self._status_error(openai.RateLimitError, 429, "Rate limit reached on tokens per minute (TPM)", headers={"retry-after": "7"}))
		self.assertEqual((e.category, e.retry_after), ("rate_limited", 7.0))
		e = classify(self._status_error(openai.RateLimitError, 429, "limit on tokens per minute", headers={"x-ratelimit-reset-tokens": "1m26.4s"}))
		self.assertAlmostEqual(e.retry_after, 86.4)
		e = classify(self._status_error(openai.RateLimitError, 429, "Rate limit reached on tokens per day (TPD)", headers={"retry-after": "3"}))
		self.assertEqual(e.category, "quota_exhausted")
		body = {"error": {"message": "Failed to generate JSON", "code": "json_validate_failed", "failed_generation": PROSE}}
		e = classify(self._status_error(openai.BadRequestError, 400, "json_validate_failed", body=body))
		self.assertEqual((e.category, e.failed_generation), ("invalid_response", PROSE))
		self.assertNotIn(PROSE, str(e))  # the user-facing message never contains model output

	def test_sdk_retries_disabled_for_router_calls(self):
		"""A 503 costs exactly one HTTP request when the router owns retries (no multiplication).
		The OpenAI SDK uses its bundled httpx2; its transport is mocked so nothing reaches the network."""
		import httpx2

		requests = []

		def transport(self, request):
			requests.append(request)
			return httpx2.Response(503, request=request, json={"error": {"message": "unavailable"}})

		p = OpenAIProvider(api_key="test-key", base_url=GROQ_URL, model="m", provider_name="Groq")
		with mock.patch.object(httpx2.HTTPTransport, "handle_request", transport), mock.patch("time.sleep"):
			with self.assertRaises(AIProviderError) as ctx:
				p.complete_json("system", [{"role": "user", "content": "hi"}], sdk_retries=0)
			self.assertEqual((ctx.exception.category, len(requests)), ("unavailable", 1))
			requests.clear()
			with self.assertRaises(AIProviderError):
				p.complete_json("system", [{"role": "user", "content": "hi"}])  # SDK default, for contrast
			self.assertEqual(len(requests), 1 + OpenAIProvider.MAX_RETRIES)


class TestRouterEndToEnd(IntegrationTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		self.s = chat.new_conversation()["session"]
		self.counts = {dt: frappe.db.count(dt) for dt in ("Sales Invoice", "Purchase Invoice", "Customer", "Item")}

	def tearDown(self):
		self.assertEqual({dt: frappe.db.count(dt) for dt in self.counts}, self.counts)  # no business records touched

	def ask(self, text, provider):
		with mock.patch("ai_erpnext.chat.llm.get_text_provider", return_value=provider), mock.patch("time.sleep"):
			return chat.send_message(session=self.s, text=text)

	def test_history_is_sent_as_json_and_prose_failure_recovers(self):
		self.ask("hi", FakeProvider({"mode": "chat", "reply": "Hello! How can I help?"}))
		p = FakeProvider(err("invalid_response", status_code=400, failed_generation=PROSE), CHAT_PLAN)
		with self.assertLogs("ai_erpnext", level="INFO") as logs:
			r = self.ask("what is sales invoice??", p)
		self.assertEqual(r["messages"][-1]["source"], "general")
		self.assertIn("bill sent to a customer", r["messages"][-1]["text"])
		assistant_turns = [m for m in p.calls[0]["messages"] if m["role"] == "assistant"]
		self.assertTrue(assistant_turns)
		self.assertTrue(all(json.loads(m["content"])["mode"] == "chat" for m in assistant_turns))  # root-cause fix
		joined = "\n".join(logs.output)
		self.assertIn("AI-CHAT route attempts=2 outcome=ok_after_retry", joined)
		self.assertNotIn("bill sent to a customer", joined)
		self.assertNotIn(PROSE, joined)

	def test_quota_exhausted_stops_and_explains(self):
		p = FakeProvider(err("quota_exhausted", status_code=429), CHAT_PLAN)
		r = self.ask("what is a sales order?", p)
		self.assertEqual(len(p.calls), 1)
		self.assertIn("usage limit has been reached", r["messages"][-1]["text"])

	def test_unusable_output_message_is_clear(self):
		bad = err("invalid_response", status_code=400, failed_generation="")
		r = self.ask("explain stock entry", FakeProvider(bad, bad, bad))
		self.assertIn("couldn't produce a usable answer", r["messages"][-1]["text"])
