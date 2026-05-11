"""
Data importer -- handles incremental and full-refresh loading for all datasets.

Incremental logic:
    1. Read existing CSV, find max date
    2. Fetch only rows after that date from DB
    3. Append to existing CSV (deduplicate)
"""

from __future__ import annotations

import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, Optional

import pandas as pd

from data_import.config.settings import Settings, get_settings
from data_import.core.database import DatabaseClient
from data_import.core.queries import QueryBuilder

logger = logging.getLogger(__name__)


# Dataset registry: key -> (file_setting, date_column, query_method, db, dim_key)
# ``dim_key`` is the canonical natural-key column tuple used to dedup
# merge results -- exactly the columns the upstream DB MERGE keys on,
# nothing more. Without an explicit list, dedup defaults to "every
# non-numeric column", which silently breaks when a non-key column
# (e.g. ModelUsed, DemandClass) changes across training runs --
# duplicate rows then accumulate forever in the CSV mirror.
_DATASETS = {
    "customer_data":   ("customer_data_file",   "TrxDate",     "customer_data",   "live", ("RouteCode", "CustomerCode", "ItemCode", "TrxDate")),
    "journey_plan":    ("journey_plan_file",    "JourneyDate", "journey_plan",    "live", ("RouteCode", "CustomerCode", "JourneyDate")),
    "sales_recent":    ("sales_recent_file",    "TrxDate",     "sales_recent",    "live", ("RouteCode", "CustomerCode", "ItemCode", "TrxDate")),
    "demand_forecast": ("demand_forecast_file", "TrxDate",     "demand_forecast", "aiml", ("RouteCode", "ItemCode", "TrxDate", "DataSplit")),
    # Van-stock reconciliation inputs
    "closing_stock":   ("closing_stock_file",   "TrxDate",     "closing_stock",   "live", ("RouteCode", "ItemCode", "TrxDate")),
    "load_allocation": ("load_allocation_file", "TrxDate",     "load_allocation", "live", ("RouteCode", "ItemCode", "TrxDate")),
    "returns_recent":  ("returns_recent_file",  "TrxDate",     "sales_returns",   "live", ("RouteCode", "CustomerCode", "ItemCode", "TrxDate")),
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

    def import_dataset(
        self,
        dataset: str,
        mode: str = "incremental",
        lookback_days: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        Import a single dataset.

        Args:
            dataset: "customer_data", "journey_plan", or "sales_recent"
            mode: "incremental" (append new rows) or "full" (replace all)
            lookback_days: Optional refresh window for incremental mode.
                When set, the importer re-pulls the last N days regardless
                of the CSV's max date, then dedup-merges (last-write-wins
                on numeric aggregates). Required when an upstream producer
                UPDATES existing rows (e.g. ``reconciliation_refresh``
                rewriting demand_forecast); pure-append
                ``trx_date > max_csv_date`` would miss those updates.
        """
        if dataset not in _DATASETS:
            return {"success": False, "error": f"Unknown dataset: {dataset}. Use: {list(_DATASETS.keys())}"}

        file_attr, date_col, query_method, db, dim_key = _DATASETS[dataset]
        file_path = self._s.data_path(getattr(self._s, file_attr))

        t0 = time.time()

        # Decide the SQL window + whether to merge with the existing CSV.
        #   ``full``                       -> replace CSV outright.
        #   ``incremental`` + lookback_days -> refresh window: re-pull the
        #       last N days, dedup-merge with existing CSV. Catches row
        #       UPDATEs that pure-append would miss.
        #   ``incremental`` + existing CSV -> auto-detect from max(date)
        #       and append rows strictly after it.
        #   ``incremental`` + no CSV       -> pull the query's default
        #       window into a fresh CSV.
        query_fn = getattr(self._qb, query_method)
        since_date: Optional[str] = None
        existing_rows = 0
        merge_with_existing = False

        if mode == "full":
            sql, params = query_fn()
        elif lookback_days and file_path.exists():
            sql, params = query_fn(lookback_days=int(lookback_days))
            merge_with_existing = True
            existing_rows = self._detect_last_date(file_path, date_col)[1]
            logger.info(
                "%s: refresh-window pull (last %d days, %d existing rows)",
                dataset, int(lookback_days), existing_rows,
            )
        elif mode == "incremental" and file_path.exists():
            since_date, existing_rows = self._detect_last_date(file_path, date_col)
            if since_date:
                sql, params = query_fn(since_date=since_date)
                merge_with_existing = True
                logger.info(
                    "%s: incremental from %s (%d existing rows)",
                    dataset, since_date, existing_rows,
                )
            else:
                sql, params = query_fn()
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

        # Merge with existing CSV when we pulled a partial window
        # (incremental or refresh-window); otherwise replace outright.
        # Dedup uses ONLY the dataset's canonical natural-key columns
        # (the same tuple the upstream DB MERGE keys on). This is
        # critical: a previous version used "every column except numeric
        # aggregates" which silently let columns like ModelUsed and
        # DemandClass drift the key across training runs -- duplicates
        # then accumulated forever, inflating the CSV and breaking
        # downstream sums. Restricting to the natural key + coercing
        # to string with empty-fill makes last-write-wins propagate
        # producer UPDATEs cleanly regardless of which non-key columns
        # changed.
        if merge_with_existing:
            existing_df = pd.read_csv(file_path, low_memory=False)
            combined = pd.concat([existing_df, new_df], ignore_index=True)
            key_cols = [c for c in dim_key if c in combined.columns]
            if not key_cols:
                # Defensive: dataset registry mis-configured. Fall back
                # to whole-row dedup so we at least don't accumulate
                # exact duplicates.
                combined = combined.drop_duplicates(keep="last")
            else:
                combined[key_cols] = (
                    combined[key_cols].astype(object).where(combined[key_cols].notna(), "").astype(str)
                )
                combined = combined.drop_duplicates(subset=key_cols, keep="last")
            total_rows = len(combined)
        else:
            combined = new_df
            total_rows = len(combined)

        # Save atomically: write to a sibling .tmp then ``os.replace`` so a
        # crash mid-write can't truncate the canonical CSV that downstream
        # services treat as the single source of truth. ``os.replace`` is
        # atomic on Windows + POSIX (rename-on-rename semantics).
        Path(file_path).parent.mkdir(parents=True, exist_ok=True)
        tmp_path = file_path.with_suffix(file_path.suffix + ".tmp")
        combined.to_csv(tmp_path, index=False)
        os.replace(tmp_path, file_path)

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

    def import_all(
        self,
        mode: str = "incremental",
        lookback_days: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Import all datasets in parallel.

        The 7 dataset queries hit two independent DBs and are otherwise
        independent of each other. pyodbc releases the GIL during network
        I/O, so a small thread pool turns the cron into a single
        connection-bounded round trip instead of a serial chain. The
        ``import_concurrency`` setting caps fan-out so OLTP load stays
        gentle even under a freshly-stale full refresh.

        ``lookback_days`` flows through to every dataset's
        ``import_dataset`` so a single cascade call can refresh the
        rolling window across all mirrors.

        A failure in one dataset is captured into its result dict and
        does not block the others -- the caller still gets a complete
        per-dataset status map.
        """
        workers = max(1, min(len(_DATASETS), int(self._s.import_concurrency)))
        if workers <= 1 or len(_DATASETS) <= 1:
            return {
                dataset: self.import_dataset(dataset, mode, lookback_days=lookback_days)
                for dataset in _DATASETS
            }

        results: Dict[str, Any] = {}
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="di-import") as pool:
            future_to_dataset = {
                pool.submit(self.import_dataset, dataset, mode, lookback_days): dataset
                for dataset in _DATASETS
            }
            for fut in as_completed(future_to_dataset):
                dataset = future_to_dataset[fut]
                try:
                    results[dataset] = fut.result()
                except Exception as exc:
                    logger.error("Parallel import crashed for %s: %s", dataset, exc, exc_info=True)
                    results[dataset] = {
                        "success": False,
                        "dataset": dataset,
                        "error": str(exc),
                        "new_rows": 0,
                    }
        # Preserve a stable key order (matches the registry) so downstream
        # consumers iterating the response see the same shape as the serial path.
        return {dataset: results[dataset] for dataset in _DATASETS if dataset in results}

    def status(self) -> Dict[str, Any]:
        """Return current state of all local data files.

        Per-dataset result is memoised keyed on (path, mtime, size). Only the
        files whose mtime changed get re-parsed -- so a stable status endpoint
        is O(1) instead of an O(N) date-column scan on every call.
        """
        info: Dict[str, Any] = {}
        cache = self._status_cache
        for dataset, (file_attr, date_col, _, _, _) in _DATASETS.items():
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
        # ``DatabaseClient.test_connection`` returns (ok, reason); only the
        # boolean is cached on the hot endpoint for back-compat. The reason
        # is logged inside the client at warning/error level so ops still
        # get the diagnostic.
        result = self._db.test_connection()
        ok = bool(result[0]) if isinstance(result, tuple) else bool(result)
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
