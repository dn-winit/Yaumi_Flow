"""
Pushes forecast predictions to yf_demand_forecast in YaumiAIML.

Wire-format columns (the ones in the prediction CSV) are read from the
pipeline YAML so the contract follows ``data.{date_col, target_col,
forecast_level}`` automatically - no hardcoded strings drift if the
config is renamed.

Writes use a #temp staging table + MERGE inside one transaction. Effect:

  * The target table is locked only for the MERGE itself, not during
    the (potentially slow) bulk INSERT into the staging table.
  * Same row count, atomic per ``data_split`` - partial failures roll
    back cleanly.
  * Existing rows update in place rather than getting deleted-and-reinserted,
    so primary-key sequences and downstream change-data-capture stay
    monotonic.

Reconciled values (``recommended_load``, ``forecast_corrected``,
``bias_pct``, ``opening_stock``) are computed via
``services.reconciliation.enrich.enrich_with_load`` BEFORE the push so
the DB always matches what the API serves. If the engine is unavailable
the columns are zero-filled and a warning is logged.
"""

from __future__ import annotations

import logging
import random
import time
from typing import Any, Optional

import numpy as np
import pandas as pd
import pyodbc

from demand_forecasting_pipeline.config.settings import (
    DEFAULT_STR_LIMITS,
    Settings,
    get_settings,
)
from demand_forecasting_pipeline.observability import DB_PUSH_ROWS
from demand_forecasting_pipeline.services.db_pool import (
    FATAL_DB_ERRORS,
    get_pool,
)
from demand_forecasting_pipeline.src.utils.config_loader import load_config

logger = logging.getLogger(__name__)

# Exact column order matching yf_demand_forecast (excludes id, created_at).
# The reconciliation columns at the tail are added by migrations.
_DB_COLUMNS = [
    "trx_date", "route_code", "item_code", "item_name",
    "data_split", "demand_class", "model_used",
    "predicted", "p_demand", "qty_if_demand", "actual_qty",
    "lower_bound", "upper_bound",
    "adi", "cv2", "nonzero_ratio", "mean_qty", "avg_gap_days",
    "recommended_load", "forecast_corrected", "bias_pct", "opening_stock",
    "load_lower_bound", "load_upper_bound",
]

# Columns added by reconciliation migrations. Surfaced separately so the
# schema guard can name them in its remediation message.
_RECONCILIATION_COLUMNS = (
    "recommended_load", "forecast_corrected", "bias_pct", "opening_stock",
    "load_lower_bound", "load_upper_bound",
)

# Natural-key columns used by MERGE to decide MATCH vs INSERT.
_MERGE_KEYS = ("trx_date", "route_code", "item_code", "data_split")
_UPDATE_COLS = tuple(c for c in _DB_COLUMNS if c not in _MERGE_KEYS)

# NVARCHAR character-length limits. Single source of truth lives in
# ``config.settings.DEFAULT_STR_LIMITS``; this alias keeps the local
# name stable for any test harness that imports it. Override per
# deployment via ``DbSettings.str_limits`` (env: ``DF_DB_STR_LIMITS``
# as JSON dict).
_DEFAULT_STR_LIMITS: dict[str, int] = DEFAULT_STR_LIMITS

_INSERT_COLS = ", ".join(f"[{c}]" for c in _DB_COLUMNS)
_INSERT_PLACEHOLDERS = ", ".join("?" for _ in _DB_COLUMNS)

def _qualify_table(table: str) -> tuple[str, str, str]:
    """Split ``[YaumiAIML].[dbo].[yf_demand_forecast]`` into
    (db, schema, name). Raw form ``yf_demand_forecast`` is treated as
    ``(default_db, dbo, yf_demand_forecast)``."""
    parts = [p.strip(" []") for p in table.split(".")]
    if len(parts) == 3:
        return parts[0], parts[1], parts[2]
    if len(parts) == 2:
        return "", parts[0], parts[1]
    return "", "dbo", parts[-1]

def _dataframe_to_records(df: pd.DataFrame, cols: list[str]) -> list[tuple]:
    """Project ``df`` to the ordered ``cols`` and emit a list of plain
    Python tuples suitable for ``cursor.executemany``."""
    return [
        tuple(None if pd.isna(v) else (v.item() if hasattr(v, "item") else v) for v in row)
        for row in df[cols].values.tolist()
    ]

class SchemaMismatchError(RuntimeError):
    """Raised when ``yf_demand_forecast`` is missing columns added by a
    migration the running code expects to be present."""

