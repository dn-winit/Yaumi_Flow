"""Prediction endpoints -- test predictions and future forecasts.

Reconciled-only contract
------------------------
The future-forecast endpoint is the source of "what to load on the van".
Every row that leaves this endpoint goes through the V5_b reconciliation
layer first, and the row's ``prediction`` field is overwritten with the
reconciled value (``recommended_load``) before serialisation. The raw
model output never leaves the backend on this surface -- callers that
need it (training accuracy, model-quality drilldowns) read it through
``accuracy_service`` or the ``/predictions/test`` endpoint instead.

Same for ``/forecast/route-summary``: the response carries ``predicted_qty``
which is the reconciled per-route total. There is no separate ``raw``
field; the contract is that the API never returns a raw van-load number.

The enrichment is a thin loop over the page being returned -- it does
not re-fetch the forecast frame, and it caches nothing the underlying
services don't already cache (bias table is mtime-keyed, closing-stock
CSV is mtime-keyed in the van-load service).
"""
from __future__ import annotations

import logging
from typing import Optional

import pandas as pd
from fastapi import APIRouter, Depends, Query

logger = logging.getLogger(__name__)

from demand_forecasting_pipeline.api.dependencies import get_artifact_service
from demand_forecasting_pipeline.api.schemas import (
    FutureRouteSummaryResponse,
    PredictionResponse,
)
from demand_forecasting_pipeline.config.settings import get_settings
from demand_forecasting_pipeline.services.artifact_service import ArtifactService
from demand_forecasting_pipeline.services.reconciliation import enrich_with_load

router = APIRouter(prefix="/predictions", tags=["predictions"])


def _detect_predicted_col(df: pd.DataFrame) -> Optional[str]:
    """Future-forecast / test-prediction artefacts use lowercase ``prediction``;
    raw demand_forecast.csv uses PascalCase ``Predicted``. Try both."""
    for c in ("prediction", "Predicted"):
        if c in df.columns:
            return c
    return None


# ----------------------------------------------------------------------
# Endpoints
# ----------------------------------------------------------------------


@router.get("/test", response_model=PredictionResponse)
def get_test_predictions(
    route_code: Optional[str] = Query(None),
    item_code: Optional[str] = Query(None),
    limit: int = Query(
        default_factory=lambda: get_settings().default_page_limit,
        ge=1, le=100_000,
    ),
    offset: int = Query(0, ge=0),
    svc: ArtifactService = Depends(get_artifact_service),
):
    df, total = svc.get_test_predictions(route_code, item_code, limit, offset)
    return PredictionResponse(
        success=True, source="test_predictions", total=total,
        data=df.to_dict("records") if not df.empty else [],
    )


