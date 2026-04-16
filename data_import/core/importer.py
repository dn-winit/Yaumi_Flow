"""
Data importer -- handles incremental and full-refresh loading for all datasets.

Incremental logic:
    1. Read existing CSV, find max date
    2. Fetch only rows after that date from DB
    3. Append to existing CSV (deduplicate)
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any, Dict, Optional

import pandas as pd

from data_import.config.settings import Settings, get_settings
from data_import.core.database import DatabaseClient
from data_import.core.queries import QueryBuilder

logger = logging.getLogger(__name__)


# Dataset registry: key -> (file_setting, date_column, query_method, db)
_DATASETS = {
    "customer_data":   ("customer_data_file",   "TrxDate",     "customer_data",   "live"),
    "journey_plan":    ("journey_plan_file",    "JourneyDate", "journey_plan",    "live"),
    "sales_recent":    ("sales_recent_file",    "TrxDate",     "sales_recent",    "live"),
    "demand_forecast": ("demand_forecast_file", "TrxDate",     "demand_forecast", "aiml"),
}


class DataImporter:
    """Fetches data from DB and saves to local CSV with incremental support."""

    _CONN_PROBE_TTL_SECONDS = 30  # avoid hitting the DB on every status/health poll

    def __init__(self, settings: Optional[Settings] = None) -> None:
        self._s = settings or get_settings()
        self._db = DatabaseClient(self._s)
        self._qb = QueryBuilder(self._s)
        self._conn_probe: tuple[float, bool] | None = None
        # dataset -> ((path, mtime_ns, size), status_entry)
        self._status_cache: Dict[str, tuple[tuple[str, int, int], Dict[str, Any]]] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def import_dataset(self, dataset: str, mode: str = "incremental") -> Dict[str, Any]:
        """
        Import a single dataset.

        Args:
            dataset: "customer_data", "journey_plan", or "sales_recent"
            mode: "incremental" (append new rows) or "full" (replace all)
        """
        if dataset not in _DATASETS:
            return {"success": False, "error": f"Unknown dataset: {dataset}. Use: {list(_DATASETS.keys())}"}

        file_attr, date_col, query_method, db = _DATASETS[dataset]
        file_path = self._s.data_path(getattr(self._s, file_attr))

        t0 = time.time()

        # Determine since_date for incremental
        since_date = None
        existing_rows = 0
        if mode == "incremental" and file_path.exists():
            since_date, existing_rows = self._detect_last_date(file_path, date_col)
            if since_date:
                logger.info("%s: incremental from %s (%d existing rows)", dataset, since_date, existing_rows)

        # Build and execute query
        query_fn = getattr(self._qb, query_method)
        if mode == "incremental" and since_date:
            sql, params = query_fn(since_date=since_date)
        else:
            sql, params = query_fn()

        try:
            new_df = self._db.execute_query(sql, tuple(params), db=db)
        except Exception as exc:
            return {"success": False, "error": str(exc), "dataset": dataset}

        if new_df.empty:
            return {
                "success": True,
                "dataset": dataset,
                "mode": mode,
                "new_rows": 0,
                "total_rows": existing_rows,
                "message": "No new data found",
                "duration_seconds": round(time.time() - t0, 2),
            }

        # Normalize date column
        if date_col in new_df.columns:
            new_df[date_col] = pd.to_datetime(new_df[date_col]).dt.strftime("%Y-%m-%d")

        # Merge with existing (incremental) or replace (full)
        if mode == "incremental" and file_path.exists() and since_date:
            existing_df = pd.read_csv(file_path, low_memory=False)
            combined = pd.concat([existing_df, new_df], ignore_index=True)
            # Deduplicate: keep last occurrence per all columns except quantity
            key_cols = [c for c in combined.columns if c not in ("TotalQuantity", "AvgUnitPrice")]
            combined = combined.drop_duplicates(subset=key_cols, keep="last")
            total_rows = len(combined)
        else:
            combined = new_df
            total_rows = len(combined)

        # Save
        Path(file_path).parent.mkdir(parents=True, exist_ok=True)
        combined.to_csv(file_path, index=False)

        duration = round(time.time() - t0, 2)
        logger.info(
            "%s: saved %d rows (%d new) in %.1fs -> %s",
            dataset, total_rows, len(new_df), duration, file_path,
        )

        return {
            "success": True,
            "dataset": dataset,
            "mode": mode,
            "new_rows": len(new_df),
            "total_rows": total_rows,
            "file": str(file_path),
            "duration_seconds": duration,
        }

    def import_all(self, mode: str = "incremental") -> Dict[str, Any]:
        """Import all datasets."""
        results = {}
        for dataset in _DATASETS:
            results[dataset] = self.import_dataset(dataset, mode)
        return results

    def status(self) -> Dict[str, Any]:
        """Return current state of all local data files.

        Per-dataset result is memoised keyed on (path, mtime, size). Only the
        files whose mtime changed get re-parsed -- so a stable status endpoint
        is O(1) instead of an O(N) date-column scan on every call.
        """
        info: Dict[str, Any] = {}
        cache = self._status_cache
        for dataset, (file_attr, date_col, _, _) in _DATASETS.items():
            file_path = self._s.data_path(getattr(self._s, file_attr))
            if not file_path.exists():
                info[dataset] = {"exists": False, "rows": 0, "last_date": None}
                continue

            stat = file_path.stat()
            key = (str(file_path), stat.st_mtime_ns, stat.st_size)
            cached = cache.get(dataset)
            if cached and cached[0] == key:
                info[dataset] = cached[1]
                continue

            df = pd.read_csv(file_path, usecols=[date_col], low_memory=False)
            rows = len(df)
            last = str(df[date_col].max()) if not df.empty else None
            first = str(df[date_col].min()) if not df.empty else None
            entry = {
                "exists": True,
                "rows": rows,
                "first_date": first,
                "last_date": last,
                "file": str(file_path),
                "size_mb": round(stat.st_size / 1024 / 1024, 2),
            }
            cache[dataset] = (key, entry)
            info[dataset] = entry
        return info

    def test_connection(self) -> bool:
        """Cached DB liveness probe -- avoids per-request DB connect on hot endpoints."""
        now = time.time()
        if self._conn_probe and (now - self._conn_probe[0]) < self._CONN_PROBE_TTL_SECONDS:
            return self._conn_probe[1]
        ok = self._db.test_connection()
        self._conn_probe = (now, ok)
        return ok

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    @staticmethod
    def _detect_last_date(file_path: Path, date_col: str) -> tuple[Optional[str], int]:
        """Read CSV and return (max_date_str, row_count). Returns (None, 0) if empty."""
        try:
            df = pd.read_csv(file_path, usecols=[date_col], low_memory=False)
            if df.empty:
                return None, 0
            max_date = pd.to_datetime(df[date_col]).max()
            return str(max_date.date()), len(df)
        except Exception as exc:
            logger.warning("Failed to read %s for incremental detection: %s", file_path, exc)
            return None, 0
