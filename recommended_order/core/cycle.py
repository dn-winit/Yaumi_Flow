"""
Purchase cycle calculator.

Sprint-1 changes:
  * dropped the "business bucket" snap (7/14/21/28/30/60/90) -- it killed
    daily staples by rounding them up to a week.
  * exponential-decay recency weighting (half-life from calibration).
  * multi-pattern detection compares on raw gaps, not rounded gaps.
"""

from __future__ import annotations

import logging
from collections import Counter
from typing import List, Tuple

import numpy as np
import pandas as pd

from recommended_order.models.recommendation import CycleInfo

logger = logging.getLogger(__name__)


class CycleCalculator:
    """Calculates purchase cycles from raw gap data."""

    # --- minimums for structural decisions, not business thresholds ---
    _MIN_PATTERN_OCCURRENCES = 2
    _MIN_PATTERN_DIFF_DAYS = 7            # two patterns must differ by > 1 week
    _DEFAULT_DAYS_WHEN_UNKNOWN = 30       # fallback ONLY when we have no data

    def __init__(self, half_life_days: float) -> None:
        self._half_life = max(1.0, float(half_life_days))

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    def calculate(
        self,
        item_history: pd.DataFrame,
        target_date: pd.Timestamp,
    ) -> CycleInfo:
        """Return ``CycleInfo`` for a customer-item pair."""
        if item_history is None or item_history.empty:
            return CycleInfo(self._DEFAULT_DAYS_WHEN_UNKNOWN, 0.0, "insufficient")

        dates = pd.to_datetime(item_history["TrxDate"]).sort_values().unique()
        if len(dates) < 2:
            return CycleInfo(self._DEFAULT_DAYS_WHEN_UNKNOWN, 0.0, "insufficient")

        gaps = np.diff(dates).astype("timedelta64[D]").astype(int)
        gap_end_dates = dates[1:]  # each gap "ends" at the second date

        # Single gap -- no smoothing possible
        if len(gaps) == 1:
            return CycleInfo(max(1, int(gaps[0])), 0.3, "two_purchases")

        # Multi-pattern detection on RAW gaps
        patterns = self._detect_patterns(gaps)
        if len(patterns) > 1:
            dominant = max(patterns, key=lambda p: p["count"])
            return CycleInfo(
                max(1, int(round(dominant["cycle"]))),
                0.7,
                f"multi_pattern_{len(patterns)}",
            )

        # Exponential-decay weighted median (recency wins)
        cycle = self._weighted_cycle(gaps, gap_end_dates, target_date)
        confidence = self._confidence(gaps, len(dates))
        return CycleInfo(max(1, int(cycle)), confidence, "weighted_recent")

    def pattern_quality(self, item_history: pd.DataFrame) -> Tuple[float, str]:
        """Return (quality_score 0-1, pattern_type). CV-based."""
        if item_history is None or item_history.empty:
            return 0.0, "unknown"
        dates = pd.to_datetime(item_history["TrxDate"]).sort_values().unique()
        if len(dates) < 2:
            return 0.0, "unknown"
        gaps = np.diff(dates).astype("timedelta64[D]").astype(int)
        mean_gap = float(np.mean(gaps))
        if mean_gap == 0:
            return 0.0, "unknown"
        cv = float(np.std(gaps)) / mean_gap
        if cv <= 0.3:
            return 1.0, "consistent"
        if cv <= 0.7:
            return 0.7, "moderate"
        if cv <= 1.0:
            return 0.4, "erratic"
        return 0.1, "very_erratic"

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _detect_patterns(self, gaps: np.ndarray) -> List[dict]:
        """Find distinct modes in the raw gap distribution.

        We bucket gaps into groups of +/- 20% of each other to find modes
        without collapsing everything to 7/14/30.
        """
        if len(gaps) < 4:
            return []
        # Bucket each gap to the nearest 20% of its value -- preserves daily
        # (1, 2, 3) vs weekly (7) vs monthly (28-31) as distinct modes.
        buckets = [int(round(g / max(1, g * 0.2)) * max(1, g * 0.2)) for g in gaps]
        counts = Counter(buckets)
        patterns = [
            {"cycle": c, "count": n}
            for c, n in counts.items()
            if n >= self._MIN_PATTERN_OCCURRENCES
        ]
        if len(patterns) >= 2:
            cycles = [p["cycle"] for p in patterns]
            if max(cycles) - min(cycles) >= self._MIN_PATTERN_DIFF_DAYS:
                return patterns
        return []

    def _weighted_cycle(
        self,
        gaps: np.ndarray,
        gap_end_dates: np.ndarray,
        target_date: pd.Timestamp,
    ) -> int:
        """Exponential-decay weighted median of raw gaps.

        Weight of a gap ending ``age`` days before target = 2^(-age / half_life).
        """
        target = pd.Timestamp(target_date).to_datetime64()
        ages = (target - gap_end_dates) / np.timedelta64(1, "D")
        ages = ages.astype(float)
        ages = np.clip(ages, 0.0, None)
        weights = np.power(2.0, -ages / self._half_life)
        if weights.sum() <= 0:
            return int(round(float(np.median(gaps))))
        # Weighted median
        order = np.argsort(gaps)
        sorted_gaps = gaps[order]
        sorted_weights = weights[order]
        cum = np.cumsum(sorted_weights)
        cutoff = cum[-1] / 2.0
        idx = int(np.searchsorted(cum, cutoff))
        idx = min(idx, len(sorted_gaps) - 1)
        return int(round(float(sorted_gaps[idx])))

    @staticmethod
    def _confidence(gaps: np.ndarray, n_purchases: int) -> float:
        if n_purchases >= 10:
            ds = 1.0
        elif n_purchases >= 5:
            ds = 0.7
        elif n_purchases >= 3:
            ds = 0.5
        else:
            ds = 0.3
        if len(gaps) >= 2:
            mean_gap = float(np.mean(gaps))
            cv = float(np.std(gaps)) / mean_gap if mean_gap > 0 else 1.0
            cs = max(0.2, min(1.0, 1.0 - cv * 0.8))
        else:
            cs = 0.3
        return round(min(1.0, ds * 0.6 + cs * 0.4), 4)
