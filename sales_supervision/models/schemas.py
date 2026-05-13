"""
Domain models -- pure data containers for supervision state.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class SessionItem:
    item_code: str
    item_name: str
    recommended_qty: int
    actual_qty: int = 0
    adjustment: int = 0
    was_sold: bool = False
    was_edited: bool = False
    # Planning context snapshot
    tier: str = ""
    priority_score: float = 0.0
    days_since_last_purchase: int = 0
    purchase_cycle_days: float = 0.0
    frequency_percent: float = 0.0
    van_inventory_qty: int = 0
    # Original PascalCase rec from the recommended_order engine. Carried
    # verbatim so explainability fields (WhyItem, WhyQuantity, Confidence,
    # PurchaseCount, AvgQuantityPerVisit, Signals, Source, ...) survive
    # the round-trip into the live session and back out to the UI without
    # the dataclass having to enumerate every field.
    raw: Dict[str, Any] = field(default_factory=dict)

    @property
    def effective_recommended(self) -> int:
        return self.recommended_qty + self.adjustment

    def to_dict(self) -> Dict[str, Any]:
        # Canonical PascalCase shape. Order matters: canonical fields
        # derived from dataclass attrs come first, then ``self.raw`` is
        # spread so engine-supplied values override the defaults, and
        # supervision-runtime fields (Actual / Adjustment / ...) land
        # last so they always win. Earlier this method spread ``raw``
        # first without emitting the dataclass fields -- callers that
        # built a SessionItem with empty ``raw`` produced dicts with NO
        # ``ItemCode`` key, which collapsed every item to ``""`` at the
        # DB layer and tripped the ``uq_yf_si`` unique constraint.
        return {
            "ItemCode":              self.item_code,
            "ItemName":              self.item_name,
            "RecommendedQuantity":   self.recommended_qty,
            "Tier":                  self.tier,
            "PriorityScore":         self.priority_score,
            "DaysSinceLastPurchase": self.days_since_last_purchase,
            "PurchaseCycleDays":     self.purchase_cycle_days,
            "FrequencyPercent":      self.frequency_percent,
            "VanLoad":               self.van_inventory_qty,
            **self.raw,
            "ActualQuantity":       self.actual_qty,
            "Adjustment":           self.adjustment,
            "EffectiveRecommended": self.effective_recommended,
            "WasSold":              self.was_sold,
            "WasEdited":            self.was_edited,
        }


@dataclass
class ScoreResult:
    score: float = 0.0
    coverage: float = 0.0
    accuracy: float = 0.0


@dataclass
class SessionCustomer:
    customer_code: str
    customer_name: str = ""
    items: List[SessionItem] = field(default_factory=list)
    visited: bool = False
    visit_sequence: int = 0
    score: ScoreResult = field(default_factory=ScoreResult)
    llm_analysis: str = ""

    @property
    def total_recommended(self) -> int:
        return sum(it.effective_recommended for it in self.items)

    @property
    def total_actual(self) -> int:
        return sum(it.actual_qty for it in self.items)

    @property
    def items_sold(self) -> int:
        return sum(1 for it in self.items if it.was_sold)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "customerCode": self.customer_code,
            "customerName": self.customer_name,
            "visited": self.visited,
            "visitSequence": self.visit_sequence,
            "score": self.score.score,
            "coverage": self.score.coverage,
            "accuracy": self.score.accuracy,
            "totalRecommended": self.total_recommended,
            "totalActual": self.total_actual,
            "totalItems": len(self.items),
            "itemsSold": self.items_sold,
            "items": [it.to_dict() for it in self.items],
            "llmAnalysis": self.llm_analysis,
        }


@dataclass
class Session:
    session_id: str
    route_code: str
    date: str
    customers: Dict[str, SessionCustomer] = field(default_factory=dict)
    status: str = "active"  # active | closed

    @property
    def total_customers(self) -> int:
        return len(self.customers)

    @property
    def visited_customers(self) -> int:
        return sum(1 for c in self.customers.values() if c.visited)

    @property
    def total_recommended(self) -> int:
        return sum(c.total_recommended for c in self.customers.values())

    @property
    def total_actual(self) -> int:
        return sum(c.total_actual for c in self.customers.values() if c.visited)

    @property
    def visit_sequence_counter(self) -> int:
        return max((c.visit_sequence for c in self.customers.values() if c.visited), default=0)

    def summary(self) -> Dict[str, Any]:
        """Wire-shaped summary the live UI binds to.

        Carries everything the LiveSessionTab used to compute on the
        client: per-customer grouped items (with qty>0), per-customer
        tile stats, and both the static recommendation totals (planned)
        and the dynamic visit totals (live, including avg score).

        ``customers_grouped`` and ``customer_tiles`` are filtered to
        items with ``effective_recommended > 0`` and customers that
        survive that filter -- matches the legacy frontend rule. The
        unfiltered map is still available via ``to_dict()['customers']``
        for the visit handler to look customers up by code.
        """
        # Two visited cohorts -- planned (rep delivered against the
        # journey plan) and unplanned (drop-in invoices, all items
        # tier="UNPLANNED"). Route-header counters and the "Visited X/Y"
        # tile must compare like-with-like (planned-visited / planned),
        # so we compute them separately. ``visit_totals.visited_count``
        # follows the same rule -- planned-only -- because the UI's
        # denominator (recommendation_totals.customers_count) is also
        # planned-only.
        from sales_supervision.core.constants import is_unplanned_customer

        planned = [c for c in self.customers.values() if not is_unplanned_customer(c)]
        planned_visited = [c for c in planned if c.visited]
        unplanned_visited = [c for c in self.customers.values()
                              if is_unplanned_customer(c) and c.visited]

        visited_rec = sum(c.total_recommended for c in planned_visited)
        visited_act = sum(c.total_actual for c in planned_visited)
        avg_score = (
            round(sum(c.score.score for c in planned_visited) / len(planned_visited), 2)
            if planned_visited else None
        )

        # --- Pre-shaped customer payload (filter qty>0 items + drop empties)
        customers_grouped: List[Dict[str, Any]] = []
        customer_tiles: List[Dict[str, Any]] = []
        unique_items: set[str] = set()
        total_units = 0
        for c in self.customers.values():
            kept_items = [it for it in c.items if it.effective_recommended > 0]
            if not kept_items:
                continue
            tile_units = sum(it.effective_recommended for it in kept_items)
            tile_skus = len({it.item_code for it in kept_items})
            unique_items.update(it.item_code for it in kept_items)
            total_units += tile_units
            customer_tiles.append({
                "customer_code": c.customer_code,
                "customer_name": c.customer_name,
                "unique_skus": tile_skus,
                "total_units": tile_units,
                "visited": bool(c.visited),
            })
            customers_grouped.append({
                "customer_code": c.customer_code,
                "customer_name": c.customer_name,
                "items": [it.to_dict() for it in kept_items],
            })

        recommendation_totals = {
            "items_count": len(unique_items),
            "total_units": total_units,
            "customers_count": len(customers_grouped),
        }
        visit_totals = {
            "visited_count": len(planned_visited),
            "total_actual": visited_act,
            "total_recommended": visited_rec,
            "avg_score": avg_score,
            # Drop-in count surfaced separately so the UI / route header
            # can render it without re-walking the full session.
            "unplanned_visited_count": len(unplanned_visited),
        }

        return {
            "sessionId": self.session_id,
            "routeCode": self.route_code,
            "date": self.date,
            "status": self.status,
            # Planned-only headline counts. The route header writer
            # reads these so customers_planned / customers_visited stay
            # comparable across the day even as drop-ins land.
            "plannedCustomers": len(planned),
            "plannedVisitedCustomers": len(planned_visited),
            "unplannedVisitedCustomers": len(unplanned_visited),
            # Legacy session-wide totals (planned + unplanned). Kept so
            # any client that used to read these still works, but no
            # longer drives the route-header tile.
            "totalCustomers": self.total_customers,
            "visitedCustomers": self.visited_customers,
            "remainingCustomers": len(planned) - len(planned_visited),
            "totalRecommended": self.total_recommended,
            "totalActual": self.total_actual,
            "visitedRecommended": visited_rec,
            "visitedActual": visited_act,
            "visitedAchievement": round(visited_act / max(visited_rec, 1) * 100, 1),
            "overallAchievement": round(self.total_actual / max(self.total_recommended, 1) * 100, 1),
            # Pure-render payload for the live UI. Frontend never
            # aggregates the recommendations array itself.
            "customers_grouped": customers_grouped,
            "customer_tiles": customer_tiles,
            "recommendation_totals": recommendation_totals,
            "visit_totals": visit_totals,
        }

    def to_dict(self) -> Dict[str, Any]:
        return {
            **self.summary(),
            "customers": {k: v.to_dict() for k, v in self.customers.items()},
        }


@dataclass
class VisitResult:
    customer_code: str
    score: ScoreResult
    unsold_items: Dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "customerCode": self.customer_code,
            "score": {"score": self.score.score, "coverage": self.score.coverage, "accuracy": self.score.accuracy},
            "unsoldItems": self.unsold_items,
        }
