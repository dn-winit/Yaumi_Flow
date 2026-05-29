"""Data importer -- incremental + full refresh for all datasets.
Incremental: read CSV max date, fetch newer rows, append, dedup.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

import pandas as pd

from data_import.config.settings import Settings, get_settings
from data_import.core.database import DatabaseClient
from data_import.core.queries import QueryBuilder

logger = logging.getLogger(__name__)


# Dataset registry: key -> (file_setting, date_column, query_method, db,
#                           dim_key, retention_attr)
# dim_key MUST match the upstream DB MERGE natural key, else non-key
# column drift (e.g. ModelUsed) accumulates duplicates forever.
# retention_attr bounds the CSV's date range after each merge.
_DATASETS = {
    "customer_data":      ("customer_data_file",      "TrxDate",     "customer_data",      "live", ("RouteCode", "CustomerCode", "ItemCode", "TrxDate"),                "customer_data_lookback_days"),
    "journey_plan":       ("journey_plan_file",       "JourneyDate", "journey_plan",       "live", ("RouteCode", "CustomerCode", "JourneyDate"),                         "journey_plan_window_days"),
    # sales_recent is a route-level rollup (GROUP BY drops CustomerCode).
    "sales_recent":       ("sales_recent_file",       "TrxDate",     "sales_recent",       "live", ("RouteCode", "ItemCode", "TrxDate"),                                 "sales_recent_lookback_days"),
    "demand_forecast":    ("demand_forecast_file",    "TrxDate",     "demand_forecast",    "aiml", ("RouteCode", "ItemCode", "TrxDate", "DataSplit"),                    "demand_forecast_lookback_days"),
    # Sales-transactions mirror: carry chain + diagnostics + actual_sold.
    "sales_transactions": ("sales_transactions_file", "TrxDate",     "sales_transactions", "aiml", ("RouteCode", "ItemCode", "TrxDate"),                                 "sales_transactions_lookback_days"),
    # Van-stock reconciliation inputs
    "closing_stock":      ("closing_stock_file",      "TrxDate",     "closing_stock",      "live", ("RouteCode", "ItemCode", "TrxDate"),                                 "closing_stock_lookback_days"),
    "load_allocation":    ("load_allocation_file",    "TrxDate",     "load_allocation",    "live", ("RouteCode", "ItemCode", "TrxDate"),                                 "load_allocation_lookback_days"),
    "returns_recent":     ("returns_recent_file",     "TrxDate",     "sales_returns",      "live", ("RouteCode", "CustomerCode", "ItemCode", "TrxDate"),                 "sales_returns_lookback_days"),
}


class DataImporter:
    """Fetches data from DB and saves to local CSV with incremental support."""

    # Positive probes (DB reachable) cached longer; negative probes shorter so
    # a recovered DB is detected quickly. Both keep ``/health`` non-blocking
    # once the first probe has answered.
    _CONN_PROBE_TTL_OK_SECONDS = 30
    _CONN_PROBE_TTL_FAIL_SECONDS = 10

    def __init__(self, settings: Settings | None = None) -> None:
        self._s = settings or get_settings()
        self._db = DatabaseClient(self._s)
        self._qb = QueryBuilder(self._s)
        # (timestamp, ok) of the most recent probe.
        self._conn_probe: tuple[float, bool] | None = None
        # Re-entrancy guard: while one caller is doing the actual pyodbc.connect,
        # other concurrent /health callers return the previous cached value
        # (or False on cold start) instead of all racing the same DB.
        self._conn_probe_lock = threading.Lock()
        self._conn_probe_inflight = False
        # dataset -> ((path, mtime_ns, size), status_entry)
        self._status_cache: dict[str, tuple[tuple[str, int, int], dict[str, Any]]] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @staticmethod
    def _csv_has_column(path: Path, column: str) -> bool:
        """Cheap header probe so the chunked merge can decide whether the
        existing CSV carries the dataset's date column without paying a
        full read. Reads exactly one row."""
        try:
            head = pd.read_csv(path, nrows=0)
            return column in head.columns
        except Exception:
            return False

    def import_dataset(
        self,
        dataset: str,
        mode: str = "incremental",
        lookback_days: int | None = None,
        routes: list[str] | None = None,
    ) -> dict[str, Any]:
        """Import a single dataset.

        ``mode``: ``"incremental"`` (append) or ``"full"`` (replace).
        ``lookback_days``: refresh-window override for incremental; required
        when upstream UPDATEs existing rows (pure-append would miss them).
        ``routes``: subset for gentle backfills (default = fleet from settings).
        """
        if dataset not in _DATASETS:
            return {"success": False, "error": f"Unknown dataset: {dataset}. Use: {list(_DATASETS.keys())}"}

        file_attr, date_col, query_method, db, dim_key, retention_attr = _DATASETS[dataset]
        file_path = self._s.data_path(getattr(self._s, file_attr))

        t0 = time.time()

        # SQL window + merge decision:
        #   full -> replace CSV; incremental+lookback_days -> refresh window;
        #   incremental+CSV -> append since max(date); incremental+no-CSV -> default.
        query_fn = getattr(self._qb, query_method)
        since_date: str | None = None
        existing_rows = 0
        merge_with_existing = False

        # Threads the ``routes`` override through every branch.
        def _build(**kwargs):
            if routes is not None:
                kwargs["routes"] = routes
            return query_fn(**kwargs)

        if mode == "full":
            sql, params = _build()
        elif lookback_days and file_path.exists():
            sql, params = _build(lookback_days=int(lookback_days))
            merge_with_existing = True
            existing_rows = self._detect_last_date(file_path, date_col)[1]
            logger.info(
                "%s: refresh-window pull (last %d days, %d existing rows)",
                dataset, int(lookback_days), existing_rows,
            )
        elif mode == "incremental" and file_path.exists():
            since_date, existing_rows = self._detect_last_date(file_path, date_col)
            if since_date:
                sql, params = _build(since_date=since_date)
                merge_with_existing = True
                logger.info(
                    "%s: incremental from %s (%d existing rows)",
                    dataset, since_date, existing_rows,
                )
            else:
                sql, params = _build()
        else:
            sql, params = _build()

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

        # Partial-window pulls merge into existing CSV; dedup uses ONLY
        # the dataset's canonical natural key so non-key drift (ModelUsed,
        # DemandClass) can't accumulate duplicates over training runs.
        if merge_with_existing:
            # Hot path: in-window pull AND existing CSV has the dataset's
            # date column. Stream the existing CSV in chunks, keeping only
            # rows STRICTLY before ``window_min``; in-window rows come
            # exclusively from new_df so DB DELETEs propagate without the
            # full-history materialisation that an unfiltered ``read_csv``
            # would force. Falls back to the legacy concat+dedup when the
            # date column is missing on either side.
            window_min: pd.Timestamp | None = None
            if not new_df.empty and date_col in new_df.columns:
                new_dates = pd.to_datetime(new_df[date_col], errors="coerce")
                wm = new_dates.min()
                if pd.notna(wm):
                    window_min = wm
            can_stream = (
                window_min is not None
                and file_path.exists()
                and self._csv_has_column(file_path, date_col)
            )
            if can_stream:
                kept_chunks: list[pd.DataFrame] = []
                dropped = 0
                # 200k-row chunks keep peak memory ~tens-of-MB even on the
                # 5y sales_recent file; the filter cost is O(chunk-rows).
                for chunk_df in pd.read_csv(
                    file_path, chunksize=200_000, low_memory=False,
                ):
                    if date_col not in chunk_df.columns:
                        kept_chunks.append(chunk_df)
                        continue
                    ex_dates = pd.to_datetime(chunk_df[date_col], errors="coerce")
                    pre_window_mask = ex_dates < window_min
                    dropped += int((~pre_window_mask).sum())
                    pre_window_chunk = chunk_df.loc[pre_window_mask]
                    if not pre_window_chunk.empty:
                        kept_chunks.append(pre_window_chunk)
                if dropped:
                    logger.info(
                        "%s: DELETE-aware merge dropped %d existing CSV "
                        "row(s) in refresh window [%s ..]; window will be "
                        "replaced exclusively by new_df",
                        dataset, dropped, window_min.date(),
                    )
                existing_kept = (
                    pd.concat(kept_chunks, ignore_index=True)
                    if kept_chunks
                    else pd.DataFrame(columns=new_df.columns)
                )
                combined = pd.concat([existing_kept, new_df], ignore_index=True)
            else:
                # Legacy path: column missing or empty new_df. Loads the
                # full CSV once; the dedup-on-keys below remains the
                # safety net for repeat keys across the union.
                existing_df = (
                    pd.read_csv(file_path, low_memory=False)
                    if file_path.exists() else pd.DataFrame(columns=new_df.columns)
                )
                combined = pd.concat([existing_df, new_df], ignore_index=True)
            key_cols = [c for c in dim_key if c in combined.columns]
            if not key_cols:
                # Defensive fallback: registry mis-configured.
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

        # Retention prune inside the same atomic write as the merge so
        # the CSV's date footprint stays bounded by the import contract.
        # prune_to_lookback=False opts out (snapshot / debug pulls).
        if self._s.prune_to_lookback and date_col in combined.columns:
            retention_days = int(getattr(self._s, retention_attr, 0) or 0)
            if retention_days > 0:
                # Mirrors SQL ``DATEADD(day, -?, GETDATE())``: keep rows
                # on/after (today - retention_days).
                cutoff = (
                    pd.Timestamp.now().normalize()
                    - pd.Timedelta(days=retention_days)
                )
                dates = pd.to_datetime(combined[date_col], errors="coerce")
                keep_mask = dates >= cutoff
                pruned = int((~keep_mask).sum())
                if pruned > 0:
                    combined = combined.loc[keep_mask].reset_index(drop=True)
                    total_rows = len(combined)
                    logger.info(
                        "%s: pruned %d row(s) older than %s "
                        "(retention=%d days from ``%s``)",
                        dataset, pruned, cutoff.date(),
                        retention_days, retention_attr,
                    )

        # Atomic save: write .tmp then os.replace (rename-on-rename is
        # atomic on Windows + POSIX) so a crash mid-write can't truncate.
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
        lookback_days: int | None = None,
        dataset_lookback_overrides: dict[str, int] | None = None,
    ) -> dict[str, Any]:
        """Import all datasets in parallel (thread pool sized by
        ``import_concurrency``). Per-dataset failures are captured into
        the result map without blocking the rest.
        ``dataset_lookback_overrides`` pins a wider window for specific
        datasets (e.g. sales_recent for late-arriving return netting).
        """
        overrides = dataset_lookback_overrides or {}
        def resolve_lookback(dataset: str) -> int | None:
            override = overrides.get(dataset)
            return override if override and override > 0 else lookback_days

        workers = max(1, min(len(_DATASETS), int(self._s.import_concurrency)))
        if workers <= 1 or len(_DATASETS) <= 1:
            return {
                dataset: self.import_dataset(dataset, mode, lookback_days=resolve_lookback(dataset))
                for dataset in _DATASETS
            }

        results: dict[str, Any] = {}
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="di-import") as pool:
            future_to_dataset = {
                pool.submit(self.import_dataset, dataset, mode, resolve_lookback(dataset)): dataset
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
        # Preserve registry order so the response shape matches the serial path.
        return {dataset: results[dataset] for dataset in _DATASETS if dataset in results}

    def status(self) -> dict[str, Any]:
        """Return current state of all local data files. Memoised on
        (path, mtime, size) so a stable endpoint is O(1) once warm."""
        info: dict[str, Any] = {}
        cache = self._status_cache
        for dataset, (file_attr, date_col, _, _, _, _) in _DATASETS.items():
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

    def status_quick(self) -> dict[str, Any]:
        """Fast file-existence summary for ``/health``. Skips the
        ``pd.read_csv`` row-count step that ``status()`` runs on cold
        cache; the per-dataset count of a 5y CSV is irrelevant to the
        binary "is the mirror present?" question health asks. O(N_datasets)
        ``Path.exists()`` calls only.
        """
        info: dict[str, Any] = {}
        for dataset, (file_attr, _, _, _, _, _) in _DATASETS.items():
            file_path = self._s.data_path(getattr(self._s, file_attr))
            info[dataset] = {"exists": file_path.exists()}
        return info

    def test_connection(self) -> bool:
        """Non-blocking DB liveness probe.

        ``/health`` calls return the most recent cached probe result instantly
        and trigger a background refresh when the cache is stale. The first
        ever call (cache empty) returns ``False`` and kicks off the probe in
        the background -- this trades "first response is provably wrong on a
        healthy DB" for "/health never blocks on DNS / TCP / login timeouts".

        Probe-side timeout is bounded by ``health_probe_timeout`` (default 3s)
        on the pyodbc connect; even a dead DB resolves the cache within a few
        seconds of the first call.
        """
        cached = self._conn_probe
        now = time.time()

        if cached is None:
            # Cold start: schedule the probe, answer "unreachable" honestly
            # (we don't know yet) without blocking the caller.
            self._kick_off_probe()
            return False

        age = now - cached[0]
        ttl = (
            self._CONN_PROBE_TTL_OK_SECONDS if cached[1]
            else self._CONN_PROBE_TTL_FAIL_SECONDS
        )
        if age >= ttl:
            # Stale: refresh in background, return the still-cached value.
            self._kick_off_probe()
        return cached[1]

    def _kick_off_probe(self) -> None:
        """Start a background DB probe iff one isn't already running.

        Concurrent ``/health`` calls coalesce to a single probe; the worker
        updates ``_conn_probe`` on completion. Daemon thread so it never
        delays process shutdown.
        """
        with self._conn_probe_lock:
            if self._conn_probe_inflight:
                return
            self._conn_probe_inflight = True

        def _run() -> None:
            try:
                result = self._db.test_connection()
                ok = bool(result[0]) if isinstance(result, tuple) else bool(result)
                self._conn_probe = (time.time(), ok)
            except Exception as exc:  # defensive: never let the worker raise
                logger.warning("db_probe worker failed: %s", exc)
                self._conn_probe = (time.time(), False)
            finally:
                with self._conn_probe_lock:
                    self._conn_probe_inflight = False

        threading.Thread(target=_run, name="db-probe", daemon=True).start()

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    @staticmethod
    def _detect_last_date(file_path: Path, date_col: str) -> tuple[str | None, int]:
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
