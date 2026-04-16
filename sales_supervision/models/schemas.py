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

    @property
    def effective_recommended(self) -> int:
        return self.recommended_qty + self.adjustment

    def to_dict(self) -> Dict[str, Any]:
        return {
            "itemCode": self.item_code,
            "itemName": self.item_name,
            "recommendedQuantity": self.recommended_qty,
            "actualQuantity": self.actual_qty,
            "adjustment": self.adjustment,
            "effectiveRecommended": self.effective_recommended,
            "wasSold": self.was_sold,
            "wasEdited": self.was_edited,
            "tier": self.tier,
            "priorityScore": self.priority_score,
            "daysSinceLastPurchase": self.days_since_last_purchase,
            "purchaseCycleDays": self.purchase_cycle_days,
            "frequencyPercent": self.frequency_percent,
            "vanInventoryQty": self.van_inventory_qty,
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
        visited = [c for c in self.customers.values() if c.visited]
        visited_rec = sum(c.total_recommended for c in visited)
        visited_act = sum(c.total_actual for c in visited)
        return {
            "sessionId": self.session_id,
            "routeCode": self.route_code,
            "date": self.date,
            "status": self.status,
            "totalCustomers": self.total_customers,
            "visitedCustomers": self.visited_customers,
            "remainingCustomers": self.total_customers - self.visited_customers,
            "totalRecommended": self.total_recommended,
            "totalActual": self.total_actual,
            "visitedRecommended": visited_rec,
            "visitedActual": visited_act,
            "visitedAchievement": round(visited_act / max(visited_rec, 1) * 100, 1),
            "overallAchievement": round(self.total_actual / max(self.total_recommended, 1) * 100, 1),
        }

    def to_dict(self) -> Dict[str, Any]:
        return {
            **self.summary(),
            "customers": {k: v.to_dict() for k, v in self.customers.items()},
        }


@dataclass
class RedistributionEntry:
    from_customer: str
    to_customer: str
    item_code: str
    item_name: str
    quantity: int
    reason: str = ""


@dataclass
class VisitResult:
    customer_code: str
    score: ScoreResult
    unsold_items: Dict[str, int] = field(default_factory=dict)
    redistributions: List[RedistributionEntry] = field(default_factory=list)
    adjustments: Dict[str, Dict[str, int]] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "customerCode": self.customer_code,
            "score": {"score": self.score.score, "coverage": self.score.coverage, "accuracy": self.score.accuracy},
            "unsoldItems": self.unsold_items,
            "redistributions": [
                {"from": r.from_customer, "to": r.to_customer,
                 "itemCode": r.item_code, "quantity": r.quantity}
                for r in self.redistributions
            ],
            "adjustments": self.adjustments,
        }


@dataclass
class RouteScoreResult:
    route_score: float = 0.0
    customer_coverage: float = 0.0
    qty_fulfillment: float = 0.0
    customer_scores: Dict[str, float] = field(default_factory=dict)
