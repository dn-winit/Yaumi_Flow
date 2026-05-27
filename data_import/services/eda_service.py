"""EDA service -- aggregated sales_recent.csv views + YaumiLive cut-throughs.

Forecast-vs-actual surfaces substitute the canonical reconciled van load
for the raw model output so accuracy metrics reflect "did we load the
right amount", not "did the abstract model predict correctly".
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

from common.db_pool import get_pool
from common.numeric import safe_float, safe_int
from data_import.config.settings import Settings, get_settings

logger = logging.getLogger(__name__)


def _envelope_available(value: Any) -> bool:
    """Cache-predicate: only ``available: True`` envelopes are cached so
    transient failures aren't pinned in the LRU for the full TTL."""
    return bool(isinstance(value, dict) and value.get("available"))


# Canonical "forecast row -> reconciled van load" lives in
# demand_forecasting_pipeline/services/reconciliation/enrich.py; shared
# across every consumer to avoid plumbing divergence.
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
    """Parse + validate a ``[start_date, end_date]`` ISO YYYY-MM-DD period;
    raises ``ValueError`` on malformed / inverted ranges."""
    if not _DATE_RE.match(start_date) or not _DATE_RE.match(end_date):
        raise ValueError(f"reporting_period requires ISO YYYY-MM-DD, got [{start_date}, {end_date}]")
    start = pd.Timestamp(start_date).normalize()
    end = pd.Timestamp(end_date).normalize()
    if start > end:
        raise ValueError(f"reporting_period inverted: start={start_date} > end={end_date}")
    return start, end


def _period_key(start_date: str, end_date: str) -> str:
    """Deterministic cache-key fragment for a reporting period."""
    return f"{start_date}::{end_date}"


