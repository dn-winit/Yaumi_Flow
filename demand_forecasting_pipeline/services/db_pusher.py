"""Pushes forecast predictions to yf_demand_forecast (YaumiAIML).

Writes raw forecast + bounds + classification stats via #temp staging + MERGE;
target table locked only for MERGE. Reconciled chain + yaumi_* columns live on
yf_sales_transactions and are written by the daily reconciliation_refresh cron.
"""

from __future__ import annotations

import logging
import random
import re
import time
from typing import Any

import numpy as np
import pandas as pd

from common.db_pool import (
    FATAL_DB_ERRORS,
    get_pool,
)
from demand_forecasting_pipeline.config.settings import (
    DEFAULT_STR_LIMITS,
    Settings,
    get_settings,
)
from demand_forecasting_pipeline.observability import DB_PUSH_ROWS
from demand_forecasting_pipeline.src.utils.config_loader import load_config

logger = logging.getLogger(__name__)

# Exact column order matching yf_demand_forecast (excludes id, created_at).
_DB_COLUMNS = [
    "trx_date", "route_code", "item_code", "item_name",
    "data_split", "demand_class", "model_used",
    "predicted", "p_demand", "qty_if_demand", "actual_qty",
    "lower_bound", "upper_bound",
    "adi", "cv2", "nonzero_ratio", "mean_qty", "avg_gap_days",
]
# Reconciliation cols live on yf_sales_transactions (written by daily cron).

# Natural-key columns used by MERGE to decide MATCH vs INSERT.
_MERGE_KEYS = ("trx_date", "route_code", "item_code", "data_split")
_UPDATE_COLS = tuple(c for c in _DB_COLUMNS if c not in _MERGE_KEYS)

# NVARCHAR widths from config.settings.DEFAULT_STR_LIMITS; alias kept for test harness imports.
_DEFAULT_STR_LIMITS: dict[str, int] = DEFAULT_STR_LIMITS

_INSERT_COLS = ", ".join(f"[{c}]" for c in _DB_COLUMNS)
_INSERT_PLACEHOLDERS = ", ".join("?" for _ in _DB_COLUMNS)

def _qualify_table(table: str) -> tuple[str, str, str]:
    """Split [db].[schema].[name]; bare name -> (default_db, dbo, name)."""
    parts = [p.strip(" []") for p in table.split(".")]
    if len(parts) == 3:
        return parts[0], parts[1], parts[2]
    if len(parts) == 2:
        return "", parts[0], parts[1]
    return "", "dbo", parts[-1]


# Defense-in-depth regex guards for the SQL identifiers we interpolate
# (table name, db name, lock hints, isolation level). Values originate
# from Settings/env so an attacker would need env-injection control,
# but a regex check is cheap and prevents a single typo from producing
# unparseable or hostile SQL. Identifiers are SQL Server's "regular
# identifier" rules (alpha or underscore start, then alphanum/underscore)
# plus optional bracket quoting; qualified names join 1-3 parts with dots.
_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
# Two alternatives -- either FULLY bracketed (every part wrapped in [..])
# or FULLY unbracketed. Half-bracketed forms ("foo].bar", "[foo.bar")
# match the user's deployed bracket style by accident only and are
# almost always a misconfiguration; rejecting them at the regex layer
# is defense-in-depth even though the DB would itself reject the
# resulting SQL. Mixing brackets across parts of the same identifier
# (``[YaumiAIML].dbo.[tbl]``) is also rejected -- pick one style and
# stick with it.
_QUALIFIED_TABLE_RE = re.compile(
    r"^(?:"
    # Fully unbracketed: 1-3 parts of [A-Za-z_]\w*, joined by dots
    r"(?:[A-Za-z_][A-Za-z0-9_]*\.){0,2}[A-Za-z_][A-Za-z0-9_]*"
    r"|"
    # Fully bracketed: 1-3 parts of [...], joined by dots
    r"(?:\[[A-Za-z_][A-Za-z0-9_]*\]\.){0,2}\[[A-Za-z_][A-Za-z0-9_]*\]"
    r")$"
)
# Lock hints: SQL Server combines tokens with commas + whitespace
# (``HOLDLOCK, UPDLOCK``). Empty allowed (no hints).
_LOCK_HINTS_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_,\s]*$|^$")
# Isolation level: tokens separated by whitespace
# (``SERIALIZABLE``, ``READ COMMITTED``).
_ISOLATION_RE = re.compile(r"^[A-Za-z][A-Za-z\s]*$")


