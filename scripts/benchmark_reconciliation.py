"""Benchmark: carry-floor ON + per-item loop  vs  floor OFF + vectorised batch.

Scope is intentionally the V5_b base kernel only -- it does NOT exercise
the V2 layers (L1b pair-maturity shrinkage, L4 quantile loading) because
those are kwargs with safe defaults in ``recommend_one`` / ``recommend_batch``
and would skew the head-to-head comparison this script is built around.

For the production V5_b vs V2 comparison (with L1b + L4 active), see
``scripts/compare_v5b_vs_v2.py`` -- that's the canonical deployment
benchmark and the one to run when validating reconciliation changes.

Run from project root: ``python scripts/benchmark_reconciliation.py``.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
load_dotenv()

from demand_forecasting_pipeline.api.dependencies import (
    get_artifact_service,
    get_bias_service,
    get_van_load_service,
)
from demand_forecasting_pipeline.config.settings import get_settings as df_settings
from demand_forecasting_pipeline.services.reconciliation.engine import ReconciliationEngine

END_DATE = "2026-05-03"
LOOKBACK = 7
ROUTES = ("9105", "9108", "9114", "9115", "9126", "9142",
          "9202", "9204", "9209", "9218", "9219", "9221")

bias = get_bias_service()
artifact = get_artifact_service()
van = get_van_load_service()
engine = ReconciliationEngine(df_settings())


def benchmark_route(ROUTE: str):
    """Build inputs + run both policies on one route. Returns metrics."""
    base = van.past_performance(ROUTE, lookback_working_days=LOOKBACK, end_date=END_DATE)
    if not base.get("daily"):
        return None
    window_dates = {pd.Timestamp(r["date"]).normalize() for r in base["daily"]}
    fc_df_full, _ = artifact.get_future_forecast(route_code=ROUTE, limit=10000)
    if fc_df_full.empty:
        return None
    fc = fc_df_full.copy()
    fc["TrxDate"] = pd.to_datetime(fc["TrxDate"], errors="coerce").dt.normalize()
    pred_col = "prediction" if "prediction" in fc.columns else "Predicted"
    fc = fc[(fc[pred_col] > 0) & (fc.TrxDate.isin(window_dates))]
    if fc.empty:
        return None
    fc["RouteCode"] = fc["RouteCode"].astype(str)
    fc["ItemCode"] = fc["ItemCode"].astype(str)

    closing = van._load_csv(van._s.closing_stock_file)
    cl = closing[(closing.RouteCode.astype(str) == ROUTE)
                 & (closing.TrxDate >= min(window_dates) - pd.Timedelta(days=1))
                 & (closing.TrxDate <= max(window_dates))]
    pivot = (cl.pivot_table(index="TrxDate", columns="ItemCode",
                            values="ClosingQty", aggfunc="sum")
             if not cl.empty else None)

    def opening_for(d, item):
        if pivot is None:
            return 0.0
        prev = d - pd.Timedelta(days=1)
        if prev not in pivot.index or item not in pivot.columns:
            return 0.0
        v = pivot.at[prev, item]
        return float(v) if pd.notna(v) else 0.0

    sales = van._load_csv(van._s.sales_recent_file)
    sales = sales[(sales.RouteCode.astype(str) == ROUTE) & (sales.TrxDate.isin(window_dates))]
    sold_per_item = sales.groupby(sales.ItemCode.astype(str))["TotalQuantity"].sum().to_dict()

    alloc_df = van._load_csv(van._s.load_allocation_file)
    alloc_df = alloc_df[(alloc_df.RouteCode.astype(str) == ROUTE)
                        & (alloc_df.TrxDate.isin(window_dates))]
    rep_fresh_per_item = alloc_df.groupby(alloc_df.ItemCode.astype(str))["AllocatedPC"].sum().to_dict()

    opening_per_item = {}
    if pivot is not None:
        for d in window_dates:
            prev = d - pd.Timedelta(days=1)
            if prev not in pivot.index:
                continue
            for ic in pivot.columns:
                v = pivot.at[prev, ic]
                if pd.notna(v) and v != 0:
                    opening_per_item[ic] = opening_per_item.get(ic, 0.0) + float(v)

    px = sales[sales.AvgUnitPrice.notna()] if "AvgUnitPrice" in sales.columns else sales.iloc[0:0]
    unit_price = {}
    if not px.empty:
        px = px.assign(_ic=px.ItemCode.astype(str))
        num = px.AvgUnitPrice.astype(float) * px.TotalQuantity.astype(float)
        den = px.groupby("_ic").TotalQuantity.sum().clip(lower=1.0)
        unit_price = (num.groupby(px["_ic"]).sum() / den).to_dict()

    def run(use_floor, vectorised):
        fresh = {}
        t0 = time.perf_counter()
        if vectorised:
            for d_str, fc_day in fc.groupby(fc.TrxDate.dt.strftime("%Y-%m-%d")):
                d_ts = pd.Timestamp(d_str).normalize()
                items = fc_day["ItemCode"].astype(str).to_numpy()
                forecasts = fc_day[pred_col].to_numpy(dtype=float)
                biases = np.fromiter(
                    (float(bias.lookup(ROUTE, ic) or 0.0) for ic in items),
                    dtype=float, count=len(items),
                )
                opens = np.fromiter(
                    (opening_for(d_ts, ic) for ic in items),
                    dtype=float, count=len(items),
                )
                loads, *_ = engine.recommend_batch(
                    forecasts=forecasts, bias_pcts=biases, openings=opens,
                    use_carry_floor=use_floor,
                )
                for ic, ld in zip(items, loads):
                    if ld > 0:
                        fresh[ic] = fresh.get(ic, 0.0) + float(ld)
        else:
            for d_str, fc_day in fc.groupby(fc.TrxDate.dt.strftime("%Y-%m-%d")):
                d_ts = pd.Timestamp(d_str).normalize()
                for _, row in fc_day.iterrows():
                    ic = str(row["ItemCode"])
                    pred = float(row[pred_col] or 0.0)
                    if pred <= 0:
                        continue
                    bp = float(bias.lookup(ROUTE, ic) or 0.0)
                    op = opening_for(d_ts, ic)
                    rec = engine.recommend_one(
                        item_code=ic, forecast=pred, bias_pct=bp,
                        opening_stock=op, use_carry_floor=use_floor,
                    )
                    if rec.recommended_load > 0:
                        fresh[ic] = fresh.get(ic, 0.0) + float(rec.recommended_load)
        return fresh, time.perf_counter() - t0

    def score(fresh_per_item):
        rep_eu = our_eu = rep_h = our_h = 0.0
        sold_t = van_t = 0.0
        keys = set(opening_per_item) | set(fresh_per_item) | set(rep_fresh_per_item) | set(sold_per_item)
        for ic in keys:
            sold = float(sold_per_item.get(ic, 0.0))
            leftover = float(opening_per_item.get(ic, 0.0))
            rep_load = leftover + float(rep_fresh_per_item.get(ic, 0.0))
            our_load = leftover + float(fresh_per_item.get(ic, 0.0))
            rep_eu += max(rep_load - sold, 0.0)
            our_eu += max(our_load - sold, 0.0)
            price = float(unit_price.get(ic, 0.0))
            if price > 0:
                rep_h += max(rep_load - sold, 0.0) * price
                our_h += max(our_load - sold, 0.0) * price
            sold_t += sold
            van_t += our_load
        return {
            "our_fresh": sum(fresh_per_item.values()),
            "rec_van": van_t, "our_excess_u": our_eu, "rep_excess_u": rep_eu,
            "our_holding": our_h, "rep_holding": rep_h, "sold": sold_t,
        }

    old_fresh, old_t = run(use_floor=True, vectorised=False)
    new_fresh, new_t = run(use_floor=False, vectorised=True)
    return {
        "old": score(old_fresh), "new": score(new_fresh),
        "old_t": old_t, "new_t": new_t,
    }


print(f"\n=== Fleet sweep | end_date {END_DATE} | {LOOKBACK}-day window ===\n")
print(
    f"{'Route':>6}  {'OLD u_saved':>11} {'NEW u_saved':>11}  "
    f"{'OLD AED':>9} {'NEW AED':>9}  "
    f"{'speedup':>8}"
)
print("-" * 70)

agg = {"old_us": 0, "new_us": 0, "old_aed": 0.0, "new_aed": 0.0,
       "old_t": 0.0, "new_t": 0.0}

for r in ROUTES:
    res = benchmark_route(r)
    if res is None:
        print(f"{r:>6}  {'(no data)':>50}")
        continue
    o, n = res["old"], res["new"]
    old_us = o["rep_excess_u"] - o["our_excess_u"]
    new_us = n["rep_excess_u"] - n["our_excess_u"]
    old_aed = o["rep_holding"] - o["our_holding"]
    new_aed = n["rep_holding"] - n["our_holding"]
    speedup = res["old_t"] / res["new_t"] if res["new_t"] > 0 else 0.0
    print(
        f"{r:>6}  {old_us:>+11.0f} {new_us:>+11.0f}  "
        f"{old_aed:>+9.0f} {new_aed:>+9.0f}  "
        f"{speedup:>7.1f}x"
    )
    agg["old_us"] += old_us; agg["new_us"] += new_us
    agg["old_aed"] += old_aed; agg["new_aed"] += new_aed
    agg["old_t"] += res["old_t"]; agg["new_t"] += res["new_t"]

print("-" * 70)
print(
    f"{'FLEET':>6}  {agg['old_us']:>+11.0f} {agg['new_us']:>+11.0f}  "
    f"{agg['old_aed']:>+9.0f} {agg['new_aed']:>+9.0f}  "
    f"{agg['old_t']/agg['new_t']:>7.1f}x"
)
print()
print(f"Total per-item-loop time: {agg['old_t']*1000:.0f} ms")
print(f"Total vectorised time:    {agg['new_t']*1000:.0f} ms")
print(f"Net wall-clock saved:     {(agg['old_t']-agg['new_t'])*1000:.0f} ms across {len(ROUTES)} routes")
