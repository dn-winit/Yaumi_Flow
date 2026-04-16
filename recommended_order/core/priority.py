"""
Priority calculator.

Sprint-1 changes:
  * Adaptive weights blended by item frequency (low-freq items don't let
    timing alone dominate; high-freq items do).
  * Smooth timing decay (gaussian about on-time) -- no 1.0 plateau, so
    yearly items don't beat weekly staples just by being more overdue.
  * Every scoring step appends a Signal to the passed-in Explanation.
"""

from __future__ import annotations

import logging
import math

import numpy as np
import pandas as pd

from recommended_order.core.calibration import RouteCalibration
from recommended_order.core.explain import (
    Explanation,
    KIND_CONSISTENT,
    KIND_DUE_NOW,
    KIND_OVERDUE,
    KIND_REGULAR_BUYER,
    Signal,
    detail_consistent_pattern,
    detail_due_now,
    detail_overdue,
    detail_regular_buyer,
)
from recommended_order.models.recommendation import PriorityResult

logger = logging.getLogger(__name__)


class PriorityCalculator:
    """Calculates explainable priority scores for customer-item pairs."""

    def calculate(
        self,
        item_history: pd.DataFrame,
        cust_history: pd.DataFrame,
        target_date: pd.Timestamp,
        *,
        cycle_days: int,
        days_since: int,
        item_frequency: float,
        calibration: RouteCalibration,
        explanation: Explanation,
    ) -> PriorityResult:
        if item_history is None or item_history.empty or cycle_days <= 0:
            return PriorityResult(0.0, 0.0, 0.0, 0.0)

        timing = self._timing_score(days_since, cycle_days)
        quantity = self._quantity_score(item_history, calibration)
        consistency = self._consistency_score(item_history)

        weights = self._blend_weights(item_frequency, calibration)

        raw = (
            weights["timing"] * timing
            + weights["quantity"] * quantity
            + weights["consistency"] * consistency
        )
        score = round(max(0.0, min(100.0, raw * 100)), 2)

        # ---- emit signals ----
        total_visits = int(cust_history["TrxDate"].nunique()) if not cust_history.empty else 0
        item_visits = int(item_history["TrxDate"].nunique())
        if total_visits > 0 and item_visits / total_visits >= calibration.frequency_floor:
            explanation.add_item_signal(Signal(
                kind=KIND_REGULAR_BUYER,
                detail=detail_regular_buyer(item_visits, total_visits),
                weight=min(1.0, item_visits / total_visits + 0.1),
                evidence={"item_visits": item_visits, "total_visits": total_visits},
            ))

        cycles_missed = (days_since / cycle_days) - 1.0
        if cycles_missed >= 1.0:
            explanation.add_item_signal(Signal(
                kind=KIND_OVERDUE,
                detail=detail_overdue(cycles_missed + 1.0, days_since),
                weight=min(1.0, 0.4 + 0.2 * cycles_missed),
                evidence={"cycles_missed": round(cycles_missed, 2), "days_since": days_since},
            ))
        elif timing >= 0.6:
            explanation.add_item_signal(Signal(
                kind=KIND_DUE_NOW,
                detail=detail_due_now(days_since, cycle_days),
                weight=timing,
                evidence={"timing": round(timing, 3), "cycle_days": cycle_days},
            ))

        if consistency >= 0.75:
            # CV derived from consistency: consistency = 1 - cv*0.7 (see below)
            cv = max(0.0, (1.0 - consistency) / 0.7)
            explanation.add_item_signal(Signal(
                kind=KIND_CONSISTENT,
                detail=detail_consistent_pattern(cv),
                weight=0.4 * consistency,
                evidence={"cv": round(cv, 3), "consistency": round(consistency, 3)},
            ))

        return PriorityResult(
            score=score,
            timing=round(timing, 4),
            quantity=round(quantity, 4),
            consistency=round(consistency, 4),
        )

    # ------------------------------------------------------------------
    # Component scores (0-1)
    # ------------------------------------------------------------------

    def _timing_score(self, days_since: int, cycle_days: int) -> float:
        """Gaussian around on-time (cycles_missed ~= 1 == "exactly due").

        score = exp(-(cycles - 1)^2 / 2), clipped to [0, 1].

        This makes on-time score highest (1.0), overdue decay smoothly,
        and early-stage (0.5 cycles) also score noticeably.
        """
        if cycle_days <= 0 or days_since < 0:
            return 0.0
        cycles = days_since / cycle_days
        score = math.exp(-((cycles - 1.0) ** 2) / 2.0)
        return max(0.0, min(1.0, score))

    @staticmethod
    def _quantity_score(item_history: pd.DataFrame, calibration: RouteCalibration) -> float:
        avg_qty = float(item_history["TotalQuantity"].mean())
        if avg_qty <= 0:
            return 0.0
        benchmark = max(1.0, calibration.qty_benchmark)
        return min(1.0, float(np.log1p(avg_qty) / np.log1p(benchmark)))

    @staticmethod
    def _consistency_score(item_history: pd.DataFrame) -> float:
        if item_history.empty or len(item_history) < 2:
            return 0.3
        dates = pd.to_datetime(item_history["TrxDate"]).unique()
        if len(dates) < 2:
            return 0.3
        intervals = np.diff(np.sort(dates)).astype("timedelta64[D]").astype(float)
        if len(intervals) == 0:
            return 0.3
        mean_iv = float(np.mean(intervals))
        if mean_iv <= 0:
            return 0.3
        cv = float(np.std(intervals)) / mean_iv
        consistency = max(0.3, 1.0 - cv * 0.7)
        data_boost = min(0.2, len(intervals) * 0.02)
        return min(1.0, consistency + data_boost)

    @staticmethod
    def _blend_weights(item_frequency: float, calibration: RouteCalibration) -> dict:
        """Linear blend of low/high-freq weight triples based on frequency.

        Frequency at or below floor -> low-freq weights.
        Frequency at or above 0.5 -> high-freq weights.
        """
        floor = calibration.frequency_floor
        ceiling = max(floor + 1e-6, 0.5)
        t = max(0.0, min(1.0, (item_frequency - floor) / (ceiling - floor)))
        lo = calibration.priority_weights_low_freq
        hi = calibration.priority_weights_high_freq
        return {k: lo[k] + t * (hi[k] - lo[k]) for k in ("timing", "quantity", "consistency")}
