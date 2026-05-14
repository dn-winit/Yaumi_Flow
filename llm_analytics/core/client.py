"""
Provider-agnostic LLM client. Supports Groq, OpenAI, Anthropic via config.

Error handling contract:

  * ``RateLimitError`` / 429s    -> wait Retry-After header (or
                                    exponential backoff) and bubble up
                                    to the analyzer's retry loop.
  * ``TruncatedResponseError``   -> raised when the provider reports
                                    ``finish_reason='length'``. Fatal --
                                    the analyzer must not retry the
                                    same prompt without more budget.
  * Any other exception          -> bubbles up; the analyzer's retry
                                    loop decides whether to fall back.

Logged via one structured ``llm_call ...`` line per attempt so token
spend, latency, and outcome are visible at INFO level.
"""

from __future__ import annotations

import json
import logging
import random
import time
from typing import Any, Dict, Optional

from llm_analytics.config.settings import Settings, get_settings

logger = logging.getLogger(__name__)


class TruncatedResponseError(RuntimeError):
    """Raised when the provider hit ``max_tokens`` mid-response.

    The analyzer treats this as fatal because retrying the same prompt
    against the same model + budget will hit the same wall. Surfacing
    the dedicated exception class also makes operational dashboards
    distinguish "we need more tokens" from "the model failed parse".
    """


def _log_call(provider: str, model: str, outcome: str,
              usage: Optional[Dict[str, Any]], elapsed_ms: float) -> None:
    u = usage or {}
    logger.info(
        "llm_call provider=%s model=%s outcome=%s input_tokens=%s output_tokens=%s elapsed_ms=%.0f",
        provider, model, outcome,
        u.get("input_tokens", "-"),
        u.get("output_tokens", "-"),
        elapsed_ms,
    )


def _retry_after_seconds(exc: Exception, default: float) -> float:
    """Extract Retry-After (seconds) from a provider's RateLimitError.

    Both Groq and OpenAI surface the raw HTTP response on the exception.
    Falls back to ``default`` if the header is missing or unparseable.
    """
    resp = getattr(exc, "response", None)
    headers = getattr(resp, "headers", None) if resp is not None else None
    if headers:
        raw = headers.get("retry-after") or headers.get("Retry-After")
        if raw:
            try:
                return max(0.0, float(raw))
            except (TypeError, ValueError):
                pass
    return default


