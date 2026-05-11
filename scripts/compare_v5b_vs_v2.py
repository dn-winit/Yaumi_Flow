"""Real before/after comparison: V5_b reconciliation vs V2 (L1b+L3+L4).

Runs offline against the same CSVs the past-performance handler uses, so
the numbers are directly comparable to what the UI shows after a service
restart picks up the new engine.

Companion script:
  * ``scripts/benchmark_reconciliation.py`` -- microbenchmark of the
    V5_b base kernel only (carry-floor on/off, vectorised vs per-item).
    Run that for engine-level perf numbers; this script is the one to
    run for end-to-end V5_b-vs-V2 deployment validation.

Usage: python scripts/compare_v5b_vs_v2.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
load_dotenv()

from demand_forecasting_pipeline.config.settings import get_settings
from demand_forecasting_pipeline.services.reconciliation.engine import ReconciliationEngine
from demand_forecasting_pipeline.services.reconciliation.bias_service import BiasService

ROUTES = ("9105", "9108", "9114", "9115", "9126", "9142",
          "9202", "9204", "9209", "9218", "9219", "9221")
END = pd.Timestamp("2026-05-04")
LOOKBACKS = (1, 7, 30)

s = get_settings()
data_dir = s.shared_data_dir
fc_full      = pd.read_csv(f"{data_dir}/demand_forecast.csv", low_memory=False)
sales_full   = pd.read_csv(f"{data_dir}/sales_recent.csv", low_memory=False)
closing_full = pd.read_csv(f"{data_dir}/closing_stock.csv", low_memory=False)
alloc_full   = pd.read_csv(f"{data_dir}/load_allocation.csv", low_memory=False)
for df in (fc_full, sales_full, closing_full, alloc_full):
    df["TrxDate"] = pd.to_datetime(df["TrxDate"], errors="coerce").dt.normalize()
    df["RouteCode"] = df.RouteCode.astype(str)
    df["ItemCode"]  = df.ItemCode.astype(str)
fc_full = fc_full[fc_full["Predicted"] > 0]

bias = BiasService(s)
engine = ReconciliationEngine(s)


def active_dates(route: str, end: pd.Timestamp, lb: int):
    sub = pd.concat([
        sales_full[sales_full.RouteCode == route].TrxDate,
        alloc_full[alloc_full.RouteCode == route].TrxDate,
    ]).unique()
    sub = sorted(d for d in sub if pd.notna(d) and d <= end)
    return sub[-lb:] if lb > 0 else []


def opening_for(route: str, day: pd.Timestamp, item: str) -> float:
    prev = day - pd.Timedelta(days=1)
    cell = closing_full[(closing_full.RouteCode == route)
                        & (closing_full.ItemCode == item)
                        & (closing_full.TrxDate == prev)]
    if cell.empty:
        return 0.0
    v = cell.ClosingQty.iloc[0]
    return float(v) if pd.notna(v) else 0.0


def run_engine(route: str, dates, *, v2: bool):
    fc = fc_full[(fc_full.RouteCode == route) & (fc_full.TrxDate.isin(dates))]
    if fc.empty:
        return None
    anchor = set(fc.ItemCode.unique())
    sold_anchor = sales_full[(sales_full.RouteCode == route)
                             & (sales_full.TrxDate.isin(dates))
                             & (sales_full.ItemCode.isin(anchor))]
    sold_total = float(sold_anchor.TotalQuantity.sum())
    rep_alloc = alloc_full[(alloc_full.RouteCode == route)
                           & (alloc_full.TrxDate.isin(dates))
                           & (alloc_full.ItemCode.isin(anchor))]
    rep_fresh_total = float(rep_alloc.AllocatedPC.sum())

    leftover_total = 0.0
    for d in dates:
        prev = d - pd.Timedelta(days=1)
        sub = closing_full[(closing_full.RouteCode == route)
                           & (closing_full.TrxDate == prev)
                           & (closing_full.ItemCode.isin(anchor))]
        leftover_total += float(sub.ClosingQty.fillna(0).sum())

    rep_van_load = leftover_total + rep_fresh_total

    if v2:
        bias_lookback = int(s.bias_lookback_days)
        cutoff = max(dates) - pd.Timedelta(days=bias_lookback)
        sales_window = sales_full[(sales_full.RouteCode == route)
                                  & (sales_full.TrxDate >= cutoff)]
        n_active_idx = sales_window.groupby("ItemCode")["TrxDate"].nunique().astype(int).to_dict()
        cls_col = ("class" if "class" in fc_full.columns
                   else "DemandClass" if "DemandClass" in fc_full.columns else None)
        cls_idx = (fc.drop_duplicates("ItemCode").set_index("ItemCode")[cls_col].astype(str).to_dict()
                   if cls_col else {})
        q_low_col = next((c for c in ("q_10", "q10", "Q10") if c in fc.columns), None)
        q_high_col = next((c for c in ("q_90", "q90", "Q90") if c in fc.columns), None)
    else:
        n_active_idx, cls_idx = {}, {}
        q_low_col = q_high_col = None

    rec_fresh_total = 0.0
    for d in dates:
        fc_day = fc[fc.TrxDate == d]
        if fc_day.empty:
            continue
        items = fc_day.ItemCode.to_numpy()
        forecasts = fc_day["Predicted"].to_numpy(dtype=float)
        biases = np.array([bias.lookup(route, ic) or 0.0 for ic in items], dtype=float)
        openings = np.array([opening_for(route, d, ic) for ic in items], dtype=float)
        kwargs = dict(forecasts=forecasts, bias_pcts=biases, openings=openings, use_carry_floor=False)
        if v2:
            n_active = np.array([float(n_active_idx.get(ic, 0)) for ic in items], dtype=float)
            classes = np.array([cls_idx.get(ic, "") for ic in items], dtype=object)
            kwargs.update(n_active_days=n_active, classes=classes)
            if q_low_col and q_high_col:
                kwargs["q_lows"] = pd.to_numeric(fc_day[q_low_col], errors="coerce").to_numpy(dtype=float)
                kwargs["q_highs"] = pd.to_numeric(fc_day[q_high_col], errors="coerce").to_numpy(dtype=float)
        loads, *_ = engine.recommend_batch(**kwargs)
        rec_fresh_total += float(loads.sum())

    rec_van_load = leftover_total + rec_fresh_total
    return {
        "rep_van_load": rep_van_load, "rec_van_load": rec_van_load, "sold": sold_total,
        "rep_fresh": rep_fresh_total, "our_fresh": rec_fresh_total, "leftover": leftover_total,
        "rep_overnight": max(rep_van_load - sold_total, 0.0),
        "our_overnight": max(rec_van_load - sold_total, 0.0),
    }


print(f"{'Route':>5}  {'LB':>3}  {'Sold':>7}  {'V5_b rec':>9} {'V2 rec':>9}  "
      f"{'V5_b nite':>9} {'V2 nite':>9}  {'d-overnight':>11}  {'d-fresh':>8}")
print("-" * 108)
all_v5: dict[int, list] = {1: [], 7: [], 30: []}
all_v2: dict[int, list] = {1: [], 7: [], 30: []}
for route in ROUTES:
    for lb in LOOKBACKS:
        dates = active_dates(route, END, lb)
        if not dates:
            continue
        v5 = run_engine(route, dates, v2=False)
        v2 = run_engine(route, dates, v2=True)
        if not v5 or not v2:
            continue
        all_v5[lb].append(v5); all_v2[lb].append(v2)
        d_overnight = v5["our_overnight"] - v2["our_overnight"]
        d_fresh = v5["our_fresh"] - v2["our_fresh"]
        v5_rec = v5["rec_van_load"]
        v2_rec = v2["rec_van_load"]
        v5_nite = v5["our_overnight"]
        v2_nite = v2["our_overnight"]
        sold = v5["sold"]
        print(f"{route:>5}  {lb:>3}  {sold:>7.0f}  {v5_rec:>9.0f} {v2_rec:>9.0f}  "
              f"{v5_nite:>9.0f} {v2_nite:>9.0f}  {d_overnight:>+11.0f}  {d_fresh:>+8.0f}")

print("-" * 108)
print()
print(f"Fleet aggregate (sum across routes per window):")
print(f"{'LB':>3}  {'Sold':>9}  {'Rep nite':>9}  {'V5_b nite':>9} {'V5_b/rep':>10}  "
      f"{'V2 nite':>9} {'V2/rep':>10}  {'V2/V5_b':>10}")
for lb in LOOKBACKS:
    rep_nite = sum(r["rep_overnight"] for r in all_v5[lb])
    v5_nite = sum(r["our_overnight"] for r in all_v5[lb])
    v2_nite = sum(r["our_overnight"] for r in all_v2[lb])
    sold = sum(r["sold"] for r in all_v5[lb])
    v5_save_rep = (rep_nite - v5_nite) / max(rep_nite, 1) * 100
    v2_save_rep = (rep_nite - v2_nite) / max(rep_nite, 1) * 100
    v2_save_v5 = (v5_nite - v2_nite) / max(v5_nite, 1) * 100
    print(f"{lb:>3}  {sold:>9.0f}  {rep_nite:>9.0f}  {v5_nite:>9.0f} {v5_save_rep:>+9.1f}%  "
          f"{v2_nite:>9.0f} {v2_save_rep:>+9.1f}%  {v2_save_v5:>+9.1f}%")