class DbPusher:
    """Pushes prediction CSVs to yf_demand_forecast."""

    def __init__(self, settings: Optional[Settings] = None) -> None:
        self._s = settings or get_settings()
        self._db = self._s.db
        self._pool = (
            get_pool(
                self._db.connection_string(),
                max_connections=max(self._db.retry_attempts + 1, 4),
                connect_timeout=self._db.connection_timeout,
                query_timeout=self._db.query_timeout,
                autocommit=False,
            )
            if self._db.configured
            else None
        )
        # Schema guard runs lazily on the first push so an unhealthy DB
        # at boot doesn't crash the whole service. ``None`` means "not
        # yet checked"; True/False is the cached verdict.
        self._schema_ok: Optional[bool] = None
        self._schema_error: Optional[str] = None

        # Source-side column names from pipeline config.
        try:
            cfg = load_config(self._s.pipeline_config)
            data_cfg = cfg.get("data", {}) or {}
            group_keys = list(data_cfg.get("forecast_level") or [])
            self._src_date_col = data_cfg.get("date_col") or "TrxDate"
            self._src_target_col = data_cfg.get("target_col") or "TotalQuantity"
            self._src_route_col = group_keys[0] if len(group_keys) >= 1 else "RouteCode"
            self._src_item_col = group_keys[1] if len(group_keys) >= 2 else "ItemCode"
        except Exception as exc:
            logger.warning("DbPusher: could not load pipeline config (%s); using defaults", exc)
            self._src_date_col = "TrxDate"
            self._src_target_col = "TotalQuantity"
            self._src_route_col = "RouteCode"
            self._src_item_col = "ItemCode"

    @property
    def available(self) -> bool:
        return self._db.configured and bool(self._s.demand_table) and self._pool is not None

    # ------------------------------------------------------------------
    # Schema guard
    # ------------------------------------------------------------------

    def check_schema(self) -> dict[str, Any]:
        """Verify the configured table has every column the pusher writes.

        Returns ``{"ok": True}`` on success, ``{"ok": False, "missing":
        [...], "remediation": "run scripts/migrations/...sql"}`` otherwise.
        Cached after the first successful check; cached failure is also
        sticky -- callers must restart the service after applying the
        migration so we don't repeatedly probe a known-bad schema.
        """
        if self._schema_ok is True:
            return {"ok": True}
        if self._schema_ok is False:
            return {"ok": False, "error": self._schema_error}

        if not self.available:
            return {"ok": False, "error": "DB not configured"}

        db_name, schema, table = _qualify_table(self._s.demand_table)
        sql = (
            "SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS "
            "WHERE TABLE_SCHEMA = ? AND TABLE_NAME = ?"
        )
        params: list = [schema, table]
        if db_name:
            # Cross-DB INFORMATION_SCHEMA query: prepend with USE-style
            # 3-part name. SQL Server supports filtering by table catalog.
            sql = (
                "SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS "
                "WHERE TABLE_CATALOG = ? AND TABLE_SCHEMA = ? AND TABLE_NAME = ?"
            )
            params = [db_name, schema, table]

        try:
            assert self._pool is not None
            with self._pool.acquire() as conn:
                cur = conn.cursor()
                cur.execute(sql, params)
                present = {row[0].lower() for row in cur.fetchall()}
        except Exception as exc:
            self._schema_ok = False
            self._schema_error = f"schema probe failed: {exc}"
            return {"ok": False, "error": self._schema_error}

        if not present:
            self._schema_ok = False
            self._schema_error = (
                f"table {self._s.demand_table} not found via INFORMATION_SCHEMA"
            )
            return {"ok": False, "error": self._schema_error}

        missing = [c for c in _DB_COLUMNS if c.lower() not in present]
        if missing:
            self._schema_ok = False
            self._schema_error = (
                f"yf_demand_forecast is missing columns {missing}. "
                f"Run migration: demand_forecasting_pipeline/scripts/migrations/"
                f"0001_add_reconciliation_cols.sql against database "
                f"{db_name or self._db.database!r} before retrying push."
            )
            logger.error("schema_mismatch: %s", self._schema_error)
            return {"ok": False, "missing": missing, "error": self._schema_error}

        self._schema_ok = True
        return {"ok": True}

    # ------------------------------------------------------------------
    # Push entry point
    # ------------------------------------------------------------------

    def push_predictions(self, datasplit: str = "forecast") -> dict[str, Any]:
        if not self.available:
            return {"success": False, "error": "DB not configured (set DF_DB_HOST + DF_DEMAND_TABLE)"}

        guard = self.check_schema()
        if not guard.get("ok"):
            return {"success": False, "error": guard.get("error"), "missing": guard.get("missing")}

        path = self._s.predictions_path(
            self._s.future_forecast_file if datasplit == "forecast" else self._s.test_predictions_file
        )
        if not path.exists():
            return {"success": False, "error": f"File not found: {path}"}

        raw = pd.read_csv(path, low_memory=False)
        if raw.empty:
            return {"success": False, "error": "File is empty"}

        # Required-column validation. Without these the push would
        # silently fall back to defaults (zero predictions, NULL
        # explainability) and the DB would carry data that doesn't
        # match what the API serves -- exactly the kind of silent
        # corruption this guard exists to prevent.
        required_cols = {
            self._src_date_col, self._src_route_col, self._src_item_col,
            "prediction",
        }
        missing_required = required_cols - set(raw.columns)
        if missing_required:
            msg = (
                f"db_push refused: predictions CSV at {path} is missing "
                f"required columns {sorted(missing_required)}; retrain to "
                f"regenerate the artifact."
            )
            logger.error(msg)
            return {"success": False, "error": msg, "missing": sorted(missing_required)}

        # Compute reconciled columns BEFORE the push so the DB matches what
        # the API serves. Failures here degrade to zero-fills so a missing
        # bias table can't block the push entirely.
        try:
            from demand_forecasting_pipeline.services.reconciliation.enrich import (
                enrich_with_load,
            )
            raw = enrich_with_load(
                raw,
                route_col=self._src_route_col,
                item_col=self._src_item_col,
                date_col=self._src_date_col,
                predicted_col="prediction",
                output_col="recommended_load",
                with_diagnostics=True,
                settings=self._s,
            )
        except Exception as exc:
            logger.warning(
                "db_push: reconciliation enrichment unavailable (%s); writing zero-filled cols",
                exc,
            )

        # Verify the four reconciliation columns are present after enrich
        # (or after the zero-fill fallback). If any are missing, the row
        # mapper would silently default to 0.0 and the DB would carry
        # unaudited zeros for the affected push -- this guard surfaces
        # the gap so ops can investigate.
        missing_recon = [
            c for c in _RECONCILIATION_COLUMNS if c not in raw.columns
        ]
        if missing_recon:
            logger.warning(
                "db_push: reconciliation columns absent after enrich %s; "
                "they will be written as 0.0 (matches migration DEFAULT). "
                "Investigate the upstream enrich path if this persists.",
                missing_recon,
            )

        df = self._map_columns(raw, datasplit)
        return self._upsert(df, datasplit)

    # ------------------------------------------------------------------
    # Column mapping
    # ------------------------------------------------------------------

    def _map_columns(self, raw: pd.DataFrame, datasplit: str) -> pd.DataFrame:
        out = pd.DataFrame()

        # ----- Date column: parse to canonical YYYY-MM-DD string -----
        # SQL Server CAST(? AS DATE) is locale-sensitive at bind time; if
        # the CSV ships ISO/US/UK formats interchangeably the bind can
        # mis-parse on non-en-US servers. Normalising here matches the
        # read side (``accuracy_service._normalize``) so train/serve
        # round-trip is byte-aligned.
        date_series = pd.to_datetime(
            raw.get(self._src_date_col, pd.NaT), errors="coerce",
        )
        out["trx_date"] = date_series.dt.strftime("%Y-%m-%d")

        # ----- Required key columns (NVARCHAR NOT NULL) -----
        out["route_code"] = raw.get(self._src_route_col, "").astype(str)
        out["item_code"] = raw.get(self._src_item_col, "").astype(str)

        # ----- Optional NVARCHAR columns: None for missing values -----
        # Keeping None (vs empty string) lets NVARCHAR NULL columns store
        # NULL rather than ``""`` so downstream queries that filter on
        # ``IS NULL`` work as expected.
        out["item_name"] = raw.get("ItemName", None)
        out["demand_class"] = raw.get("class", None)
        out["model_used"] = raw.get("model_used", raw.get("best_model", None))

        # data_split is always populated by the pipeline; canonical casing
        # matches the WHEN NOT MATCHED BY SOURCE filter in the MERGE SQL.
        out["data_split"] = datasplit.capitalize()

        # ----- Numeric columns: explicit float coercion -----
        # NaN/Inf -> None below in the records projection; numpy scalars
        # are unboxed there too so pyodbc sees plain Python types.
        for col, src in [
            ("predicted", "prediction"),
            ("p_demand", "p_demand"),
            ("qty_if_demand", "qty_if_demand"),
            ("actual_qty", self._src_target_col),
            ("lower_bound", "q_10"),
            ("upper_bound", "q_90"),
            # Reconciliation columns (added by migrations). Default to
            # 0.0 to match each migration's NOT NULL DEFAULT 0.
            ("recommended_load", "recommended_load"),
            ("forecast_corrected", "forecast_corrected"),
            ("bias_pct", "bias_pct"),
            ("opening_stock", "opening_stock"),
            ("load_lower_bound", "load_lower_bound"),
            ("load_upper_bound", "load_upper_bound"),
        ]:
            # When ``src`` isn't a column on ``raw`` (e.g. ``actual_qty``
            # for the forecast split, which has no actuals yet), ``.get``
            # returns the scalar default. Wrap it in a Series so the
            # ``.fillna`` below has a valid receiver -- a plain float
            # has no ``.fillna``.
            val = raw.get(src, 0.0)
            if not isinstance(val, pd.Series):
                val = pd.Series(val, index=out.index)
            out[col] = pd.to_numeric(val, errors="coerce").fillna(0.0)

        # ----- Nullable float columns (FLOAT NULL allowed) -----
        # Preserve NaN -> None semantics so the DB stores NULL rather
        # than 0.0 when an explainability stat is genuinely unknown.
        for col in ("adi", "cv2", "nonzero_ratio", "mean_qty", "avg_gap_days"):
            out[col] = pd.to_numeric(raw.get(col, np.nan), errors="coerce")

        # ----- NVARCHAR truncation (settings-driven) -----
        # Only applied to non-null values; ``.astype(str)`` on None/NaN
        # would otherwise emit literal "None"/"nan" strings into the
        # column instead of NULL.
        limits = dict(_DEFAULT_STR_LIMITS)
        configured = getattr(self._db, "str_limits", None) or {}
        if isinstance(configured, dict):
            limits.update({str(k): int(v) for k, v in configured.items()})
        for col, limit in limits.items():
            if col not in out.columns:
                continue
            mask = out[col].notna()
            out.loc[mask, col] = (
                out.loc[mask, col].astype(str).str.slice(0, int(limit))
            )
        return out

    # ------------------------------------------------------------------
    # Upsert (staging table + MERGE inside one transaction)
    # ------------------------------------------------------------------

    def _upsert(self, df: pd.DataFrame, datasplit: str) -> dict[str, Any]:
        table = self._s.demand_table
        records = _dataframe_to_records(df, _DB_COLUMNS)
        chunk = self._db.executemany_chunk_size

        merge_sql = self._build_merge_sql(table, lock_hints=self._db.merge_target_lock_hints)
        isolation = self._db.merge_isolation_level
        # Catalog from the qualified table name so the staging-table
        # ``SELECT TOP 0 ... INTO #stage`` always lands in the same
        # database as the target, regardless of the connection's
        # current database context. Empty ``db_name`` -> table is
        # unqualified; the caller is responsible for ensuring the
        # connection's current DB is the right one.
        db_name, _, _ = _qualify_table(table)
        split_label = datasplit.capitalize()

        last_error: Optional[str] = None
        for attempt in range(1, self._db.retry_attempts + 1):
            t0 = time.time()
            try:
                assert self._pool is not None
                with self._pool.acquire() as conn:
                    cursor = conn.cursor()
                    cursor.fast_executemany = True

                    # Pin the connection to the target catalog before we
                    # create #stage_yf; otherwise SELECT TOP 0 against a
                    # 3-part name can land the staging table in the wrong
                    # database (or fail with "Invalid object name").
                    if db_name:
                        cursor.execute(f"USE [{db_name}];")

                    # MERGE on SQL Server has documented phantom-row races
                    # at READ COMMITTED when two writers target the same key
                    # space. SERIALIZABLE + HOLDLOCK/UPDLOCK on the target
                    # closes that hole without serialising unrelated writes
                    # against the rest of the table.
                    cursor.execute(f"SET TRANSACTION ISOLATION LEVEL {isolation};")

                    # 1. Materialise a session-scoped staging table that
                    # mirrors the column types of the target. ``SELECT TOP
                    # 0 ... INTO #stage`` borrows the schema from the live
                    # table -- no DDL drift between code and DB.
                    cursor.execute(
                        f"SELECT TOP 0 {_INSERT_COLS} INTO #stage_yf FROM {table}"
                    )

                    # 2. Bulk INSERT the records into the staging table.
                    # Doesn't lock the target -- the target stays available
                    # to readers throughout this slow phase.
                    stage_insert = (
                        f"INSERT INTO #stage_yf ({_INSERT_COLS}) "
                        f"VALUES ({_INSERT_PLACEHOLDERS})"
                    )
                    for i in range(0, len(records), chunk):
                        cursor.executemany(stage_insert, records[i : i + chunk])

                    # 3. Atomic MERGE: matched rows update in place, new
                    # rows insert, target rows of THIS data_split that
                    # have no source counterpart get deleted. The
                    # data_split filter is bound as a single positional
                    # parameter; a previous version DECLAREd it in a
                    # separate batch, but ``DECLARE @var`` is batch-
                    # scoped in SQL Server and pyodbc puts each
                    # ``cursor.execute()`` in its own batch, so the
                    # variable was out of scope by the time the MERGE
                    # ran. Lock hints applied via the SQL template
                    # (see _build_merge_sql).
                    cursor.execute(merge_sql, (split_label,))

                    conn.commit()
                duration = round(time.time() - t0, 2)
                logger.info(
                    "db_push wrote %d rows to %s (split=%s) in %.1fs",
                    len(records), table, datasplit, duration,
                )
                DB_PUSH_ROWS.labels(datasplit=datasplit).inc(len(records))
                return {
                    "success": True,
                    "table": table,
                    "rows": len(records),
                    "duration_seconds": duration,
                }
            except FATAL_DB_ERRORS as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                logger.error("db_push fatal error (no retry): %s", exc)
                break
            except Exception as exc:
                last_error = str(exc)
                logger.warning(
                    "db_push attempt %d/%d failed (transient): %s",
                    attempt, self._db.retry_attempts, exc,
                )
                if attempt < self._db.retry_attempts:
                    # Exponential backoff with jitter. Two reasons over the
                    # plain linear backoff: (1) exponential gives the DB more
                    # breathing room on the second retry (typical recovery
                    # is multi-second after a transient lock); (2) the
                    # uniform jitter prevents thundering herd if multiple
                    # writers retry the same target table at the same instant.
                    base = self._db.retry_delay * (2 ** (attempt - 1))
                    time.sleep(base + random.uniform(0.0, float(self._db.retry_delay)))

        return {"success": False, "error": last_error or "All push attempts failed"}

    @staticmethod
    def _build_merge_sql(table: str, *, lock_hints: str = "") -> str:
        """Compose the MERGE statement once per push.

        ``WHEN NOT MATCHED BY SOURCE AND tgt.[data_split] = ?`` deletes
        rows of the current split that have no replacement in the
        staging table -- preserving the DELETE+INSERT semantics (a push
        replaces the entirety of its data_split). The data_split value
        is passed positionally on the cursor.execute() call.

        ``lock_hints`` (e.g. ``HOLDLOCK, UPDLOCK``) is injected into the
        target table reference. Microsoft Books Online recommends
        ``HOLDLOCK`` on every MERGE target to prevent the read-then-write
        race; ``UPDLOCK`` upgrades the read lock so concurrent readers
        don't deadlock with the implicit conversion to a write lock.
        Empty string disables hints (single-writer-per-split deployments).
        """
        keys_on = " AND ".join(
            f"tgt.[{k}] = src.[{k}]" for k in _MERGE_KEYS
        )
        update_set = ", ".join(
            f"tgt.[{c}] = src.[{c}]" for c in _UPDATE_COLS
        )
        cols_csv = ", ".join(f"[{c}]" for c in _DB_COLUMNS)
        src_csv = ", ".join(f"src.[{c}]" for c in _DB_COLUMNS)
        hint_clause = f" WITH ({lock_hints})" if lock_hints else ""
        return (
            f"MERGE {table}{hint_clause} AS tgt "
            f"USING #stage_yf AS src "
            f"ON ({keys_on}) "
            f"WHEN MATCHED THEN UPDATE SET {update_set} "
            f"WHEN NOT MATCHED BY TARGET THEN INSERT ({cols_csv}) VALUES ({src_csv}) "
            f"WHEN NOT MATCHED BY SOURCE AND tgt.[data_split] = ? THEN DELETE"
            f";"
        )
