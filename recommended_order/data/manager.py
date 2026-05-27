"""Data manager -- in-memory cache over shared CSVs produced by data_import.
RO never hits the source DB; the only DB write is the rec push via db_pusher.
"""

from __future__ import annotations

import json
import logging
import math
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

from recommended_order.config.settings import Settings, get_settings

logger = logging.getLogger(__name__)


# Wire columns from the canonical rename map so a producer-side schema
# change propagates here automatically (loud KeyError vs silent NaN).
from demand_forecasting_pipeline.services.storage.file_storage import (
    SALES_TRANSACTIONS_RENAME as _DF_RENAME,
)

_SX_WIRE: Dict[str, str] = {v: k for k, v in _DF_RENAME.items()}  # snake -> Pascal

_SX_COL_TRX_DATE      = _SX_WIRE["trx_date"]
_SX_COL_ROUTE_CODE    = _SX_WIRE["route_code"]
_SX_COL_ITEM_CODE     = _SX_WIRE["item_code"]
_SX_COL_OPENING_STOCK = _SX_WIRE["opening_stock"]
_SX_COL_FRESH_LOAD    = _SX_WIRE["fresh_load"]
# Yesterday's leftover = today's opening (see enrich.py:611-617); Tier-2
# fallback uses this when today's row hasn't landed in sales_transactions yet.
_SX_COL_LEFTOVER_NEXT = _SX_WIRE["leftover_to_next_day"]
_SX_REQUIRED_COLS = frozenset({
    _SX_COL_TRX_DATE, _SX_COL_ROUTE_CODE, _SX_COL_ITEM_CODE,
    _SX_COL_OPENING_STOCK, _SX_COL_FRESH_LOAD,
    _SX_COL_LEFTOVER_NEXT,
})


