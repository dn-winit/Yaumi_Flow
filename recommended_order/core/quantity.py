"""Quantity calculator: recency-weighted avg * trend, clamped to the
supervision ``[qty_center_lo, qty_center_hi]`` perfect zone (the band
sales_supervision scores 100%). Emits a qty_derivation Signal."""

from __future__ import annotations

import numpy as np
import pandas as pd

from recommended_order.config.constants import SafetyClamps
from recommended_order.core.calibration import RouteCalibration
from recommended_order.core.explain import (
    KIND_QTY_DERIVATION,
    Explanation,
    Signal,
    detail_qty_recency,
)

# Shared definition of the accuracy "perfect zone" -- DO NOT redefine.
try:
    from sales_supervision.config.constants import AccuracyZone
    _ACCURACY_ZONE = AccuracyZone()
except Exception:  # pragma: no cover -- safety fallback for isolated boots
    _ACCURACY_ZONE = None


class QuantityCalculator:
    """Calculates recommended quantity using recency-weighted history."""

    def __init__(self, clamps: SafetyClamps) -> None:
        self._clamps = clamps

    def calculate(
        self,
        item_history: pd.DataFrame,
        target_date: pd.Timestamp,
        van_qty: int,
        trend_factor: float,
        calibration: RouteCalibration,
        explanation: Explanation,
        *,
        half_life_override: float | None = None,
    ) -> int:
        if item_history is None or item_history.empty:
            return 0

        qtys = pd.to_numeric(item_history["TotalQuantity"], errors="coerce").fillna(0).to_numpy()
        # TrxDate is already datetime64 from data.manager._normalize.
        dates = item_history["TrxDate"].to_numpy()
        if qtys.size == 0:
            return 0

        # Recency weights: exp-decay at customer half-life (override) else
        # route default.
        target = pd.Timestamp(target_date).to_datetime64()
        ages = ((target - dates) / np.timedelta64(1, "D")).astype(float)
        ages = np.clip(ages, 0.0, None)
        half = max(1.0, float(half_life_override if half_life_override is not None else calibration.recency_half_life_days))
        weights = np.power(2.0, -ages / half)
        wsum = weights.sum()

        raw_avg = float(np.mean(qtys))

        # Winsorise bulk-buy outliers (e.g. one qty=200 in an 8-unit SKU)
        # so they don't dominate the recency-weighted mean.
        qtys_for_avg = qtys
        if qtys.size >= 4:
            upper = float(
                np.percentile(qtys, self._clamps.outlier_winsor_percentile)
            )
            if upper > 0 and qtys.max() > upper:
                qtys_for_avg = np.minimum(qtys, upper)

        if wsum <= 0:
            weighted_avg = float(np.mean(qtys_for_avg))
        else:
            weighted_avg = float(np.sum(qtys_for_avg * weights) / wsum)

        # Expected actual purchase = recency-weighted avg x trend factor
        expected = max(0.0, weighted_avg * float(trend_factor))
        if expected <= 0:
            return 0

        # Center on perfect zone -- aim at midpoint of center band
        lo = self._clamps.qty_center_lo
        hi = self._clamps.qty_center_hi
        mid = (lo + hi) / 2.0
        proposed = expected * mid

        # Safety clamp to perfect-zone endpoints (never pitch outside)
        proposed = max(expected * lo, min(expected * hi, proposed))

        # Ceil to whole units to match the engine/van/supervision contract.
        qty = max(1, int(np.ceil(proposed)))
        qty = min(qty, int(van_qty))

        # Emit qty_derivation signal; ``half_life_source`` records whether
        # the route default or the customer override was applied.
        explanation.add_quantity_signal(Signal(
            kind=KIND_QTY_DERIVATION,
            detail=detail_qty_recency(raw_avg, weighted_avg, trend_factor, qty),
            weight=1.0,
            evidence={
                "raw_avg": round(raw_avg, 2),
                "recency_weighted_avg": round(weighted_avg, 2),
                "trend_factor": round(float(trend_factor), 3),
                "expected_actual": round(expected, 2),
                "perfect_zone": [lo, hi],
                "van_cap": int(van_qty),
                "recommended": qty,
                "half_life_days": round(half, 2),
                "half_life_source": (
                    "customer" if half_life_override is not None else "route"
                ),
            },
        ))
        return qty

    @staticmethod
    def perfect_zone() -> tuple[float, float]:
        """Expose the shared perfect-zone bounds for diagnostic use."""
        if _ACCURACY_ZONE is None:
            return (0.75, 1.20)
        return (_ACCURACY_ZONE.perfect_low, _ACCURACY_ZONE.perfect_high)
