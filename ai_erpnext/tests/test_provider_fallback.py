"""Provider routing, retry policy and the Groq -> OpenAI quota fallback.

No real provider is ever called: the OpenAI SDK's HTTP transport (its bundled httpx2) is
replaced by a scripted fake that fails the test on any request it was not told about.
No API keys are needed; the keys below are dummies that never leave the process.

Run: bench --site <site> run-tests --module ai_erpnext.tests.test_provider_fallback
"""

import contextlib
import json
from types import SimpleNamespace
from unittest import mock

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import cint

from ai_erpnext.ai import providers
from ai_erpnext.ai.errors import AIProviderError
from ai_erpnext.chat import llm
from ai_erpnext.tests.fixtures import test_file

GROQ_URL = "https://api.groq.com/openai/v1"
GROQ, OPENAI, OPENROUTER = "api.groq.com", "api.openai.com", "openrouter.ai"
GROQ_KEY, OPENAI_KEY = "groq-dummy-key-0001", "openai-dummy-key-0002"
TPD = "Rate limit reached for model `qwen/qwen3.8-27b` on tokens per day (TPD): Limit 500000, Used 499990, Requested 900."
TPM = "Rate limit reached for model `qwen/qwen3.8-27b` on tokens per minute (TPM): Limit 6000, Used 5990, Requested 900."


def completion(content, model="m"):
	return {
		"id": "chatcmpl-test",
		"object": "chat.completion",
		"created": 0,
		"model": model,
		"choices": [
			{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}
		],
		"usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
	}


def ok(payload, model="m"):
	return (200, completion(json.dumps(payload), model), None)


def error(status, message, headers=None, code=None):
	return (status, {"error": {"message": message, "type": "tokens", "code": code}}, headers)


class FakeNet:
	"""Scripted HTTP responses per host; every request is recorded (host, model, auth header)."""

	def __init__(self, **scripts):
		self.scripts = {host.replace("_", "."): list(responses) for host, responses in scripts.items()}
		self.requests = []

	def handle(self, request):
		import httpx2

		raw = request.read() or b"{}"
		body = json.loads(raw)
		self.requests.append(
			{
				"host": request.url.host,
				"model": body.get("model"),
				"auth": request.headers.get("authorization"),
				"has_image": b"image_url" in raw,
			}
		)
		queue = self.scripts.get(request.url.host)
		if not queue:
			raise AssertionError(f"unexpected request to {request.url.host}")
		status, payload, headers = queue.pop(0)
		return httpx2.Response(status, request=request, json=payload, headers=headers or {})

	def hosts(self):
		return [r["host"] for r in self.requests]


def settings(**overrides):
	values = {
		"provider": "Groq",
		"api_key": "set",
		"base_url": GROQ_URL,
		"model": "qwen/qwen3.8-27b",
		"fallback_enabled": 0,
		"fallback_provider": "OpenAI",
		"fallback_model": "gpt-4.1-mini",
		"fallback_base_url": None,
		"fallback_api_key": "set",
		"fallback_daily_limit": 20,
	}
	values.update(overrides)
	secrets = {"api_key": GROQ_KEY, "fallback_api_key": OPENAI_KEY}
	return SimpleNamespace(**values, get_password=lambda field: secrets[field])