class DataManager:
    """Loads + caches shared CSVs; auto-reloads on mtime change."""

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
        # Atomic .tmp + os.replace so concurrent refreshes can't leave a
        # half-written JSON for the reader.
        try:
            self._meta_path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = self._meta_path.with_suffix(self._meta_path.suffix + ".tmp")
            tmp_path.write_text(json.dumps(meta, indent=2, default=str))
            os.replace(tmp_path, self._meta_path)
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
        """Auto-reload if any source CSV's mtime changed (stat-only O(1))."""
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

                # Drop return rows (TotalQuantity<=0) so downstream consumers
                # see a net-of-returns view of customer history.
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

        # UTC-aware so /health's last_refresh carries an explicit offset.
        now = datetime.now(timezone.utc)
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

    # Getters take ``self._lock`` only long enough to snapshot the frame
    # reference (refresh rebinds new frames; never mutates in place).
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
        """Return ``{ItemCode: van_load_quantity}`` for one (route, date),
        where van_load = opening_stock + fresh_load (full physical capacity).

        Tier-1 reads past+today from sales_transactions.csv (cron-written
        carry chain). Tier-2 inline-enriches the forecast slice for future
        dates (no carry chain exists there); when today's sx row is missing,
        a walk-back carry seed restores yesterday's leftover.
        """
        demand = self.get_demand_data(route_code)
        if demand.empty:
            return {}

        target_dt = pd.to_datetime(target_date).normalize()
        same_day = demand[demand["TrxDate"].dt.normalize() == target_dt]
        if same_day.empty:
            return {}

        # Tier 1: past + today -- sales_transactions.csv is canonical.
        sx_idx = self._sales_transactions_index()
        if sx_idx:
            key_date = pd.Timestamp(target_dt)
            # Restrict to items the forecast surfaces today; stale leftovers
            # on the van shouldn't be recommended for sale.
            items_today = same_day["ItemCode"].astype(str).unique()
            route_key = str(route_code)
            out: Dict[str, int] = {}
            hits = 0
            for item in items_today:
                row = sx_idx.get((route_key, str(item), key_date))
                if row is None:
                    continue
                hits += 1
                fresh, opening, _leftover_next = row
                total = max(0.0, float(fresh) + float(opening))
                if total > 0:
                    # Ceil so forecast (float) and rec (int) align (35.28 -> 36).
                    out[str(item)] = int(math.ceil(total))
            if hits > 0:
                return out
            # Future request: index has no row -> fall through to Tier 2.

        # Tier 2: inline enrich + carry seed. Single-day slices open at 0
        # by construction; the seed walks back to recover yesterday's
        # leftover when today's sx row is still missing.
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
        # enrich_with_load emits ``recommended_load`` + ``opening_stock``;
        # fall back to Predicted if the helper degraded to a no-op.
        load_col = "recommended_load" if "recommended_load" in enriched.columns else "Predicted"
        has_opening_col = "opening_stock" in enriched.columns
        # Weekend-aware carry seed via shared walk-back (also used by the
        # page-view override) so semantics stay aligned across consumers.
        from common.carry_lookup import lookup_prior_leftover
        route_key = str(route_code)
        target_ts = pd.Timestamp(target_dt)
        lookback_days = int(getattr(self._settings, "carry_chain_lookback_days", 14))
        out: Dict[str, int] = {}
        for r in enriched.itertuples(index=False):
            # item_key defined per-row regardless of walk-back firing
            # (test_tier2_multi_item_no_unbound_or_key_leak locks this in).
            item_key = str(r.ItemCode)
            fresh = float(getattr(r, load_col) or 0.0)
            opening = float(getattr(r, "opening_stock", 0.0) or 0.0) if has_opening_col else 0.0
            if opening == 0.0 and sx_idx:
                # leftover_pos=2 -> tuple shape (fresh, opening, leftover_next).
                seeded, _src = lookup_prior_leftover(
                    sx_idx, route_key, item_key, target_ts,
                    lookback_days=lookback_days, leftover_pos=2,
                )
                opening = seeded
            total = max(0.0, fresh + opening)
            if total > 0:
                # Ceil to keep integer surfaces consistent (a van holds 31, not 30.7).
                out[item_key] = int(math.ceil(total))
        return out

    # Sales-transactions index (mtime-cached) so every get_van_items()
    # call is O(1) lookups against yf_sales_transactions's CSV mirror.

    _SX_LOCK = threading.Lock()
    _SX_INDEX: Optional[Dict[Tuple[str, str, pd.Timestamp], Tuple[float, float, float]]] = None
    _SX_MTIME: Optional[float] = None
    _SX_SIZE: Optional[int] = None

    def _sales_transactions_index(
        self,
    ) -> Dict[Tuple[str, str, pd.Timestamp], Tuple[float, float, float]]:
        """Per-(route, item, date) -> (fresh_load, opening_stock, leftover_next) lookup.

        Returns an empty dict if the mirror CSV is missing / empty /
        unparseable -- callers must treat that as "fall through to the
        inline enrich path", not as "no van today".

        ``leftover_next`` is yesterday-aware: callers that miss today's
        row (Tier-2 fallback) read ``idx[(route, item, target - 1d)][2]``
        to seed today's ``opening_stock`` from the prior day's carry,
        instead of silently dropping it to zero.
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
        # Schema guard: missing required columns -> log + degrade to inline enrich.
        missing = _SX_REQUIRED_COLS - set(df.columns)
        if missing:
            logger.warning(
                "sales_transactions mirror missing columns %s; falling back to inline enrich",
                sorted(missing),
            )
            return {}
        if df.empty:
            empty: Dict[Tuple[str, str, pd.Timestamp], Tuple[float, float, float]] = {}
            with self._SX_LOCK:
                DataManager._SX_INDEX = empty
                DataManager._SX_MTIME = stat.st_mtime
                DataManager._SX_SIZE = stat.st_size
            return empty
        # Normalise join keys exactly like the demand frame.
        df[_SX_COL_ROUTE_CODE] = df[_SX_COL_ROUTE_CODE].astype(str).str.strip()
        df[_SX_COL_ITEM_CODE]  = df[_SX_COL_ITEM_CODE].astype(str).str.strip()
        df[_SX_COL_TRX_DATE]   = pd.to_datetime(df[_SX_COL_TRX_DATE], errors="coerce").dt.normalize()
        df = df.dropna(subset=[_SX_COL_TRX_DATE])
        df[_SX_COL_FRESH_LOAD]    = pd.to_numeric(df[_SX_COL_FRESH_LOAD],    errors="coerce").fillna(0.0)
        df[_SX_COL_OPENING_STOCK] = pd.to_numeric(df[_SX_COL_OPENING_STOCK], errors="coerce").fillna(0.0)
        df[_SX_COL_LEFTOVER_NEXT] = pd.to_numeric(df[_SX_COL_LEFTOVER_NEXT], errors="coerce").fillna(0.0)
        # Defensive dedup with max() so backfill dupes never shrink van qty.
        agg = (
            df.groupby(
                [_SX_COL_ROUTE_CODE, _SX_COL_ITEM_CODE, _SX_COL_TRX_DATE],
                sort=False,
            )
            .agg({
                _SX_COL_FRESH_LOAD:    "max",
                _SX_COL_OPENING_STOCK: "max",
                _SX_COL_LEFTOVER_NEXT: "max",
            })
        )
        idx: Dict[Tuple[str, str, pd.Timestamp], Tuple[float, float, float]] = {
            (str(r), str(i), pd.Timestamp(d)): (float(f), float(o), float(ln))
            for (r, i, d), (f, o, ln) in zip(
                agg.index,
                zip(
                    agg[_SX_COL_FRESH_LOAD].to_numpy(),
                    agg[_SX_COL_OPENING_STOCK].to_numpy(),
                    agg[_SX_COL_LEFTOVER_NEXT].to_numpy(),
                ),
            )
        }
        with self._SX_LOCK:
            DataManager._SX_INDEX = idx
            DataManager._SX_MTIME = stat.st_mtime
            DataManager._SX_SIZE = stat.st_size
        return idx

    # Bulk reconciliation -- thin wrapper over the canonical enrich helper.

    @staticmethod
    def reconcile_demand_frame(df: pd.DataFrame) -> pd.DataFrame:
        """Return a copy of ``df`` with a ``recommended_load`` column;
        engine-unavailable falls back to the clipped raw forecast."""
        if df is None or df.empty:
            return df
        from demand_forecasting_pipeline.services.reconciliation import enrich_with_load
        return enrich_with_load(df, output_col="recommended_load")

    def get_item_names(self, route_code: Optional[str] = None) -> Dict[str, str]:
        """Return {ItemCode: ItemName}; merges demand+customer frames and
        falls back to the global frames if route-scoped are empty."""
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

        if not names:  # route missing -- fall back to global frames
            # Snapshot under the lock so a concurrent reload can't swap mid-iteration.
            with self._lock:
                demand_snapshot = self._demand_df
                customer_snapshot = self._customer_df
            for source in (demand_snapshot, customer_snapshot):
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
        """Return {ItemCode: avg unit price}; zero/NaN prices dropped."""
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
        # Lock guards against /health observing a half-published write.
        with self._lock:
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
        """Return max date per dataset for /health."""
        # Snapshot all three frames under one lock to avoid mixed-vintage reads.
        with self._lock:
            journey_snapshot = self._journey_df
            customer_snapshot = self._customer_df
            demand_snapshot = self._demand_df
        j = self._max_date(journey_snapshot, "JourneyDate")
        c = self._max_date(customer_snapshot, "TrxDate")
        d = self._max_date(demand_snapshot, "TrxDate")
        return {
            "journey_max_date": None if j is None else str(j.date()),
            "customer_max_date": None if c is None else str(c.date()),
            "demand_max_date": None if d is None else str(d.date()),
        }

    def assert_fresh(self, target_date: str) -> None:
        """Raise ``RuntimeError`` if any input CSV is older than ``target_date``.

        Checks ALL three input streams: journey_plan (today's planned visits),
        customer_data (purchase history that feeds frequency/cycle), demand_forecast
        (model output for the target date). Previously only journey was checked --
        a stale customer history silently produced wrong recs the day after a
        partial import failure. Friday (UAE weekend) is allowed to have no
        journey_plan row.
        """
        target = pd.to_datetime(target_date).normalize()
        is_friday = target.weekday() == 4

        if not is_friday:
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

        # Customer history and demand forecast must cover up to AT LEAST the
        # day before target (customer history is real sales; demand_forecast
        # has a forward horizon so target itself should be present).
        prior_day = (target - pd.Timedelta(days=1)).normalize()
        c_max = self._max_date(self._customer_df, "TrxDate")
        if c_max is not None and c_max.normalize() < prior_day:
            raise RuntimeError(
                f"Stale customer data: max TrxDate={c_max.date()} < {prior_day.date()}. "
                f"Run data_import to refresh customer_data.csv."
            )
        d_max = self._max_date(self._demand_df, "TrxDate")
        if d_max is not None and d_max.normalize() < target:
            raise RuntimeError(
                f"Stale demand forecast: max TrxDate={d_max.date()} < target={target.date()}. "
                f"Run data_import / retrain to refresh demand_forecast.csv."
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
