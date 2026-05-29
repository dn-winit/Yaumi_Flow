"""
Main analyzer -- orchestrates client, prompts, formatting, caching, rate limiting.

Three production-grade invariants the rest of the module enforces:

  1. **Cache key reflects content.** Every cache call passes the full
     identity of the call (route+date+customer **plus** a content
     signature **plus** model name **plus** prompt-source hash). A
     visit's analysis cached at 09:00 with zero actuals does not get
     served back at 17:00 after the sales have landed.

  2. **Fallbacks never persist.** When the LLM is rate-limited, errors,
     or returns garbage, the analyzer returns a ``_degraded=True`` dict.
     The cache is bypassed and the API layer downgrades ``success`` to
     ``False`` so the supervision saver skips DB write -- a later tick
     will retry on a clean slate.

  3. **Empty-state short-circuit.** If the input genuinely has no items
     or no sales activity, we synthesise a deterministic, honest
     response (and cache it cheaply) instead of letting the LLM
     hallucinate "clean slate / build relationship" prose from nothing.
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from llm_analytics.config.settings import Settings, get_settings
from llm_analytics.core.client import LLMClient, TruncatedResponseError, record_cache_hit
from llm_analytics.core.formatter import DataFormatter
from llm_analytics.core.prompt_loader import PromptLoader
from llm_analytics.core.schema_render import render_schema_for_prompt
from llm_analytics.core.validator import (
    CUSTOMER_ANALYSIS_TEXT_FIELDS,
    PRE_VISIT_BRIEFING_TEXT_FIELDS,
    ROUTE_ANALYSIS_TEXT_FIELDS,
    scrub_hallucinated_entities,
)
from llm_analytics.models.schemas import CustomerAnalysis, PreVisitBriefing, RouteAnalysis
from llm_analytics.services.cache import LLMCache
from llm_analytics.services.rate_limiter import RateLimiter

logger = logging.getLogger(__name__)

# Marker key on responses that did not come from a successful LLM call.
# The API route reads this to downgrade the HTTP success flag; the cache
# write path reads it to skip persistence; the supervision saver reads
# the downgraded success flag to skip the DB UPDATE. One marker, three
# enforcement points -- impossible for a fallback to silently land in the
# user-facing column.
DEGRADED_KEY = "_degraded"


def _items_signature(items: list[dict[str, Any]]) -> str:
    """Stable digest over EVERY per-item field the prompt actually reads.

    Folds quantities (rec, actual), tier, frequency, days_since, cycle,
    AND the explainability fields (why_item, why_quantity, trend, source)
    because the briefing template renders all of them. If a rec-engine
    edit changes ``why_item`` for an existing recommendation, the cache
    MUST miss so the new narrative is generated. Pre-fix the signature
    only hashed (code, rec, act), so explainability edits served stale
    briefings for up to 24h.
    """
    from llm_analytics.core.formatter import _pick
    parts = []
    for it in items or []:
        code  = str(_pick(it, "item_code", "itemCode", "ItemCode", default=""))
        rec   = int(_pick(it, *_REC_QTY_KEYS, default=0) or 0)
        act   = int(_pick(it, *_ACT_QTY_KEYS, default=0) or 0)
        tier  = str(_pick(it, "tier", "Tier", default=""))
        freq  = round(float(_pick(it, "frequency_percent", "frequencyPercent", "FrequencyPercent", default=0) or 0), 1)
        last  = int(_pick(it, "days_since_last_purchase", "daysSinceLastPurchase", "DaysSinceLastPurchase", default=0) or 0)
        cycle = round(float(_pick(it, "purchase_cycle_days", "purchaseCycleDays", "PurchaseCycleDays", default=0) or 0), 1)
        why_i = str(_pick(it, "why_item", "whyItem", "WhyItem", default=""))
        why_q = str(_pick(it, "why_quantity", "whyQuantity", "WhyQuantity", default=""))
        src   = str(_pick(it, "source", "Source", default=""))
        trend = str(_pick(it, "trend_factor", "trendFactor", "TrendFactor", default=""))
        parts.append(
            f"{code}|{rec}|{act}|{tier}|{freq}|{last}|{cycle}|{why_i}|{why_q}|{src}|{trend}"
        )
    parts.sort()
    return hashlib.sha256("\n".join(parts).encode()).hexdigest()[:16]


# Canonical key tuples live in formatter.py so the analyzer + formatter
# read the same naming-convention list. Re-aliased here so existing call
# sites stay readable. If a new naming convention shows up from a new
# producer, add it to formatter.py once and every consumer picks it up.
from llm_analytics.core.formatter import (
    ACT_QTY_KEYS as _ACT_QTY_KEYS,
)
from llm_analytics.core.formatter import (
    REC_QTY_KEYS as _REC_QTY_KEYS,
)


def _is_empty_visit(items: list[dict[str, Any]]) -> bool:
    """No items at all OR every item has zero recommended *and* zero actual."""
    from llm_analytics.core.formatter import _pick
    if not items:
        return True
    for it in items:
        rec = int(_pick(it, *_REC_QTY_KEYS, default=0) or 0)
        act = int(_pick(it, *_ACT_QTY_KEYS, default=0) or 0)
        if rec > 0 or act > 0:
            return False
    return True


def _prompts_version(prompts_dir: str) -> str:
    """Hash the prompt YAMLs so a prompt edit auto-invalidates the cache.

    Walks every ``*.yaml`` under ``prompts_dir`` (deterministic ordering
    by path) and folds the content bytes into a sha256. Cheap -- runs
    once at analyzer construction.
    """
    h = hashlib.sha256()
    root = Path(prompts_dir)
    if not root.exists():
        return "no-prompts"
    for f in sorted(root.rglob("*.yaml")):
        try:
            h.update(f.name.encode())
            h.update(f.read_bytes())
        except Exception:
            continue
    return h.hexdigest()[:12]


class Analyzer:
    """Production-grade analysis engine with cache, rate limiting, retries."""

    def __init__(self, settings: Settings | None = None) -> None:
        self._s = settings or get_settings()
        self._client = LLMClient(self._s)
        self._prompts = PromptLoader(self._s)
        self._formatter = DataFormatter(self._s)
        self._cache = LLMCache(
            cache_dir=self._s.cache_dir,
            ttl_hours=self._s.cache_ttl_hours,
            enabled=self._s.cache_enabled,
            janitor_every_n_writes=self._s.cache_janitor_every_n_writes,
        )
        self._limiter = RateLimiter(
            max_requests=self._s.rate_limit_max_requests,
            window_seconds=self._s.rate_limit_window_seconds,
            acquire_timeout_seconds=self._s.rate_limit_acquire_timeout_seconds,
            poll_interval_seconds=self._s.rate_limit_poll_interval_seconds,
        )
        # Snapshot of the model + prompt source at construction. Folded
        # into every cache key so a model swap or prompt edit invalidates
        # the cache without an explicit clear.
        self._cache_version = f"{self._s.model}|{_prompts_version(self._s.prompts_dir)}"
        # Per-artifact schema text -- built once at construction from the
        # Pydantic models so the system prompt and the validator never drift.
        # If a field is added/renamed in models/schemas.py the next service
        # restart picks it up without any prompt YAML edit.
        self._schema_text: dict[str, str] = {
            "pre_visit_briefing":  render_schema_for_prompt(PreVisitBriefing),
            "customer_analysis":   render_schema_for_prompt(CustomerAnalysis),
            "route_analysis":      render_schema_for_prompt(RouteAnalysis),
        }

    def _system_with_schema(self, artifact: str) -> str:
        """System prompt + the Pydantic-rendered output schema.

        Single source of truth: the model is shown the exact contract the
        post-call Pydantic validator will enforce. No YAML edit can break
        this alignment.
        """
        base = self._prompts.get_system_prompt(artifact)
        schema = self._schema_text.get(artifact, "")
        if not schema:
            return base
        return (
            f"{base}\n\n"
            "OUTPUT FORMAT (VALID JSON, MATCHING THIS SCHEMA EXACTLY):\n"
            f"{schema}\n\n"
            "Do not include keys outside the schema. Do not wrap in markdown fences. "
            "Do not add prose outside the JSON object. Every code/identifier you "
            "cite MUST appear in the data section of the user message -- "
            "no inventing item codes, customer codes, or percentages."
        )

    # ------------------------------------------------------------------
    # Customer analysis
    # ------------------------------------------------------------------

    def analyze_customer(
        self,
        customer_code: str,
        route_code: str,
        date: str,
        current_items: list[dict[str, Any]],
        performance_score: float = 0.0,
        coverage: float = 0.0,
        accuracy: float = 0.0,
    ) -> dict[str, Any]:
        cache_kwargs = dict(
            customer_code=customer_code, route_code=route_code, date=date,
            _v=self._cache_version,
            _content=_items_signature(current_items),
            _score=int(round(performance_score)),
        )
        cached = self._cache.get("customer_analysis", **cache_kwargs)
        if cached:
            record_cache_hit(artifact="customer_analysis",
                             route_code=route_code, customer_code=customer_code)
            return cached

        # Empty-state contract: honest deterministic response. Saves a
        # rate-limit slot and an LLM call, and -- critically -- prevents
        # the model from inventing "clean slate / build relationship"
        # prose when there is literally no data to analyse.
        #
        # Marked degraded so the API layer downgrades ``success`` to
        # False and supervision skips the DB save. The next tick (after
        # real actuals land) regenerates against a non-empty payload
        # and writes the real analysis. Without this, the empty-state
        # placeholder would land in the column and the saver's
        # ``WHERE column IS NULL`` guard would block the real analysis
        # from ever being written.
        if _is_empty_visit(current_items):
            return self._empty_customer_response(customer_code)

        if not self._limiter.acquire():
            return self._fallback("customer", customer_code=customer_code,
                                  reason="Rate limit exceeded")

        system, user = self.build_customer_prompt(
            customer_code=customer_code, route_code=route_code, date=date,
            current_items=current_items,
            performance_score=performance_score, coverage=coverage, accuracy=accuracy,
        )

        result = self._call_with_retry(
            system, user, CustomerAnalysis, "customer_analysis",
            route_code=route_code, customer_code=customer_code,
            max_tokens=self._s.max_tokens_customer_analysis,
        )
        result["customer_code"] = customer_code
        # Scrub any item code the model cited that wasn't in the prompt.
        # Defense in depth against hallucination -- the schema instruction is
        # the first line; the validator below is the unbypassable second line.
        from llm_analytics.core.formatter import _pick
        allowed_items = {
            str(_pick(it, "item_code", "itemCode", "ItemCode", default=""))
            for it in current_items if it
        } - {""}
        scrub_hallucinated_entities(
            result,
            allowed_items=allowed_items,
            allowed_customers={customer_code},
            text_fields=CUSTOMER_ANALYSIS_TEXT_FIELDS,
        )
        if not result.get(DEGRADED_KEY):
            self._cache.set("customer_analysis", result, **cache_kwargs)
        return result

    # ------------------------------------------------------------------
    # Route analysis
    # ------------------------------------------------------------------

    def analyze_route(
        self,
        route_code: str,
        date: str,
        visited_customers: list[dict[str, Any]],
        total_customers: int,
        total_actual: int = 0,
        total_recommended: int = 0,
        actual_customer_codes: set[str] | None = None,
    ) -> dict[str, Any]:
        # Route signature folds in every visited customer's totals so the
        # cache distinguishes "13/40 customers in, partial actuals" from
        # "40/40 in, full actuals" -- the two states produce different
        # narratives and must not collide.
        from llm_analytics.core.formatter import _pick
        route_sig = hashlib.sha256(
            "|".join(
                f"{_pick(c, 'customer_code', 'customerCode', default='')}:"
                f"{int(_pick(c, 'total_actual', 'totalActual', default=0) or 0)}:"
                f"{int(_pick(c, 'total_recommended', 'totalRecommended', default=0) or 0)}"
                for c in sorted(
                    visited_customers or [],
                    key=lambda x: str(_pick(x, "customer_code", "customerCode", default="")),
                )
            ).encode()
        ).hexdigest()[:16]

        cache_kwargs = dict(
            route_code=route_code, date=date,
            _v=self._cache_version,
            _content=route_sig,
            _visited=len(visited_customers or []),
        )
        cached = self._cache.get("route_analysis", **cache_kwargs)
        if cached:
            record_cache_hit(artifact="route_analysis", route_code=route_code)
            return cached

        if not visited_customers:
            return self._empty_route_response(route_code)

        if not self._limiter.acquire():
            return self._fallback("route", route_code=route_code, reason="Rate limit exceeded")

        system, user = self.build_route_prompt(
            route_code=route_code, date=date,
            visited_customers=visited_customers,
            total_customers=total_customers,
            total_actual=total_actual, total_recommended=total_recommended,
        )

        result = self._call_with_retry(
            system, user, RouteAnalysis, "route_analysis",
            route_code=route_code,
            max_tokens=self._s.max_tokens_route_analysis,
        )
        result["route_code"] = route_code

        # Customer-code whitelist (item codes are not in the route prompt
        # so allowed_items is empty -- any item-code-looking token in the
        # output is a hallucination by definition).
        scrub_hallucinated_entities(
            result,
            allowed_items=(),
            allowed_customers=actual_customer_codes or set(),
            text_fields=ROUTE_ANALYSIS_TEXT_FIELDS,
        )

        if not result.get(DEGRADED_KEY):
            self._cache.set("route_analysis", result, **cache_kwargs)
        return result

    # ------------------------------------------------------------------
    # Pre-visit briefing
    # ------------------------------------------------------------------

    def pre_visit_briefing(
        self,
        customer_code: str,
        customer_name: str,
        route_code: str,
        date: str,
        items: list[dict[str, Any]],
    ) -> dict[str, Any]:
        cache_kwargs = dict(
            customer_code=customer_code, route_code=route_code, date=date,
            analysis_type="pre_visit",
            _v=self._cache_version,
            _content=_items_signature(items),
        )
        # Cache lookup records a hit so /cost endpoints see cached answers
        # as zero-cost calls and cache_hit_rate is meaningful.
        cached = self._cache.get("pre_visit_briefing", **cache_kwargs)
        if cached:
            record_cache_hit(
                artifact="pre_visit_briefing",
                route_code=route_code, customer_code=customer_code,
            )
            return cached

        if _is_empty_visit(items):
            return self._empty_briefing_response(customer_code)

        if not self._limiter.acquire():
            return self._fallback("pre_visit", customer_code=customer_code, reason="Rate limit exceeded")

        system, user = self.build_pre_visit_prompt(
            customer_code=customer_code, customer_name=customer_name,
            route_code=route_code, date=date, items=items,
        )

        result = self._call_with_retry(
            system, user, PreVisitBriefing, "pre_visit_briefing",
            route_code=route_code, customer_code=customer_code,
            max_tokens=self._s.max_tokens_briefing,
        )
        result["customer_code"] = customer_code
        # Scrub hallucinated item codes -- briefing must only cite items
        # that were actually in the recommendations payload.
        from llm_analytics.core.formatter import _pick as _pick_field
        allowed_items = {
            str(_pick_field(it, "item_code", "itemCode", "ItemCode", default=""))
            for it in items if it
        } - {""}
        scrub_hallucinated_entities(
            result,
            allowed_items=allowed_items,
            allowed_customers={customer_code},
            text_fields=PRE_VISIT_BRIEFING_TEXT_FIELDS,
        )
        if not result.get(DEGRADED_KEY):
            self._cache.set("pre_visit_briefing", result, **cache_kwargs)
        return result

    # ------------------------------------------------------------------
    # Prompt builders -- single source of truth shared by live analyze
    # methods and the /analyze/*/preview endpoints. Pure functions of
    # their inputs + the loaded system prompt; no LLM call, no cache, no
    # rate-limit. Adding a new field to a prompt happens here once and
    # both live and preview pick it up.
    # ------------------------------------------------------------------

    def build_customer_prompt(
        self, *, customer_code: str, route_code: str, date: str,
        current_items: list[dict[str, Any]],
        performance_score: float, coverage: float, accuracy: float,
    ) -> tuple[str, str]:
        visit_table = self._formatter.format_current_visit(current_items)
        system = self._system_with_schema("customer_analysis")
        user = self._prompts.render(
            "customer_analysis", "customer_analysis_template",
            customer_code=customer_code, route_code=route_code, date=date,
            performance_score=round(float(performance_score), 1),
            coverage=round(float(coverage), 1),
            accuracy=round(float(accuracy), 1),
            current_visit_table=visit_table,
        )
        return system, user

    def build_route_prompt(
        self, *, route_code: str, date: str,
        visited_customers: list[dict[str, Any]],
        total_customers: int,
        total_actual: int = 0, total_recommended: int = 0,
    ) -> tuple[str, str]:
        visited = len(visited_customers or [])
        coverage_pct = (visited / max(total_customers, 1)) * 100
        qty_achievement = (total_actual / max(total_recommended, 1)) * 100
        actual_data = self._formatter.format_route_performance(visited_customers or [])
        system = self._system_with_schema("route_analysis")
        user = self._prompts.render(
            "route_analysis", "route_analysis_template",
            route_code=route_code, date=date,
            visited_customers=visited, total_customers=total_customers,
            coverage_percentage=coverage_pct,
            total_actual=int(total_actual), total_recommended=int(total_recommended),
            quantity_achievement=qty_achievement,
            actual_data=actual_data,
        )
        return system, user

    def build_pre_visit_prompt(
        self, *, customer_code: str, customer_name: str,
        route_code: str, date: str, items: list[dict[str, Any]],
    ) -> tuple[str, str]:
        """Render the briefing prompt with the rich items table that carries
        every explainability field the prompt template references."""
        from llm_analytics.core.formatter import _pick as _get
        # Local _safe vs the formatter's _sanitize: formatter sanitises for
        # data-table contexts (collapses braces); briefing text just needs
        # the JSON-breaking inch-mark + backslash neutralised.
        def _safe(v: Any) -> str:
            return str(v).replace('"', " inch").replace("\\", "/") if v is not None else ""

        lines: list[str] = []
        total_qty = 0
        for it in items:
            qty   = int(_get(it, "recommended_qty", "recommendedQty", "RecommendedQuantity", default=0) or 0)
            total_qty += qty
            cycle = _get(it, "purchase_cycle_days", "purchaseCycleDays", "PurchaseCycleDays")
            days_since = _get(it, "days_since_last_purchase", "daysSinceLastPurchase", "DaysSinceLastPurchase")
            freq  = _get(it, "frequency_percent", "frequencyPercent", "FrequencyPercent")
            trend = _get(it, "trend_factor", "trendFactor", "TrendFactor")
            why_item = _get(it, "why_item", "whyItem", "WhyItem")
            why_qty  = _get(it, "why_quantity", "whyQuantity", "WhyQuantity")
            source = _get(it, "source", "Source")
            tier   = _get(it, "tier", "Tier")
            parts = [
                f"  Item: {_safe(_get(it, 'item_code', 'itemCode', 'ItemCode', default='?'))} - {_safe(_get(it, 'item_name', 'itemName', 'ItemName'))}",
                f"  Recommended qty: {qty} | Tier: {_safe(tier)} | Source: {_safe(source)}",
            ]
            facts: list[str] = []
            if cycle:
                try: facts.append(f"buys every {int(round(float(cycle)))} days")
                except (TypeError, ValueError): pass
            if days_since:
                try: facts.append(f"last bought {int(days_since)} days ago")
                except (TypeError, ValueError): pass
            if freq:
                try: facts.append(f"buys this on {int(round(float(freq)))}% of visits")
                except (TypeError, ValueError): pass
            if trend and str(trend) != "1.0":
                facts.append(f"trend factor {trend}")
            if facts:
                parts.append(f"  Facts: {', '.join(facts)}")
            if why_item: parts.append(f"  Why recommended: {_safe(why_item)}")
            if why_qty:  parts.append(f"  Why this quantity: {_safe(why_qty)}")
            lines.append("\n".join(parts))
        items_table = "\n\n".join(lines) if lines else "No items"

        context_parts = []
        for it in items:
            why = _get(it, "why_item", "whyItem", "WhyItem")
            if why:
                context_parts.append(
                    f"- {_safe(_get(it, 'item_code', 'itemCode', 'ItemCode'))}: {_safe(why)}"
                )
        customer_context = "\n".join(context_parts) if context_parts else "No additional context"

        system = self._system_with_schema("pre_visit_briefing")
        user = self._prompts.render(
            "pre_visit_briefing", "pre_visit_template",
            customer_code=customer_code,
            customer_name=_safe(customer_name) or customer_code,
            route_code=route_code, date=date,
            item_count=len(items), total_qty=total_qty,
            items_table=items_table, customer_context=customer_context,
        )
        return system, user

    # ------------------------------------------------------------------
    # Health
    # ------------------------------------------------------------------

    def health(self) -> dict[str, Any]:
        return {
            **self._client.health(),
            "cache": self._cache.stats(),
            "prompts": self._prompts.list_templates(),
        }

    # ------------------------------------------------------------------
    # Cache management
    # ------------------------------------------------------------------

    def clear_cache(self) -> int:
        return self._cache.clear()

    def cache_stats(self) -> dict[str, Any]:
        return self._cache.stats()

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _call_with_retry(
        self, system: str, user: str, model_cls: type, label: str,
        *, route_code: str = "", customer_code: str = "", max_tokens: int | None = None,
    ) -> dict[str, Any]:
        """Call LLM with retries on transient errors. Validation failures
        on the final attempt return the raw dict (so a structurally
        almost-right response still degrades gracefully). Truncations are
        treated as fatal because retrying without a bigger token budget
        just repeats the failure.

        ``label`` doubles as the cost-tracker ``artifact`` label so spend
        slices naturally per analyze_* method.
        """
        last_exc: str | None = None
        for attempt in range(self._s.max_retries):
            raw: dict[str, Any] | None = None
            try:
                raw = self._client.chat(
                    system, user, attempt=attempt,
                    artifact=label,
                    route_code=route_code,
                    customer_code=customer_code,
                    max_tokens=max_tokens,
                )
                validated = model_cls(**raw)
                return validated.model_dump()

            except TruncatedResponseError as exc:
                # Bigger ``max_tokens`` is the real fix; a retry on the
                # same prompt won't help. Skip remaining attempts.
                logger.error("%s truncated (max_tokens=%d): %s",
                             label, self._s.max_tokens, exc)
                return self._fallback(label, reason=f"output truncated at {self._s.max_tokens} tokens")

            except ValidationError as exc:
                logger.warning("%s validation failed (attempt %d): %s", label, attempt + 1, exc)
                last_exc = str(exc)
                if attempt == self._s.max_retries - 1 and isinstance(raw, dict):
                    # Pydantic rejected one field but the shape is mostly
                    # right -- pass the raw dict through so the UI can at
                    # least render the populated keys instead of an
                    # error placeholder. Mark degraded so the saver
                    # treats it as "best-effort, retry next tick".
                    raw[DEGRADED_KEY] = True
                    raw["_degraded_reason"] = f"validation: {exc}"
                    return raw
            except Exception as exc:
                logger.error("%s failed (attempt %d): %s", label, attempt + 1, exc)
                last_exc = str(exc)
                if attempt == self._s.max_retries - 1:
                    return self._fallback(label, reason=str(exc))

        return self._fallback(label, reason=last_exc or "All retries exhausted")

    @staticmethod
    def _fallback(label: str, **context: Any) -> dict[str, Any]:
        reason = context.get("reason", "Analysis unavailable")
        base: dict[str, Any] = {
            "performance_summary": f"Analysis not available: {reason}",
            "route_summary": f"Analysis not available: {reason}",
            "briefing": f"Analysis not available: {reason}",
            "supervisor_instructions": [],
            "supervisor_priorities": [],
            "strengths": [],
            "weaknesses": [],
            "high_performers_with_practices": [],
            "critical_issues": [],
            "key_items": [],
            "heads_up": "",
            DEGRADED_KEY: True,
            "_degraded_reason": reason,
        }
        for k, v in context.items():
            if k != "reason":
                base[k] = v
        return base

    @staticmethod
    def _empty_customer_response(customer_code: str) -> dict[str, Any]:
        """Deterministic honest response when there is no visit data to
        analyse. Saves an LLM call and prevents hallucinated narratives
        like "clean slate / build relationship from the ground up".

        Marked degraded so the API layer downgrades ``success`` to False
        and supervision skips the DB save -- a later tick with real
        actuals overwrites NULL with the real analysis. Without the
        marker, the placeholder text would land in the column and the
        ``WHERE column IS NULL`` saver guard would freeze it forever.
        """
        return {
            "customer_code": customer_code,
            "performance_summary": "No sales activity recorded for this visit yet.",
            "supervisor_instructions": [],
            "strengths": [],
            "weaknesses": [],
            DEGRADED_KEY: True,
            "_degraded_reason": "empty input -- pending real visit data",
        }

    @staticmethod
    def _empty_route_response(route_code: str) -> dict[str, Any]:
        return {
            "route_code": route_code,
            "route_summary": "No visited customers yet on this route.",
            "supervisor_priorities": [],
            "high_performers_with_practices": [],
            "critical_issues": [],
            DEGRADED_KEY: True,
            "_degraded_reason": "empty input -- pending real visit data",
        }

    @staticmethod
    def _empty_briefing_response(customer_code: str) -> dict[str, Any]:
        return {
            "customer_code": customer_code,
            "briefing": "No recommendations for this customer today.",
            "key_items": [],
            "heads_up": "",
            DEGRADED_KEY: True,
            "_degraded_reason": "empty input -- no items recommended",
        }