def _assert_ident(value: str, *, kind: str, pattern: re.Pattern) -> str:
    """Raise ValueError if value doesn't match pattern; returns value for inline use."""
    if not isinstance(value, str) or not pattern.match(value):
        raise ValueError(
            f"refusing to interpolate {kind}={value!r} into SQL: must "
            f"match {pattern.pattern}"
        )
    return value

def _dataframe_to_records(df: pd.DataFrame, cols: list[str]) -> list[tuple]:
    """df -> list of Python tuples in ordered ``cols`` for cursor.executemany."""
    return [
        tuple(None if pd.isna(v) else (v.item() if hasattr(v, "item") else v) for v in row)
        for row in df[cols].values.tolist()
    ]

class DbPusher:
    """Pushes prediction CSVs to yf_demand_forecast."""

    def __init__(self, settings: Settings | None = None) -> None:
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
        # Schema guard runs lazily on first push; None = unchecked.
        self._schema_ok: bool | None = None
        self._schema_error: str | None = None

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
        """Verify all push-target columns exist; cached (success and failure both sticky)."""
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
            # Cross-DB INFORMATION_SCHEMA via TABLE_CATALOG filter.
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

        # Required-column validation; missing -> hard fail (no silent default-filled rows).
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

        # Recon enrichment NOT run here; that's reconciliation_refresh cron's job.
        df = self._map_columns(raw, datasplit)
        return self._upsert(df, datasplit)

    # ------------------------------------------------------------------
    # Column mapping
    # ------------------------------------------------------------------

    def _map_columns(self, raw: pd.DataFrame, datasplit: str) -> pd.DataFrame:
        out = pd.DataFrame()

        # Date -> canonical YYYY-MM-DD. SQL Server CAST(? AS DATE) is locale-sensitive at bind;
        # normalising here matches the read side (accuracy_service._normalize).
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

        # ----- Bounded-numeric columns: missing -> 0.0 -----
        # Semantics: "no recorded value means zero". Correct for
        # ``predicted`` (a never-predicted row has no demand to push),
        # ``p_demand`` / ``qty_if_demand`` (same), and ``actual_qty``
        # (the forecast split has no observed actuals yet -- 0.0 is the
        # only meaningful default given the column is NOT NULL).
        for col, src in [
            ("predicted", "prediction"),
            ("p_demand", "p_demand"),
            ("qty_if_demand", "qty_if_demand"),
            ("actual_qty", self._src_target_col),
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
        # than 0.0 when the value is genuinely UNKNOWN as opposed to
        # ZERO. Two distinct groups land here:
        #
        # 1. Explainability stats (adi, cv2, ...): missing when the
        #    pair wasn't in the classifier output for this run.
        #
        # 2. Quantile bounds (lower_bound, upper_bound): missing for
        #    every non-quantile model (Croston, ETS, Naive, etc.) --
        #    those models produce a point forecast only, with no
        #    confidence band. Previously these were 0.0-filled, which
        #    made every Croston row look like "predicted X with a
        #    point band at zero" -- indistinguishable from a quantile
        #    model that genuinely said "10% likelihood of demand below
        #    zero". NULL is the only honest representation of "this
        #    model doesn't compute bounds".
        for col, src in [
            ("adi",            "adi"),
            ("cv2",            "cv2"),
            ("nonzero_ratio",  "nonzero_ratio"),
            ("mean_qty",       "mean_qty"),
            ("avg_gap_days",   "avg_gap_days"),
            ("lower_bound",    "q_10"),
            ("upper_bound",    "q_90"),
        ]:
            out[col] = pd.to_numeric(raw.get(src, np.nan), errors="coerce")

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

        # Compute the staging frame's trx_date window. The MERGE's DELETE
        # branch (WHEN NOT MATCHED BY SOURCE) is scoped to this range so a
        # short-horizon push (e.g. 7 days after a retrain) cannot wipe
        # historical rows outside the window. Without this, a retrain
        # emitting a smaller horizon than the prior run silently truncated
        # the prior horizon's audit rows.
        # ``_map_columns`` writes the canonical column as ``trx_date`` (lower-snake);
        # an earlier check against ``"TrxDate"`` silently never matched, leaving
        # min_date/max_date None and the WHEN NOT MATCHED BY SOURCE DELETE branch
        # unreachable (``ISNULL(?, '9999-12-31')`` vs ``ISNULL(?, '0001-01-01')``
        # collapses to a vacuous predicate).
        if "trx_date" in df.columns and not df.empty:
            dates = pd.to_datetime(df["trx_date"], errors="coerce").dropna()
            min_date = dates.min().strftime("%Y-%m-%d") if not dates.empty else None
            max_date = dates.max().strftime("%Y-%m-%d") if not dates.empty else None
        else:
            min_date = max_date = None

        # Validate every SQL identifier we interpolate BEFORE building
        # any SQL string. Defense-in-depth: these come from Settings
        # (env) so a regex guard catches typos and stops env-injection
        # from producing hostile SQL.
        _assert_ident(table, kind="demand_table", pattern=_QUALIFIED_TABLE_RE)
        lock_hints = self._db.merge_target_lock_hints or ""
        _assert_ident(lock_hints, kind="merge_target_lock_hints",
                      pattern=_LOCK_HINTS_RE)
        isolation = self._db.merge_isolation_level
        _assert_ident(isolation, kind="merge_isolation_level",
                      pattern=_ISOLATION_RE)

        merge_sql = self._build_merge_sql(table, lock_hints=lock_hints)
        split_label = datasplit.capitalize()

        last_error: str | None = None
        for attempt in range(1, self._db.retry_attempts + 1):
            t0 = time.time()
            try:
                assert self._pool is not None
                with self._pool.acquire() as conn:
                    cursor = conn.cursor()
                    cursor.fast_executemany = True

                    # NOTE: previously this issued ``USE [{db_name}]`` to pin
                    # the connection to the target catalog. That leaks across
                    # the pool: the next caller for a different database
                    # silently writes to the wrong DB. ``#stage_yf`` lives in
                    # tempdb regardless of current DB context, and ``{table}``
                    # is a fully-qualified 3-part name, so ``SELECT TOP 0
                    # ... INTO #stage_yf FROM <3-part-name>`` resolves the
                    # source schema from the qualified reference -- no USE
                    # needed.

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
                    # MERGE params: split_label for WHEN MATCHED filter,
                    # min/max trx_date for the WHEN NOT MATCHED BY SOURCE
                    # date-scoped DELETE. If the staging frame had no dates,
                    # pass NULL bounds -- the SQL CASE then never deletes
                    # outside the (empty) range.
                    cursor.execute(merge_sql, (split_label, min_date, max_date))

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
            # Date-scoped DELETE: only rows in the same data_split AND within
            # the staging frame's [min_date, max_date] window are eligible.
            # ISNULL bounds (9999-12-31 / 0001-01-01) make an empty/NULL window
            # never match -- a degenerate empty push deletes nothing.
            f"WHEN NOT MATCHED BY SOURCE "
            f"  AND tgt.[data_split] = ? "
            f"  AND tgt.[TrxDate] >= ISNULL(?, '9999-12-31') "
            f"  AND tgt.[TrxDate] <= ISNULL(?, '0001-01-01') "
            f"THEN DELETE"
            f";"
        )
