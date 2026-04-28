"""
Pushes forecast predictions to yf_demand_forecast in YaumiAIML.
Column mapping matches scripts/create_tables.sql exactly.

Source-side column names (the ones in the prediction CSV) are read from the
pipeline YAML so the wire-format contract follows ``data.{date_col,
target_col, forecast_level}`` automatically — no hardcoded strings drift if
the config is renamed.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, Optional

import pandas as pd
import pyodbc

from demand_forecasting_pipeline.config.settings import Settings, get_settings
from demand_forecasting_pipeline.src.utils.config_loader import load_config

logger = logging.getLogger(__name__)

# Exact column order matching yf_demand_forecast (excludes id, created_at)
_DB_COLUMNS = [
    "trx_date", "route_code", "item_code", "item_name",
    "data_split", "demand_class", "model_used",
    "predicted", "p_demand", "qty_if_demand", "actual_qty",
    "lower_bound", "upper_bound",
    "adi", "cv2", "nonzero_ratio", "mean_qty", "avg_gap_days",
]


class DbPusher:
    """Pushes prediction CSVs to yf_demand_forecast."""

    def __init__(self, settings: Optional[Settings] = None) -> None:
        self._s = settings or get_settings()
        self._db = self._s.db
        # Source-side column names from pipeline config. Falling back to the
        # historical defaults keeps the service usable even when the YAML is
        # absent (e.g. in unit tests that don't ship a config).
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
        return self._db.configured and bool(self._s.demand_table)

    def _connect(self) -> pyodbc.Connection:
        return pyodbc.connect(self._db.connection_string(), autocommit=False)

    def push_predictions(self, datasplit: str = "forecast") -> Dict[str, Any]:
        if not self.available:
            return {"success": False, "error": "DB not configured (set DF_DB_HOST + DF_DEMAND_TABLE)"}

        path = self._s.predictions_path(
            self._s.future_forecast_file if datasplit == "forecast" else self._s.test_predictions_file
        )
        if not path.exists():
            return {"success": False, "error": f"File not found: {path}"}

        raw = pd.read_csv(path, low_memory=False)
        if raw.empty:
            return {"success": False, "error": "File is empty"}

        df = self._map_columns(raw, datasplit)
        return self._insert(df, datasplit)

    def _map_columns(self, raw: pd.DataFrame, datasplit: str) -> pd.DataFrame:
        out = pd.DataFrame()
        out["trx_date"] = raw.get(self._src_date_col, "")
        out["route_code"] = raw.get(self._src_route_col, "").astype(str)
        out["item_code"] = raw.get(self._src_item_col, "").astype(str)
        out["item_name"] = raw.get("ItemName", "")
        out["data_split"] = datasplit.capitalize()
        out["demand_class"] = raw.get("class", "")
        out["model_used"] = raw.get("model_used", raw.get("best_model", ""))
        out["predicted"] = raw.get("prediction", 0.0)
        out["p_demand"] = raw.get("p_demand", 0.0)
        out["qty_if_demand"] = raw.get("qty_if_demand", 0.0)
        out["actual_qty"] = raw.get(self._src_target_col, 0.0)
        out["lower_bound"] = raw.get("q_10", 0.0)
        out["upper_bound"] = raw.get("q_90", 0.0)
        out["adi"] = raw.get("adi", None)
        out["cv2"] = raw.get("cv2", None)
        out["nonzero_ratio"] = raw.get("nonzero_ratio", None)
        out["mean_qty"] = raw.get("mean_qty", None)
        # ``avg_gap_days`` is now produced in genuine day units by the
        # explainability stage (granularity-corrected). Pass straight through.
        out["avg_gap_days"] = raw.get("avg_gap_days", None)
        return out

    def _insert(self, df: pd.DataFrame, datasplit: str) -> Dict[str, Any]:
        table = self._s.demand_table
        t0 = time.time()

        placeholders = ", ".join("?" for _ in _DB_COLUMNS)
        col_list = ", ".join(f"[{c}]" for c in _DB_COLUMNS)
        insert_sql = f"INSERT INTO {table} ({col_list}) VALUES ({placeholders})"
        delete_sql = f"DELETE FROM {table} WHERE [data_split] = ?"

        records = [
            tuple(None if pd.isna(v) else (v.item() if hasattr(v, "item") else v) for v in row)
            for row in df[_DB_COLUMNS].values.tolist()
        ]

        last_error: Optional[str] = None
        for attempt in range(1, self._db.retry_attempts + 1):
            conn: Optional[pyodbc.Connection] = None
            try:
                conn = self._connect()
                cursor = conn.cursor()
                cursor.fast_executemany = True
                cursor.timeout = self._db.query_timeout
                # DELETE + chunked INSERT in one transaction; rollback on
                # any failure keeps the table in its pre-push state.
                cursor.execute(delete_sql, (datasplit.capitalize(),))
                for i in range(0, len(records), 1000):
                    cursor.executemany(insert_sql, records[i : i + 1000])
                conn.commit()
                duration = round(time.time() - t0, 2)
                logger.info(
                    "Pushed %d rows to %s (split=%s) in %.1fs",
                    len(records), table, datasplit, duration,
                )
                return {
                    "success": True,
                    "table": table,
                    "rows": len(records),
                    "duration_seconds": duration,
                }
            except Exception as exc:
                last_error = str(exc)
                logger.error("Push attempt %d failed: %s", attempt, exc)
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
                    except Exception:
                        pass

        return {"success": False, "error": last_error or "All push attempts failed"}
