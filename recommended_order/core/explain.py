"""Explainability primitives. Single source for every plain-English
string the engine emits -- generators / scoring add Signals; no other
module constructs sentences."""

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
    """One reason contributing to a recommendation. ``kind`` is the UI key,
    ``detail`` is the verbatim sentence, ``weight`` is renormalised by
    Explanation, ``evidence`` carries structured numbers for auditors."""

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


# Detail-sentence factories (the only place these strings live).
# Numeric evidence rides on Signal.evidence; the user-facing string is headline only.

def detail_regular_buyer(item_visits: int, total_visits: int) -> str:
    return f"Bought on {item_visits} of the last {total_visits} visits."


def detail_due_now(days_since: int, cycle_days: int) -> str:
    return (
        f"Last bought {days_since} days ago "
        f"— usually buys this every {cycle_days} days."
    )


def detail_overdue(cycles_missed: float, days_since: int) -> str:
    # cycles_missed kept on evidence only.
    _ = cycles_missed
    return (
        f"Overdue — last bought {days_since} days ago, "
        f"longer than the usual gap."
    )


def detail_trending_up(old_cycle: int, new_cycle: int) -> str:
    return (
        f"Buying more often lately — used to buy every {old_cycle} days, "
        f"now every {new_cycle} days."
    )


def detail_trending_down(old_cycle: int, new_cycle: int) -> str:
    return (
        f"Buying less often lately — used to buy every {old_cycle} days, "
        f"now every {new_cycle} days."
    )


def detail_lookalike_peer(score_pct: float, n_peers: int) -> str:
    # Score kept on evidence; n_peers carries the user-facing story.
    _ = score_pct
    return (
        f"{n_peers} customers with similar shopping patterns "
        f"regularly buy this item."
    )


def detail_basket_complement(anchor_item: str, confidence: float) -> str:
    return (
        f"Often bought together with {anchor_item} "
        f"— {confidence:.0%} of the time."
    )


def detail_reactivation(days_since_any: int) -> str:
    return (
        f"No purchases in {days_since_any} days "
        f"— suggesting a popular item to bring them back."
    )


def detail_first_visit() -> str:
    return (
        "First-time customer on this route — "
        "suggesting items that sell well to others here."
    )


def detail_consistent_pattern(cv: float) -> str:
    # cv kept on evidence only.
    _ = cv
    return "Very regular buying pattern — quantities stay close to the average."


def detail_qty_recency(avg_qty: float, recent_weighted: float, trend_factor: float, capped_to: int) -> str:
    # raw avg + trend kept on evidence only.
    _ = avg_qty, trend_factor
    return (
        f"Recent visits averaged {recent_weighted:.0f} units. "
        f"Suggesting {capped_to} units."
    )


def detail_qty_seed(qty: int) -> str:
    return f"Suggesting {qty} units — a small starter quantity for a first-time customer."


def detail_qty_peer(median_peer_qty: float, n_peers: int) -> str:
    return (
        f"Customers with similar patterns ({n_peers} of them) "
        f"typically buy {median_peer_qty:.0f} units."
    )


def detail_qty_basket(median_qty: float) -> str:
    return f"Typical quantity when bought together: {median_qty:.0f} units."


def detail_feedback_adjusted(multiplier: float, source: str, n_samples: int) -> str:
    """Boost/dampen annotation when a lane was reweighted by recent sales."""
    direction = "Boosted" if multiplier >= 1.0 else "Dampened"
    _ = source
    return (
        f"{direction} based on what actually sold on this route recently "
        f"({n_samples} similar recommendations tracked)."
    )


# Explanation accumulator.

@dataclass
class Explanation:
    """Signals for one candidate row. Separates ``item_signals`` (why
    the item) from ``quantity_signals`` (how the qty was sized)."""

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
        """Weighted avg signal strength [0, 1]; boosted by corroborating signals."""
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
