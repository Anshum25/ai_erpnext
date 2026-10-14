"""Provider selection and the quota fallback, shared by invoice extraction and the chat router.

The primary provider comes from AI Provider Settings. The fallback (OpenAI, e.g.
gpt-4.1-mini) is a paid provider, so it is used only when all of these hold:
- an administrator enabled it (fallback_enabled; off by default) and configured a key and model;
- the primary provider is Groq and the error is an exhausted daily quota (never for rate
  limits, timeouts, outages, invalid output or any other error);
- the per-site daily cap (fallback_daily_limit) is not used up.
It then gets exactly one request (no retries) per operation.
"""

import frappe
from frappe.utils import cint, nowdate

from ai_erpnext.ai.errors import AIProviderError, get_ai_logger

OPENAI_COMPATIBLE = ("OpenAI", "Groq", "Nvidia NIM", "OpenRouter")
FALLBACK_PROVIDERS = ("OpenAI",)
FALLBACK_FROM = ("Groq",)
FALLBACK_ON = ("quota_exhausted",)


def get_settings():
	return frappe.get_single("AI Provider Settings")


def build(provider, api_key, base_url=None, model=None):
	"""A provider client with SDK retries disabled (ai.policy owns retries)."""
	if not provider or not api_key:
		raise AIProviderError("config")
	if provider == "Gemini":
		from ai_erpnext.ai.gemini import GeminiProvider

		return GeminiProvider(api_key=api_key)
	if provider in OPENAI_COMPATIBLE:
		from ai_erpnext.ai.openai_provider import OpenAIProvider

		return OpenAIProvider(api_key=api_key, base_url=base_url or None, model=model, provider_name=provider, max_retries=0)
	raise AIProviderError("config")


def primary(settings=None, text_only=False):
	"""The configured provider.

	text_only: for the chat router, which needs an OpenAI-compatible provider and uses the
	optional text-only model (text_model) when one is set; image extraction uses `model`.
	"""
	s = settings or get_settings()
	provider = getattr(s, "provider", None)
	if not getattr(s, "api_key", None) or (text_only and provider not in OPENAI_COMPATIBLE):
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
		logger.warning(f"AI-FALLBACK used operation={operation} from={primary_provider.provider_name} "
			f"to={fallback.provider_name} model={fallback.model} reason={error.category}")
		try:
			result = call(fallback, True)
		except AIProviderError as fallback_error:
			logger.warning(f"AI-FALLBACK failed operation={operation} category={fallback_error.category}")
			raise
		logger.info(f"AI-FALLBACK ok operation={operation}")
		return result


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
