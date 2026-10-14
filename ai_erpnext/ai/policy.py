"""The one retry policy for AI provider calls (invoice extraction and the chat router).

SDK-level retries are disabled for providers built by ai.providers, so retries never
multiply and every rule lives here:
- timeouts, network errors, 5xx: exponential backoff with jitter;
- 429 per-minute limits: wait for the provider's retry-after, only if it fits the budget;
- exhausted daily quota, auth/config/billing errors, invalid requests: never retried.
"""

import random
import time

from ai_erpnext.ai.errors import AIProviderError

TRANSIENT = ("timeout", "network", "unavailable")
BACKOFF_BASE_S, BACKOFF_CAP_S = 0.5, 4.0


def retry_delay(error, attempt, jitter=random.random):
	"""Seconds to wait before retrying after `error` on attempt number `attempt`, or None if it must not be retried."""
	if error.category in TRANSIENT:
		return min(BACKOFF_CAP_S, BACKOFF_BASE_S * 2 ** (attempt - 1)) * (0.5 + jitter() / 2)
	if error.category == "rate_limited" and error.retry_after is not None:
		return error.retry_after + jitter() * 0.25
	return None


def call_with_retries(fn, max_attempts, budget_s, sleep=None, jitter=random.random):
	"""Run fn() with at most max_attempts attempts and at most budget_s seconds of waiting in total."""
	sleep = sleep or time.sleep  # looked up per call so tests can patch time.sleep
	attempt, waited = 0, 0.0
	while True:
		attempt += 1
		try:
			return fn()
		except AIProviderError as error:
			delay = retry_delay(error, attempt, jitter)
			if delay is None or attempt >= max_attempts or waited + delay > budget_s:
				raise
			sleep(delay)
			waited += delay
