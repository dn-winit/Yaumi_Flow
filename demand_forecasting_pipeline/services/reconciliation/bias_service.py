"""Rolling per-(route, item) reconciliation cache; two values per pair.

  bias_pct          = clipped mean relative error (legacy fallback).
  calibration_ratio = sum(actual) / sum(predicted) (preferred; uncapped).

Engine prefers ratio; bias_pct used when no calibration history exists.
mtime-keyed against demand_forecast.csv; parquet artifact for cold starts.
"""
from __future__ import annotations

import logging
import os
import threading
from pathlib import Path

import numpy as np
import pandas as pd

from demand_forecasting_pipeline.config.settings import Settings, get_settings

logger = logging.getLogger(__name__)


class BiasService:
    """Per-(route, item) bias_pct + calibration_ratio cache; parquet-persisted, mtime-keyed."""

    def __init__(self, settings: Settings | None = None) -> None:
        self._s = settings or get_settings()
        self._lock = threading.Lock()
        self._cache_key: tuple[int, int] | None = None
        # Parallel tables computed in one pass.
        self._cache_bias: dict[tuple[str, str], float] | None = None
        self._cache_calibration: dict[tuple[str, str], float] | None = None

    def get_table(self) -> dict[tuple[str, str], float]:
        """Legacy bias_pct table."""
        self._ensure_cached()
        return self._cache_bias or {}

    def get_calibration_table(self) -> dict[tuple[str, str], float]:
        """Calibration ratios; pairs without history are absent (caller defaults to 1.0)."""
        self._ensure_cached()
        return self._cache_calibration or {}

    def lookup(self, route_code: str, item_code: str) -> float:
        return self.get_table().get((str(route_code), str(item_code)), 0.0)

    def lookup_calibration(self, route_code: str, item_code: str) -> float:
        """Calibration ratio; defaults to 1.0 (no correction) when no history."""
        return self.get_calibration_table().get(
            (str(route_code), str(item_code)), 1.0,
        )

    def correct(self, predicted: float, route_code: str, item_code: str) -> float:
        """Adaptive: calibration_ratio if available, else legacy bias_pct."""
        if predicted <= 0:
            return 0.0
        ratio = self.get_calibration_table().get((str(route_code), str(item_code)))
        if ratio is not None:
            return float(predicted) * float(ratio)
        bias = self.lookup(route_code, item_code)
        return float(predicted) / (1.0 + bias)

    def invalidate(self) -> None:
        with self._lock:
            self._cache_key = None
            self._cache_bias = None
            self._cache_calibration = None

    # ------------------------------------------------------------------
    # Cache machinery
    # ------------------------------------------------------------------

    def _ensure_cached(self) -> None:
        path = self._forecast_path()
        if not path.exists():
            with self._lock:
                self._cache_bias = self._cache_bias or {}
                self._cache_calibration = self._cache_calibration or {}
            return
        stat = path.stat()
        key = (stat.st_mtime_ns, stat.st_size)
        with self._lock:
            if self._cache_key == key and self._cache_bias is not None and self._cache_calibration is not None:
                return
            persisted = self._load_persisted(key)
            if persisted is not None:
                self._cache_bias, self._cache_calibration = persisted
            else:
                self._cache_bias, self._cache_calibration = self._compute(path)
            self._cache_key = key

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _forecast_path(self) -> Path:
        return self._s.shared_data_path(self._s.demand_forecast_file)

    def _persisted_path(self) -> Path:
        return self._s.artifact_path(self._s.bias_table_file)


    def _load_persisted(
        self, key: tuple[int, int],
    ) -> tuple[dict[tuple[str, str], float], dict[tuple[str, str], float]] | None:
        path = self._persisted_path()
        if not path.exists():
            return None
        try:
            df = pd.read_parquet(path)
            stored_key = df.attrs.get("source_key")
            if stored_key != list(key):
                return None
            if "calibration_ratio" not in df.columns:
                # Legacy parquet (bias_pct only) -- treat as a cache miss
                # so the next call recomputes the calibration table.
                return None
            bias_table = {
                (str(r), str(i)): float(b)
                for r, i, b in df[["route_code", "item_code", "bias_pct"]].itertuples(index=False)
            }
            calib_table = {
                (str(r), str(i)): float(c)
                for r, i, c in df[["route_code", "item_code", "calibration_ratio"]].itertuples(index=False)
            }
            logger.info("BiasService: loaded %d entries from %s (bias + calibration)",
                        len(bias_table), path)
            return bias_table, calib_table
        except Exception as exc:
            logger.warning("BiasService: persisted table unreadable (%s); recomputing", exc)
            return None

    def _persist(
        self,
        bias_table: dict[tuple[str, str], float],
        calib_table: dict[tuple[str, str], float],
        key: tuple[int, int],
    ) -> None:
        path = self._persisted_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        keys = sorted(set(bias_table.keys()) | set(calib_table.keys()))
        df = pd.DataFrame(
            [
                {
                    "route_code":        r,
                    "item_code":         i,
                    "bias_pct":          float(bias_table.get((r, i), 0.0)),
                    "calibration_ratio": float(calib_table.get((r, i), 1.0)),
                }
                for (r, i) in keys
            ]
        )
        df.attrs["source_key"] = list(key)
        # Atomic write: a concurrent engine read mid-publish would otherwise
        # hit a half-written parquet and crash. tmp+os.replace is the same
        # idiom every other artifact in this pipeline uses.
        tmp = path.with_suffix(path.suffix + ".tmp")
        try:
            df.to_parquet(tmp, index=False)
            os.replace(tmp, path)
        except Exception as exc:
            logger.warning("BiasService: failed to persist table (%s)", exc)
            try:
                tmp.unlink(missing_ok=True)
            except Exception:
                pass

    def _compute(
        self, path: Path,
    ) -> tuple[dict[tuple[str, str], float], dict[tuple[str, str], float]]:
        """Compute the per-pair calibration_ratio (and the legacy bias_pct
        fallback) from the last ``bias_lookback_days`` of data.

        Two sources, joined per (route, item, date):
          * ``demand_forecast.csv`` -> ``Predicted``       (model output)
          * ``customer_data.csv``   -> ``TotalQuantity``   (real sales)

        Per-pair: ``calibration_ratio = sum(actual) / sum(predicted)``.
        That's it. No shrinkage, no decay, no extras. Pairs with no
        predicted signal in the window are dropped (engine falls back
        to the legacy bias formula for them).
        """
        cap = float(self._s.bias_cap_pct)
        days = int(self._s.bias_lookback_days)

        # ---- Predictions: sum per (route, item, date) --------------------
        try:
            preds = pd.read_csv(
                path,
                usecols=["TrxDate", "RouteCode", "ItemCode", "Predicted"],
                low_memory=False,
            )
        except Exception as exc:
            logger.error("BiasService: failed reading %s: %s", path, exc)
            return {}, {}
        if preds.empty:
            return {}, {}
        preds["TrxDate"] = pd.to_datetime(preds["TrxDate"], errors="coerce")
        preds = preds.dropna(subset=["TrxDate"])
        preds = preds[preds["Predicted"] > 0]
        if preds.empty:
            return {}, {}

        # Exclude unsettled days from the calibration window. The
        # ``preds`` frame carries the full forecast horizon (30+ days
        # ahead), so anchoring on its max would slide the window into
        # the future where no actuals exist. Anchor on today's wall
        # clock minus the settlement guard the accuracy_service
        # already uses: today + (settle-1) prior days are excluded
        # (partial-day sales, late-return-lag adjustments).
        #
        # ``preds["TrxDate"]`` is naive local-day after the to_datetime
        # parse above; anchoring on ``utcnow().normalize()`` would drift
        # by a day in non-UTC deployments. Cap the anchor at the
        # frame's own max so the window can never slide into the future
        # of the data we have.
        settle = int(getattr(self._s, "accuracy_settlement_window_days", 2))
        today_local = pd.Timestamp.now().normalize()
        preds_max = preds["TrxDate"].max()
        anchor = min(today_local, preds_max) - pd.Timedelta(days=settle)
        cutoff = anchor - pd.Timedelta(days=days)
        preds = preds[(preds["TrxDate"] >= cutoff) & (preds["TrxDate"] <= anchor)]
        if preds.empty:
            return {}, {}

        preds["RouteCode"] = preds["RouteCode"].astype(str).str.strip()
        preds["ItemCode"]  = preds["ItemCode"].astype(str).str.strip()
        preds["Predicted"] = preds["Predicted"].astype(float)
        preds = (
            preds.groupby(["RouteCode", "ItemCode", "TrxDate"], as_index=False)
                 ["Predicted"].sum()
        )

        # ---- Real actuals from sales_recent.csv --------------------------
        sales_path = self._s.shared_data_path(self._s.sales_recent_file)
        actuals = pd.DataFrame(columns=["RouteCode", "ItemCode", "TrxDate", "ActualQty"])
        active_dates_per_route: dict[str, set] = {}
        if sales_path.exists():
            try:
                sales = pd.read_csv(
                    sales_path,
                    usecols=["TrxDate", "RouteCode", "ItemCode", "TotalQuantity"],
                    low_memory=False,
                )
                sales["TrxDate"] = pd.to_datetime(sales["TrxDate"], errors="coerce")
                sales = sales.dropna(subset=["TrxDate"])
                sales = sales[(sales["TrxDate"] >= cutoff) & (sales["TrxDate"] <= anchor)]
                sales["RouteCode"] = sales["RouteCode"].astype(str).str.strip()
                sales["ItemCode"]  = sales["ItemCode"].astype(str).str.strip()
                sales["TotalQuantity"] = (
                    pd.to_numeric(sales["TotalQuantity"], errors="coerce")
                      .fillna(0.0).clip(lower=0.0)
                )
                # A route's "active days" = dates where it had any sales
                # activity at all. The model emits predictions for every
                # day, but on days the route wasn't visiting that route's
                # customers, ``actual = 0`` is NOT a real over-prediction
                # signal -- the rep simply wasn't there. Filtering the
                # calibration window to active days gives the honest
                # comparison: predicted vs actual on days the route was
                # serving.
                for rc, g in sales.groupby("RouteCode"):
                    active_dates_per_route[rc] = set(g["TrxDate"].unique())
                actuals = (
                    sales.groupby(["RouteCode", "ItemCode", "TrxDate"], as_index=False)
                         ["TotalQuantity"].sum()
                         .rename(columns={"TotalQuantity": "ActualQty"})
                )
            except Exception as exc:
                logger.warning("BiasService: failed reading %s (%s); calibration disabled",
                               sales_path, exc)
        else:
            logger.warning("BiasService: %s missing; calibration disabled", sales_path)

        # Filter predictions to dates where the route was serving. This
        # is the "honest comparison" -- we only judge the model on days
        # the rep actually visited. Vectorised via merge -- the previous
        # row-by-row ``apply`` was O(n^2) on large frames; merge against
        # an active-pairs frame is O(n log n) and gives the same result.
        if active_dates_per_route:
            active_pairs_df = pd.DataFrame(
                [
                    {"RouteCode": rc, "TrxDate": d}
                    for rc, dates in active_dates_per_route.items()
                    for d in dates
                ]
            )
            preds = preds.merge(active_pairs_df, on=["RouteCode", "TrxDate"], how="inner")
            if preds.empty:
                return {}, {}

        # ---- Join + per-pair recency-weighted ratio ----------------------
        # FMCG buying patterns shift week-to-week (promotions, seasonality,
        # routes adding / dropping customers). An equal-weight 30-day
        # average is too sluggish for fast-movers and too noisy for
        # rare-movers. Solution: exponential weight per row, with
        # half-life DERIVED FROM EACH PAIR'S OWN purchase cadence:
        #
        #     half_life_days = window_days / max(n_active_days_for_pair, 1)
        #                    = avg gap between sales for this pair
        #
        # That makes:
        #   - smooth daily-seller pairs:  half_life ~= 1 day  -> very responsive
        #   - intermittent (2-3x/week):   half_life ~= 3 days -> tracks weekly drifts
        #   - lumpy (once a month):       half_life ~= cycle  -> gentle smoothing
        #
        # No global magic numbers: every pair's responsiveness is set by
        # its own observed cadence. The ratio remains
        # ``weighted_actual / weighted_predicted`` so the engine's
        # ``forecast_corrected = predicted * ratio`` semantics are
        # unchanged; what changes is the recency of the sample.
        df = preds.merge(actuals, on=["RouteCode", "ItemCode", "TrxDate"], how="left")
        df["ActualQty"] = df["ActualQty"].fillna(0.0)

        # n_active_days per pair: distinct dates where ActualQty > 0.
        # That's the pair's own evidence of "this is a real selling
        # cadence" -- pairs that never sold get 0 -> half_life clamped
        # to the full window so weights are nearly flat (no over-fitting
        # to nothing).
        active = df[df["ActualQty"] > 0]
        n_active = (
            active.groupby(["RouteCode", "ItemCode"])["TrxDate"].nunique()
            if not active.empty
            else pd.Series(dtype="int64")
        )
        n_active.name = "n_active"
        df = df.merge(n_active, on=["RouteCode", "ItemCode"], how="left")
        df["n_active"] = df["n_active"].fillna(0).astype(float).clip(lower=1.0)
        df["half_life"] = float(days) / df["n_active"]
        df["days_back"] = (anchor - df["TrxDate"]).dt.days.clip(lower=0).astype(float)
        df["weight"]    = np.power(0.5, df["days_back"] / df["half_life"])
        df["w_actual"]    = df["weight"] * df["ActualQty"]
        df["w_predicted"] = df["weight"] * df["Predicted"]

        agg = (
            df.groupby(["RouteCode", "ItemCode"], as_index=False)
              .agg(sum_actual=("ActualQty",    "sum"),
                   sum_predicted=("Predicted", "sum"),
                   sum_w_actual=("w_actual",     "sum"),
                   sum_w_predicted=("w_predicted","sum"))
        )
        agg = agg[(agg["sum_predicted"] > 0.0) & (agg["sum_w_predicted"] > 0.0)]

        # Per-pair: TWO ratios derived from the same pair's history.
        #   * flat_ratio    = sum(actual) / sum(predicted)
        #     "What this pair has been doing on average over the window."
        #   * recency_ratio = sum(weight*actual) / sum(weight*predicted)
        #     "What this pair has been doing lately."
        # Cap rule (per pair, no global number):
        #   - flat_ratio >= 1 (pair under-predicted overall) -> trust
        #     recency: it captures recent acceleration / drift up.
        #   - flat_ratio  < 1 (pair over-predicted overall) -> use the
        #     SMALLER of (flat, recency). A rare recent big-sale day
        #     should never push us to load MORE than the pair's own
        #     typical pattern says is normal -- that protects lumpy
        #     items from amplification while still letting smooth
        #     pairs adapt downward when demand drops.
        flat = agg["sum_actual"]    / agg["sum_predicted"]
        rec  = agg["sum_w_actual"]  / agg["sum_w_predicted"]
        ratios = np.where(flat >= 1.0, rec, np.minimum(flat, rec))
        # Clip to ``[0, calibration_cap]``. Lower bound 0 lets dormant
        # pairs (sum_actual = 0) recommend zero fresh -- correct. Upper
        # bound is settings-driven (default 2.0): a single high-sale day
        # on a sparse-history item can produce a recency_ratio of 5-80x
        # which the previous uncapped clip then propagated across every
        # future forecast day, generating phantom demand. The 2.0 cap
        # leaves a 100% safety buffer over the model while bounding the
        # tail. The complementary maturity shrinkage in the engine
        # kernel (engine.py L1) further attenuates ratios from low-
        # active-day pairs toward 1.0 (no correction).
        cap = float(self._s.bias_calibration_cap)
        ratios = np.clip(ratios, a_min=0.0, a_max=cap)

        calib_table: dict[tuple[str, str], float] = {
            (str(rc), str(ic)): float(r)
            for rc, ic, r in zip(agg["RouteCode"], agg["ItemCode"], ratios, strict=False)
        }

        # ---- Legacy bias_pct (used as fallback for pairs without ratio) --
        nz = df[df["ActualQty"] > 0]
        if nz.empty:
            bias_table: dict[tuple[str, str], float] = {}
        else:
            bias_raw = ((nz["Predicted"] - nz["ActualQty"]) / nz["ActualQty"]).clip(-cap, cap)
            agg_bias = (
                pd.DataFrame({"RouteCode": nz["RouteCode"],
                              "ItemCode": nz["ItemCode"],
                              "bias": bias_raw})
                  .groupby(["RouteCode", "ItemCode"])["bias"].mean()
            )
            bias_table = {(str(r), str(i)): float(b) for (r, i), b in agg_bias.items()}

        stat = path.stat()
        self._persist(bias_table, calib_table, (stat.st_mtime_ns, stat.st_size))
        logger.info(
            "BiasService: %d calibration ratios + %d bias entries over %d (route, item, date) cells",
            len(calib_table), len(bias_table), len(df),
        )
        return bias_table, calib_table
