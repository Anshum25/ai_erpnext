"""Provider selection and the quota fallback, shared by invoice extraction and the chat router.

The primary provider comes from AI Provider Settings. The fallback (OpenAI, e.g.
gpt-4.1-mini) is a paid provider, so it is used only when all of these hold:
- an administrator enabled it (fallback_enabled; off by default) and configured a key and model;
- the primary provider is Groq and the error is an exhausted daily quota (never for rate
  limits, timeouts, outages, missing credits, invalid output or any other error; see
  ai.errors.rate_limit_category for how a 429 is read);
- the per-site daily cap (fallback_daily_limit) is not used up.
It then gets exactly one request (no retries) per operation, and a failing fallback never
falls back again.

Configuration is checked locally (validate_settings, configuration_issues): nothing here
calls a provider to "test" a key, so saving settings never spends credits.
"""

import frappe
from frappe import _
from frappe.utils import cint, nowdate

from ai_erpnext.ai.errors import AIProviderError, get_ai_logger

# Providers with an adapter in this app. base_url/model: used when the setting is empty
# (None = the SDK default endpoint / no safe default, so the administrator must set a model).
# chat: the text-only chat router needs complete_json(), which only the OpenAI-compatible
# adapter has. extract: invoice image extraction.
PROVIDERS = {
	"Groq": {
		"base_url": "https://api.groq.com/openai/v1",
		"model": "qwen/qwen3.8-27b",
		"chat": True,
		"extract": True,
	},
	"OpenAI": {"base_url": None, "model": "gpt-4.1-mini", "chat": True, "extract": True},
	"OpenRouter": {"base_url": "https://openrouter.ai/api/v1", "model": None, "chat": True, "extract": True},
	"Nvidia NIM": {
		"base_url": "https://integrate.api.nvidia.com/v1",
		"model": None,
		"chat": True,
		"extract": True,
	},
	"Gemini": {"base_url": None, "model": "gemini-3.6-flash", "chat": False, "extract": True},
}
OPENAI_COMPATIBLE = tuple(name for name, info in PROVIDERS.items() if info["chat"])
# Only OpenAI has been benchmarked as the paid fallback (chat routing and invoice images).
FALLBACK_PROVIDERS = ("OpenAI",)
FALLBACK_FROM = ("Groq",)
FALLBACK_ON = ("quota_exhausted",)


def get_settings():
	return frappe.get_single("AI Provider Settings")


def build(provider, api_key, base_url=None, model=None):
	"""A provider client with SDK retries disabled (ai.policy owns retries)."""
	if not provider or not api_key:
		raise AIProviderError("config")
	info = PROVIDERS.get(provider)
	if not info:
		raise AIProviderError("config")
	model = model or info["model"]
	if not model:
		raise AIProviderError("config")
	if provider == "Gemini":
		from ai_erpnext.ai.gemini import GeminiProvider

		return GeminiProvider(api_key=api_key, model=model)
	from ai_erpnext.ai.openai_provider import OpenAIProvider

	return OpenAIProvider(
		api_key=api_key,
		base_url=base_url or info["base_url"],
		model=model,
		provider_name=provider,
		max_retries=0,
	)


def primary(settings=None, text_only=False):
	"""The configured provider.

	text_only: for the chat router, which needs an OpenAI-compatible provider and uses the
	optional text-only model (text_model) when one is set; image extraction uses `model`.
	"""
	s = settings or get_settings()
	provider = getattr(s, "provider", None)
	if (
		not getattr(s, "api_key", None)
		or provider not in PROVIDERS
		or (text_only and provider not in OPENAI_COMPATIBLE)
	):
		raise AIProviderError("config")
	model = (text_only and getattr(s, "text_model", None)) or getattr(s, "model", None)
	return build(provider, s.get_password("api_key"), getattr(s, "base_url", None), model)


def fallback_for(error, primary_provider, settings=None, operation="ai"):
	"""The fallback provider for this error, or None when the fallback must not be used."""
	if not isinstance(error, AIProviderError) or error.category not in FALLBACK_ON:
		return None
	if getattr(primary_provider, "provider_name", None) not in FALLBACK_FROM:
		return None
	s = settings or get_settings()
	if not cint(getattr(s, "fallback_enabled", 0)):
		return None
	name, model = getattr(s, "fallback_provider", None), getattr(s, "fallback_model", None)
	if name not in FALLBACK_PROVIDERS or not model or not getattr(s, "fallback_api_key", None):
		get_ai_logger().warning(f"AI-FALLBACK skipped operation={operation} reason=not_configured")
		return None
	if not _take_daily_slot(cint(getattr(s, "fallback_daily_limit", 0))):
		get_ai_logger().warning(f"AI-FALLBACK skipped operation={operation} reason=daily_limit")
		return None
	return build(name, s.get_password("fallback_api_key"), getattr(s, "fallback_base_url", None), model)


