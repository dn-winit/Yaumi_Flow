"""
Redistribution engine -- reallocates unsold items to unvisited customers.
"""

from __future__ import annotations

import logging
from typing import Dict, List, Optional

from sales_supervision.config.constants import SupervisionConstants
from sales_supervision.models.schemas import (
    RedistributionEntry,
    Session,
    SessionCustomer,
)

logger = logging.getLogger(__name__)


class RedistributionEngine:
    """Redistributes unsold items to eligible unvisited customers."""

    def __init__(self, constants: Optional[SupervisionConstants] = None) -> None:
        self._c = (constants or SupervisionConstants()).redistribution

    def redistribute(
        self,
        session: Session,
        from_customer: str,
        unsold_items: Dict[str, int],
    ) -> List[RedistributionEntry]:
        """
        Distribute unsold items from a visited customer to unvisited ones.

        Rules:
        - Only unvisited customers who have the same item in their recommendations
        - Sorted by tier priority (MUST_STOCK first)
        - Max N recipients per item
        - Max 50% increase per customer-item
        """
        if not unsold_items:
            return []

        unvisited = {
            code: cust for code, cust in session.customers.items()
            if not cust.visited
        }
        if not unvisited:
            return []

        entries: List[RedistributionEntry] = []

        for item_code, unsold_qty in unsold_items.items():
            if unsold_qty <= 0:
                continue

            # Find eligible recipients: unvisited customers who have this item
            eligible = self._find_eligible(unvisited, item_code)
            if not eligible:
                continue

            remaining = unsold_qty
            for cust_code, cust, item in eligible[: self._c.max_recipients]:
                if remaining <= 0:
                    break

                max_add = max(1, int(item.recommended_qty * self._c.max_increase_pct))
                give = min(remaining, max_add)

                item.adjustment += give
                remaining -= give

                entries.append(RedistributionEntry(
                    from_customer=from_customer,
                    to_customer=cust_code,
                    item_code=item_code,
                    item_name=item.item_name,
                    quantity=give,
                    reason=f"Unsold by {from_customer}, tier={item.tier}",
                ))

        if entries:
            logger.info(
                "Redistributed %d entries from %s across %d items",
                len(entries), from_customer, len(unsold_items),
            )

        return entries

    def _find_eligible(self, unvisited: Dict[str, SessionCustomer], item_code: str):
        """Find unvisited customers with this item, sorted by tier priority."""
        tier_order = {t: i for i, t in enumerate(self._c.tier_priority)}
        candidates = []

        for code, cust in unvisited.items():
            for item in cust.items:
                if item.item_code == item_code:
                    candidates.append((code, cust, item))
                    break

        # Sort by tier priority (lower index = higher priority), then by priority score desc
        candidates.sort(key=lambda x: (tier_order.get(x[2].tier, 99), -x[2].priority_score))
        return candidates
