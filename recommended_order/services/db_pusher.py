"""Pushes recommendations to yf_recommended_orders in YaumiAIML.
Column mapping matches scripts/create_tables.sql exactly."""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional

import pandas as pd

# Shared AIML pool factory so a single semaphore bounds connections across services.
from common.db_pool import (
    FATAL_DB_ERRORS as _POOL_FATAL_ERRORS,
    get_pool,
)
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

# PascalCase DF -> snake_case DB. Explainability columns are folded into
# ``reason_*`` by ``_push`` (needs type conversion + composition).
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

# DB column length caps (matches scripts/create_tables.sql); truncation
# prevents INSERT errors on long explanations.
_REASON_STATUS_MAX = 100
_REASON_EXPLANATION_MAX = 500

# Pre-built SQL fragments (column list + placeholders are schema-determined).
_INSERT_COLS = ", ".join(f"[{c}]" for c in _DB_COLUMNS)
_INSERT_PLACEHOLDERS = ", ".join("?" for _ in _DB_COLUMNS)
_INSERT_SQL_TPL = f"INSERT INTO {{table}} ({_INSERT_COLS}) VALUES ({_INSERT_PLACEHOLDERS})"
# DELETE acquires range locks via the hint clause so the DELETE+INSERT
# transaction window is invisible to concurrent readers.
_DELETE_SQL_BASE = (
    "DELETE FROM {table}{hint_clause} "
    "WHERE [trx_date] >= ? AND [trx_date] < DATEADD(day, 1, ?)"
)
_DELETE_ROUTE_SUFFIX = " AND [route_code] = ?"
# Allowlist regexes so a misconfigured env var can't inject SQL.
_LOCK_HINTS_RE = __import__("re").compile(r"^[A-Z_, ]+$")
_ISOLATION_RE = __import__("re").compile(r"^[A-Z ]+$")


