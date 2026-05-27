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

Every call emits one structured ``llm_call ...`` log line AND appends one
``CostEntry`` to the process cost tracker. The log line is the forensic
audit trail; the tracker backs the live ``/cost`` endpoints.
"""

from __future__ import annotations

import json
import logging
import random
import re
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from llm_analytics.config.settings import Settings, get_settings
from llm_analytics.services.cost_tracker import CostEntry, get_cost_tracker

logger = logging.getLogger(__name__)


class TruncatedResponseError(RuntimeError):
    """Raised when the provider hit ``max_tokens`` mid-response.

    The analyzer treats this as fatal because retrying the same prompt
    against the same model + budget will hit the same wall. Surfacing
    the dedicated exception class also makes operational dashboards
    distinguish "we need more tokens" from "the model failed parse".
    """


def _emit_call_record(
    provider: str, model: str, outcome: str,
    usage: Optional[Dict[str, Any]], elapsed_ms: float,
    *, artifact: str, route_code: str, customer_code: str,
    cache_hit: bool = False,
) -> None:
    """Single side-effect for every LLM call: log line + cost-tracker append.

    Co-locating both writes guarantees the audit trail and the live
    aggregation can't diverge. Caller passes the artifact/route/customer
    context so the cost endpoint can slice by any of them.
    """
    u = usage or {}
    in_tok  = u.get("input_tokens")  or 0
    out_tok = u.get("output_tokens") or 0
    logger.info(
        "llm_call provider=%s model=%s artifact=%s outcome=%s "
        "input_tokens=%s output_tokens=%s elapsed_ms=%.0f "
        "route=%s customer=%s cache_hit=%s",
        provider, model, artifact, outcome,
        in_tok if in_tok else "-",
        out_tok if out_tok else "-",
        elapsed_ms,
        route_code or "-", customer_code or "-", cache_hit,
    )
    get_cost_tracker().record(CostEntry(
        timestamp=datetime.now(timezone.utc),
        artifact=artifact,
        model=model,
        provider=provider,
        input_tokens=int(in_tok or 0),
        output_tokens=int(out_tok or 0),
        elapsed_ms=float(elapsed_ms),
        outcome=outcome,
        cache_hit=cache_hit,
        route_code=route_code or "",
        customer_code=customer_code or "",
    ))


def record_cache_hit(
    *, artifact: str, route_code: str = "", customer_code: str = "",
) -> None:
    """Cost-tracker entry for a cached answer (no LLM call). Zero tokens,
    zero cost, but appears in the call rate + cache_hit_rate metrics."""
    s = get_settings()
    _emit_call_record(
        s.provider, s.model, "cache_hit", None, 0.0,
        artifact=artifact, route_code=route_code, customer_code=customer_code,
        cache_hit=True,
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

    def chat(
        self,
        system_prompt: str,
        user_prompt: str,
        attempt: int = 0,
        *,
        artifact: str = "unknown",
        route_code: str = "",
        customer_code: str = "",
        max_tokens: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Send a chat request and return parsed JSON response.

        ``artifact``, ``route_code``, ``customer_code`` flow into the cost
        tracker so the /cost endpoints can slice spend by any of them.
        ``max_tokens`` overrides the settings default (per-artifact sizing
        is owned by the analyzer; this is the wire override).
        """
        if not self.is_available:
            raise RuntimeError("LLM client not available -- check API key and provider")

        provider = self._s.provider
        effective_max = int(max_tokens or self._s.max_tokens)

        if provider in ("groq", "openai"):
            return self._chat_openai_compat(
                system_prompt, user_prompt, attempt,
                artifact=artifact, route_code=route_code,
                customer_code=customer_code, max_tokens=effective_max,
            )
        if provider == "anthropic":
            return self._chat_anthropic(
                system_prompt, user_prompt, attempt,
                artifact=artifact, route_code=route_code,
                customer_code=customer_code, max_tokens=effective_max,
            )
        raise RuntimeError(f"Unsupported provider: {provider}")

    def _chat_openai_compat(
        self, system: str, user: str, attempt: int,
        *, artifact: str, route_code: str, customer_code: str, max_tokens: int,
    ) -> Dict[str, Any]:
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
                max_tokens=max_tokens,
                top_p=self._s.top_p,
                seed=self._s.seed + attempt,
                response_format={"type": "json_object"},
            )
        except self._rate_limit_cls as exc:
            elapsed_ms = (time.monotonic() - t0) * 1000.0
            wait = _retry_after_seconds(exc, default=min(2 ** attempt + random.random(), 30.0))
            _emit_call_record(
                self._s.provider, self._s.model, f"rate_limited:{wait:.1f}s",
                None, elapsed_ms,
                artifact=artifact, route_code=route_code, customer_code=customer_code,
            )
            # Sleep here rather than in the analyzer because the wait is
            # provider-state-specific: bubbling up would lose the Retry-After
            # signal. Cap so a misconfigured Retry-After can't park the worker.
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
            _emit_call_record(
                self._s.provider, self._s.model, "truncated", usage, elapsed_ms,
                artifact=artifact, route_code=route_code, customer_code=customer_code,
            )
            raise TruncatedResponseError(
                f"finish_reason=length at max_tokens={max_tokens} for {artifact}"
            )

        _emit_call_record(
            self._s.provider, self._s.model, "ok", usage, elapsed_ms,
            artifact=artifact, route_code=route_code, customer_code=customer_code,
        )
        return self._parse_json(choice.message.content)

    def _chat_anthropic(
        self, system: str, user: str, attempt: int,
        *, artifact: str, route_code: str, customer_code: str, max_tokens: int,
    ) -> Dict[str, Any]:
        """Anthropic Messages API. Same Retry-After + truncation
        contract as the OpenAI-compatible path."""
        t0 = time.monotonic()
        try:
            response = self._client.messages.create(
                model=self._s.model,
                max_tokens=max_tokens,
                system=system,
                messages=[{"role": "user", "content": user}],
                temperature=self._s.temperature,
                top_p=self._s.top_p,
            )
        except self._rate_limit_cls as exc:
            elapsed_ms = (time.monotonic() - t0) * 1000.0
            wait = _retry_after_seconds(exc, default=min(2 ** attempt + random.random(), 30.0))
            _emit_call_record(
                self._s.provider, self._s.model, f"rate_limited:{wait:.1f}s",
                None, elapsed_ms,
                artifact=artifact, route_code=route_code, customer_code=customer_code,
            )
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
            _emit_call_record(
                self._s.provider, self._s.model, "truncated", usage, elapsed_ms,
                artifact=artifact, route_code=route_code, customer_code=customer_code,
            )
            raise TruncatedResponseError(
                f"stop_reason=max_tokens at max_tokens={max_tokens} for {artifact}"
            )

        _emit_call_record(
            self._s.provider, self._s.model, "ok", usage, elapsed_ms,
            artifact=artifact, route_code=route_code, customer_code=customer_code,
        )
        return self._parse_json(response.content[0].text)

    @staticmethod
    def _parse_json(raw: str) -> Dict[str, Any]:
        """Extract + parse a JSON object from arbitrary model output.

        Defense chain (each layer only runs if the prior raised):
          1. Strict json.loads on the trimmed input -- the happy path.
          2. Strip ```json/``` fences if present.
          3. Slice the first balanced ``{...}`` block out of any prose
             prefix/suffix (some models with response_format=json_object
             still emit a leading "Here is the JSON:" sentence).
          4. Light repair: remove trailing commas before ``}``/``]``,
             normalise smart-quotes that some templates inject.
          5. If all layers fail, raise the ORIGINAL exception with the
             raw payload's first 300 chars so the retry log is actionable.
        """
        text = (raw or "").strip()
        if not text:
            raise ValueError("LLM returned empty content")

        # Layer 1: try as-is.
        try:
            return json.loads(text)
        except json.JSONDecodeError as primary_exc:
            primary = primary_exc

        # Layer 2: strip code fences.
        if "```" in text:
            stripped = "\n".join(
                line for line in text.split("\n")
                if not line.strip().startswith("```")
            ).strip()
            try:
                return json.loads(stripped)
            except json.JSONDecodeError:
                text = stripped  # carry the fence-stripped form into layer 3

        # Layer 3: extract the first balanced JSON object.
        sliced = _extract_first_json_object(text)
        if sliced:
            try:
                return json.loads(sliced)
            except json.JSONDecodeError:
                text = sliced

        # Layer 4: light repair (trailing commas, smart quotes).
        repaired = _repair_json(text)
        try:
            return json.loads(repaired)
        except json.JSONDecodeError:
            pass

        raise ValueError(
            f"LLM response not parseable as JSON after fence/slice/repair: "
            f"{primary.msg!r}; raw[:300]={raw[:300]!r}"
        )

    def health(self) -> Dict[str, Any]:
        return {
            "available": self.is_available,
            "provider": self._s.provider,
            "model": self._s.model,
        }


def _extract_first_json_object(text: str) -> str:
    """Find the first balanced ``{...}`` substring or return ''.

    Walks the string tracking brace depth + string-literal state so that
    a ``}`` inside a string value can't terminate the slice early.
    """
    start = text.find("{")
    if start < 0:
        return ""
    depth = 0
    in_string = False
    escape = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return ""


_TRAILING_COMMA_RE = re.compile(r",(\s*[}\]])")


def _repair_json(text: str) -> str:
    """Best-effort fixup for common LLM JSON quirks.

    Keeps the change set tiny so a real malformed response still fails
    visibly rather than silently mis-parsing into wrong structure.
    """
    fixed = text.replace("“", '"').replace("”", '"')
    fixed = fixed.replace("‘", "'").replace("’", "'")
    fixed = _TRAILING_COMMA_RE.sub(r"\1", fixed)
    return fixed