class EdaService:
    """Aggregated EDA over sales_recent.csv + live YaumiLive cut-throughs."""

    # 24h TTL is safe because importer.scheduler invalidates on each pull.
    _MAX_CACHE_ENTRIES = 2000  # cap so per-item keys can't grow unbounded

    def __init__(self, settings: Optional[Settings] = None, ttl_seconds: int = 24 * 3600) -> None:
        self._s = settings or get_settings()
        self._ttl = ttl_seconds
        self._lock = threading.Lock()
        self._cache: OrderedDict[str, tuple[float, Any]] = OrderedDict()
        # Parsed-DataFrame memo kept separate from the aggregation cache so
        # LRU eviction can't throw away heavy parses for cheap tile responses.
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
        """LRU + TTL cache. ``ttl`` overrides default 24h (use 60s for live
        cut-throughs). ``cacheable`` predicate filters values before
        storing so transient failures aren't pinned (e.g. envelopes with
        ``available: False`` are skipped)."""
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

    # File-mtime keyed DataFrame memo: parse pays once per importer write,
    # subsequent reads are lookups (atomic os.replace bumps mtime so a
    # fresh CSV invalidates the memo without explicit busting).

    def _load_df_memo(self, path: Path, loader) -> pd.DataFrame:
        """Caller has already confirmed ``path`` exists."""
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

    # Shared dashboard-filter layer; identical semantics across every
    # surface. Empty list = no filter at that level. Category uses Name
    # because sales_recent only carries CategoryName.

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
        """Deterministic cache-key fragment; sorted so reorderings share a slot."""
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
        """Cascading filter options for the dashboard FilterBar; each
        downstream dimension is scoped by upstream selections.
        ``trimmed_selections`` drops codes absent from the cascaded sets so
        FE applies the cleaned vector verbatim."""
        item_codes = item_codes or []
        key = "filter_dims::" + self._filter_key(
            warehouse_codes, route_codes, category_codes, []
        )
        result = self._cached(key, lambda: self._compute_filter_dimensions(
            warehouse_codes, route_codes, category_codes,
        ))
        # Cached payload is shared across requests with different item
        # selections; trim outside the cache and return a fresh dict.
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

        # Warehouses: master set; selecting one doesn't shrink its dropdown.
        warehouses = (
            df[["WarehouseCode", "WarehouseName"]]
            .dropna()
            .drop_duplicates("WarehouseCode")
            .sort_values("WarehouseCode")
            .to_dict("records")
        )

        # Routes: scoped by warehouse; RouteCode doubles as name; each
        # carries its parent warehouse so the VanLoad grid can group without
        # a second call.
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

        # Categories: scoped by warehouse+route; name doubles as identifier.
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
        """Return {ItemCode: avg unit price} from customer_data.csv;
        single source of truth for item pricing, cached like other derivations."""
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

    # Last active date: most recent day with sales activity in
    # sales_recent.csv; drawers seed defaults from this to avoid empty-state.

    def get_last_active_date(self) -> Dict[str, Any]:
        """Most recent date in sales_recent.csv (route-agnostic)."""
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
        """Sales aggregates for ``[start_date, end_date]``, optionally
        FilterBar-scoped. Daily axis pads every calendar day (zero bars
        on holidays). Leaderboards sorted by revenue to match UI titles."""
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

        # Reindex daily to every day in [start, end] so the chart axis
        # is contiguous (zero bars on inactive days).
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
                # Distinct active dates (post-filter); lets UI separate
                # "22 active days" from "all 30 active" without recomputing.
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
        # Validate up front so the bad-input message matches /eda/sales
        # exactly across dashboard surfaces.
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

    # Demand-forecast loader; DemandClass + bounds flow through so
    # enrich_with_load's L4 quantile layer activates for dashboard tiles
    # too (else reconciled values silently diverge from the cron).
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
        # Pad missing columns so older snapshots still load.
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

    # Shared (sales join forecast) merge consumed by /eda/business-kpis
    # and /eda/forecast-rows so they can't disagree on scope/period.

    def _actual_vs_forecast_merge(
        self,
        start_date: str,
        end_date: str,
        warehouse_codes: List[str],
        route_codes: List[str],
        category_codes: List[str],
        item_codes: List[str],
    ) -> Optional[Dict[str, Any]]:
        """Cached entry to the shared (sales join forecast) merge; amortises
        the heavy join across business-kpis and forecast-rows for the common
        dashboard case of matching scopes."""
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
        """Per-(date, route, item) merge of past forecasts vs actual
        sales over ``[start_date, end_date]``; returns None if nothing in
        scope, else dict(sales, merged, forecast, anchor, working_days,
        covered_routes, covered_days)."""
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

        # Aggregate sales to (date, route, item); pull ItemName along
        # so drawer rows don't need a second lookup.
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
            # Both Test (clean held-out) and Forecast (accumulated past
            # forward-horizon rows) qualify as real ahead-of-time
            # predictions; Test wins on duplicate keys for cleaner provenance.
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
            # enrich_with_load's L4 quantile layer stays active.
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

        # Outer-join so both "forecast with no sale" (lost) and "sale with
        # no forecast" (uncovered demand) surface.
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
        """Four headline KPIs: total_revenue, total_volume, unique_items,
        lost_opportunity (Sigma max(0, predicted-actual) * price). All
        derive from the shared (sales join forecast) merge so dashboard
        and drawer can't drift."""
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
            # Lost-opportunity uses the reconciled van load (how much of
            # the recommendation didn't sell); falls back to raw predicted.
            if enrich_with_load is not None:
                merged = enrich_with_load(merged, predicted_col="predicted")
            load_col = (
                "recommended_load" if "recommended_load" in merged.columns
                else "predicted"
            )
            lost_qty_series = (merged[load_col] - merged["actual_qty"]).clip(lower=0)
            lost_amount = float((lost_qty_series * merged["price"]).sum())
            lost_units = float(lost_qty_series.sum())
            # Distinct SKUs contributing lost units (not row count).
            items_lost = int(merged.loc[lost_qty_series > 0, "ItemCode"].nunique())

            # Per-(day, route) coverage: fraction of sold items that were
            # on that day's forecast for that route; averaged across cells.
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
            # Tiles 1/2/3 denominator: active sales dates.
            "working_days": working_days,
            # Tile 4 denominator: forecast-scored dates.
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
        """Live items+qty for one (route, date, customer) from
        VW_GET_SALES_DETAILS (positive PCs, OrderItem, SalesInvoice).
        60s cached so rapid-fire visit clicks don't hammer the DB."""
        key = f"live_sales::{route_code}::{date}::{customer_code}"
        return self._cached(
            key,
            lambda: self._fetch_live_customer_sales(route_code, date, customer_code),
            ttl=self._s.live_cache_ttl_seconds,
            cacheable=_envelope_available,
        )

    def get_live_route_sales(self, route_code: str, date: str) -> Dict[str, Any]:
        """Live (customer, item, qty) tuples sold on (route, date); same
        filter chain as ``get_live_customer_sales`` to guarantee parity."""
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
        # Pool-bound live read: bypasses YaumiLive's per-connection session cap
        # when many dashboard tiles burst concurrently. Earlier raw pyodbc.connect
        # opened a fresh socket per request; under a 12-route UI fan-out that
        # surfaced as 08001 connect errors.
        try:
            pool = get_pool(
                self._s.db.connection_string(live=True),
                connect_timeout=int(self._s.db.live_connection_timeout),
                query_timeout=int(self._s.db.live_query_timeout),
                autocommit=True,
            )
            with pool.acquire() as conn:
                cursor = conn.cursor()
                cursor.execute(sql, (
                    self._s.sales_item_type, self._s.sales_invoice_trx_type,
                    str(route_code), date,
                ))
                rows = cursor.fetchall()
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
            qty = safe_int(r[3])
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
        try:
            pool = get_pool(
                self._s.db.connection_string(live=True),
                connect_timeout=int(self._s.db.live_connection_timeout),
                query_timeout=int(self._s.db.live_query_timeout),
                autocommit=True,
            )
            with pool.acquire() as conn:
                cursor = conn.cursor()
                cursor.execute(sql, (
                    self._s.sales_item_type, self._s.sales_invoice_trx_type,
                    str(route_code), str(customer_code), date,
                ))
                rows = cursor.fetchall()
        except Exception as exc:
            logger.error("Live customer-sales query failed: %s", exc)
            return {"available": False, "message": str(exc), "items": []}

        items = [
            {"item_code": str(r[0]).strip(), "qty": safe_int(r[1])}
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
    # Live van composition for one (route, date): past leftover +
    # today's allocation + sales/returns. 60s cached.
    # Identity per item:
    #   van_load     = past_leftover + today_allocation
    #   leftover_now = max(0, van_load - sold - bad_return - good_return)

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

        # Strict TrxDate = date-1 for past_leftover -- matches the cron's
        # conservative ``forward_fill_closing`` semantic (items off the
        # truck stay off). A wider lookback over-counted live vs cron.
        prior_sql = "DATEADD(day, -1, CAST(? AS DATE))"
        try:
            pool = get_pool(
                self._s.db.connection_string(live=True),
                connect_timeout=int(self._s.db.live_connection_timeout),
                query_timeout=int(self._s.db.live_query_timeout),
                autocommit=True,
            )
            with pool.acquire() as conn:
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
            e["past_leftover"] = safe_float(r[4])
        for r in alloc:
            e = _ensure(r[0], r[1] or "", r[2] or "", r[3] or "")
            e["today_allocation"] = safe_float(r[4])
        for r in mov:
            e = _ensure(r[0], r[1] or "", r[2] or "", r[3] or "")
            e["sold_qty"]        = safe_float(r[4])
            e["bad_return_qty"]  = safe_float(r[5])
            e["good_return_qty"] = safe_float(r[6])
        for r in today_close:
            ic = str(r[0] or "").strip()
            if ic in items:
                items[ic]["end_closing"] = safe_float(r[1])

        for e in items.values():
            e["van_load"] = e["past_leftover"] + e["today_allocation"]
            consumed = e["sold_qty"] + e["bad_return_qty"] + e["good_return_qty"]
            e["leftover_now"] = max(0.0, e["van_load"] - consumed)

        items_list = [e for e in items.values()
                      if e["van_load"] > 0 or e["sold_qty"] > 0
                      or e["bad_return_qty"] > 0 or e["good_return_qty"] > 0]
        items_list.sort(key=lambda x: x["van_load"], reverse=True)

        # Per-row returns feed leftover_now; route-level return totals
        # are unused so they're omitted from the payload.
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


