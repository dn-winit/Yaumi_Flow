"""
Domain models for the recommendation engine.
Pure data containers -- no business logic, no I/O.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class CycleInfo:
    cycle_days: int
    confidence: float
    method: str


@dataclass
class TrendInfo:
    factor: float
    trend_type: str
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class PriorityResult:
    score: float
    timing: float
    quantity: float
    consistency: float


@dataclass
class Candidate:
    """A single recommendation candidate produced by one generator lane.

    The engine de-dupes and merges candidates by ``item_code`` before the
    final van-load constraint pass.
    """

    item_code: str
    recommended_qty: int
    priority_score: float
    source: str                                  # history|peer|basket|reactivation|seed
    van_qty: int
    # Metrics that end up on the row
    avg_qty: float = 0.0
    days_since: int = 0
    cycle_days: float = 0.0
    frequency_pct: float = 0.0
    pattern_quality: float = 0.0
    purchase_count: int = 0
    trend_factor: float = 1.0
    churn_probability: float = 0.0
    # Explainability (Explanation is not serialisable -- we carry its outputs)
    signals: List[Dict[str, Any]] = field(default_factory=list)
    why_item: str = ""
    why_quantity: str = ""
    confidence: float = 0.0


@dataclass
class Recommendation:
    trx_date: str
    route_code: str
    customer_code: str
    customer_name: str
    item_code: str
    item_name: str
    recommended_quantity: int
    tier: str
    van_load: int
    priority_score: float
    avg_quantity_per_visit: int
    days_since_last_purchase: int
    purchase_cycle_days: float
    frequency_percent: float
    churn_probability: float
    pattern_quality: float
    purchase_count: int
    trend_factor: float
    # Sprint-1 explainability fields
    signals: List[Dict[str, Any]] = field(default_factory=list)
    why_item: str = ""
    why_quantity: str = ""
    confidence: float = 0.0
    candidate_source: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "TrxDate": self.trx_date,
            "RouteCode": self.route_code,
            "CustomerCode": self.customer_code,
            "CustomerName": self.customer_name,
            "ItemCode": self.item_code,
            "ItemName": self.item_name,
            "RecommendedQuantity": self.recommended_quantity,
            "Tier": self.tier,
            "VanLoad": self.van_load,
            "PriorityScore": self.priority_score,
            "AvgQuantityPerVisit": self.avg_quantity_per_visit,
            "DaysSinceLastPurchase": self.days_since_last_purchase,
            "PurchaseCycleDays": self.purchase_cycle_days,
            "FrequencyPercent": self.frequency_percent,
            "ChurnProbability": self.churn_probability,
            "PatternQuality": self.pattern_quality,
            "PurchaseCount": self.purchase_count,
            "TrendFactor": self.trend_factor,
            # New Sprint-1 columns. Signals is a JSON string for CSV portability.
            "Signals": json.dumps(self.signals, ensure_ascii=False),
            "WhyItem": self.why_item,
            "WhyQuantity": self.why_quantity,
            "Confidence": round(float(self.confidence), 4),
            "Source": self.candidate_source,
        }
