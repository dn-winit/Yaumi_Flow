"""Cross-check the pattern-envelope refactor against demand_forecast.csv.

Verifies the 7 identities for routes 9105, 9115, 9219, 9209 on 2026-05-12,
plus probes the specific item 50-0730 / route 9115 and surfaces one
ceiling-fired case + one class_factors fallback case.

Run from project root:
    .venv\\Scripts\\python.exe demand_forecasting_pipeline\\scripts\\envelope_xcheck.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

PROJECT_ROOT = Path(r"D:\Projects\Yaumi\forecast_new")
sys.path.insert(0, str(PROJECT_ROOT))

from demand_forecasting_pipeline.config.settings import get_settings  # noqa: E402
from demand_forecasting_pipeline.services.reconciliation.bias_service import BiasService  # noqa: E402

EPS_FACTOR = 0.01  # 1% tolerance for forecast_corrected identity
EPS_ABS = 0.05     # absolute tolerance for floor/ceiling band check
TARGET_DATE = "2026-05-12"
TARGET_ROUTES = ["9105", "9115", "9219", "9209"]


def main() -> int:
    s = get_settings()
    bias_svc = BiasService(s)
    calib_table = bias_svc.get_calibration_table()
    cold_start_ratio = float(s.calibration_cold_start_ratio)
    fc_path = PROJECT_ROOT / "data" / "imports" / "demand_forecast.csv"
    sr_path = PROJECT_ROOT / "data" / "imports" / "sales_recent.csv"
    fc = pd.read_csv(fc_path, low_memory=False)
    sr = pd.read_csv(sr_path, low_memory=False)
    fc["TrxDate"] = pd.to_datetime(fc["TrxDate"], errors="coerce").dt.normalize()
    sr["TrxDate"] = pd.to_datetime(sr["TrxDate"], errors="coerce").dt.normalize()
    fc["RouteCode"] = fc["RouteCode"].astype(str)
    fc["ItemCode"] = fc["ItemCode"].astype(str)
    sr["RouteCode"] = sr["RouteCode"].astype(str)
    sr["ItemCode"] = sr["ItemCode"].astype(str)
    sr["TotalQuantity"] = pd.to_numeric(sr["TotalQuantity"], errors="coerce").fillna(0.0)

    # Recompute recent stats from sales_recent.csv directly for ground truth.
    window_days = int(s.forecast_below_recent_window_days)
    sr_max = sr["TrxDate"].max()
    cutoff = sr_max - pd.Timedelta(days=window_days)
    sr_win = sr[sr["TrxDate"] >= cutoff]
    daily = (
        sr_win.groupby(["RouteCode", "ItemCode", "TrxDate"], sort=False)["TotalQuantity"]
        .sum().astype(float)
    )
    daily = daily[daily > 0.0]
    agg = daily.groupby(level=[0, 1], sort=False).agg(["mean", "std", "count"])
    truth_idx = {
        (str(r), str(i)): (float(row["mean"]),
                           float(row["std"]) if pd.notna(row["std"]) else 0.0,
                           int(row["count"]))
        for (r, i), row in agg.iterrows()
    }

    target_dt = pd.Timestamp(TARGET_DATE).normalize()
    day_fc = fc[fc["TrxDate"] == target_dt]

    def class_floor_factor(cls: str) -> float:
        return s.pattern_floor_factor_for_class(cls)

    def class_ceiling_factor(cls: str) -> float:
        return s.pattern_ceiling_factor_for_class(cls)

    def below_factor(cls: str) -> float:
        return s.forecast_below_recent_factor_for_class(cls)

    def z_for_class(cls: str) -> float:
        return s.pattern_envelope_z_for_class(cls)

    results = []
    summary = {
        "z_score_rows": 0,
        "class_factors_rows": 0,
        "ceiling_fired_z": 0,
        "floor_fired_z": 0,
        "ceiling_fired_cls": 0,
        "floor_fired_cls": 0,
    }
    fail_count = 0

    # For each route, pick 3 items with the highest recent_avg so identities
    # are non-trivial.
    for route in TARGET_ROUTES:
        sub = day_fc[day_fc["RouteCode"] == route].copy()
        if sub.empty:
            print(f"WARN: route {route} has no rows on {TARGET_DATE}", file=sys.stderr)
            continue
        sub["_ra"] = pd.to_numeric(sub["RecentAvgPerSellingDay"], errors="coerce").fillna(0.0)
        sub = sub.sort_values("_ra", ascending=False)
        # Take the top 3 with non-zero recent_avg, plus pin item 50-0730
        # specifically for route 9115.
        picks = []
        if route == "9115":
            r9115_target = sub[sub["ItemCode"] == "50-0730"]
            if not r9115_target.empty:
                picks.append(r9115_target.iloc[0])
        for _, r in sub.head(6).iterrows():
            if any(p["ItemCode"] == r["ItemCode"] for p in picks):
                continue
            picks.append(r)
            if len(picks) >= 3 + (1 if route == "9115" else 0):
                break

        for r in picks:
            item = str(r["ItemCode"])
            cls = str(r.get("DemandClass") or "").strip().lower()
            predicted = float(r["Predicted"])
            bias_pct = float(r["BiasPct"])
            forecast_corrected = float(r["ForecastCorrected"])
            recent_avg_csv = float(r["RecentAvgPerSellingDay"])
            recent_std_csv = float(r.get("RecentStdPerSellingDay", 0.0))
            expected_demand = float(r["ExpectedDemand"])
            opening_stock = float(r["OpeningStock"])
            recommended_load = float(r["RecommendedLoad"])
            floor_app = str(r["PatternFloorApplied"]).strip().lower() in {"true", "1", "t", "yes"}
            ceiling_app = str(r["PatternCeilingApplied"]).strip().lower() in {"true", "1", "t", "yes"}
            basis = str(r.get("EnvelopeBasis", "class_factors")).strip() or "class_factors"
            below_recent = str(r["ForecastBelowRecent"]).strip().lower() in {"true", "1", "t", "yes"}

            truth = truth_idx.get((route, item), (0.0, 0.0, 0))
            t_mean, t_std, t_active = truth

            row_failures = []

            # Identity 1: forecast_corrected = engine's POST-L4 target
            # (documented in engine.py recommend_batch). For classes with
            # loading_quantile != 0.5 (erratic / lumpy) L4 interpolates
            # the target between (q_low, p_corr, q_high) so a strict
            # "predicted * (1-bias) ~ fc" check only applies when the
            # class quantile is 0.5 (smooth / intermittent / default).
            cap = s.bias_trim_cap_for_class(cls)
            ratio_raw = calib_table.get((route, item))
            if ratio_raw is not None and np.isfinite(ratio_raw):
                ratio = float(ratio_raw)
                if cap is not None:
                    ratio = max(1.0 - cap, min(1.0 + cap, ratio))
                pre_l4_fc = predicted * ratio
                path = "calib"
            else:
                pre_l4_fc = predicted * (1.0 - bias_pct)
                path = "bias"
            class_q = s.loading_quantile_for_class(cls)
            l4_active = (cls in {"erratic", "lumpy"}) and abs(class_q - 0.5) > 1e-9
            if predicted > 0 and not l4_active:
                # No L4 interpolation -> stored fc must equal pre-L4 fc.
                rel = abs(pre_l4_fc - forecast_corrected) / max(abs(predicted), 1.0)
                if rel > 0.02:
                    row_failures.append(
                        f"fc identity drift ({path}): expected~{pre_l4_fc:.4f} got {forecast_corrected:.4f} (rel={rel:.3f})"
                    )
            elif predicted > 0 and l4_active:
                # L4 active: fc is the post-L4 target. Sanity-check it
                # sits inside the quantile band [q_low, q_high].
                q_lo = float(r.get("LowerBound") or 0.0)
                q_hi = float(r.get("UpperBound") or 0.0)
                if q_hi > 0:
                    lo_ok = forecast_corrected >= min(q_lo, pre_l4_fc) - 1e-6
                    hi_ok = forecast_corrected <= max(q_hi, pre_l4_fc) + 1e-6
                    if not (lo_ok and hi_ok):
                        row_failures.append(
                            f"fc out of L4 band ({path}): fc={forecast_corrected:.4f} pre_l4={pre_l4_fc:.4f} q_lo={q_lo:.4f} q_hi={q_hi:.4f}"
                        )

            # Identity 2: recent_avg matches our recomputation.
            if abs(recent_avg_csv - t_mean) > 0.01:
                row_failures.append(
                    f"recent_avg drift: csv={recent_avg_csv:.4f} recomputed={t_mean:.4f}"
                )

            # Identity 3: recent_std matches recomputation.
            if abs(recent_std_csv - t_std) > 0.01:
                row_failures.append(
                    f"recent_std drift: csv={recent_std_csv:.4f} recomputed={t_std:.4f}"
                )

            # Identity 4: expected_demand in [floor, ceiling].
            if basis == "z_score":
                z = z_for_class(cls)
                floor = max(0.0, t_mean - z * t_std)
                ceiling = t_mean + z * t_std
                summary["z_score_rows"] += 1
            else:
                floor = t_mean * class_floor_factor(cls) if t_mean > 0 else 0.0
                ceiling = t_mean * class_ceiling_factor(cls) if t_mean > 0 else None
                summary["class_factors_rows"] += 1
            if t_mean > 0:
                lo_ok = expected_demand >= floor - EPS_ABS
                hi_ok = (ceiling is None) or (expected_demand <= ceiling + EPS_ABS)
                if not lo_ok:
                    row_failures.append(
                        f"expected_demand below floor: ed={expected_demand:.4f} floor={floor:.4f}"
                    )
                if not hi_ok:
                    row_failures.append(
                        f"expected_demand above ceiling: ed={expected_demand:.4f} ceiling={ceiling:.4f}"
                    )

            # Identity 5: floor/ceiling flag identities.
            if t_mean > 0:
                expect_floor = expected_demand > forecast_corrected + 1e-6
                expect_ceil = expected_demand < forecast_corrected - 1e-6
                if expect_floor != floor_app:
                    row_failures.append(
                        f"floor flag mismatch: csv={floor_app} expected={expect_floor} (ed={expected_demand:.4f} fc={forecast_corrected:.4f})"
                    )
                if expect_ceil != ceiling_app:
                    row_failures.append(
                        f"ceiling flag mismatch: csv={ceiling_app} expected={expect_ceil} (ed={expected_demand:.4f} fc={forecast_corrected:.4f})"
                    )

            # Identity 6: recommended_van_load = opening + max(0, expected - opening).
            # For smooth/intermittent classes the engine loads at the mean
            # (q=0.5). For erratic/lumpy quantile loading kicks in, so
            # check approximately. recommended_load is the FRESH load.
            van_load = opening_stock + max(0.0, recommended_load)
            engine_expected_fresh = max(0.0, expected_demand - opening_stock)
            if cls in {"smooth", "intermittent"}:
                if abs(recommended_load - engine_expected_fresh) > 1.0:
                    row_failures.append(
                        f"van_load identity drift ({cls}): fresh={recommended_load:.4f} expected_fresh={engine_expected_fresh:.4f}"
                    )
            else:
                # Loose check for erratic/lumpy (quantile loading may bump up)
                if recommended_load < engine_expected_fresh - 1e-6:
                    row_failures.append(
                        f"van_load identity dropped below floor ({cls}): fresh={recommended_load:.4f} engine_floor={engine_expected_fresh:.4f}"
                    )

            # Identity 7: forecast_below_recent matches class-aware threshold.
            if t_mean > 0:
                below_calc = forecast_corrected < t_mean * below_factor(cls)
                if below_calc != below_recent:
                    row_failures.append(
                        f"below_recent flag mismatch: csv={below_recent} expected={below_calc}"
                    )

            # Counters for ceiling/floor fires by basis.
            if floor_app:
                if basis == "z_score":
                    summary["floor_fired_z"] += 1
                else:
                    summary["floor_fired_cls"] += 1
            if ceiling_app:
                if basis == "z_score":
                    summary["ceiling_fired_z"] += 1
                else:
                    summary["ceiling_fired_cls"] += 1

            status = "PASS" if not row_failures else "FAIL"
            if row_failures:
                fail_count += 1
            results.append({
                "route": route,
                "item": item,
                "class": cls,
                "active_days": t_active,
                "basis": basis,
                "predicted": predicted,
                "bias_pct": round(bias_pct, 4),
                "fc": round(forecast_corrected, 3),
                "ra": round(recent_avg_csv, 3),
                "rstd": round(recent_std_csv, 3),
                "ed": round(expected_demand, 3),
                "floor_app": floor_app,
                "ceiling_app": ceiling_app,
                "below": below_recent,
                "open": round(opening_stock, 3),
                "load": round(recommended_load, 3),
                "status": status,
                "failures": row_failures,
            })

    print(f"\n=== Cross-check matrix ({TARGET_DATE}) ===")
    header = (
        f"{'route':>6} {'item':<12} {'cls':<13} {'active':>6} {'basis':<14} "
        f"{'pred':>7} {'bias':>7} {'fc':>8} {'ra':>8} {'rstd':>7} {'ed':>8} "
        f"{'floor':>5} {'ceil':>5} {'low':>5} {'open':>6} {'load':>7} {'status':<7}"
    )
    print(header)
    print("-" * len(header))
    for r in results:
        print(
            f"{r['route']:>6} {r['item']:<12} {r['class']:<13} {r['active_days']:>6} "
            f"{r['basis']:<14} {r['predicted']:>7.2f} {r['bias_pct']:>7.3f} "
            f"{r['fc']:>8.2f} {r['ra']:>8.2f} {r['rstd']:>7.2f} {r['ed']:>8.2f} "
            f"{str(r['floor_app']):>5} {str(r['ceiling_app']):>5} "
            f"{str(r['below']):>5} {r['open']:>6.2f} {r['load']:>7.2f} {r['status']:<7}"
        )
        for f in r["failures"]:
            print(f"    ! {f}")

    print("\n=== Summary ===")
    print(json.dumps(summary, indent=2))
    print(f"\nTotal rows: {len(results)}  Failures: {fail_count}")

    # Locate one ceiling-fired case under z_score and one class_factors row globally.
    z_ceiling_fc = day_fc[
        (day_fc["EnvelopeBasis"].astype(str).str.strip() == "z_score")
        & (day_fc["PatternCeilingApplied"].astype(str).str.strip().str.lower().isin({"true", "1", "t", "yes"}))
    ]
    cls_fb = day_fc[day_fc["EnvelopeBasis"].astype(str).str.strip() == "class_factors"]
    print(
        f"\nGlobal counts for {TARGET_DATE}: "
        f"z_ceiling_fired={len(z_ceiling_fc)} class_factors_rows={len(cls_fb)} "
        f"z_score_rows={len(day_fc) - len(cls_fb)}"
    )
    if not z_ceiling_fc.empty:
        ex = z_ceiling_fc.iloc[0]
        print(
            f"Example z-score ceiling fired: route={ex['RouteCode']} item={ex['ItemCode']} "
            f"cls={ex.get('DemandClass')} ra={float(ex['RecentAvgPerSellingDay']):.2f} "
            f"rstd={float(ex.get('RecentStdPerSellingDay', 0.0)):.2f} "
            f"fc={float(ex['ForecastCorrected']):.2f} ed={float(ex['ExpectedDemand']):.2f}"
        )
    if not cls_fb.empty:
        ex = cls_fb.iloc[0]
        print(
            f"Example class_factors fallback: route={ex['RouteCode']} item={ex['ItemCode']} "
            f"cls={ex.get('DemandClass')} ra={float(ex['RecentAvgPerSellingDay']):.2f} "
            f"rstd={float(ex.get('RecentStdPerSellingDay', 0.0)):.2f} "
            f"fc={float(ex['ForecastCorrected']):.2f} ed={float(ex['ExpectedDemand']):.2f}"
        )

    return 0 if fail_count == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
