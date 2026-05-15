"""Reconciliation endpoints -- van composition + V5_b load + past-perf.

Four GET/POST endpoints, all dynamic and parameterised:

* ``/reconciliation/van-load``        composition only for one (route, date)
* ``/reconciliation/recommend``       V5_b recommendation joined with composition
* ``/reconciliation/past-performance`` per-day chart series + return metrics
* ``/reconciliation/refresh``          manual trigger for the daily refresh cron
"""
from __future__ import annotations

import logging
from typing import Any, Dict

import pandas as pd
from fastapi import APIRouter, Depends, Query

from demand_forecasting_pipeline.api.dependencies import (
    get_artifact_service,
    get_bias_service,
    get_reconciliation_engine,
    get_van_load_service,
)
from demand_forecasting_pipeline.api.schemas import (
    PastPerformanceCategoryRow,
    PastPerformanceItem,
    PastPerformanceResponse,
    ReconciliationResponse,
    VanLoadResponse,
)
from demand_forecasting_pipeline.config.settings import get_settings
from demand_forecasting_pipeline.services.artifact_service import ArtifactService
from demand_forecasting_pipeline.services.reconciliation.bias_service import BiasService
from demand_forecasting_pipeline.services.reconciliation.engine import ReconciliationEngine
from demand_forecasting_pipeline.services.reconciliation.van_load_service import VanLoadService
from demand_forecasting_pipeline.services.reconciliation_refresh import refresh_reconciliation

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/reconciliation", tags=["reconciliation"])

# Single ISO-date regex shared by every endpoint that accepts a date
# query param. Matches the regex on data_import's /eda/* endpoints so a
# bad date is rejected with the same HTTP 422 surface across services.
_DATE_RE = r"^\d{4}-\d{2}-\d{2}$"


@router.post("/refresh")
def manual_refresh(
    horizon_days_behind: int = Query(
        0, ge=0, le=365,
        description="Days back from today. 0 = today only; daily cron "
                    "default is 1 (today + yesterday). Wider values "
                    "back-fill past dates.",
    ),
) -> Dict[str, Any]:
    """Manually run the reconciliation refresh -- same code path the
    daily cron uses. Writes to yf_sales_transactions for past + today.
    Future dates are out of scope by design (no transactions yet).
    """
    return refresh_reconciliation(
        horizon_days_behind=int(horizon_days_behind),
        settings=get_settings(),
    )

@router.get("/van-load", response_model=VanLoadResponse)
def van_load(
    route_code: str = Query(..., description="Route code, e.g. 9105"),
    date: str = Query(..., pattern=_DATE_RE, description="YYYY-MM-DD"),
    svc: VanLoadService = Depends(get_van_load_service),
):
    """Per-item van composition for one (route, date)."""
    return svc.get(route_code, date)

