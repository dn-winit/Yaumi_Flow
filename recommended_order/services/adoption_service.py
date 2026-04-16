"""
Adoption analytics -- did the recommendations we produced actually convert?

Joins historical rows from ``yf_recommended_orders`` (read via
:class:`RecommendationStore`) with the customer-level sales CSV that
``data_import`` maintains in ``data/customer_data.csv`` (already cached in the
``DataManager``). No live DB round-trip, no regeneration of past recs.

An adoption event is a ``(trx_date, route_code, customer_code, item_code)``
tuple that exists in both the recommendation set and the customer-sales set.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import pandas as pd

from recommended_order.config.constants import ANALYTICS_CACHE_TTL_SECONDS
from recommended_order.data.manager import DataManager
from recommended_order.services.storage.store import RecommendationStore

logger = logging.getLogger(__name__)

_JOIN_KEYS = ["trx_date", "route_code", "customer_code", "item_code"]


class AdoptionService:
    """Compute adoption KPIs over a date window.

    Results are cached by ``(start_date, end_date, route_code)`` with a short
    TTL so repeated drawer opens don't rerun the cross-frame merge.
    """

    def __init__(
        self,
        store: RecommendationStore,
        dm: DataManager,
        cache_ttl_seconds: int = ANALYTICS_CACHE_TTL_SECONDS,
    ) -> None:
        self._store = store
        self._dm = dm
        self._ttl = cache_ttl_seconds
        self._lock = threading.Lock()
        self._cache: Dict[tuple, tuple[float, Dict[str, Any]]] = {}

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    def get_adoption(
        self,
        start_date: str,
        end_date: str,
        route_code: Optional[str] = None,
    ) -> Dict[str, Any]:
        key = (start_date, end_date, route_code or "")
        now = time.time()
        with self._lock:
            cached = self._cache.get(key)
            if cached and (now - cached[0]) < self._ttl:
                return cached[1]

        result = self._compute(start_date, end_date, route_code)
        with self._lock:
            self._cache[key] = (now, result)
        return result

    def invalidate(self) -> None:
        with self._lock:
            self._cache.clear()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _compute(self, start_date: str, end_date: str, route_code: Optional[str]) -> Dict[str, Any]:
        recs = self._load_recs(start_date, end_date, route_code)
        if recs.empty:
            return self._empty_response(start_date, end_date, reason="No recommendations stored for this window")

        sales = self._load_sales(start_date, end_date, route_code, recs)
        merged = self._merge(recs, sales)

        return {
            "available": True,
            "start_date": start_date,
            "end_date": end_date,
            "route_code": route_code,
            "summary": self._summary(merged),
            "daily": self._daily(merged),
            "top_over_recommended": self._top_items(merged, which="over", limit=10),
            "top_missed": self._top_items(merged, which="missed", limit=10),
            "by_tier": self._by_tier(merged),
        }

    def _load_recs(self, start_date: str, end_date: str, route_code: Optional[str]) -> pd.DataFrame:
        """Read per-day recommendations from the store and concat."""
        days = _daterange(start_date, end_date)
        frames: List[pd.DataFrame] = []
        for day in days:
            df = self._store.get(day, route_code)
            if df.empty:
                continue
            # Drop to the columns we need + normalize types
            keep = [
                "TrxDate", "RouteCode", "CustomerCode", "ItemCode", "ItemName",
                "RecommendedQuantity", "Tier",
            ]
            cols = [c for c in keep if c in df.columns]
            df = df[cols].copy()
            df["TrxDate"] = pd.to_datetime(df["TrxDate"], errors="coerce").dt.strftime("%Y-%m-%d")
            frames.append(df)

        if not frames:
            return pd.DataFrame(columns=_JOIN_KEYS)

        out = pd.concat(frames, ignore_index=True)
        out.rename(columns={
            "TrxDate": "trx_date",
            "RouteCode": "route_code",
            "CustomerCode": "customer_code",
            "ItemCode": "item_code",
            "ItemName": "item_name",
            "RecommendedQuantity": "recommended_qty",
            "Tier": "tier",
        }, inplace=True)
        for col in ("route_code", "customer_code", "item_code"):
            out[col] = out[col].astype(str).str.strip()
        return out

    def _load_sales(
        self,
        start_date: str,
        end_date: str,
        route_code: Optional[str],
        recs: pd.DataFrame,
    ) -> pd.DataFrame:
        """Customer-level sales from ``data/customer_data.csv`` via DataManager."""
        df = self._dm.get_customer_data(route_code)
        if df.empty:
            return pd.DataFrame(columns=_JOIN_KEYS + ["actual_qty"])

        start = pd.to_datetime(start_date)
        end = pd.to_datetime(end_date)
        mask = (df["TrxDate"] >= start) & (df["TrxDate"] <= end)
        sub = df.loc[mask, ["TrxDate", "RouteCode", "CustomerCode", "ItemCode", "TotalQuantity"]].copy()
        sub["TrxDate"] = sub["TrxDate"].dt.strftime("%Y-%m-%d")
        sub.rename(columns={
            "TrxDate": "trx_date",
            "RouteCode": "route_code",
            "CustomerCode": "customer_code",
            "ItemCode": "item_code",
            "TotalQuantity": "actual_qty",
        }, inplace=True)
        for col in ("route_code", "customer_code", "item_code"):
            sub[col] = sub[col].astype(str).str.strip()
        # Scope sales further to the (customer, date) pairs that were actually visited
        # -- keeps "missed SKUs" focused on the same trip rather than the whole route-day.
        visit_keys = recs[["trx_date", "route_code", "customer_code"]].drop_duplicates()
        if not visit_keys.empty:
            sub = sub.merge(visit_keys, on=["trx_date", "route_code", "customer_code"], how="inner")
        return sub

    @staticmethod
    def _merge(recs: pd.DataFrame, sales: pd.DataFrame) -> pd.DataFrame:
        """Outer merge so we can count three categories:
            * adopted: in both (sold_actually > 0 and was recommended)
            * over_recommended: in recs only
            * missed: in sales only (customer bought something we didn't push)
        """
        merged = recs.merge(sales, on=_JOIN_KEYS, how="outer", indicator=True)
        merged["recommended_qty"] = pd.to_numeric(merged.get("recommended_qty"), errors="coerce").fillna(0)
        merged["actual_qty"] = pd.to_numeric(merged.get("actual_qty"), errors="coerce").fillna(0)
        merged["adopted"] = (merged["_merge"] == "both") & (merged["actual_qty"] > 0)
        merged["over_recommended"] = (merged["_merge"] == "left_only") | (
            (merged["_merge"] == "both") & (merged["actual_qty"] <= 0)
        )
        merged["missed"] = merged["_merge"] == "right_only"
        return merged

    @staticmethod
    def _summary(merged: pd.DataFrame) -> Dict[str, Any]:
        recommended = merged[merged["_merge"].isin(["left_only", "both"])]
        rows_recommended = int(len(recommended))
        rows_adopted = int(merged["adopted"].sum())
        rows_over = int(merged["over_recommended"].sum())
        rows_missed = int(merged["missed"].sum())

        adoption_pct = round(rows_adopted / rows_recommended * 100, 1) if rows_recommended else None

        # "Uplift": qty sold when we recommended, vs qty we DIDN'T recommend but was still sold.
        # Using mean-per-row keeps it meaningful regardless of period length.
        sold_when_rec = merged.loc[merged["adopted"], "actual_qty"]
        sold_when_not_rec = merged.loc[merged["missed"], "actual_qty"]
        rec_mean = float(sold_when_rec.mean()) if not sold_when_rec.empty else 0.0
        not_rec_mean = float(sold_when_not_rec.mean()) if not sold_when_not_rec.empty else 0.0
        uplift_pct = round((rec_mean - not_rec_mean) / not_rec_mean * 100, 1) if not_rec_mean > 0 else None

        total_recommended_qty = float(recommended["recommended_qty"].sum())
        total_sold_qty = float(merged["actual_qty"].sum())
        return {
            "rows_recommended": rows_recommended,
            "rows_adopted": rows_adopted,
            "rows_over_recommended": rows_over,
            "rows_missed": rows_missed,
            "adoption_pct": adoption_pct,
            "uplift_pct": uplift_pct,
            "total_recommended_qty": round(total_recommended_qty, 1),
            "total_sold_qty": round(total_sold_qty, 1),
        }

    @staticmethod
    def _daily(merged: pd.DataFrame) -> List[Dict[str, Any]]:
        """Daily adoption rate, ordered by date."""
        if merged.empty:
            return []
        grouped = merged.groupby("trx_date")
        rows: List[Dict[str, Any]] = []
        for day, g in grouped:
            if pd.isna(day):
                continue
            recommended = int(((g["_merge"] == "left_only") | (g["_merge"] == "both")).sum())
            adopted = int(g["adopted"].sum())
            rate = round(adopted / recommended * 100, 1) if recommended else 0.0
            rows.append({
                "date": str(day),
                "recommended": recommended,
                "adopted": adopted,
                "adoption_pct": rate,
            })
        rows.sort(key=lambda r: r["date"])
        return rows

    @staticmethod
    def _top_items(merged: pd.DataFrame, *, which: str, limit: int) -> List[Dict[str, Any]]:
        """Top N items by either over-recommended or missed rows."""
        if merged.empty:
            return []
        if which == "over":
            sub = merged[merged["over_recommended"]]
        else:
            sub = merged[merged["missed"]]
        if sub.empty:
            return []
        grouped = (
            sub.groupby("item_code")
            .agg(
                rows=("item_code", "count"),
                qty=("actual_qty" if which == "missed" else "recommended_qty", "sum"),
            )
            .reset_index()
            .sort_values("rows", ascending=False)
            .head(limit)
        )
        return [
            {"item_code": str(r.item_code), "rows": int(r.rows), "qty": round(float(r.qty), 1)}
            for r in grouped.itertuples(index=False)
        ]

    @staticmethod
    def _by_tier(merged: pd.DataFrame) -> List[Dict[str, Any]]:
        """Adoption rate split by tier -- did MUST_STOCK really sell?"""
        recommended = merged[merged["_merge"].isin(["left_only", "both"])]
        if recommended.empty or "tier" not in recommended.columns:
            return []
        rows: List[Dict[str, Any]] = []
        for tier, g in recommended.groupby("tier"):
            if not tier:
                continue
            rec = int(len(g))
            adopted = int(g["adopted"].sum())
            rate = round(adopted / rec * 100, 1) if rec else 0.0
            rows.append({"tier": str(tier), "recommended": rec, "adopted": adopted, "adoption_pct": rate})
        rows.sort(key=lambda r: r["recommended"], reverse=True)
        return rows

    def _empty_response(self, start_date: str, end_date: str, *, reason: str) -> Dict[str, Any]:
        return {
            "available": False,
            "start_date": start_date,
            "end_date": end_date,
            "message": reason,
            "summary": None,
            "daily": [],
            "top_over_recommended": [],
            "top_missed": [],
            "by_tier": [],
        }


def _daterange(start_date: str, end_date: str) -> List[str]:
    start = datetime.strptime(start_date, "%Y-%m-%d").date()
    end = datetime.strptime(end_date, "%Y-%m-%d").date()
    days: List[str] = []
    d = start
    while d <= end:
        days.append(d.isoformat())
        d += timedelta(days=1)
    return days
