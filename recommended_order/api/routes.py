"""
API routes for the recommended order service.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd
from fastapi import APIRouter, Depends, Query

from recommended_order.api.dependencies import (
    get_adoption_service,
    get_data_manager,
    get_db_pusher,
    get_engine,
    get_fresh_data_manager,
    get_planning_service,
    get_store,
)
from recommended_order.services.adoption_service import AdoptionService
from recommended_order.services.planning_service import PlanningService
from recommended_order.services.db_pusher import DbPusher
from recommended_order.api.schemas import (
    ExistsRequest,
    ExistsResponse,
    FilterOptionsResponse,
    GenerateRequest,
    GenerateResponse,
    GenerationInfoResponse,
    HealthResponse,
    RecommendationSummaryResponse,
    RetrieveRequest,
    RetrieveResponse,
)
from recommended_order.config.constants import SafetyClamps
from recommended_order.config.settings import get_settings
from recommended_order.core.calibration import (
    RouteCalibration,
    calibrate,
    cache_size as calibration_cache_size,
)
from recommended_order.core.engine import RecommendationEngine
from recommended_order.core.feedback import compute_feedback_adjustments
from recommended_order.core.metrics import get_last_generation_tracker
from recommended_order.data.manager import DataManager
from recommended_order.services.storage.store import RecommendationStore

logger = logging.getLogger(__name__)

router = APIRouter()


# ------------------------------------------------------------------
# Health
# ------------------------------------------------------------------


@router.get("/summary", response_model=RecommendationSummaryResponse)
def summary(
    store: RecommendationStore = Depends(get_store),
):
    """Aggregated KPI summary for dashboard."""
    from pathlib import Path
    import re

    settings = get_settings()
    routes_configured = len(settings.route_codes)

    # Discover latest date from stored rec files
    file_dir = Path(settings.file_storage_dir)
    latest_date: str | None = None
    if file_dir.exists():
        date_re = re.compile(r"recommendations_(\d{4}-\d{2}-\d{2})_.+\.csv$")
        dates = set()
        for f in file_dir.glob("recommendations_*.csv"):
            m = date_re.match(f.name)
            if m:
                dates.add(m.group(1))
        if dates:
            latest_date = max(dates)

    total_recs = 0
    routes_with_recs = 0
    customers = 0
    if latest_date:
        df = store.get(latest_date)
        if not df.empty:
            total_recs = len(df)
            if "RouteCode" in df.columns:
                routes_with_recs = int(df["RouteCode"].nunique())
            if "CustomerCode" in df.columns:
                customers = int(df["CustomerCode"].nunique())

    return RecommendationSummaryResponse(
        routes_configured=routes_configured,
        last_generated_date=latest_date,
        total_recs_latest_date=total_recs,
        routes_with_recs_latest=routes_with_recs,
        customers_latest=customers,
    )


@router.get("/health", response_model=HealthResponse)
def health(
    dm: DataManager = Depends(get_fresh_data_manager),
    engine: RecommendationEngine = Depends(get_engine),
):
    fresh = dm.freshness()
    tracker = get_last_generation_tracker()
    return HealthResponse(
        status="healthy",
        last_refresh=dm.last_refresh.isoformat() if dm.last_refresh else None,
        route_codes=dm.get_route_codes(),
        per_route_last_generation=tracker.route_last_timestamps(),
        calibration_cache_size=calibration_cache_size(),
        lookalike_cache_size=engine.lookalike_cache_size(),
        avg_generation_seconds_last_n=tracker.avg_duration_seconds(),
        feedback_routes_active=engine.feedback_routes_active(),
        **fresh,
    )


@router.get("/metrics/last-generation")
def metrics_last_generation():
    """Per-generator counts, source mix, and calibration snapshot for the
    most recent generation run. Useful for dashboarding; not paginated."""
    return get_last_generation_tracker().snapshot()


# ------------------------------------------------------------------
# Filter options
# ------------------------------------------------------------------


@router.get("/filter-options", response_model=FilterOptionsResponse)
def filter_options(
    date: Optional[str] = Query(None, description="Date to check journey counts for"),
    dm: DataManager = Depends(get_fresh_data_manager),
):
    routes = dm.get_route_codes()
    journey_counts: Dict[str, int] = {}
    if date:
        for rc in routes:
            custs = dm.get_journey_customers(rc, date)
            if custs:
                journey_counts[rc] = len(custs)
    return FilterOptionsResponse(routes=routes, journey_counts=journey_counts)


# ------------------------------------------------------------------
# Generate recommendations
# ------------------------------------------------------------------


def _corpus_median_active_customers(
    dm: DataManager, route_codes: List[str],
) -> Optional[float]:
    """Median active-customer count across every configured route.

    Calibration uses this to detect sparse routes (where the per-route customer
    count is materially below the corpus norm) and soften filters accordingly.
    """
    counts = []
    for rc in route_codes:
        df = dm.get_customer_data(rc)
        if df.empty:
            continue
        counts.append(int(df["CustomerCode"].nunique()))
    if not counts:
        return None
    return float(np.median(counts))


def _corpus_field_distributions(
    dm: DataManager, route_codes: List[str], clamps: SafetyClamps,
) -> Dict[str, List[float]]:
    """Build corpus-wide distributions of each calibration field (for the
    Sprint-3 anti-overfit sanity clamp).

    We compute calibration once per route using a fresh (un-sanity-clamped)
    pass -- passing ``corpus_field_values=None`` ensures the clamp itself
    doesn't influence the corpus distribution.
    """
    field_names = (
        "frequency_floor", "dormancy_days", "qty_benchmark",
        "completion_gate", "basket_min_confidence", "recency_half_life_days",
    )
    values: Dict[str, List[float]] = {k: [] for k in field_names}
    for rc in route_codes:
        df = dm.get_customer_data(rc)
        if df.empty:
            continue
        try:
            calib = calibrate(
                customer_df=df,
                demand_df=pd.DataFrame(),
                route_code=rc,
                clamps=clamps,
                window_days=clamps.calibration_window_days,
                corpus_field_values=None,   # base pass -- no recursion
            )
        except Exception as exc:
            logger.warning("corpus calibration skipped for %s: %s", rc, exc)
            continue
        for f in field_names:
            values[f].append(float(getattr(calib, f)))
    return values


def _load_feedback_adjustments(
    dm: DataManager, clamps: SafetyClamps,
) -> tuple[Dict[str, Dict[str, float]], Dict[str, Dict[str, float]]]:
    """Sprint-4: read stored recs + supervision sessions in the rolling
    window, compute per-(route, source) shrinkage multipliers + confidence,
    EMA-smooth against the persisted file, and return both.

    Opt-in via ``SafetyClamps.feedback_enabled``; cold-start safe
    (returns empty dicts when no sessions exist).
    """
    if not clamps.feedback_enabled:
        return {}, {}
    try:
        from sales_supervision.config.settings import get_settings as _ss
        sessions_dir = str(Path(_ss().storage_dir) / "sessions")
    except Exception as exc:
        logger.info("feedback disabled: could not locate sessions dir (%s)", exc)
        return {}, {}
    ro_settings = get_settings()
    return compute_feedback_adjustments(
        file_storage_dir=ro_settings.file_storage_dir,
        sessions_dir=sessions_dir,
        shared_data_dir=ro_settings.shared_data_dir,
        clamps=clamps,
    )


def _generate_routes(
    target_date: str,
    route_codes: List[str],
    *,
    dm: DataManager,
    engine: RecommendationEngine,
    store: RecommendationStore,
    pusher: DbPusher,
    skip_existing: bool = True,
) -> Dict[str, Any]:
    """Generate + save + DB-push recommendations for a set of routes.

    Shared by POST /generate and POST /get (lazy path). Skips routes that
    already have data when ``skip_existing`` is True. Routes without source
    data (no van items / no journey customers) are recorded but not failed.
    """
    t0 = time.time()

    # Ensure CSVs contain data for the target date (Friday allowed as no-journey)
    try:
        dm.assert_fresh(target_date)
    except RuntimeError as exc:
        logger.warning("Freshness guard: %s", exc)
        return {
            "routes_requested": 0,
            "routes_generated": 0,
            "total_records": 0,
            "duration_seconds": round(time.time() - t0, 2),
            "details": [{"status": "stale_data", "error": str(exc)}],
        }

    if skip_existing:
        existing = store.exists_batch(target_date, route_codes)
        to_generate = [rc for rc in route_codes if not existing.get(rc, False)]
    else:
        to_generate = list(route_codes)

    # Inject corpus-level stats so per-route calibration can detect sparse routes
    # AND sanity-clamp outlier per-route values against the corpus distribution.
    clamps = SafetyClamps()
    engine.set_corpus_stats(
        median_active_customers=_corpus_median_active_customers(dm, route_codes),
        field_values=_corpus_field_distributions(dm, route_codes, clamps),
    )
    # Inject feedback multipliers + confidence (opt-in, cold-start safe).
    adj, conf = _load_feedback_adjustments(dm, clamps)
    engine.set_feedback_adjustments(adj, confidence=conf)

    details: List[Dict[str, Any]] = []
    total_records = 0
    generated_routes = 0

    for rc in to_generate:
        try:
            van_items = dm.get_van_items(rc, target_date)
            journey_custs = dm.get_journey_customers(rc, target_date)
            if not van_items or not journey_custs:
                details.append({"route": rc, "status": "skipped", "reason": "no van items or journey customers"})
                continue

            df = engine.generate(
                customer_df=dm.get_customer_data(rc),
                journey_customers=journey_custs,
                van_items=van_items,
                item_names=dm.get_item_names(rc),
                customer_names=dm.get_customer_names(rc),
                route_code=rc,
                target_date=target_date,
                demand_df=dm.get_demand_data(rc),
            )

            if df.empty:
                details.append({"route": rc, "status": "empty", "records": 0})
                continue

            save_result = store.save(df, target_date, rc)
            saved = int(save_result.get("records_saved", 0))
            total_records += saved
            generated_routes += 1

            if pusher.available:
                pusher.push_dataframe(df, target_date, rc)

            details.append({"route": rc, "status": "generated", "records": saved})

        except Exception as exc:
            logger.error("Failed to generate for route %s: %s", rc, exc, exc_info=True)
            details.append({"route": rc, "status": "error", "error": str(exc)})

    return {
        "routes_requested": len(to_generate),
        "routes_generated": generated_routes,
        "total_records": total_records,
        "duration_seconds": round(time.time() - t0, 2),
        "details": details,
    }


@router.post("/generate", response_model=GenerateResponse)
def generate_recommendations(
    req: GenerateRequest,
    dm: DataManager = Depends(get_fresh_data_manager),
    engine: RecommendationEngine = Depends(get_engine),
    store: RecommendationStore = Depends(get_store),
    pusher: DbPusher = Depends(get_db_pusher),
):
    target_date = req.date
    route_codes = req.route_codes or dm.get_route_codes()

    res = _generate_routes(
        target_date, route_codes, dm=dm, engine=engine, store=store, pusher=pusher,
        skip_existing=not req.force,
    )

    if res["routes_requested"] == 0:
        return GenerateResponse(
            success=True,
            message="All routes already generated",
            date=target_date,
            routes_processed=0,
            duration_seconds=res["duration_seconds"],
        )

    return GenerateResponse(
        success=True,
        message=f"Generated {res['total_records']} recommendations for {res['routes_generated']} routes",
        date=target_date,
        routes_processed=res["routes_requested"],
        total_records=res["total_records"],
        duration_seconds=res["duration_seconds"],
        details=res["details"],
    )


# ------------------------------------------------------------------
# Retrieve recommendations
# ------------------------------------------------------------------


@router.post("/get", response_model=RetrieveResponse)
def get_recommendations(
    req: RetrieveRequest,
    dm: DataManager = Depends(get_fresh_data_manager),
    engine: RecommendationEngine = Depends(get_engine),
    store: RecommendationStore = Depends(get_store),
    pusher: DbPusher = Depends(get_db_pusher),
):
    """Retrieve stored recommendations, with lazy top-up generation.

    * Single route: if nothing is stored for that route, generate it on demand.
    * Grid view (no ``route_code``): derive the expected route set from the
      day's journey plan and generate every route that's missing from the
      store, so the grid always reflects the full planned fleet.
    """
    source = "store"
    generated_routes = 0

    if req.route_code:
        df = store.get(req.date, req.route_code)
        if df.empty:
            res = _generate_routes(
                req.date, [req.route_code], dm=dm, engine=engine, store=store, pusher=pusher,
                skip_existing=True,
            )
            generated_routes = res["routes_generated"]
            if generated_routes > 0:
                df = store.get(req.date, req.route_code)
                source = "generated"
    else:
        # Grid view: figure out which routes are supposed to run today
        # (journey plan) and fill any gaps so every card has data.
        journey = dm.get_journey_plan(date=req.date)
        expected = (
            sorted(journey["RouteCode"].dropna().astype(str).str.strip().unique().tolist())
            if not journey.empty
            else dm.get_route_codes()
        )
        if expected:
            existing = store.exists_batch(req.date, expected)
            missing = [rc for rc in expected if not existing.get(rc, False)]
            if missing:
                res = _generate_routes(
                    req.date, missing, dm=dm, engine=engine, store=store, pusher=pusher,
                    skip_existing=False,  # `missing` is already the gap list
                )
                generated_routes = res["routes_generated"]
                if generated_routes > 0:
                    source = "generated"
        df = store.get(req.date, None)

    if df.empty:
        return RetrieveResponse(
            success=True, date=req.date, total=0, data=[], source=source, generated_routes=generated_routes
        )

    if req.customer_code:
        df = df[df["CustomerCode"] == req.customer_code]
    if req.item_code:
        df = df[df["ItemCode"] == req.item_code]
    if req.tier:
        df = df[df["Tier"] == req.tier]
    if req.min_priority is not None:
        df = df[df["PriorityScore"] >= req.min_priority]

    total = len(df)
    df = df.iloc[req.offset : req.offset + req.limit].copy()

    # Coerce date/datetime columns to ISO strings for JSON serialization
    if "TrxDate" in df.columns:
        df["TrxDate"] = pd.to_datetime(df["TrxDate"], errors="coerce").dt.strftime("%Y-%m-%d")

    # Upstream CSVs sometimes type RouteCode/CustomerCode/ItemCode as ints when
    # the values are numeric -- force string to match the response schema.
    for col in ("RouteCode", "CustomerCode", "ItemCode"):
        if col in df.columns:
            df[col] = df[col].astype(str).str.strip()

    # Name columns are optional strings in the schema -- pandas reads missing
    # values as NaN (float), which fails pydantic str validation. Fill first.
    for col in ("CustomerName", "ItemName"):
        if col in df.columns:
            df[col] = df[col].fillna("").astype(str)

    # Sprint-1 explainability columns:
    #   * Signals is stored as a JSON string in the CSV for portability;
    #     decode it back to list[dict] for the API contract.
    #   * WhyItem / WhyQuantity / Source default to "" when missing.
    import json as _json
    if "Signals" in df.columns:
        def _decode(v):
            if isinstance(v, list):
                return v
            if not isinstance(v, str) or not v.strip():
                return []
            try:
                out = _json.loads(v)
                return out if isinstance(out, list) else []
            except Exception:
                return []
        df["Signals"] = df["Signals"].apply(_decode)
    for col in ("WhyItem", "WhyQuantity", "Source"):
        if col in df.columns:
            df[col] = df[col].fillna("").astype(str)
    if "Confidence" in df.columns:
        df["Confidence"] = pd.to_numeric(df["Confidence"], errors="coerce").fillna(0.0)

    return RetrieveResponse(
        success=True,
        date=req.date,
        total=total,
        data=df.to_dict("records"),
        source=source,
        generated_routes=generated_routes,
    )


# ------------------------------------------------------------------
# Check existence
# ------------------------------------------------------------------


@router.post("/exists", response_model=ExistsResponse)
def check_exists(
    req: ExistsRequest,
    store: RecommendationStore = Depends(get_store),
    dm: DataManager = Depends(get_fresh_data_manager),
):
    routes = req.route_codes or dm.get_route_codes()
    exists_map = store.exists_batch(req.date, routes)
    return ExistsResponse(date=req.date, exists=exists_map)


# ------------------------------------------------------------------
# Generation info
# ------------------------------------------------------------------


@router.get("/info/{date}", response_model=GenerationInfoResponse)
def generation_info(
    date: str,
    store: RecommendationStore = Depends(get_store),
):
    info = store.generation_info(date)
    return GenerationInfoResponse(**info)


# ------------------------------------------------------------------
# Analytics: adoption (historical) + upcoming plan (forward)
# ------------------------------------------------------------------


@router.get("/analytics/adoption")
def adoption(
    start_date: str = Query(..., pattern=r"^\d{4}-\d{2}-\d{2}$"),
    end_date: str = Query(..., pattern=r"^\d{4}-\d{2}-\d{2}$"),
    route_code: Optional[str] = Query(default=None),
    svc: AdoptionService = Depends(get_adoption_service),
):
    """Did recommendations convert? Read-only join of stored recs and sales."""
    return svc.get_adoption(start_date, end_date, route_code)


@router.get("/analytics/upcoming")
def upcoming_plan(
    days: int = Query(default=7, ge=1, le=30),
    route_code: Optional[str] = Query(default=None),
    svc: PlanningService = Depends(get_planning_service),
):
    """Daily plan for the next ``days`` days (journey + forecast + prices)."""
    return svc.get_upcoming(days, route_code)


# ------------------------------------------------------------------
# Refresh cached data
# ------------------------------------------------------------------


@router.post("/refresh-data")
def refresh_data(dm: DataManager = Depends(get_data_manager)):
    result = dm.refresh()
    # Sprint-1: drop per-route calibration cache so next generate recomputes.
    from recommended_order.core.calibration import invalidate_cache
    invalidate_cache()
    return {"success": result["success"], "data": result["data"], "errors": result["errors"]}


# ------------------------------------------------------------------
# Push to DB
# ------------------------------------------------------------------


@router.post("/push-to-db")
def push_to_db(
    date: str = Query(..., description="Date YYYY-MM-DD"),
    route_code: str = Query(default=None),
    pusher: DbPusher = Depends(get_db_pusher),
):
    """Push local recommendation files to YaumiAIML database."""
    return pusher.push_recommendations(date, route_code)
