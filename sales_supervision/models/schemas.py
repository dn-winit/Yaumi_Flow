"""
Domain models -- pure data containers for supervision state.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


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
    # Original PascalCase rec from recommended_order; carries explainability fields verbatim.
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def effective_recommended(self) -> int:
        return self.recommended_qty + self.adjustment

    def to_dict(self) -> dict[str, Any]:
        # Canonical PascalCase; dataclass fields first, then raw, then supervision runtime fields (winners).
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

    def to_llm_payload(self) -> dict[str, Any]:
        """Canonical snake_case payload for llm_analytics.

        Single source of truth for the LLM-facing item shape -- both the
        pre-visit briefing fan-out and the post-visit customer-analysis fan-out
        call this helper. Carries every field the prompt YAMLs read:
        - typed fields (item_code, recommended_qty, tier, frequency_percent...)
        - explainability fields from the rec-engine (why_item, why_quantity,
          trend_factor, source) via ``self.raw`` lookup
        - runtime fields (actual_qty, was_sold) for post-visit context

        Adding a new field to the prompts means: add one lookup line here,
        and every consumer gets it -- no two-tier quality across callers.
        """
        def _from_raw(*keys: str, default: Any = None) -> Any:
            for k in keys:
                v = self.raw.get(k)
                if v is not None:
                    return v
            return default

        return {
            "item_code":                self.item_code,
            "item_name":                self.item_name,
            "recommended_qty":          int(self.recommended_qty),
            "actual_qty":               int(self.actual_qty),
            "tier":                     self.tier,
            "priority_score":           float(self.priority_score or 0.0),
            "purchase_cycle_days":      float(self.purchase_cycle_days or 0.0),
            "days_since_last_purchase": int(self.days_since_last_purchase or 0),
            "frequency_percent":        float(self.frequency_percent or 0.0),
            "was_sold":                 bool(self.was_sold),
            # Explainability fields -- carried verbatim from the rec engine.
            "trend_factor":  _from_raw("trend_factor", "TrendFactor", "trendFactor"),
            "why_item":      _from_raw("why_item", "WhyItem", "whyItem"),
            "why_quantity":  _from_raw("why_quantity", "WhyQuantity", "whyQuantity"),
            "source":        _from_raw("source", "Source"),
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
    items: list[SessionItem] = field(default_factory=list)
    visited: bool = False
    visit_sequence: int = 0
    score: ScoreResult = field(default_factory=ScoreResult)

    @property
    def total_recommended(self) -> int:
        return sum(it.effective_recommended for it in self.items)

    @property
    def total_actual(self) -> int:
        return sum(it.actual_qty for it in self.items)

    @property
    def items_sold(self) -> int:
        return sum(1 for it in self.items if it.was_sold)

    def to_dict(self) -> dict[str, Any]:
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
        }


@dataclass
class Session:
    session_id: str
    route_code: str
    date: str
    customers: dict[str, SessionCustomer] = field(default_factory=dict)
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

    def summary(self) -> dict[str, Any]:
        """Wire-shaped summary for the live UI.

        ``customers_grouped`` and ``customer_tiles`` filter to items with effective_recommended > 0;
        the unfiltered map is still available via to_dict()['customers'].
        """
        # Planned vs unplanned cohorts are tracked separately so "Visited X/Y" compares like-with-like.
        from sales_supervision.core.constants import is_unplanned_customer

        planned = [c for c in self.customers.values() if not is_unplanned_customer(c)]
        planned_visited = [c for c in planned if c.visited]
        unplanned_visited = [c for c in self.customers.values()
                              if is_unplanned_customer(c) and c.visited]

        # Shared aggregator so in-memory and DB-derived paths stay byte-identical.
        from sales_supervision.core.visit_totals import aggregate_visit_totals
        _aggregated = aggregate_visit_totals(
            planned_visited=[
                {
                    "total_actual": c.total_actual,
                    "total_recommended": c.total_recommended,
                    "score": c.score.score,
                }
                for c in planned_visited
            ],
            unplanned_visited_count=len(unplanned_visited),
        )
        visited_rec = _aggregated["total_recommended"]
        visited_act = _aggregated["total_actual"]
        _aggregated["avg_score"]

        # Pre-shaped customer payload: filter qty>0 items and drop empty customers.
        customers_grouped: list[dict[str, Any]] = []
        customer_tiles: list[dict[str, Any]] = []
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
        visit_totals = _aggregated

        return {
            "sessionId": self.session_id,
            "routeCode": self.route_code,
            "date": self.date,
            "status": self.status,
            # Planned-only headline counts; the route header writer reads these.
            "plannedCustomers": len(planned),
            "plannedVisitedCustomers": len(planned_visited),
            "unplannedVisitedCustomers": len(unplanned_visited),
            # Legacy session-wide totals retained for backward compat.
            "totalCustomers": self.total_customers,
            "visitedCustomers": self.visited_customers,
            "remainingCustomers": len(planned) - len(planned_visited),
            "totalRecommended": self.total_recommended,
            "totalActual": self.total_actual,
            "visitedRecommended": visited_rec,
            "visitedActual": visited_act,
            "visitedAchievement": round(visited_act / max(visited_rec, 1) * 100, 1),
            "overallAchievement": round(self.total_actual / max(self.total_recommended, 1) * 100, 1),
            # Pre-aggregated payload so the frontend never sums recommendations itself.
            "customers_grouped": customers_grouped,
            "customer_tiles": customer_tiles,
            "recommendation_totals": recommendation_totals,
            "visit_totals": visit_totals,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.summary(),
            "customers": {k: v.to_dict() for k, v in self.customers.items()},
        }


@dataclass
class VisitResult:
    customer_code: str
    score: ScoreResult
    unsold_items: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "customerCode": self.customer_code,
            "score": {"score": self.score.score, "coverage": self.score.coverage, "accuracy": self.score.accuracy},
            "unsoldItems": self.unsold_items,
        }