@router.get("/forecast/route-summary", response_model=FutureRouteSummaryResponse)
def get_future_route_summary(
    date: Optional[str] = Query(None, description="YYYY-MM-DD; defaults to full horizon"),
    svc: ArtifactService = Depends(get_artifact_service),
):
    """Per-route aggregates for the VanLoad page tiles.

    Contract (matches the ``VanLoadSummary`` tile so the grid card and the
    detail summary always show the **same** number):

      ``predicted_qty`` = sum of ``(opening_stock + recommended_load)``
                          across items on the route -- i.e. the rep's
                          TOTAL van load for the day (leftover already on
                          the truck + V5_b fresh-issuance to add).

      ``skus``          = distinct items physically on the truck for the
                          day -- ``(opening_stock + recommended_load) > 0``.
                          Mirrors the drill-down's carry-aware items count
                          (``page_views.py``) so the route grid and the
                          per-route page never disagree.

      ``peak_day``      = day with the highest van-load total within the
                          horizon (only meaningful when ``date`` is None;
                          when scoped to one day it equals that day).

      ``reconciled``    = True when the V5_b engine produced reconciled
                          values for every row; False when it degraded to
                          raw forecast (bias table missing, cold start).
                          The UI surfaces this as a warning chip.
    """
    fc_df = svc.van_load_view()
    pred_col = _detect_predicted_col(fc_df) if not fc_df.empty else None
    if fc_df.empty or pred_col is None:
        return FutureRouteSummaryResponse(
            date=date, routes=[], reconciled=True,
        )

    scope = fc_df.copy()
    scope["TrxDate"] = pd.to_datetime(scope["TrxDate"], errors="coerce").dt.normalize()
    if date:
        scope = scope[scope.TrxDate == pd.Timestamp(date).normalize()]
    if scope.empty:
        return FutureRouteSummaryResponse(
            date=date, routes=[], reconciled=True,
        )

    # Prefer the DB-stored reconciled values (written by the daily 03:30
    # cron and ``db_pusher`` at training time) -- zero CPU on the hot
    # path, and matches the row contract the table beneath the tile
    # serves. Detect "stored value present" via the column existing AND
    # not being uniformly zero (the migration default for un-pushed rows).
    have_stored = (
        "recommended_load" in scope.columns
        and "opening_stock" in scope.columns
        and pd.to_numeric(scope["recommended_load"], errors="coerce").fillna(0.0).abs().sum() > 0
    )
    if have_stored:
        enriched = scope
        have_recon = True
    else:
        # Lazy fallback: brand-new dates, fresh deploys, post-cron skips.
        # ``enrich_with_load`` is the same canonical function ``db_pusher``
        # and the cron use, so any value computed here is identical to
        # what the next cron tick will write back -- the gap closes
        # itself on the next refresh.
        enriched = enrich_with_load(scope, predicted_col=pred_col)
        have_recon = (
            "recommended_load" in enriched.columns
            and "opening_stock" in enriched.columns
        )

    if have_recon:
        opening = pd.to_numeric(enriched["opening_stock"], errors="coerce").fillna(0.0)
        fresh = pd.to_numeric(enriched["recommended_load"], errors="coerce").fillna(0.0)
        enriched = enriched.assign(_van_load=(opening + fresh).clip(lower=0.0))
    else:
        # Degraded path: surface the raw forecast as the headline so the
        # tile isn't blank. ``reconciled=False`` on the response tells
        # the UI to render a warning chip.
        logger.error(
            "route_summary_reconciliation_degraded -- engine outputs absent; "
            "tile will show raw forecast until bias table / engine recovers"
        )
        enriched = enriched.assign(
            _van_load=pd.to_numeric(enriched[pred_col], errors="coerce").fillna(0.0)
        )

    # "Items on the truck today" -- distinct ItemCodes with non-zero van
    # load (opening + fresh). Mirrors the drill-down's carry-aware mask
    # in ``page_views.py`` (``carry_mask = (units_to_load > 0) | (opening
    # > 0)``) so the tile's items count matches the per-route page by
    # construction. Filtering on raw ``prediction > 0`` here used to
    # double-count guard-zeroed rows (pred > 0 but engine zeroed the
    # load because the dominant buyer was off-plan) and drop pure
    # carry-only items -- both bugs collapsed into one off-by-N items
    # mismatch on the route grid.
    enriched = enriched[enriched["_van_load"] > 0]
    if enriched.empty:
        return FutureRouteSummaryResponse(
            date=date, routes=[], reconciled=have_recon,
        )

    enriched["RouteCode"] = enriched["RouteCode"].astype(str)
    by_route = (
        enriched.groupby("RouteCode", as_index=False)
        .agg(predicted_qty=("_van_load", "sum"),
             skus=("ItemCode", "nunique"))
    )

    # Peak-day per route: only meaningful when the response sums multiple
    # days. When ``date`` is given, peak_day is just that date.
    peak_by_route: dict[str, Optional[str]] = {}
    if date:
        for r in by_route.itertuples(index=False):
            peak_by_route[str(r.RouteCode)] = date
    elif "TrxDate" in enriched.columns:
        per_day = (
            enriched.groupby(["RouteCode", "TrxDate"])["_van_load"]
            .sum()
            .reset_index()
        )
        if not per_day.empty:
            idx = per_day.groupby("RouteCode")["_van_load"].idxmax()
            for r in per_day.loc[idx].itertuples(index=False):
                peak_by_route[str(r.RouteCode)] = (
                    r.TrxDate.strftime("%Y-%m-%d") if pd.notna(r.TrxDate) else None
                )

    routes = [
        {
            "route_code":    str(r.RouteCode),
            "skus":          int(r.skus),
            "predicted_qty": round(float(r.predicted_qty), 1),
            "peak_day":      peak_by_route.get(str(r.RouteCode)),
        }
        for r in by_route.itertuples(index=False)
    ]
    return FutureRouteSummaryResponse(
        date=date, routes=routes, reconciled=have_recon,
    )


