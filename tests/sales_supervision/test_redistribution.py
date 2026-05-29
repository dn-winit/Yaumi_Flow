"""``GET /session/redistribution/{route}/{date}/{customer}``

Drill-in view: replay the per-customer redistribution against the rest
of the day's plan. Skipping the customer should return a structured
error, not 500.
"""
from __future__ import annotations

import httpx

from tests.common.helpers import assert_not_5xx


def test_redistribution_unknown_customer_no_5xx(
    client: httpx.Client, base_urls: dict[str, str],
    primary_route: str, today: str,
) -> None:
    """Unknown customer code -- endpoint should return a structured
    'not available' response, not crash."""
    resp = client.get(
        f"{base_urls['sales_supervision']}/session/redistribution/"
        f"{primary_route}/{today}/00000000"
    )
    assert_not_5xx(resp, "redistribution unknown customer")


def test_redistribution_real_customer_when_present(
    client: httpx.Client, base_urls: dict[str, str],
    primary_route: str, today: str,
) -> None:
    """If today's saved-visits include any customer for this route,
    drill into the first one. Otherwise no-op (no data to drill)."""
    saved_resp = client.get(
        f"{base_urls['sales_supervision']}/session/saved",
        params={"route_code": primary_route, "date": today},
    )
    if saved_resp.status_code != 200:
        return  # other tests will surface this
    visits = (saved_resp.json() or {}).get("visits") or {}
    if not visits:
        return  # no visits to drill into
    cust = next(iter(visits.keys()))
    resp = client.get(
        f"{base_urls['sales_supervision']}/session/redistribution/"
        f"{primary_route}/{today}/{cust}"
    )
    assert_not_5xx(resp, f"redistribution real customer {cust}")
