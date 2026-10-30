"""Reusable Groq chat-completions wrapper: bounded retries, rate-limit aware, distinct error types."""
from __future__ import annotations

import logging
import os
import time

import groq

from . import config  # noqa: F401  (loads .env)

log = logging.getLogger(__name__)
DEFAULT_MODEL = "llama-3.3-70b-versatile"   # starting point only; verify it is available to your account
# Reasoning models (e.g. openai/gpt-oss-*) spend completion tokens on hidden reasoning before any answer text.
# A small max_tokens is used up by reasoning -> finish_reason="length" and empty content.
REASONING_MODEL_PREFIXES = ("openai/gpt-oss",)
REASONING_MIN_TOKENS = 2048


class LLMError(RuntimeError):
    """Base class."""


class MissingAPIKeyError(LLMError):
    """GROQ_API_KEY is not set."""


class LLMFatalError(LLMError):
    """Auth/permission/bad-request style failure: retrying cannot help."""


class LLMTransientError(LLMError):
    """Rate limit / overload / network problem that persisted through every retry."""


class LLMResponseError(LLMError):
    """The API answered but the response was empty or unusable."""


def _is_transient(exc: groq.APIError) -> bool:
    if isinstance(exc, (groq.RateLimitError, groq.APIConnectionError, groq.InternalServerError)):
        return True
    return isinstance(exc, groq.APIStatusError) and exc.status_code in (408, 409)


def _fatal_message(exc: groq.APIError, model: str) -> str:
    msg = f"{type(exc).__name__}: {exc}"
    status = getattr(exc, "status_code", None)
    if status in (401, 403):
        msg += " - check GROQ_API_KEY"
    elif status in (400, 404):
        msg += f" - check that GROQ_MODEL ('{model}') exists and is available to your account"
    return msg


def _extract_text(resp) -> tuple[str, str | None]:
    """Return (stripped text, finish_reason). Handles str content and lists of text parts."""
    choices = getattr(resp, "choices", None) or []
    if not choices:
        return "", None
    finish = getattr(choices[0], "finish_reason", None)
    content = getattr(getattr(choices[0], "message", None), "content", None)
    if isinstance(content, list):
        content = "".join(p.get("text") or "" if isinstance(p, dict) else getattr(p, "text", None) or ""
                          for p in content)
    return (content if isinstance(content, str) else "").strip(), finish


def _retry_after(exc: groq.APIError) -> float | None:
    headers = getattr(getattr(exc, "response", None), "headers", None) or {}
    try:
        return max(float(headers.get("retry-after")), 0.0)
    except (TypeError, ValueError):
        return None


class GroqService:
    def __init__(self, api_key: str | None = None, model: str | None = None, client=None,
                 max_retries: int = 3, base_delay: float = 2.0, max_delay: float = 30.0,
                 timeout: float = 60.0, sleep=time.sleep):
        self.model = model or os.getenv("GROQ_MODEL", "").strip() or DEFAULT_MODEL
        self.max_retries, self.base_delay, self.max_delay = max_retries, base_delay, max_delay
        self._sleep = sleep
        if client is None:
            key = api_key or os.getenv("GROQ_API_KEY", "").strip()
            if not key:
                raise MissingAPIKeyError("GROQ_API_KEY is not set (see .env.example)")
            client = groq.Groq(api_key=key, max_retries=0, timeout=timeout)  # we own the retries
        self._client = client

    def complete(self, system: str, user: str, max_tokens: int = 600, temperature: float = 0.7) -> str:
        """Return the model's text. Retries transient failures at most max_retries times."""
        last: Exception | None = None
        extra: dict = {}
        if self.model.startswith(REASONING_MODEL_PREFIXES):
            max_tokens = max(max_tokens, REASONING_MIN_TOKENS)      # leave room for the answer after reasoning
            extra["extra_body"] = {"reasoning_effort": "low"}
        for attempt in range(self.max_retries + 1):
            try:
                resp = self._client.chat.completions.create(
                    model=self.model, max_tokens=max_tokens, temperature=temperature, **extra,
                    messages=[{"role": "system", "content": system}, {"role": "user", "content": user}])
            except groq.APIError as exc:
                if not _is_transient(exc):
                    raise LLMFatalError(_fatal_message(exc, self.model)) from exc
                last = exc
                wait = _retry_after(exc)
                if wait is not None and wait > self.max_delay:   # e.g. daily quota: waiting here is pointless
                    raise LLMTransientError(f"{type(exc).__name__}: retry-after {wait:.0f}s exceeds the "
                                            f"{self.max_delay:.0f}s cap - try again later") from exc
                log.warning("Transient API error (%s), attempt %d/%d", type(exc).__name__,
                            attempt + 1, self.max_retries + 1)
                if attempt < self.max_retries:
                    self._sleep(wait if wait is not None else min(self.base_delay * 2 ** attempt, self.max_delay))
                continue
            text, finish = _extract_text(resp)
            if not text:
                hint = " (token limit reached before any answer text)" if finish == "length" else ""
                raise LLMResponseError(f"empty model response, finish_reason={finish}{hint}")
            return text
        raise LLMTransientError(f"gave up after {self.max_retries + 1} attempts: {type(last).__name__}")
