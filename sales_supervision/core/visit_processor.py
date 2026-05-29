"""
Visit processor -- apply actuals + score for a single customer visit.

Redistribution view is computed at request time by ``shape_redistribution_view``
and never mutates session state.
"""

from __future__ import annotations

import logging

from sales_supervision.config.constants import SupervisionConstants
from sales_supervision.core.scoring import ScoringEngine
from sales_supervision.models.schemas import Session, VisitResult

logger = logging.getLogger(__name__)


class VisitProcessor:
    """Processes a customer visit: apply actuals + score."""

    def __init__(self, constants: SupervisionConstants | None = None) -> None:
        c = constants or SupervisionConstants()
        self._scorer = ScoringEngine(c)

    def process(
        self,
        session: Session,
        customer_code: str,
        actual_sales: dict[str, int],
    ) -> VisitResult:
        """
        Process a visit:
        1. Mark visited + stamp visit_sequence.
        2. Apply actual quantities to each planned item.
        3. Compute the customer score (coverage + accuracy).
        """
        customer = session.customers.get(customer_code)
        if customer is None:
            raise ValueError(f"Customer {customer_code} not in session")

        # Mark visited
        if not customer.visited:
            customer.visited = True
            customer.visit_sequence = session.visit_sequence_counter + 1

        # Actuals come from YaumiLive via data_import; was_edited stays default (no manual override).
        unsold: dict[str, int] = {}
        for item in customer.items:
            qty = actual_sales.get(item.item_code, 0)
            item.actual_qty = qty
            item.was_sold = qty > 0

            # Surface the per-item shortfall (rec > actual) for diagnostics.
            diff = item.effective_recommended - qty
            if diff > 0:
                unsold[item.item_code] = diff

        # Score
        score = self._scorer.customer_score(customer)
        customer.score = score

        return VisitResult(
            customer_code=customer_code,
            score=score,
            unsold_items=unsold,
        )