@router.get("/recommend", response_model=ReconciliationResponse)
def recommend(
    route_code: str = Query(...),
    date: str = Query(..., pattern=_DATE_RE, description="YYYY-MM-DD"),
    typical_lookback_days: int = Query(
        default_factory=lambda: get_settings().reconciliation_default_lookback_days,
        ge=1,
        le=365,
        description="Bounded by settings.reconciliation_max_lookback_days at runtime.",
    ),
    bias: BiasService = Depends(get_bias_service),
    van: VanLoadService = Depends(get_van_load_service),
    engine: ReconciliationEngine = Depends(get_reconciliation_engine),
    artifact_svc: ArtifactService = Depends(get_artifact_service),
):
    """V5_b load recommendation for one (route, date), joined with the
    actual van composition so the UI can show recommended | on-van | sold
    in a single row per item."""
    settings = get_settings()
    typical_lookback_days = min(
        int(typical_lookback_days),
        int(settings.reconciliation_max_lookback_days),
    )
    composition = van.get(route_code, date)
    if not composition.get("available"):
        return ReconciliationResponse(
            available=False,
            message=composition.get("message", "van composition unavailable"),
            route_code=route_code, date=date,
        )

    fc_df, _ = artifact_svc.get_future_forecast(
        route_code=route_code,
        limit=int(get_settings().reconciliation_forecast_limit),
    )
    target_dt = pd.Timestamp(date).normalize()
    fcst_lookup: dict[str, dict] = {}
    pred_col = (
        "prediction" if "prediction" in fc_df.columns else
        "Predicted"  if "Predicted"  in fc_df.columns else
        None
    )
    if not fc_df.empty and pred_col is not None:
        fc_df = fc_df.copy()
        fc_df["TrxDate"] = pd.to_datetime(fc_df["TrxDate"], errors="coerce").dt.normalize()
        fc_df = fc_df[(fc_df.TrxDate == target_dt) & (fc_df[pred_col] > 0)]
        fc_df = fc_df.sort_values(pred_col, ascending=False).drop_duplicates(["ItemCode"])
        cls_col = (
            "class"        if "class"        in fc_df.columns else
            "DemandClass"  if "DemandClass"  in fc_df.columns else None
        )
        name_col = (
            "ItemName" if "ItemName" in fc_df.columns else
            "item_name" if "item_name" in fc_df.columns else None
        )
        # ``iterrows`` (not ``itertuples``) -- the artifact frame has a
        # column literally named ``class``, which is a Python keyword
        # and gets renamed to ``_<n>`` in itertuples namedtuples. Series
        # row access by column name avoids the renaming entirely.
        fcst_lookup = {
            str(row["ItemCode"]): {
                "predicted": float(row[pred_col] or 0.0),
                "demand_class": (row[cls_col] if cls_col else None),
                "item_name": (str(row[name_col] or "") if name_col else ""),
            }
            for _, row in fc_df.iterrows()
        }

    typical = van.typical_allocation(route_code, date, typical_lookback_days)

    composition_items = {it["item_code"]: it for it in composition.get("items", [])}
    union_keys = set(composition_items) | set(fcst_lookup)

    out_items = []
    rec_total = 0.0
    # Recommended-side breakdown (mirrors actual side: carried + fresh).
    # Counts and quantities come out of one pass so the totals can never
    # drift from the items list.
    rec_carried_qty = 0.0
    rec_carried_items = 0
    rec_issue_items = 0
    rec_van_load_items = 0
    # Actual-side per-component item counts (van_load_service emits a
    # single ``items_count`` only). Same single-pass derivation.
    actual_carried_items = 0
    actual_issued_items = 0

    for item_code in sorted(union_keys):
        comp = composition_items.get(item_code)
        fc = fcst_lookup.get(item_code)
        opening = float(comp["past_leftover"]) if comp else 0.0
        forecast_val = float(fc["predicted"]) if fc else 0.0
        item_name = ((fc.get("item_name") if fc else None)
                     or (comp.get("item_name") if comp else "")
                     or "")

        rec = engine.recommend_one(
            item_code=item_code,
            item_name=item_name,
            demand_class=fc["demand_class"] if fc else None,
            forecast=forecast_val,
            bias_pct=bias.lookup(route_code, item_code),
            calibration_ratio=bias.get_calibration_table().get(
                (str(route_code), str(item_code))
            ),
            opening_stock=opening,
            typical_alloc=typical.get(item_code),
            # Match the production batch path (enrich.py:324) and the
            # back-test (past_performance):  use_carry_floor=False. With
            # the floor on, this single-row endpoint produced numbers
            # 50% above the same row's value in the DB / drawer, which
            # confused supervisors. Off everywhere = one consistent
            # number for the same (route, item, day) regardless of
            # which surface asks.
            use_carry_floor=False,
        )
        merged = rec.to_dict()
        # Reconciled-only contract on the wire: the engine's ``forecast_raw``
        # (raw model output before V5_b) must never leave the backend.
        # Drop it here and from the totals below.
        merged.pop("forecast_raw", None)
        if comp:
            for f in ("today_allocation", "van_load", "sold_qty",
                      "bad_return_qty", "good_return_qty",
                      "leftover_now", "end_closing", "category_code", "category_name"):
                merged[f] = comp.get(f)
        else:
            for f in ("today_allocation", "van_load", "sold_qty",
                      "bad_return_qty", "good_return_qty", "leftover_now"):
                merged[f] = 0
            merged["end_closing"] = None

        # Per-row recommended composition (single source of truth: the
        # engine already returned ``recommended_load`` and ``opening_stock``).
        projected = float(rec.recommended_load) + float(rec.opening_stock)
        merged["projected_van_load"] = round(projected, 2)
        merged["carried_qty"] = round(float(rec.opening_stock), 2)
        merged["issue_qty"] = round(float(rec.recommended_load), 2)

        out_items.append(merged)
        rec_total += rec.recommended_load

        if rec.opening_stock > 0:
            rec_carried_qty += rec.opening_stock
            rec_carried_items += 1
        if rec.recommended_load > 0:
            rec_issue_items += 1
        if projected > 0:
            rec_van_load_items += 1
        if comp:
            if float(comp.get("past_leftover") or 0) > 0:
                actual_carried_items += 1
            if float(comp.get("today_allocation") or 0) > 0:
                actual_issued_items += 1

    out_items.sort(key=lambda x: (x.get("recommended_load", 0), x.get("van_load", 0)),
                   reverse=True)

    totals = dict(composition.get("totals", {}))
    # Reconciled-only wire contract: only the V5_b-reconciled total
    # leaves the backend. ``rec_total`` is the per-route sum;
    # ``recommended_van_load_total`` below carries reconciled + leftover.
    totals["recommended_load_total"] = round(rec_total, 2)
    # Recommended-side totals -- shape mirrors the actual van composition
    # so the UI can render the two cards from one shared component.
    totals["recommended_carried_qty"] = round(rec_carried_qty, 2)
    totals["recommended_carried_items"] = rec_carried_items
    totals["recommended_issue_qty"] = round(rec_total, 2)
    totals["recommended_issue_items"] = rec_issue_items
    totals["recommended_van_load_total"] = round(rec_total + rec_carried_qty, 2)
    totals["recommended_van_load_items"] = rec_van_load_items
    # Actual-side per-component item counts for the same UI shape.
    totals["past_leftover_items"] = actual_carried_items
    totals["today_allocation_items"] = actual_issued_items
    if totals.get("van_load_total"):
        totals["recommended_vs_van_load_pct"] = round(
            (rec_total - totals["van_load_total"]) / totals["van_load_total"] * 100, 2
        )

    return ReconciliationResponse(
        available=True,
        route_code=route_code,
        date=date,
        source=composition.get("source"),
        items=out_items,
        totals=totals,
        fetched_at=composition.get("fetched_at"),
    )