def run(operation, call, primary_provider, settings=None):
	"""call(provider, is_fallback) on the primary provider, then at most once on the fallback.

	call must make a single request when is_fallback is True.
	"""
	try:
		return call(primary_provider, False)
	except AIProviderError as error:
		fallback = fallback_for(error, primary_provider, settings, operation)
		if not fallback:
			raise
		logger = get_ai_logger()
		logger.warning(
			f"AI-FALLBACK used operation={operation} from={primary_provider.provider_name} "
			f"to={fallback.provider_name} model={fallback.model} reason={error.category}"
		)
		try:
			result = call(fallback, True)
		except AIProviderError as fallback_error:
			logger.warning(f"AI-FALLBACK failed operation={operation} category={fallback_error.category}")
			raise
		logger.info(f"AI-FALLBACK ok operation={operation}")
		return result


def configuration_issues(s):
	"""(errors, warnings) for the settings, checked locally without contacting any provider.

	errors block saving; warnings are shown to the administrator. Messages never contain keys."""
	errors, warnings = [], []
	provider = getattr(s, "provider", None)
	info = PROVIDERS.get(provider)
	if not info:
		errors.append(_("Choose a supported Primary AI Provider: {0}.").format(", ".join(PROVIDERS)))
	else:
		if not getattr(s, "api_key", None):
			warnings.append(_("No Primary API Key is set, so the AI features are off until one is added."))
		if not getattr(s, "model", None) and not info["model"]:
			errors.append(_("Model Name is required for {0}.").format(provider))
		if not info["chat"]:
			warnings.append(
				_(
					"{0} is used for invoice image extraction only. Chat and data questions need Groq, OpenAI, "
					"OpenRouter or Nvidia NIM; with {0} the chat answers only what it can without AI."
				).format(provider)
			)
	for field in ("base_url", "fallback_base_url"):
		url = (getattr(s, field, None) or "").strip()
		if url and not url.startswith("https://"):
			errors.append(
				_("{0} must start with https://.").format(
					_("Fallback Base URL") if field.startswith("fallback") else _("Base URL")
				)
			)
	if cint(getattr(s, "fallback_enabled", 0)):
		fallback = getattr(s, "fallback_provider", None)
		if fallback not in FALLBACK_PROVIDERS:
			errors.append(_("Choose a Fallback AI Provider ({0}).").format(", ".join(FALLBACK_PROVIDERS)))
		if not getattr(s, "fallback_api_key", None):
			errors.append(_("The fallback is enabled but no Fallback API Key is set."))
		if not getattr(s, "fallback_model", None) and not (PROVIDERS.get(fallback) or {}).get("model"):
			errors.append(_("Fallback Model is required."))
		if cint(getattr(s, "fallback_daily_limit", 0)) <= 0:
			errors.append(_("Fallback Requests Per Day must be at least 1 while the fallback is enabled."))
		if provider not in FALLBACK_FROM:
			warnings.append(
				_(
					"The fallback is only used when {0} reports an exhausted daily quota; with {1} as the primary "
					"provider it will not be used."
				).format(" / ".join(FALLBACK_FROM), provider or _("no provider"))
			)
		warnings.append(
			_(
				"The fallback is a paid service: requests are only sent when its account has credits, and each "
				"one counts against Fallback Requests Per Day."
			)
		)
	return errors, warnings


def validate_settings(doc, method=None):
	"""doc_events validate hook of AI Provider Settings (no provider is contacted)."""
	errors, warnings = configuration_issues(doc)
	if errors:
		frappe.throw("<br>".join(errors), title=_("AI Provider Settings"))
	for warning in warnings:
		frappe.msgprint(warning, indicator="orange", alert=True)


def daily_counter_key():
	return f"ai_erpnext:fallback_requests:{nowdate()}"


def _take_daily_slot(limit):
	"""Atomically count one fallback request for today; False once the cap is reached."""
	if limit <= 0:
		return False
	key = frappe.cache.make_key(daily_counter_key())
	used = frappe.cache.incr(key)
	if used == 1:
		frappe.cache.expire(key, 2 * 24 * 3600)
	return used <= limit
