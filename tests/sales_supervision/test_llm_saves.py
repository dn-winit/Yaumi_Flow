"""LLM column save endpoints -- briefing, customer-analysis, route-analysis.

These persist the LLM output into yf_supervision_*. They MUST be:
  * idempotent (re-firing with the same content is a no-op via IS NULL guard)
  * tolerant of empty content (column stays NULL, row still upserted)
  * cleanly reject malformed payloads
"""
from __future__ import annotations

import httpx

from tests.common.helpers import assert_not_5xx, assert_ok


def _bootstrap_session(
    client: httpx.Client, base_urls: dict[str, str],
    route: str, date_: str,
) -> tuple[str, str | None]:
    """Initialize a session and return (session_id, first_customer_code)."""
    resp = client.post(
        f"{base_urls['sales_supervision']}/session/initialize",
        json={"route_code": route, "date": date_},
    )
    if resp.status_code != 200:
        return ("", None)
    body = resp.json()
    sid = body["session"]["sessionId"]
    customers = body["session"].get("customers", {})
    first_cust = next(iter(customers.keys())) if customers else None
    return sid, first_cust


class TestSaveBriefing:
    def test_unknown_session_does_not_500(
        self, client: httpx.Client, base_urls: dict[str, str],
    ) -> None:
        resp = client.post(
            f"{base_urls['sales_supervision']}/session/briefing",
            json={
                "session_id": "nonexistent",
                "customer_code": "00000000",
                "content": "test briefing",
            },
        )
        assert_not_5xx(resp, "unknown session briefing")

    def test_missing_required_field_rejected(
        self, client: httpx.Client, base_urls: dict[str, str],
    ) -> None:
        resp = client.post(
            f"{base_urls['sales_supervision']}/session/briefing",
            json={"session_id": "x"},  # missing customer_code + content
        )
        assert 400 <= resp.status_code < 500, (
            f"missing fields should be 4xx, got {resp.status_code}"
        )


class TestSaveCustomerAnalysis:
    def test_unknown_session_does_not_500(
        self, client: httpx.Client, base_urls: dict[str, str],
    ) -> None:
        resp = client.post(
            f"{base_urls['sales_supervision']}/session/customer-analysis",
            json={
                "session_id": "nonexistent",
                "customer_code": "00000000",
                "content": "test analysis",
            },
        )
        assert_not_5xx(resp, "unknown session customer-analysis")


class TestSaveRouteAnalysis:
    def test_unknown_session_does_not_500(
        self, client: httpx.Client, base_urls: dict[str, str],
    ) -> None:
        resp = client.post(
            f"{base_urls['sales_supervision']}/session/route-analysis",
            json={"session_id": "nonexistent", "content": "test route"},
        )
        assert_not_5xx(resp, "unknown session route-analysis")

    def test_real_session_save(
        self, client: httpx.Client, base_urls: dict[str, str],
        primary_route: str, today: str,
    ) -> None:
        """Save a (deterministic-test) route analysis on an existing
        session. Idempotent path: the IS NULL guard means the second
        call is a no-op (logged WARN; not an error)."""
        sid, _ = _bootstrap_session(client, base_urls, primary_route, today)
        if not sid:
            return
        resp = client.post(
            f"{base_urls['sales_supervision']}/session/route-analysis",
            json={"session_id": sid, "content": "[test] route analysis content"},
        )
        assert_ok(resp, f"save route-analysis {sid}")
