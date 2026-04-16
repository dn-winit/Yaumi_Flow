"""
Accuracy service -- joins predicted (yf_demand_forecast in YaumiAIML)
with live actual sales (VW_GET_SALES_DETAILS in YaumiLive).

Aggregation matches the pipeline grouping exactly:
GROUP BY (TrxDate, RouteCode, ItemCode), SUM positive QuantityInPCs.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

import numpy as np
import pandas as pd
import pyodbc

from demand_forecasting_pipeline.config.settings import Settings, get_settings

logger = logging.getLogger(__name__)


_EMPTY_SUMMARY: Dict[str, Any] = {
    "rows_compared": 0,
    "total_predicted": 0.0,
    "total_actual": 0.0,
    "mae": 0.0,
    "rmse": 0.0,
    "mape": 0.0,
    "accuracy_pct": 0.0,
}


class AccuracyService:
    """Cross-DB query: predicted from YaumiAIML + actual from YaumiLive."""

    def __init__(self, settings: Optional[Settings] = None) -> None:
        self._s = settings or get_settings()

    @property
    def available(self) -> bool:
        return self._s.db.configured and self._s.live_db_configured and bool(self._s.demand_table)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _query(self, conn_str: str, sql: str, params: list) -> pd.DataFrame:
        """Execute a SQL query and return a DataFrame. Raises on connection failure."""
        with pyodbc.connect(conn_str, autocommit=False) as conn:
            cursor = conn.cursor()
            cursor.execute(sql, params)
            cols = [d[0] for d in cursor.description]
            return pd.DataFrame.from_records(cursor.fetchall(), columns=cols)

    @staticmethod
    def _normalize(df: pd.DataFrame) -> pd.DataFrame:
        """Apply consistent typing for join keys."""
        if df.empty:
            return df
        df["route_code"] = df["route_code"].astype(str).str.strip()
        df["item_code"] = df["item_code"].astype(str).str.strip()
        df["trx_date"] = pd.to_datetime(df["trx_date"]).dt.strftime("%Y-%m-%d")
        return df

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    def get_comparison(
        self,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        route_code: Optional[str] = None,
        item_code: Optional[str] = None,
        limit: int = 5000,
    ) -> Dict[str, Any]:
        """Return per-(date, route, item) rows with predicted + live actual."""
        if not self.available:
            return {
                "success": False,
                "error": "Configure DF_DB_* (YaumiAIML) and DF_LIVE_DB_* (YaumiLive)",
                "rows": [],
                "summary": _EMPTY_SUMMARY,
            }

        try:
            pred_df = self._fetch_predicted(start_date, end_date, route_code, item_code, limit)
        except Exception as exc:
            logger.error("Predicted fetch failed: %s", exc)
            return {"success": False, "error": f"Predicted fetch failed: {exc}", "rows": [], "summary": _EMPTY_SUMMARY}

        if pred_df.empty:
            return {"success": True, "rows": [], "summary": _EMPTY_SUMMARY}

        try:
            actual_df = self._fetch_actuals(pred_df, route_code, item_code)
        except Exception as exc:
            logger.error("Actuals fetch failed: %s", exc)
            return {"success": False, "error": f"Actuals fetch failed: {exc}", "rows": [], "summary": _EMPTY_SUMMARY}

        merged = pred_df.merge(actual_df, on=["trx_date", "route_code", "item_code"], how="left")
        merged["actual_qty"] = merged["actual_qty"].fillna(0).astype(float)
        merged["variance"] = merged["actual_qty"] - merged["predicted"]
        # Vectorized: variance% = variance / predicted * 100, 0 when predicted==0
        merged["variance_pct"] = np.where(
            merged["predicted"] > 0,
            merged["variance"] / merged["predicted"] * 100,
            0.0,
        )

        return {
            "success": True,
            "rows": merged.to_dict("records"),
            "summary": self._compute_summary(merged),
        }

    # ------------------------------------------------------------------
    # Fetch helpers
    # ------------------------------------------------------------------

    def _fetch_predicted(
        self,
        start_date: Optional[str],
        end_date: Optional[str],
        route_code: Optional[str],
        item_code: Optional[str],
        limit: int,
    ) -> pd.DataFrame:
        sql = f"""
            SELECT TOP {int(limit)}
                CAST(trx_date AS DATE) AS trx_date,
                route_code, item_code, item_name, demand_class, model_used,
                predicted, lower_bound, upper_bound
            FROM {self._s.demand_table} WITH (NOLOCK)
            WHERE 1=1
        """
        params: list = []
        if start_date:
            sql += " AND trx_date >= ?"
            params.append(start_date)
        if end_date:
            sql += " AND trx_date <= ?"
            params.append(end_date)
        if not start_date and not end_date:
            sql += " AND trx_date >= DATEADD(day, -30, GETDATE())"
        if route_code:
            sql += " AND route_code = ?"
            params.append(route_code)
        if item_code:
            sql += " AND item_code = ?"
            params.append(item_code)
        sql += " ORDER BY trx_date, route_code, item_code"

        return self._normalize(self._query(self._s.db.connection_string(), sql, params))

    def _fetch_actuals(
        self,
        pred_df: pd.DataFrame,
        route_code: Optional[str],
        item_code: Optional[str],
    ) -> pd.DataFrame:
        """Pull actuals from YaumiLive with EXACT pipeline aggregation."""
        # Use the date range from predicted to scope the actuals query
        min_date = pred_df["trx_date"].min()
        max_date = pred_df["trx_date"].max()

        if route_code:
            routes = [route_code]
        else:
            routes = self._s.live_route_codes or sorted(pred_df["route_code"].unique().tolist())

        if not routes:
            return pd.DataFrame(columns=["trx_date", "route_code", "item_code", "actual_qty"])

        ph = ",".join("?" for _ in routes)
        sql = f"""
            SELECT
                CAST(TrxDate AS DATE) AS trx_date,
                RouteCode AS route_code,
                ItemCode AS item_code,
                SUM(CASE WHEN QuantityInPCs > 0 THEN QuantityInPCs ELSE 0 END) AS actual_qty
            FROM {self._s.live_sales_view} WITH (NOLOCK)
            WHERE ItemType = 'OrderItem'
              AND TrxType  = 'SalesInvoice'
              AND RouteCode IN ({ph})
              AND TrxDate >= ?
              AND TrxDate <= ?
        """
        params: list = list(routes) + [min_date, max_date]

        if item_code:
            sql += " AND ItemCode = ?"
            params.append(item_code)

        sql += " GROUP BY CAST(TrxDate AS DATE), RouteCode, ItemCode"

        df = self._normalize(self._query(self._s.live_connection_string(), sql, params))
        if not df.empty:
            df["actual_qty"] = df["actual_qty"].astype(float)
        return df

    # ------------------------------------------------------------------
    # Summary
    # ------------------------------------------------------------------

    @staticmethod
    def _compute_summary(df: pd.DataFrame) -> Dict[str, Any]:
        if df.empty:
            return _EMPTY_SUMMARY

        # Only score rows where actual occurred (otherwise MAPE is undefined)
        scored = df[df["actual_qty"] > 0]
        total_pred = round(float(df["predicted"].sum()), 1)
        total_actual = round(float(df["actual_qty"].sum()), 1)

        if scored.empty:
            return {**_EMPTY_SUMMARY, "rows_compared": int(len(df)), "total_predicted": total_pred}

        diff = scored["actual_qty"] - scored["predicted"]
        mape = float((diff.abs() / scored["actual_qty"]).mean() * 100)
        return {
            "rows_compared": int(len(df)),
            "total_predicted": total_pred,
            "total_actual": total_actual,
            "mae": round(float(diff.abs().mean()), 2),
            "rmse": round(float((diff ** 2).mean() ** 0.5), 2),
            "mape": round(mape, 2),
            "accuracy_pct": round(max(0.0, 100.0 - mape), 1),
        }
