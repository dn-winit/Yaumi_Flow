"""HTTP client to recommended_order; short-TTL cache feeds the auto-visit reconciler.

Fail-soft: upstream failures return []; reconciler skips the route for that tick.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any

import httpx

from sales_supervision.config.settings import Settings, get_settings

logger = logging.getLogger(__name__)


class RecommendedOrderClient:
    """Pulls today's per-customer recommendations for one (route, date)."""

    def __init__(self, settings: Settings | None = None) -> None:
        self._s = settings or get_settings()
        self._lock = threading.Lock()
        # (route, date) -> (fetched_at_epoch, recs_list)
        self._cache: dict[tuple[str, str], tuple[float, list[dict[str, Any]]]] = {}

    @property
    def base_url(self) -> str:
        return f"{self._s.recommended_order_url.rstrip('/')}/api/v1/recommended-order"

    def get_recommendations(self, route_code: str, date: str) -> list[dict[str, Any]]:
        """Return per-(customer, item) recommendation rows; cached for recommendation_cache_seconds."""
        key = (str(route_code), str(date))
        ttl = float(self._s.recommendation_cache_seconds)
        now = time.time()
        with self._lock:
            cached = self._cache.get(key)
            if cached and (now - cached[0]) < ttl:
                return cached[1]
        recs = self._fetch(route_code, date)
        with self._lock:
            # Only cache positive results; empty lists usually mean transient upstream issues.
            if recs:
                self._cache[key] = (now, recs)
        return recs

    def _fetch(self, route_code: str, date: str) -> list[dict[str, Any]]:
        url = f"{self.base_url}/get"
        body = {
            "date": date,
            "route_code": route_code,
            "limit": int(self._s.recommendation_fetch_limit),
        }
        try:
            with httpx.Client(timeout=self._s.recommended_order_timeout) as client:
                r = client.post(url, json=body)
                r.raise_for_status()
                payload = r.json()
        except Exception as exc:
            logger.warning(
                "recommended_order fetch failed for %s/%s: %s",
                route_code, date, exc,
            )
            return []
        if not payload.get("success"):
            return []
        return payload.get("data") or []