class TestProviderFallback(IntegrationTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		self.counter_key = f"ai_erpnext:test_fallback:{frappe.generate_hash(length=10)}"
		patcher = mock.patch.object(providers, "daily_counter_key", return_value=self.counter_key)
		patcher.start()
		self.addCleanup(patcher.stop)
		self.addCleanup(lambda: frappe.cache.delete(frappe.cache.make_key(self.counter_key)))

	@contextlib.contextmanager
	def network(self, net, config):
		import httpx2

		with (
			mock.patch.object(
				httpx2.HTTPTransport, "handle_request", lambda _transport, request: net.handle(request)
			),
			mock.patch.object(providers, "get_settings", return_value=config),
			mock.patch("time.sleep") as sleep,
		):
			yield sleep

	def route(self, net, config, text="What is a sales order?"):
		with self.network(net, config):
			return llm.route([], text)

	# ---- defaults ----------------------------------------------------------------------------
	def test_fallback_is_disabled_by_default(self):
		meta = frappe.get_meta("AI Provider Settings")
		self.assertEqual(meta.get_field("fallback_enabled").default, "0")
		self.assertEqual(cint(frappe.get_single("AI Provider Settings").get("fallback_enabled")), 0)
		net = FakeNet(api_groq_com=[error(429, TPD, {"retry-after": "3600"})])
		with self.assertRaises(AIProviderError) as ctx:
			self.route(net, settings())  # fallback_enabled=0
		self.assertEqual(ctx.exception.category, "quota_exhausted")
		self.assertEqual(net.hosts(), [GROQ])  # one request, no fallback, no retry

	def test_factory_providers_have_sdk_retries_disabled(self):
		p = providers.build("Groq", GROQ_KEY, GROQ_URL, "qwen/qwen3.8-27b")
		self.assertEqual(p.client.max_retries, 0)
		self.assertEqual(p.provider_name, "Groq")

	# ---- the one case the fallback is for ---------------------------------------------------
	def test_groq_daily_quota_uses_openai_fallback_once(self):
		net = FakeNet(
			api_groq_com=[error(429, TPD, {"retry-after": "3600"})],
			api_openai_com=[
				ok(
					{"mode": "chat", "reply": "A Sales Order is a confirmed order from a customer."},
					"gpt-4.1-mini",
				)
			],
		)
		with self.assertLogs("ai_erpnext", level="INFO") as logs:
			plan = self.route(net, settings(fallback_enabled=1))
		self.assertEqual(plan["mode"], "chat")
		self.assertIn("confirmed order", plan["reply"])
		self.assertEqual(net.hosts(), [GROQ, OPENAI])
		self.assertEqual(net.requests[1]["model"], "gpt-4.1-mini")
		self.assertEqual(net.requests[0]["auth"], f"Bearer {GROQ_KEY}")
		self.assertEqual(net.requests[1]["auth"], f"Bearer {OPENAI_KEY}")
		joined = "\n".join(logs.output)
		self.assertIn(
			"AI-FALLBACK used operation=chat from=Groq to=OpenAI model=gpt-4.1-mini reason=quota_exhausted",
			joined,
		)
		for secret in (GROQ_KEY, OPENAI_KEY, "confirmed order"):
			self.assertNotIn(secret, joined)

	def test_fallback_is_a_single_request(self):
		net = FakeNet(api_groq_com=[error(429, TPD)], api_openai_com=[error(503, "overloaded")])
		with self.assertRaises(AIProviderError) as ctx:
			self.route(net, settings(fallback_enabled=1))
		self.assertEqual(ctx.exception.category, "unavailable")
		self.assertEqual(net.hosts(), [GROQ, OPENAI])  # no SDK or policy retry on the paid provider

	def test_groq_success_needs_one_request_and_no_fallback(self):
		net = FakeNet(api_groq_com=[ok({"mode": "chat", "reply": "Hello!"})])
		self.assertEqual(self.route(net, settings(fallback_enabled=1))["reply"], "Hello!")
		self.assertEqual(net.hosts(), [GROQ])

	def test_openai_out_of_credits_or_rejected_key_is_a_clear_error(self):
		credits = error(
			429,
			"You exceeded your current quota, please check your plan and billing details.",
			code="insufficient_quota",
		)
		# no credits is a billing problem, not a daily quota: it is reported as such and never retried
		for response, category in ((credits, "billing"), (error(401, "Incorrect API key provided"), "auth")):
			net = FakeNet(api_groq_com=[error(429, TPD)], api_openai_com=[response])
			with self.assertRaises(AIProviderError) as ctx:
				self.route(net, settings(fallback_enabled=1))
			self.assertEqual(ctx.exception.category, category)
			self.assertEqual(net.hosts(), [GROQ, OPENAI])  # one attempt each, no loop back to Groq
			self.assertNotIn("billing details", str(ctx.exception))  # fixed, user-safe wording only

	def test_text_chat_and_image_extraction_use_their_own_models(self):
		from ai_erpnext.tests.test_extraction_safety import b2_output

		config = settings(text_model="text-only-model")
		net = FakeNet(api_groq_com=[ok({"mode": "chat", "reply": "Hi"})])
		self.route(net, config)
		self.assertEqual((net.requests[0]["model"], net.requests[0]["has_image"]), ("text-only-model", False))
		net = FakeNet(api_groq_com=[ok(b2_output())])
		self._extract(net, config)
		self.assertEqual((net.requests[0]["model"], net.requests[0]["has_image"]), ("qwen/qwen3.8-27b", True))
		net = FakeNet(api_groq_com=[ok({"mode": "chat", "reply": "Hi"})])
		self.route(net, settings())  # no text model configured: the one model is used, as before
		self.assertEqual(net.requests[0]["model"], "qwen/qwen3.8-27b")

	# ---- never for anything else ------------------------------------------------------------
	def test_other_errors_never_use_the_fallback(self):
		cases = {
			"rate_limited": [
				error(429, TPM, {"retry-after": "30"})
			],  # per-minute limit, wait exceeds the budget
			"unavailable": [error(503, "overloaded")] * llm.MAX_ATTEMPTS,
			"auth": [error(401, "Invalid API Key")],
			"billing": [error(402, "payment required")],
			"bad_request": [error(400, "invalid request: unsupported parameter")],
			"request_too_large": [
				error(400, "context length exceeded")
			],  # too big for the model: retrying or a fallback can't help
			"invalid_response": [error(400, "Failed to generate JSON", code="json_validate_failed")]
			* llm.MAX_ATTEMPTS,
		}
		for category, responses in cases.items():
			net = FakeNet(api_groq_com=list(responses))
			with self.assertRaises(AIProviderError) as ctx:
				self.route(net, settings(fallback_enabled=1))
			self.assertEqual(ctx.exception.category, category, category)
			self.assertNotIn(OPENAI, net.hosts(), category)

	def test_fallback_only_from_groq(self):
		config = settings(
			fallback_enabled=1,
			provider="OpenRouter",
			base_url="https://openrouter.ai/api/v1",
			model="some/model",
		)
		net = FakeNet(openrouter_ai=[error(429, "You have exceeded your daily quota")])
		with self.assertRaises(AIProviderError) as ctx:
			self.route(net, config)
		self.assertEqual(ctx.exception.category, "quota_exhausted")
		self.assertEqual(net.hosts(), [OPENROUTER])

	def test_fallback_needs_complete_configuration(self):
		for missing in (
			{"fallback_api_key": None},
			{"fallback_model": ""},
			{"fallback_provider": ""},
			{"fallback_provider": "Gemini"},
		):
			net = FakeNet(api_groq_com=[error(429, TPD)])
			with self.assertRaises(AIProviderError):
				self.route(net, settings(fallback_enabled=1, **missing))
			self.assertEqual(net.hosts(), [GROQ], missing)

	def test_daily_limit_caps_fallback_requests(self):
		config = settings(fallback_enabled=1, fallback_daily_limit=1)
		net = FakeNet(
			api_groq_com=[error(429, TPD), error(429, TPD)],
			api_openai_com=[ok({"mode": "chat", "reply": "ok"})],
		)
		self.assertEqual(self.route(net, config)["reply"], "ok")
		with self.assertLogs("ai_erpnext", level="INFO") as logs, self.assertRaises(AIProviderError) as ctx:
			self.route(net, config)
		self.assertEqual(ctx.exception.category, "quota_exhausted")
		self.assertEqual(net.hosts(), [GROQ, OPENAI, GROQ])
		self.assertIn("AI-FALLBACK skipped operation=chat reason=daily_limit", "\n".join(logs.output))
		net = FakeNet(api_groq_com=[error(429, TPD)])
		with self.assertRaises(AIProviderError):
			self.route(net, settings(fallback_enabled=1, fallback_daily_limit=0))  # 0 = never
		self.assertEqual(net.hosts(), [GROQ])

	# ---- invoice extraction uses the same rules -----------------------------------------------
	def _pending_entry(self):
		name = (
			frappe.get_doc({"doctype": "AI Document Entry", "status": "Processed", "file": test_file("png")})
			.insert()
			.name
		)
		frappe.db.set_value("AI Document Entry", name, "status", "Pending")
		return name

	def _extract(self, net, config):
		from ai_erpnext.pipeline.controller import run_ai_pipeline

		name = self._pending_entry()
		with self.network(net, config) as sleep, mock.patch.object(frappe.db, "commit"):
			run_ai_pipeline(name)
		return frappe.get_doc("AI Document Entry", name), sleep

	def test_extraction_falls_back_on_groq_daily_quota(self):
		from ai_erpnext.tests.test_extraction_safety import b2_output

		net = FakeNet(api_groq_com=[error(429, TPD)], api_openai_com=[ok(b2_output(), "gpt-4.1-mini")])
		with self.assertLogs("ai_erpnext", level="INFO") as logs:
			doc, _ = self._extract(net, settings(fallback_enabled=1))
		self.assertNotEqual(doc.status, "Failed")
		self.assertEqual(json.loads(doc.extracted_data)["invoice_number"], "AISAFE-0001")
		self.assertEqual(net.hosts(), [GROQ, OPENAI])
		self.assertIn("AI-FALLBACK used operation=extract", "\n".join(logs.output))

	def test_extraction_without_fallback_fails_safely_after_one_request(self):
		net = FakeNet(api_groq_com=[error(429, TPD)])
		doc, _ = self._extract(net, settings())
		self.assertEqual(doc.status, "Failed")
		self.assertIn("usage limit for today", doc.error_log)
		self.assertEqual(net.hosts(), [GROQ])  # the SDK used to retry this twice more

	def test_failed_extraction_keeps_the_upload_for_retry(self):
		from ai_erpnext.api.extraction import retry_extraction

		net = FakeNet(
			api_groq_com=[error(429, TPD)],
			api_openai_com=[error(429, "You exceeded your current quota", code="insufficient_quota")],
		)
		doc, _ = self._extract(net, settings(fallback_enabled=1))
		self.assertEqual(doc.status, "Failed")
		self.assertTrue(frappe.db.exists("File", {"file_url": doc.file}))  # the uploaded image is kept
		self.assertFalse(doc.created_document)
		with mock.patch("frappe.enqueue") as enqueue:
			self.assertEqual(retry_extraction(doc.name)["status"], "Pending")
		self.assertEqual(enqueue.call_count, 1)

	def test_extraction_retries_outages_within_the_policy(self):
		from ai_erpnext.tests.test_extraction_safety import b2_output

		net = FakeNet(api_groq_com=[error(503, "overloaded"), error(503, "overloaded"), ok(b2_output())])
		doc, sleep = self._extract(net, settings(fallback_enabled=1))
		self.assertNotEqual(doc.status, "Failed")
		self.assertEqual(
			net.hosts(), [GROQ, GROQ, GROQ]
		)  # retried on Groq; an outage never triggers the paid fallback
		self.assertEqual(sleep.call_count, 2)
		net = FakeNet(api_groq_com=[error(503, "overloaded")] * 3)
		doc, _ = self._extract(net, settings(fallback_enabled=1))
		self.assertEqual(doc.status, "Failed")
		self.assertEqual(net.hosts(), [GROQ] * 3)  # bounded: at most 3 attempts
