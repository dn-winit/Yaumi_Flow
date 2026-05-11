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

import numpy as np
import pandas as pd

from recommended_order.config.constants import ANALYTICS_CACHE_TTL_SECONDS
from recommended_order.data.manager import DataManager
from recommended_order.services.storage.store import RecommendationStore

logger = logging.getLogger(__name__)

_JOIN_KEYS = ["trx_date", "route_code", "customer_code", "item_code"]

# "Perfect pick" tolerance: total recommended quantity for an adopted SKU is
# within this fraction of total actual quantity. Mirrors the ±20% convention
# used in the VanLoad accuracy drawer (webapp/src/lib/format.ts TOLERANCE_PCT)
# so both dashboards reason about "on target" the same way.
_PERFECT_PICK_TOLERANCE = 0.20


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
        category_codes: Optional[List[str]] = None,
        item_codes: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        # Sorted-tuple cache key so different orderings of the same filter
        # selection share a slot.
        cats = tuple(sorted(set(map(str, category_codes or []))))
        items = tuple(sorted(set(map(str, item_codes or []))))
        key = (start_date, end_date, route_code or "", cats, items)
        now = time.time()
        with self._lock:
            cached = self._cache.get(key)
            if cached and (now - cached[0]) < self._ttl:
                return cached[1]

        result = self._compute(start_date, end_date, route_code, cats, items)
        with self._lock:
            self._cache[key] = (now, result)
        return result

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _compute(
        self,
        start_date: str,
        end_date: str,
        route_code: Optional[str],
        category_codes: tuple = (),
        item_codes: tuple = (),
    ) -> Dict[str, Any]:
        # Translate (category_codes ∪ item_codes) into a flat set of items
        # that recs and sales should be filtered to. Both empty -> None
        # (no filter). Done once per request -- the result is reused by
        # both loaders so they always see the same scope.
        item_filter = self._resolve_item_set(category_codes, item_codes, route_code)
        if item_filter is not None and not item_filter:
            return self._empty_response(
                start_date, end_date,
                reason="No items match the current category / item filter",
            )

        recs = self._load_recs(start_date, end_date, route_code, item_filter)
        if recs.empty:
            return self._empty_response(start_date, end_date, reason="No recommendations stored for this window")

        sales = self._load_sales(start_date, end_date, route_code, recs, item_filter)
        merged = self._merge(recs, sales)

        # Attach avg unit price per SKU so revenue metrics can be computed
        # over the same merged frame. Missing prices map to 0 -- callers check
        # whether any price > 0 before emitting revenue fields.
        prices = self._dm.get_item_prices()
        merged["unit_price"] = (
            merged["item_code"].map(prices).fillna(0.0)
            if prices
            else pd.Series(0.0, index=merged.index)
        )

        # Single source of truth for "what counts as a working day" in
        # this window: dates with actual sales activity for the chosen
        # scope. Same input as the dashboard's working-day axis, so the
        # padded chart and the dashboard line up by construction.
        active_dates = self._active_dates(merged, sales)

        # ``daily`` is padded to every active date so the X-axis never
        # skips a day. Days without recommendations land with
        # ``adoption_pct=None`` so the line chart breaks cleanly instead
        # of dragging to the floor.
        daily = self._daily_padded(merged, active_dates)
        summary = self._summary(merged)
        # Attach the derived metrics that used to be computed on the
        # client (pick / perfect-pick percentages, best-performing day,
        # working-day count) so the drawer never aggregates the daily
        # series itself.
        summary.update(self._derived_metrics(summary, daily, active_dates))

        return {
            "available": True,
            "start_date": start_date,
            "end_date": end_date,
            "route_code": route_code,
            "summary": summary,
            "daily": daily,
            "top_over_recommended": self._top_items(merged, which="over", limit=10),
            "top_missed": self._top_items(merged, which="missed", limit=10),
            "by_tier": self._by_tier(merged),
        }

    def _resolve_item_set(  # noqa: D401 -- signature widened, see body
        self,
        category_codes: tuple,
        item_codes: tuple,
        route_code: Optional[str] = None,
    ) -> Optional[set]:
        """Translate (categories, items) filter into a flat ItemCode set.

        Returns None when there is no filter (caller should not narrow the
        recs / sales frames). Returns an empty set when the filter is
        non-empty but resolves to zero items (caller should short-circuit
        and return an empty response). Otherwise returns the explicit set.
        """
        if not category_codes and not item_codes:
            return None

        explicit_items: Optional[set] = set(map(str, item_codes)) if item_codes else None

        if category_codes:
            cats = set(map(str, category_codes))
            # Prefilter to the active route when the request is route-
            # scoped -- avoids loading the corpus-wide customer frame
            # just to expand a category list.
            sales_all = self._dm.get_customer_data(route_code)
            if sales_all.empty or "CategoryName" not in sales_all.columns:
                # No way to expand category -> item; if explicit items also
                # absent, treat as "no match" rather than "no filter".
                return explicit_items if explicit_items is not None else set()
            cat_items = set(
                sales_all.loc[
                    sales_all["CategoryName"].astype(str).isin(cats),
                    "ItemCode",
                ].astype(str).str.strip().unique()
            )
            return cat_items if explicit_items is None else (cat_items & explicit_items)

        return explicit_items

    def _load_recs(
        self,
        start_date: str,
        end_date: str,
        route_code: Optional[str],
        item_filter: Optional[set] = None,
    ) -> pd.DataFrame:
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
        if item_filter is not None:
            out = out[out["item_code"].isin(item_filter)]
        return out

    def _load_sales(
        self,
        start_date: str,
        end_date: str,
        route_code: Optional[str],
        recs: pd.DataFrame,
        item_filter: Optional[set] = None,
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
        if item_filter is not None:
            sub = sub[sub["item_code"].isin(item_filter)]
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
        """Business-facing summary with a clean arithmetic identity.

        Decomposes total recommended volume/revenue on the SAME rows using the
        SAME base so Tile 1 and Tile 4 reconcile:

            driven_* + unsold_* = recommended_*   (per SKU and in aggregate)

        driven = Σ min(recommended_qty, actual_qty)  -- recs that converted
        unsold = Σ max(0, recommended_qty − actual)  -- recs that didn't
        This mirrors the VanLoad drawer's decomposition on the forecast side.
        """
        recommended_rows = merged[merged["_merge"].isin(["left_only", "both"])]
        adopted_rows = merged[merged["adopted"]]
        prices_available = (
            "unit_price" in merged.columns and float(merged["unit_price"].sum()) > 0
        )

        # --- Unique-SKU sets (drive the tile counts) ---
        rec_skus = set(recommended_rows["item_code"].astype(str).unique())
        bought_skus = set(
            merged.loc[
                merged["_merge"].isin(["right_only", "both"]) & (merged["actual_qty"] > 0),
                "item_code",
            ].astype(str).unique()
        )
        adopted_skus = rec_skus & bought_skus

        # --- Vectorised decomposition of recommended volume/revenue ---
        # Both tiles pull from the same rec_qty and actual_qty columns on the
        # same row set, so there's no base-mismatch or universe-mismatch.
        rec_q = recommended_rows["recommended_qty"]
        act_q = recommended_rows["actual_qty"]
        driven_q = np.minimum(rec_q, act_q)
        unsold_q = np.maximum(0.0, rec_q - act_q)

        driven_volume = float(driven_q.sum())
        unsold_volume = float(unsold_q.sum())
        recommended_volume = float(rec_q.sum())

        if prices_available:
            price = recommended_rows["unit_price"]
            driven_revenue: Optional[float] = float((driven_q * price).sum())
            unsold_revenue: Optional[float] = float((unsold_q * price).sum())
            recommended_revenue: Optional[float] = float((rec_q * price).sum())
        else:
            driven_revenue = None
            unsold_revenue = None
            recommended_revenue = None

        # Items with any shortfall (aggregate rec > aggregate actual). Broader
        # than strict dud SKUs -- captures partial over-recommendation too.
        if not recommended_rows.empty:
            by_sku = recommended_rows.groupby("item_code").agg(
                rec=("recommended_qty", "sum"),
                act=("actual_qty", "sum"),
            )
            unsold_sku_count = int((by_sku["rec"] > by_sku["act"]).sum())
        else:
            unsold_sku_count = 0

        # --- Tile 3: Perfect picks (adopted SKUs within ±tolerance of actual) ---
        if not adopted_rows.empty:
            ad_sku = adopted_rows.groupby("item_code").agg(
                rec=("recommended_qty", "sum"),
                act=("actual_qty", "sum"),
            )
            ad_sku = ad_sku[ad_sku["act"] > 0]
            within = (ad_sku["rec"] - ad_sku["act"]).abs() / ad_sku["act"] <= _PERFECT_PICK_TOLERANCE
            skus_perfect = int(within.sum())
        else:
            skus_perfect = 0

        return {
            # Tile 1: Revenue driven by our list (recs that converted)
            "driven_volume": round(driven_volume, 1),
            "driven_revenue": round(driven_revenue, 2) if driven_revenue is not None else None,
            # Tile 4: Lost sales (recs that didn't convert)
            "unsold_volume": round(unsold_volume, 1),
            "unsold_revenue": round(unsold_revenue, 2) if unsold_revenue is not None else None,
            "unsold_sku_count": unsold_sku_count,
            # Totals so the UI can show the "X of Y" ratio honestly
            "recommended_volume": round(recommended_volume, 1),
            "recommended_revenue": round(recommended_revenue, 2) if recommended_revenue is not None else None,
            # Tile 3: Perfect picks
            "skus_perfect": skus_perfect,
            "perfect_pick_tolerance": _PERFECT_PICK_TOLERANCE,
            # Tile 2 + context bar + highlights
            "skus_recommended": len(rec_skus),
            "skus_adopted": len(adopted_skus),
            "skus_bought": len(bought_skus),
        }

    @staticmethod
    def _active_dates(merged: pd.DataFrame, sales: pd.DataFrame) -> List[str]:
        """Working-day axis for the window. A day counts when either
        the rep was on the road (recs OR sales activity in scope).

        The union keeps days with recs but no sales (engine ran on a
        no-sale day) and days with sales but no recs (rep delivered
        without a list) both visible in the chart -- the supervisor
        needs to see both kinds of gaps.
        """
        seen: set[str] = set()
        for frame in (merged, sales):
            if frame.empty or "trx_date" not in frame.columns:
                continue
            for d in frame["trx_date"].dropna().astype(str).unique():
                seen.add(d)
        return sorted(seen)

    @staticmethod
    def _daily_padded(
        merged: pd.DataFrame, active_dates: List[str]
    ) -> List[Dict[str, Any]]:
        """Daily adoption rate padded to every working day in the window.

        Days without any recommendation rows land with
        ``adoption_pct=None`` (the chart treats them as a break in the
        line). A real zero (rec > 0 but adopted = 0) plots as 0.0 --
        the honest signal a supervisor needs to see.
        """
        if not active_dates and merged.empty:
            return []
        by_date: Dict[str, Dict[str, Any]] = {}
        if not merged.empty:
            for day, g in merged.groupby("trx_date"):
                if pd.isna(day):
                    continue
                recommended = int(
                    ((g["_merge"] == "left_only") | (g["_merge"] == "both")).sum()
                )
                adopted = int(g["adopted"].sum())
                rate = (
                    round(adopted / recommended * 100, 1) if recommended else 0.0
                )
                by_date[str(day)] = {
                    "date": str(day),
                    "recommended": recommended,
                    "adopted": adopted,
                    "adoption_pct": rate,
                }
        rows: List[Dict[str, Any]] = []
        for d in active_dates:
            if d in by_date:
                rows.append(by_date[d])
            else:
                rows.append(
                    {
                        "date": d,
                        "recommended": 0,
                        "adopted": 0,
                        "adoption_pct": None,
                    }
                )
        return rows

    @staticmethod
    def _derived_metrics(
        summary: Dict[str, Any],
        daily: List[Dict[str, Any]],
        active_dates: List[str],
    ) -> Dict[str, Any]:
        """Server-side compute for the four "derived" values the drawer
        used to recompute on every render: pick accuracy, perfect-pick
        rate, days-with-recommendations count, best-performing day.

        Each is None when the source counts can't yield a meaningful
        number (e.g. no recommendations at all in the window).
        """
        skus_recommended = int(summary.get("skus_recommended") or 0)
        skus_adopted = int(summary.get("skus_adopted") or 0)
        skus_perfect = int(summary.get("skus_perfect") or 0)

        pick_accuracy_pct: Optional[float] = (
            round(skus_adopted / skus_recommended * 100.0, 1)
            if skus_recommended > 0
            else None
        )
        perfect_pick_pct: Optional[float] = (
            round(skus_perfect / skus_adopted * 100.0, 1)
            if skus_adopted > 0
            else None
        )

        days_with_recs = sum(1 for d in daily if (d.get("recommended") or 0) > 0)
        active_days = len(active_dates)

        best_day: Optional[Dict[str, Any]] = None
        for d in daily:
            rec = int(d.get("recommended") or 0)
            pct = d.get("adoption_pct")
            if rec <= 0 or pct is None:
                continue
            if best_day is None or float(pct) > float(best_day["pct"]):
                best_day = {"date": d["date"], "pct": float(pct)}

        return {
            "pick_accuracy_pct": pick_accuracy_pct,
            "perfect_pick_pct": perfect_pick_pct,
            "days_with_recs": days_with_recs,
            "active_days": active_days,
            "best_day": best_day,
        }

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
