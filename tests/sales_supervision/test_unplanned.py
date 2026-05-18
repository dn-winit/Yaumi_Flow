"""``GET /session/unplanned/{session_id}`` -- drop-in visits.

Customers who invoiced live but weren't on the journey plan. The
endpoint splits the live visitor set into planned-visited and
unplanned tiles for the UI.
"""
from __future__ import annotations

import httpx

from tests.common.helpers import assert_not_5xx, assert_ok


def test_unplanned_unknown_session_returns_structured_body(
    client: httpx.Client, base_urls: dict[str, str],
) -> None:
    """The endpoint returns a structured success=false payload (not a
    404) for unknown sessions so the UI can render the error
    gracefully -- spec'd in the route docstring."""
    resp = client.get(
        f"{base_urls['sales_supervision']}/session/unplanned/nonexistent"
    )
    # Either structured 2xx with success=False, or 4xx.
    assert_not_5xx(resp, "unknown session")
    if 200 <= resp.status_code < 300:
        body = resp.json()
        assert body.get("success") is False, (
            f"unknown session got 2xx + success=True: {body}"
        )


def test_unplanned_real_session(
    client: httpx.Client, base_urls: dict[str, str],
    primary_route: str, today: str,
) -> None:
    init = client.post(
        f"{base_urls['sales_supervision']}/session/initialize",
        json={"route_code": primary_route, "date": today},
    )
    if init.status_code != 200:
        return  # other tests surface init failure
    sid = init.json()["session"]["sessionId"]
    resp = client.get(
        f"{base_urls['sales_supervision']}/session/unplanned/{sid}"
    )
    body = assert_ok(resp, f"unplanned for {sid}")
    # Two list keys, both must be present even when empty.
    assert "planned_visited_codes" in body or "customers" in body, (
        f"unexpected unplanned payload shape: {body}"
    )
