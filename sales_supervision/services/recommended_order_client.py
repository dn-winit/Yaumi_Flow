"""HTTP client to the recommended_order service.

The auto-visit reconciler needs the day's plan (per-customer recommended
items) so it can scope a session and score visits against the right
baseline. Recommendations live in ``recommended_order``; we fetch them
on demand with a short-TTL cache so a 5-minute reconcile cycle on 12
routes doesn't fan out into 12 cold HTTP calls every tick.

Fail-soft contract: any upstream failure returns ``[]`` so the
reconciler skips the route for this tick rather than crashing the
whole loop.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

import httpx

from sales_supervision.config.settings import Settings, get_settings

logger = logging.getLogger(__name__)


class RecommendedOrderClient:
    """Pulls today's per-customer recommendations for one (route, date)."""

    def __init__(self, settings: Optional[Settings] = None) -> None:
        self._s = settings or get_settings()
        self._lock = threading.Lock()
        # (route, date) -> (fetched_at_epoch, recs_list)
        self._cache: Dict[Tuple[str, str], Tuple[float, List[Dict[str, Any]]]] = {}

    @property
    def base_url(self) -> str:
        return f"{self._s.recommended_order_url.rstrip('/')}/api/v1/recommended-order"

    def get_recommendations(self, route_code: str, date: str) -> List[Dict[str, Any]]:
        """Return ``[{CustomerCode, ItemCode, RecommendedQuantity, ...}, ...]``.

        Empty list = no recommendations stored for that route+date OR
        an upstream error (logged once per tick). Cached for
        ``recommendation_cache_seconds`` so a route polled every 5
        minutes only triggers one upstream call per cache window.
        """
        key = (str(route_code), str(date))
        ttl = float(self._s.recommendation_cache_seconds)
        now = time.time()
        with self._lock:
            cached = self._cache.get(key)
            if cached and (now - cached[0]) < ttl:
                return cached[1]
        recs = self._fetch(route_code, date)
        with self._lock:
            # Cache only POSITIVE results. Empty lists are typically
            # transient (recommended_order CSV load racing supervision
            # boot, a brand-new route, a network blip). Caching empty
            # for the full ``recommendation_cache_seconds`` made auto-
            # visit invisible to a route for 5 minutes after any
            # transient miss; retrying every tick is what makes the
            # system self-healing without operator intervention.
            if recs:
                self._cache[key] = (now, recs)
        return recs

    def _fetch(self, route_code: str, date: str) -> List[Dict[str, Any]]:
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
