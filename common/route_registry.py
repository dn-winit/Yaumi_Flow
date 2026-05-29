"""Single source of truth for the fleet's route codes. Overridable via
``YF_ROUTE_CODES`` (CSV/whitespace); consumers pass ``get_route_codes`` as
``default_factory`` so per-service env overrides (``DI_ROUTE_CODES`` etc.)
still apply on top of this baseline.
"""

from __future__ import annotations

import os

# Baseline fleet as of 2026-05-18; field-roster order for scannable logs.
_DEFAULT_ROUTE_CODES: tuple[str, ...] = (
    "9105", "9108", "9114", "9115", "9126", "9142",
    "9202", "9204", "9209", "9218", "9219", "9221",
)


def _parse_env_routes(raw: str) -> list[str]:
    """Parse CSV / whitespace separated env value to a clean list;
    empty tokens are dropped to avoid breaking SQL parameter binding.
    """
    parts = [p.strip() for p in raw.replace(",", " ").split()]
    return [p for p in parts if p]


def get_route_codes() -> list[str]:
    """Return the configured fleet (env override or default). Reads env
    at call time so test fixtures that patch after import still work.
    """
    raw = (os.environ.get("YF_ROUTE_CODES") or "").strip()
    if raw:
        parsed = _parse_env_routes(raw)
        if parsed:
            return parsed
    return list(_DEFAULT_ROUTE_CODES)


__all__ = ["get_route_codes"]
