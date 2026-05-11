"""
Pushes recommendations to yf_recommended_orders in YaumiAIML.
Column mapping matches scripts/create_tables.sql exactly.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd
import pyodbc

from recommended_order.config.settings import Settings, get_settings

logger = logging.getLogger(__name__)

# Exact column order matching yf_recommended_orders (excludes id, generated_at)
_DB_COLUMNS = [
    "trx_date", "route_code", "customer_code", "customer_name",
    "item_code", "item_name", "recommended_quantity", "tier",
    "van_load", "priority_score", "avg_quantity_per_visit",
    "days_since_last_purchase", "purchase_cycle_days", "frequency_percent",
    "churn_probability", "pattern_quality", "purchase_count",
    "trend_factor", "reason_status", "reason_explanation", "reason_confidence",
    "generated_by",
]

# Map from DataFrame column names (PascalCase) to DB column names (snake_case).
# The CSV the engine writes uses the right-hand-side of Recommendation.to_dict;
# the explainability columns (Source / WhyItem / WhyQuantity / Confidence) are
# folded into the DB's `reason_*` columns by ``_push`` -- not via this map --
# because they need type conversion (Confidence: 0-1 float -> 0-100 int) and
# composition (reason_explanation = WhyItem + " | " + WhyQuantity, truncated).
_COL_MAP = {
    "TrxDate": "trx_date", "RouteCode": "route_code",
    "CustomerCode": "customer_code", "CustomerName": "customer_name",
    "ItemCode": "item_code", "ItemName": "item_name",
    "RecommendedQuantity": "recommended_quantity", "Tier": "tier",
    "VanLoad": "van_load", "PriorityScore": "priority_score",
    "AvgQuantityPerVisit": "avg_quantity_per_visit",
    "DaysSinceLastPurchase": "days_since_last_purchase",
    "PurchaseCycleDays": "purchase_cycle_days",
    "FrequencyPercent": "frequency_percent",
    "ChurnProbability": "churn_probability",
    "PatternQuality": "pattern_quality",
    "PurchaseCount": "purchase_count",
    "TrendFactor": "trend_factor",
}

# DB column character-length limits (matches scripts/create_tables.sql).
# Defensive truncation prevents an INSERT error on long explanation strings.
_REASON_STATUS_MAX = 100
_REASON_EXPLANATION_MAX = 500

# Pre-built SQL fragments. Both the column list and placeholder block are
# fully determined by the schema, so we assemble them once at import and
# slot the configured table name in at runtime. Saves the per-push string
# work and matches the pattern used in sales_supervision/services/db_saver.py.
_INSERT_COLS = ", ".join(f"[{c}]" for c in _DB_COLUMNS)
_INSERT_PLACEHOLDERS = ", ".join("?" for _ in _DB_COLUMNS)
_INSERT_SQL_TPL = f"INSERT INTO {{table}} ({_INSERT_COLS}) VALUES ({_INSERT_PLACEHOLDERS})"
# DELETE for a date is unconditional on route_code; the runtime appends
# the route filter when one is supplied.
_DELETE_SQL_BASE = "DELETE FROM {table} WHERE [trx_date] >= ? AND [trx_date] < DATEADD(day, 1, ?)"
_DELETE_ROUTE_SUFFIX = " AND [route_code] = ?"


def _dataframe_to_records(df: pd.DataFrame, cols: List[str]) -> List[tuple]:
    """Project ``df`` to the ordered ``cols`` and emit a list of plain
    Python tuples suitable for ``cursor.executemany``. ``NaN`` becomes
    ``None`` (so SQL Server stores NULL) and numpy scalars are unboxed
    via ``.item()`` so pyodbc's parameter binder doesn't see numpy
    types it cannot serialise.
    """
    return [
        tuple(None if pd.isna(v) else (v.item() if hasattr(v, "item") else v) for v in row)
        for row in df[cols].values.tolist()
    ]


class DbPusher:
    """Pushes recommendation data to yf_recommended_orders."""

    def __init__(self, settings: Optional[Settings] = None) -> None:
        self._s = settings or get_settings()
        self._db = self._s.db

    @property
    def available(self) -> bool:
        return bool(self._db.host and self._db.username and self._s.recommendation_table)

    def _connect(self) -> pyodbc.Connection:
        """Open a transaction-mode connection with the configured query
        timeout already applied. Centralised so every caller gets the
        same shape -- no per-call ``conn.timeout =`` reminders."""
        conn = pyodbc.connect(self._db.aiml_connection_string, autocommit=False)
        conn.timeout = self._db.query_timeout
        return conn

    def push_dataframe(self, df: pd.DataFrame, date: str, route_code: str) -> Dict[str, Any]:
        """Push a DataFrame directly (called after generation)."""
        if not self.available:
            return {"success": False, "error": "DB not configured"}
        return self._push(df, date, route_code)

    def push_recommendations(self, date: str, route_code: Optional[str] = None) -> Dict[str, Any]:
        """Push local CSV files to DB."""
        if not self.available:
            return {"success": False, "error": "DB not configured (set RO_DB_HOST + RO_RECOMMENDATION_TABLE)"}

        file_dir = Path(self._s.file_storage_dir)
        pattern = f"recommendations_{date}_{route_code}.csv" if route_code else f"recommendations_{date}_*.csv"
        files = list(file_dir.glob(pattern))
        if not files:
            return {"success": False, "error": f"No files matching {pattern}"}

        df = pd.concat([pd.read_csv(f, low_memory=False) for f in files], ignore_index=True)
        if df.empty:
            return {"success": False, "error": "Files are empty"}
        return self._push(df, date, route_code)

    def _push(self, df: pd.DataFrame, date: str, route_code: Optional[str]) -> Dict[str, Any]:
        table = self._s.recommendation_table
        t0 = time.time()

        db_df = self._project_to_schema(df)

        insert_sql = _INSERT_SQL_TPL.format(table=table)
        # DELETE matches by date (and optional route) so a re-run is
        # idempotent: same key set lands the same rows. DELETE + chunked
        # INSERT live in one transaction so a partial INSERT failure
        # rolls the DELETE back too -- never any orphan gaps.
        delete_sql = _DELETE_SQL_BASE.format(table=table)
        delete_params: List[Any] = [date, date]
        if route_code:
            delete_sql += _DELETE_ROUTE_SUFFIX
            delete_params.append(route_code)

        records = _dataframe_to_records(db_df, _DB_COLUMNS)
        chunk = self._db.executemany_chunk_size

        last_error: Optional[str] = None
        for attempt in range(1, self._db.retry_attempts + 1):
            conn: Optional[pyodbc.Connection] = None
            try:
                conn = self._connect()
                cursor = conn.cursor()
                cursor.fast_executemany = True
                cursor.execute(delete_sql, delete_params)
                for i in range(0, len(records), chunk):
                    cursor.executemany(insert_sql, records[i : i + chunk])
                conn.commit()

                duration = round(time.time() - t0, 2)
                logger.info("Pushed %d recs to %s for %s in %.1fs", len(records), table, date, duration)
                return {"success": True, "table": table, "rows": len(records), "duration_seconds": duration}
            except (pyodbc.ProgrammingError, pyodbc.DataError, pyodbc.IntegrityError) as exc:
                # Bad SQL / bad data -- retrying is futile and the upstream
                # caller deserves a fast, accurate failure response.
                last_error = f"{type(exc).__name__}: {exc}"
                logger.error("Push fatal error for %s/%s (no retry): %s",
                             date, route_code or "ALL", exc)
                if conn is not None:
                    try:
                        conn.rollback()
                    except Exception:
                        pass
                break
            except Exception as exc:
                last_error = str(exc)
                logger.warning(
                    "Push attempt %d/%d failed for %s/%s (transient): %s",
                    attempt, self._db.retry_attempts, date, route_code or "ALL", exc,
                )
                if conn is not None:
                    try:
                        conn.rollback()
                    except Exception:
                        pass
                if attempt < self._db.retry_attempts:
                    time.sleep(self._db.retry_delay * attempt)
            finally:
                if conn is not None:
                    try:
                        conn.close()
                    except Exception as close_exc:
                        logger.warning("conn.close() failed: %s", close_exc)

        return {"success": False, "error": last_error or "All push attempts failed"}

    def _project_to_schema(self, df: pd.DataFrame) -> pd.DataFrame:
        """Translate the engine's CSV shape into the DB column shape.

        Three independent rewrites: (1) PascalCase -> snake_case via the
        rename map, (2) explainability columns folded into the single
        ``reason_*`` slot the schema exposes, (3) any structural column
        the engine didn't emit is back-filled with NULL so a legacy CSV
        from a pre-explainability run still pushes cleanly.
        """
        rename = {k: v for k, v in _COL_MAP.items() if k in df.columns}
        out = df.rename(columns=rename).copy()
        out["generated_by"] = "API"

        # Source -> reason_status: 1:1 string copy (defensive truncation).
        if "Source" in df.columns:
            out["reason_status"] = df["Source"].astype(str).str.slice(0, _REASON_STATUS_MAX)
        else:
            out["reason_status"] = None

        # WhyItem + WhyQuantity collapse into reason_explanation. The DB
        # only has one explanation slot; concatenating preserves both
        # signals in a single sentence the UI / auditor can read.
        why_item = df["WhyItem"].astype(str) if "WhyItem" in df.columns else pd.Series([""] * len(df))
        why_qty = df["WhyQuantity"].astype(str) if "WhyQuantity" in df.columns else pd.Series([""] * len(df))
        composed = (why_item.fillna("") + " | " + why_qty.fillna("")).str.strip(" |")
        out["reason_explanation"] = composed.str.slice(0, _REASON_EXPLANATION_MAX)

        # Confidence is 0..1 float in the engine; reason_confidence is INT
        # 0..100 in the schema -> rescale.
        if "Confidence" in df.columns:
            conf = pd.to_numeric(df["Confidence"], errors="coerce").fillna(0.0)
            out["reason_confidence"] = (conf.clip(0.0, 1.0) * 100).round().astype(int)
        else:
            out["reason_confidence"] = 0

        for col in _DB_COLUMNS:
            if col not in out.columns:
                out[col] = None
        return out
