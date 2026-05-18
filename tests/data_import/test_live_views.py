"""Live-view endpoints -- read-through to YaumiLive.

These cut through the CSV mirror and query YaumiLive directly for
real-time signals (today's invoices, current van composition). They
sit on the supervision path's hot tier and on the explainability
modal -- correctness here is critical, performance also matters.
"""
from __future__ import annotations

import httpx
import pytest

from tests.common.helpers import assert_not_5xx, assert_ok


class TestLiveRouteSales:
    """Today's invoiced customers for a (route, date)."""

    def test_happy(
        self, client: httpx.Client, base_urls: dict[str, str],
        primary_route: str, today: str,
    ) -> None:
        resp = client.get(
            f"{base_urls['data_import']}/eda/live-route-sales",
            params={"route_code": primary_route, "date": today},
        )
        body = assert_ok(resp, f"{primary_route}/{today}")
        # The supervision auto_visit cron consumes this every 60s -- it
        # must always return ``available`` so the cron's fail-soft
        # branch is reachable. Empty ``customers`` is legitimate (no
        # invoices yet today); missing ``available`` would crash the
        # client.
        assert "available" in body, f"missing 'available' in {body}"

    @pytest.mark.parametrize("route_code", ["0000", "9999"])
    def test_unknown_route(
        self, client: httpx.Client, base_urls: dict[str, str],
        today: str, route_code: str,
    ) -> None:
        resp = client.get(
            f"{base_urls['data_import']}/eda/live-route-sales",
            params={"route_code": route_code, "date": today},
        )
        assert_not_5xx(resp, f"unknown route={route_code}")

    def test_far_past_date(
        self, client: httpx.Client, base_urls: dict[str, str],
        primary_route: str, far_past: str,
    ) -> None:
        resp = client.get(
            f"{base_urls['data_import']}/eda/live-route-sales",
            params={"route_code": primary_route, "date": far_past},
        )
        assert_not_5xx(resp, f"far-past {primary_route}/{far_past}")


class TestLiveCustomerSales:
    """Per-customer invoiced items for one visit."""

    def test_happy(
        self, client: httpx.Client, base_urls: dict[str, str],
        primary_route: str, today: str,
    ) -> None:
        # Probe live-route-sales first to find a customer that actually
        # invoiced today; if none, the test is a no-op verification
        # that the empty-path doesn't crash.
        route_sales = client.get(
            f"{base_urls['data_import']}/eda/live-route-sales",
            params={"route_code": primary_route, "date": today},
        ).json()
        customers = (route_sales.get("customers") or [])
        if not customers:
            pytest.skip(f"no invoiced customer for {primary_route}/{today}")
        cust = str(customers[0].get("customer_code") or "")
        assert cust, f"customer entry missing customer_code: {customers[0]}"
        resp = client.get(
            f"{base_urls['data_import']}/eda/live-customer-sales",
            params={
                "route_code": primary_route,
                "date": today,
                "customer_code": cust,
            },
        )
        body = assert_ok(resp, f"{primary_route}/{today}/{cust}")
        assert "available" in body, f"missing 'available' in {body}"

    def test_unknown_customer(
        self, client: httpx.Client, base_urls: dict[str, str],
        primary_route: str, today: str,
    ) -> None:
        resp = client.get(
            f"{base_urls['data_import']}/eda/live-customer-sales",
            params={
                "route_code": primary_route, "date": today,
                "customer_code": "00000000",
            },
        )
        assert_not_5xx(resp, "unknown customer")


class TestLiveVanComposition:
    """Live van composition -- closing + allocation per (route, date)."""

    def test_happy(
        self, client: httpx.Client, base_urls: dict[str, str],
        primary_route: str, today: str,
    ) -> None:
        resp = client.get(
            f"{base_urls['data_import']}/eda/live-van-composition",
            params={"route_code": primary_route, "date": today},
        )
        assert_not_5xx(resp, f"happy {primary_route}/{today}")
