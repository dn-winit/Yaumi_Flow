"""Accuracy service: joins predicted (YaumiAIML) with live actuals (YaumiLive).

Aggregation: GROUP BY (TrxDate, RouteCode, ItemCode), SUM positive QuantityInPCs.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

import numpy as np
import pandas as pd

from demand_forecasting_pipeline.config.settings import Settings, get_settings
from common.db_pool import (
    FATAL_DB_ERRORS,
    get_pool,
    with_db_retry,
)
from demand_forecasting_pipeline.src.evaluation.metrics import (
    composite_kwargs_from_yaml,
    composite_summary,
)

logger = logging.getLogger(__name__)

# accuracy_pct=None = "no honest number"; real 0% stays a real 0%.
# Two lenses: model_accuracy_pct (raw forecast vs actual, drift-comparable to baseline)
# and reconciled_accuracy_pct (V5_b van-load vs actual, operational lift).
# accuracy_pct / wape are backward-compat aliases of the model lens.
_EMPTY_SUMMARY: dict[str, Any] = {
    "rows_compared": 0,
    "total_predicted": 0.0,
    "total_actual": 0.0,
    "mae": None,
    "rmse": None,
    "wape": None,
    "accuracy_pct": None,
    "model_wape": None,
    "model_accuracy_pct": None,
    "reconciled_wape": None,
    "reconciled_accuracy_pct": None,
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

    # Errors that should NEVER be retried; re-export for back-compat with prior class-level alias.
    _FATAL_ERRORS = FATAL_DB_ERRORS

    def _query(self, conn_str: str, sql: str, params: list) -> pd.DataFrame:
        """SQL -> DataFrame via shared pool; query_timeout-bounded, transient retries via db_pool."""
        cfg = self._s.db
        pool = get_pool(
            conn_str,
            max_connections=max(int(getattr(cfg, "retry_attempts", 1)) + 1, 4),
            connect_timeout=int(getattr(cfg, "connection_timeout", 30)),
            query_timeout=int(getattr(cfg, "query_timeout", 60)),
            autocommit=False,
        )

        @with_db_retry
        def _run() -> pd.DataFrame:
            with pool.acquire() as conn:
                cursor = conn.cursor()
                cursor.execute(sql, params)
                cols = [d[0] for d in cursor.description]
                return pd.DataFrame.from_records(cursor.fetchall(), columns=cols)

        try:
            return _run()
        except FATAL_DB_ERRORS as exc:
            logger.error(
                "AccuracyService fatal DB error (%s): %s",
                type(exc).__name__, exc,
            )
            raise

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
        limit: Optional[int] = 5000,
    ) -> dict[str, Any]:
        """Per-(date, route, item) rows with predicted + live actual.

        ``limit=None`` returns every prediction in window (drift MUST pass None).
        """
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

        # Settlement-window guard: drop rows within N days of today (in-flight invoices).
        # settlement_window_days=0 disables (dev fixtures).
        settle = int(getattr(self._s, "accuracy_settlement_window_days", 2))
        if settle > 0 and not merged.empty:
            # Cutoff anchored to reconciliation_refresh_timezone (business day, not UTC).
            biz_tz = getattr(
                self._s, "reconciliation_refresh_timezone", "UTC",
            ) or "UTC"
            try:
                now_biz = pd.Timestamp.now(tz=biz_tz)
            except Exception:
                # Bad tz string -- fall back to UTC + warn; don't crash on config typo.
                logger.warning(
                    "accuracy: reconciliation_refresh_timezone=%r is not a "
                    "valid IANA zone; falling back to UTC for settlement "
                    "cutoff", biz_tz,
                )
                now_biz = pd.Timestamp.now(tz="UTC")
            cutoff = (
                now_biz.tz_convert(None).normalize() - pd.Timedelta(days=settle)
            )
            merged_dt = pd.to_datetime(merged["trx_date"], errors="coerce")
            # Log NaT rows separately so date corruption doesn't hide inside "unsettled".
            nat_count = int(merged_dt.isna().sum())
            if nat_count:
                logger.warning(
                    "accuracy: %d rows have unparseable trx_date "
                    "(treated as unsettled and dropped)",
                    nat_count,
                )
            settled_mask = merged_dt <= cutoff
            dropped = int((~settled_mask).sum())
            unsettled = dropped - nat_count
            if unsettled > 0:
                logger.info(
                    "accuracy: dropped %d unsettled rows (trx_date > %s; "
                    "settlement_window_days=%d)",
                    unsettled, cutoff.date(), settle,
                )
            merged = merged.loc[settled_mask].reset_index(drop=True)

        # Substitute raw forecast with reconciled van load for variance/WAPE/accuracy
        # (measures recommendation vs reality, not abstract model). Falls back to raw on engine miss.
        merged["forecast_raw"] = merged["predicted"].astype(float)
        merged = self._reconcile_predicted(merged)

        merged["variance"] = merged["actual_qty"] - merged["predicted"]
        merged["variance_pct"] = np.where(
            merged["predicted"] > 0,
            merged["variance"] / merged["predicted"] * 100,
            0.0,
        )

        # JSON-safe: NaN / +/-Inf -> None for strict consumers.
        json_safe = merged.replace([np.nan, np.inf, -np.inf], None)
        return {
            "success": True,
            "rows": json_safe.to_dict("records"),
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
        limit: Optional[int],
    ) -> pd.DataFrame:
        # limit=None skips the SQL TOP cap (drift/audit); paged UI keeps the cap.
        top_clause = f"TOP {int(limit)}" if limit is not None else ""
        sql = f"""
            SELECT {top_clause}
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

    # Reconciliation wrapper; drift accuracy = van-load accuracy (not raw model).

    @staticmethod
    def _reconcile_predicted(df: pd.DataFrame) -> pd.DataFrame:
        """Overwrite ``predicted`` with V5_b reconciled load via canonical engine helper."""
        if df.empty or "predicted" not in df.columns:
            return df
        from demand_forecasting_pipeline.services.reconciliation import enrich_with_load
        out = enrich_with_load(
            df,
            route_col="route_code",
            item_col="item_code",
            date_col="trx_date",
            predicted_col="predicted",
            output_col="_reconciled",
        )
        if "_reconciled" in out.columns:
            out = out.copy()
            out["predicted"] = out["_reconciled"].astype(float)
            out = out.drop(columns=["_reconciled"])
        return out

    # ------------------------------------------------------------------
    # Summary
    # ------------------------------------------------------------------

    def _compute_summary(self, df: pd.DataFrame) -> dict[str, Any]:
        """Score under both lenses: model (raw, baseline-comparable) + reconciled (operational).

        Tolerances from same pipeline YAML as training baseline; both computed server-side.
        """
        if df.empty:
            return _EMPTY_SUMMARY

        from demand_forecasting_pipeline.src.evaluation.metrics import (
            resolve_class_array,
        )
        cls_arr = resolve_class_array(df)
        kwargs = composite_kwargs_from_yaml(self._s.pipeline_config)
        actual = df["actual_qty"].to_numpy()
        reconciled = composite_summary(actual, df["predicted"].to_numpy(), cls_arr, **kwargs)
        # forecast_raw populated pre-reconciliation; defensive fallback for unit tests.
        if "forecast_raw" in df.columns:
            model = composite_summary(actual, df["forecast_raw"].to_numpy(), cls_arr, **kwargs)
        else:
            model = reconciled

        # Side metrics (mae/rmse) on reconciled, both-positive subset (same as composite numerator).
        scored = df[(df["actual_qty"] > 0) & (df["predicted"] > 0)]
        if scored.empty:
            return {
                **_EMPTY_SUMMARY,
                "rows_compared": int(len(df)),
                "total_predicted": round(reconciled["total_predicted"], 1),
                "total_actual": round(reconciled["total_actual"], 1),
            }
        diff = scored["actual_qty"] - scored["predicted"]

        return {
            "rows_compared": int(len(df)),
            "total_predicted": round(reconciled["total_predicted"], 1),
            "total_actual": round(reconciled["total_actual"], 1),
            "mae": round(float(diff.abs().mean()), 2),
            "rmse": round(float((diff ** 2).mean() ** 0.5), 2),
            # Two lenses, both pre-rounded by composite_summary.
            "model_wape":              model["wape"],
            "model_accuracy_pct":      model["accuracy_pct"],
            "reconciled_wape":         reconciled["wape"],
            "reconciled_accuracy_pct": reconciled["accuracy_pct"],
            # Legacy keys carry the model lens (baseline-comparable).
            "wape":         model["wape"],
            "accuracy_pct": model["accuracy_pct"],
        }
