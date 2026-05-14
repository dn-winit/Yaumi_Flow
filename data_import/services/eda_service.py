"""
EDA service -- aggregated overview of sales_recent.csv + customer overview from YaumiLive.
Cached aggregates (5-min TTL) so repeated dashboard hits stay fast.

Forecast-vs-actual surfaces (forecast-rows, business-kpis) substitute the
canonical reconciled van load for the raw model output, so every accuracy
metric the UI shows reflects "did we load the right amount" rather than
"did the abstract model predict correctly". Baseline / model-quality
metrics still read raw values from the Pipeline page (different question).
"""

from __future__ import annotations

import logging
import re
import threading
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd
import pyodbc

from data_import.config.settings import Settings, get_settings

logger = logging.getLogger(__name__)


def _envelope_available(value: Any) -> bool:
    """Cache-predicate for live cut-through endpoints. Returns True only
    when the envelope reports ``available: True`` so transient YaumiLive
    failures (timeouts, connection drops) don't get pinned in the LRU
    for the full TTL -- subsequent calls retry instead of replaying
    a stale error."""
    return bool(isinstance(value, dict) and value.get("available"))


# Reconciliation: imported once from the canonical helper. Single
# implementation of "forecast row -> reconciled van load" lives in
# demand_forecasting_pipeline/services/reconciliation/enrich.py and is
# shared by every consumer (this service, predictions endpoint, accuracy
# service, recommended_order). No local plumbing duplication.
try:
    from demand_forecasting_pipeline.services.reconciliation import enrich_with_load
except Exception as _exc:
    enrich_with_load = None  # type: ignore[assignment]
    logger.warning(
        "EdaService: canonical reconciliation helper unavailable (%s) -- "
        "accuracy surfaces will fall back to raw forecast values", _exc,
    )


_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _validate_period(start_date: str, end_date: str) -> tuple[pd.Timestamp, pd.Timestamp]:
    """Parse and validate a ``[start_date, end_date]`` reporting period.

    Both bounds must be ISO ``YYYY-MM-DD`` and ``start_date <= end_date``.
    Raises ``ValueError`` on malformed or inverted ranges -- callers
    surface this through the existing ``available: False`` envelope so
    the frontend renders a clean empty-state instead of a crash.
    """
    if not _DATE_RE.match(start_date) or not _DATE_RE.match(end_date):
        raise ValueError(f"reporting_period requires ISO YYYY-MM-DD, got [{start_date}, {end_date}]")
    start = pd.Timestamp(start_date).normalize()
    end = pd.Timestamp(end_date).normalize()
    if start > end:
        raise ValueError(f"reporting_period inverted: start={start_date} > end={end_date}")
    return start, end


