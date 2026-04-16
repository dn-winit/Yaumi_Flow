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
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd

from recommended_order.config.settings import Settings, get_settings

logger = logging.getLogger(__name__)


class DataManager:
    """Loads data from databases and caches in memory for fast access."""

    def __init__(self, settings: Optional[Settings] = None) -> None:
        self._settings = settings or get_settings()

        self._demand_df: Optional[pd.DataFrame] = None
        self._customer_df: Optional[pd.DataFrame] = None
        self._journey_df: Optional[pd.DataFrame] = None
        self._last_refresh: Optional[datetime] = None
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

    def refresh_incremental(self) -> Dict[str, Any]:
        """Re-read shared CSVs. Incremental semantics are owned by data_import;
        here we just reload what's already on disk."""
        return self._load_from_shared_csvs()

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

    def get_demand_data(self, route_code: Optional[str] = None) -> pd.DataFrame:
        df = self._demand_df
        if df is None:
            return pd.DataFrame()
        if route_code:
            return df[df["RouteCode"] == str(route_code)]
        return df

    def get_customer_data(self, route_code: Optional[str] = None) -> pd.DataFrame:
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
        """Return {ItemCode: predicted_quantity} for a route on a date."""
        demand = self.get_demand_data(route_code)
        if demand.empty:
            return {}

        target_dt = pd.to_datetime(target_date).normalize()

        # Prefer forecast-split data for future dates
        date_mask = demand["TrxDate"].dt.normalize() == target_dt
        forecast = demand[date_mask & (demand["DataSplit"] == "Forecast")]
        if forecast.empty:
            forecast = demand[date_mask]

        if forecast.empty:
            return {}

        van = (
            forecast.groupby("ItemCode")["Predicted"]
            .sum()
            .clip(lower=0)
            .round()
            .astype(int)
            .to_dict()
        )
        return {k: v for k, v in van.items() if v > 0}

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
