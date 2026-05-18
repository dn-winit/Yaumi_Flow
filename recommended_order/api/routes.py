"""
API routes for the recommended order service.
"""

from __future__ import annotations

import logging
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd
from fastapi import APIRouter, Depends, Query

from recommended_order.api.dependencies import (
    get_adoption_service,
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
    AdoptionResponse,
    EmptyRouteCustomer,
    EmptyRouteDiagnosis,
    FilterOptionsResponse,
    GenerateRequest,
    GenerateResponse,
    HealthResponse,
    RecommendationSummaryResponse,
    RetrieveRequest,
    RetrieveResponse,
    UpcomingPlanResponse,
)
from recommended_order.config.constants import SafetyClamps
from recommended_order.config.settings import get_settings
from recommended_order.core.calibration import (
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


# ------------------------------------------------------------------
# Filter options
# ------------------------------------------------------------------


@router.get("/filter-options", response_model=FilterOptionsResponse)
def filter_options(
    date: Optional[str] = Query(None, description="Date to check journey counts for"),
    dm: DataManager = Depends(get_fresh_data_manager),
    store: RecommendationStore = Depends(get_store),
):
    routes = dm.get_route_codes()
    journey_counts: Dict[str, int] = {}
    route_diagnoses: Dict[str, EmptyRouteDiagnosis] = {}

    if date:
        # Single-pass groupby instead of one ``get_journey_customers`` call
        # per route -- the picker grid would otherwise issue N filter
        # operations on the same DataFrame for the 12-route fleet.
        jp = dm.get_journey_plan(date=date)
        if not jp.empty and "RouteCode" in jp.columns and "CustomerCode" in jp.columns:
            grouped = jp.groupby("RouteCode")["CustomerCode"]
            for rc, custs in grouped:
                n = int(custs.dropna().astype(str).nunique())
                if n > 0:
                    journey_counts[str(rc)] = n

        # For routes that ARE planned but have no stored recommendations,
        # surface the diagnosis on the picker grid so the supervisor sees
        # the gap up-front instead of a vague "Click to generate".
        if journey_counts:
            existing = store.exists_batch(date, list(journey_counts.keys()))
            for rc in journey_counts:
                if not existing.get(rc, False):
                    route_diagnoses[rc] = _diagnose_empty_route(rc, date, dm)

    return FilterOptionsResponse(
        routes=routes,
        journey_counts=journey_counts,
        route_diagnoses=route_diagnoses,
    )


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
    """Sprint-4: read stored recs + supervision visits in the rolling
    window, compute per-(route, source) shrinkage multipliers + confidence,
    EMA-smooth against the persisted file, and return both.

    Opt-in via ``SafetyClamps.feedback_enabled``; cold-start safe
    (returns empty dicts when no visits exist or the supervision DB is
    unreachable).
    """
    if not clamps.feedback_enabled:
        return {}, {}
    db_loader = None
    try:
        # Lazy import keeps recommended_order's startup independent of
        # the supervision module being importable in every deployment.
        from sales_supervision.services.feedback_loader import SessionDbLoader
        db_loader = SessionDbLoader()
        if not db_loader.available:
            logger.info("feedback disabled: supervision DB not configured")
            return {}, {}
    except Exception as exc:
        logger.info("feedback disabled: could not init supervision loader (%s)", exc)
        return {}, {}
    ro_settings = get_settings()
    return compute_feedback_adjustments(
        file_storage_dir=ro_settings.file_storage_dir,
        db_loader=db_loader,
        clamps=clamps,
    )


# Number of typical items to surface per customer in the empty-state diagnosis.
# Kept small so the UI stays scannable; salespeople just need the top SKUs to
# verify the van load, not the whole basket.
_DIAGNOSIS_TOP_ITEMS = 3


def _diagnose_empty_route(
    rc: str, target_date: str, dm: DataManager
) -> EmptyRouteDiagnosis:
    """Explain why a route returned 0 recommendations -- positively framed.

    The engine ran honestly; this function tells the supervisor what the
    engine SAW so they can act (reload van, reassign route, or accept that
    today's plan is genuinely empty). Every branch is data-driven; no string
    is hardcoded against a specific route or customer.
    """
    journey_custs = dm.get_journey_customers(rc, target_date) or []
    van_items = dm.get_van_items(rc, target_date) or {}

    if not journey_custs and not van_items:
        return EmptyRouteDiagnosis(
            reason="no_plan",
            headline="No activity planned today",
            detail="Neither customers nor van items are scheduled for this route on this date.",
        )
    if not journey_custs:
        return EmptyRouteDiagnosis(
            reason="no_journey",
            headline="Van loaded, no customers planned",
            detail=f"{len(van_items)} items are loaded for this route but no customers are on today's journey.",
        )
    if not van_items:
        return EmptyRouteDiagnosis(
            reason="no_van",
            headline="Customers planned, van not loaded",
            detail=f"{len(journey_custs)} customer(s) are planned but no van load is recorded yet for this route.",
        )

    cust_df = dm.get_customer_data(rc)
    cust_names = dm.get_customer_names(rc)
    item_names = dm.get_item_names(rc)
    van_set = {str(c) for c in van_items.keys()}
    journey_set = {str(c) for c in journey_custs}

    # Normalise the join keys exactly once. The cached customer frame may use
    # stringified ints with trailing whitespace upstream, so we coerce here
    # rather than inside the per-customer loop (which would be O(rows × custs)).
    norm_cust = cust_df["CustomerCode"].astype(str).str.strip()
    norm_item = cust_df["ItemCode"].astype(str).str.strip()
    hist_custs = set(norm_cust.unique())
    known = sorted(journey_set & hist_custs)
    unknown = sorted(journey_set - hist_custs)

    if not known and unknown:
        return EmptyRouteDiagnosis(
            reason="all_new_customers",
            headline="First-visit customers detected",
            detail=f"{len(unknown)} planned customer(s) with no buying history yet -- the salesperson should ask what they want.",
        )

    # Known customers exist -- check which have van overlap and which don't.
    mismatch: List[EmptyRouteCustomer] = []
    for cust in known:
        cust_mask = norm_cust == cust
        if not cust_mask.any():
            continue
        cust_items_freq = norm_item[cust_mask].value_counts()
        if cust_items_freq.empty:
            continue
        cust_codes = list(cust_items_freq.index)
        if any(code in van_set for code in cust_codes):
            continue  # this customer has at least one matching item -- not a mismatch
        mismatch.append(
            EmptyRouteCustomer(
                customer_code=cust,
                customer_name=cust_names.get(cust, ""),
                typical_items=[
                    {"code": code, "name": item_names.get(code, "")}
                    for code in cust_codes[:_DIAGNOSIS_TOP_ITEMS]
                ],
            )
        )

    if mismatch and unknown:
        return EmptyRouteDiagnosis(
            reason="mixed",
            headline="Van load gap caught",
            detail=(
                f"{len(mismatch)} customer(s) typically buy items that aren't loaded today, "
                f"plus {len(unknown)} first-visit customer(s). Reload the van or reassign the route."
            ),
            customers=mismatch,
        )
    if mismatch:
        n = len(mismatch)
        return EmptyRouteDiagnosis(
            reason="van_mismatch",
            headline="Van load gap caught",
            detail=(
                f"{n} planned customer(s) usually buy items not loaded today. "
                "Add those items to the van or reassign the customer to a route that carries them."
            ),
            customers=mismatch,
        )

    # Every known customer DID have van overlap, yet the engine still produced
    # no candidates. Falls through to a generic but honest message.
    return EmptyRouteDiagnosis(
        reason="engine_no_match",
        headline="No items cleared the recommendation threshold",
        detail=(
            "Customers and van items overlap, but no items met the calibrated "
            "frequency / recency / quantity thresholds for this route today."
        ),
    )


def _sales_transactions_has(s, target_date: str) -> bool:
    """True if sales_transactions.csv has any row for ``target_date``."""
    path = Path(s.shared_data_dir) / s.sales_transactions_file
    if not path.exists():
        return False
    df = pd.read_csv(path, usecols=["TrxDate"], low_memory=False)
    return df["TrxDate"].astype(str).str.startswith(target_date).any()


def _trigger_reconciliation_refresh(s, horizon_days_behind: int) -> dict:
    """POST demand_forecasting's /reconciliation/refresh.

    Wraps every transport / HTTP-status error as a ``RuntimeError`` so
    callers ``_ensure_carry_chain_present`` and ``_generate_routes`` see
    a single exception type and return the structured "carry_chain_
    missing" envelope instead of leaking a 500 to the supervisor UI.
    Without the wrap, a 422 (horizon out of bounds) bubbled up as an
    uncaught ``httpx.HTTPStatusError`` -- e.g. a pre-system date request
    asking for a 2329-day horizon against demand_forecasting's 365-day
    cap rendered as a bare ``Internal Server Error``.
    """
    import httpx
    url = f"{s.demand_forecasting_url.rstrip('/')}/api/v1/forecast/reconciliation/refresh"
    logger.warning("carry_chain_auto_heal: POST %s?horizon_days_behind=%d", url, horizon_days_behind)
    try:
        with httpx.Client(timeout=s.reconciliation_preflight_timeout_seconds) as client:
            resp = client.post(url, params={"horizon_days_behind": horizon_days_behind})
            resp.raise_for_status()
            body = resp.json()
    except httpx.HTTPStatusError as exc:
        raise RuntimeError(
            f"reconciliation_refresh rejected horizon={horizon_days_behind} "
            f"with {exc.response.status_code}: {exc.response.text[:200]}"
        ) from exc
    except httpx.RequestError as exc:
        raise RuntimeError(
            f"reconciliation_refresh unreachable at {url}: {exc}"
        ) from exc
    if not body.get("success"):
        raise RuntimeError(f"reconciliation_refresh returned success=False: {body}")
    return body


def _ensure_carry_chain_present(target_date: str, dm: DataManager) -> None:
    """Auto-heal yf_sales_transactions for non-future dates by triggering
    reconciliation_refresh with the minimal horizon covering target_date."""
    s = get_settings()
    from datetime import date as _date
    today_d = _date.today()
    if target_date > today_d.strftime("%Y-%m-%d"):
        return
    if _sales_transactions_has(s, target_date):
        return
    if not s.demand_forecasting_url:
        raise RuntimeError(
            f"yf_sales_transactions missing for {target_date} and "
            f"RO_DEMAND_FORECASTING_URL is unset -- cannot auto-heal."
        )
    horizon = max(0, (today_d - _date.fromisoformat(target_date)).days)
    # demand_forecasting caps horizon at 365 days (one full year of
    # back-fill). A request for a date older than that is by definition
    # out of the auto-heal envelope -- the reconciliation cron has
    # never carried state that far back. Fail fast with a clear
    # message; the caller catches RuntimeError and returns the
    # structured "carry_chain_missing" envelope to the UI instead of
    # letting the inevitable 422 from demand_forecasting bubble up as
    # an unhandled HTTPStatusError -> bare 500.
    _MAX_AUTOHEAL_HORIZON = 365
    if horizon > _MAX_AUTOHEAL_HORIZON:
        raise RuntimeError(
            f"yf_sales_transactions has no row for {target_date} "
            f"({horizon} days back); the auto-heal window is "
            f"{_MAX_AUTOHEAL_HORIZON} days -- request a date within "
            f"the past year or run a manual backfill."
        )
    body = _trigger_reconciliation_refresh(s, horizon)
    dm.refresh()
    if _sales_transactions_has(s, target_date):
        logger.info(
            "carry_chain_healed: %s (refresh wrote %s rows, horizon=%d)",
            target_date, body.get("rows_updated"), horizon,
        )
        return
    raise RuntimeError(
        f"reconciliation_refresh succeeded but yf_sales_transactions still "
        f"has no row for {target_date} -- generation aborted."
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

    try:
        _ensure_carry_chain_present(target_date, dm)
    except RuntimeError as exc:
        logger.error("carry_chain_guard_failed: %s", exc)
        return {
            "routes_requested": 0,
            "routes_generated": 0,
            "total_records": 0,
            "duration_seconds": round(time.time() - t0, 2),
            "details": [{"status": "carry_chain_missing", "error": str(exc)}],
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

    # Per-route generation is independent: same engine, distinct DataManager
    # slices, distinct store keys, distinct DB rows. Run in a thread pool so
    # cold-path latency scales sub-linearly with the route count. The engine
    # is mostly numpy/pandas under the hood; the GIL releases on those calls
    # so threads give a real wall-clock win.
    workers = max(1, int(get_settings().generation_concurrency))

    def _one(rc: str) -> Dict[str, Any]:
        try:
            van_items = dm.get_van_items(rc, target_date)
            journey_custs = dm.get_journey_customers(rc, target_date)
            if not van_items or not journey_custs:
                return {"route": rc, "status": "skipped",
                        "reason": "no van items or journey customers", "records": 0}

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
                return {"route": rc, "status": "empty", "records": 0}

            save_result = store.save(df, target_date, rc)
            saved = int(save_result.get("records_saved", 0))

            if pusher.available:
                pusher.push_dataframe(df, target_date, rc)

            return {"route": rc, "status": "generated", "records": saved}
        except Exception as exc:
            logger.error("Failed to generate for route %s: %s", rc, exc, exc_info=True)
            return {"route": rc, "status": "error", "error": str(exc), "records": 0}

    details: List[Dict[str, Any]] = []
    if workers <= 1 or len(to_generate) <= 1:
        details = [_one(rc) for rc in to_generate]
    else:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=workers) as pool:
            details = list(pool.map(_one, to_generate))

    total_records = sum(int(d.get("records", 0)) for d in details)
    generated_routes = sum(1 for d in details if d.get("status") == "generated")

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
        # Diagnose the empty result so the UI can give the supervisor an
        # actionable, positively-framed explanation. Only meaningful when a
        # single route is requested -- the grid view doesn't need per-route
        # diagnosis since the picker shows route status separately.
        diagnosis = (
            _diagnose_empty_route(req.route_code, req.date, dm)
            if req.route_code
            else None
        )
        return RetrieveResponse(
            success=True, date=req.date, total=0, data=[], source=source,
            generated_routes=generated_routes, diagnosis=diagnosis,
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


# ------------------------------------------------------------------
# Analytics: adoption (historical) + upcoming plan (forward)
# ------------------------------------------------------------------


@router.get("/analytics/adoption", response_model=AdoptionResponse)
def adoption(
    start_date: str = Query(..., pattern=r"^\d{4}-\d{2}-\d{2}$"),
    end_date: str = Query(..., pattern=r"^\d{4}-\d{2}-\d{2}$"),
    route_code: Optional[str] = Query(default=None),
    category_codes: List[str] = Query(default=[], alias="category_codes"),
    item_codes: List[str] = Query(default=[], alias="item_codes"),
    svc: AdoptionService = Depends(get_adoption_service),
):
    """Did recommendations convert? Read-only join of stored recs and sales,
    optionally narrowed to specific categories and/or items so the drawer's
    filters can scope adoption metrics in the same shape as the dashboard.
    """
    return svc.get_adoption(start_date, end_date, route_code, category_codes, item_codes)


@router.get("/analytics/upcoming", response_model=UpcomingPlanResponse)
def upcoming_plan(
    days: int = Query(default=7, ge=1, le=30),
    route_code: Optional[str] = Query(default=None),
    svc: PlanningService = Depends(get_planning_service),
):
    """Daily plan for the next ``days`` days (journey + forecast + prices)."""
    return svc.get_upcoming(days, route_code)


