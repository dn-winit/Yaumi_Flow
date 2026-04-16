"""
Main analyzer -- orchestrates client, prompts, formatting, caching, rate limiting.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Set

import pandas as pd
from pydantic import ValidationError

from llm_analytics.config.settings import Settings, get_settings
from llm_analytics.core.client import LLMClient
from llm_analytics.core.formatter import DataFormatter
from llm_analytics.core.prompt_loader import PromptLoader
from llm_analytics.core.validator import sanitize_customer_codes
from llm_analytics.models.schemas import CustomerAnalysis, RouteAnalysis, PlanningInsights
from llm_analytics.services.cache import LLMCache
from llm_analytics.services.rate_limiter import RateLimiter

logger = logging.getLogger(__name__)


class Analyzer:
    """Production-grade analysis engine with cache, rate limiting, retries."""

    def __init__(self, settings: Optional[Settings] = None) -> None:
        self._s = settings or get_settings()
        self._client = LLMClient(self._s)
        self._prompts = PromptLoader(self._s)
        self._formatter = DataFormatter(self._s)
        self._cache = LLMCache(
            cache_dir=self._s.cache_dir,
            ttl_hours=self._s.cache_ttl_hours,
            enabled=self._s.cache_enabled,
        )
        self._limiter = RateLimiter(
            max_requests=self._s.rate_limit_max_requests,
            window_seconds=self._s.rate_limit_window_seconds,
        )

    # ------------------------------------------------------------------
    # Customer analysis
    # ------------------------------------------------------------------

    def analyze_customer(
        self,
        customer_code: str,
        route_code: str,
        date: str,
        customer_data: pd.DataFrame,
        current_items: List[Dict[str, Any]],
        performance_score: float = 0.0,
        coverage: float = 0.0,
        accuracy: float = 0.0,
    ) -> Dict[str, Any]:
        cache_kwargs = dict(customer_code=customer_code, route_code=route_code, date=date)
        cached = self._cache.get("customer_analysis", **cache_kwargs)
        if cached:
            return cached

        if not self._limiter.acquire():
            return self._fallback("customer", customer_code=customer_code,
                                  reason="Rate limit exceeded")

        historical = self._formatter.format_historical_context(customer_data)
        visit_table = self._formatter.format_current_visit(current_items)

        system = self._prompts.get_system_prompt("customer_analysis")
        user = self._prompts.render(
            "customer_analysis", "customer_analysis_template",
            customer_code=customer_code, route_code=route_code, date=date,
            performance_score=performance_score, coverage=coverage, accuracy=accuracy,
            historical_context=historical, current_visit_table=visit_table,
        )

        result = self._call_with_retry(system, user, CustomerAnalysis, "customer_analysis")
        result["customer_code"] = customer_code
        self._cache.set("customer_analysis", result, **cache_kwargs)
        return result

    # ------------------------------------------------------------------
    # Route analysis
    # ------------------------------------------------------------------

    def analyze_route(
        self,
        route_code: str,
        date: str,
        visited_customers: List[Dict[str, Any]],
        total_customers: int,
        total_actual: int = 0,
        total_recommended: int = 0,
        pre_context: str = "",
        actual_customer_codes: Optional[Set[str]] = None,
    ) -> Dict[str, Any]:
        cache_kwargs = dict(route_code=route_code, date=date)
        cached = self._cache.get("route_analysis", **cache_kwargs)
        if cached:
            return cached

        if not self._limiter.acquire():
            return self._fallback("route", route_code=route_code, reason="Rate limit exceeded")

        visited = len(visited_customers)
        coverage_pct = (visited / max(total_customers, 1)) * 100
        qty_achievement = (total_actual / max(total_recommended, 1)) * 100
        actual_data = self._formatter.format_route_performance(visited_customers)

        system = self._prompts.get_system_prompt("route_analysis")
        user = self._prompts.render(
            "route_analysis", "route_analysis_template",
            route_code=route_code, date=date,
            visited_customers=visited, total_customers=total_customers,
            coverage_percentage=coverage_pct,
            total_actual=total_actual, total_recommended=total_recommended,
            quantity_achievement=qty_achievement,
            pre_context=pre_context, actual_data=actual_data,
        )

        result = self._call_with_retry(system, user, RouteAnalysis, "route_analysis")
        result["route_code"] = route_code

        if actual_customer_codes:
            result = sanitize_customer_codes(result, actual_customer_codes)

        self._cache.set("route_analysis", result, **cache_kwargs)
        return result

    # ------------------------------------------------------------------
    # Planning insights
    # ------------------------------------------------------------------

    def analyze_planning(
        self,
        route_code: str,
        date: str,
        van_load_items: List[Dict[str, Any]],
        customer_recommendations: List[Dict[str, Any]],
        van_load_skus: int = 0,
        van_load_qty: int = 0,
        total_customers: int = 0,
        total_rec_qty: int = 0,
    ) -> Dict[str, Any]:
        cache_kwargs = dict(route_code=route_code, date=date, analysis_type="planning")
        cached = self._cache.get("planning_analysis", **cache_kwargs)
        if cached:
            return cached

        if not self._limiter.acquire():
            return self._fallback("planning", route_code=route_code, reason="Rate limit exceeded")

        van_table = self._formatter.format_van_load(van_load_items)
        cust_table = self._formatter.format_customer_recommendations(customer_recommendations)

        system = self._prompts.get_system_prompt("planning_analysis")
        user = self._prompts.render(
            "planning_analysis", "planning_analysis_template",
            route_code=route_code, date=date,
            van_load_skus=van_load_skus, van_load_qty=van_load_qty,
            van_load_table=van_table,
            total_customers=total_customers, total_rec_qty=total_rec_qty,
            customer_recommendations_table=cust_table,
        )

        result = self._call_with_retry(system, user, PlanningInsights, "planning_analysis")
        self._cache.set("planning_analysis", result, **cache_kwargs)
        return result

    # ------------------------------------------------------------------
    # Health
    # ------------------------------------------------------------------

    def health(self) -> Dict[str, Any]:
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

    def cache_stats(self) -> Dict[str, Any]:
        return self._cache.stats()

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _call_with_retry(self, system: str, user: str, model_cls: type, label: str) -> Dict[str, Any]:
        """Call LLM with retries on JSON parse/validation failure."""
        for attempt in range(self._s.max_retries):
            try:
                raw = self._client.chat(system, user, attempt=attempt)

                # Validate with Pydantic
                validated = model_cls(**raw)
                return validated.model_dump()

            except ValidationError as exc:
                logger.warning("%s validation failed (attempt %d): %s", label, attempt + 1, exc)
                if attempt == self._s.max_retries - 1:
                    # Return raw if it's at least a dict
                    if isinstance(raw, dict):
                        return raw
            except Exception as exc:
                logger.error("%s failed (attempt %d): %s", label, attempt + 1, exc)
                if attempt == self._s.max_retries - 1:
                    return self._fallback(label, reason=str(exc))

        return self._fallback(label, reason="All retries exhausted")

    @staticmethod
    def _fallback(label: str, **context: Any) -> Dict[str, Any]:
        reason = context.get("reason", "Analysis unavailable")
        base = {
            "performance_summary": f"Analysis not available: {reason}",
            "route_summary": f"Analysis not available: {reason}",
            "supervisor_instructions": [],
            "supervisor_priorities": [],
            "strengths": [],
            "weaknesses": [],
            "high_performers_with_practices": [],
            "critical_issues": [],
            "priority_customers": [],
            "van_load_alerts": [],
            "opportunities": [],
            "quick_tips": [],
        }
        for k, v in context.items():
            if k != "reason":
                base[k] = v
        return base
