"""HTTP client to the llm_analytics service.

Drives the auto-visit reconciler's "fire LLM analysis when a customer
visit lands and when a route completes" path. Calls are best-effort:
any failure logs and returns ``None`` so the reconciler keeps going.

The supervision schema persists analyses as JSON-strings under
``customer_analysis`` / ``pre_visit_briefing`` (yf_supervision_customers)
and ``llm_route_analysis`` (yf_supervision_routes). This client just
fetches the JSON; the caller writes it via the existing ``DbSaver``
methods so we have one persist path for both manual and auto flows.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

import httpx

from sales_supervision.config.settings import Settings, get_settings

logger = logging.getLogger(__name__)


class LlmClient:
    """Thin HTTP wrapper around llm_analytics' analyse endpoints."""

    def __init__(self, settings: Optional[Settings] = None) -> None:
        self._s = settings or get_settings()

    @property
    def base_url(self) -> str:
        return f"{self._s.llm_analytics_url.rstrip('/')}/api/v1/analytics"

    @property
    def configured(self) -> bool:
        return bool(self._s.llm_analytics_url)

    # ------------------------------------------------------------------
    # Public surface
    # ------------------------------------------------------------------

    def analyze_customer(
        self,
        *,
        customer_code: str,
        route_code: str,
        date: str,
        current_items: List[Dict[str, Any]],
        performance_score: float,
        coverage: float,
        accuracy: float,
    ) -> Optional[str]:
        """Customer-level visit retrospective. Returns the JSON-stringified
        payload (ready for ``customer_analysis`` column) or None on any
        failure.

        ``customer_data`` is sent as an empty list -- the upstream API
        accepts a per-row history frame the auto path doesn't have.
        Browser-driven analysis fills it; the auto-reconciler path
        relies on the LLM's own RAG to look up history if needed.
        """
        body = {
            "customer_code": customer_code,
            "route_code": route_code,
            "date": date,
            "customer_data": [],
            "current_items": current_items,
            "performance_score": float(performance_score),
            "coverage": float(coverage),
            "accuracy": float(accuracy),
        }
        return self._post("/analyze/customer", body)

    def analyze_route(
        self,
        *,
        route_code: str,
        date: str,
        visited_customers: List[Dict[str, Any]],
        total_customers: int,
        total_actual: float,
        total_recommended: float,
        actual_customer_codes: List[str],
    ) -> Optional[str]:
        """Route-level retrospective. Same return contract as
        :meth:`analyze_customer` -- JSON string or None."""
        body = {
            "route_code": route_code,
            "date": date,
            "visited_customers": visited_customers,
            "total_customers": int(total_customers),
            "total_actual": float(total_actual),
            "total_recommended": float(total_recommended),
            "pre_context": None,
            "actual_customer_codes": actual_customer_codes,
        }
        return self._post("/analyze/route", body)

    def pre_visit_briefing(
        self,
        *,
        customer_code: str,
        customer_name: str,
        route_code: str,
        date: str,
        items: List[Dict[str, Any]],
    ) -> Optional[str]:
        """Quick briefing the supervisor sees on the customer card before
        the visit. Cached on the llm_analytics side so repeat calls for
        the same (customer, route, date) are free."""
        body = {
            "customer_code": customer_code,
            "customer_name": customer_name,
            "route_code": route_code,
            "date": date,
            "items": items,
        }
        return self._post("/analyze/pre-visit", body)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _post(self, path: str, body: Dict[str, Any]) -> Optional[str]:
        if not self.configured:
            return None
        url = f"{self.base_url}{path}"
        try:
            with httpx.Client(timeout=self._s.llm_analytics_timeout) as client:
                r = client.post(url, json=body)
                r.raise_for_status()
                payload = r.json()
        except Exception as exc:
            logger.warning("llm_analytics %s failed: %s", path, exc)
            return None
        if not payload.get("success"):
            return None
        # Persist what the analyser returned, JSON-stringified, so the
        # DB schema can store it as NVARCHAR(MAX) with no decoding.
        try:
            return json.dumps(payload.get("data") or {}, default=str)
        except Exception as exc:
            logger.warning("llm_analytics %s payload not JSON-serialisable: %s", path, exc)
            return None
