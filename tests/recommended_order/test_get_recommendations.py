"""POST /get -- the consumer-facing retrieval endpoint.

Used by the webapp recommendation tile + by sales_supervision's
RecommendedOrderClient on every 60s reconcile tick. Must:
  * return 2xx + structured payload on a configured route
  * not 500 on unknown route (return empty data instead)
  * respect ``limit`` parameter
"""
from __future__ import annotations

import httpx
import pytest

from tests.common.helpers import assert_not_5xx, assert_ok


class TestGet:
    """``POST /get`` -- date is required; route_code/customer_code/item_code
    are optional filters."""

    def test_happy_one_route(
        self, client: httpx.Client, base_urls: dict[str, str],
        primary_route: str, today: str,
    ) -> None:
        resp = client.post(
            f"{base_urls['recommended_order']}/get",
            json={"date": today, "route_code": primary_route, "limit": 100},
        )
        body = assert_ok(resp, f"{primary_route}/{today}")
        # The supervision client reads ``success`` + ``data``;
        # contract: success boolean + data list.
        assert "success" in body, f"missing 'success' in {body}"
        assert isinstance(body.get("data"), list), (
            f"'data' is not a list: {type(body.get('data')).__name__}"
        )

    @pytest.mark.parametrize("route_code", ["0000", "9999"])
    def test_unknown_route(
        self, client: httpx.Client, base_urls: dict[str, str],
        today: str, route_code: str,
    ) -> None:
        resp = client.post(
            f"{base_urls['recommended_order']}/get",
            json={"date": today, "route_code": route_code, "limit": 10},
        )
        assert_not_5xx(resp, f"unknown route {route_code}")

    def test_far_past_date(
        self, client: httpx.Client, base_urls: dict[str, str],
        primary_route: str, far_past: str,
    ) -> None:
        resp = client.post(
            f"{base_urls['recommended_order']}/get",
            json={"date": far_past, "route_code": primary_route, "limit": 10},
        )
        # Far-past predates the data; route should auto-heal carry chain
        # OR return empty. Either way: not a 5xx.
        assert_not_5xx(resp, f"far-past {far_past}")

    def test_bad_date_format_rejected(
        self, client: httpx.Client, base_urls: dict[str, str],
        primary_route: str,
    ) -> None:
        """Malformed date must be rejected with a 4xx (Pydantic
        validation), never accepted and crash later."""
        resp = client.post(
            f"{base_urls['recommended_order']}/get",
            json={"date": "not-a-date", "route_code": primary_route},
        )
        assert 400 <= resp.status_code < 500, (
            f"bad date should be 4xx, got {resp.status_code}"
        )

    def test_all_routes_no_500(
        self, client: httpx.Client, base_urls: dict[str, str],
        routes: list[str], today: str,
    ) -> None:
        """Cross-fleet sweep on the supervision hot path."""
        for r in routes:
            resp = client.post(
                f"{base_urls['recommended_order']}/get",
                json={"date": today, "route_code": r, "limit": 50},
            )
            assert_not_5xx(resp, f"route={r}")


@pytest.mark.slow
def test_generate_force_one_route(
    client: httpx.Client, base_urls: dict[str, str],
    primary_route: str, today: str,
) -> None:
    """``POST /generate?force=true`` for one route. Slow because it
    re-runs the engine and writes to DB."""
    resp = client.post(
        f"{base_urls['recommended_order']}/generate",
        json={
            "date": today,
            "route_codes": [primary_route],
            "force": True,
        },
        timeout=300.0,
    )
    body = assert_ok(resp, f"generate {primary_route}/{today}")
    assert body.get("success") in (True, None) or "routes_processed" in body, (
        f"generate result lacks success/routes_processed: {body}"
    )
