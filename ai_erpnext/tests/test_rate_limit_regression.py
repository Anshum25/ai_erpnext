"""Phase C regression tests: rate limit root-cause fix verification.

Covers all 10 investigation questions from the bug report, using mocked providers.
No paid API calls are made.

Run: bench --site ci-test.local run-tests --module ai_erpnext.tests.test_rate_limit_regression
"""
import json
from unittest import mock

import frappe
from frappe.tests import IntegrationTestCase

from ai_erpnext.ai.errors import AIProviderError
from ai_erpnext.api import chat
from ai_erpnext.chat import llm
from ai_erpnext.tests.test_chat_router_retry import FakeProvider

NO_MODEL = AssertionError("must not reach the AI provider for this message")

CHAT_PLAN = {"mode": "chat", "reply": "Sure, how can I help?"}
QUERY_PLAN = {"mode": "query", "query": {"doctype": "Customer", "operation": "count", "filters": {}}}


def err(category, **kw):
	return AIProviderError(category, **kw)


class TestRateLimitRegression(IntegrationTestCase):
	"""Verifies each root-cause finding: request counts, history bounds, error classification."""

	def setUp(self):
		frappe.set_user("Administrator")
		self.s = chat.new_conversation()["session"]
		self.counts = {dt: frappe.db.count(dt) for dt in ("Sales Invoice", "Customer", "Item")}

	def tearDown(self):
		self.assertEqual({dt: frappe.db.count(dt) for dt in self.counts}, self.counts)

	def send(self, text, provider=None):
		if provider is not None:
			with mock.patch("ai_erpnext.chat.llm.get_text_provider", return_value=provider), \
				 mock.patch("time.sleep"):
				return chat.send_message(session=self.s, text=text)
		return chat.send_message(session=self.s, text=text)

	# --- Q1: Greeting makes no provider call ---
	def test_greeting_uses_shortcut_no_provider_call(self):
		"""'hi', 'hello', 'thanks' are handled by shortcuts - zero LLM calls."""
		for greeting in ("hi", "hello", "Good morning!", "thanks"):
			with mock.patch("ai_erpnext.chat.llm.route", side_effect=NO_MODEL), \
				 mock.patch("ai_erpnext.chat.llm.get_text_provider", side_effect=NO_MODEL):
				r = chat.send_message(session=self.s, text=greeting)
			last = r["messages"][-1]
			self.assertIn("text", last, f"Greeting '{greeting}' got no reply")
			self.assertTrue(last["text"], f"Greeting '{greeting}' got empty reply")

	# --- Q2: One AI-dependent message = exactly 1 provider call ---
	def test_one_non_shortcut_message_makes_exactly_one_provider_call(self):
		p = FakeProvider(CHAT_PLAN)
		self.send("what is a cost center in ERPNext?", provider=p)
		self.assertEqual(len(p.calls), 1, "Expected exactly 1 provider call")

	def test_data_query_makes_exactly_one_provider_call(self):
		p = FakeProvider(QUERY_PLAN)
		# "how many customers do we have?" alone is a shortcut (zero calls); this one needs the router
		self.send("roughly how many customers are there in each territory?", provider=p)
		self.assertEqual(len(p.calls), 1, "Expected exactly 1 provider call for a query")

	# --- Q3: History stays bounded as conversation grows ---
	def test_history_sent_to_model_stays_bounded(self):
		"""History sent to the router never exceeds HISTORY_MESSAGES turns."""
		for i in range(10):
			p = FakeProvider({"mode": "chat", "reply": f"Answer {i}"})
			self.send(f"explain concept number {i}", provider=p)
		p = FakeProvider(CHAT_PLAN)
		self.send("explain the final concept", provider=p)
		self.assertEqual(len(p.calls), 1)
		sent_messages = p.calls[0]["messages"]
		assistant_turns = [m for m in sent_messages if m["role"] == "assistant"]
		self.assertLessEqual(len(assistant_turns), llm.HISTORY_MESSAGES,
			f"History exceeded bound: {len(assistant_turns)} assistant turns")

	def test_history_chars_are_bounded_per_message(self):
		"""Each history message content is truncated to HISTORY_CHARS."""
		p1 = FakeProvider({"mode": "chat", "reply": "x" * 2000})
		self.send("what is a sales order?", provider=p1)
		p2 = FakeProvider(CHAT_PLAN)
		self.send("explain it more", provider=p2)
		for m in p2.calls[0]["messages"]:
			content = m.get("content") or ""
			self.assertLessEqual(len(content), llm.HISTORY_CHARS + 200,
				f"Sent message exceeded char limit: {len(content)}")

	# --- Q4: Follow-up questions still get recent context ---
	def test_follow_up_questions_receive_query_context(self):
		"""After a query, the next message receives the previous query in its context."""
		p1 = FakeProvider(QUERY_PLAN)
		self.send("count customers", provider=p1)
		p2 = FakeProvider(CHAT_PLAN)
		self.send("what about suppliers?", provider=p2)
		self.assertEqual(len(p2.calls), 1)
		user_msg = p2.calls[0]["messages"][-1]["content"]
		self.assertIn("previous_query", user_msg)

	# --- Q5: Page context does not add extra LLM calls ---
	def test_page_context_answer_makes_no_llm_call(self):
		"""A page-context question is answered server-side, not via LLM."""
		with mock.patch("ai_erpnext.chat.page_context.answer", return_value=True) as mock_answer, \
			 mock.patch("ai_erpnext.chat.llm.route", side_effect=NO_MODEL), \
			 mock.patch("ai_erpnext.chat.llm.get_text_provider", side_effect=NO_MODEL):
			chat.send_message(session=self.s, text="explain this form",
				context=json.dumps({"route": ["Form", "Sales Invoice", "SINV-0001"]}))
		mock_answer.assert_called_once()

	# --- Q6: Transient 429 bounded retries ---
	def test_rate_limit_with_retry_after_retries_once_within_budget(self):
		p = FakeProvider(err("rate_limited", status_code=429, retry_after=2.0), CHAT_PLAN)
		sleeps = []
		result = llm.complete_with_policy(p, "system", [{"role": "user", "content": "q"}],
			sleep=sleeps.append, jitter=lambda: 0.0)
		self.assertEqual(len(p.calls), 2)
		self.assertEqual(result["mode"], "chat")

	def test_rate_limit_without_retry_after_not_retried(self):
		"""A 429 without Retry-After is NOT retried - prevents silent request multiplication."""
		p = FakeProvider(err("rate_limited", status_code=429, retry_after=None), CHAT_PLAN)
		with self.assertRaises(AIProviderError) as ctx:
			llm.complete_with_policy(p, "system", [{"role": "user", "content": "q"}])
		self.assertEqual(ctx.exception.category, "rate_limited")
		self.assertEqual(len(p.calls), 1, "rate_limited without retry_after must not be retried")

	def test_rate_limit_large_retry_after_not_retried(self):
		p = FakeProvider(err("rate_limited", status_code=429, retry_after=60.0), CHAT_PLAN)
		with self.assertRaises(AIProviderError):
			llm.complete_with_policy(p, "system", [{"role": "user", "content": "q"}])
		self.assertEqual(len(p.calls), 1, "retry_after > budget must not be retried")

	# --- Q7: Daily quota_exhausted message differs from rate_limited ---
	def test_quota_exhausted_message_differs_from_rate_limited(self):
		from ai_erpnext.chat.engine import CHAT_ERRORS
		rate_msg = CHAT_ERRORS["rate_limited"]
		quota_msg = CHAT_ERRORS["quota_exhausted"]
		self.assertNotEqual(rate_msg, quota_msg)
		self.assertTrue(any(w in quota_msg.lower() for w in ("today", "daily", "limit")),
			"quota_exhausted should mention the daily nature")
		self.assertTrue(any(w in rate_msg.lower() for w in ("wait", "minute", "per-minute")),
			"rate_limited should mention waiting/per-minute")

	def test_quota_exhausted_end_to_end_shows_daily_limit_message(self):
		p = FakeProvider(err("quota_exhausted", status_code=429))
		r = self.send("what is ERPNext?", provider=p)
		last_text = r["messages"][-1]["text"]
		self.assertIn("usage limit", last_text, "quota_exhausted should say usage limit reached")
		self.assertNotIn("per-minute", last_text, "quota_exhausted should not say per-minute")

	# --- Q8: Auth/network/bad_request errors are classified correctly ---
	def test_auth_error_classified_correctly_and_not_retried(self):
		from ai_erpnext.chat.engine import CHAT_ERRORS
		p = FakeProvider(err("auth", status_code=401))
		r = self.send("list invoices", provider=p)
		self.assertIn(CHAT_ERRORS["auth"], r["messages"][-1]["text"])
		self.assertEqual(len(p.calls), 1, "auth errors must not be retried")

	def test_bad_request_not_retried(self):
		p = FakeProvider(err("bad_request", status_code=400), CHAT_PLAN)
		r = self.send("explain this", provider=p)
		self.assertEqual(len(p.calls), 1, "bad_request must not be retried")

	def test_billing_not_retried(self):
		p = FakeProvider(err("billing", status_code=402), CHAT_PLAN)
		r = self.send("which sales invoices did our biggest customers get recently?", provider=p)  # "show sales invoices" is a shortcut
		self.assertEqual(len(p.calls), 1, "billing errors must not be retried")

	# --- Q9: Duplicate client_id not processed twice ---
	def test_duplicate_client_id_not_processed_twice(self):
		"""A message with the same client_id is not re-processed (network retry / double click)."""
		client_id = "dedup-regression-test-001"
		p1 = FakeProvider(CHAT_PLAN)
		with mock.patch("ai_erpnext.chat.llm.get_text_provider", return_value=p1), \
			 mock.patch("time.sleep"):
			r1 = chat.send_message(session=self.s, text="explain stock entry", client_id=client_id)
		p2 = FakeProvider(CHAT_PLAN)
		with mock.patch("ai_erpnext.chat.llm.get_text_provider", return_value=p2), \
			 mock.patch("time.sleep"):
			r2 = chat.send_message(session=self.s, text="explain stock entry", client_id=client_id)
		self.assertEqual(len(p2.calls), 0, "Duplicate client_id must not trigger another provider call")
		self.assertEqual(len(r1["messages"]), len(r2["messages"]),
			"Duplicate send must return same message set")

	# --- Q10: Diagnostic logging includes prompt size estimate ---
	def test_route_log_includes_prompt_size_estimate(self):
		"""Route log includes est_tokens so token usage is visible without expensive API calls."""
		p = FakeProvider(CHAT_PLAN)
		with mock.patch("ai_erpnext.chat.llm.get_text_provider", return_value=p), \
			 mock.patch("time.sleep"), \
			 self.assertLogs("ai_erpnext", level="INFO") as logs:
			chat.send_message(session=self.s, text="what is a supplier?")
		joined = "\n".join(logs.output)
		self.assertIn("AI-CHAT route", joined)
		self.assertIn("prompt_chars=", joined)
		self.assertIn("est_tokens=", joined)
		for line in logs.output:
			if "est_tokens=" in line:
				est = int(line.split("est_tokens=")[1].split()[0])
				self.assertGreater(est, 0, "est_tokens must be positive")
				break


class TestHistoryPrivacyIntact(IntegrationTestCase):
	"""Regression: ERPNext query results are not included in model history."""

	def setUp(self):
		frappe.set_user("Administrator")
		self.s = chat.new_conversation()["session"]

	def test_erpnext_query_results_not_in_model_history(self):
		"""Query result messages (source=erpnext) are excluded from the history sent to the model."""
		p1 = FakeProvider(QUERY_PLAN)
		with mock.patch("ai_erpnext.chat.llm.get_text_provider", return_value=p1), \
			 mock.patch("time.sleep"):
			chat.send_message(session=self.s, text="how many customers?")
		p2 = FakeProvider(CHAT_PLAN)
		with mock.patch("ai_erpnext.chat.llm.get_text_provider", return_value=p2), \
			 mock.patch("time.sleep"):
			chat.send_message(session=self.s, text="what does that number mean?")
		sent = json.dumps(p2.calls[0]["messages"])
		self.assertNotIn('"erpnext"', sent,
			"erpnext-source messages must not be included in model history")
