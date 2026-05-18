"""
Data manager -- single in-memory source for demand, customer, and journey data.

Single-source architecture: this service *never* hits the source DB for data.
All three datasets are read from shared CSVs under ``data/`` that are produced
by ``data_import``. The only DB interaction RO has is writing generated
recommendations back to ``yf_recommended_orders`` via ``db_pusher``.

Boot flow:
    1. Load all three CSVs into memory (takes seconds).
    2. Scheduler at 03:30 Dubai re-reads from CSVs after data_import's 03:00 job.
"""

from __future__ import annotations

import json
import logging
import math
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

from recommended_order.config.settings import Settings, get_settings

logger = logging.getLogger(__name__)


# Wire-column names on ``sales_transactions.csv`` (the DB mirror of
# ``yf_sales_transactions``) used by the van-load fast path. Derived
# from the canonical wire-schema map in
# ``demand_forecasting_pipeline.services.storage.file_storage.
# SALES_TRANSACTIONS_RENAME`` so a column rename on the producer side
# propagates here automatically (failing loudly via KeyError rather
# than silently turning into NaN). We expose only the five fields the
# loader actually touches; the PascalCase form is what the rest of
# recommended_order already uses for the demand frame.
from demand_forecasting_pipeline.services.storage.file_storage import (
    SALES_TRANSACTIONS_RENAME as _DF_RENAME,
)

_SX_WIRE: Dict[str, str] = {v: k for k, v in _DF_RENAME.items()}  # snake -> Pascal

_SX_COL_TRX_DATE      = _SX_WIRE["trx_date"]
_SX_COL_ROUTE_CODE    = _SX_WIRE["route_code"]
_SX_COL_ITEM_CODE     = _SX_WIRE["item_code"]
_SX_COL_OPENING_STOCK = _SX_WIRE["opening_stock"]
_SX_COL_FRESH_LOAD    = _SX_WIRE["fresh_load"]
_SX_REQUIRED_COLS = frozenset({
    _SX_COL_TRX_DATE, _SX_COL_ROUTE_CODE, _SX_COL_ITEM_CODE,
    _SX_COL_OPENING_STOCK, _SX_COL_FRESH_LOAD,
})


