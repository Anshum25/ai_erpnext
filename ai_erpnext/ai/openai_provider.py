import base64
import json
import time

import openai
from openai import OpenAI

from .errors import AIProviderError, classify, log_ai_event
from .provider import LLMProvider


class OpenAIProvider(LLMProvider):
	"""OpenAI-compatible chat completions provider (used for OpenAI, Groq, OpenRouter, ...).

	Never logs the model response, the image or request headers. Every failure is raised as
	an AIProviderError with a user-safe message. Providers built by ai.providers pass
	max_retries=0 so that ai.policy is the only retry layer; fallback rules live in ai.providers.
	"""

	TIMEOUT_SECONDS = 90  # per attempt
	MAX_RETRIES = 2  # SDK default when max_retries is not given (408/409/429/5xx and connection errors)

	def __init__(self, api_key: str, base_url: str = None, model: str = "gpt-4o", provider_name: str = None, max_retries: int | None = None):
		self.model = model
		self.provider_name = provider_name or "openai-compatible"
		self._requests = 0
		self._last_status = None
		http_client = openai.DefaultHttpxClient(
			event_hooks={"request": [self._on_request], "response": [self._on_response]}
		)
		self.client = OpenAI(
			api_key=api_key,
			base_url=base_url,
			timeout=self.TIMEOUT_SECONDS,
			max_retries=self.MAX_RETRIES if max_retries is None else max_retries,
			http_client=http_client,
		)

	def _on_request(self, request):
		self._requests += 1

	def _on_response(self, response):
		self._last_status = response.status_code

	def extract_document_data(self, file_content: bytes, file_type: str, prompt: str) -> dict:
		mime_type = "application/pdf" if file_type.lower() == "pdf" else f"image/{file_type.lower()}"
		image_url = f"data:{mime_type};base64,{base64.b64encode(file_content).decode('utf-8')}"

		self._requests, self._last_status = 0, None
		started = time.perf_counter()
		meta = {"provider": self.provider_name, "model": self.model}

		try:
			response = self.client.chat.completions.create(
				model=self.model,
				messages=[
					{
						"role": "user",
						"content": [
							{"type": "text", "text": prompt},
							{"type": "image_url", "image_url": {"url": image_url}},
						],
					}
				],
				response_format={"type": "json_object"},
			)
		except Exception as exc:
			error = classify(exc)
			log_ai_event(**meta, status="error", category=error.category, http_status=error.status_code or self._last_status,
				elapsed_ms=self._elapsed(started), retries=max(0, self._requests - 1))
			raise error from None

		usage = getattr(response, "usage", None)
		meta.update(http_status=self._last_status, elapsed_ms=self._elapsed(started), retries=max(0, self._requests - 1),
			tokens_in=getattr(usage, "prompt_tokens", None), tokens_out=getattr(usage, "completion_tokens", None))

		content = response.choices[0].message.content if response.choices else None
		try:
			data = json.loads(content or "")
		except ValueError:
			data = None
		if not isinstance(data, dict):  # the pipeline needs exactly one JSON object
			log_ai_event(**meta, status="error", category="invalid_response")
			raise AIProviderError("invalid_response")

		log_ai_event(**meta, status="ok")
		return data

	def complete_json(self, system: str, messages: list, max_tokens: int = 900, sdk_retries=None) -> dict:
		"""Text-only chat completion returning one JSON object (used by the chat router).
		Same safety rules as extraction: no content logging, bounded retries, safe errors.
		sdk_retries=0 lets the caller own the retry policy so retries never multiply."""
		self._requests, self._last_status = 0, None
		started = time.perf_counter()
		meta = {"provider": self.provider_name, "model": self.model}
		client = self.client if sdk_retries is None else self.client.with_options(max_retries=sdk_retries)
		try:
			response = client.chat.completions.create(
				model=self.model,
				messages=[{"role": "system", "content": system}, *messages],
				response_format={"type": "json_object"},
				temperature=0,
				max_tokens=max_tokens,
			)
		except Exception as exc:
			error = classify(exc)
			log_ai_event(**meta, status="error", category=error.category, limit=error.limit, http_status=error.status_code or self._last_status,
				elapsed_ms=self._elapsed(started), retries=max(0, self._requests - 1))
			raise error from None
		usage = getattr(response, "usage", None)
		meta.update(http_status=self._last_status, elapsed_ms=self._elapsed(started), retries=max(0, self._requests - 1),
			tokens_in=getattr(usage, "prompt_tokens", None), tokens_out=getattr(usage, "completion_tokens", None))
		content = response.choices[0].message.content if response.choices else None
		try:
			data = json.loads(content or "")
		except ValueError:
			data = None
		if not isinstance(data, dict):
			log_ai_event(**meta, status="error", category="invalid_response")
			raise AIProviderError("invalid_response", status_code=self._last_status, failed_generation=content)
		log_ai_event(**meta, status="ok")
		return data

	@staticmethod
	def _elapsed(started):
		return round((time.perf_counter() - started) * 1000)
