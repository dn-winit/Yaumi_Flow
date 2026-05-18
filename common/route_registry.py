"""Single source of truth for the fleet's route codes.

Why this lives in ``common/`` rather than each service's settings.py:
every service (``data_import``, ``recommended_order``, supervision)
plus the verification scripts used to carry its own copy of the same
12 codes. When the fleet expanded, those copies drifted -- the cron
imported on one set, the recommender generated on another, and the
verification regression silently passed because it compared a route
to itself instead of the canonical roster. Centralising the list here
collapses the four copies into one, kept overridable via
``YF_ROUTE_CODES`` (CSV-separated) for ops to add or retire a route
without a code change.

Consumers (data_import / recommended_order Settings, scripts) read
``ROUTE_CODES`` and pass it as the ``default_factory`` of their own
``route_codes`` field so per-service overrides via service-scoped env
vars (``DI_ROUTE_CODES`` etc.) still work; the helper here only
provides the BASELINE.
"""

from __future__ import annotations

import os
from typing import List

# Baseline fleet as of 2026-05-18. Ordered the way the field roster
# carries it so log output stays human-scannable in operator review.
_DEFAULT_ROUTE_CODES: tuple[str, ...] = (
    "9105", "9108", "9114", "9115", "9126", "9142",
    "9202", "9204", "9209", "9218", "9219", "9221",
)


def _parse_env_routes(raw: str) -> List[str]:
    """Parse a CSV / whitespace separated env value into a clean list.

    Accepts ``"9105,9108,9114"`` and ``"9105 9108 9114"`` and the mixed
    form. Empty tokens are dropped so a stray trailing comma cannot
    inject an empty route_code that later breaks SQL parameter binding.
    """
    parts = [p.strip() for p in raw.replace(",", " ").split()]
    return [p for p in parts if p]


def get_route_codes() -> List[str]:
    """Return the configured fleet -- env override falls back to default.

    Reading ``os.environ`` at call time (not import time) keeps the
    function correct under test fixtures that patch the environment
    after the module is already imported.
    """
    raw = (os.environ.get("YF_ROUTE_CODES") or "").strip()
    if raw:
        parsed = _parse_env_routes(raw)
        if parsed:
            return parsed
    return list(_DEFAULT_ROUTE_CODES)


# Module-level constant for callers that prefer a literal import to a
# function call (Pydantic ``default_factory`` wants a callable; pure
# scripts can grab the snapshot directly).
ROUTE_CODES: tuple[str, ...] = tuple(get_route_codes())


__all__ = ["ROUTE_CODES", "get_route_codes"]
