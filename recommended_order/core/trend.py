"""Trend calculator: accelerating / declining purchase patterns."""

from __future__ import annotations

import logging
import numpy as np
import pandas as pd

from dataclasses import dataclass

from recommended_order.models.recommendation import TrendInfo

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _TrendThresholds:
    """Ratio boundaries for trend classification (math transform, not
    business thresholds; may move to calibration later)."""
    accelerating_fast: float = 0.8
    accelerating: float = 0.9
    stable_upper: float = 1.1
    declining: float = 1.2
    factor_accelerating_fast: float = 1.30
    factor_accelerating: float = 1.15
    factor_stable: float = 1.00
    factor_declining: float = 0.85
    factor_declining_fast: float = 0.70


class TrendCalculator:
    """Compares recent vs historical purchase cycle to detect trends."""

    def __init__(self) -> None:
        self._t = _TrendThresholds()

    # 2-gap signal at half strength; full strength activates from 4+ gaps.
    _LOW_CONFIDENCE_DAMPING: float = 0.5

    def calculate(
        self,
        item_history: pd.DataFrame,
        target_date: pd.Timestamp,
    ) -> TrendInfo:
        if item_history is None or item_history.empty:
            return TrendInfo(1.0, "NO_DATA")

        # TrxDate already datetime64 (normalised at load).
        dates = item_history["TrxDate"].sort_values().unique()
        if len(dates) < 3:
            return TrendInfo(1.0, "INSUFFICIENT_DATA", {"purchase_count": len(dates)})

        gaps = np.diff(dates).astype("timedelta64[D]").astype(int)
        n_gaps = len(gaps)
        if n_gaps < 2:
            return TrendInfo(1.0, "INSUFFICIENT_DATA")

        if n_gaps == 2:
            # Outlier removal meaningless on n=2; damp the factor instead.
            historical = float(gaps[0])
            recent = float(gaps[1])
            damping = self._LOW_CONFIDENCE_DAMPING
        else:
            clean = self._remove_outliers(gaps)
            if len(clean) < 3:
                return TrendInfo(1.0, "INSUFFICIENT_DATA", {"filtered_out": True})
            historical = float(np.median(clean))
            recent = float(np.median(clean[-min(3, len(clean)):]))
            damping = 1.0

        if historical == 0:
            return TrendInfo(1.0, "STABLE")

        ratio = recent / historical

        if ratio < self._t.accelerating_fast:
            factor, ttype = self._t.factor_accelerating_fast, "ACCELERATING_FAST"
        elif ratio < self._t.accelerating:
            factor, ttype = self._t.factor_accelerating, "ACCELERATING"
        elif ratio <= self._t.stable_upper:
            factor, ttype = self._t.factor_stable, "STABLE"
        elif ratio <= self._t.declining:
            factor, ttype = self._t.factor_declining, "DECLINING"
        else:
            factor, ttype = self._t.factor_declining_fast, "DECLINING_FAST"

        if damping < 1.0 and ttype != "STABLE":
            # Pull factor toward 1.0 by ``damping``.
            factor = 1.0 + damping * (factor - 1.0)
            ttype = f"{ttype}_LOW_CONF"

        meta = {
            "historical_cycle": int(historical),
            "recent_cycle": int(recent),
            "ratio": round(ratio, 3),
            "trend_factor": round(factor, 3),
            "n_gaps": n_gaps,
        }
        return TrendInfo(factor, ttype, meta)

    @staticmethod
    def _remove_outliers(gaps: np.ndarray) -> np.ndarray:
        if len(gaps) < 4:
            return gaps
        q1, q3 = float(np.percentile(gaps, 25)), float(np.percentile(gaps, 75))
        iqr = q3 - q1
        if iqr == 0:
            return gaps
        lo = max(1, q1 - 1.5 * iqr)
        hi = q3 + 1.5 * iqr
        clean = gaps[(gaps >= lo) & (gaps <= hi)]
        if len(clean) < len(gaps) * 0.4:
            return gaps
        return clean