class DataManager:
    """Loads data from shared CSVs and caches in memory for fast access.

    Watches CSV modification times so it auto-reloads when data_import writes
    newer files — no manual refresh needed, no race conditions on startup.
    """

    def __init__(self, settings: Optional[Settings] = None) -> None:
        self._settings = settings or get_settings()

        self._demand_df: Optional[pd.DataFrame] = None
        self._customer_df: Optional[pd.DataFrame] = None
        self._journey_df: Optional[pd.DataFrame] = None
        self._last_refresh: Optional[datetime] = None
        self._csv_mtimes: Dict[str, float] = {}
        self._lock = threading.Lock()

        self._meta_path = Path(self._settings.shared_data_dir) / ".ro_manager_meta.json"

    def _write_meta(self, meta: Dict[str, Any]) -> None:
        try:
            self._meta_path.parent.mkdir(parents=True, exist_ok=True)
            self._meta_path.write_text(json.dumps(meta, indent=2, default=str))
        except Exception as exc:
            logger.warning("Failed to write manager meta: %s", exc)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def initialize(self) -> Dict[str, Any]:
        """Load all three datasets from the shared ``data/`` CSVs."""
        return self._load_from_shared_csvs()

    def refresh(self) -> Dict[str, Any]:
        """Re-read from shared CSVs (call after data_import has refreshed them)."""
        return self._load_from_shared_csvs()

    def ensure_fresh(self) -> None:
        """Auto-reload if any source CSV was modified since the last load.

        Called before every generation run so the engine always works with
        current data — even if data_import's startup import finished after
        this service booted. The mtime check is O(1) per file (stat call
        only, no read) so calling it on every request is cheap.
        """
        loaders = {
            "demand":   self._settings.demand_forecast_file,
            "customer": self._settings.customer_data_file,
            "journey":  self._settings.journey_plan_file,
        }
        for name, filename in loaders.items():
            path = Path(self._settings.shared_data_dir) / filename
            if not path.exists():
                continue
            current_mtime = path.stat().st_mtime
            if current_mtime != self._csv_mtimes.get(name, 0):
                logger.info("CSV changed on disk (%s) — auto-reloading all data", name)
                self._load_from_shared_csvs()
                return

    def _load_from_shared_csvs(self) -> Dict[str, Any]:
        """Load demand / customer / journey from shared ``data/`` CSVs."""
        errors: list[str] = []
        results: Dict[str, int] = {}

        loaders = {
            "demand":   (self._settings.demand_forecast_file, "demand_df"),
            "customer": (self._settings.customer_data_file,   "customer_df"),
            "journey":  (self._settings.journey_plan_file,    "journey_df"),
        }

        for name, (filename, attr) in loaders.items():
            path = Path(self._settings.shared_data_dir) / filename
            try:
                if not path.exists():
                    raise FileNotFoundError(
                        f"{name} CSV missing at {path}. Run data_import first (POST /import-all)."
                    )
                df = pd.read_csv(path, low_memory=False)
                self._normalize(df)

                # Demand-only: keep confident predictions (matches old DB filter)
                if name == "demand" and "DemandProbability" in df.columns:
                    threshold = self._settings.demand_probability_threshold
                    df = df[pd.to_numeric(df["DemandProbability"], errors="coerce") >= threshold]

                # Edge case (Sprint-3, Theme C.1): returns.
                # Customer history occasionally contains negative TotalQuantity
                # rows -- these are returns, not buys. Averaging them in drags
                # the recency-weighted mean down and can mask real demand. Drop
                # them at load so every downstream consumer (calibration,
                # priority, quantity, peer matrix) sees a net-of-returns view.
                if name == "customer" and "TotalQuantity" in df.columns:
                    qty = pd.to_numeric(df["TotalQuantity"], errors="coerce")
                    n_before = len(df)
                    df = df[qty > 0].copy()
                    dropped = n_before - len(df)
                    if dropped > 0:
                        logger.info(
                            "Filtered %d return rows (TotalQuantity<=0) from customer CSV",
                            dropped,
                        )

                with self._lock:
                    setattr(self, f"_{attr}", df)
                    self._csv_mtimes[name] = path.stat().st_mtime
                results[name] = len(df)
                logger.info("Loaded %s: %d rows from %s", name, len(df), path.name)
            except Exception as exc:
                errors.append(f"{name}: {exc}")
                logger.error("Failed to load %s: %s", name, exc)

        now = datetime.now()
        with self._lock:
            self._last_refresh = now

        if not errors:
            self._write_meta({
                "last_refresh": now.isoformat(),
                "rows": results,
                "source": "shared_csv",
            })

        return {"success": len(errors) == 0, "data": results, "errors": errors, "from_cache": False}

    # ------------------------------------------------------------------
    # Getters
    # ------------------------------------------------------------------

    # ``_load_from_shared_csvs`` swaps ``_demand_df`` / ``_customer_df``
    # / ``_journey_df`` references under ``self._lock``. The getters
    # take the SAME lock just long enough to snapshot the reference,
    # then release before slicing -- so a concurrent refresh cannot
    # leave a reader holding a half-swapped attribute. Once we own a
    # local reference, the underlying frame is immutable for the slice
    # (refresh produces NEW frames and rebinds, never mutates in place).
    def get_demand_data(self, route_code: Optional[str] = None) -> pd.DataFrame:
        with self._lock:
            df = self._demand_df
        if df is None:
            return pd.DataFrame()
        if route_code:
            return df[df["RouteCode"] == str(route_code)]
        return df

    def get_customer_data(self, route_code: Optional[str] = None) -> pd.DataFrame:
        with self._lock:
            df = self._customer_df
        if df is None:
            return pd.DataFrame()
        if route_code:
            return df[df["RouteCode"] == str(route_code)]
        return df

    def get_journey_plan(
        self,
        route_code: Optional[str] = None,
        date: Optional[str] = None,
    ) -> pd.DataFrame:
        with self._lock:
            df = self._journey_df
        if df is None:
            return pd.DataFrame()
        if route_code:
            df = df[df["RouteCode"] == str(route_code)]
        if date:
            date_col = "JourneyDate" if "JourneyDate" in df.columns else "TrxDate"
            if date_col in df.columns:
                target_dt = pd.to_datetime(date).normalize()
                df = df[df[date_col].dt.normalize() == target_dt]
        return df

    def get_van_items(self, route_code: str, target_date: str) -> Dict[str, int]:
        """Return ``{ItemCode: van_load_quantity}`` for one (route, date).

        ``van_load_quantity`` is the rep's **complete physical capacity
        for the day** = ``opening_stock`` (yesterday's closing + today's
        depot allocation) + ``fresh_load`` (the V5_b fresh-issuance the
        engine adds on top). Without ``opening_stock`` in the sum, an
        item already covered by leftover stock (``fresh_load == 0``)
        would be invisible to the recommendation engine -- the rep has
        physical units to sell but nothing would get recommended.

        Two-tier source resolution:

          1. **Past + today** -- read directly from ``sales_transactions.csv``
             which mirrors ``yf_sales_transactions`` (carry chain +
             reconciliation outputs written by the daily refresh cron).
             ``opening_stock`` there reflects the policy's accumulated
             leftover walked across the multi-day horizon, NOT a fresh
             day-1-zero recompute. Re-running ``enrich_with_load`` on a
             single-day slice here would reset the simulation to day 1
             and silently produce opening = 0 for every row, which would
             understate van capacity. The sales-transactions CSV stops
             at ``today`` by design (no actuals for the future).

          2. **Future horizon** -- ``sales_transactions.csv`` has no
             row for tomorrow+. Fall through to an inline enrich on the
             day's forecast slice. Day-1 of the slice starts at
             opening = 0 (the engine has no historical anchor in that
             single-day frame), so the resulting van quantity is
             effectively ``fresh_load`` only. This is the intended
             contract: no carry chain exists for future dates.

        The forecast frame is the single SOT for which (route, item)
        pairs exist on a given day -- the lookup keys the
        sales-transactions join. Forecast and Test splits are unioned
        upstream in ``van_load_view``; here we just take the demand
        slice for the requested date.
        """
        demand = self.get_demand_data(route_code)
        if demand.empty:
            return {}

        target_dt = pd.to_datetime(target_date).normalize()
        same_day = demand[demand["TrxDate"].dt.normalize() == target_dt]
        if same_day.empty:
            return {}

        # ---- Tier 1: past + today -- sales_transactions.csv is canonical ----
        sx_idx = self._sales_transactions_index()
        if sx_idx:
            key_date = pd.Timestamp(target_dt)
            # Restrict to items that the forecast actually surfaces for
            # this (route, date). Items present in sales_transactions but
            # absent from today's forecast are stale leftovers -- the
            # rep may still have them on the van, but they shouldn't be
            # recommended for sale today.
            items_today = same_day["ItemCode"].astype(str).unique()
            route_key = str(route_code)
            out: Dict[str, int] = {}
            hits = 0
            for item in items_today:
                row = sx_idx.get((route_key, str(item), key_date))
                if row is None:
                    continue
                hits += 1
                fresh, opening = row
                total = max(0.0, float(fresh) + float(opening))
                if total > 0:
                    # Ceil so a 35.28-unit truck capacity reads as 36,
                    # not 35. Matches the DB-canonical contract that
                    # quantity columns round to the next whole unit and
                    # the van_load_lower_bound / van_load_upper_bound
                    # rounding in the engine, so forecast (FLOAT) and
                    # rec (INT) surfaces align byte-for-byte.
                    out[str(item)] = int(math.ceil(total))
            if hits > 0:
                return out
            # Index loaded but had no rows for this (route, date) =>
            # the request is on the forecast horizon (future). Fall
            # through to the inline-enrich path which handles future
            # dates with opening = 0 by construction.

        # ---- Tier 2: future horizon -- inline enrich (no carry chain) ----
        agg_map = {"Predicted": "max"}
        for col in ("LowerBound", "UpperBound"):
            if col in same_day.columns:
                agg_map[col] = "max"
        if "DemandClass" in same_day.columns:
            agg_map["DemandClass"] = "first"
        per_item = (
            same_day.groupby("ItemCode", as_index=False)
            .agg(agg_map)
            .assign(RouteCode=str(route_code), TrxDate=target_dt)
        )
        enriched = self.reconcile_demand_frame(per_item)
        # ``enrich_with_load`` populates ``recommended_load`` (snake_case)
        # as the output column and ``opening_stock`` as a diagnostic. The
        # diagnostic is 0 for future dates by design (no historical
        # anchor in a single-day slice). Use ``Predicted`` if the engine
        # was unavailable and the helper degraded to the no-op path.
        load_col = "recommended_load" if "recommended_load" in enriched.columns else "Predicted"
        has_opening_col = "opening_stock" in enriched.columns
        out: Dict[str, int] = {}
        for r in enriched.itertuples(index=False):
            fresh = float(getattr(r, load_col) or 0.0)
            opening = float(getattr(r, "opening_stock", 0.0) or 0.0) if has_opening_col else 0.0
            total = max(0.0, fresh + opening)
            if total > 0:
                out[str(r.ItemCode)] = int(math.ceil(total))
        return out

    # ------------------------------------------------------------------
    # Sales-transactions index (mtime-cached)
    # ------------------------------------------------------------------
    #
    # The carry chain + reconciliation outputs moved from
    # ``yf_demand_forecast`` into ``yf_sales_transactions``. We mirror
    # the latter to ``data/imports/sales_transactions.csv`` via
    # data_import. This index is parsed once per file revision per
    # process so every ``get_van_items`` call is O(1) lookups.
    # ------------------------------------------------------------------

    _SX_LOCK = threading.Lock()
    _SX_INDEX: Optional[Dict[Tuple[str, str, pd.Timestamp], Tuple[float, float]]] = None
    _SX_MTIME: Optional[float] = None
    _SX_SIZE: Optional[int] = None

    def _sales_transactions_index(
        self,
    ) -> Dict[Tuple[str, str, pd.Timestamp], Tuple[float, float]]:
        """Per-(route, item, date) -> (fresh_load, opening_stock) lookup.

        Returns an empty dict if the mirror CSV is missing / empty /
        unparseable -- callers must treat that as "fall through to the
        inline enrich path", not as "no van today".
        """
        filename = getattr(self._settings, "sales_transactions_file", "sales_transactions.csv")
        path = Path(self._settings.shared_data_dir) / filename
        if not path.exists():
            return {}
        stat = path.stat()
        with self._SX_LOCK:
            if (
                self._SX_INDEX is not None
                and self._SX_MTIME == stat.st_mtime
                and self._SX_SIZE == stat.st_size
            ):
                return self._SX_INDEX
        try:
            df = pd.read_csv(
                path, low_memory=False,
                usecols=lambda c: c in _SX_REQUIRED_COLS,
            )
        except Exception as exc:
            logger.warning(
                "sales_transactions index build failed (%s); falling back to inline enrich",
                exc,
            )
            return {}
        # Schema guard: any required column missing means the mirror is
        # malformed or the canonical schema drifted -- log loudly and
        # degrade to inline enrich rather than silently mis-aligning.
        missing = _SX_REQUIRED_COLS - set(df.columns)
        if missing:
            logger.warning(
                "sales_transactions mirror missing columns %s; falling back to inline enrich",
                sorted(missing),
            )
            return {}
        if df.empty:
            empty: Dict[Tuple[str, str, pd.Timestamp], Tuple[float, float]] = {}
            with self._SX_LOCK:
                DataManager._SX_INDEX = empty
                DataManager._SX_MTIME = stat.st_mtime
                DataManager._SX_SIZE = stat.st_size
            return empty
        # Normalise join keys identical to the way ``_normalize`` treats
        # the demand frame so the (route, item, date) tuple lookup hits.
        df[_SX_COL_ROUTE_CODE] = df[_SX_COL_ROUTE_CODE].astype(str).str.strip()
        df[_SX_COL_ITEM_CODE]  = df[_SX_COL_ITEM_CODE].astype(str).str.strip()
        df[_SX_COL_TRX_DATE]   = pd.to_datetime(df[_SX_COL_TRX_DATE], errors="coerce").dt.normalize()
        df = df.dropna(subset=[_SX_COL_TRX_DATE])
        df[_SX_COL_FRESH_LOAD]    = pd.to_numeric(df[_SX_COL_FRESH_LOAD],    errors="coerce").fillna(0.0)
        df[_SX_COL_OPENING_STOCK] = pd.to_numeric(df[_SX_COL_OPENING_STOCK], errors="coerce").fillna(0.0)
        # Defensive dedup: the upstream cron emits one row per
        # (route, item, date); if a backfill ever produces dupes, we
        # take the max so a partial overwrite never loses the larger
        # van quantity. Matches the ``"max"`` agg the legacy fast path
        # used on the forecast frame.
        agg = (
            df.groupby(
                [_SX_COL_ROUTE_CODE, _SX_COL_ITEM_CODE, _SX_COL_TRX_DATE],
                sort=False,
            )
            .agg({_SX_COL_FRESH_LOAD: "max", _SX_COL_OPENING_STOCK: "max"})
        )
        idx: Dict[Tuple[str, str, pd.Timestamp], Tuple[float, float]] = {
            (str(r), str(i), pd.Timestamp(d)): (float(f), float(o))
            for (r, i, d), (f, o) in zip(
                agg.index,
                zip(
                    agg[_SX_COL_FRESH_LOAD].to_numpy(),
                    agg[_SX_COL_OPENING_STOCK].to_numpy(),
                ),
            )
        }
        with self._SX_LOCK:
            DataManager._SX_INDEX = idx
            DataManager._SX_MTIME = stat.st_mtime
            DataManager._SX_SIZE = stat.st_size
        return idx

    # ------------------------------------------------------------------
    # Bulk reconciliation -- thin wrapper around the single canonical
    # helper. Engine fallback, closing-stock caching, and the V5_b
    # formula all live there. This module just hands off the frame.
    # ------------------------------------------------------------------

    @staticmethod
    def reconcile_demand_frame(df: pd.DataFrame) -> pd.DataFrame:
        """Return a copy of ``df`` with a ``recommended_load`` column.

        ``df`` must carry ``RouteCode``, ``ItemCode``, ``TrxDate`` and
        ``Predicted``. The helper fills ``recommended_load`` with the
        clipped raw forecast if the engine can't load, so callers never
        need a separate fallback path. Output column name matches the
        snake_case convention ``enrich_with_load`` uses by default, which
        in turn matches the alias the artifact_service's van_load_view
        emits -- one shared name across the whole stack.
        """
        if df is None or df.empty:
            return df
        from demand_forecasting_pipeline.services.reconciliation import enrich_with_load
        return enrich_with_load(df, output_col="recommended_load")

    def get_item_names(self, route_code: Optional[str] = None) -> Dict[str, str]:
        """Return {ItemCode: ItemName} lookup.

        Merges the demand and customer frames so an item missing a name in one
        source is filled from the other. Route-scoped for locality; if the
        route-filtered frames are empty we fall back to the global frames so
        we still resolve names.
        """
        def _extract(df: pd.DataFrame) -> Dict[str, str]:
            if df.empty or "ItemName" not in df.columns:
                return {}
            sub = df[["ItemCode", "ItemName"]].dropna(subset=["ItemName"])
            sub = sub[sub["ItemName"].astype(str).str.strip() != ""]
            if sub.empty:
                return {}
            return sub.drop_duplicates("ItemCode").set_index("ItemCode")["ItemName"].to_dict()

        names: Dict[str, str] = {}
        for source in (self.get_demand_data(route_code), self.get_customer_data(route_code)):
            for code, name in _extract(source).items():
                names.setdefault(code, name)

        if not names:  # route may be missing -- fall back to global frames
            for source in (self._demand_df, self._customer_df):
                if source is None:
                    continue
                for code, name in _extract(source).items():
                    names.setdefault(code, name)

        return names

    def get_customer_names(self, route_code: Optional[str] = None) -> Dict[str, str]:
        """Return {CustomerCode: CustomerName} lookup."""
        df = self.get_customer_data(route_code)
        if df.empty:
            return {}
        return df.drop_duplicates("CustomerCode").set_index("CustomerCode")["CustomerName"].to_dict()

    def get_item_prices(self, route_code: Optional[str] = None) -> Dict[str, float]:
        """Return {ItemCode: avg unit price} from customer sales data.

        Averaged across all rows for each item so a single outlier invoice
        can't skew the lookup. Zero/NaN prices are dropped -- the caller
        should treat a missing key as "price unknown".
        """
        df = self.get_customer_data(route_code)
        if df.empty or "ItemCode" not in df.columns or "AvgUnitPrice" not in df.columns:
            return {}
        prices = (
            df[["ItemCode", "AvgUnitPrice"]]
            .dropna()
            .groupby("ItemCode", as_index=True)["AvgUnitPrice"]
            .mean()
        )
        return {str(k): float(v) for k, v in prices.items() if pd.notna(v) and float(v) > 0}

    def get_journey_customers(self, route_code: str, target_date: str) -> List[str]:
        """Return list of customer codes planned for a route on a date."""
        jp = self.get_journey_plan(route_code, target_date)
        if jp.empty:
            return []
        col = "CustomerCode" if "CustomerCode" in jp.columns else jp.columns[0]
        return jp[col].dropna().astype(str).unique().tolist()

    def get_route_codes(self) -> List[str]:
        return list(self._settings.route_codes)

    @property
    def last_refresh(self) -> Optional[datetime]:
        return self._last_refresh

    # ------------------------------------------------------------------
    # Freshness guard (Sprint-1)
    # ------------------------------------------------------------------

    def _max_date(self, df: Optional[pd.DataFrame], col: str) -> Optional[pd.Timestamp]:
        if df is None or df.empty or col not in df.columns:
            return None
        m = pd.to_datetime(df[col], errors="coerce").max()
        return None if pd.isna(m) else m

    def freshness(self) -> Dict[str, Optional[str]]:
        """Return max date per dataset, for /health and logging."""
        j = self._max_date(self._journey_df, "JourneyDate")
        c = self._max_date(self._customer_df, "TrxDate")
        d = self._max_date(self._demand_df, "TrxDate")
        return {
            "journey_max_date": None if j is None else str(j.date()),
            "customer_max_date": None if c is None else str(c.date()),
            "demand_max_date": None if d is None else str(d.date()),
        }

    def assert_fresh(self, target_date: str) -> None:
        """Raise ``RuntimeError`` if the journey plan is older than ``target_date``.

        Friday is the UAE weekend and legitimately has no journey; skip the
        check in that case.
        """
        target = pd.to_datetime(target_date).normalize()
        # UAE weekend -- no journey plan expected
        if target.weekday() == 4:  # Friday
            return
        j_max = self._max_date(self._journey_df, "JourneyDate")
        if j_max is None:
            raise RuntimeError(
                "Journey data is empty -- run data_import before generating."
            )
        if j_max.normalize() < target:
            raise RuntimeError(
                f"Stale journey data: max JourneyDate={j_max.date()} < target={target.date()}. "
                f"Run data_import to refresh CSVs."
            )

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    @staticmethod
    def _normalize(df: pd.DataFrame) -> None:
        """In-place normalisation of common columns."""
        for col in ("RouteCode", "CustomerCode", "ItemCode"):
            if col in df.columns:
                df[col] = df[col].astype(str).str.strip()
        for col in ("TrxDate", "JourneyDate"):
            if col in df.columns:
                df[col] = pd.to_datetime(df[col], errors="coerce")