class LLMClient:
    """Sends messages to the configured LLM provider and returns parsed JSON."""

    def __init__(self, settings: Optional[Settings] = None) -> None:
        self._s = settings or get_settings()
        self._client: Any = None
        self.is_available = False
        # Provider-specific exception classes, looked up once so the
        # hot path doesn't pay the import cost on every error.
        self._rate_limit_cls: tuple = ()
        self._init_client()

    def _init_client(self) -> None:
        if not self._s.api_key:
            logger.info("No LLM API key configured -- analysis features disabled")
            return

        provider = self._s.provider
        try:
            if provider == "groq":
                from groq import Groq, RateLimitError as GroqRateLimit
                self._client = Groq(api_key=self._s.api_key, timeout=self._s.timeout)
                self._rate_limit_cls = (GroqRateLimit,)
            elif provider == "openai":
                from openai import OpenAI, RateLimitError as OAIRateLimit
                self._client = OpenAI(api_key=self._s.api_key, timeout=self._s.timeout)
                self._rate_limit_cls = (OAIRateLimit,)
            elif provider == "anthropic":
                import anthropic
                self._client = anthropic.Anthropic(
                    api_key=self._s.api_key, timeout=self._s.timeout,
                )
                self._rate_limit_cls = (getattr(anthropic, "RateLimitError", Exception),)
            self.is_available = True
            logger.info("LLM client initialized: provider=%s, model=%s", provider, self._s.model)
        except Exception as exc:
            logger.warning("Failed to init LLM client (%s): %s", provider, exc)

    def chat(self, system_prompt: str, user_prompt: str, attempt: int = 0) -> Dict[str, Any]:
        """Send a chat request and return parsed JSON response."""
        if not self.is_available:
            raise RuntimeError("LLM client not available -- check API key and provider")

        provider = self._s.provider

        if provider in ("groq", "openai"):
            return self._chat_openai_compat(system_prompt, user_prompt, attempt)
        if provider == "anthropic":
            return self._chat_anthropic(system_prompt, user_prompt, attempt)
        raise RuntimeError(f"Unsupported provider: {provider}")

    def _chat_openai_compat(self, system: str, user: str, attempt: int) -> Dict[str, Any]:
        """OpenAI-compatible API (Groq, OpenAI). Honours Retry-After on
        429 and applies bounded exponential backoff for other transient
        failures. Surfaces ``finish_reason='length'`` as a dedicated
        ``TruncatedResponseError`` rather than parsing a truncated JSON."""
        t0 = time.monotonic()
        try:
            response = self._client.chat.completions.create(
                model=self._s.model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                temperature=self._s.temperature,
                max_tokens=self._s.max_tokens,
                top_p=self._s.top_p,
                seed=self._s.seed + attempt,
                response_format={"type": "json_object"},
            )
        except self._rate_limit_cls as exc:
            elapsed_ms = (time.monotonic() - t0) * 1000.0
            wait = _retry_after_seconds(exc, default=min(2 ** attempt + random.random(), 30.0))
            _log_call(self._s.provider, self._s.model, f"rate_limited:{wait:.1f}s", None, elapsed_ms)
            # Sleep here rather than in the analyzer because the wait
            # is provider-state-specific: bubbling up would lose the
            # Retry-After signal.
            time.sleep(min(wait, 30.0))
            raise

        elapsed_ms = (time.monotonic() - t0) * 1000.0
        u = getattr(response, "usage", None)
        usage = {
            "input_tokens":  getattr(u, "prompt_tokens", None),
            "output_tokens": getattr(u, "completion_tokens", None),
        } if u else {}

        choice = response.choices[0]
        finish_reason = getattr(choice, "finish_reason", None)
        if finish_reason == "length":
            _log_call(self._s.provider, self._s.model, "truncated", usage, elapsed_ms)
            raise TruncatedResponseError(
                f"finish_reason=length at max_tokens={self._s.max_tokens}"
            )

        _log_call(self._s.provider, self._s.model, "ok", usage, elapsed_ms)
        return self._parse_json(choice.message.content)

    def _chat_anthropic(self, system: str, user: str, attempt: int) -> Dict[str, Any]:
        """Anthropic Messages API. Same Retry-After + truncation
        contract as the OpenAI-compatible path."""
        t0 = time.monotonic()
        try:
            response = self._client.messages.create(
                model=self._s.model,
                max_tokens=self._s.max_tokens,
                system=system,
                messages=[{"role": "user", "content": user}],
                temperature=self._s.temperature,
                top_p=self._s.top_p,
            )
        except self._rate_limit_cls as exc:
            elapsed_ms = (time.monotonic() - t0) * 1000.0
            wait = _retry_after_seconds(exc, default=min(2 ** attempt + random.random(), 30.0))
            _log_call(self._s.provider, self._s.model, f"rate_limited:{wait:.1f}s", None, elapsed_ms)
            time.sleep(min(wait, 30.0))
            raise

        elapsed_ms = (time.monotonic() - t0) * 1000.0
        u = getattr(response, "usage", None)
        usage = {
            "input_tokens":  getattr(u, "input_tokens", None),
            "output_tokens": getattr(u, "output_tokens", None),
        } if u else {}

        stop_reason = getattr(response, "stop_reason", None)
        if stop_reason == "max_tokens":
            _log_call(self._s.provider, self._s.model, "truncated", usage, elapsed_ms)
            raise TruncatedResponseError(
                f"stop_reason=max_tokens at max_tokens={self._s.max_tokens}"
            )

        _log_call(self._s.provider, self._s.model, "ok", usage, elapsed_ms)
        return self._parse_json(response.content[0].text)

    @staticmethod
    def _parse_json(raw: str) -> Dict[str, Any]:
        """Strip markdown fences and parse JSON.

        Tolerates models that emit a leading ```json ... ``` fence
        despite ``response_format=json_object`` being set (we have seen
        this from llama-3.1-8b on Groq). Anything more exotic (trailing
        commas, comments) is left to surface as a parse error so the
        retry loop catches it.
        """
        text = raw.strip()
        if text.startswith("```"):
            lines = [l for l in text.split("\n") if not l.strip().startswith("```")]
            text = "\n".join(lines)
        return json.loads(text)

    def health(self) -> Dict[str, Any]:
        return {
            "available": self.is_available,
            "provider": self._s.provider,
            "model": self._s.model,
        }
