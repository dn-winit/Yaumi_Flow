"""Single source of truth for visit_totals aggregation.

Shared by /session/initialize (in-memory), /session/saved (DB rows), and
/session/visit so all three render the "Visited X/Y" + "Avg score" tiles
from identical arithmetic. Callers normalise to a per-customer dict with
``total_actual``, ``total_recommended``, and ``score`` keys.
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, List, Mapping, Optional


def aggregate_visit_totals(
    planned_visited: Iterable[Mapping[str, Any]],
    unplanned_visited_count: int = 0,
) -> Dict[str, Any]:
    """Compose the canonical ``visit_totals`` dict.

    ``planned_visited`` covers planned-customer visits (denominator of "Visited X/Y");
    drop-ins ride separately on ``unplanned_visited_count``. Returns the
    ``SessionVisitTotals``-shaped dict; ``avg_score`` is None only when no visits.
    """
    items: List[Mapping[str, Any]] = list(planned_visited)
    visited_count = len(items)
    if visited_count == 0:
        return {
            "visited_count": 0,
            "total_actual": 0,
            "total_recommended": 0,
            "avg_score": None,
            "unplanned_visited_count": int(unplanned_visited_count),
        }
    total_actual = int(sum(int(i["total_actual"]) for i in items))
    total_recommended = int(sum(int(i["total_recommended"]) for i in items))
    avg_score: Optional[float] = round(
        sum(float(i["score"]) for i in items) / visited_count, 2,
    )
    return {
        "visited_count": visited_count,
        "total_actual": total_actual,
        "total_recommended": total_recommended,
        "avg_score": avg_score,
        "unplanned_visited_count": int(unplanned_visited_count),
    }
