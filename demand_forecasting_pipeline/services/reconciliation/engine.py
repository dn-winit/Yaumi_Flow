"""Reconciliation engine -- pure arithmetic, no I/O.

V5_b base formula (all knobs from Settings):

    P_corrected = Predicted / (1 + bias_pct)
    load        = max(carry_floor_pct * P_corrected,  P_corrected - opening)

V2 layers (kept after the audit -- only what measurably helps):

    L1b  Pair-maturity bias shrinkage
         effective_bias = bias_pct * min(1, n_active_days / threshold)
         Pairs with too few sale-days for the bias estimate to be
         meaningful get a proportionally weaker correction. Pairs at
         or above the threshold behave exactly like V5_b.

    L4   Quantile loading (newsvendor)
         target = interpolate(q_low, q50, q_high) at class_quantile
         q50 := P_corrected. Lumpy/erratic items deliberately load
         below the mean to trade lost-sales risk for less overnight
         stock. Smooth items load at q50 (== V5_b behaviour).

Both layers are opt-in via per-call arrays; absent argument => V5_b
behaviour. Either can be disabled in settings without code edits.

Empirically chosen on a 12-route audit: textbook ``+ z*sigma`` safety
stock fails because ``Predicted`` is already calibrated -- layering a
second uncertainty buffer double-counts noise. L1b + L4 add modest
improvement over V5_b without changing the model.

This module is intentionally I/O-free so the formula can be unit-tested
deterministically and so callers can swap bias / opening sources
without touching the engine.

Two surfaces:

* ``recommend_one``  -- single-row, dataclass-returning. Used by the live
  recommendation path and any caller that needs the full diagnostic
  breakdown for one item.
* ``recommend_batch`` -- vectorised over numpy arrays. Used by hot paths
  (past-performance window simulation, batch generation) where a 5-10x
  speed-up matters and the per-row dataclass overhead is wasted.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Sequence, Tuple

import numpy as np

from demand_forecasting_pipeline.config.settings import Settings, get_settings


# Decision-path labels surfaced per item so the explainability layer can
# tell the supervisor *why* the engine emitted a given fresh quantity.
# Centralised so writer + reader (UI tooltip) reference one set of strings.
DECISION_SKIP            = "skip"             # forecast <= 0
DECISION_LEFTOVER_COVERS = "leftover_covers"  # leftover >= target
DECISION_FRESH_LOAD      = "fresh_load"       # fresh = target - leftover
DECISION_FLOOR_PROTECTED = "floor_protected"  # carry-floor lifted candidate


@dataclass
class LoadRecommendation:
    """Per-item recommendation with full diagnostic breakdown."""

    item_code: str
    item_name: str = ""
    demand_class: Optional[str] = None
    forecast_raw: float = 0.0
    bias_pct: float = 0.0
    forecast_corrected: float = 0.0
    opening_stock: float = 0.0
    # V2 diagnostics (populated only when the corresponding inputs are
    # supplied; legacy callers without them see V5_b dict shape).
    loading_quantile: Optional[float] = None
    target_qty: Optional[float] = None
    recommended_load: float = 0.0
    carry_floor_applied: bool = False
    decision_path: str = DECISION_SKIP
    typical_alloc: Optional[float] = None
    decision_vs_typical: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "item_code": self.item_code,
            "item_name": self.item_name,
            "demand_class": self.demand_class,
            "forecast_raw":       round(self.forecast_raw, 2),
            "bias_pct":           round(self.bias_pct, 4),
            "forecast_corrected": round(self.forecast_corrected, 2),
            "opening_stock":      round(self.opening_stock, 2),
            "recommended_load":   round(self.recommended_load, 2),
            "carry_floor_applied": self.carry_floor_applied,
            "decision_path":      self.decision_path,
            "typical_alloc": (None if self.typical_alloc is None
                              else round(self.typical_alloc, 2)),
            "decision_vs_typical": self.decision_vs_typical,
        }
        if self.loading_quantile is not None:
            out["loading_quantile"] = round(self.loading_quantile, 4)
        if self.target_qty is not None:
            out["target_qty"] = round(self.target_qty, 2)
        return out


@dataclass
class ReconciliationEngine:
    """Stateless engine; only carries config defaults from Settings."""

    settings: Settings = field(default_factory=get_settings)

    @property
    def carry_floor_pct(self) -> float:
        return float(self.settings.carry_floor_pct)

    # ------------------------------------------------------------------
    # Constants
    # ------------------------------------------------------------------

    # The bias term lower bound. ``denom = 1 + bias_pct`` must stay
    # strictly positive or P_corrected diverges; clamping at -0.99 keeps
    # the math stable when an extreme bias slips past the configured cap.
    _BIAS_DENOM_MIN = -0.99
    # Divide-by-zero guard for the L4 quantile interpolation when
    # q_low == q_50 == q_high (degenerate predictive interval).
    _Q_INTERP_EPS = 1e-9

    # ------------------------------------------------------------------
    # Single-row arithmetic
    # ------------------------------------------------------------------

    def recommend_one(
        self,
        *,
        item_code: str,
        item_name: str = "",
        demand_class: Optional[str] = None,
        forecast: float,
        bias_pct: float,
        opening_stock: float,
        # L4 inputs -- predictive interval bounds. Either both supplied
        # or both None; partial input degrades to V5_b cleanly.
        forecast_q_low: Optional[float] = None,
        forecast_q_high: Optional[float] = None,
        # The interval the q_low / q_high cover (default 10..90 percentile
        # band, matching the model's emitted ``q_10`` / ``q_90`` columns).
        q_low_pct: float = 0.10,
        q_high_pct: float = 0.90,
        # L1b input -- per-pair sample size for bias-shrinkage. None ->
        # full correction (V5_b behaviour).
        n_active_days: Optional[float] = None,
        # Adaptive per-pair calibration ratio (preferred correction).
        # ``None`` -> kernel falls back to legacy bias_pct formula.
        calibration_ratio: Optional[float] = None,
        typical_alloc: Optional[float] = None,
        # Default False to match every production caller (enrich,
        # past_performance, /recommend). The carry floor was a
        # transitional safety net that no current path actually wants
        # firing -- leaving the default True was a footgun for any
        # future caller that forgot to pass the kwarg.
        use_carry_floor: bool = False,
    ) -> LoadRecommendation:
        """Compute the fresh-issuance recommendation for one (item, day)."""
        def _arr(v: Optional[float]) -> np.ndarray:
            return np.array([np.nan if v is None else float(v)], dtype=float)

        cls_arr: Optional[np.ndarray] = (
            np.array([demand_class], dtype=object) if demand_class is not None else None
        )

        loads, _p_corr_arr, floor_applied_arr, decisions_arr, diag = self._kernel(
            forecasts=np.array([forecast], dtype=float),
            bias_pcts=np.array([bias_pct], dtype=float),
            openings=np.array([opening_stock], dtype=float),
            use_carry_floor=use_carry_floor,
            q_lows=_arr(forecast_q_low) if forecast_q_low is not None else None,
            q_highs=_arr(forecast_q_high) if forecast_q_high is not None else None,
            classes=cls_arr,
            n_active_days=_arr(n_active_days) if n_active_days is not None else None,
            calibration_ratios=_arr(calibration_ratio) if calibration_ratio is not None else None,
            q_low_pct=q_low_pct,
            q_high_pct=q_high_pct,
        )
        load = float(loads[0])
        # ``forecast_corrected`` is the post-L4 target -- the same value
        # ``recommend_batch`` returns and the same value the kernel
        # actually subtracts opening from. Identity holds:
        #   load = max(0, forecast_corrected - opening_stock)
        target = float(diag["target_qty"][0])
        return LoadRecommendation(
            item_code=item_code, item_name=item_name, demand_class=demand_class,
            forecast_raw=float(forecast), bias_pct=float(bias_pct),
            forecast_corrected=target, opening_stock=float(opening_stock),
            recommended_load=load,
            carry_floor_applied=bool(floor_applied_arr[0]),
            decision_path=str(decisions_arr[0]),
            loading_quantile=(None if diag is None
                              else float(diag["loading_quantile"][0])),
            target_qty=(None if diag is None else float(diag["target_qty"][0])),
            typical_alloc=typical_alloc,
            decision_vs_typical=self._decide(load, typical_alloc),
        )

    def recommend_batch(
        self,
        *,
        forecasts: np.ndarray,
        bias_pcts: np.ndarray,
        openings: np.ndarray,
        # Default False to match every production caller. See
        # recommend_one for the rationale -- one consistent default
        # means a caller that forgets to pass the kwarg gets the
        # production-correct behaviour, not the dead-default 50% floor.
        use_carry_floor: bool = False,
        q_lows: Optional[np.ndarray] = None,
        q_highs: Optional[np.ndarray] = None,
        classes: Optional[Sequence[Any]] = None,
        n_active_days: Optional[np.ndarray] = None,
        calibration_ratios: Optional[np.ndarray] = None,
        q_low_pct: float = 0.10,
        q_high_pct: float = 0.90,
    ) -> Tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
        """Vectorised over (item, day) pairs.

        Required arrays (all 1-D, equal length):
          ``forecasts``, ``bias_pcts``, ``openings``.

        Optional arrays for the V2 layers:
          ``q_lows`` / ``q_highs`` -- model q_low_pct / q_high_pct quantile
                                      (L4 quantile loading)
          ``classes``              -- per-item demand-class string
                                      (L4 quantile lookup)
          ``n_active_days``        -- per-pair sample size (L1b shrinkage)
          ``calibration_ratios``   -- adaptive per-pair correction
                                      ``sum(actual)/sum(predicted)`` from
                                      the bias service. When supplied AND
                                      finite for a row, replaces the
                                      capped legacy bias correction with
                                      ``P_corrected = f * ratio``. Pairs
                                      missing a ratio (NaN) fall back to
                                      the legacy bias formula transparently.

        Each layer is OFF when its inputs are None or fully NaN; the
        engine falls back to V5_b for that pair, item-by-item.

        Returns ``(loads, forecast_corrected, carry_floor_applied,
        decision_path)`` where ``forecast_corrected`` is the **post-L4
        target** -- i.e. the same number the kernel subtracts opening
        from to produce ``loads``. Callers that persist this column
        alongside ``opening_stock`` and ``recommended_load`` get the
        identity ``loads = max(0, forecast_corrected - opening_stock)``
        for free, which is what the dashboard relies on for
        transparency. (Pre-L4 ``p_corr`` is internal-only; if a future
        consumer needs it, surface a separate diagnostic.)
        """
        loads, _p_corr, floor_applied, decisions, diag = self._kernel(
            forecasts=np.asarray(forecasts, dtype=float),
            bias_pcts=np.asarray(bias_pcts, dtype=float),
            openings=np.asarray(openings, dtype=float),
            use_carry_floor=use_carry_floor,
            q_lows=(np.asarray(q_lows, dtype=float) if q_lows is not None else None),
            q_highs=(np.asarray(q_highs, dtype=float) if q_highs is not None else None),
            classes=(np.asarray(classes, dtype=object) if classes is not None else None),
            n_active_days=(np.asarray(n_active_days, dtype=float)
                           if n_active_days is not None else None),
            calibration_ratios=(np.asarray(calibration_ratios, dtype=float)
                                if calibration_ratios is not None else None),
            q_low_pct=q_low_pct,
            q_high_pct=q_high_pct,
        )
        return loads, diag["target_qty"], floor_applied, decisions

    # ------------------------------------------------------------------
    # Kernel
    # ------------------------------------------------------------------

    def _kernel(
        self,
        *,
        forecasts: np.ndarray,
        bias_pcts: np.ndarray,
        openings: np.ndarray,
        use_carry_floor: bool,
        q_lows: Optional[np.ndarray] = None,
        q_highs: Optional[np.ndarray] = None,
        classes: Optional[np.ndarray] = None,
        n_active_days: Optional[np.ndarray] = None,
        calibration_ratios: Optional[np.ndarray] = None,
        q_low_pct: float = 0.10,
        q_high_pct: float = 0.90,
    ) -> Tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, Optional[Dict[str, np.ndarray]]]:
        """Pure numpy kernel shared by ``recommend_one`` and
        ``recommend_batch``. One implementation, no drift between
        single-row and batch paths.

        Robustness contract -- any combination of inputs returns a
        well-defined output:
          * NaN / +inf forecasts treated as 0 (skip).
          * Negative forecasts treated as 0 (skip).
          * NaN / -inf bias treated as 0 (no correction).
          * Bias clamped at ``_BIAS_DENOM_MIN`` so the denominator is
            always strictly positive.
          * NaN / negative leftover treated as 0 (truck is empty).
          * NaN q_low / q_high       -> L4 disabled for that row.
          * NaN n_active_days        -> L1b disabled for that row.
          * NaN calibration_ratio    -> falls back to legacy bias formula
                                        for that row.
          * Negative calibration_ratio treated as 0 (dormant pair).
          * Missing class            -> per-class default from Settings.
          * Zero-length input        -> empty arrays in matching shape.
        """
        if forecasts.shape != bias_pcts.shape or forecasts.shape != openings.shape:
            raise ValueError(
                f"recommend_batch: shape mismatch -- forecasts={forecasts.shape}, "
                f"bias_pcts={bias_pcts.shape}, openings={openings.shape}"
            )
        for nm, a in (("q_lows", q_lows), ("q_highs", q_highs),
                      ("classes", classes), ("n_active_days", n_active_days)):
            if a is not None and a.shape != forecasts.shape:
                raise ValueError(
                    f"recommend_batch: shape mismatch -- {nm}={a.shape}, "
                    f"forecasts={forecasts.shape}"
                )

        n = forecasts.shape[0]
        loads = np.zeros(n, dtype=float)
        p_corr = np.zeros(n, dtype=float)
        floor_applied = np.zeros(n, dtype=bool)
        decisions = np.full(n, DECISION_SKIP, dtype=object)
        target_diag = np.zeros(n, dtype=float)
        quantile_diag = np.zeros(n, dtype=float)
        if n == 0:
            return (loads, p_corr, floor_applied, decisions,
                    {"target_qty": target_diag, "loading_quantile": quantile_diag})

        # ------- Sanitise inputs -----------------------------------------
        # NaN / -inf treated as missing/zero so a single bad row never
        # poisons the rest of the batch. ``+inf`` forecast falls through
        # ``active`` mask, producing inf load -- still bounded by the
        # upstream forecast cap, never crashes.
        f = np.where(np.isfinite(forecasts), forecasts, 0.0)
        b = np.where(np.isfinite(bias_pcts), bias_pcts, 0.0)
        o = np.where(np.isfinite(openings) & (openings > 0), openings, 0.0)

        active = f > 0
        if not active.any():
            return (loads, p_corr, floor_applied, decisions,
                    {"target_qty": target_diag, "loading_quantile": quantile_diag})

        # ------- L1b: Pair-maturity bias shrinkage ----------------------
        # Shrinks ``bias_pct`` toward zero when the pair has too few
        # sale-days for the per-row bias estimate to be trustworthy:
        #   maturity = clip(n_active_days / threshold, 0, 1)
        #   effective_bias = bias_pct * maturity
        # The legacy bias_pct is an average of per-row relative errors
        # so a handful of noisy rows can dominate it -- shrinkage stops
        # that. The calibration_ratio path uses sums (numerator and
        # denominator both aggregate over the whole window) so it is
        # already a stable estimator and does NOT need this shrinkage;
        # tail outliers are bounded by ``bias_calibration_cap`` in
        # ``BiasService._compute``.
        # NaN n_active_days OR layer disabled => maturity=1.0 (V5_b).
        if (self.settings.maturity_shrinkage_enabled
                and n_active_days is not None):
            threshold = max(float(self.settings.pair_maturity_threshold_days), 1.0)
            n_clean = np.where(np.isfinite(n_active_days) & (n_active_days >= 0),
                               n_active_days, threshold)
            maturity = np.clip(n_clean / threshold, 0.0, 1.0)
            b_effective = b * maturity
        else:
            b_effective = b

        # ------- L1: Adaptive correction ---------------------------------
        # Preferred path: per-pair calibration_ratio = sum(actual)/sum(predicted)
        # over a rolling, exponentially-decayed window with Bayesian
        # shrinkage toward 1.0. Industry-grade because it is:
        #   * symmetric (handles over- and under-forecast equally)
        #   * uncapped in a principled way (bounded by data, not by
        #     hardcoded magic numbers)
        #   * dormant-aware (ratio -> 0 for items that stop selling)
        #
        # Legacy fallback: ``f / (1 + bias_pct)`` is applied only to rows
        # where the calibration ratio is missing (NaN), so adopting
        # calibration is non-breaking for new pairs.
        bias_clamped = np.maximum(b_effective[active], self._BIAS_DENOM_MIN)
        denom = 1.0 + bias_clamped
        p_corr_legacy = f[active] / denom

        if calibration_ratios is not None:
            cr = np.where(
                np.isfinite(calibration_ratios) & (calibration_ratios >= 0.0),
                calibration_ratios, np.nan,
            )
            # Maturity shrinkage is NOT applied to the ratio. It already
            # is to bias_pct above (legacy fallback path) because the
            # bias_pct estimate is per-day variance and a few rows is
            # enough to make it noisy. The calibration_ratio is a
            # ratio of SUMS over the entire window -- a fundamentally
            # more stable estimator -- and applying shrinkage on top
            # under-corrects legitimate weekly-cadence pairs (5-7
            # active days in 30, threshold 14) without addressing the
            # tail outlier that the cap in BiasService._compute already
            # handles. Empirically the cap alone closes the 30-day gap
            # while the cap+shrinkage combination over-shrinks the
            # 7-day metric on well-behaved routes.
            cr_act = cr[active]
            use_calib = np.isfinite(cr_act)
            # Cold-start handling: pairs without calibration history land
            # NaN here. The legacy bias path produces raw forecast for
            # them (cold pairs typically have no bias history either) --
            # the worst behaviour for the highest-uncertainty rows.
            # Apply the configured cold-start ratio (settings-driven,
            # default 0.85) instead so brand-new pairs get a conservative
            # 15% shrink until the bias service accumulates enough data.
            cold_start = float(self.settings.calibration_cold_start_ratio)
            p_corr[active] = np.where(
                use_calib,
                f[active] * cr_act,
                f[active] * cold_start,
            )
        else:
            p_corr[active] = p_corr_legacy

        # ------- L4: Quantile loading -----------------------------------
        # target = interpolate(q_low, q50, q_high) at class_quantile.
        # q50 := P_corrected (the bias-corrected mean).
        layer_l4 = (self.settings.quantile_loading_enabled
                    and q_lows is not None and q_highs is not None)
        if layer_l4:
            class_q = self._quantiles_for(classes, n) if classes is not None else \
                np.full(n, float(self.settings.loading_quantile_default), dtype=float)
            q_lo = np.where(np.isfinite(q_lows), q_lows, np.nan)
            q_hi = np.where(np.isfinite(q_highs), q_highs, np.nan)
            target = p_corr.copy()
            usable = active & np.isfinite(q_lo) & np.isfinite(q_hi)
            if usable.any():
                # Force monotonic (q_lo <= p_corr <= q_hi). When the model
                # emits inverted bounds we clamp instead of trusting them.
                q_lo_u = np.minimum(q_lo[usable], p_corr[usable])
                q_hi_u = np.maximum(q_hi[usable], p_corr[usable])
                cq = class_q[usable]
                seg_target = p_corr[usable].copy()
                low_seg = cq < 0.5
                high_seg = cq >= 0.5
                if low_seg.any():
                    span_low = max(0.5 - float(q_low_pct), self._Q_INTERP_EPS)
                    seg_target[low_seg] = (
                        q_lo_u[low_seg]
                        + (cq[low_seg] - float(q_low_pct)) / span_low
                        * (p_corr[usable][low_seg] - q_lo_u[low_seg])
                    )
                if high_seg.any():
                    span_high = max(float(q_high_pct) - 0.5, self._Q_INTERP_EPS)
                    seg_target[high_seg] = (
                        p_corr[usable][high_seg]
                        + (cq[high_seg] - 0.5) / span_high
                        * (q_hi_u[high_seg] - p_corr[usable][high_seg])
                    )
                target[usable] = np.maximum(seg_target, 0.0)
            target_diag[:] = target
            quantile_diag[:] = class_q
        else:
            target = p_corr
            target_diag[:] = p_corr
            quantile_diag[active] = 0.5  # V5_b loads at the mean = q50

        # ------- L2: Leftover-aware fresh issuance + carry floor --------
        floor_scalar = float(self.carry_floor_pct) if use_carry_floor else 0.0
        floor = floor_scalar * target[active]
        candidate = target[active] - o[active]
        raw_load = np.maximum(floor, candidate)
        loads[active] = np.maximum(0.0, raw_load)

        # ------- Decision attribution -----------------------------------
        floor_active = floor >= candidate
        leftover_covers = (candidate <= 0) & (floor <= 0)
        active_idx = np.where(active)[0]
        local_decisions = np.where(
            leftover_covers, DECISION_LEFTOVER_COVERS,
            np.where(floor_active, DECISION_FLOOR_PROTECTED, DECISION_FRESH_LOAD),
        )
        decisions[active_idx] = local_decisions
        floor_applied[active_idx] = floor_active

        return (loads, p_corr, floor_applied, decisions,
                {"target_qty": target_diag, "loading_quantile": quantile_diag})

    # ------------------------------------------------------------------
    # Per-class lookups (settings-backed; no hardcoded numbers)
    # ------------------------------------------------------------------

    def _quantiles_for(self, classes: np.ndarray, n: int) -> np.ndarray:
        """Vectorised lookup of loading quantile per row."""
        out = np.full(n, float(self.settings.loading_quantile_default), dtype=float)
        for i in range(n):
            out[i] = float(self.settings.loading_quantile_for_class(classes[i]))
        return out

    # ------------------------------------------------------------------
    # vs-typical decision label
    # ------------------------------------------------------------------
    # Band thresholds are sourced from Settings so ops can widen / narrow
    # the SAME band via env var without code edits. Read on demand rather
    # than cached at construction so a settings reload (test override,
    # /reload endpoint) is picked up immediately.

    def _decide(self, load: float, typical: Optional[float]) -> Optional[str]:
        if typical is None:
            return None
        if typical == 0 and load == 0:
            return "skip"
        if typical == 0:
            return "MORE"
        if load == 0:
            return "LESS"
        ratio = load / typical
        if ratio < float(self.settings.typical_alloc_band_low):
            return "LESS"
        if ratio > float(self.settings.typical_alloc_band_high):
            return "MORE"
        return "SAME"
