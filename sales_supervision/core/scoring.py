"""
Scoring engine -- item accuracy, customer score.

Item Accuracy: perfect zone 75-120%, linear decay outside.
Customer Score: (coverage x 0.4) + (accuracy x 0.6)
"""

from __future__ import annotations

from typing import Optional

from sales_supervision.config.constants import SupervisionConstants
from sales_supervision.models.schemas import ScoreResult, SessionCustomer


class ScoringEngine:
    """Stateless scoring calculator."""

    def __init__(self, constants: Optional[SupervisionConstants] = None) -> None:
        self._c = constants or SupervisionConstants()
        self._az = self._c.accuracy
        self._sw = self._c.scoring

    # ------------------------------------------------------------------
    # Item-level accuracy (0-100)
    # ------------------------------------------------------------------

    def item_accuracy(self, actual: int, recommended: int) -> float:
        if recommended <= 0:
            return 100.0 if actual == 0 else 0.0
        if actual == 0:
            return 0.0

        ratio = actual / recommended

        # Perfect zone
        if self._az.perfect_low <= ratio <= self._az.perfect_high:
            return 100.0

        # Below sweet spot: linear from 0% -> 100% as ratio goes 0 -> perfect_low
        if ratio < self._az.perfect_low:
            return round((ratio / self._az.perfect_low) * 100, 1)

        # Above sweet spot: linear from 100% -> 0% as ratio goes perfect_high -> max_over
        if ratio <= self._az.max_over:
            overshoot_range = self._az.max_over - self._az.perfect_high
            over = ratio - self._az.perfect_high
            return round(max(0.0, 100.0 - (over / overshoot_range) * 100), 1)

        # Way over
        return 0.0

    # ------------------------------------------------------------------
    # Customer-level score
    # ------------------------------------------------------------------

    def customer_score(self, customer: SessionCustomer) -> ScoreResult:
        total_items = len(customer.items)
        if total_items == 0:
            return ScoreResult()

        items_sold = sum(1 for it in customer.items if it.was_sold)
        coverage = round(items_sold / total_items * 100, 1)

        accuracies = [
            self.item_accuracy(it.actual_qty, it.effective_recommended)
            for it in customer.items
        ]
        avg_accuracy = round(sum(accuracies) / len(accuracies), 1) if accuracies else 0.0

        score = round(
            self._sw.coverage * coverage + self._sw.accuracy * avg_accuracy, 1
        )

        return ScoreResult(score=score, coverage=coverage, accuracy=avg_accuracy)
