"""
Visit processor -- handles a single customer visit (scoring + redistribution).
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from sales_supervision.config.constants import SupervisionConstants
from sales_supervision.core.redistribution import RedistributionEngine
from sales_supervision.core.scoring import ScoringEngine
from sales_supervision.models.schemas import Session, VisitResult

logger = logging.getLogger(__name__)


class VisitProcessor:
    """Processes a customer visit: apply actuals, score, redistribute unsold."""

    def __init__(self, constants: Optional[SupervisionConstants] = None) -> None:
        c = constants or SupervisionConstants()
        self._scorer = ScoringEngine(c)
        self._redistributor = RedistributionEngine(c)

    def process(
        self,
        session: Session,
        customer_code: str,
        actual_sales: Dict[str, int],
    ) -> VisitResult:
        """
        Process a visit:
        1. Apply actual quantities
        2. Score the customer
        3. Redistribute unsold items
        """
        customer = session.customers.get(customer_code)
        if customer is None:
            raise ValueError(f"Customer {customer_code} not in session")

        # Mark visited
        if not customer.visited:
            customer.visited = True
            customer.visit_sequence = session.visit_sequence_counter + 1

        # Apply actual quantities
        unsold: Dict[str, int] = {}
        for item in customer.items:
            qty = actual_sales.get(item.item_code, 0)
            item.actual_qty = qty
            item.was_sold = qty > 0
            if item.item_code in actual_sales:
                item.was_edited = True

            # Calculate unsold for redistribution
            diff = item.effective_recommended - qty
            if diff > 0:
                unsold[item.item_code] = diff

        # Score
        score = self._scorer.customer_score(customer)
        customer.score = score

        # Redistribute unsold items to unvisited customers
        redistributions = self._redistributor.redistribute(session, customer_code, unsold)

        # Build adjustments map {to_customer: {item_code: qty}}
        adjustments: Dict[str, Dict[str, int]] = {}
        for r in redistributions:
            adjustments.setdefault(r.to_customer, {})[r.item_code] = (
                adjustments.get(r.to_customer, {}).get(r.item_code, 0) + r.quantity
            )

        return VisitResult(
            customer_code=customer_code,
            score=score,
            unsold_items=unsold,
            redistributions=redistributions,
            adjustments=adjustments,
        )
