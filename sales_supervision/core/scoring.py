"""
Scoring engine -- item accuracy, customer score, route score.

Item Accuracy: perfect zone 75-120%, linear decay outside.
Customer Score: (coverage x 0.4) + (accuracy x 0.6)
Route Score: average of visited customer scores.
"""

from __future__ import annotations

from typing import List, Optional

from sales_supervision.config.constants import SupervisionConstants
from sales_supervision.models.schemas import (
    RouteScoreResult,
    ScoreResult,
    SessionCustomer,
    SessionItem,
)


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

    # ------------------------------------------------------------------
    # Route-level score
    # ------------------------------------------------------------------

    def route_score(self, customers: List[SessionCustomer]) -> RouteScoreResult:
        visited = [c for c in customers if c.visited]
        if not visited:
            return RouteScoreResult()

        customer_scores = {c.customer_code: c.score.score for c in visited}
        avg_score = round(sum(customer_scores.values()) / len(customer_scores), 1)

        total_planned = len(customers)
        cust_coverage = round(len(visited) / max(total_planned, 1) * 100, 1)

        total_rec = sum(c.total_recommended for c in visited)
        total_act = sum(c.total_actual for c in visited)
        qty_fulfillment = round(total_act / max(total_rec, 1) * 100, 1)

        return RouteScoreResult(
            route_score=avg_score,
            customer_coverage=cust_coverage,
            qty_fulfillment=qty_fulfillment,
            customer_scores=customer_scores,
        )
