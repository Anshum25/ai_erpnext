"""Safe AI provider errors and metadata-only logging.

Provider exceptions often carry the full request/response body, account or organisation
identifiers and sometimes echoed document content. They are converted into an
AIProviderError whose message is a fixed, user-safe sentence. Raw provider text is used
only in memory to pick a category and is never logged or shown to users.
"""

import json
import logging
import os

USER_MESSAGES = {
	"rate_limited": "AI request limit reached. Please wait and try again.",
	"quota_exhausted": "The AI usage limit for today has been reached. Please try again later.",
	"unavailable": "AI service is temporarily unavailable. Please try again shortly.",
	"auth": "AI service configuration needs attention. Please contact the administrator.",
	"config": "AI service configuration needs attention. Please contact the administrator.",
	"billing": "AI service account needs attention. Please contact the administrator.",
	"timeout": "AI extraction timed out. Please retry.",
	"network": "Could not reach the AI service. Please check the connection and retry.",
	"bad_request": "The AI service could not process this file. Please upload a clear JPG, PNG or WEBP image of the invoice.",
	"invalid_response": "The AI returned a response that could not be read. Please retry.",
	"unknown": "AI extraction failed. Please retry or contact the administrator.",
}

# Phrases in a 429 body that mean a daily/monthly allowance is used up rather than a
# short per-minute limit. Matched case-insensitively, never logged.
_QUOTA_HINTS = ("per day", "(tpd)", "(rpd)", "daily", "quota", "insufficient", "credits", "per month")


class AIProviderError(Exception):
	"""Provider failure with a safe message. str(error) is always user-safe.

	retry_after: seconds the provider asked us to wait (from headers), if known.
	failed_generation: the model output a JSON-mode provider rejected (memory only; never logged or shown raw).
	"""

	def __init__(self, category, status_code=None, retry_after=None, failed_generation=None):
		self.category = category if category in USER_MESSAGES else "unknown"
		self.status_code = status_code
		self.retry_after = retry_after
		self.failed_generation = failed_generation
		self.user_message = USER_MESSAGES[self.category]
		super().__init__(self.user_message)


def classify(exc) -> AIProviderError:
	"""Map any provider/SDK exception to an AIProviderError without keeping its text."""
	if isinstance(exc, AIProviderError):
		return exc
	if isinstance(exc, (json.JSONDecodeError, ValueError)) and not hasattr(exc, "status_code"):
		return AIProviderError("invalid_response")

	try:
		import openai
	except ImportError:  # pragma: no cover
		openai = None
	if openai is not None:
		if isinstance(exc, openai.APITimeoutError):
			return AIProviderError("timeout")
		if isinstance(exc, openai.APIConnectionError):
			return AIProviderError("network")
		if isinstance(exc, openai.APIStatusError):
			error = _from_status(exc.status_code, _text(exc))
			error.retry_after = _retry_after(getattr(getattr(exc, "response", None), "headers", None))
			if error.category == "invalid_response":
				error.failed_generation = _failed_generation(getattr(exc, "body", None))
			return error

	try:
		import httpx

		if isinstance(exc, httpx.TimeoutException):
			return AIProviderError("timeout")
		if isinstance(exc, httpx.TransportError):
			return AIProviderError("network")
	except ImportError:  # pragma: no cover
		pass

	# google-genai errors expose an integer `code`
	code = getattr(exc, "code", None) or getattr(exc, "status_code", None)
	if isinstance(code, int):
		return _from_status(code, _text(exc))
	return AIProviderError("unknown")


def _text(exc):
	parts = [str(getattr(exc, "message", "") or "")]
	body = getattr(exc, "body", None)
	if body is not None:
		parts.append(json.dumps(body, default=str) if not isinstance(body, str) else body)
	return " ".join(parts).lower()


def _failed_generation(body):
	if isinstance(body, dict):
		err = body.get("error") if isinstance(body.get("error"), dict) else body
		value = err.get("failed_generation")
		return value if isinstance(value, str) else None
	return None


def _duration(value):
	"""Parse "7.66s", "1m26.4s", "2h3m", "450ms" or a plain number of seconds."""
	value = str(value or "").strip()
	if not value:
		return None
	try:
		return float(value)
	except ValueError:
		pass
	import re

	total, found = 0.0, False
	for number, unit in re.findall(r"([\d.]+)\s*(ms|h|m|s)", value):
		found = True
		total += float(number) * {"ms": 0.001, "s": 1, "m": 60, "h": 3600}[unit]
	return total if found else None


def _retry_after(headers):
	"""Seconds to wait before retrying, from standard or rate-limit reset headers (None if unknown)."""
	if not headers:
		return None
	if headers.get("retry-after-ms"):
		ms = _duration(headers.get("retry-after-ms"))
		return ms / 1000 if ms is not None else None
	for name in ("retry-after", "x-ratelimit-reset-tokens", "x-ratelimit-reset-requests"):
		seconds = _duration(headers.get(name))
		if seconds is not None:
			return seconds
	return None


def _from_status(status, text):
	if status == 429:
		category = "quota_exhausted" if any(h in text for h in _QUOTA_HINTS) else "rate_limited"
	elif status in (401, 403):
		category = "auth"
	elif status == 402:
		category = "billing"
	elif status == 404:
		category = "config"  # unknown model or endpoint
	elif status == 408:
		category = "timeout"
	elif status == 400 and any(h in text for h in ("json_validate_failed", "failed_generation", "failed to generate json")):
		category = "invalid_response"  # the model produced invalid JSON (Groq JSON mode); retrying may help
	elif status in (400, 413, 415, 422):
		category = "bad_request"
	elif status >= 500:
		category = "unavailable"
	else:
		category = "unknown"
	return AIProviderError(category, status_code=status)


def get_ai_logger():
	"""Logger for logs/ai.log. Callers must pass metadata only (see log_ai_event)."""
	logger = logging.getLogger("ai_erpnext")
	if not logger.handlers:
		try:
			import frappe

			path = os.path.join(frappe.utils.get_bench_path(), "logs", "ai.log")
			handler = logging.FileHandler(path)
		except Exception:
			handler = logging.StreamHandler()
		handler.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))
		logger.addHandler(handler)
		logger.setLevel(logging.INFO)
	return logger


_SAFE_FIELDS = ("provider", "model", "status", "category", "http_status", "elapsed_ms", "retries", "tokens_in", "tokens_out")


def log_ai_event(**fields):
	"""Write one metadata-only line. Unknown keys are dropped so content cannot leak in."""
	line = " ".join(f"{k}={fields[k]}" for k in _SAFE_FIELDS if fields.get(k) is not None)
	level = logging.INFO if fields.get("status") == "ok" else logging.WARNING
	get_ai_logger().log(level, "AI-EXTRACT " + line)


def log_safe_error(title, exc, reference_doctype=None, reference_name=None):
	"""Error Log entry with the code path only.

	frappe.log_error() without a message stores the traceback *with local variables*, which
	here would include extracted invoice data. This keeps file/line/function frames and the
	exception type, never variable values or the exception message.
	"""
	import traceback

	import frappe

	frames = "".join(traceback.format_tb(exc.__traceback__)) if exc.__traceback__ else ""
	frappe.log_error(title=title, message=f"{frames}{type(exc).__name__} (message omitted: may contain document data)",
		reference_doctype=reference_doctype, reference_name=reference_name)
