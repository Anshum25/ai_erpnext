"""Primary/fallback provider configuration, adapter selection and the remaining fallback rules.

No provider is ever called: requests go to the scripted FakeNet transport of
test_provider_fallback, which fails the test on any request it was not told about. The keys
below are dummies that never leave the process.

Run: bench --site <site> run-tests --module ai_erpnext.tests.test_provider_config
"""

from types import SimpleNamespace
from unittest import mock

import frappe
from frappe.tests import IntegrationTestCase

from ai_erpnext.ai import providers
from ai_erpnext.ai.errors import AIProviderError, rate_limit_category
from ai_erpnext.chat import llm
from ai_erpnext.tests.test_provider_fallback import (
	GROQ,
	GROQ_KEY,
	OPENAI,
	OPENAI_KEY,
	TPD,
	TPM,
	FakeNet,
	error,
	ok,
	settings,
)

# Groq's real 429 messages end with a link to its billing page; that must not turn them into "no credits".
GROQ_TPD = TPD + " Need more tokens? Upgrade to Dev Tier today at https://console.groq.com/settings/billing"
GROQ_TPM = (
	TPM
	+ " Please try again in 2.48s. Need more tokens? Upgrade to Dev Tier today at https://console.groq.com/settings/billing"
)


class TestProviderConfig(IntegrationTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		self.counter_key = f"ai_erpnext:test_fallback:{frappe.generate_hash(length=10)}"
		patcher = mock.patch.object(providers, "daily_counter_key", return_value=self.counter_key)
		patcher.start()
		self.addCleanup(patcher.stop)
		self.addCleanup(lambda: frappe.cache.delete(frappe.cache.make_key(self.counter_key)))

	def route(self, net, config, text="What is a sales order?"):
		import httpx2

		with (
			mock.patch.object(
				httpx2.HTTPTransport, "handle_request", lambda _t, request: net.handle(request)
			),
			mock.patch.object(providers, "get_settings", return_value=config),
			mock.patch("time.sleep"),
		):
			return llm.route([], text)

	# ---- settings form -----------------------------------------------------------------------
	def test_settings_offer_primary_and_fallback_configuration(self):
		meta = frappe.get_meta("AI Provider Settings")
		provider, key = meta.get_field("provider"), meta.get_field("api_key")
		self.assertEqual((provider.label, provider.fieldtype), ("Primary AI Provider", "Select"))
		self.assertEqual([p for p in providers.PROVIDERS if p not in provider.options.split("\n")], [])
		self.assertEqual((key.label, key.fieldtype), ("Primary API Key", "Password"))
		fields = {
			f: meta.get_field(f)
			for f in (
				"fallback_enabled",
				"fallback_provider",
				"fallback_api_key",
				"fallback_model",
				"fallback_base_url",
				"fallback_daily_limit",
			)
		}
		self.assertEqual(fields["fallback_enabled"].label, "Enable Fallback Provider")
		self.assertEqual(fields["fallback_enabled"].default, "0")
		self.assertEqual(fields["fallback_api_key"].fieldtype, "Password")
		for name in (
			"fallback_provider",
			"fallback_api_key",
			"fallback_model",
			"fallback_base_url",
			"fallback_daily_limit",
		):
			self.assertEqual(
				fields[name].depends_on, "eval:doc.fallback_enabled", name
			)  # shown only once enabled
		self.assertEqual(fields["fallback_daily_limit"].label, "Fallback Daily Request Limit")
		self.assertEqual([p.role for p in meta.permissions], ["System Manager"])

	def test_saved_keys_are_never_returned_in_clear(self):
		doc = frappe.get_single("AI Provider Settings")
		doc.update(
			{
				"provider": "Groq",
				"api_key": GROQ_KEY,
				"fallback_enabled": 1,
				"fallback_provider": "OpenAI",
				"fallback_model": "gpt-4.1-mini",
				"fallback_api_key": OPENAI_KEY,
				"fallback_daily_limit": 5,
			}
		)
		doc.save()
		loaded = frappe.get_single("AI Provider Settings")
		dumped = frappe.as_json(loaded.as_dict())
		for secret in (GROQ_KEY, OPENAI_KEY):
			self.assertNotIn(secret, dumped)  # the form / API only ever sees the mask
		self.assertTrue(set(loaded.api_key) == {"*"})
		self.assertEqual(loaded.get_password("fallback_api_key"), OPENAI_KEY)  # server side only
		self.assertEqual(
			frappe.db.get_single_value("AI Provider Settings", "fallback_api_key"), loaded.fallback_api_key
		)

	def test_non_admin_users_cannot_read_provider_settings(self):
		email = "ai-provider-nonadmin@example.com"
		if not frappe.db.exists("User", email):
			frappe.get_doc(
				{
					"doctype": "User",
					"email": email,
					"first_name": "No Admin",
					"send_welcome_email": 0,
					"roles": [{"role": "Sales User"}],
				}
			).insert(ignore_permissions=True)
		frappe.set_user(email)
		try:
			self.assertFalse(frappe.has_permission("AI Provider Settings", "read"))
			from frappe.client import get_password

			with self.assertRaises(frappe.PermissionError):
				get_password("AI Provider Settings", "AI Provider Settings", "api_key")
		finally:
			frappe.set_user("Administrator")

	def test_configuration_is_validated_locally(self):
		cases = {
			"fallback without key": (
				settings(fallback_enabled=1, fallback_api_key=None),
				"no Fallback API Key",
			),
			"fallback without cap": (settings(fallback_enabled=1, fallback_daily_limit=0), "at least 1"),
			"unsupported fallback": (
				settings(fallback_enabled=1, fallback_provider="Gemini"),
				"Fallback AI Provider",
			),
			"unsupported primary": (settings(provider="SomeCloud"), "supported Primary AI Provider"),
			"model required": (
				settings(provider="OpenRouter", model=None, base_url=None),
				"Model Name is required for OpenRouter",
			),
			"plain http": (settings(base_url="http://api.groq.com/openai/v1"), "https://"),
		}
		import httpx2

		with mock.patch.object(
			httpx2.HTTPTransport, "handle_request", side_effect=AssertionError("no request may be sent")
		):
			for label, (config, message) in cases.items():
				errors, _warnings = providers.configuration_issues(config)
				self.assertTrue(any(message in e for e in errors), (label, errors))
				with self.assertRaises(frappe.ValidationError, msg=label):
					providers.validate_settings(config)
			errors, warnings = providers.configuration_issues(settings(fallback_enabled=1))
			self.assertEqual(errors, [])
			self.assertTrue(any("paid service" in w for w in warnings))
			_errors, warnings = providers.configuration_issues(settings(provider="Gemini", model=None))
			self.assertTrue(any("invoice image extraction only" in w for w in warnings))

	# ---- adapter selection ---------------------------------------------------------------------
	def test_selected_provider_picks_adapter_endpoint_and_default_model(self):
		p = providers.primary(settings(base_url=None, model=None))
		self.assertEqual(
			(p.provider_name, p.model, str(p.client.base_url).rstrip("/")),
			("Groq", "qwen/qwen3.8-27b", "https://api.groq.com/openai/v1"),
		)
		p = providers.primary(settings(provider="OpenAI", base_url=None, model=None))
		self.assertEqual(
			(p.provider_name, p.model, p.client.base_url.host), ("OpenAI", "gpt-4.1-mini", "api.openai.com")
		)
		p = providers.primary(settings(provider="Nvidia NIM", base_url=None, model="some/vision-model"))
		self.assertEqual(p.client.base_url.host, "integrate.api.nvidia.com")
		with self.assertRaises(AIProviderError):
			providers.primary(
				settings(provider="OpenRouter", base_url=None, model=None)
			)  # no safe default model
		with self.assertRaises(AIProviderError):
			providers.primary(settings(provider="Gemini"), text_only=True)  # Gemini can't route chat
		gemini = providers.primary(settings(provider="Gemini", model=None))
		self.assertEqual((type(gemini).__name__, gemini.model), ("GeminiProvider", "gemini-3.6-flash"))

	def test_empty_base_url_reaches_the_providers_own_endpoint(self):
		net = FakeNet(api_groq_com=[ok({"mode": "chat", "reply": "Hi"})])
		self.assertEqual(self.route(net, settings(base_url=None))["reply"], "Hi")
		self.assertEqual(net.hosts(), [GROQ])  # never api.openai.com with a Groq key

	# ---- error classification ----------------------------------------------------------------
	def test_429_is_read_by_window_and_credits(self):
		self.assertEqual(rate_limit_category(GROQ_TPD.lower()), "quota_exhausted")
		self.assertEqual(rate_limit_category(GROQ_TPM.lower()), "rate_limited")
		self.assertEqual(rate_limit_category("rate limit exceeded: free-models-per-day"), "quota_exhausted")
		self.assertEqual(
			rate_limit_category("you exceeded your current quota", "insufficient_quota"), "billing"
		)
		self.assertEqual(
			rate_limit_category("too many requests"), "rate_limited"
		)  # unknown window: never the paid fallback
		self.assertEqual(rate_limit_category("quota exceeded"), "rate_limited")

	# ---- fallback rules not covered by test_provider_fallback -----------------------------------
	def test_realistic_groq_daily_quota_message_uses_the_fallback(self):
		net = FakeNet(
			api_groq_com=[error(429, GROQ_TPD, {"retry-after": "3600"})],
			api_openai_com=[ok({"mode": "chat", "reply": "ok"})],
		)
		self.assertEqual(self.route(net, settings(fallback_enabled=1))["reply"], "ok")
		self.assertEqual(net.hosts(), [GROQ, OPENAI])

	def test_per_minute_limit_waits_on_groq_and_never_uses_the_fallback(self):
		net = FakeNet(
			api_groq_com=[
				error(429, GROQ_TPM, {"retry-after": "2"}),
				ok({"mode": "chat", "reply": "after the wait"}),
			]
		)
		self.assertEqual(self.route(net, settings(fallback_enabled=1))["reply"], "after the wait")
		self.assertEqual(net.hosts(), [GROQ, GROQ])

	def test_ambiguous_429_does_not_spend_fallback_requests(self):
		net = FakeNet(api_groq_com=[error(429, "Too many requests")])
		with self.assertRaises(AIProviderError) as ctx:
			self.route(net, settings(fallback_enabled=1))
		self.assertEqual(ctx.exception.category, "rate_limited")
		self.assertEqual(net.hosts(), [GROQ])

	def test_failing_fallback_is_never_retried_or_chained(self):
		for response, category in (
			(error(429, TPM, {"retry-after": "1"}), "rate_limited"),
			(error(429, TPD), "quota_exhausted"),
			(error(503, "overloaded"), "unavailable"),
		):
			net = FakeNet(api_groq_com=[error(429, TPD)], api_openai_com=[response])
			with self.assertRaises(AIProviderError) as ctx:
				self.route(net, settings(fallback_enabled=1))
			self.assertEqual(ctx.exception.category, category)
			self.assertEqual(
				net.hosts(), [GROQ, OPENAI], category
			)  # one fallback request, then a clear error

	def test_primary_out_of_credits_or_bad_key_never_falls_back(self):
		for response, category in (
			(error(429, "You exceeded your current quota", code="insufficient_quota"), "billing"),
			(error(401, "Invalid API Key"), "auth"),
			(error(402, "Payment required"), "billing"),
		):
			net = FakeNet(api_groq_com=[response])
			with self.assertRaises(AIProviderError) as ctx:
				self.route(net, settings(fallback_enabled=1))
			self.assertEqual(ctx.exception.category, category)
			self.assertEqual(net.hosts(), [GROQ])

	def test_errors_and_logs_never_contain_keys_or_provider_text(self):
		net = FakeNet(
			api_groq_com=[error(429, TPD)],
			api_openai_com=[error(401, f"Incorrect API key provided: {OPENAI_KEY[:12]}…")],
		)
		with self.assertLogs("ai_erpnext", level="INFO") as logs, self.assertRaises(AIProviderError) as ctx:
			self.route(net, settings(fallback_enabled=1))
		joined = "\n".join(logs.output) + str(ctx.exception)
		for secret in (GROQ_KEY, OPENAI_KEY, OPENAI_KEY[:12], "Incorrect API key"):
			self.assertNotIn(secret, joined)
		self.assertEqual(
			net.requests[1]["auth"], f"Bearer {OPENAI_KEY}"
		)  # the key is only in the request header

	def test_disabled_fallback_settings_object_is_never_contacted(self):
		config = SimpleNamespace(**{**vars(settings()), "fallback_enabled": 0})
		net = FakeNet(api_groq_com=[error(429, TPD)])
		with self.assertRaises(AIProviderError):
			self.route(net, config)
		self.assertNotIn(OPENAI, net.hosts())