def _dataframe_to_records(df: pd.DataFrame, cols: List[str]) -> List[tuple]:
    """Project ``df`` to ``cols`` as a list of plain Python tuples for
    ``cursor.executemany``; NaN -> None, numpy scalars unboxed via .item()."""
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

    def _pool(self):
        """Lazy-resolve the shared AIML pool; sized ``retry_attempts + 1``
        (floor 4) so retries can't starve concurrent writers."""
        return get_pool(
            self._db.aiml_connection_string,
            max_connections=max(int(self._db.retry_attempts) + 1, 4),
            connect_timeout=int(self._db.connection_timeout),
            query_timeout=int(self._db.query_timeout),
            autocommit=False,
        )

    def push_dataframe(self, df: pd.DataFrame, date: str, route_code: str) -> Dict[str, Any]:
        """Push a DataFrame directly (called after generation)."""
        if not self.available:
            return {"success": False, "error": "DB not configured"}
        return self._push(df, date, route_code)

    def _push(self, df: pd.DataFrame, date: str, route_code: Optional[str]) -> Dict[str, Any]:
        table = self._s.recommendation_table
        t0 = time.time()

        db_df = self._project_to_schema(df)

        # Settings-driven isolation + hints (allowlisted). Empty -> disabled.
        isolation = (self._db.merge_isolation_level or "").strip().upper()
        if isolation and not _ISOLATION_RE.match(isolation):
            raise ValueError(
                f"merge_isolation_level must contain only uppercase letters "
                f"and spaces; got {self._db.merge_isolation_level!r}"
            )
        lock_hints = (self._db.merge_target_lock_hints or "").strip()
        if lock_hints and not _LOCK_HINTS_RE.match(lock_hints):
            raise ValueError(
                f"merge_target_lock_hints must contain only uppercase letters "
                f"and ``,`` / spaces; got {self._db.merge_target_lock_hints!r}"
            )
        hint_clause = f" WITH ({lock_hints})" if lock_hints else ""
        insert_sql = _INSERT_SQL_TPL.format(table=table)
        # DELETE+chunked INSERT in one txn so partial INSERT failures roll
        # back the DELETE; re-runs are idempotent on the (date[, route]) key.
        delete_sql = _DELETE_SQL_BASE.format(table=table, hint_clause=hint_clause)
        delete_params: List[Any] = [date, date]
        if route_code:
            delete_sql += _DELETE_ROUTE_SUFFIX
            delete_params.append(route_code)

        records = _dataframe_to_records(db_df, _DB_COLUMNS)
        chunk = self._db.executemany_chunk_size

        pool = self._pool()
        last_error: Optional[str] = None
        for attempt in range(1, self._db.retry_attempts + 1):
            try:
                # pool.acquire() releases the semaphore + closes the conn on exit.
                with pool.acquire() as conn:
                    cursor = conn.cursor()
                    cursor.fast_executemany = True
                    try:
                        # Pin isolation for the DELETE+INSERT txn so
                        # readers can't see the empty window.
                        if isolation:
                            cursor.execute(f"SET TRANSACTION ISOLATION LEVEL {isolation};")
                        cursor.execute(delete_sql, delete_params)
                        for i in range(0, len(records), chunk):
                            cursor.executemany(insert_sql, records[i : i + chunk])
                        conn.commit()
                    except Exception:
                        # Rollback the DELETE on INSERT failure; if rollback
                        # itself raises, the conn is in an undefined state
                        # so log loudly before re-raising the original error.
                        try:
                            conn.rollback()
                        except Exception as rollback_exc:
                            logger.error(
                                "db_pusher rollback FAILED after write "
                                "exception (connection state undefined; "
                                "pool will close + recreate this slot): %s",
                                rollback_exc,
                            )
                        raise

                duration = round(time.time() - t0, 2)
                logger.info("Pushed %d recs to %s for %s in %.1fs", len(records), table, date, duration)
                return {"success": True, "table": table, "rows": len(records), "duration_seconds": duration}
            except _POOL_FATAL_ERRORS as exc:
                # Bad SQL / bad data -- no retry.
                last_error = f"{type(exc).__name__}: {exc}"
                logger.error("Push fatal error for %s/%s (no retry): %s",
                             date, route_code or "ALL", exc)
                break
            except Exception as exc:
                last_error = str(exc)
                logger.warning(
                    "Push attempt %d/%d failed for %s/%s (transient): %s",
                    attempt, self._db.retry_attempts, date, route_code or "ALL", exc,
                )
                if attempt < self._db.retry_attempts:
                    time.sleep(self._db.retry_delay * attempt)

        return {"success": False, "error": last_error or "All push attempts failed"}

    def _project_to_schema(self, df: pd.DataFrame) -> pd.DataFrame:
        """Translate engine CSV shape -> DB schema: rename, fold
        explainability into ``reason_*``, NULL-fill missing columns."""
        rename = {k: v for k, v in _COL_MAP.items() if k in df.columns}
        out = df.rename(columns=rename).copy()
        out["generated_by"] = "API"

        # Source -> reason_status (truncated).
        if "Source" in df.columns:
            out["reason_status"] = df["Source"].astype(str).str.slice(0, _REASON_STATUS_MAX)
        else:
            out["reason_status"] = None

        # WhyItem + WhyQuantity -> single reason_explanation slot.
        why_item = df["WhyItem"].astype(str) if "WhyItem" in df.columns else pd.Series([""] * len(df))
        why_qty = df["WhyQuantity"].astype(str) if "WhyQuantity" in df.columns else pd.Series([""] * len(df))
        composed = (why_item.fillna("") + " | " + why_qty.fillna("")).str.strip(" |")
        out["reason_explanation"] = composed.str.slice(0, _REASON_EXPLANATION_MAX)

        # Confidence 0..1 float -> reason_confidence 0..100 int.
        if "Confidence" in df.columns:
            conf = pd.to_numeric(df["Confidence"], errors="coerce").fillna(0.0)
            out["reason_confidence"] = (conf.clip(0.0, 1.0) * 100).round().astype(int)
        else:
            out["reason_confidence"] = 0

        for col in _DB_COLUMNS:
            if col not in out.columns:
                out[col] = None
        return out
