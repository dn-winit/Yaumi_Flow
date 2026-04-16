"""
Explainability primitives.

Single source of truth for every plain-English string the recommendation
engine emits. Scoring / quantity / generator logic *adds* Signals here;
no other module constructs the sentences. This keeps the UI narrative
consistent and makes A/B copy changes trivial.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List


# Canonical signal kinds. Keep these short, lower-snake -- the UI keys off them.
KIND_REGULAR_BUYER = "regular_buyer"
KIND_DUE_NOW = "due_now"
KIND_OVERDUE = "overdue"
KIND_TRENDING_UP = "trending_up"
KIND_TRENDING_DOWN = "trending_down"
KIND_LOOKALIKE_PEER = "lookalike_peer"
KIND_BASKET_COMPLEMENT = "basket_complement"
KIND_REACTIVATION = "reactivation"
KIND_FIRST_VISIT = "first_visit"
KIND_CONSISTENT = "consistent_pattern"
KIND_QTY_DERIVATION = "qty_derivation"
KIND_FEEDBACK_ADJUSTED = "feedback_adjusted"


@dataclass
class Signal:
    """One reason contributing to a recommendation.

    Attributes:
        kind:      canonical tag, e.g. ``"regular_buyer"`` -- UI keys off this.
        detail:    plain-English sentence rendered verbatim in the card.
        weight:    relative contribution in [0, 1]; Explanation renormalises
                   so weights sum to 1 across the row.
        evidence:  structured numbers (counts, ratios) the UI may surface.
    """

    kind: str
    detail: str
    weight: float
    evidence: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "kind": self.kind,
            "detail": self.detail,
            "weight": round(float(self.weight), 4),
            "evidence": self.evidence,
        }


# ---------------------------------------------------------------------------
# Detail-sentence factories (ONLY place these strings live)
# ---------------------------------------------------------------------------

def detail_regular_buyer(item_visits: int, total_visits: int) -> str:
    return f"Bought on {item_visits} of last {total_visits} visits."


def detail_due_now(days_since: int, cycle_days: int) -> str:
    return f"Last bought {days_since}d ago; typical cycle is {cycle_days}d."


def detail_overdue(cycles_missed: float, days_since: int) -> str:
    return f"{cycles_missed:.1f} cycles overdue ({days_since}d since last buy)."


def detail_trending_up(old_cycle: int, new_cycle: int) -> str:
    return f"Buying faster lately -- cycle shrank from {old_cycle}d to {new_cycle}d."


def detail_trending_down(old_cycle: int, new_cycle: int) -> str:
    return f"Buying slower lately -- cycle grew from {old_cycle}d to {new_cycle}d."


def detail_lookalike_peer(score_pct: float, n_peers: int) -> str:
    return (
        f"Customers with similar baskets to yours buy this item "
        f"(similarity-weighted score {score_pct:.0f}%, {n_peers} peers)."
    )


def detail_basket_complement(anchor_item: str, confidence: float) -> str:
    return f"Often bought with {anchor_item} (co-purchase rate {confidence:.0%})."


def detail_reactivation(days_since_any: int) -> str:
    return f"No visits in {days_since_any}d -- offering top route items to restart the relationship."


def detail_first_visit() -> str:
    return "First-time customer on this route -- seeding popular van items."


def detail_consistent_pattern(cv: float) -> str:
    return f"Very regular buying pattern (variability {cv:.0%})."


def detail_qty_recency(avg_qty: float, recent_weighted: float, trend_factor: float, capped_to: int) -> str:
    return (
        f"Recency-weighted avg {recent_weighted:.1f} (raw avg {avg_qty:.1f}) "
        f"x trend {trend_factor:.2f} -> {capped_to} (clamped to perfect zone)."
    )


def detail_qty_seed(qty: int) -> str:
    return f"Fixed seed qty of {qty} for a first-time customer -- low-risk starter."


def detail_qty_peer(median_peer_qty: float, n_peers: int) -> str:
    return (
        f"Similarity-weighted median qty across {n_peers} lookalike peers is "
        f"{median_peer_qty:.0f} units."
    )


def detail_qty_basket(median_qty: float) -> str:
    return f"Median co-purchased quantity is {median_qty:.0f} units."


def detail_feedback_adjusted(multiplier: float, source: str, n_samples: int) -> str:
    """Sprint-4: plain-language annotation when a source's score was reweighted
    based on observed driver outcomes for this route."""
    pct = (multiplier - 1.0) * 100.0
    direction = "Boosted" if pct >= 0 else "Dampened"
    return (
        f"{direction} by {abs(pct):.0f}% based on driver outcomes "
        f"({source} lane, {n_samples} attributed recs on this route)."
    )


# ---------------------------------------------------------------------------
# Explanation accumulator
# ---------------------------------------------------------------------------

@dataclass
class Explanation:
    """Accumulates signals for one candidate row.

    Separate ``item_signals`` (why we're recommending the item at all)
    from ``quantity_signals`` (how we sized the qty). The UI renders
    them in two sections.
    """

    item_signals: List[Signal] = field(default_factory=list)
    quantity_signals: List[Signal] = field(default_factory=list)

    # ---- building ----
    def add_item_signal(self, signal: Signal) -> None:
        self.item_signals.append(signal)

    def add_quantity_signal(self, signal: Signal) -> None:
        self.quantity_signals.append(signal)

    # ---- merging (used when two generators pick the same item) ----
    def merge(self, other: "Explanation") -> None:
        self.item_signals.extend(other.item_signals)
        self.quantity_signals.extend(other.quantity_signals)

    # ---- outputs ----
    def _normalised(self, signals: List[Signal]) -> List[Signal]:
        total = sum(max(0.0, s.weight) for s in signals)
        if total <= 0:
            return signals
        return [
            Signal(s.kind, s.detail, s.weight / total, s.evidence) for s in signals
        ]

    def signals(self) -> List[Dict[str, Any]]:
        """All signals (item + quantity), normalised so weights sum to 1."""
        all_sig = self.item_signals + self.quantity_signals
        return [s.to_dict() for s in self._normalised(all_sig)]

    def why_item(self) -> str:
        """One-sentence headline from the top item signals."""
        if not self.item_signals:
            return ""
        ranked = sorted(self.item_signals, key=lambda s: -s.weight)
        # Take up to 2 top signals, join with " "
        return " ".join(s.detail for s in ranked[:2])

    def why_quantity(self) -> str:
        """One-sentence description of how the qty was derived."""
        if not self.quantity_signals:
            return ""
        ranked = sorted(self.quantity_signals, key=lambda s: -s.weight)
        return ranked[0].detail

    def confidence(self) -> float:
        """Weighted average signal strength in [0, 1].

        Higher when we have multiple corroborating signals. Falls back to the
        max single-signal weight when only one signal fired.
        """
        all_sig = self.item_signals + self.quantity_signals
        if not all_sig:
            return 0.0
        weights = [max(0.0, s.weight) for s in all_sig]
        total = sum(weights)
        if total <= 0:
            return 0.0
        # Boost for corroboration: sqrt(N) up to 2 signals capped
        boost = min(1.0, (len(all_sig) / 3.0) ** 0.5)
        avg = total / len(all_sig)
        return round(min(1.0, avg * (0.7 + 0.3 * boost)), 4)
