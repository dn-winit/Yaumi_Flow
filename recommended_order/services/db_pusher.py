"""
Pushes recommendations to yf_recommended_orders in YaumiAIML.
Column mapping matches scripts/create_tables.sql exactly.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any, Dict, Optional

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


class DbPusher:
    """Pushes recommendation data to yf_recommended_orders."""

    def __init__(self, settings: Optional[Settings] = None) -> None:
        self._s = settings or get_settings()
        self._db = self._s.db

    @property
    def available(self) -> bool:
        return bool(self._db.host and self._db.username and self._s.recommendation_table)

    def _connect(self) -> pyodbc.Connection:
        return pyodbc.connect(self._db.aiml_connection_string, autocommit=False)

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

        # 1. Rename the structural columns (PascalCase -> snake_case).
        rename = {k: v for k, v in _COL_MAP.items() if k in df.columns}
        db_df = df.rename(columns=rename).copy()
        db_df["generated_by"] = "API"

        # 2. Compose the reason_* columns from the explainability fields the
        # engine emits. We cannot rely on the rename map for these because:
        #   * Source -> reason_status is a 1:1 string copy (truncate-safe)
        #   * WhyItem + WhyQuantity collapse into reason_explanation
        #     (the DB only has one explanation slot; concatenating preserves
        #     both signals in a single sentence the UI / auditor can read)
        #   * Confidence is 0..1 float in the engine but reason_confidence
        #     is INT in the schema -> rescale to 0..100 percent.
        # Each derivation defends against a missing source column so a
        # legacy CSV (from a pre-explainability run) still pushes cleanly.
        if "Source" in df.columns:
            db_df["reason_status"] = df["Source"].astype(str).str.slice(0, _REASON_STATUS_MAX)
        else:
            db_df["reason_status"] = None

        why_item = df["WhyItem"].astype(str) if "WhyItem" in df.columns else pd.Series([""] * len(df))
        why_qty = df["WhyQuantity"].astype(str) if "WhyQuantity" in df.columns else pd.Series([""] * len(df))
        composed = (why_item.fillna("") + " | " + why_qty.fillna("")).str.strip(" |")
        db_df["reason_explanation"] = composed.str.slice(0, _REASON_EXPLANATION_MAX)

        if "Confidence" in df.columns:
            conf = pd.to_numeric(df["Confidence"], errors="coerce").fillna(0.0)
            db_df["reason_confidence"] = (conf.clip(0.0, 1.0) * 100).round().astype(int)
        else:
            db_df["reason_confidence"] = 0

        # 3. Backfill any structural column the engine didn't emit. This only
        # bites when the upstream schema is intentionally smaller (e.g. a
        # recovery push from an old CSV); a current run hits every column.
        for col in _DB_COLUMNS:
            if col not in db_df.columns:
                db_df[col] = None

        placeholders = ", ".join("?" for _ in _DB_COLUMNS)
        col_list = ", ".join(f"[{c}]" for c in _DB_COLUMNS)
        insert_sql = f"INSERT INTO {table} ({col_list}) VALUES ({placeholders})"

        # 4. DELETE matches by date (and optional route) so a re-run is
        # idempotent: same key set lands the same rows. The transaction
        # below makes DELETE+INSERT atomic.
        delete_sql = f"DELETE FROM {table} WHERE [trx_date] >= ? AND [trx_date] < DATEADD(day, 1, ?)"
        delete_params = [date, date]
        if route_code:
            delete_sql += " AND [route_code] = ?"
            delete_params.append(route_code)

        records = [
            tuple(None if pd.isna(v) else (v.item() if hasattr(v, "item") else v) for v in row)
            for row in db_df[_DB_COLUMNS].values.tolist()
        ]

        conn: Optional[pyodbc.Connection] = None
        try:
            conn = self._connect()
            conn.timeout = self._db.query_timeout  # pyodbc query timeout lives on the connection
            cursor = conn.cursor()
            cursor.fast_executemany = True
            # DELETE + chunked INSERT in one transaction so a partial
            # INSERT failure rolls the DELETE back too -- no orphaned
            # gaps, idempotent on retry.
            cursor.execute(delete_sql, delete_params)
            for i in range(0, len(records), 1000):
                cursor.executemany(insert_sql, records[i : i + 1000])
            conn.commit()

            duration = round(time.time() - t0, 2)
            logger.info("Pushed %d recs to %s for %s in %.1fs", len(records), table, date, duration)
            return {"success": True, "table": table, "rows": len(records), "duration_seconds": duration}
        except Exception as exc:
            logger.error("Push failed for %s/%s: %s", date, route_code or "ALL", exc)
            if conn is not None:
                try:
                    conn.rollback()
                except Exception:
                    pass
            return {"success": False, "error": str(exc)}
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
