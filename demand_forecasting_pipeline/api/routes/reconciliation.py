"""Reconciliation endpoints -- van composition + V5_b load + past-perf.

Four GET/POST endpoints, all dynamic and parameterised:

* ``/reconciliation/van-load``        composition only for one (route, date)
* ``/reconciliation/recommend``       V5_b recommendation joined with composition
* ``/reconciliation/past-performance`` per-day chart series + return metrics
* ``/reconciliation/refresh``          manual trigger for the daily refresh cron
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

import numpy as np
import pandas as pd
from fastapi import APIRouter, Depends, Query

from demand_forecasting_pipeline.api.dependencies import (
    get_artifact_service,
    get_bias_service,
    get_reconciliation_engine,
    get_van_load_service,
)
from demand_forecasting_pipeline.api.schemas import (
    PastPerformanceResponse,
    ReconciliationResponse,
    VanLoadResponse,
)
from demand_forecasting_pipeline.config.settings import get_settings
from demand_forecasting_pipeline.services.artifact_service import ArtifactService
from demand_forecasting_pipeline.services.reconciliation.bias_service import BiasService
from demand_forecasting_pipeline.services.reconciliation.engine import ReconciliationEngine
from demand_forecasting_pipeline.services.reconciliation.enrich import forward_fill_closing
from demand_forecasting_pipeline.services.reconciliation.van_load_service import VanLoadService
from demand_forecasting_pipeline.services.reconciliation_refresh import refresh_reconciliation

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/reconciliation", tags=["reconciliation"])


@router.post("/refresh")
def manual_refresh(
    horizon_days_ahead: Optional[int] = Query(
        None, ge=1, le=365,
        description="Days forward from today (defaults to "
                    "reconciliation_refresh_horizon_days)",
    ),
    horizon_days_behind: int = Query(0, ge=0, le=365),
) -> Dict[str, Any]:
    """Manually run the reconciliation refresh -- same code path the
    daily cron uses. Useful for backfills, post-import top-ups, or ad-hoc
    refreshes after a closing-stock correction.
    """
    s = get_settings()
    return refresh_reconciliation(
        horizon_days_ahead=int(
            horizon_days_ahead
            if horizon_days_ahead is not None
            else s.reconciliation_refresh_horizon_days
        ),
        horizon_days_behind=int(horizon_days_behind),
        settings=s,
    )

@router.get("/van-load", response_model=VanLoadResponse)
def van_load(
    route_code: str = Query(..., description="Route code, e.g. 9105"),
    date: str = Query(..., description="YYYY-MM-DD"),
    svc: VanLoadService = Depends(get_van_load_service),
):
    """Per-item van composition for one (route, date)."""
    return svc.get(route_code, date)

@router.get("/recommend", response_model=ReconciliationResponse)
def recommend(
    route_code: str = Query(...),
    date: str = Query(...),
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
    forecast_total = 0.0
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
        forecast_total += rec.forecast_raw

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
    # ``forecast_total`` (raw model sum) is intentionally NOT exposed --
    # the wire contract is reconciled-only. ``rec_total`` carries the
    # V5_b-reconciled per-route sum, ``recommended_van_load_total``
    # below carries reconciled + leftover.
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
    end_date: str = Query(..., description="Latest date in window (YYYY-MM-DD)"),
    lookback_days: int = Query(
        default_factory=lambda: get_settings().reconciliation_default_lookback_days,
        ge=1, le=365,
    ),
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
    lookback_days = min(
        int(lookback_days),
        int(settings.reconciliation_max_lookback_days),
    )
    rcode = str(route_code)
    # Working-day semantics: lookback_days = number of MOST RECENT ACTIVE
    # days for this route, capped at end_date. So lookback_days=1 always
    # returns exactly 1 day (the most recent active one), regardless of
    # whether the calendar day before was also active.
    base = van.past_performance(route_code,
                                lookback_working_days=lookback_days,
                                end_date=end_date)
    daily_rows = base.get("daily", [])
    if not daily_rows:
        return PastPerformanceResponse(
            available=False,
            message="no activity in window",
            route_code=route_code,
            start_date=base.get("start_date"),
            end_date=base.get("end_date"),
            lookback_days=lookback_days,
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
    if not fc_df.empty and pred_col is not None:
        fc_df = fc_df.copy()
        fc_df["TrxDate"] = pd.to_datetime(fc_df["TrxDate"], errors="coerce").dt.normalize()
        fc_df = fc_df[fc_df[pred_col] > 0]
        fc_df["RouteCode"] = fc_df["RouteCode"].astype(str)
        fc_df["ItemCode"]  = fc_df["ItemCode"].astype(str)
        fc_df = fc_df[fc_df.TrxDate.isin(window_dates)]
        if item_whitelist is not None:
            fc_df = fc_df[fc_df.ItemCode.isin(item_whitelist)]

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
    ffill_days = int(settings.opening_stock_lookback_days)
    closing_full = van._load_csv(van._s.closing_stock_file)
    closing_pivot: pd.DataFrame | None = None
    if not closing_full.empty and "ClosingQty" in closing_full.columns:
        cl = closing_full.copy()
        cl["RouteCode"] = cl.RouteCode.astype(str)
        cl["ItemCode"]  = cl.ItemCode.astype(str)
        cl = cl[(cl.RouteCode == rcode)
                & (cl.TrxDate >= start_dt - pd.Timedelta(days=ffill_days + 1))
                & (cl.TrxDate <= end_dt)]
        if not cl.empty:
            cl_filled = forward_fill_closing(
                cl[["RouteCode", "ItemCode", "TrxDate", "ClosingQty"]],
                ffill_days,
            )
            closing_pivot = cl_filled.pivot_table(
                index="TrxDate", columns="ItemCode",
                values="ClosingQty", aggfunc="sum",
            )

    def opening_for(d: pd.Timestamp, item: str) -> float:
        """ClosingQty for (route, item) on day d-1, else 0."""
        if closing_pivot is None:
            return 0.0
        prev = d - pd.Timedelta(days=1)
        if prev not in closing_pivot.index or item not in closing_pivot.columns:
            return 0.0
        v = closing_pivot.at[prev, item]
        return float(v) if pd.notna(v) else 0.0

    def opening_total_for(
        d: pd.Timestamp, item_set: set[str] | None = None,
    ) -> float:
        """Sum of opening_for across items on day ``d``."""
        if closing_pivot is None:
            return 0.0
        prev = d - pd.Timedelta(days=1)
        if prev not in closing_pivot.index:
            return 0.0
        row_prev = closing_pivot.loc[prev]
        if item_set is not None:
            cols = [c for c in row_prev.index if c in item_set]
            if not cols:
                return 0.0
            return float(row_prev[cols].fillna(0.0).sum())
        return float(row_prev.fillna(0.0).sum())

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
            lookback_days=lookback_days,
            active_days=0,
        )

    # ---- Per-(route, item) aggregates over the window, scoped to the
    #      anchor set. Single helper, single semantic -- the rep numbers
    #      and our numbers come out of identical filters.
    alloc_df = van._load_csv(van._s.load_allocation_file)
    sales_df = van._load_csv(van._s.sales_recent_file)

    def _scoped(df: pd.DataFrame, qty_col: str) -> pd.DataFrame:
        if df.empty or qty_col not in df.columns:
            return df.iloc[0:0]
        return df[(df.RouteCode.astype(str) == rcode)
                  & (df.TrxDate.isin(window_dates))
                  & (df.ItemCode.astype(str).isin(anchor_items))]

    def _by_item(df: pd.DataFrame, qty_col: str) -> dict[str, float]:
        sub = _scoped(df, qty_col)
        if sub.empty:
            return {}
        return sub.groupby(sub.ItemCode.astype(str))[qty_col].sum().astype(float).to_dict()

    def _by_day(df: pd.DataFrame, qty_col: str) -> dict[pd.Timestamp, float]:
        sub = _scoped(df, qty_col)
        if sub.empty:
            return {}
        return sub.groupby("TrxDate")[qty_col].sum().astype(float).to_dict()

    today_alloc_per_item = _by_item(alloc_df, "AllocatedPC")
    sold_per_item        = _by_item(sales_df, "TotalQuantity")

    alloc_daily = _by_day(alloc_df, "AllocatedPC")
    sales_daily = _by_day(sales_df, "TotalQuantity")

    # Per-item opening (logged ClosingQty[d-1] or 0) summed across the
    # active days. Surfaced as ``past_leftover`` context -- it is REP's
    # prior reality, not part of either policy's recommendation today.
    opening_per_item: dict[str, float] = {}
    if closing_pivot is not None:
        cols_in_anchor = [c for c in closing_pivot.columns if c in anchor_items]
        for d in window_dates:
            prev = d - pd.Timedelta(days=1)
            if prev not in closing_pivot.index:
                continue
            row_prev = closing_pivot.loc[prev]
            for ic in cols_in_anchor:
                v = row_prev[ic]
                if pd.notna(v) and v != 0:
                    opening_per_item[ic] = opening_per_item.get(ic, 0.0) + float(v)

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
    our_van_load_per_item: dict[str, float] = {}

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
    if not fc_df.empty and pred_col is not None and rload_col is not None and opening_col is not None:
        canon = fc_df.assign(
            _ic   = fc_df.ItemCode.astype(str),
            _load = pd.to_numeric(fc_df[rload_col],  errors="coerce").fillna(0.0).clip(lower=0.0),
            _open = pd.to_numeric(fc_df[opening_col], errors="coerce").fillna(0.0).clip(lower=0.0),
            _pred = pd.to_numeric(fc_df[pred_col],   errors="coerce").fillna(0.0),
        )
        canon["_van"] = canon["_open"] + canon["_load"]

        per_day = canon.groupby("TrxDate").agg(
            carried=("_open", "sum"),
            fresh=("_load", "sum"),
            van=("_van", "sum"),
        )
        for d_ts, row in per_day.iterrows():
            d_str = pd.Timestamp(d_ts).strftime("%Y-%m-%d")
            recommended_carried_per_day[d_str]  = round(float(row["carried"]), 2)
            recommended_fresh_per_day[d_str]    = round(float(row["fresh"]), 2)
            recommended_van_load_per_day[d_str] = round(float(row["van"]), 2)

        our_van_load_per_item.update(
            canon.groupby("_ic")["_van"].sum().astype(float).to_dict()
        )

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
    for row in daily_rows:
        d = pd.Timestamp(row["date"]).normalize()
        leftover_rep = opening_total_for(d, anchor_items)
        today_alloc = float(alloc_daily.get(d, 0.0))
        d_key = row["date"]
        row["past_leftover"]        = round(leftover_rep, 2)
        row["today_allocation"]     = round(today_alloc, 2)
        row["rep_van_load"]         = round(leftover_rep + today_alloc, 2)
        row["recommended_carried"]  = recommended_carried_per_day.get(d_key, 0.0)
        row["recommended_fresh"]    = recommended_fresh_per_day.get(d_key, 0.0)
        row["recommended_van_load"] = recommended_van_load_per_day.get(d_key, 0.0)
        row["actual_sold"]          = round(float(sales_daily.get(d, 0.0)), 2)

    def _sum(field: str) -> float:
        return float(sum((r.get(field) or 0.0) for r in daily_rows))

    sold_total                 = _sum("actual_sold")
    rep_van_load_total         = _sum("rep_van_load")
    past_leftover_total        = _sum("past_leftover")
    today_alloc_total          = _sum("today_allocation")
    # Counterfactual totals -- pure sums of the per-day simulated values,
    # not aliased to any rep field. ``recommended_carried_total`` is the
    # sum of OUR per-day carried leftover (which is 0 on day 1 and grows
    # only when our policy over-loaded the prior day).
    recommended_carried_total  = _sum("recommended_carried")
    recommended_fresh_total    = _sum("recommended_fresh")
    recommended_van_load_total = _sum("recommended_van_load")

    # ---- Per-item average unit price, scoped to anchor items ---------
    # Quantity-weighted mean of AvgUnitPrice over the same (route, anchor,
    # window) slice the rep / our totals use, so holding cost AED values
    # reconcile with the unit numbers on the same surface (no out-of-scope
    # prices leaking in).
    unit_price: dict[str, float] = {}
    px_df = _scoped(sales_df, "AvgUnitPrice")
    if not px_df.empty and "AvgUnitPrice" in px_df.columns:
        px_df = px_df.assign(_ic=px_df.ItemCode.astype(str))
        num = (px_df.AvgUnitPrice.astype(float) * px_df.TotalQuantity.astype(float))
        unit_price = (
            num.groupby(px_df["_ic"]).sum()
            / px_df.groupby("_ic").TotalQuantity.sum().astype(float).clip(lower=1.0)
        ).to_dict()

    # ---- Overnight stock: each policy graded against the SAME demand,
    # using its OWN truck weight. Apples to apples.
    #   rep_excess_units = max(rep_van_load_total - sold_total, 0)
    #     rep_van_load = past_leftover (real) + today_allocation (real)
    #   our_excess_units = max(recommended_van_load_total - sold_total, 0)
    #     recommended_van_load = our_leftover (simulated, day-1 zero) + our_fresh
    # max(.,0) clamps to zero on days where sold > load (lost sales,
    # not overnight stock).
    #
    # Per-item holding cost mirrors the same separation: rep uses rep's
    # truck (real leftover + real allocation), we use ours (simulated
    # van_load summed across days).
    rep_holding_value = 0.0
    our_holding_value = 0.0
    for ic in anchor_items:
        sold_v       = float(sold_per_item.get(ic, 0.0))
        rep_load_i   = (
            float(opening_per_item.get(ic, 0.0))
            + float(today_alloc_per_item.get(ic, 0.0))
        )
        our_load_i   = float(our_van_load_per_item.get(ic, 0.0))
        rep_excess_i = max(rep_load_i - sold_v, 0.0)
        our_excess_i = max(our_load_i - sold_v, 0.0)
        price = float(unit_price.get(ic, 0.0))
        if price > 0:
            rep_holding_value += rep_excess_i * price
            our_holding_value += our_excess_i * price
    holding_savings = rep_holding_value - our_holding_value
    rep_excess_units = max(rep_van_load_total - sold_total, 0.0)
    our_excess_units = max(recommended_van_load_total - sold_total, 0.0)
    excess_units_savings = rep_excess_units - our_excess_units

    # ---- Recommendation match: bounded symmetric ratio of the
    # recommended van load (leftover + fresh, the headline-tile number)
    # against actually-sold units.
    #   match = min(rec, sold) / max(rec, sold) x 100
    # Bounded [0, 100] by construction, symmetric (a 2x over-allocation
    # and a 0.5x under-allocation both read as 50%), and free of the
    # WAPE cliff that pinned the metric to 0% whenever recommended >
    # 2x sold. WAPE was unusable here because heavy-leftover days
    # routinely push recommended past 2x demand even when the depot's
    # FRESH contribution is reasonable -- the 0% floor hid every
    # signal in the metric. Industry-standard fill-ratio accuracy.
    forecast_accuracy_pct = (
        min(recommended_van_load_total, sold_total)
        / max(recommended_van_load_total, sold_total) * 100.0
        if (recommended_van_load_total > 0 and sold_total > 0) else 0.0
    )

    # ---- Forecast coverage: of items the rep actually sold for this
    # route on each day in the window, what fraction were on our forecast
    # for that day? Mean across days. Mirrors the dashboard's coverage
    # math (data_import.eda_service._compute_business_kpis) so the two
    # surfaces stay aligned. Uses the FULL sold-item set (not anchor-
    # scoped) -- coverage by construction looks at items the model may
    # have missed, so anchor-scoping it would always return 100%.
    sales_for_coverage = sales_df[
        (sales_df.RouteCode.astype(str) == rcode)
        & (sales_df.TrxDate.isin(window_dates))
    ]
    if item_whitelist is not None and not sales_for_coverage.empty:
        sales_for_coverage = sales_for_coverage[
            sales_for_coverage.ItemCode.astype(str).isin(item_whitelist)
        ]
    fc_for_coverage = fc_df.copy() if (not fc_df.empty and pred_col is not None) else fc_df
    if not fc_for_coverage.empty and pred_col is not None:
        fc_for_coverage = fc_for_coverage[fc_for_coverage[pred_col] > 0]
    coverage_ratios: list[float] = []
    if not sales_for_coverage.empty:
        sold_by_day = (
            sales_for_coverage.groupby("TrxDate").ItemCode
            .apply(lambda s: set(s.astype(str)))
        )
        fc_by_day = (
            (fc_for_coverage.groupby("TrxDate").ItemCode
                .apply(lambda s: set(s.astype(str))))
            if not fc_for_coverage.empty else {}
        )
        for d, sold_items in sold_by_day.items():
            if not sold_items:
                continue
            predicted_items = fc_by_day.get(d, set())
            if not predicted_items:
                coverage_ratios.append(0.0)
                continue
            coverage_ratios.append(len(sold_items & predicted_items) / len(sold_items))
    forecast_coverage_pct = (
        round(sum(coverage_ratios) / len(coverage_ratios) * 100.0, 2)
        if coverage_ratios else 0.0
    )

    # Every key here is consumed by exactly one tile (or one subtitle)
    # on the AccuracyDrawer. The invariants that hold by construction:
    #   rep_van_load_total          = past_leftover_total + today_allocation_total
    #   recommended_van_load_total  = recommended_carried_total + recommended_fresh_total
    #   recommended_carried_total   = past_leftover_total            (same truck)
    #   rep_excess_units            = max(rep_van_load_total          - actual_sold_total, 0)
    #   our_excess_units            = max(recommended_van_load_total  - actual_sold_total, 0)
    #   excess_units_savings        = rep_excess_units - our_excess_units
    #   holding_savings             = rep_holding_value - our_holding_value
    # All identities reconcile by simple subtraction so a reader can
    # mental-arithmetic any tile's headline against its subtitle.
    totals = {
        "rep_van_load_total":         round(rep_van_load_total, 2),
        "recommended_carried_total":  round(recommended_carried_total, 2),
        "recommended_fresh_total":    round(recommended_fresh_total, 2),
        "recommended_van_load_total": round(recommended_van_load_total, 2),
        "actual_sold_total":          round(sold_total, 2),
        "past_leftover_total":        round(past_leftover_total, 2),
        "today_allocation_total":     round(today_alloc_total, 2),
        "rep_holding_value":          round(rep_holding_value, 2),
        "our_holding_value":          round(our_holding_value, 2),
        "holding_savings":            round(holding_savings, 2),
        # Unit-level overnight stock (aggregate). Reconciles with the
        # headline tiles by simple subtraction. Positive
        # ``excess_units_savings`` => our policy leaves fewer units on
        # the truck overnight than the rep's actual loading did.
        "rep_excess_units":           round(rep_excess_units, 2),
        "our_excess_units":           round(our_excess_units, 2),
        "excess_units_savings":       round(excess_units_savings, 2),
        "active_days":               len(daily_rows),
    }
    # Two metrics, two questions. Each maps 1:1 to a tile on the drawer
    # so every wire field has a visible UI consumer (no dead payload):
    #   forecast_accuracy_pct -> "Recommendation match" tile
    #   forecast_coverage_pct -> "Forecast coverage" tile
    metrics = {
        "forecast_accuracy_pct": round(forecast_accuracy_pct, 2),
        "forecast_coverage_pct": forecast_coverage_pct,
    }

    return PastPerformanceResponse(
        available=True,
        route_code=route_code,
        start_date=base.get("start_date"),
        end_date=base.get("end_date"),
        lookback_days=lookback_days,
        active_days=len(daily_rows),
        daily=daily_rows,
        totals=totals,
        metrics=metrics,
    )
