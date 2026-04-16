"""
Upcoming-week planning view -- who will be visited, how much to carry, how
much revenue is on the table. Pure local read: journey plan + demand forecast
+ price lookup, all already sitting in :class:`DataManager`'s cached frames.

Does NOT regenerate recommendations. The question this service answers is
"what does my week look like based on the plan and the forecast?" -- not
"what exact recommendations do I have?". If the supervisor wants per-customer
recommendations for a specific future date, the normal ``/get`` endpoint
handles that lazily.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

import pandas as pd

from recommended_order.config.constants import ANALYTICS_CACHE_TTL_SECONDS
from recommended_order.config.settings import Settings
from recommended_order.data.manager import DataManager

logger = logging.getLogger(__name__)

_DEFAULT_HORIZON_DAYS = 7
_MAX_HORIZON_DAYS = 30


class PlanningService:
    """Aggregated daily plan for the next ``days`` days."""

    def __init__(self, dm: DataManager, settings: Settings, cache_ttl_seconds: int = ANALYTICS_CACHE_TTL_SECONDS) -> None:
        self._dm = dm
        self._s = settings
        self._ttl = cache_ttl_seconds
        self._lock = threading.Lock()
        self._cache: Dict[tuple, tuple[float, Dict[str, Any]]] = {}

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    def get_upcoming(self, days: int = _DEFAULT_HORIZON_DAYS, route_code: Optional[str] = None) -> Dict[str, Any]:
        days = max(1, min(days, _MAX_HORIZON_DAYS))
        today = self._today()
        key = (today, days, route_code or "")
        now = time.time()
        with self._lock:
            cached = self._cache.get(key)
            if cached and (now - cached[0]) < self._ttl:
                return cached[1]

        result = self._compute(today, days, route_code)
        with self._lock:
            self._cache[key] = (now, result)
        return result

    def invalidate(self) -> None:
        with self._lock:
            self._cache.clear()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _today(self) -> str:
        """Local Dubai date so we're consistent with the scheduler and webapp."""
        tz = getattr(self._s, "scheduler", None)
        tz_name = tz.timezone if tz is not None else "Asia/Dubai"
        try:
            return datetime.now(ZoneInfo(tz_name)).strftime("%Y-%m-%d")
        except Exception:
            return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def _compute(self, today: str, days: int, route_code: Optional[str]) -> Dict[str, Any]:
        horizon = [
            (datetime.strptime(today, "%Y-%m-%d").date() + timedelta(days=i)).isoformat()
            for i in range(days)
        ]

        journey = self._dm.get_journey_plan(route_code=route_code)
        demand = self._dm.get_demand_data(route_code=route_code)
        prices = self._price_lookup()

        daily: List[Dict[str, Any]] = []
        for day in horizon:
            target = pd.to_datetime(day).normalize()
            jp = _slice_by_date(journey, ("JourneyDate", "TrxDate"), target)
            fc = _slice_by_date(demand, ("TrxDate",), target)

            customers = (
                int(jp["CustomerCode"].dropna().astype(str).nunique())
                if "CustomerCode" in jp.columns and not jp.empty
                else 0
            )
            routes = (
                int(jp["RouteCode"].dropna().astype(str).nunique())
                if "RouteCode" in jp.columns and not jp.empty
                else 0
            )

            qty = float(fc["Predicted"].sum()) if "Predicted" in fc.columns and not fc.empty else 0.0
            revenue = 0.0
            if not fc.empty and "Predicted" in fc.columns and "ItemCode" in fc.columns:
                for code, group in fc.groupby("ItemCode"):
                    price = prices.get(str(code))
                    if price is None:
                        continue
                    revenue += float(group["Predicted"].sum()) * price

            daily.append({
                "date": day,
                "customers": customers,
                "routes": routes,
                "predicted_qty": round(qty, 1),
                "est_revenue": round(revenue, 2) if revenue > 0 else None,
            })

        summary = self._summary(daily)
        return {
            "available": True,
            "today": today,
            "days": days,
            "route_code": route_code,
            "summary": summary,
            "daily": daily,
        }

    def _price_lookup(self) -> Dict[str, float]:
        """Average unit price per item code from the customer-level sales cache."""
        df = self._dm.get_customer_data()
        if df.empty or "ItemCode" not in df.columns or "AvgUnitPrice" not in df.columns:
            return {}
        prices = (
            df[["ItemCode", "AvgUnitPrice"]]
            .dropna()
            .groupby("ItemCode", as_index=True)["AvgUnitPrice"]
            .mean()
        )
        return {str(k): float(v) for k, v in prices.items() if pd.notna(v) and float(v) > 0}

    @staticmethod
    def _summary(daily: List[Dict[str, Any]]) -> Dict[str, Any]:
        if not daily:
            return {
                "total_visits": 0, "total_qty": 0.0, "total_revenue": 0.0,
                "peak_day": None, "active_days": 0,
            }
        total_visits = sum(d["customers"] for d in daily)
        total_qty = sum(d["predicted_qty"] for d in daily)
        total_revenue = sum((d["est_revenue"] or 0.0) for d in daily)
        active = [d for d in daily if d["customers"] > 0 or d["predicted_qty"] > 0]
        peak = max(daily, key=lambda d: d["predicted_qty"]) if daily else None
        return {
            "total_visits": total_visits,
            "total_qty": round(total_qty, 1),
            "total_revenue": round(total_revenue, 2) if total_revenue > 0 else None,
            "peak_day": {"date": peak["date"], "predicted_qty": peak["predicted_qty"]} if peak else None,
            "active_days": len(active),
        }


def _slice_by_date(df: pd.DataFrame, date_cols: tuple[str, ...], target: pd.Timestamp) -> pd.DataFrame:
    for col in date_cols:
        if col in df.columns:
            return df[df[col].dt.normalize() == target]
    return df.iloc[0:0]
