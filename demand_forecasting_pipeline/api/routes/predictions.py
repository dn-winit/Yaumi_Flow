"""Prediction endpoints: test predictions + reconciled future forecasts.

Reconciled-only contract: ``/forecast`` and ``/forecast/route-summary`` overwrite
``prediction`` with V5_b ``recommended_load`` before serialisation. Raw model
output stays internal; callers needing it use ``/predictions/test`` or accuracy_service.
"""
from __future__ import annotations

import logging

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

router = APIRouter(prefix="/predictions", tags=["predictions"])


def _detect_predicted_col(df: pd.DataFrame) -> str | None:
    """Artefacts use ``prediction``; raw demand_forecast.csv uses ``Predicted``."""
    for c in ("prediction", "Predicted"):
        if c in df.columns:
            return c
    return None


@router.get("/test", response_model=PredictionResponse)
def get_test_predictions(
    route_code: str | None = Query(None),
    item_code: str | None = Query(None),
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
    date: str | None = Query(None, description="YYYY-MM-DD; defaults to full horizon"),
    svc: ArtifactService = Depends(get_artifact_service),
):
    """Per-route aggregates for the VanLoad page tiles.

    predicted_qty = sum(opening_stock + recommended_load) per route (rep's TOTAL van load).
    skus = distinct ItemCodes with (opening + fresh) > 0 (mirrors drill-down's carry-aware count).
    peak_day = highest van-load day in horizon; equals ``date`` when scoped to one day.
    reconciled = False when engine degraded to raw forecast; UI shows a warning chip.
    """
    # van_load_view_enriched returns reconciled frame (DB-stored past/today + engine-computed future).
    # Unenriched view would drop future rows; this is the same source page_views/van-load uses.
    fc_df = svc.van_load_view_enriched()
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

    # van_load_view_enriched populates recommended_load/opening_stock for every row;
    # no inline enrich_with_load needed (view layer owns cache).
    enriched = scope
    have_recon = (
        "recommended_load" in enriched.columns
        and "opening_stock" in enriched.columns
    )

    if have_recon:
        # Per-cell pack quantity via shared pack_qty helper so grid card and detail tile agree byte-for-byte.
        from common.numeric import pack_qty
        opening = pd.to_numeric(enriched["opening_stock"], errors="coerce").fillna(0.0)
        fresh = pd.to_numeric(enriched["recommended_load"], errors="coerce").fillna(0.0)
        enriched = enriched.assign(
            _van_load=opening.apply(pack_qty) + fresh.apply(pack_qty)
        )
    else:
        # Degraded: surface raw forecast; reconciled=False prompts UI warning chip.
        logger.error(
            "route_summary_reconciliation_degraded -- engine outputs absent; "
            "tile will show raw forecast until bias table / engine recovers"
        )
        enriched = enriched.assign(
            _van_load=pd.to_numeric(enriched[pred_col], errors="coerce").fillna(0.0)
        )

    # Distinct items with non-zero van load; mirrors drill-down's carry-aware mask
    # in page_views.py so tile count matches per-route page by construction.
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

    # Peak-day per route only meaningful across multiple days; with ``date`` it equals that date.
    peak_by_route: dict[str, str | None] = {}
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
    route_code: str | None = Query(None),
    item_code: str | None = Query(None),
    limit: int = Query(
        default_factory=lambda: get_settings().default_page_limit,
        ge=1, le=100_000,
    ),
    offset: int = Query(0, ge=0),
    svc: ArtifactService = Depends(get_artifact_service),
):
    """Per-(route, item, date) rows; ``prediction`` is always V5_b reconciled load.

    DB-populated by db_pusher + daily cron; lazy fallback runs canonical engine via
    van_load_view_enriched (mtime-cached). Engine intermediates stay on the row.
    """
    df, total = svc.get_future_forecast(route_code, item_code, limit, offset)
    pred_col = _detect_predicted_col(df) if not df.empty else None
    if pred_col is None or df.empty:
        return PredictionResponse(
            success=True, source="future_forecast", total=total,
            data=df.to_dict("records") if not df.empty else [],
        )

    # Wire contract: prediction = V5_b load; lower/upper_bound = leftover-aware bands.
    # Engine helper columns dropped (one source of truth per concept on the wire).
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
