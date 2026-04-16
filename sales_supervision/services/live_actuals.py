"""
Live actuals client -- fetches per-customer sales from YaumiLive via the
``data_import`` service. Keeps all DB access in a single place while letting
the supervisor see real-time sales the moment a customer is marked visited.
"""

from __future__ import annotations

import logging
from typing import Dict

import httpx

from sales_supervision.config.settings import Settings, get_settings

logger = logging.getLogger(__name__)


class LiveActualsClient:
    """Thin HTTP client to data_import's live-sales endpoints."""

    def __init__(self, settings: Settings | None = None) -> None:
        self._s = settings or get_settings()

    def _base_url(self) -> str:
        return f"{self._s.data_import_url.rstrip('/')}/api/v1/data/eda"

    def get_route_sales(self, route_code: str, date: str) -> list[dict]:
        """All customers who invoiced on this route/date. Empty list on failure."""
        try:
            with httpx.Client(timeout=self._s.data_import_timeout) as client:
                r = client.get(
                    f"{self._base_url()}/live-route-sales",
                    params={"route_code": route_code, "date": date},
                )
                r.raise_for_status()
                payload = r.json()
        except Exception as exc:
            logger.warning("Live route-sales fetch failed for %s/%s: %s", route_code, date, exc)
            return []
        if not payload.get("available"):
            return []
        return payload.get("customers", []) or []

    def get_actuals(self, route_code: str, date: str, customer_code: str) -> Dict[str, int]:
        """Return ``{item_code: qty}`` for today's (or any) visit to a customer.

        Returns an empty dict on any upstream failure -- the caller treats that
        as "no recorded sales yet", score = 0 for that visit, still safe.
        """
        params = {"route_code": route_code, "date": date, "customer_code": customer_code}
        try:
            with httpx.Client(timeout=self._s.data_import_timeout) as client:
                r = client.get(f"{self._base_url()}/live-customer-sales", params=params)
                r.raise_for_status()
                payload = r.json()
        except Exception as exc:
            logger.warning("Live actuals fetch failed for %s/%s/%s: %s",
                           route_code, date, customer_code, exc)
            return {}

        if not payload.get("available"):
            return {}
        out: Dict[str, int] = {}
        for row in payload.get("items", []):
            code = str(row.get("item_code") or "").strip()
            if not code:
                continue
            out[code] = int(row.get("qty") or 0)
        return out
