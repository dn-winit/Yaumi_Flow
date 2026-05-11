"""
Accuracy service -- joins predicted (yf_demand_forecast in YaumiAIML)
with live actual sales (VW_GET_SALES_DETAILS in YaumiLive).

Aggregation matches the pipeline grouping exactly:
GROUP BY (TrxDate, RouteCode, ItemCode), SUM positive QuantityInPCs.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

import numpy as np
import pandas as pd
import pyodbc

from demand_forecasting_pipeline.config.settings import Settings, get_settings
from demand_forecasting_pipeline.services.db_pool import (
    FATAL_DB_ERRORS,
    get_pool,
    with_db_retry,
)
from demand_forecasting_pipeline.src.evaluation.metrics import (
    composite_kwargs_from_yaml,
    composite_summary,
)

logger = logging.getLogger(__name__)

# ``accuracy_pct = None`` is the "no honest number" signal -- mirrors the
# Pipeline summary endpoint's nullable convention so callers (drift,
# dashboard) never have to disambiguate "0% accurate" from "no data
# scored". A real 0% is still a real 0%; absence is None.
#
# Two accuracy lenses are emitted side by side:
#   * ``model_accuracy_pct``      -- raw model forecast vs invoiced sales.
#                                    Apples-to-apples with the
#                                    training-time baseline (which also
#                                    scores raw forecast vs actual on the
#                                    held-out test split). Drift uses
#                                    this so recent vs baseline measures
#                                    the same thing on both sides.
#   * ``reconciled_accuracy_pct`` -- V5_b reconciled van-load vs
#                                    invoiced sales. The operational
#                                    metric: "did the rep load the right
#                                    amount". Captures the lift from
#                                    post-forecast reconciliation.
#
# ``accuracy_pct`` / ``wape`` remain as backward-compatible aliases of
# the model lens so any legacy caller that read them gets the honest,
# baseline-comparable number.
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

    # Errors that should NEVER be retried -- bad SQL / bad data won't
    # become right by waiting. Re-export from db_pool so the class still
    # surfaces ``_FATAL_ERRORS`` for any external caller that referenced
    # it before the pool consolidation.
    _FATAL_ERRORS = FATAL_DB_ERRORS

    def _query(self, conn_str: str, sql: str, params: list) -> pd.DataFrame:
        """Execute a SQL query and return a DataFrame via the shared pool.

        Honest production semantics:
          * Cursor timeout = ``db.query_timeout`` -- a hung warehouse
            never blocks the request thread indefinitely.
          * Transient errors retried with exponential back-off via
            ``with_db_retry`` (centralised in db_pool).
          * Programming / data errors fail fast.
        """
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
        """Return per-(date, route, item) rows with predicted + live actual.

        ``limit=None`` returns every prediction in the window (no SQL TOP
        cap). Drift detection MUST pass ``None`` -- otherwise the summary
        is computed on a truncated sample and ``recent_accuracy`` drifts
        from the true number on routes/dates that scroll past the cap.
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

        # Substitute the raw forecast with the **reconciled van load**
        # before computing variance / WAPE / accuracy. This makes every
        # downstream accuracy metric (drift, recent-accuracy tile,
        # comparison rows) measure "how well did our recommendation match
        # reality" rather than "how well did the abstract model predict".
        # Baseline accuracy stays raw -- computed separately on test set.
        # Falls back transparently to raw ``predicted`` if the engine is
        # unavailable, so the path never goes blank.
        merged["forecast_raw"] = merged["predicted"].astype(float)
        merged = self._reconcile_predicted(merged)

        merged["variance"] = merged["actual_qty"] - merged["predicted"]
        merged["variance_pct"] = np.where(
            merged["predicted"] > 0,
            merged["variance"] / merged["predicted"] * 100,
            0.0,
        )

        # JSON-safe conversion: NaN / +/-Inf become None so the FastAPI
        # response survives strict JSON consumers.
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
        # ``limit is None`` skips the SQL TOP cap entirely -- callers that
        # need the complete window (drift comparison, audit) pass None;
        # paged UI calls keep a finite cap.
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

    # ------------------------------------------------------------------
    # Reconciliation -- thin wrapper around the canonical helper. Drift
    # accuracy reflects "did we load the right amount" (van-load
    # accuracy), not raw model accuracy. Baseline is computed elsewhere.
    # ------------------------------------------------------------------

    @staticmethod
    def _reconcile_predicted(df: pd.DataFrame) -> pd.DataFrame:
        """Replace each row's ``predicted`` with its V5_b reconciled value
        using the single canonical helper. Returns the frame untouched if
        any required column is missing (engine fallback handled inside).
        """
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
        """Score the merged comparison frame under both lenses.

        Both ``predicted`` (V5_b reconciled van-load) and ``forecast_raw``
        (raw model output) are present on the frame -- ``get_comparison``
        captures the raw column before reconciliation overwrites
        ``predicted``. Each gets its own composite_summary call so:

          * ``model_accuracy_pct`` is comparable to the training-time
            baseline (raw model vs actual on held-out test cells).
          * ``reconciled_accuracy_pct`` is the operational "did we load
            the right amount" metric.

        Tolerances come from the SAME pipeline YAML the training-time
        baseline uses (resolved via ``composite_kwargs_from_yaml``), so a
        config-driven tolerance change automatically applies to drift
        scoring -- no silent divergence from baseline.

        Computing both server-side is cheap (the heavy work is the
        cross-DB join already done) and it keeps the frontend a pure
        renderer -- no duplicated math, no "which column to score"
        decision in two places.
        """
        if df.empty:
            return _EMPTY_SUMMARY

        cls_arr = (
            df["demand_class"].astype(str).to_numpy()
            if "demand_class" in df.columns else None
        )
        kwargs = composite_kwargs_from_yaml(self._s.pipeline_config)
        actual = df["actual_qty"].to_numpy()
        reconciled = composite_summary(actual, df["predicted"].to_numpy(), cls_arr, **kwargs)
        # ``forecast_raw`` is captured pre-reconciliation in get_comparison.
        # If absent (defensive), the model lens degenerates to the
        # reconciled one rather than crashing -- but get_comparison always
        # populates it, so this branch is for unit-test safety only.
        if "forecast_raw" in df.columns:
            model = composite_summary(actual, df["forecast_raw"].to_numpy(), cls_arr, **kwargs)
        else:
            model = reconciled

        # Side metrics (mae / rmse) are scored against the reconciled
        # number -- they describe operational error magnitude, which is
        # what the comparison-row UI used them for. Same both-positive
        # subset as the composite numerator, no recompute.
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
            # Backward-compat aliases: the legacy keys carry the model
            # lens (the honest, baseline-comparable number) so any caller
            # that still reads ``accuracy_pct`` gets the apples-to-apples
            # value rather than the inflated reconciled one.
            "wape":         model["wape"],
            "accuracy_pct": model["accuracy_pct"],
        }
