"""
EDA service -- aggregated overview of sales_recent.csv + customer overview from YaumiLive.
Cached aggregates (5-min TTL) so repeated dashboard hits stay fast.
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd
import pyodbc

from data_import.config.settings import Settings, get_settings

logger = logging.getLogger(__name__)


class EdaService:
    """Aggregated EDA over sales_recent.csv + live customer overview from YaumiLive."""

    # 24h TTL is safe because the importer's scheduler explicitly invalidates
    # the cache after each incremental pull (see data_import.scheduler).
    def __init__(self, settings: Optional[Settings] = None, ttl_seconds: int = 24 * 3600) -> None:
        self._s = settings or get_settings()
        self._ttl = ttl_seconds
        self._lock = threading.Lock()
        self._cache: Dict[str, tuple[float, Any]] = {}

    # ------------------------------------------------------------------
    # Cache helpers
    # ------------------------------------------------------------------

    def _cached(self, key: str, loader) -> Any:
        now = time.time()
        with self._lock:
            entry = self._cache.get(key)
            if entry and (now - entry[0]) < self._ttl:
                return entry[1]
        value = loader()
        with self._lock:
            self._cache[key] = (now, value)
        return value

    def invalidate(self) -> None:
        with self._lock:
            self._cache.clear()

    # ------------------------------------------------------------------
    # Sales overview (from local sales_recent.csv)
    # ------------------------------------------------------------------

    def get_sales_overview(self) -> Dict[str, Any]:
        return self._cached("sales_overview", self._compute_sales_overview)

    def _load_sales_df(self) -> pd.DataFrame:
        path = self._s.data_path(self._s.sales_recent_file)
        if not path.exists():
            logger.warning("Sales file not found: %s", path)
            return pd.DataFrame()
        df = pd.read_csv(path, low_memory=False)
        df["TrxDate"] = pd.to_datetime(df["TrxDate"], errors="coerce")
        df["TotalQuantity"] = pd.to_numeric(df["TotalQuantity"], errors="coerce").fillna(0)
        df["AvgUnitPrice"] = pd.to_numeric(df["AvgUnitPrice"], errors="coerce").fillna(0)
        df["revenue"] = df["TotalQuantity"] * df["AvgUnitPrice"]
        return df.dropna(subset=["TrxDate"])

    def _compute_sales_overview(self) -> Dict[str, Any]:
        df = self._load_sales_df()
        if df.empty:
            return {"available": False, "message": "sales_recent.csv not found or empty"}

        total_qty = float(df["TotalQuantity"].sum())
        total_rev = float(df["revenue"].sum())

        # Daily trend (last 90 days)
        cutoff = df["TrxDate"].max() - pd.Timedelta(days=90)
        recent = df[df["TrxDate"] >= cutoff]
        daily = (
            recent.groupby(recent["TrxDate"].dt.date)
            .agg(quantity=("TotalQuantity", "sum"), revenue=("revenue", "sum"))
            .reset_index()
            .rename(columns={"TrxDate": "date"})
        )
        daily["date"] = daily["date"].astype(str)

        # Top items
        top_items = (
            df.groupby(["ItemCode", "ItemName"], as_index=False)
            .agg(quantity=("TotalQuantity", "sum"), revenue=("revenue", "sum"))
            .sort_values("quantity", ascending=False)
            .head(10)
        )

        # Top routes
        top_routes = (
            df.groupby("RouteCode", as_index=False)
            .agg(quantity=("TotalQuantity", "sum"), revenue=("revenue", "sum"), items=("ItemCode", "nunique"))
            .sort_values("quantity", ascending=False)
            .head(10)
        )
        top_routes["RouteCode"] = top_routes["RouteCode"].astype(str)

        # Categories
        categories = (
            df.groupby("CategoryName", as_index=False)
            .agg(quantity=("TotalQuantity", "sum"), revenue=("revenue", "sum"))
            .sort_values("quantity", ascending=False)
            .head(10)
        )
        categories["CategoryName"] = categories["CategoryName"].fillna("Uncategorized")

        return {
            "available": True,
            "totals": {
                "transactions": int(len(df)),
                "total_quantity": round(total_qty, 1),
                "total_revenue": round(total_rev, 2),
                "unique_routes": int(df["RouteCode"].nunique()),
                "unique_items": int(df["ItemCode"].nunique()),
                "unique_warehouses": int(df["WarehouseCode"].nunique()),
                "unique_categories": int(df["CategoryName"].nunique()),
                "first_date": df["TrxDate"].min().strftime("%Y-%m-%d"),
                "last_date": df["TrxDate"].max().strftime("%Y-%m-%d"),
                "days_covered": int((df["TrxDate"].max() - df["TrxDate"].min()).days),
            },
            "daily_trend": daily.to_dict("records"),
            "top_items": top_items.to_dict("records"),
            "top_routes": top_routes.to_dict("records"),
            "categories": categories.to_dict("records"),
        }

    # ------------------------------------------------------------------
    # Item catalog -- latest price + metadata per item (from sales_recent.csv)
    # ------------------------------------------------------------------

    def get_item_catalog(self) -> Dict[str, Any]:
        return self._cached("item_catalog", self._compute_item_catalog)

    def _compute_item_catalog(self) -> Dict[str, Any]:
        df = self._load_sales_df()
        if df.empty:
            return {"available": False, "items": []}

        # Latest price per item = weighted-recent (keep last 365 days for freshness)
        cutoff = df["TrxDate"].max() - pd.Timedelta(days=365)
        recent = df[df["TrxDate"] >= cutoff].copy()
        if recent.empty:
            recent = df

        catalog = (
            recent.groupby(["ItemCode", "ItemName", "CategoryName"], as_index=False)
            .agg(
                avg_price=("AvgUnitPrice", "mean"),
                last_price=("AvgUnitPrice", "last"),
                total_quantity=("TotalQuantity", "sum"),
                transactions=("TrxDate", "count"),
                last_seen=("TrxDate", "max"),
            )
        )
        catalog["ItemCode"] = catalog["ItemCode"].astype(str).str.strip()
        catalog["avg_price"] = catalog["avg_price"].round(2).astype(float)
        catalog["last_price"] = catalog["last_price"].round(2).astype(float)
        catalog["total_quantity"] = catalog["total_quantity"].round(1).astype(float)
        catalog["last_seen"] = catalog["last_seen"].dt.strftime("%Y-%m-%d")

        return {
            "available": True,
            "count": int(len(catalog)),
            "items": catalog.to_dict("records"),
        }

    # ------------------------------------------------------------------
    # Business KPIs -- actionable daily-ops view (uses cached CSVs only)
    # ------------------------------------------------------------------

    def get_business_kpis(self) -> Dict[str, Any]:
        return self._cached("business_kpis", self._compute_business_kpis)

    def _compute_business_kpis(self) -> Dict[str, Any]:
        """Compute a small, opinionated set of metrics a supervisor can act on:
            * yesterday's revenue + WoW delta
            * 7-day revenue + WoW delta
            * forecast accuracy over the last 7 days
            * today's planned operations (routes + customers from journey plan)
        All from local CSVs -- no DB round-trips.
        """
        sales = self._load_sales_df()
        if sales.empty:
            return {"available": False, "message": "sales_recent.csv not found or empty"}

        anchor = pd.Timestamp(sales["TrxDate"].max().date())
        yesterday = anchor
        week_ago = anchor - pd.Timedelta(days=6)
        prior_week_end = week_ago - pd.Timedelta(days=1)
        prior_week_start = prior_week_end - pd.Timedelta(days=6)
        same_day_last_week = anchor - pd.Timedelta(days=7)

        def _revenue_in(start: pd.Timestamp, end: pd.Timestamp) -> float:
            mask = (sales["TrxDate"] >= start) & (sales["TrxDate"] <= end)
            return float(sales.loc[mask, "revenue"].sum())

        yesterday_rev = _revenue_in(yesterday, yesterday)
        last_week_same_day_rev = _revenue_in(same_day_last_week, same_day_last_week)
        last_7d_rev = _revenue_in(week_ago, anchor)
        prior_7d_rev = _revenue_in(prior_week_start, prior_week_end)

        def _delta_pct(now: float, prev: float) -> Optional[float]:
            if prev <= 0:
                return None
            return round((now - prev) / prev * 100, 1)

        # Forecast accuracy (7d)
        forecast_accuracy = self._forecast_accuracy_recent(days=7, anchor=anchor)

        # Today's planned operations
        today_ops = self._today_operations(anchor)

        return {
            "available": True,
            "anchor_date": anchor.strftime("%Y-%m-%d"),
            "yesterday": {
                "revenue": round(yesterday_rev, 2),
                "delta_pct_vs_last_week": _delta_pct(yesterday_rev, last_week_same_day_rev),
                "comparison_label": "vs same day last week",
            },
            "last_7_days": {
                "revenue": round(last_7d_rev, 2),
                "delta_pct_vs_prior_7d": _delta_pct(last_7d_rev, prior_7d_rev),
                "prior_revenue": round(prior_7d_rev, 2),
            },
            "forecast_accuracy_7d": forecast_accuracy,
            "today_operations": today_ops,
        }

    def _forecast_accuracy_recent(self, *, days: int, anchor: pd.Timestamp) -> Dict[str, Any]:
        """Mean accuracy for forecast rows in the trailing `days` window.

        Uses ``demand_forecast.csv`` which carries ``Predicted`` + ``ActualQty``.
        Computes (1 - WAPE) over rows where actual > 0, mirroring the accuracy
        service's formula so this card and the drawer agree.
        """
        path = self._s.data_path(self._s.demand_forecast_file)
        if not path.exists():
            return {"available": False, "message": "demand_forecast.csv not found"}
        df = pd.read_csv(path, low_memory=False)
        if df.empty or "Predicted" not in df.columns or "ActualQty" not in df.columns:
            return {"available": False}

        df["TrxDate"] = pd.to_datetime(df["TrxDate"], errors="coerce")
        cutoff = anchor - pd.Timedelta(days=days - 1)
        window = df[(df["TrxDate"] >= cutoff) & (df["TrxDate"] <= anchor)]
        scored = window[pd.to_numeric(window["ActualQty"], errors="coerce") > 0]
        if scored.empty:
            return {"available": True, "rows_compared": 0, "accuracy_pct": None}
        actual = pd.to_numeric(scored["ActualQty"], errors="coerce")
        predicted = pd.to_numeric(scored["Predicted"], errors="coerce")
        wape = float((actual - predicted).abs().sum() / actual.sum() * 100)
        return {
            "available": True,
            "window_days": days,
            "rows_compared": int(len(scored)),
            "accuracy_pct": round(max(0.0, 100.0 - wape), 1),
        }

    def _today_operations(self, anchor: pd.Timestamp) -> Dict[str, Any]:
        """Routes & customers planned for today, from journey_plan.csv."""
        path = self._s.data_path(self._s.journey_plan_file)
        if not path.exists():
            return {"available": False, "message": "journey_plan.csv not found"}
        df = pd.read_csv(path, low_memory=False)
        if df.empty:
            return {"available": True, "routes": 0, "customers": 0}
        date_col = "JourneyDate" if "JourneyDate" in df.columns else "TrxDate"
        if date_col not in df.columns:
            return {"available": False, "message": f"{date_col} missing"}
        df[date_col] = pd.to_datetime(df[date_col], errors="coerce")
        today = df[df[date_col].dt.normalize() == anchor.normalize()]
        return {
            "available": True,
            "date": anchor.strftime("%Y-%m-%d"),
            "routes": int(today["RouteCode"].nunique()) if "RouteCode" in today.columns else 0,
            "customers": int(today["CustomerCode"].nunique()) if "CustomerCode" in today.columns else 0,
        }

    # ------------------------------------------------------------------
    # Per-item rolling stats (from sales_recent.csv)
    # ------------------------------------------------------------------

    def get_item_stats(self, item_code: str, route_code: Optional[str] = None) -> Dict[str, Any]:
        key = f"item_stats::{item_code}::{route_code or ''}"
        return self._cached(key, lambda: self._compute_item_stats(item_code, route_code))

    def _compute_item_stats(self, item_code: str, route_code: Optional[str]) -> Dict[str, Any]:
        df = self._load_sales_df()
        if df.empty:
            return {"available": False, "message": "sales_recent.csv not found or empty"}

        df = df[df["ItemCode"].astype(str).str.strip() == str(item_code).strip()]
        if route_code:
            df = df[df["RouteCode"].astype(str).str.strip() == str(route_code).strip()]

        if df.empty:
            return {
                "available": True,
                "item_code": item_code,
                "route_code": route_code,
                "windows": {w: None for w in ("last_week", "last_4_weeks", "last_3_months", "last_6_months")},
                "total_transactions": 0,
            }

        # Daily series: sum qty per day so averages reflect per-day demand
        daily = (
            df.groupby(df["TrxDate"].dt.normalize())["TotalQuantity"].sum().reset_index()
        )
        daily.columns = ["date", "qty"]
        daily = daily.sort_values("date")

        anchor = daily["date"].max()
        windows = {
            "last_week": 7,
            "last_4_weeks": 28,
            "last_3_months": 90,
            "last_6_months": 180,
        }

        def window_stats(days: int) -> Dict[str, Any]:
            cutoff = anchor - pd.Timedelta(days=days - 1)
            window = daily[daily["date"] >= cutoff]
            total = float(window["qty"].sum()) if not window.empty else 0.0
            active = int((window["qty"] > 0).sum()) if not window.empty else 0
            return {
                "avg": round(total / active, 2) if active > 0 else None,
                "total": round(total, 2),
                "active_days": active,
                "days": days,
            }

        return {
            "available": True,
            "item_code": item_code,
            "route_code": route_code,
            "anchor_date": anchor.strftime("%Y-%m-%d"),
            "windows": {name: window_stats(days) for name, days in windows.items()},
            "total_transactions": int(len(df)),
        }

    # ------------------------------------------------------------------
    # Live per-customer sales (live from YaumiLive -- short-TTL cached)
    # ------------------------------------------------------------------

    def get_live_customer_sales(self, route_code: str, date: str, customer_code: str) -> Dict[str, Any]:
        """Return ``{items: [{item_code, qty}], fetched_at, route, date, customer}``
        by querying VW_GET_SALES_DETAILS live for a single (route, date, customer).

        Cached for 60 s so rapid-fire visit clicks don't hammer the live DB.
        Matches the aggregation used everywhere else: positive QuantityInPCs,
        ItemType = OrderItem, TrxType = SalesInvoice.
        """
        key = f"live_sales::{route_code}::{date}::{customer_code}"
        return self._cached_with_ttl(
            key,
            lambda: self._fetch_live_customer_sales(route_code, date, customer_code),
            ttl_seconds=60,
        )

    def get_live_route_sales(self, route_code: str, date: str) -> Dict[str, Any]:
        """Return every ``(customer_code, customer_name, item_code, qty)`` sold on
        the given route/date. Live query against YaumiLive, 60-s cached.

        Same filter chain as :meth:`get_live_customer_sales` to guarantee the
        two endpoints never disagree on totals."""
        key = f"live_route_sales::{route_code}::{date}"
        return self._cached_with_ttl(
            key,
            lambda: self._fetch_live_route_sales(route_code, date),
            ttl_seconds=60,
        )

    def _fetch_live_route_sales(self, route_code: str, date: str) -> Dict[str, Any]:
        if not (self._s.db.host and self._s.db.username):
            return {"available": False, "message": "DB not configured", "customers": []}

        sql = f"""
            SELECT
                CustomerCode,
                MAX(CustomerName) AS CustomerName,
                ItemCode,
                SUM(CASE WHEN QuantityInPCs > 0 THEN QuantityInPCs ELSE 0 END) AS Qty
            FROM {self._s.sales_view} WITH (NOLOCK)
            WHERE ItemType = 'OrderItem'
              AND TrxType  = 'SalesInvoice'
              AND RouteCode = ?
              AND CAST(TrxDate AS DATE) = ?
            GROUP BY CustomerCode, ItemCode
        """
        try:
            conn = pyodbc.connect(self._s.db.connection_string(), autocommit=False)
            cursor = conn.cursor()
            cursor.execute(sql, (str(route_code), date))
            rows = cursor.fetchall()
            conn.close()
        except Exception as exc:
            logger.error("Live route-sales query failed: %s", exc)
            return {"available": False, "message": str(exc), "customers": []}

        # Pivot to one entry per customer with nested items.
        by_cust: Dict[str, Dict[str, Any]] = {}
        for r in rows:
            code = str(r[0] or "").strip()
            if not code:
                continue
            name = (r[1] or "").strip() if r[1] is not None else ""
            item = str(r[2] or "").strip()
            qty = int(float(r[3] or 0))
            if qty <= 0 or not item:
                continue
            entry = by_cust.setdefault(code, {"customer_code": code, "customer_name": name, "items": []})
            entry["items"].append({"item_code": item, "qty": qty})

        customers = sorted(by_cust.values(), key=lambda c: c["customer_code"])
        return {
            "available": True,
            "route_code": str(route_code),
            "date": date,
            "customers": customers,
            "fetched_at": pd.Timestamp.now().isoformat(),
        }

    def _fetch_live_customer_sales(self, route_code: str, date: str, customer_code: str) -> Dict[str, Any]:
        if not (self._s.db.host and self._s.db.username):
            return {"available": False, "message": "DB not configured", "items": []}

        sql = f"""
            SELECT
                ItemCode,
                SUM(CASE WHEN QuantityInPCs > 0 THEN QuantityInPCs ELSE 0 END) AS Qty
            FROM {self._s.sales_view} WITH (NOLOCK)
            WHERE ItemType = 'OrderItem'
              AND TrxType  = 'SalesInvoice'
              AND RouteCode = ?
              AND CustomerCode = ?
              AND CAST(TrxDate AS DATE) = ?
            GROUP BY ItemCode
        """
        try:
            conn = pyodbc.connect(self._s.db.connection_string(), autocommit=False)
            cursor = conn.cursor()
            cursor.execute(sql, (str(route_code), str(customer_code), date))
            rows = cursor.fetchall()
            conn.close()
        except Exception as exc:
            logger.error("Live customer-sales query failed: %s", exc)
            return {"available": False, "message": str(exc), "items": []}

        items = [
            {"item_code": str(r[0]).strip(), "qty": int(float(r[1] or 0))}
            for r in rows
            if r[0] is not None
        ]
        return {
            "available": True,
            "route_code": str(route_code),
            "date": date,
            "customer_code": str(customer_code),
            "items": items,
            "fetched_at": pd.Timestamp.now().isoformat(),
        }

    def _cached_with_ttl(self, key: str, loader, *, ttl_seconds: int) -> Any:
        """Variant of ``_cached`` that overrides the default TTL for short-lived
        live queries (the main EDA cache is 24 h)."""
        now = time.time()
        with self._lock:
            entry = self._cache.get(key)
            if entry and (now - entry[0]) < ttl_seconds:
                return entry[1]
        value = loader()
        with self._lock:
            self._cache[key] = (now, value)
        return value

    # ------------------------------------------------------------------
    # Customer overview (live from YaumiLive)
    # ------------------------------------------------------------------

    def get_customer_overview(self, lookback_days: int = 90) -> Dict[str, Any]:
        return self._cached(f"customer_overview_{lookback_days}", lambda: self._compute_customer_overview(lookback_days))

    def _compute_customer_overview(self, lookback_days: int) -> Dict[str, Any]:
        if not (self._s.db.host and self._s.db.username):
            return {"available": False, "message": "DB not configured"}

        routes = self._s.route_codes
        if not routes:
            return {"available": False, "message": "No route codes configured"}

        ph = ",".join("?" for _ in routes)
        sql = f"""
            SELECT
                CustomerCode AS customer_code,
                MAX(CustomerName) AS customer_name,
                MAX(RouteCode) AS route_code,
                COUNT(DISTINCT TrxDate) AS visits,
                COUNT(DISTINCT ItemCode) AS unique_items,
                SUM(CASE WHEN QuantityInPCs > 0 THEN QuantityInPCs ELSE 0 END) AS total_quantity,
                MAX(TrxDate) AS last_purchase
            FROM {self._s.sales_view} WITH (NOLOCK)
            WHERE ItemType = 'OrderItem'
              AND TrxType  = 'SalesInvoice'
              AND RouteCode IN ({ph})
              AND TrxDate >= DATEADD(day, -?, GETDATE())
            GROUP BY CustomerCode
        """
        params = list(routes) + [lookback_days]

        try:
            conn = pyodbc.connect(self._s.db.connection_string(), autocommit=False)
            cursor = conn.cursor()
            cursor.execute(sql, params)
            cols = [d[0] for d in cursor.description]
            df = pd.DataFrame.from_records(cursor.fetchall(), columns=cols)
            conn.close()
        except Exception as exc:
            logger.error("Customer overview query failed: %s", exc)
            return {"available": False, "message": f"DB query failed: {exc}"}

        if df.empty:
            return {"available": True, "totals": self._empty_customer_totals(lookback_days), "top_customers": [], "by_route": []}

        df["last_purchase"] = pd.to_datetime(df["last_purchase"]).dt.strftime("%Y-%m-%d")
        df["total_quantity"] = df["total_quantity"].astype(float)

        top_customers = df.sort_values("total_quantity", ascending=False).head(10).to_dict("records")

        by_route = (
            df.groupby("route_code", as_index=False)
            .agg(customers=("customer_code", "nunique"), total_quantity=("total_quantity", "sum"))
            .sort_values("customers", ascending=False)
            .to_dict("records")
        )

        return {
            "available": True,
            "totals": {
                "lookback_days": lookback_days,
                "active_customers": int(df["customer_code"].nunique()),
                "total_visits": int(df["visits"].sum()),
                "total_quantity": round(float(df["total_quantity"].sum()), 1),
                "avg_visits_per_customer": round(float(df["visits"].mean()), 1),
            },
            "top_customers": top_customers,
            "by_route": by_route,
        }

    @staticmethod
    def _empty_customer_totals(lookback_days: int) -> Dict[str, Any]:
        return {
            "lookback_days": lookback_days,
            "active_customers": 0,
            "total_visits": 0,
            "total_quantity": 0,
            "avg_visits_per_customer": 0,
        }