@router.get("/past-performance", response_model=PastPerformanceResponse)
def past_performance(
    route_code: str = Query(..., description="Route code"),
    start_date: str = Query(..., pattern=_DATE_RE,
        description="Inclusive lower bound of the window (YYYY-MM-DD)"),
    end_date:   str = Query(..., pattern=_DATE_RE,
        description="Inclusive upper bound of the window (YYYY-MM-DD), >= start_date"),
    item_codes: list[str] = Query(default_factory=list, alias="item_codes",
        description="Optional whitelist of ItemCodes to scope every total/series to."),
    category_codes: list[str] = Query(default_factory=list, alias="category_codes",
        description="Optional whitelist of CategoryCodes (or names if codes unavailable)."),
    van: VanLoadService = Depends(get_van_load_service),
    artifact_svc: ArtifactService = Depends(get_artifact_service),
):
    """Single canonical source for the AccuracyDrawer.

    Two independent worlds, plotted side by side:

      * ``rep_van_load[d]``         = past_leftover[d] + today_allocation[d]
        Real truck reality. ``past_leftover`` is the rep's actual
        ClosingQty on day d-1 (forward-filled across calendar gaps);
        ``today_allocation`` is what the depot actually issued.

      * ``recommended_van_load[d]`` = our_leftover[d] + our_fresh[d]
        Counterfactual simulation: our policy from day 1 of the window.
        ``our_leftover[1]`` is 0 by construction (clean start, no
        inheritance from the rep's prior decisions); subsequent days
        carry ``max(0, our_van_load[d-1] - actual_sold[d-1])`` forward.
        ``our_fresh[d]`` is the engine's bias-corrected, calibration-
        scaled, leftover-aware fresh recommendation given the simulated
        opening. Surfaces under the legacy field names
        ``recommended_carried`` / ``recommended_fresh`` / ``recommended_van_load``
        to keep the chart wire-shape stable.

      * ``actual_sold[d]`` -- invoiced demand for the day (ground truth).

    Per-item totals + holding cost compare each policy against the same
    sold floor, but each side uses its OWN truck weight (rep's real
    load on rep side, our simulated load on our side). Apples to apples.
    Anchor scope is unchanged: only (route, item) pairs with
    ``Predicted > 0`` flow into either side.
    """
    settings = get_settings()
    # Reject ranges wider than the configured cap so a fat-fingered date
    # selection never burns the engine on a multi-year scan. Surface as
    # ``available=False`` so the UI renders empty-state, not an error.
    # The regex on the Query param admits regex-pass-but-invalid dates
    # like ``2026-13-01``; pd.Timestamp raises on those, so wrap the
    # parse and convert the failure into the same envelope shape
    # downstream handlers expect.
    try:
        span_days = (
            pd.Timestamp(end_date).normalize() - pd.Timestamp(start_date).normalize()
        ).days + 1
    except (ValueError, TypeError) as exc:
        return PastPerformanceResponse(
            available=False,
            message=f"invalid reporting_period: {exc}",
            route_code=str(route_code),
            start_date=start_date, end_date=end_date,
            lookback_days=0, active_days=0,
        )
    if span_days < 1:
        return PastPerformanceResponse(
            available=False,
            message=f"reporting_period inverted: start_date={start_date} > end_date={end_date}",
            route_code=str(route_code),
            start_date=start_date, end_date=end_date,
            lookback_days=0, active_days=0,
        )
    if span_days > int(settings.reconciliation_max_lookback_days):
        return PastPerformanceResponse(
            available=False,
            message=(
                f"reporting_period too wide: requested {span_days} days, "
                f"cap is {settings.reconciliation_max_lookback_days}"
            ),
            route_code=str(route_code),
            start_date=start_date, end_date=end_date,
            lookback_days=span_days, active_days=0,
        )
    rcode = str(route_code)
    base = van.past_performance(route_code, start_date=start_date, end_date=end_date)
    daily_rows = base.get("daily", [])
    if not daily_rows:
        return PastPerformanceResponse(
            available=False,
            message="no activity in window",
            route_code=route_code,
            start_date=base.get("start_date"),
            end_date=base.get("end_date"),
            lookback_days=span_days,
            active_days=0,
        )

    # Resolve filters into one ItemCode whitelist. Category narrows the
    # whitelist further if provided. Empty lists = no filter (all items).
    item_whitelist: set[str] | None = (
        {str(x).strip() for x in item_codes if str(x).strip()} if item_codes else None
    )
    if category_codes:
        cat_set = {str(x).strip() for x in category_codes if str(x).strip()}
        # Resolve via the catalog -- map category codes/names -> item codes
        catalog_df = van._load_csv(van._s.sales_recent_file)
        if not catalog_df.empty and "CategoryName" in catalog_df.columns:
            cat_items = set(
                catalog_df[catalog_df.CategoryName.astype(str).isin(cat_set)]
                .ItemCode.astype(str).unique()
            )
            item_whitelist = (
                cat_items if item_whitelist is None else item_whitelist & cat_items
            )

    # ---- Forecast frame in window, route-scoped ----
    window_dates = {pd.Timestamp(r["date"]).normalize() for r in daily_rows}
    fc_df, _ = artifact_svc.get_future_forecast(
        route_code=route_code,
        limit=int(get_settings().reconciliation_forecast_limit),
    )
    pred_col = (
        "prediction" if "prediction" in fc_df.columns else
        "Predicted"  if "Predicted"  in fc_df.columns else
        None
    )
    # ``fc_df_full`` retains rows for the day immediately AFTER the window
    # so the per-(item, date) carry-out lookup below can read
    # ``opening_stock[d+1]`` -- the SAME persisted column the Van Load tile
    # renders as "Carried from yesterday" on day+1. Reading this here
    # (instead of the in-row ``leftover_to_next_day[d]``) eliminates the
    # cross-pass drift: ``opening_stock[d+1]`` is written by the day+1
    # reconciliation with fresher inputs, so it's the canonical answer
    # to "what carries from d into d+1". One source per concept.
    fc_df_full = fc_df
    if not fc_df.empty and pred_col is not None:
        fc_df = fc_df.copy()
        fc_df["TrxDate"] = pd.to_datetime(fc_df["TrxDate"], errors="coerce").dt.normalize()
        # Single source of truth for the activity predicate -- the same
        # helper ``ArtifactService.van_load_view`` calls. Sharing one
        # definition makes scope drift between Past Performance and the
        # Van Load tile physically impossible: both pass through the
        # same boolean mask. See ``activity_mask`` in enrich.py for the
        # column list and rationale.
        from demand_forecasting_pipeline.services.reconciliation.enrich import (
            activity_mask,
        )
        fc_df = fc_df[activity_mask(fc_df)]
        fc_df["RouteCode"] = fc_df["RouteCode"].astype(str)
        fc_df["ItemCode"]  = fc_df["ItemCode"].astype(str)
        # fc_df_full keeps a route-scoped, carry-aware view for cross-day
        # lookups (next-day opening_stock); fc_df itself stays window-only.
        fc_df_full = fc_df.copy()
        fc_df = fc_df[fc_df.TrxDate.isin(window_dates)]
        if item_whitelist is not None:
            fc_df = fc_df[fc_df.ItemCode.isin(item_whitelist)]
            fc_df_full = fc_df_full[fc_df_full.ItemCode.isin(item_whitelist)]

    # ---- Per-item closing index (direct, single-day lookup) ----------
    # Rule, validated empirically against 21,073 (item, day) cells:
    #   opening[d, item] = ClosingQty logged for (route, item, d-1)
    #                    = 0 otherwise.
    # The system never logs ``ClosingQty = 0`` (audit: 0 zero-rows out
    # of 942 logged closings on route 9105 alone), so a missing row IS
    # the schema's way of saying "rep had nothing left." 94.4% of cells
    # with a missing closing also satisfy the operational identity for
    # zero leftover, validating the convention.
    # Closing range covers exactly the active-day span plus one calendar
    # day before, to provide opening on the first active day in window.
    start_dt = min(window_dates) if window_dates else pd.Timestamp(end_date).normalize()
    end_dt   = max(window_dates) if window_dates else pd.Timestamp(end_date).normalize()
    # Forward-fill window for closing-stock lookups. Mirrors the live
    # enrich.py path: when closing_stock.csv has a calendar gap (route
    # didn't run that day, or the data pipeline missed the row), the
    # back-test must walk back up to ``opening_stock_lookback_days`` for
    # the most recent closing rather than defaulting to opening = 0,
    # which would silently treat the truck as empty and inflate the
    # back-test's recommendation. The CSV pull covers (lookback + 1)
    # extra days before window start so the first day in the window has
    # ffill source available.
    # ---- Anchor item set -----------------------------------------------
    # ``anchor_items`` -- the (route, item) pairs with Predicted > 0 in
    # the window -- is the ONLY scope this endpoint compares. Items the
    # rep loaded but we did not predict are explicitly out of scope so
    # we never claim phantom credit on items we made no call on.
    # Bias correction can never zero out a positive prediction (the
    # engine clamps the denominator at 1 + ``_BIAS_DENOM_MIN`` > 0), so
    # ``Predicted > 0`` is sufficient to define the anchor.
    anchor_items: set[str] = (
        set(fc_df.ItemCode.astype(str).unique())
        if (not fc_df.empty and pred_col is not None) else set()
    )

    # No forecasted items in the window means nothing to compare. Bail
    # before touching the rep CSVs so we never present a "rep loaded X,
    # we recommend 0" story which is just "we made no recommendation".
    if not anchor_items:
        return PastPerformanceResponse(
            available=False,
            message=(
                "no reconciled recommendation in window for this scope "
                "(no (route, item) pairs with Predicted > 0)"
            ),
            route_code=route_code,
            start_date=base.get("start_date"),
            end_date=base.get("end_date"),
            lookback_days=span_days,
            active_days=0,
        )

    # ---- Sales CSV for price + coverage lookups (item catalog source).
    # Rep van load, today's allocation, and actual sold all flow through
    # ``fc_df`` (= yf_sales_transactions via the sales_transactions.csv
    # mirror) -- single source, identical to what the cron persists.
    sales_df = van._load_csv(van._s.sales_recent_file)

    def _scoped(df: pd.DataFrame, qty_col: str) -> pd.DataFrame:
        if df.empty or qty_col not in df.columns:
            return df.iloc[0:0]
        return df[(df.RouteCode.astype(str) == rcode)
                  & (df.TrxDate.isin(window_dates))
                  & (df.ItemCode.astype(str).isin(anchor_items))]

    # ---- Counterfactual forward simulation ---------------------------
    # Two independent worlds, plotted side by side:
    #
    #   THEIR PATH (real, from the rep's actual loading):
    #     rep_van_load[d] = past_leftover[d] + today_allocation[d]
    #     past_leftover   -- ClosingQty[d-1] from closing_stock.csv
    #     today_allocation-- AllocatedPC[d] from load_allocation.csv
    #
    #   OUR PATH (counterfactual, our policy from day 1 of the window):
    #     our_leftover[1] = 0   (clean start; we did not inherit rep's history)
    #     our_fresh[d]    = engine.recommend_batch given our_leftover[d]
    #     our_van_load[d] = our_leftover[d] + our_fresh[d]
    #     our_leftover[d+1] = max(0, our_van_load[d] - actual_sold[d])
    #
    # Wire field names retained for backward compat with the chart:
    #     recommended_carried  = our_leftover            (was rep's leftover alias)
    #     recommended_fresh    = our_fresh given our leftover
    #     recommended_van_load = our_leftover + our_fresh
    #
    # Sequential per-day pass: each tick uses the engine's full V5_b/L1/L4/calibration
    # logic with simulated leftover as opening, then updates the per-(item)
    # leftover state for the next tick.
    recommended_fresh_per_day: dict[str, float] = {}
    recommended_carried_per_day: dict[str, float] = {}
    recommended_van_load_per_day: dict[str, float] = {}
    # Per-(item, day) canonical reconciled values from fc_df. Keyed by
    # (item_code, date_str) so the per-(item, date) emission below reads
    # the same cell the per-day totals aggregate (single source of
    # truth, no drift).
    rec_carried_by_item_day: dict[tuple[str, str], float] = {}
    rec_fresh_by_item_day:   dict[tuple[str, str], float] = {}
    rec_van_by_item_day:     dict[tuple[str, str], float] = {}

    # Drawer aggregates the canonical reconciled values the daily cron
    # already wrote to yf_demand_forecast. Same cells the page-view tile
    # reads, so per-day totals and the headline match by construction --
    # no parallel engine call, no leftover simulation drift.
    rload_col = next(
        (c for c in ("recommended_load", "RecommendedLoad") if c in fc_df.columns),
        None,
    )
    opening_col = next(
        (c for c in ("opening_stock", "OpeningStock") if c in fc_df.columns),
        None,
    )
    # Rep-side persisted columns (single source of truth: yaumi_*).
    # Reading from fc_df (= yf_sales_transactions via _merge_sales_transactions)
    # eliminates the parallel CSV path that previously recomputed
    # past_leftover from closing_stock.csv and today_allocation from
    # load_allocation.csv. Same data, but one path -- the persisted
    # cron output -- so the AccuracyDrawer numbers byte-match
    # yf_sales_transactions.yaumi_total_van_load.
    rep_open_by_item_day:    dict[tuple[str, str], float] = {}
    rep_fresh_by_item_day:   dict[tuple[str, str], float] = {}
    rep_leftover_by_item_day: dict[tuple[str, str], float] = {}
    rep_van_by_item_day:     dict[tuple[str, str], float] = {}
    sold_by_item_day_fc:     dict[tuple[str, str], float] = {}
    rep_past_leftover_per_day: dict[str, float] = {}
    rep_today_alloc_per_day:   dict[str, float] = {}
    rep_van_load_per_day:      dict[str, float] = {}
    sold_per_day_fc:           dict[str, float] = {}

    if not fc_df.empty and pred_col is not None and rload_col is not None and opening_col is not None:
        canon = fc_df.assign(
            _ic   = fc_df.ItemCode.astype(str),
            _load = pd.to_numeric(fc_df[rload_col],  errors="coerce").fillna(0.0).clip(lower=0.0),
            _open = pd.to_numeric(fc_df[opening_col], errors="coerce").fillna(0.0).clip(lower=0.0),
            # Rep-side persisted columns. ``yaumi_*`` are written by
            # reconciliation_refresh from VW_GET_CLOSING_STOCK and
            # VW_GET_LOAD_ALLOCATION_DETAILS; clipping at 0 mirrors the
            # ingestion-side guard.
            _ryopen  = pd.to_numeric(
                fc_df.get("yaumi_opening_stock", 0), errors="coerce",
            ).fillna(0.0).clip(lower=0.0),
            _ryfresh = pd.to_numeric(
                fc_df.get("yaumi_fresh_load", 0), errors="coerce",
            ).fillna(0.0).clip(lower=0.0),
            _ryleft  = pd.to_numeric(
                fc_df.get("yaumi_leftover", 0), errors="coerce",
            ).fillna(0.0).clip(lower=0.0),
            _sold    = pd.to_numeric(
                fc_df.get("actual_sold", 0), errors="coerce",
            ).fillna(0.0).clip(lower=0.0),
        )
        canon["_van"]   = canon["_open"] + canon["_load"]
        canon["_ryvan"] = canon["_ryopen"] + canon["_ryfresh"]

        per_day = canon.groupby("TrxDate").agg(
            carried=("_open", "sum"),
            fresh=("_load", "sum"),
            van=("_van", "sum"),
            r_carried=("_ryopen", "sum"),
            r_fresh=("_ryfresh", "sum"),
            r_van=("_ryvan", "sum"),
            sold=("_sold", "sum"),
        )
        for d_ts, row in per_day.iterrows():
            d_str = pd.Timestamp(d_ts).strftime("%Y-%m-%d")
            recommended_carried_per_day[d_str]  = round(float(row["carried"]), 2)
            recommended_fresh_per_day[d_str]    = round(float(row["fresh"]), 2)
            recommended_van_load_per_day[d_str] = round(float(row["van"]), 2)
            rep_past_leftover_per_day[d_str]    = round(float(row["r_carried"]), 2)
            rep_today_alloc_per_day[d_str]      = round(float(row["r_fresh"]), 2)
            rep_van_load_per_day[d_str]         = round(float(row["r_van"]), 2)
            sold_per_day_fc[d_str]              = round(float(row["sold"]), 2)

        # Per-(item, day) breakdown -- one row per (item, date) the
        # per-(item, date) emission consumes verbatim.
        for _, r in canon.iterrows():
            d_str = pd.Timestamp(r["TrxDate"]).strftime("%Y-%m-%d")
            key = (str(r["_ic"]), d_str)
            rec_carried_by_item_day[key]      = float(r["_open"])
            rec_fresh_by_item_day[key]        = float(r["_load"])
            rec_van_by_item_day[key]          = float(r["_van"])
            rep_open_by_item_day[key]         = float(r["_ryopen"])
            rep_fresh_by_item_day[key]        = float(r["_ryfresh"])
            rep_leftover_by_item_day[key]     = float(r["_ryleft"])
            rep_van_by_item_day[key]          = float(r["_ryvan"])
            sold_by_item_day_fc[key]          = float(r["_sold"])

    # ---- Daily rebuild over anchor scope -----------------------------
    # Three chart lines:
    #   rep_van_load[d]         = past_leftover[d] + today_allocation[d]
    #                             (rep's PHYSICAL truck reality)
    #   recommended_van_load[d] = our_leftover[d] + our_fresh[d]
    #                             (counterfactual: our policy from day 1)
    #   actual_sold[d]          = invoiced demand
    # ``past_leftover`` and ``today_allocation`` keep their original
    # meaning (the rep's truth). ``recommended_carried`` and
    # ``recommended_fresh`` are now both OUR simulation -- no shared
    # leftover with the rep.
    # daily_rows + totals get FINAL values after the widening pass below
    # (so unforecasted-but-active items flow into both). Seed with the
    # anchor-only aggregates here to keep the structure populated for any
    # early-exit path; the recompute downstream overwrites with the wider
    # numbers once items_payload is complete.
    for row in daily_rows:
        d_key = row["date"]
        row["rep_van_load"]         = rep_van_load_per_day.get(d_key, 0.0)
        row["recommended_van_load"] = recommended_van_load_per_day.get(d_key, 0.0)
        row["actual_sold"]          = sold_per_day_fc.get(d_key, 0.0)

    # Per-item lookups for the items[] payload:
    #   * item-name (from forecast frame's name column, sales fallback)
    #   * per-item leftover that becomes the NEXT day's opening (read
    #     from ``fc_df_full.opening_stock`` on the first forecast date
    #     strictly after ``last_day_in_window``; same cell the cron's
    #     actuals-grounded simulation wrote).
    item_name_lookup: dict[str, str] = {}
    item_category_lookup: dict[str, str] = {}

    def _ingest_lookup(
        df: pd.DataFrame, col: str, target: dict[str, str]
    ) -> None:
        if df.empty or col not in df.columns:
            return
        pairs = (
            df.assign(_ic=df.ItemCode.astype(str))
            .groupby("_ic")[col]
            .agg(lambda s: next(
                (str(v).strip() for v in s if pd.notna(v) and str(v).strip()),
                "",
            ))
            .to_dict()
        )
        for k, v in pairs.items():
            if k not in target and v:
                target[k] = str(v)

    # Forecast frame first (canonical name on the dashboard), then sales
    # frame as a fallback for windows where the artifact's ItemName is
    # NaN (test/train rows merged via column intersection drop the name).
    if not fc_df.empty and pred_col is not None:
        for nc in ("ItemName", "item_name"):
            if nc in fc_df.columns:
                _ingest_lookup(fc_df, nc, item_name_lookup)
                break
    if not sales_df.empty:
        # Route-scoped (NOT anchor-scoped) so unforecasted items the rep
        # actually loaded/sold also get a categoryName + itemName on the
        # disjoint-set widening pass below. Same catalog the filter
        # dropdown reads -- by construction, the drawer's scope and the
        # filter's universe align.
        sales_scope = sales_df[
            sales_df.RouteCode.astype(str) == rcode
        ]
        for nc in ("ItemName", "item_name"):
            if nc in sales_scope.columns:
                _ingest_lookup(sales_scope, nc, item_name_lookup)
                break
        for cc in ("CategoryName", "category_name"):
            if cc in sales_scope.columns:
                _ingest_lookup(sales_scope, cc, item_category_lookup)
                break
    # ---- Per-(item, date) row emission --------------------------------
    # One row per (anchor_item, active_day) in the window with:
    #   * rep_van_load          -- the rep's physical truck total
    #   * recommended_van_load  -- engine's recommended truck total
    #   * actual_sold           -- invoiced demand for the day
    #   * actual_leftover       -- max(rep_van_load - actual_sold, 0)
    #   * recommended_leftover  -- max(recommended_van_load - actual_sold, 0)
    # Identity by construction: sum across rows equals the matching
    # totals fields (rep_van_load_total, recommended_van_load_total,
    # actual_sold_total, rep_leftover_units, our_leftover_units).
    sorted_active_days = sorted(window_dates)

    # Canonical-source leftover lookups -- read from yf_sales_transactions
    # so Past Performance and Van Load show identical numbers.
    rec_leftover_by_item_day: dict[tuple[str, str], float] = {}
    next_day_opening_by_item_date: dict[tuple[str, str], float] = {}
    if not fc_df_full.empty:
        op_col = next((c for c in ("opening_stock", "OpeningStock") if c in fc_df_full.columns), None)
        lo_col = next((c for c in ("leftover_to_next_day", "LeftoverToNextDay") if c in fc_df_full.columns), None)
        if op_col is not None or lo_col is not None:
            ic_arr = fc_df_full.ItemCode.astype(str).tolist()
            d_arr  = [d.strftime("%Y-%m-%d") for d in fc_df_full.TrxDate]
            op_arr = (
                pd.to_numeric(fc_df_full[op_col], errors="coerce").fillna(0.0).clip(lower=0.0).tolist()
                if op_col is not None else [None] * len(ic_arr)
            )
            lo_arr = (
                pd.to_numeric(fc_df_full[lo_col], errors="coerce").fillna(0.0).clip(lower=0.0).tolist()
                if lo_col is not None else [None] * len(ic_arr)
            )
            for ic, d_str2, op, lo in zip(ic_arr, d_arr, op_arr, lo_arr):
                if op is not None:
                    next_day_opening_by_item_date[(ic, d_str2)] = float(op)
                if lo is not None:
                    rec_leftover_by_item_day[(ic, d_str2)] = float(lo)

    def _recommended_leftover(ic_str: str, d_str: str, rec_van: float, sold_i: float) -> float:
        """opening_stock[d+1] -> leftover_to_next_day[d] -> naive fallback."""
        d_next = (pd.Timestamp(d_str) + pd.Timedelta(days=1)).strftime("%Y-%m-%d")
        v = next_day_opening_by_item_date.get((ic_str, d_next))
        if v is not None:
            return v
        v = rec_leftover_by_item_day.get((ic_str, d_str))
        if v is not None:
            return v
        return max(0.0, rec_van - sold_i)

    items_payload: list[PastPerformanceItem] = []
    for d_ts in sorted_active_days:
        d_str = d_ts.strftime("%Y-%m-%d")
        for ic in anchor_items:
            ic_str = str(ic)
            key = (ic_str, d_str)
            # Rep + sold sides: read persisted ``yaumi_*`` and
            # ``actual_sold`` from fc_df (single source -- same data
            # the cron wrote to yf_sales_transactions).
            past_left     = rep_open_by_item_day.get(key, 0.0)
            today_alloc_i = rep_fresh_by_item_day.get(key, 0.0)
            rep_van_i     = rep_van_by_item_day.get(key, past_left + today_alloc_i)
            rec_carry     = float(rec_carried_by_item_day.get(key, 0.0))
            rec_fresh     = float(rec_fresh_by_item_day.get(key, 0.0))
            rec_van       = float(rec_van_by_item_day.get(key, rec_carry + rec_fresh))
            sold_i        = sold_by_item_day_fc.get(key, 0.0)
            # actual_leftover: rep's measured closing on day d (yaumi_leftover).
            # rec_leftover  : opening_stock on day d+1 (canonical carry-out
            #                 -- same column the Van Load tile reads).
            actual_lo     = rep_leftover_by_item_day.get(key, max(0.0, rep_van_i - sold_i))
            rec_lo        = _recommended_leftover(ic_str, d_str, rec_van, sold_i)

            # Skip rows that are completely empty across every field --
            # no rep activity, no recommendation, no sale, no leftover.
            if (
                rep_van_i == 0.0
                and rec_van == 0.0
                and sold_i == 0.0
                and actual_lo == 0.0
                and rec_lo == 0.0
            ):
                continue
            items_payload.append(
                PastPerformanceItem(
                    itemCode=ic_str,
                    itemName=item_name_lookup.get(ic_str, ""),
                    categoryName=item_category_lookup.get(ic_str, ""),
                    date=d_str,
                    rep_van_load=round(rep_van_i, 2),
                    recommended_van_load=round(rec_van, 2),
                    actual_sold=round(sold_i, 2),
                    actual_leftover=round(actual_lo, 2),
                    recommended_leftover=round(rec_lo, 2),
                )
            )

    # ---- Disjoint-set widening: unforecasted-but-active items ---------
    # The drawer scope above is "items the engine forecasted in the
    # window" (anchor_items). Items the rep loaded or sold for this
    # route that we did NOT forecast would otherwise be invisible, which
    # creates a transparency hole AND makes SKU coverage circular.
    #
    # Widen scope to the filter dropdown's universe (sales_recent.csv
    # per route) so the drawer counts match the filter counts by
    # construction. For these unforecasted items:
    #   * rep_van           <- closing_stock.csv[d-1] + load_allocation.csv[d]
    #   * actual_sold       <- sales_recent.csv groupby (route, item, date)
    #   * recommended_van   = 0 (we made no call)
    # The two item sets (anchor / non-anchor) are disjoint, so each
    # row has exactly ONE source.
    non_anchor_items: set[str] = set()
    if not sales_scope.empty and "ItemCode" in sales_scope.columns:
        all_route_items = set(sales_scope.ItemCode.astype(str).unique())
        non_anchor_items = all_route_items - anchor_items
        if item_whitelist is not None:
            non_anchor_items &= item_whitelist

    if non_anchor_items:
        closing_csv = van._load_csv(van._s.closing_stock_file)
        alloc_csv   = van._load_csv(van._s.load_allocation_file)

        # Build per-(item, date) lookups for the non-anchor scope from
        # the upstream CSVs (route-scoped). We need prior-day closing
        # for the opening component of rep_van, so the closing read
        # covers the window MINUS one day at the start.
        sorted_active = sorted(window_dates)
        first_active  = sorted_active[0]
        last_active   = sorted_active[-1]
        closing_start = first_active - pd.Timedelta(days=1)

        def _route_window(df: pd.DataFrame, start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
            if df.empty or "TrxDate" not in df.columns:
                return df.iloc[0:0]
            return df[(df.RouteCode.astype(str) == rcode)
                      & (df.TrxDate >= start) & (df.TrxDate <= end)
                      & df.ItemCode.astype(str).isin(non_anchor_items)]

        # ClosingQty[d] -- end-of-day stock for each (item, date).
        # ClosingQty[d-1] is THIS day's opening (carry) per the chain.
        closing_window = _route_window(closing_csv, closing_start, last_active)
        prior_close: dict[tuple[str, str], float] = {}
        same_day_close: dict[tuple[str, str], float] = {}
        if not closing_window.empty and "ClosingQty" in closing_window.columns:
            tmp = closing_window.assign(
                _ic = closing_window.ItemCode.astype(str),
                _qty = pd.to_numeric(closing_window.ClosingQty, errors="coerce").fillna(0.0).clip(lower=0.0),
            )
            for ic, d, qty in zip(tmp._ic, tmp.TrxDate, tmp._qty):
                same_day_close[(ic, d.strftime("%Y-%m-%d"))] = float(qty)
            # Map each row's date forward by one day -- ClosingQty[d-1]
            # is the opening (carry) on date d. Saves a join on the hot path.
            for ic, d, qty in zip(tmp._ic, tmp.TrxDate, tmp._qty):
                nxt = (d + pd.Timedelta(days=1)).strftime("%Y-%m-%d")
                prior_close[(ic, nxt)] = float(qty)

        # AllocatedPC[d] -- today's depot-issued fresh load.
        alloc_window = _route_window(alloc_csv, first_active, last_active)
        alloc_lookup: dict[tuple[str, str], float] = {}
        if not alloc_window.empty and "AllocatedPC" in alloc_window.columns:
            tmp = alloc_window.assign(
                _ic = alloc_window.ItemCode.astype(str),
                _qty = pd.to_numeric(alloc_window.AllocatedPC, errors="coerce").fillna(0.0).clip(lower=0.0),
            )
            for ic, d, qty in zip(tmp._ic, tmp.TrxDate, tmp._qty):
                alloc_lookup[(ic, d.strftime("%Y-%m-%d"))] = float(qty)

        # TotalQuantity from sales_recent.csv (route-scoped, window-scoped).
        sales_window = sales_scope[
            sales_scope.TrxDate.isin(window_dates)
            & sales_scope.ItemCode.astype(str).isin(non_anchor_items)
        ] if "TrxDate" in sales_scope.columns else sales_scope.iloc[0:0]
        sold_lookup: dict[tuple[str, str], float] = {}
        if not sales_window.empty and "TotalQuantity" in sales_window.columns:
            tmp = sales_window.assign(
                _ic = sales_window.ItemCode.astype(str),
                _qty = pd.to_numeric(sales_window.TotalQuantity, errors="coerce").fillna(0.0).clip(lower=0.0),
            )
            grp = tmp.groupby(["_ic", "TrxDate"])._qty.sum()
            for (ic, d), qty in grp.items():
                sold_lookup[(ic, d.strftime("%Y-%m-%d"))] = float(qty)

        for d_ts in sorted_active:
            d_str = d_ts.strftime("%Y-%m-%d")
            for ic_str in non_anchor_items:
                key = (ic_str, d_str)
                past_left     = prior_close.get(key, 0.0)
                today_alloc_i = alloc_lookup.get(key, 0.0)
                rep_van_i     = past_left + today_alloc_i
                sold_i        = sold_lookup.get(key, 0.0)
                actual_lo     = max(0.0, rep_van_i - sold_i)
                # Skip rows with zero activity (avoids 13 catalog ghosts
                # × 22 days = 286 empty rows for a typical route).
                if rep_van_i == 0.0 and sold_i == 0.0 and actual_lo == 0.0:
                    continue
                items_payload.append(
                    PastPerformanceItem(
                        itemCode=ic_str,
                        itemName=item_name_lookup.get(ic_str, ""),
                        categoryName=item_category_lookup.get(ic_str, ""),
                        date=d_str,
                        rep_van_load=round(rep_van_i, 2),
                        recommended_van_load=0.0,
                        actual_sold=round(sold_i, 2),
                        actual_leftover=round(actual_lo, 2),
                        recommended_leftover=0.0,
                    )
                )

    # Sort: date asc, then recommended_leftover desc (items still left
    # over under our policy surface first), then recommended_van_load
    # desc, itemCode asc for determinism.
    items_payload.sort(
        key=lambda it: (
            it.date,
            -it.recommended_leftover,
            -it.recommended_van_load,
            it.itemCode,
        )
    )

    # ---- Recompute daily aggregates + totals from items_payload --------
    # Now that items_payload includes the disjoint-set widening (forecasted
    # AND unforecasted activity), re-derive the per-day chart series and
    # the headline totals from this single source. Identity by construction:
    #   sum(daily[*].field)  == sum(items[*].field)  for every field below.
    # Five per-day series so the chart's toggle (Van load / Leftovers)
    # can switch between them without a re-fetch.
    rep_per_day:    dict[str, float] = {}
    rec_per_day:    dict[str, float] = {}
    sold_per_day:   dict[str, float] = {}
    rep_lo_per_day: dict[str, float] = {}
    rec_lo_per_day: dict[str, float] = {}
    for it in items_payload:
        rep_per_day[it.date]    = rep_per_day.get(it.date, 0.0)    + float(it.rep_van_load)
        rec_per_day[it.date]    = rec_per_day.get(it.date, 0.0)    + float(it.recommended_van_load)
        sold_per_day[it.date]   = sold_per_day.get(it.date, 0.0)   + float(it.actual_sold)
        rep_lo_per_day[it.date] = rep_lo_per_day.get(it.date, 0.0) + float(it.actual_leftover)
        rec_lo_per_day[it.date] = rec_lo_per_day.get(it.date, 0.0) + float(it.recommended_leftover)
    for row in daily_rows:
        d_key = row["date"]
        row["rep_van_load"]         = round(rep_per_day.get(d_key, 0.0),    2)
        row["recommended_van_load"] = round(rec_per_day.get(d_key, 0.0),    2)
        row["actual_sold"]          = round(sold_per_day.get(d_key, 0.0),   2)
        row["actual_leftover"]      = round(rep_lo_per_day.get(d_key, 0.0), 2)
        row["recommended_leftover"] = round(rec_lo_per_day.get(d_key, 0.0), 2)
    rep_van_load_total         = sum(it.rep_van_load          for it in items_payload)
    recommended_van_load_total = sum(it.recommended_van_load  for it in items_payload)
    sold_total                 = sum(it.actual_sold           for it in items_payload)

    # ---- Category rollup ----------------------------------------------
    # Aggregate items_payload by categoryName -- one row per category
    # across the whole window. Same source as the per-(item, date)
    # rows, so totals reconcile by construction. Items without a
    # categoryName fall under "Uncategorised" so the rollup is
    # exhaustive (sum of category rows == items_payload aggregate).
    _UNCATEGORISED = "Uncategorised"
    cat_accum: dict[str, dict[str, Any]] = {}
    for it in items_payload:
        cat = (it.categoryName or "").strip() or _UNCATEGORISED
        bucket = cat_accum.setdefault(cat, {
            "categoryName": cat,
            "skus": set(),
            "rep_van_load": 0.0,
            "recommended_van_load": 0.0,
            "actual_sold": 0.0,
            "actual_leftover": 0.0,
            "recommended_leftover": 0.0,
        })
        bucket["skus"].add(it.itemCode)
        bucket["rep_van_load"]         += float(it.rep_van_load)
        bucket["recommended_van_load"] += float(it.recommended_van_load)
        bucket["actual_sold"]          += float(it.actual_sold)
        bucket["actual_leftover"]      += float(it.actual_leftover)
        bucket["recommended_leftover"] += float(it.recommended_leftover)
    categories_payload: list[PastPerformanceCategoryRow] = [
        PastPerformanceCategoryRow(
            categoryName=b["categoryName"],
            skus=len(b["skus"]),
            rep_van_load=round(b["rep_van_load"], 2),
            recommended_van_load=round(b["recommended_van_load"], 2),
            actual_sold=round(b["actual_sold"], 2),
            actual_leftover=round(b["actual_leftover"], 2),
            recommended_leftover=round(b["recommended_leftover"], 2),
        )
        for b in cat_accum.values()
    ]
    # Sort: recommended_van_load desc (biggest category first), then
    # categoryName asc for determinism on ties.
    categories_payload.sort(
        key=lambda c: (-c.recommended_van_load, c.categoryName)
    )

    # Served-units estimate under our load: what we'd have served if our
    # truck had rolled out, per item capped at the truck's capacity.
    #   served_i = min(actual_sold_i, recommended_van_load_i)
    # Aggregate gives the demand we'd have served under our right-sized
    # load -- the honest counter to "but lighter loads miss sales".
    served_units = round(sum(
        min(float(it.actual_sold or 0.0), float(it.recommended_van_load or 0.0))
        for it in items_payload
    ), 2)

    # Leftovers comparison -- sum the per-row fields directly so the
    # aggregate is byte-identical to what the breakdown table renders.
    rep_leftover_units = round(sum(it.actual_leftover       for it in items_payload), 2)
    our_leftover_units = round(sum(it.recommended_leftover  for it in items_payload), 2)
    leftover_units_saved = round(rep_leftover_units - our_leftover_units, 2)
    leftover_pct_saved = (
        round(leftover_units_saved / rep_leftover_units * 100.0)
        if rep_leftover_units > 0 else 0
    )

    # SKU coverage -- of the SKUs the rep actually sold across the
    # window, how many did our recommendation also cover? Roll up to the
    # SKU level first so a one-day blip on a slow SKU does not double-count.
    sold_skus: set[str] = set()
    covered_skus: set[str] = set()
    rec_skus_by_item: dict[str, float] = {}
    sold_skus_by_item: dict[str, float] = {}
    for it in items_payload:
        sold_skus_by_item[it.itemCode] = sold_skus_by_item.get(it.itemCode, 0.0) + float(it.actual_sold or 0.0)
        rec_skus_by_item[it.itemCode]  = rec_skus_by_item.get(it.itemCode, 0.0)  + float(it.recommended_van_load or 0.0)
    for ic, sold_q in sold_skus_by_item.items():
        if sold_q > 0:
            sold_skus.add(ic)
            if rec_skus_by_item.get(ic, 0.0) > 0:
                covered_skus.add(ic)
    skus_sold_count    = len(sold_skus)
    skus_covered_count = len(covered_skus)
    skus_coverage_pct  = (
        round(skus_covered_count / skus_sold_count * 100.0)
        if skus_sold_count > 0 else 0
    )

    totals = {
        "rep_van_load_total":         round(rep_van_load_total, 2),
        "recommended_van_load_total": round(recommended_van_load_total, 2),
        "actual_sold_total":          round(sold_total, 2),
        "served_units":               served_units,
        "active_days":                len(daily_rows),
        # Leftovers comparison (rep vs our policy). Same items_payload
        # source as the headline totals -- per-(item, date) max(load - sold, 0)
        # summed, so the math is auditable from the rendered breakdown table.
        "rep_leftover_units":         rep_leftover_units,
        "our_leftover_units":         our_leftover_units,
        "leftover_units_saved":       leftover_units_saved,
        "leftover_pct_saved":         leftover_pct_saved,
        # SKU coverage -- of the items the rep actually sold across the
        # window, how many did our recommendation also cover (recommended_van_load > 0)?
        "skus_sold":                  skus_sold_count,
        "skus_covered":               skus_covered_count,
        "skus_coverage_pct":          skus_coverage_pct,
    }

    # Identity checks: per-(item, date) sums must reconcile with the
    # aggregate totals block. Threshold lifted from settings -- rounding
    # tolerance only. Drift logs a warning; emission is unaffected.
    drift_threshold = float(settings.reconciliation_items_drift_threshold)
    items_rep_sum      = round(sum(it.rep_van_load          for it in items_payload), 2)
    items_van_load_sum = round(sum(it.recommended_van_load  for it in items_payload), 2)
    items_sold_sum     = round(sum(it.actual_sold           for it in items_payload), 2)

    def _drift(label: str, lhs: float, rhs: float) -> None:
        if abs(lhs - rhs) > drift_threshold:
            logger.warning(
                "past_performance %s items-sum %.2f does not reconcile with "
                "totals %.2f (drift %.2f) for route=%s window=%s..%s span=%d",
                label, lhs, rhs, lhs - rhs, rcode, start_date, end_date, span_days,
            )

    _drift("rep_van_load",         items_rep_sum,      totals["rep_van_load_total"])
    _drift("recommended_van_load", items_van_load_sum, totals["recommended_van_load_total"])
    _drift("actual_sold",          items_sold_sum,     totals["actual_sold_total"])

    # Disjoint-set integrity check: every itemCode appears in
    # items_payload exactly once per date. A duplicate would indicate
    # an anchor item leaking into the widening pass, double-counting
    # rep_van and actual_sold in the headline tiles.
    seen_keys: set[tuple[str, str]] = set()
    dup_count = 0
    for it in items_payload:
        k = (it.itemCode, it.date)
        if k in seen_keys:
            dup_count += 1
        seen_keys.add(k)
    if dup_count > 0:
        logger.warning(
            "past_performance disjoint-set violation: %d duplicate "
            "(itemCode, date) rows in items_payload for route=%s window=%s..%s",
            dup_count, rcode, start_date, end_date,
        )

    return PastPerformanceResponse(
        available=True,
        route_code=route_code,
        start_date=base.get("start_date"),
        end_date=base.get("end_date"),
        lookback_days=span_days,
        active_days=len(daily_rows),
        daily=daily_rows,
        totals=totals,
        categories=categories_payload,
        items=items_payload,
    )