@router.get("/forecast", response_model=PredictionResponse)
def get_future_forecast(
    route_code: Optional[str] = Query(None),
    item_code: Optional[str] = Query(None),
    limit: int = Query(
        default_factory=lambda: get_settings().default_page_limit,
        ge=1, le=100_000,
    ),
    offset: int = Query(0, ge=0),
    svc: ArtifactService = Depends(get_artifact_service),
):
    """Per-(route, item, date) rows -- always reconciled van load.

    Read path (production): the row's ``recommended_load`` is already
    populated in the DB (filled by ``db_pusher`` at training/inference
    time and refreshed daily by the reconciliation cron). We just copy
    it onto ``prediction`` so the wire field ``prediction`` carries the
    reconciled V5_b value, and drop the helper column.

    Lazy fallback: when the DB column is zero (cron skipped, brand-new
    date, or pre-migration row), we run ``enrich_with_load`` inline so
    the page never shows an unreconciled number. The result is the same
    function ``db_pusher`` and the cron use, so the inline value will
    be byte-identical to what the next cron run will write back.

    Engine intermediates (``forecast_corrected``, ``bias_pct``,
    ``opening_stock``, bounds, demand class, etc.) stay on the row for
    the explainability popup.
    """
    df, total = svc.get_future_forecast(route_code, item_code, limit, offset)
    pred_col = _detect_predicted_col(df) if not df.empty else None
    if pred_col is None or df.empty:
        return PredictionResponse(
            success=True, source="future_forecast", total=total,
            data=df.to_dict("records") if not df.empty else [],
        )

    # If the DB-mirror already carries reconciled values, prefer them --
    # zero CPU cost on the hot path. Detect "stored value present" via
    # the column existing AND not being uniformly zero (the migration
    # default for un-pushed rows).
    have_stored = (
        "recommended_load" in df.columns
        and pd.to_numeric(df["recommended_load"], errors="coerce").fillna(0.0).abs().sum() > 0
    )
    if not have_stored:
        df = enrich_with_load(df, predicted_col=pred_col)

    # Reconciled-only contract on the wire:
    #   * ``prediction``  carries V5_b reconciled load (was raw model output)
    #   * ``lower_bound`` carries the bias-corrected, leftover-aware low band
    #   * ``upper_bound`` carries the same for the high band
    # Helper columns the engine produced are dropped so the wire has
    # exactly one source of truth per concept.
    if "recommended_load" in df.columns:
        df[pred_col] = pd.to_numeric(df["recommended_load"], errors="coerce").fillna(0.0).astype(float)
        df = df.drop(columns="recommended_load")
    if "load_lower_bound" in df.columns:
        df["lower_bound"] = pd.to_numeric(df["load_lower_bound"], errors="coerce").fillna(0.0).astype(float)
        df = df.drop(columns="load_lower_bound")
    if "load_upper_bound" in df.columns:
        df["upper_bound"] = pd.to_numeric(df["load_upper_bound"], errors="coerce").fillna(0.0).astype(float)
        df = df.drop(columns="load_upper_bound")
    return PredictionResponse(
        success=True, source="future_forecast", total=total,
        data=df.to_dict("records") if not df.empty else [],
    )
