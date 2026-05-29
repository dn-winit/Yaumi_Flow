"""Reconciliation endpoints -- van composition + V5_b load + past-perf.

Four GET/POST endpoints, all dynamic and parameterised:

* ``/reconciliation/van-load``        composition only for one (route, date)
* ``/reconciliation/recommend``       V5_b recommendation joined with composition
* ``/reconciliation/past-performance`` per-day chart series + return metrics
* ``/reconciliation/refresh``          manual trigger for the daily refresh cron
"""
from __future__ import annotations

import logging
from typing import Any

import pandas as pd
from fastapi import APIRouter, Depends, Query

from common.numeric import safe_float
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

# Shared ISO-date regex; matches data_import's /eda/* for uniform 422 across services.
_DATE_RE = r"^\d{4}-\d{2}-\d{2}$"


@router.post("/refresh")
def manual_refresh(
    horizon_days_behind: int = Query(
        0, ge=0, le=365,
        description="Days back from today; 0 = today only.",
    ),
    force: bool = Query(
        True,
        description="False short-circuits if a recent refresh ran (same dedup window as cron).",
    ),
) -> dict[str, Any]:
    """Run reconciliation refresh; same path as the daily cron.

    Writes to yf_sales_transactions for past + today; future dates out of scope.
    """
    return refresh_reconciliation(
        horizon_days_behind=int(horizon_days_behind),
        settings=get_settings(),
        force=bool(force),
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
    """V5_b load recommendation for (route, date), joined with actual van composition."""
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
        # iterrows (not itertuples): the ``class`` column is a keyword and gets renamed by itertuples.
        fcst_lookup = {
            str(row["ItemCode"]): {
                "predicted": safe_float(row[pred_col]),
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
    # Recommended-side carried+fresh breakdown; single pass keeps totals consistent.
    rec_carried_qty = 0.0
    rec_carried_items = 0
    rec_issue_items = 0
    rec_van_load_items = 0
    # Actual-side per-component item counts (van_load_service emits items_count only).
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
            # use_carry_floor=False to match enrich.py:324 and past_performance backtest.
            use_carry_floor=False,
        )
        merged = rec.to_dict()
        # Reconciled-only wire: drop forecast_raw (raw model output stays internal).
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

        # Per-row recommended composition; engine already returned the canonical fields.
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
    # Only V5_b-reconciled total leaves; recommended_van_load_total = reconciled + leftover.
    totals["recommended_load_total"] = round(rec_total, 2)
    # Recommended-side totals; same shape as actual composition for shared UI component.
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
    """Single canonical source for the AccuracyDrawer; plots two independent worlds.

      rep_van_load[d]         = past_leftover[d] + today_allocation[d]   (rep's truck reality)
      recommended_van_load[d] = our_leftover[d] + our_fresh[d]           (our policy from day 1)
      actual_sold[d]                                                     (invoiced ground truth)

    Anchor scope: only (route, item) pairs with Predicted > 0 enter either side.
    """
    settings = get_settings()
    # Reject ranges wider than the cap; regex admits invalid dates so wrap pd.Timestamp.
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
    # pack_qty: single rounding policy across every quantity cell below.
    from common.numeric import pack_qty as _pq
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

    # Resolve filters into one ItemCode whitelist (categories narrow it further).
    item_whitelist: set[str] | None = (
        {str(x).strip() for x in item_codes if str(x).strip()} if item_codes else None
    )
    if category_codes:
        cat_set = {str(x).strip() for x in category_codes if str(x).strip()}
        # Category -> item via catalog; columns pre-cast to str in VanLoadService._load_csv.
        catalog_df = van._load_csv(van._s.sales_recent_file)
        if not catalog_df.empty and "CategoryName" in catalog_df.columns:
            cat_items = set(
                catalog_df[catalog_df.CategoryName.isin(cat_set)]
                .ItemCode.unique()
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
    # fc_df_full keeps the day-after-window row so carry-out can read opening_stock[d+1]
    # (same persisted column the Van Load tile reads -- canonical "what carries from d into d+1").
    fc_df_full = fc_df
    if not fc_df.empty and pred_col is not None:
        fc_df = fc_df.copy()
        fc_df["TrxDate"] = pd.to_datetime(fc_df["TrxDate"], errors="coerce").dt.normalize()
        # Shared activity predicate (ArtifactService.van_load_view uses the same mask).
        from demand_forecasting_pipeline.services.reconciliation.enrich import (
            activity_mask,
        )
        fc_df = fc_df[activity_mask(fc_df)]
        fc_df["RouteCode"] = fc_df["RouteCode"].astype(str)
        fc_df["ItemCode"]  = fc_df["ItemCode"].astype(str)
        # fc_df_full = route-scoped carry-aware view for next-day opening; fc_df is window-only.
        fc_df_full = fc_df.copy()
        fc_df = fc_df[fc_df.TrxDate.isin(window_dates)]
        if item_whitelist is not None:
            fc_df = fc_df[fc_df.ItemCode.isin(item_whitelist)]
            fc_df_full = fc_df_full[fc_df_full.ItemCode.isin(item_whitelist)]

    # Closing index convention: opening[d, item] = ClosingQty[(route, item, d-1)] or 0.
    # ClosingQty=0 is never logged, so a missing row means "rep had nothing left".
    min(window_dates) if window_dates else pd.Timestamp(end_date).normalize()
    max(window_dates) if window_dates else pd.Timestamp(end_date).normalize()
    # Anchor items: (route, item) pairs with Predicted > 0 in window.
    # Only scope compared so we never claim credit on items we didn't predict.
    anchor_items: set[str] = (
        set(fc_df.ItemCode.unique())
        if (not fc_df.empty and pred_col is not None) else set()
    )

    # No forecasted items -> nothing to compare; bail before rep CSV reads.
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

    # Item-catalog source for price + coverage; rep/sold flow through fc_df (single source).
    sales_df = van._load_csv(van._s.sales_recent_file)

    # Counterfactual forward simulation across two independent worlds (rep vs our policy).
    # Field names recommended_carried / recommended_fresh / recommended_van_load kept stable for the chart.
    recommended_fresh_per_day: dict[str, float] = {}
    recommended_carried_per_day: dict[str, float] = {}
    recommended_van_load_per_day: dict[str, float] = {}
    # Per-(item, day) reconciled values; same cells per-(item, date) emission reads.
    rec_carried_by_item_day: dict[tuple[str, str], float] = {}
    rec_fresh_by_item_day:   dict[tuple[str, str], float] = {}
    rec_van_by_item_day:     dict[tuple[str, str], float] = {}

    # Aggregate canonical cron-written values from yf_demand_forecast; no parallel engine call.
    rload_col = next(
        (c for c in ("recommended_load", "RecommendedLoad") if c in fc_df.columns),
        None,
    )
    opening_col = next(
        (c for c in ("opening_stock", "OpeningStock") if c in fc_df.columns),
        None,
    )
    # Rep-side persisted yaumi_* columns from fc_df (= yf_sales_transactions);
    # byte-matches yaumi_total_van_load (one path, no parallel CSV recompute).
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
        # ItemCode already string-typed above.
        canon = fc_df.assign(
            _ic   = fc_df.ItemCode,
            _load = pd.to_numeric(fc_df[rload_col],  errors="coerce").fillna(0.0).clip(lower=0.0),
            _open = pd.to_numeric(fc_df[opening_col], errors="coerce").fillna(0.0).clip(lower=0.0),
            # yaumi_* written by reconciliation_refresh; clip(0) mirrors ingestion guard.
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

        # Per-(item, day) breakdown via numpy arrays + zip; iterrows was the biggest CPU sink.
        _ic_arr    = canon["_ic"].to_numpy()
        # fillna("NaT") keeps equivalence with prior pd.Timestamp.strftime under future reshuffles.
        _dates_str = (
            pd.to_datetime(canon["TrxDate"], errors="coerce")
            .dt.strftime("%Y-%m-%d")
            .fillna("NaT")
            .to_numpy()
        )
        _open_arr   = canon["_open"].to_numpy(dtype=float)
        _load_arr   = canon["_load"].to_numpy(dtype=float)
        _van_arr    = canon["_van"].to_numpy(dtype=float)
        _ryopen_arr = canon["_ryopen"].to_numpy(dtype=float)
        _ryfresh_arr = canon["_ryfresh"].to_numpy(dtype=float)
        _ryleft_arr = canon["_ryleft"].to_numpy(dtype=float)
        _ryvan_arr  = canon["_ryvan"].to_numpy(dtype=float)
        _sold_arr   = canon["_sold"].to_numpy(dtype=float)
        for ic, d_str, op, ld, vn, ryop, ryfr, rylo, ryvn, sld in zip(
            _ic_arr, _dates_str,
            _open_arr, _load_arr, _van_arr,
            _ryopen_arr, _ryfresh_arr, _ryleft_arr, _ryvan_arr,
            _sold_arr, strict=False,
        ):
            key = (str(ic), str(d_str))
            rec_carried_by_item_day[key]  = float(op)
            rec_fresh_by_item_day[key]    = float(ld)
            rec_van_by_item_day[key]      = float(vn)
            rep_open_by_item_day[key]     = float(ryop)
            rep_fresh_by_item_day[key]    = float(ryfr)
            rep_leftover_by_item_day[key] = float(rylo)
            rep_van_by_item_day[key]      = float(ryvn)
            sold_by_item_day_fc[key]      = float(sld)

    # Seed daily_rows with anchor-only aggregates; the widening pass below recomputes
    # over the full disjoint-set items_payload.
    for row in daily_rows:
        d_key = row["date"]
        row["rep_van_load"]         = rep_van_load_per_day.get(d_key, 0.0)
        row["recommended_van_load"] = recommended_van_load_per_day.get(d_key, 0.0)
        row["actual_sold"]          = sold_per_day_fc.get(d_key, 0.0)

    # Per-item lookups for items[] payload: item-name (forecast frame, sales fallback) +
    # next-day opening (fc_df_full.opening_stock on date > last_window_day -- cron-written cell).
    item_name_lookup: dict[str, str] = {}
    item_category_lookup: dict[str, str] = {}

    def _ingest_lookup(
        df: pd.DataFrame, col: str, target: dict[str, str]
    ) -> None:
        if df.empty or col not in df.columns:
            return
        pairs = (
            df.assign(_ic=df.ItemCode)
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

    # Forecast frame first (canonical), sales fallback for NaN ItemName windows.
    if not fc_df.empty and pred_col is not None:
        for nc in ("ItemName", "item_name"):
            if nc in fc_df.columns:
                _ingest_lookup(fc_df, nc, item_name_lookup)
                break
    if not sales_df.empty:
        # Route-scoped (NOT anchor-scoped) so unforecasted rep-loaded items also get categoryName.
        sales_scope = sales_df[sales_df.RouteCode == rcode]
        for nc in ("ItemName", "item_name"):
            if nc in sales_scope.columns:
                _ingest_lookup(sales_scope, nc, item_name_lookup)
                break
        for cc in ("CategoryName", "category_name"):
            if cc in sales_scope.columns:
                _ingest_lookup(sales_scope, cc, item_category_lookup)
                break
    # Per-(item, date) row emission; one row per (anchor_item, active_day).
    # Identity: sum across rows == matching totals fields by construction.
    sorted_active_days = sorted(window_dates)

    # Canonical leftover lookups from yf_sales_transactions for Past Performance / Van Load parity.
    rec_leftover_by_item_day: dict[tuple[str, str], float] = {}
    next_day_opening_by_item_date: dict[tuple[str, str], float] = {}
    if not fc_df_full.empty:
        op_col = next((c for c in ("opening_stock", "OpeningStock") if c in fc_df_full.columns), None)
        lo_col = next((c for c in ("leftover_to_next_day", "LeftoverToNextDay") if c in fc_df_full.columns), None)
        if op_col is not None or lo_col is not None:
            ic_arr = fc_df_full.ItemCode.tolist()
            d_arr  = [d.strftime("%Y-%m-%d") for d in fc_df_full.TrxDate]
            op_arr = (
                pd.to_numeric(fc_df_full[op_col], errors="coerce").fillna(0.0).clip(lower=0.0).tolist()
                if op_col is not None else [None] * len(ic_arr)
            )
            lo_arr = (
                pd.to_numeric(fc_df_full[lo_col], errors="coerce").fillna(0.0).clip(lower=0.0).tolist()
                if lo_col is not None else [None] * len(ic_arr)
            )
            for ic, d_str2, op, lo in zip(ic_arr, d_arr, op_arr, lo_arr, strict=False):
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
            # Rep + sold from persisted yaumi_*/actual_sold in fc_df (cron output).
            past_left     = rep_open_by_item_day.get(key, 0.0)
            today_alloc_i = rep_fresh_by_item_day.get(key, 0.0)
            rep_van_i     = rep_van_by_item_day.get(key, past_left + today_alloc_i)
            rec_carry     = float(rec_carried_by_item_day.get(key, 0.0))
            rec_fresh     = float(rec_fresh_by_item_day.get(key, 0.0))
            rec_van       = float(rec_van_by_item_day.get(key, rec_carry + rec_fresh))
            sold_i        = sold_by_item_day_fc.get(key, 0.0)
            # actual_lo = rep's yaumi_leftover; rec_lo = opening_stock[d+1] (canonical carry-out).
            actual_lo     = rep_leftover_by_item_day.get(key, max(0.0, rep_van_i - sold_i))
            rec_lo        = _recommended_leftover(ic_str, d_str, rec_van, sold_i)

            # Skip rows empty across every field (no activity at all).
            if (
                rep_van_i == 0.0
                and rec_van == 0.0
                and sold_i == 0.0
                and actual_lo == 0.0
                and rec_lo == 0.0
            ):
                continue
            # All quantities through pack_qty (ceil-int) for one consistent rounding policy.
            items_payload.append(
                PastPerformanceItem(
                    itemCode=ic_str,
                    itemName=item_name_lookup.get(ic_str, ""),
                    categoryName=(item_category_lookup.get(ic_str, "") or "").strip() or "Uncategorised",
                    date=d_str,
                    rep_van_load=float(_pq(rep_van_i)),
                    recommended_van_load=float(_pq(rec_van)),
                    actual_sold=float(_pq(sold_i)),
                    actual_leftover=float(_pq(actual_lo)),
                    recommended_leftover=float(_pq(rec_lo)),
                )
            )

    # Disjoint-set widening: add unforecasted-but-active items from sales_recent.csv per route.
    # rep_van <- closing[d-1] + alloc[d]; actual_sold <- sales groupby; recommended_van = 0.
    # Sets are disjoint so each row has exactly one source.
    non_anchor_items: set[str] = set()
    if not sales_scope.empty and "ItemCode" in sales_scope.columns:
        all_route_items = set(sales_scope.ItemCode.unique())
        non_anchor_items = all_route_items - anchor_items
        if item_whitelist is not None:
            non_anchor_items &= item_whitelist

    if non_anchor_items:
        closing_csv = van._load_csv(van._s.closing_stock_file)
        alloc_csv   = van._load_csv(van._s.load_allocation_file)

        # Per-(item, date) lookups from upstream CSVs; closing read covers window - 1 day.
        first_active  = sorted_active_days[0]
        last_active   = sorted_active_days[-1]
        closing_start = first_active - pd.Timedelta(days=1)

        def _route_window(df: pd.DataFrame, start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
            if df.empty or "TrxDate" not in df.columns:
                return df.iloc[0:0]
            return df[(df.RouteCode == rcode)
                      & (df.TrxDate >= start) & (df.TrxDate <= end)
                      & df.ItemCode.isin(non_anchor_items)]

        # ClosingQty[d-1] = day d's opening (carry).
        closing_window = _route_window(closing_csv, closing_start, last_active)
        prior_close: dict[tuple[str, str], float] = {}
        same_day_close: dict[tuple[str, str], float] = {}
        if not closing_window.empty and "ClosingQty" in closing_window.columns:
            tmp = closing_window.assign(
                _ic = closing_window.ItemCode,
                _qty = pd.to_numeric(closing_window.ClosingQty, errors="coerce").fillna(0.0).clip(lower=0.0),
            )
            for ic, d, qty in zip(tmp._ic, tmp.TrxDate, tmp._qty, strict=False):
                same_day_close[(ic, d.strftime("%Y-%m-%d"))] = float(qty)
            # Map row date forward by one day so ClosingQty[d-1] becomes opening[d].
            for ic, d, qty in zip(tmp._ic, tmp.TrxDate, tmp._qty, strict=False):
                nxt = (d + pd.Timedelta(days=1)).strftime("%Y-%m-%d")
                prior_close[(ic, nxt)] = float(qty)

        # AllocatedPC[d] -- today's depot-issued fresh load.
        alloc_window = _route_window(alloc_csv, first_active, last_active)
        alloc_lookup: dict[tuple[str, str], float] = {}
        if not alloc_window.empty and "AllocatedPC" in alloc_window.columns:
            tmp = alloc_window.assign(
                _ic = alloc_window.ItemCode,
                _qty = pd.to_numeric(alloc_window.AllocatedPC, errors="coerce").fillna(0.0).clip(lower=0.0),
            )
            for ic, d, qty in zip(tmp._ic, tmp.TrxDate, tmp._qty, strict=False):
                alloc_lookup[(ic, d.strftime("%Y-%m-%d"))] = float(qty)

        # TotalQuantity from sales_recent.csv (route-scoped, window-scoped).
        sales_window = sales_scope[
            sales_scope.TrxDate.isin(window_dates)
            & sales_scope.ItemCode.isin(non_anchor_items)
        ] if "TrxDate" in sales_scope.columns else sales_scope.iloc[0:0]
        sold_lookup: dict[tuple[str, str], float] = {}
        if not sales_window.empty and "TotalQuantity" in sales_window.columns:
            tmp = sales_window.assign(
                _ic = sales_window.ItemCode,
                _qty = pd.to_numeric(sales_window.TotalQuantity, errors="coerce").fillna(0.0).clip(lower=0.0),
            )
            grp = tmp.groupby(["_ic", "TrxDate"])._qty.sum()
            for (ic, d), qty in grp.items():
                sold_lookup[(ic, d.strftime("%Y-%m-%d"))] = float(qty)

        for d_ts in sorted_active_days:
            d_str = d_ts.strftime("%Y-%m-%d")
            for ic_str in non_anchor_items:
                key = (ic_str, d_str)
                past_left     = prior_close.get(key, 0.0)
                today_alloc_i = alloc_lookup.get(key, 0.0)
                rep_van_i     = past_left + today_alloc_i
                sold_i        = sold_lookup.get(key, 0.0)
                actual_lo     = max(0.0, rep_van_i - sold_i)
                # Skip empty rows (avoid catalog ghosts).
                if rep_van_i == 0.0 and sold_i == 0.0 and actual_lo == 0.0:
                    continue
                # Same pack_qty policy as anchor-set rows.
                items_payload.append(
                    PastPerformanceItem(
                        itemCode=ic_str,
                        itemName=item_name_lookup.get(ic_str, ""),
                        categoryName=(item_category_lookup.get(ic_str, "") or "").strip() or "Uncategorised",
                        date=d_str,
                        rep_van_load=float(_pq(rep_van_i)),
                        recommended_van_load=0.0,
                        actual_sold=float(_pq(sold_i)),
                        actual_leftover=float(_pq(actual_lo)),
                        recommended_leftover=0.0,
                    )
                )

    # Sort: date asc, recommended_leftover desc, recommended_van_load desc, itemCode asc.
    items_payload.sort(
        key=lambda it: (
            it.date,
            -it.recommended_leftover,
            -it.recommended_van_load,
            it.itemCode,
        )
    )

    # Recompute daily aggregates + totals from items_payload (single source; identity by construction).
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
        # Per-day sums of pack_qty cells; tile/table/chart share these so identities hold.
        row["rep_van_load"]         = float(rep_per_day.get(d_key, 0.0))
        row["recommended_van_load"] = float(rec_per_day.get(d_key, 0.0))
        row["actual_sold"]          = float(sold_per_day.get(d_key, 0.0))
        row["actual_leftover"]      = float(rep_lo_per_day.get(d_key, 0.0))
        row["recommended_leftover"] = float(rec_lo_per_day.get(d_key, 0.0))
    rep_van_load_total         = sum(it.rep_van_load          for it in items_payload)
    recommended_van_load_total = sum(it.recommended_van_load  for it in items_payload)
    sold_total                 = sum(it.actual_sold           for it in items_payload)

    # Category rollup: one row per categoryName from items_payload (totals reconcile by construction).
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
    # Category values are integer sums; float(...) only sets wire type.
    categories_payload: list[PastPerformanceCategoryRow] = [
        PastPerformanceCategoryRow(
            categoryName=b["categoryName"],
            skus=len(b["skus"]),
            rep_van_load=float(b["rep_van_load"]),
            recommended_van_load=float(b["recommended_van_load"]),
            actual_sold=float(b["actual_sold"]),
            actual_leftover=float(b["actual_leftover"]),
            recommended_leftover=float(b["recommended_leftover"]),
        )
        for b in cat_accum.values()
    ]
    # Sort: recommended_van_load desc, categoryName asc.
    categories_payload.sort(
        key=lambda c: (-c.recommended_van_load, c.categoryName)
    )

    # Served units: integer pack_qty cells; min/sum of ints stays int.
    served_units = float(sum(
        min(float(it.actual_sold or 0.0), float(it.recommended_van_load or 0.0))
        for it in items_payload
    ))

    # Leftovers comparison: integer per-row sums; pct via common.numeric.pct.
    rep_leftover_units = float(sum(it.actual_leftover       for it in items_payload))
    our_leftover_units = float(sum(it.recommended_leftover  for it in items_payload))
    leftover_units_saved = rep_leftover_units - our_leftover_units
    leftover_pct_saved = (
        int(round(leftover_units_saved / rep_leftover_units * 100.0))
        if rep_leftover_units > 0 else 0
    )

    # SKU coverage: of SKUs rep sold, how many did our recommendation cover?
    # Roll up to SKU level first to avoid double-counting one-day blips on slow SKUs.
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
        int(round(skus_covered_count / skus_sold_count * 100.0))
        if skus_sold_count > 0 else 0
    )

    # Integer sums; identity sum(items[*].field) == totals[*] from shared pack_qty cells.
    totals = {
        "rep_van_load_total":         float(rep_van_load_total),
        "recommended_van_load_total": float(recommended_van_load_total),
        "actual_sold_total":          float(sold_total),
        "served_units":               served_units,
        "active_days":                len(daily_rows),
        "rep_leftover_units":         rep_leftover_units,
        "our_leftover_units":         our_leftover_units,
        "leftover_units_saved":       leftover_units_saved,
        "leftover_pct_saved":         leftover_pct_saved,
        "skus_sold":                  skus_sold_count,
        "skus_covered":               skus_covered_count,
        "skus_coverage_pct":          skus_coverage_pct,
    }

    # Disjoint-set integrity check: anchor item leaking into widening would double-count headline tiles.
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
