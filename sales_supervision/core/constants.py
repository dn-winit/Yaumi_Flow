"""
Shared supervision-domain literals + predicates.

Centralises the ``UNPLANNED`` tier marker and the drop-in classifier so
the redistribution shaper, the session summary, the DB saver, and the
auto-visit reconciler agree on one rule -- a customer is a drop-in if
they carry at least one item AND every item has ``tier == UNPLANNED``.
A customer with zero items is treated as planned (briefing-only stub).
"""

from __future__ import annotations

from sales_supervision.models.schemas import SessionCustomer

TIER_UNPLANNED = "UNPLANNED"


def is_unplanned_customer(cust: SessionCustomer) -> bool:
    """Drop-in predicate: items non-empty AND every item is UNPLANNED."""
    return bool(cust.items) and all(it.tier == TIER_UNPLANNED for it in cust.items)
