"""
EDA service -- aggregated overview of sales_recent.csv + customer overview from YaumiLive.
Cached aggregates (5-min TTL) so repeated dashboard hits stay fast.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd
import pyodbc

from data_import.config.settings import Settings, get_settings

logger = logging.getLogger(__name__)


# Reporting-period enum exposed to the dashboard. A "working day" is any
# date that has actual sales activity in sales_recent.csv -- this naturally
# excludes weekends, public holidays, and any other closure without us
# having to hard-code a calendar. The numeric value is the count of such
# active dates to include in the trailing window.
LOOKBACK_OPTIONS: Dict[str, int] = {
    "last_working_day": 1,
    "last_7_working_days": 7,
}
DEFAULT_LOOKBACK = "last_7_working_days"


def _resolve_lookback(lookback: Optional[str]) -> tuple[str, int]:
    """Return (canonical key, working-day count). Unknown values fall back
    to the default so a stale frontend can never crash a backend call.
    """
    key = lookback if lookback in LOOKBACK_OPTIONS else DEFAULT_LOOKBACK
    return key, LOOKBACK_OPTIONS[key]


class EdaService:
    """Aggregated EDA over sales_recent.csv + live customer overview from YaumiLive."""

    # 24h TTL is safe because the importer's scheduler explicitly invalidates
    # the cache after each incremental pull (see data_import.scheduler).
    _MAX_CACHE_ENTRIES = 2000  # safety cap so per-item keys can't grow unbounded

    def __init__(self, settings: Optional[Settings] = None, ttl_seconds: int = 24 * 3600) -> None:
        self._s = settings or get_settings()
        self._ttl = ttl_seconds
        self._lock = threading.Lock()
        self._cache: OrderedDict[str, tuple[float, Any]] = OrderedDict()

    # ------------------------------------------------------------------
    # Cache helpers
    # ------------------------------------------------------------------

    def _cached(self, key: str, loader) -> Any:
        now = time.time()
        with self._lock:
            entry = self._cache.get(key)
            if entry and (now - entry[0]) < self._ttl:
                self._cache.move_to_end(key)
                return entry[1]
        value = loader()
        with self._lock:
            self._cache[key] = (now, value)
            self._cache.move_to_end(key)
            while len(self._cache) > self._MAX_CACHE_ENTRIES:
                self._cache.popitem(last=False)
        return value

    def invalidate(self) -> None:
        with self._lock:
            self._cache.clear()

    # ------------------------------------------------------------------
    # Shared dashboard-filter layer
    # ------------------------------------------------------------------
    #
    # Every dashboard endpoint that reads sales_recent.csv goes through this
    # same helper so the filter semantics are identical across tiles, charts,
    # and the cascading dimensions endpoint. Filters are passed as lists of
    # codes (warehouse, route, item) or names (category -- sales_recent only
    # carries CategoryName). An empty list means "no filter at this level."

    @staticmethod
    def _apply_sales_filters(
        df: pd.DataFrame,
        warehouse_codes: List[str],
        route_codes: List[str],
        category_codes: List[str],
        item_codes: List[str],
    ) -> pd.DataFrame:
        if df.empty:
            return df
        if warehouse_codes:
            df = df[df["WarehouseCode"].astype(str).isin(set(map(str, warehouse_codes)))]
        if route_codes:
            df = df[df["RouteCode"].astype(str).isin(set(map(str, route_codes)))]
        if category_codes:
            df = df[df["CategoryName"].astype(str).isin(set(map(str, category_codes)))]
        if item_codes:
            df = df[df["ItemCode"].astype(str).isin(set(map(str, item_codes)))]
        return df

    @staticmethod
    def _filter_key(
        warehouse_codes: List[str],
        route_codes: List[str],
        category_codes: List[str],
        item_codes: List[str],
    ) -> str:
        """Deterministic cache-key fragment for a filter combination.
        Sorted so different orderings of the same selection share a cache slot.
        """
        def part(name: str, vals: List[str]) -> str:
            return f"{name}={'|'.join(sorted(set(map(str, vals))))}" if vals else f"{name}="
        return ";".join([
            part("w", warehouse_codes),
            part("r", route_codes),
            part("c", category_codes),
            part("i", item_codes),
        ])

    def get_filter_dimensions(
        self,
        warehouse_codes: List[str],
        route_codes: List[str],
        category_codes: List[str],
    ) -> Dict[str, Any]:
        """Return the cascading filter options for the dashboard FilterBar.

        Each downstream dimension is the set of unique values present in the
        sales slice that already matches every upstream selection. Items are
        filtered by all three upstream levels so the deepest dropdown stays
        scoped tightly even when the user makes multiple picks above it.
        """
        key = "filter_dims::" + self._filter_key(warehouse_codes, route_codes, category_codes, [])
        return self._cached(key, lambda: self._compute_filter_dimensions(
            warehouse_codes, route_codes, category_codes,
        ))

    def _compute_filter_dimensions(
        self,
        warehouse_codes: List[str],
        route_codes: List[str],
        category_codes: List[str],
    ) -> Dict[str, Any]:
        df = self._load_sales_df()
        if df.empty:
            return {"warehouses": [], "routes": [], "categories": [], "items": []}

        # Warehouses: every distinct warehouse in the full slice (the master
        # set -- selecting one warehouse doesn't shrink its own dropdown).
        warehouses = (
            df[["WarehouseCode", "WarehouseName"]]
            .dropna()
            .drop_duplicates("WarehouseCode")
            .sort_values("WarehouseCode")
            .to_dict("records")
        )

        # Routes: filtered by warehouse selection. RouteCode is the only
        # identifier in sales_recent; we surface it as both code and name so
        # the UI can render "9105" without a fallback path. Each route also
        # carries its parent warehouse so the VanLoad route grid can group
        # cards by warehouse without a second API call.
        scoped = self._apply_sales_filters(df, warehouse_codes, [], [], [])
        route_warehouse = (
            scoped[["RouteCode", "WarehouseCode", "WarehouseName"]]
            .dropna(subset=["RouteCode"])
            .drop_duplicates("RouteCode")
            .sort_values("RouteCode")
        )
        routes = [
            {
                "code": str(row.RouteCode).strip(),
                "name": str(row.RouteCode).strip(),
                "warehouse_code": str(row.WarehouseCode).strip(),
                "warehouse_name": str(row.WarehouseName).strip(),
            }
            for row in route_warehouse.itertuples(index=False)
        ]

        # Categories: filtered by warehouse + route. sales_recent only carries
        # CategoryName so name doubles as the identifier.
        scoped = self._apply_sales_filters(df, warehouse_codes, route_codes, [], [])
        cat_vals = sorted(
            scoped["CategoryName"].dropna().astype(str).str.strip().replace("", pd.NA).dropna().unique()
        )
        categories = [{"code": c, "name": c} for c in cat_vals]

        # Items: filtered by all three upstream levels.
        scoped = self._apply_sales_filters(df, warehouse_codes, route_codes, category_codes, [])
        item_pairs = (
            scoped[["ItemCode", "ItemName"]]
            .dropna(subset=["ItemCode"])
            .drop_duplicates("ItemCode")
            .sort_values("ItemCode")
        )
        items = [
            {"code": str(r.ItemCode).strip(), "name": str(r.ItemName).strip() or str(r.ItemCode).strip()}
            for r in item_pairs.itertuples(index=False)
        ]

        return {
            "warehouses": [
                {"code": str(r["WarehouseCode"]).strip(), "name": str(r["WarehouseName"]).strip()}
                for r in warehouses
            ],
            "routes": routes,
            "categories": categories,
            "items": items,
        }

    # ------------------------------------------------------------------
    # Shared reference data
    # ------------------------------------------------------------------

    def get_item_prices(self) -> Dict[str, float]:
        """Return {ItemCode: avg unit price} from customer_data.csv.

        Single source of truth for item pricing across the app; cached like
        every other derivation so repeat callers don't re-read the CSV.
        """
        return self._cached("item_prices", self._compute_item_prices)

    def _compute_item_prices(self) -> Dict[str, float]:
        path = self._s.data_path(self._s.customer_data_file)
        if not path.exists():
            return {}
        df = pd.read_csv(path, low_memory=False, usecols=["ItemCode", "AvgUnitPrice"])
        df["AvgUnitPrice"] = pd.to_numeric(df["AvgUnitPrice"], errors="coerce")
        df = df.dropna(subset=["ItemCode", "AvgUnitPrice"])
        df = df[df["AvgUnitPrice"] > 0]
        if df.empty:
            return {}
        grouped = df.groupby("ItemCode")["AvgUnitPrice"].mean()
        return {str(k): round(float(v), 2) for k, v in grouped.items()}

    # ------------------------------------------------------------------
    # Sales overview (from local sales_recent.csv)
    # ------------------------------------------------------------------

    def get_sales_overview(
        self,
        lookback: Optional[str] = None,
        warehouse_codes: Optional[List[str]] = None,
        route_codes: Optional[List[str]] = None,
        category_codes: Optional[List[str]] = None,
        item_codes: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        canonical, _ = _resolve_lookback(lookback)
        w, r, c, i = warehouse_codes or [], route_codes or [], category_codes or [], item_codes or []
        key = f"sales_overview::{canonical}::" + self._filter_key(w, r, c, i)
        return self._cached(key, lambda: self._compute_sales_overview(canonical, w, r, c, i))

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

    def _compute_sales_overview(
        self,
        lookback: str = DEFAULT_LOOKBACK,
        warehouse_codes: Optional[List[str]] = None,
        route_codes: Optional[List[str]] = None,
        category_codes: Optional[List[str]] = None,
        item_codes: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Sales aggregates for the trailing N working-day window, optionally
        scoped to the dashboard FilterBar selection.

        A "working day" is any date that has actual sales activity in
        sales_recent.csv -- naturally excludes weekends, holidays, and any
        other closure without a hard-coded calendar. All four breakdowns
        (daily trend, top items, top routes, categories) share the window
        so the dashboard period selector drives every chart consistently.
        """
        df = self._load_sales_df()
        if df.empty:
            return {"available": False, "message": "sales_recent.csv not found or empty"}

        canonical, n_working = _resolve_lookback(lookback)
        # Working-day slice: pick the last N distinct dates with sales,
        # then keep only rows whose TrxDate falls in that set. Silently
        # clamps when CSV has fewer than N active dates.
        active_dates = sorted(df["TrxDate"].dt.normalize().unique(), reverse=True)[:n_working]
        if not active_dates:
            return {"available": True, "lookback": canonical, "totals": {}, "daily_trend": [],
                    "top_items": [], "top_routes": [], "categories": []}
        df = df[df["TrxDate"].dt.normalize().isin(active_dates)]
        df = self._apply_sales_filters(
            df, warehouse_codes or [], route_codes or [], category_codes or [], item_codes or [],
        )
        if df.empty:
            return {"available": True, "lookback": canonical, "totals": {}, "daily_trend": [],
                    "top_items": [], "top_routes": [], "categories": []}

        total_qty = float(df["TotalQuantity"].sum())
        total_rev = float(df["revenue"].sum())

        daily = (
            df.groupby(df["TrxDate"].dt.date)
            .agg(quantity=("TotalQuantity", "sum"), revenue=("revenue", "sum"))
            .reset_index()
            .rename(columns={"TrxDate": "date"})
        )
        daily["date"] = daily["date"].astype(str)

        top_items = (
            df.groupby(["ItemCode", "ItemName"], as_index=False)
            .agg(quantity=("TotalQuantity", "sum"), revenue=("revenue", "sum"))
            .sort_values("quantity", ascending=False)
            .head(10)
        )

        top_routes = (
            df.groupby("RouteCode", as_index=False)
            .agg(quantity=("TotalQuantity", "sum"), revenue=("revenue", "sum"), items=("ItemCode", "nunique"))
            .sort_values("quantity", ascending=False)
            .head(10)
        )
        top_routes["RouteCode"] = top_routes["RouteCode"].astype(str)

        categories = (
            df.groupby("CategoryName", as_index=False)
            .agg(quantity=("TotalQuantity", "sum"), revenue=("revenue", "sum"))
            .sort_values("quantity", ascending=False)
            .head(10)
        )
        categories["CategoryName"] = categories["CategoryName"].fillna("Uncategorized")

        return {
            "available": True,
            "lookback": canonical,
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
                "working_days": int(df["TrxDate"].dt.normalize().nunique()),
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

    def get_business_kpis(
        self,
        lookback: Optional[str] = None,
        warehouse_codes: Optional[List[str]] = None,
        route_codes: Optional[List[str]] = None,
        category_codes: Optional[List[str]] = None,
        item_codes: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        canonical, _ = _resolve_lookback(lookback)
        w, r, c, i = warehouse_codes or [], route_codes or [], category_codes or [], item_codes or []
        key = f"business_kpis::{canonical}::" + self._filter_key(w, r, c, i)
        return self._cached(key, lambda: self._compute_business_kpis(canonical, w, r, c, i))

    # ------------------------------------------------------------------
    # Demand-forecast loader -- the van-load source. Predicted = what we
    # told the van to load; ActualQty is included for some rows but we
    # join against customer_data for fresher actuals + per-period prices.
    # ------------------------------------------------------------------

    _FORECAST_COLUMNS = ["TrxDate", "RouteCode", "ItemCode", "DataSplit", "Predicted"]

    def _load_forecast_df(self) -> pd.DataFrame:
        path = self._s.data_path(self._s.demand_forecast_file)
        if not path.exists():
            logger.warning("Demand-forecast file not found: %s", path)
            return pd.DataFrame()
        df = pd.read_csv(path, low_memory=False, usecols=self._FORECAST_COLUMNS)
        df["TrxDate"] = pd.to_datetime(df["TrxDate"], errors="coerce")
        df["RouteCode"] = df["RouteCode"].astype(str).str.strip()
        df["ItemCode"] = df["ItemCode"].astype(str).str.strip()
        df["Predicted"] = pd.to_numeric(df["Predicted"], errors="coerce").fillna(0)
        return df.dropna(subset=["TrxDate"])

    # ------------------------------------------------------------------
    # Shared (sales ⋈ forecast) merge -- consumed by both /eda/business-kpis
    # and /eda/forecast-rows. Single compute path so the two endpoints can
    # never disagree on what "this scope, this period" means.
    # ------------------------------------------------------------------

    def _actual_vs_forecast_merge(
        self,
        lookback: str,
        warehouse_codes: List[str],
        route_codes: List[str],
        category_codes: List[str],
        item_codes: List[str],
    ) -> Optional[Dict[str, Any]]:
        """Build the per-(date, route, item) merge of past forecasts vs
        actual sales over the lookback window. Returns None when there is
        nothing in scope; otherwise a dict with:

            sales         -- filtered + date-restricted sales rows
            merged        -- forecast OUTER-JOIN sold, with price fallback
            anchor        -- max active date
            working_days  -- count of active dates
            covered_routes / covered_days -- forecast scope counters
        """
        _, n_working = _resolve_lookback(lookback)

        sales = self._apply_sales_filters(
            self._load_sales_df(),
            warehouse_codes, route_codes, category_codes, item_codes,
        )
        if sales.empty:
            return None

        active_dates = sorted(sales["TrxDate"].dt.normalize().unique(), reverse=True)[:n_working]
        if not active_dates:
            return None
        active_set = set(active_dates)
        sales = sales[sales["TrxDate"].dt.normalize().isin(active_set)].copy()
        sales["TrxDate"] = sales["TrxDate"].dt.normalize()
        sales["RouteCode"] = sales["RouteCode"].astype(str).str.strip()
        sales["ItemCode"] = sales["ItemCode"].astype(str).str.strip()
        anchor = sales["TrxDate"].max()

        # Aggregate sales to (date, route, item); pull ItemName along so
        # downstream consumers (drawer rows) don't need a second lookup.
        sold = (
            sales.groupby(["TrxDate", "RouteCode", "ItemCode"], as_index=False)
            .agg(
                actual_qty=("TotalQuantity", "sum"),
                revenue=("revenue", "sum"),
                ItemName=("ItemName", "first"),
            )
        )
        sold["price"] = (sold["revenue"] / sold["actual_qty"].replace(0, pd.NA)).fillna(0.0)

        # Forecast scope mirrors the sales scope (route + item set).
        scope_routes = set(sales["RouteCode"].unique())
        scope_items = set(sales["ItemCode"].unique())
        forecast = self._load_forecast_df()
        if not forecast.empty:
            # "Forecast" split = the predictions that actually drove van
            # loads in production (future_forecast.csv). "Test" is the
            # held-out evaluation slice and is irrelevant for impact.
            forecast = forecast[forecast["DataSplit"] == "Forecast"]
            # Predicted == 0 means "skip this item" -- not coverage.
            forecast = forecast[forecast["Predicted"] > 0]
            forecast = forecast[forecast["TrxDate"].dt.normalize().isin(active_set)]
            forecast = forecast[forecast["RouteCode"].isin(scope_routes)]
            forecast = forecast[forecast["ItemCode"].isin(scope_items)]

        if not forecast.empty:
            forecast["TrxDate"] = forecast["TrxDate"].dt.normalize()
            forecast = (
                forecast.groupby(["TrxDate", "RouteCode", "ItemCode"], as_index=False)
                .agg(predicted=("Predicted", "sum"))
            )
            covered_cells = forecast[["RouteCode", "TrxDate"]].drop_duplicates()
            covered_routes = int(covered_cells["RouteCode"].nunique())
            covered_days = int(covered_cells["TrxDate"].nunique())
        else:
            forecast = pd.DataFrame(columns=["TrxDate", "RouteCode", "ItemCode", "predicted"])
            covered_routes = 0
            covered_days = 0

        # Outer-join so a forecast with no sale (lost) AND a sale without
        # forecast (uncovered demand) both surface in `merged`.
        merged = forecast.merge(
            sold[["TrxDate", "RouteCode", "ItemCode", "actual_qty", "price", "ItemName"]],
            on=["TrxDate", "RouteCode", "ItemCode"], how="outer",
        )
        for col in ("predicted", "actual_qty", "price"):
            merged[col] = merged[col].fillna(0.0)

        # Price fallback for forecast-only rows that had no matching sale.
        zero_price = merged["price"] <= 0
        if zero_price.any():
            price_lookup = self.get_item_prices()
            merged.loc[zero_price, "price"] = (
                merged.loc[zero_price, "ItemCode"].map(price_lookup).fillna(0.0)
            )

        return {
            "sales": sales,
            "merged": merged,
            "forecast": forecast,
            "anchor": anchor,
            "working_days": len(active_dates),
            "covered_routes": covered_routes,
            "covered_days": covered_days,
        }

    def _compute_business_kpis(
        self,
        lookback: str,
        warehouse_codes: List[str],
        route_codes: List[str],
        category_codes: List[str],
        item_codes: List[str],
    ) -> Dict[str, Any]:
        """Four headline metrics for the executive dashboard:

            1. total_revenue   -- AED sold in the period (sales_recent)
            2. total_volume    -- total units sold + transaction count
            3. unique_items    -- count of distinct SKUs that sold
            4. lost_opportunity -- AED of forecast that didn't sell
                                  (Σ max(0, predicted - actual) × price)

        All four are derived from the shared (sales ⋈ forecast) merge --
        the same helper that powers the Past-analysis drawer, so the
        numbers can never drift between the two surfaces.
        """
        ctx = self._actual_vs_forecast_merge(
            lookback, warehouse_codes, route_codes, category_codes, item_codes,
        )
        if ctx is None:
            return {"available": False, "message": "sales_recent.csv not found or no rows in scope"}

        sales = ctx["sales"]
        merged = ctx["merged"]
        forecast = ctx["forecast"]
        anchor = ctx["anchor"]
        working_days = ctx["working_days"]
        covered_routes = ctx["covered_routes"]
        covered_days = ctx["covered_days"]

        # ---- Tiles 1 / 2 / 3: business pulse (sales-only) --------------
        total_revenue = float(sales["revenue"].sum())
        total_qty = float(sales["TotalQuantity"].sum())
        transactions = int(len(sales))
        items_sold = int(sales["ItemCode"].nunique())
        avg_unique_per_day = (
            float(sales.groupby("TrxDate")["ItemCode"].nunique().mean())
            if working_days else 0.0
        )
        # Averages from sums collapse to total / day count -- no extra groupby.
        daily_avg_revenue = total_revenue / working_days if working_days else None
        daily_avg_units = total_qty / working_days if working_days else None
        daily_avg_txns = transactions / working_days if working_days else None

        # ---- Tile 4 + coverage (from the merged forecast frame) --------
        if forecast.empty:
            lost_amount = 0.0
            lost_units = 0.0
            items_lost = 0
            avg_daily_coverage_pct: Optional[float] = None
            daily_avg_lost: Optional[float] = None
        else:
            lost_qty_series = (merged["predicted"] - merged["actual_qty"]).clip(lower=0)
            lost_amount = float((lost_qty_series * merged["price"]).sum())
            lost_units = float(lost_qty_series.sum())
            # Distinct SKUs that contributed any lost units -- not row count.
            items_lost = int(merged.loc[lost_qty_series > 0, "ItemCode"].nunique())

            # Per-(day, route) coverage: of items each route actually sold
            # on each scored day, what fraction were on that day's
            # forecast for that route. Average across (day, route) cells.
            sold_items_by_day_route = (
                sales.groupby(["TrxDate", "RouteCode"])["ItemCode"]
                .apply(lambda s: set(s.unique()))
            )
            forecast_by_day_route = (
                forecast.groupby(["TrxDate", "RouteCode"])["ItemCode"]
                .apply(lambda s: set(s.unique()))
            )
            cell_ratios: List[float] = []
            for key, sold_items_cell in sold_items_by_day_route.items():
                if not sold_items_cell:
                    continue
                predicted_cell = forecast_by_day_route.get(key)
                if not predicted_cell:
                    continue
                cell_ratios.append(
                    len(sold_items_cell & predicted_cell) / len(sold_items_cell)
                )
            avg_daily_coverage_pct = (
                round(sum(cell_ratios) / len(cell_ratios) * 100, 1)
                if cell_ratios else None
            )
            daily_avg_lost = (
                lost_amount / covered_days if covered_days > 0 else None
            )

        return {
            "available": True,
            "lookback": lookback,
            "anchor_date": anchor.strftime("%Y-%m-%d"),
            # Denominator for tiles 1/2/3 averages (active sales dates).
            "working_days": working_days,
            # Denominator for tile 4's average (forecast-scored dates).
            "covered_routes": covered_routes,
            "covered_days": covered_days,
            "total_revenue": {
                "available": True,
                "amount": round(total_revenue, 2),
                "daily_avg": (
                    round(daily_avg_revenue, 2) if daily_avg_revenue is not None else None
                ),
            },
            "total_volume": {
                "available": True,
                "units": round(total_qty, 1),
                "transactions": transactions,
                "daily_avg_units": (
                    round(daily_avg_units, 1) if daily_avg_units is not None else None
                ),
                "daily_avg_transactions": (
                    round(daily_avg_txns, 1) if daily_avg_txns is not None else None
                ),
            },
            "unique_items": {
                "available": True,
                "count": items_sold,
                "daily_avg": round(avg_unique_per_day, 1),
                "avg_daily_coverage_pct": avg_daily_coverage_pct,
            },
            "lost_opportunity": {
                "available": True,
                "amount": round(lost_amount, 2),
                "units": round(lost_units, 1),
                "items_affected": items_lost,
                "daily_avg": (
                    round(daily_avg_lost, 2) if daily_avg_lost is not None else None
                ),
            },
        }

    # ------------------------------------------------------------------
    # Forecast rows -- per-(date, route, item) predicted vs actual rows
    # for the VanLoad "Past analysis" drawer. Same merge as business KPIs,
    # different projection (rows in, not aggregates), so the two surfaces
    # always agree on what falls inside a given filter scope.
    # ------------------------------------------------------------------

    def get_forecast_rows(
        self,
        lookback: Optional[str] = None,
        warehouse_codes: Optional[List[str]] = None,
        route_codes: Optional[List[str]] = None,
        category_codes: Optional[List[str]] = None,
        item_codes: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        canonical, _ = _resolve_lookback(lookback)
        w, r, c, i = warehouse_codes or [], route_codes or [], category_codes or [], item_codes or []
        key = f"forecast_rows::{canonical}::" + self._filter_key(w, r, c, i)
        return self._cached(key, lambda: self._compute_forecast_rows(canonical, w, r, c, i))

    def _compute_forecast_rows(
        self,
        lookback: str,
        warehouse_codes: List[str],
        route_codes: List[str],
        category_codes: List[str],
        item_codes: List[str],
    ) -> Dict[str, Any]:
        ctx = self._actual_vs_forecast_merge(
            lookback, warehouse_codes, route_codes, category_codes, item_codes,
        )
        if ctx is None:
            return {
                "available": False,
                "lookback": lookback,
                "message": "no rows in scope for this period",
            }

        merged = ctx["merged"]
        # Item names: merge already carries ItemName for rows where a sale
        # exists; fill gaps from the global catalog so forecast-only rows
        # also show a friendly label.
        if "ItemName" not in merged.columns:
            merged["ItemName"] = ""
        missing_name = merged["ItemName"].isna() | (merged["ItemName"] == "")
        if missing_name.any():
            catalog = self.get_item_catalog()
            name_map = {it["ItemCode"]: it.get("ItemName", "") for it in catalog.get("items", [])}
            merged.loc[missing_name, "ItemName"] = (
                merged.loc[missing_name, "ItemCode"].map(name_map).fillna("")
            )

        rows = [
            {
                "trx_date": r.TrxDate.strftime("%Y-%m-%d"),
                "route_code": str(r.RouteCode),
                "item_code": str(r.ItemCode),
                "item_name": str(r.ItemName) if isinstance(r.ItemName, str) else "",
                "predicted": round(float(r.predicted), 2),
                "actual_qty": round(float(r.actual_qty), 2),
                "price": round(float(r.price), 4),
            }
            for r in merged.itertuples(index=False)
        ]

        return {
            "available": True,
            "lookback": lookback,
            "anchor_date": ctx["anchor"].strftime("%Y-%m-%d"),
            "working_days": ctx["working_days"],
            "covered_routes": ctx["covered_routes"],
            "covered_days": ctx["covered_days"],
            "rows": rows,
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
                self._cache.move_to_end(key)
                return entry[1]
        value = loader()
        with self._lock:
            self._cache[key] = (now, value)
            self._cache.move_to_end(key)
            while len(self._cache) > self._MAX_CACHE_ENTRIES:
                self._cache.popitem(last=False)
        return value