def _period_key(start_date: str, end_date: str) -> str:
    """Cache-key fragment for a reporting period. Validation lives in
    ``_validate_period``; this is purely a deterministic string used by
    the LRU cache to dedupe identical windows across callers."""
    return f"{start_date}::{end_date}"


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
        # Parsed-DataFrame memo, keyed by ``Path`` -> ``((path, mtime_ns, size), df)``.
        # Separate from the aggregation cache so the LRU eviction policy
        # never throws away the heavy parses to make room for cheap
        # tile responses.
        self._df_memo: Dict[Path, tuple[tuple[str, int, int], pd.DataFrame]] = {}

    # ------------------------------------------------------------------
    # Cache helpers
    # ------------------------------------------------------------------

    def _cached(
        self,
        key: str,
        loader,
        *,
        ttl: Optional[int] = None,
        cacheable=None,
    ) -> Any:
        """LRU + TTL cache. Pass ``ttl=`` to override the default 24h
        window (e.g. 60s for live cut-throughs that must stay fresh).
        Single helper -- no second copy to keep in sync.

        ``cacheable`` is an optional predicate run on the freshly-loaded
        value; only truthy results are stored. Live cut-throughs pass a
        predicate that rejects ``{available: False}`` envelopes so a
        transient YaumiLive timeout doesn't get pinned for the full TTL
        -- the supervision auto-reconciler walks the same key every 60s,
        and a cached failure used to silently swallow every visit
        arriving in that window."""
        ttl_eff = self._ttl if ttl is None else int(ttl)
        now = time.time()
        with self._lock:
            entry = self._cache.get(key)
            if entry and (now - entry[0]) < ttl_eff:
                self._cache.move_to_end(key)
                return entry[1]
        value = loader()
        if cacheable is None or cacheable(value):
            with self._lock:
                self._cache[key] = (now, value)
                self._cache.move_to_end(key)
                while len(self._cache) > self._MAX_CACHE_ENTRIES:
                    self._cache.popitem(last=False)
        return value

    def invalidate(self) -> None:
        with self._lock:
            self._cache.clear()
            self._df_memo.clear()

    # ------------------------------------------------------------------
    # File-mtime keyed DataFrame memo
    # ------------------------------------------------------------------
    #
    # Parsing the multi-MB CSVs on every dashboard request is the tile-load
    # hotspot. The memo holds the parsed DataFrame keyed on the file's
    # ``(mtime_ns, size)`` so the parse pays once per importer write and
    # subsequent reads are lookups. The importer's atomic ``os.replace``
    # bumps mtime, so a fresh CSV transparently invalidates the memo
    # without any explicit ``invalidate()`` call.

    def _load_df_memo(self, path: Path, loader) -> pd.DataFrame:
        """Caller has already confirmed ``path`` exists -- the memo trusts
        that check rather than repeating the syscall on every hit."""
        stat = path.stat()
        sig = (str(path), stat.st_mtime_ns, stat.st_size)
        with self._lock:
            cached = self._df_memo.get(path)
            if cached and cached[0] == sig:
                return cached[1]
        df = loader(path)
        with self._lock:
            self._df_memo[path] = (sig, df)
        return df

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
        item_codes: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Return the cascading filter options for the dashboard FilterBar.

        Each downstream dimension is the set of unique values present in the
        sales slice that already matches every upstream selection. Items are
        filtered by all three upstream levels so the deepest dropdown stays
        scoped tightly even when the user makes multiple picks above it.

        ``trimmed_selections`` is the same input vector with any codes that
        no longer exist in the cascaded option sets dropped. The frontend
        applies it verbatim, so a stale code never lingers and silently
        filters results to nothing.
        """
        item_codes = item_codes or []
        key = "filter_dims::" + self._filter_key(
            warehouse_codes, route_codes, category_codes, []
        )
        result = self._cached(key, lambda: self._compute_filter_dimensions(
            warehouse_codes, route_codes, category_codes,
        ))
        # Cached payload is shared across requests with different item
        # selections, so trim outside the cache and return a fresh dict.
        warehouses_set = {str(o["code"]) for o in result.get("warehouses") or []}
        routes_set = {str(o["code"]) for o in result.get("routes") or []}
        categories_set = {str(o["code"]) for o in result.get("categories") or []}
        items_set = {str(o["code"]) for o in result.get("items") or []}

        trimmed = {
            "warehouse_codes": [c for c in warehouse_codes if str(c) in warehouses_set],
            "route_codes":     [c for c in route_codes     if str(c) in routes_set],
            "category_codes":  [c for c in category_codes  if str(c) in categories_set],
            "item_codes":      [c for c in item_codes      if str(c) in items_set],
        }
        return {**result, "trimmed_selections": trimmed}

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
    # Last active date -- the most recent calendar day with sales activity
    # in sales_recent.csv. Drawers use this to seed defaults that always
    # land on a date the data actually covers (so the user never opens a
    # drawer onto an empty weekend). The value is purely a query against
    # the local CSV mirror; no calendar assumptions about which days are
    # working days, holidays, etc.
    # ------------------------------------------------------------------

    def get_last_active_date(self) -> Dict[str, Any]:
        """Most recent date in sales_recent.csv (route-agnostic).

        Cached for the full LRU TTL. The mtime-keyed DataFrame memo
        invalidates the underlying parse the moment the importer rewrites
        the CSV, so a fresh import (data_import cron) propagates without
        explicit cache busting here.
        """
        return self._cached("last_active_date", self._compute_last_active_date)

    def _compute_last_active_date(self) -> Dict[str, Any]:
        df = self._load_sales_df()
        if df.empty:
            return {"available": False, "date": None}
        return {
            "available": True,
            "date": df["TrxDate"].max().strftime("%Y-%m-%d"),
        }

    # ------------------------------------------------------------------
    # Sales overview (from local sales_recent.csv)
    # ------------------------------------------------------------------

    def get_sales_overview(
        self,
        start_date: str,
        end_date: str,
        warehouse_codes: Optional[List[str]] = None,
        route_codes: Optional[List[str]] = None,
        category_codes: Optional[List[str]] = None,
        item_codes: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        w, r, c, i = warehouse_codes or [], route_codes or [], category_codes or [], item_codes or []
        key = f"sales_overview::{_period_key(start_date, end_date)}::" + self._filter_key(w, r, c, i)
        return self._cached(key, lambda: self._compute_sales_overview(start_date, end_date, w, r, c, i))

    def _load_sales_df(self) -> pd.DataFrame:
        path = self._s.data_path(self._s.sales_recent_file)
        if not path.exists():
            logger.warning("Sales file not found: %s", path)
            return pd.DataFrame()
        return self._load_df_memo(path, self._parse_sales_csv)

    @staticmethod
    def _parse_sales_csv(path: Path) -> pd.DataFrame:
        df = pd.read_csv(path, low_memory=False)
        df["TrxDate"] = pd.to_datetime(df["TrxDate"], errors="coerce")
        df["TotalQuantity"] = pd.to_numeric(df["TotalQuantity"], errors="coerce").fillna(0)
        df["AvgUnitPrice"] = pd.to_numeric(df["AvgUnitPrice"], errors="coerce").fillna(0)
        df["revenue"] = df["TotalQuantity"] * df["AvgUnitPrice"]
        return df.dropna(subset=["TrxDate"])

    def _compute_sales_overview(
        self,
        start_date: str,
        end_date: str,
        warehouse_codes: Optional[List[str]] = None,
        route_codes: Optional[List[str]] = None,
        category_codes: Optional[List[str]] = None,
        item_codes: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Sales aggregates for the ``[start_date, end_date]`` window,
        optionally scoped to the dashboard FilterBar selection.

        The daily chart axis pads to every calendar day in the window so
        a weekend / holiday inside the range shows as a zero bar, not as
        a missing tick. Both leaderboards (top routes, categories) are
        sorted by REVENUE so the response order matches the card titles
        the dashboard shows -- no silent re-sort on the frontend.
        """
        try:
            start, end = _validate_period(start_date, end_date)
        except ValueError as exc:
            return {"available": False, "message": str(exc)}

        df = self._load_sales_df()
        if df.empty:
            return {
                "available": False,
                "message": "sales_recent.csv not found or empty",
                "start_date": start_date, "end_date": end_date,
            }

        ts = df["TrxDate"].dt.normalize()
        df = df[(ts >= start) & (ts <= end)]
        df = self._apply_sales_filters(
            df, warehouse_codes or [], route_codes or [], category_codes or [], item_codes or [],
        )
        empty_envelope = {
            "available": True, "start_date": start_date, "end_date": end_date,
            "totals": {}, "daily_trend": [], "top_routes": [], "categories": [],
        }
        if df.empty:
            return empty_envelope

        total_qty = float(df["TotalQuantity"].sum())
        total_rev = float(df["revenue"].sum())

        # Reindex daily series to every calendar day in [start, end] so
        # the chart axis renders contiguously regardless of scope. Days
        # with no activity show as zeros, not as gaps.
        full_dates = pd.date_range(start, end, freq="D").normalize()
        daily = (
            df.groupby(df["TrxDate"].dt.normalize())
            .agg(quantity=("TotalQuantity", "sum"), revenue=("revenue", "sum"))
            .reindex(full_dates, fill_value=0.0)
            .sort_index()
            .reset_index()
            .rename(columns={"index": "date"})
        )
        daily["date"] = daily["date"].dt.strftime("%Y-%m-%d")

        top_routes = (
            df.groupby("RouteCode", as_index=False)
            .agg(quantity=("TotalQuantity", "sum"), revenue=("revenue", "sum"), items=("ItemCode", "nunique"))
            .sort_values("revenue", ascending=False)
            .head(10)
        )
        top_routes["RouteCode"] = top_routes["RouteCode"].astype(str)

        categories = (
            df.groupby("CategoryName", as_index=False)
            .agg(quantity=("TotalQuantity", "sum"), revenue=("revenue", "sum"))
            .sort_values("revenue", ascending=False)
            .head(10)
        )
        categories["CategoryName"] = categories["CategoryName"].fillna("Uncategorized")

        return {
            "available": True,
            "start_date": start_date,
            "end_date":   end_date,
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
                # Count of distinct dates inside the window that had activity
                # (after scope filters). Lets the UI distinguish "30-day
                # window, 22 days had sales" from "30-day window, all days
                # active" without recomputing from the daily_trend array.
                "working_days": int(df["TrxDate"].dt.normalize().nunique()),
            },
            "daily_trend": daily.to_dict("records"),
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
        start_date: str,
        end_date: str,
        warehouse_codes: Optional[List[str]] = None,
        route_codes: Optional[List[str]] = None,
        category_codes: Optional[List[str]] = None,
        item_codes: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        # Validate up front -- the message contract here must match the
        # sister ``/eda/sales`` endpoint exactly so a bad input gives
        # the same explanation on every dashboard surface (inverted range
        # is "inverted", not the generic "no rows in scope" that the
        # downstream merge would otherwise emit).
        try:
            _validate_period(start_date, end_date)
        except ValueError as exc:
            return {
                "available": False,
                "message": str(exc),
                "start_date": start_date, "end_date": end_date,
            }
        w, r, c, i = warehouse_codes or [], route_codes or [], category_codes or [], item_codes or []
        key = f"business_kpis::{_period_key(start_date, end_date)}::" + self._filter_key(w, r, c, i)
        return self._cached(key, lambda: self._compute_business_kpis(start_date, end_date, w, r, c, i))

    # ------------------------------------------------------------------
    # Demand-forecast loader -- the van-load source. Predicted = what we
    # told the van to load; ActualQty is included for some rows but we
    # join against customer_data for fresher actuals + per-period prices.
    # ------------------------------------------------------------------

    # DemandClass + Lower/UpperBound flow through so the L4 quantile
    # layer of ``enrich_with_load`` activates for dashboard tiles too.
    # Without these the dashboard's reconciled values would silently
    # diverge from the cron's: same engine, different inputs, different
    # ``recommended_load`` for the same row.
    _FORECAST_COLUMNS = [
        "TrxDate", "RouteCode", "ItemCode", "DataSplit", "Predicted", "DemandClass",
        "LowerBound", "UpperBound",
    ]

    def _load_forecast_df(self) -> pd.DataFrame:
        path = self._s.data_path(self._s.demand_forecast_file)
        if not path.exists():
            logger.warning("Demand-forecast file not found: %s", path)
            return pd.DataFrame()
        return self._load_df_memo(path, self._parse_forecast_csv)

    @classmethod
    def _parse_forecast_csv(cls, path: Path) -> pd.DataFrame:
        # Pad missing columns so older snapshots without DemandClass /
        # bounds still load (dashboard degrades to V5_b cleanly).
        try:
            df = pd.read_csv(path, low_memory=False, usecols=cls._FORECAST_COLUMNS)
        except ValueError:
            df = pd.read_csv(path, low_memory=False)
            for col in cls._FORECAST_COLUMNS:
                if col not in df.columns:
                    df[col] = "" if col == "DemandClass" else 0
            df = df[cls._FORECAST_COLUMNS]
        df["TrxDate"] = pd.to_datetime(df["TrxDate"], errors="coerce")
        df["RouteCode"] = df["RouteCode"].astype(str).str.strip()
        df["ItemCode"] = df["ItemCode"].astype(str).str.strip()
        df["DemandClass"] = df["DemandClass"].fillna("").astype(str).str.strip().str.lower()
        df["Predicted"] = pd.to_numeric(df["Predicted"], errors="coerce").fillna(0)
        df["LowerBound"] = pd.to_numeric(df["LowerBound"], errors="coerce").fillna(0)
        df["UpperBound"] = pd.to_numeric(df["UpperBound"], errors="coerce").fillna(0)
        return df.dropna(subset=["TrxDate"])

    # ------------------------------------------------------------------
    # Shared (sales ⋈ forecast) merge -- consumed by both /eda/business-kpis
    # and /eda/forecast-rows. Single compute path so the two endpoints can
    # never disagree on what "this scope, this period" means.
    # ------------------------------------------------------------------

    def _actual_vs_forecast_merge(
        self,
        start_date: str,
        end_date: str,
        warehouse_codes: List[str],
        route_codes: List[str],
        category_codes: List[str],
        item_codes: List[str],
    ) -> Optional[Dict[str, Any]]:
        """Cached entry to the shared (sales ⋈ forecast) merge.

        ``business-kpis`` and ``forecast-rows`` both call this; they used
        to recompute the heavy join independently and only cached their
        own output. With the cache here, an identical FilterBar scope
        across the two endpoints (which is the common dashboard case)
        amortises the merge to a single compute.
        """
        key = f"av_merge::{_period_key(start_date, end_date)}::" + self._filter_key(
            warehouse_codes, route_codes, category_codes, item_codes,
        )
        return self._cached(
            key,
            lambda: self._compute_actual_vs_forecast_merge(
                start_date, end_date, warehouse_codes, route_codes, category_codes, item_codes,
            ),
        )

    def _compute_actual_vs_forecast_merge(
        self,
        start_date: str,
        end_date: str,
        warehouse_codes: List[str],
        route_codes: List[str],
        category_codes: List[str],
        item_codes: List[str],
    ) -> Optional[Dict[str, Any]]:
        """Build the per-(date, route, item) merge of past forecasts vs
        actual sales over ``[start_date, end_date]``. Returns None when
        there is nothing in scope; otherwise a dict with:

            sales         -- filtered + date-restricted sales rows
            merged        -- forecast OUTER-JOIN sold, with price fallback
            anchor        -- max active date inside the window
            working_days  -- count of active dates inside the window
            covered_routes / covered_days -- forecast scope counters
        """
        try:
            start, end = _validate_period(start_date, end_date)
        except ValueError:
            return None

        sales = self._apply_sales_filters(
            self._load_sales_df(),
            warehouse_codes, route_codes, category_codes, item_codes,
        )
        if sales.empty:
            return None

        ts = sales["TrxDate"].dt.normalize()
        sales = sales[(ts >= start) & (ts <= end)].copy()
        if sales.empty:
            return None
        active_set = set(sales["TrxDate"].dt.normalize().unique())
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
            # Past-window comparison surfaces a real predicted-vs-actual
            # for each historical date. Both splits qualify, both are
            # real model predictions:
            #   * Test          -- the model's clean held-out backtest
            #                      slice from the most recent training
            #                      run (predictions on rows the model
            #                      never saw during fitting).
            #   * Forecast      -- production forward-horizon rows that
            #                      data_import has accumulated locally
            #                      across past inference runs; for any
            #                      date now in the past, that row was
            #                      a real ahead-of-time prediction made
            #                      before the day arrived.
            # The forward horizon (today + forecast_horizon days) is
            # naturally excluded: ``active_set`` only contains dates that
            # have actual sales, so future-dated Forecast rows can never
            # match. When the same (date, route, item) appears in both
            # splits, Test wins -- cleaner provenance for the metric.
            forecast = forecast[forecast["Predicted"] > 0]
            forecast = forecast[forecast["TrxDate"].dt.normalize().isin(active_set)]
            forecast = forecast[forecast["RouteCode"].isin(scope_routes)]
            forecast = forecast[forecast["ItemCode"].isin(scope_items)]
            forecast["_split_priority"] = (
                forecast["DataSplit"].map({"Test": 0, "Forecast": 1}).fillna(2)
            )
            forecast = (
                forecast.sort_values("_split_priority")
                .drop_duplicates(["TrxDate", "RouteCode", "ItemCode"], keep="first")
                .drop(columns="_split_priority")
            )

        if not forecast.empty:
            forecast["TrxDate"] = forecast["TrxDate"].dt.normalize()
            # Preserve LowerBound/UpperBound through the groupby so
            # ``enrich_with_load`` keeps the L4 quantile layer active --
            # otherwise the dashboard's reconciled van load silently
            # diverges from the cron's for the same logical cell.
            agg_kwargs = {
                "predicted":    ("Predicted",   "sum"),
                "demand_class": ("DemandClass", "first"),
            }
            if "LowerBound" in forecast.columns:
                agg_kwargs["q_low"]  = ("LowerBound", "max")
            if "UpperBound" in forecast.columns:
                agg_kwargs["q_high"] = ("UpperBound", "max")
            forecast = (
                forecast.groupby(["TrxDate", "RouteCode", "ItemCode"], as_index=False)
                .agg(**agg_kwargs)
            )
            covered_cells = forecast[["RouteCode", "TrxDate"]].drop_duplicates()
            covered_routes = int(covered_cells["RouteCode"].nunique())
            covered_days = int(covered_cells["TrxDate"].nunique())
        else:
            forecast = pd.DataFrame(
                columns=["TrxDate", "RouteCode", "ItemCode", "predicted", "demand_class"]
            )
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
        if "demand_class" in merged.columns:
            merged["demand_class"] = merged["demand_class"].fillna("").astype(str).str.lower()
        else:
            merged["demand_class"] = ""

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
            "working_days": len(active_set),
            "covered_routes": covered_routes,
            "covered_days": covered_days,
        }

    def _compute_business_kpis(
        self,
        start_date: str,
        end_date: str,
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
                                  (Sigma max(0, predicted - actual) x price)

        All four are derived from the shared (sales merge forecast) helper --
        the same helper that powers the Past-analysis drawer, so the
        numbers can never drift between the two surfaces.
        """
        ctx = self._actual_vs_forecast_merge(
            start_date, end_date, warehouse_codes, route_codes, category_codes, item_codes,
        )
        if ctx is None:
            return {
                "available": False,
                "message": "sales_recent.csv not found or no rows in scope",
                "start_date": start_date, "end_date": end_date,
            }

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
            # Lost-opportunity uses the reconciled van load -- we want to
            # know how much of the recommendation didn't sell, not how
            # much the raw model over-shot. Falls back silently to the
            # raw ``predicted`` when the engine is unavailable.
            if enrich_with_load is not None:
                merged = enrich_with_load(merged, predicted_col="predicted")
            load_col = (
                "recommended_load" if "recommended_load" in merged.columns
                else "predicted"
            )
            lost_qty_series = (merged[load_col] - merged["actual_qty"]).clip(lower=0)
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
            "start_date": start_date,
            "end_date":   end_date,
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
        return self._cached(
            key,
            lambda: self._fetch_live_customer_sales(route_code, date, customer_code),
            ttl=self._s.live_cache_ttl_seconds,
            cacheable=_envelope_available,
        )

    def get_live_route_sales(self, route_code: str, date: str) -> Dict[str, Any]:
        """Return every ``(customer_code, customer_name, item_code, qty)`` sold on
        the given route/date. Live query against YaumiLive, 60-s cached.

        Same filter chain as :meth:`get_live_customer_sales` to guarantee the
        two endpoints never disagree on totals."""
        key = f"live_route_sales::{route_code}::{date}"
        return self._cached(
            key,
            lambda: self._fetch_live_route_sales(route_code, date),
            ttl=self._s.live_cache_ttl_seconds,
            cacheable=_envelope_available,
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
            WHERE ItemType = ?
              AND TrxType  = ?
              AND RouteCode = ?
              AND CAST(TrxDate AS DATE) = ?
            GROUP BY CustomerCode, ItemCode
        """
        conn = None
        try:
            conn = pyodbc.connect(
                self._s.db.connection_string(live=True), autocommit=False,
                timeout=self._s.db.live_connection_timeout,
            )
            conn.timeout = self._s.db.live_query_timeout
            cursor = conn.cursor()
            cursor.execute(sql, (
                self._s.sales_item_type, self._s.sales_invoice_trx_type,
                str(route_code), date,
            ))
            rows = cursor.fetchall()
        except Exception as exc:
            logger.error("Live route-sales query failed: %s", exc)
            return {"available": False, "message": str(exc), "customers": []}
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception as close_exc:
                    logger.warning("Live conn.close() failed: %s", close_exc)

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
            WHERE ItemType = ?
              AND TrxType  = ?
              AND RouteCode = ?
              AND CustomerCode = ?
              AND CAST(TrxDate AS DATE) = ?
            GROUP BY ItemCode
        """
        conn = None
        try:
            conn = pyodbc.connect(
                self._s.db.connection_string(live=True), autocommit=False,
                timeout=self._s.db.live_connection_timeout,
            )
            conn.timeout = self._s.db.live_query_timeout
            cursor = conn.cursor()
            cursor.execute(sql, (
                self._s.sales_item_type, self._s.sales_invoice_trx_type,
                str(route_code), str(customer_code), date,
            ))
            rows = cursor.fetchall()
        except Exception as exc:
            logger.error("Live customer-sales query failed: %s", exc)
            return {"available": False, "message": str(exc), "items": []}
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception as close_exc:
                    logger.warning("Live conn.close() failed: %s", close_exc)

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
    # ------------------------------------------------------------------
    # Live van composition for one (route, date) -- past leftover +
    # today's allocation + sales/returns, in a single 60-s-cached call.
    # The reconciliation layer in demand_forecasting consumes this to
    # surface "what's actually on the van right now".
    #
    # Identity guaranteed per item:
    #     van_load     = past_leftover + today_allocation
    #     leftover_now = max(0, van_load - sold - bad_return - good_return)
    # ------------------------------------------------------------------

    def get_live_van_composition(self, route_code: str, date: str) -> Dict[str, Any]:
        key = f"live_van_comp::{route_code}::{date}"
        return self._cached(
            key,
            lambda: self._fetch_live_van_composition(route_code, date),
            ttl=self._s.live_cache_ttl_seconds,
            cacheable=_envelope_available,
        )

    def _fetch_live_van_composition(self, route_code: str, date: str) -> Dict[str, Any]:
        if not (self._s.db.host and self._s.db.username):
            return {"available": False, "message": "DB not configured",
                    "items": [], "totals": {}}

        prior_sql = "DATEADD(day, -1, CAST(? AS DATE))"
        conn = None
        try:
            conn = pyodbc.connect(
                self._s.db.connection_string(live=True), autocommit=False,
                timeout=self._s.db.live_connection_timeout,
            )
            conn.timeout = self._s.db.live_query_timeout
            cur = conn.cursor()

            cur.execute(f"""
                SELECT ItemCode,
                       MAX(ItemName)    AS ItemName,
                       MAX(CategoryCode) AS CategoryCode,
                       MAX(CategoryName) AS CategoryName,
                       SUM(CAST(ClosingQty AS DECIMAL(18,2))) AS Qty
                FROM {self._s.closing_stock_view} WITH (NOLOCK)
                WHERE RouteCode = ? AND CAST(TrxDate AS DATE) = {prior_sql}
                GROUP BY ItemCode
            """, (str(route_code), date))
            past = cur.fetchall()

            cur.execute(f"""
                SELECT ItemCode,
                       MAX(ItemName)    AS ItemName,
                       MAX(CategoryCode) AS CategoryCode,
                       MAX(CategoryName) AS CategoryName,
                       SUM(CAST(AllocatedQuantityInPC AS DECIMAL(18,2))) AS Qty
                FROM {self._s.load_allocation_view} WITH (NOLOCK)
                WHERE RouteCode = ? AND CAST(MovementDate AS DATE) = ?
                GROUP BY ItemCode
            """, (str(route_code), date))
            alloc = cur.fetchall()

            cur.execute(f"""
                SELECT ItemCode,
                       MAX(ItemName)     AS ItemName,
                       MAX(CategoryCode) AS CategoryCode,
                       MAX(CategoryName) AS CategoryName,
                       SUM(CASE WHEN TrxType = ? AND QuantityInPCs > 0 THEN QuantityInPCs ELSE 0 END) AS SoldQty,
                       SUM(CASE WHEN TrxType = ? THEN -QuantityInPCs ELSE 0 END) AS BadReturnQty,
                       SUM(CASE WHEN TrxType = ? THEN -QuantityInPCs ELSE 0 END) AS GoodReturnQty
                FROM {self._s.sales_view} WITH (NOLOCK)
                WHERE RouteCode = ? AND ItemType = ? AND CAST(TrxDate AS DATE) = ?
                GROUP BY ItemCode
            """, (
                self._s.sales_invoice_trx_type,
                self._s.bad_return_trx_type,
                self._s.good_return_trx_type,
                str(route_code),
                self._s.sales_item_type,
                date,
            ))
            mov = cur.fetchall()

            cur.execute(f"""
                SELECT ItemCode, SUM(CAST(ClosingQty AS DECIMAL(18,2))) AS Qty
                FROM {self._s.closing_stock_view} WITH (NOLOCK)
                WHERE RouteCode = ? AND CAST(TrxDate AS DATE) = ?
                GROUP BY ItemCode
            """, (str(route_code), date))
            today_close = cur.fetchall()
        except Exception as exc:
            logger.error("Live van-composition query failed: %s", exc)
            return {"available": False, "message": str(exc),
                    "items": [], "totals": {}}
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception as close_exc:
                    logger.warning("Live conn.close() failed: %s", close_exc)

        items: Dict[str, Dict[str, Any]] = {}

        def _ensure(item_code, name="", cat="", cat_name=""):
            ic = str(item_code or "").strip()
            entry = items.setdefault(ic, {
                "item_code": ic, "item_name": "", "category_code": "", "category_name": "",
                "past_leftover": 0.0, "today_allocation": 0.0, "van_load": 0.0,
                "sold_qty": 0.0, "bad_return_qty": 0.0, "good_return_qty": 0.0,
                "leftover_now": 0.0, "end_closing": None,
            })
            if name and not entry["item_name"]:
                entry["item_name"] = str(name).strip()
            if cat and not entry["category_code"]:
                entry["category_code"] = str(cat).strip()
            if cat_name and not entry["category_name"]:
                entry["category_name"] = str(cat_name).strip()
            return entry

        for r in past:
            e = _ensure(r[0], r[1] or "", r[2] or "", r[3] or "")
            e["past_leftover"] = float(r[4] or 0.0)
        for r in alloc:
            e = _ensure(r[0], r[1] or "", r[2] or "", r[3] or "")
            e["today_allocation"] = float(r[4] or 0.0)
        for r in mov:
            e = _ensure(r[0], r[1] or "", r[2] or "", r[3] or "")
            e["sold_qty"]        = float(r[4] or 0.0)
            e["bad_return_qty"]  = float(r[5] or 0.0)
            e["good_return_qty"] = float(r[6] or 0.0)
        for r in today_close:
            ic = str(r[0] or "").strip()
            if ic in items:
                items[ic]["end_closing"] = float(r[1] or 0.0)

        for e in items.values():
            e["van_load"] = e["past_leftover"] + e["today_allocation"]
            consumed = e["sold_qty"] + e["bad_return_qty"] + e["good_return_qty"]
            e["leftover_now"] = max(0.0, e["van_load"] - consumed)

        items_list = [e for e in items.values()
                      if e["van_load"] > 0 or e["sold_qty"] > 0
                      or e["bad_return_qty"] > 0 or e["good_return_qty"] > 0]
        items_list.sort(key=lambda x: x["van_load"], reverse=True)

        # Per-item return quantities feed ``leftover_now`` per row but no
        # UI consumes the route-level return totals -- keep the per-row
        # numbers, drop the dead-payload aggregates.
        totals = {
            "items_count":            len(items_list),
            "past_leftover_total":    sum(e["past_leftover"]    for e in items_list),
            "today_allocation_total": sum(e["today_allocation"] for e in items_list),
            "van_load_total":         sum(e["van_load"]         for e in items_list),
            "sold_total":             sum(e["sold_qty"]         for e in items_list),
            "leftover_now_total":     sum(e["leftover_now"]     for e in items_list),
            "items_sold_out": sum(1 for e in items_list
                                  if e["van_load"] > 0 and e["leftover_now"] == 0),
        }

        return {
            "available": True,
            "route_code": str(route_code),
            "date": date,
            "items": items_list,
            "totals": totals,
            "fetched_at": pd.Timestamp.now().isoformat(),
        }


