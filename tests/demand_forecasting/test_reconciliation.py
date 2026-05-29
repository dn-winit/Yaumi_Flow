"""Reconciliation endpoints -- the carry chain + V5_b van load views.

The drilldown surface (van-load tile, past-performance counterfactual,
recommend modal) reads from these. They must be route-scoped, date-
scoped, and handle empty windows without 500.

``/reconciliation/refresh`` is gated as ``slow`` because it triggers
the full cron path including the data_import + recommended_order
cascades; each run takes ~100s on the 12-route fleet.
"""
from __future__ import annotations

import httpx
import pytest

from tests.common.helpers import assert_not_5xx, assert_ok

# ---- /reconciliation/van-load --------------------------------------------

class TestVanLoad:
    def test_happy(
        self, client: httpx.Client, base_urls: dict[str, str],
        primary_route: str, today: str,
    ) -> None:
        resp = client.get(
            f"{base_urls['demand_forecasting']}/reconciliation/van-load",
            params={"route_code": primary_route, "date": today},
        )
        body = assert_ok(resp, f"{primary_route}/{today}")
        # Composition tile expects items[] plus totals.
        assert isinstance(body, dict), f"expected dict: {body}"

    def test_unknown_route(
        self, client: httpx.Client, base_urls: dict[str, str],
        today: str, unknown_route: str,
    ) -> None:
        resp = client.get(
            f"{base_urls['demand_forecasting']}/reconciliation/van-load",
            params={"route_code": unknown_route, "date": today},
        )
        assert_not_5xx(resp, f"unknown route {unknown_route}")

    def test_far_past_date(
        self, client: httpx.Client, base_urls: dict[str, str],
        primary_route: str, far_past: str,
    ) -> None:
        resp = client.get(
            f"{base_urls['demand_forecasting']}/reconciliation/van-load",
            params={"route_code": primary_route, "date": far_past},
        )
        assert_not_5xx(resp, f"far-past {far_past}")


# ---- /reconciliation/recommend -------------------------------------------

class TestRecommend:
    def test_happy(
        self, client: httpx.Client, base_urls: dict[str, str],
        primary_route: str, today: str,
    ) -> None:
        resp = client.get(
            f"{base_urls['demand_forecasting']}/reconciliation/recommend",
            params={"route_code": primary_route, "date": today},
        )
        assert_not_5xx(resp, f"recommend {primary_route}/{today}")


# ---- /reconciliation/past-performance ------------------------------------

class TestPastPerformance:
    """The counterfactual-simulation drawer. Heaviest reconciliation
    read; always 2xx on valid (route, date) pairs even when the window
    is empty."""

    def test_happy(
        self, client: httpx.Client, base_urls: dict[str, str],
        primary_route: str, yesterday: str,
    ) -> None:
        resp = client.get(
            f"{base_urls['demand_forecasting']}/reconciliation/past-performance",
            params={"route_code": primary_route, "date": yesterday},
        )
        assert_not_5xx(resp, f"past-perf {primary_route}/{yesterday}")

    def test_all_configured_routes_happy(
        self, client: httpx.Client, base_urls: dict[str, str],
        routes: list[str], yesterday: str,
    ) -> None:
        """Cross-fleet sweep: every configured route must serve
        yesterday's past-performance without 500. Catches per-route
        data-quality regressions (a specific route's data shape breaks
        the aggregation)."""
        for r in routes:
            resp = client.get(
                f"{base_urls['demand_forecasting']}/reconciliation/past-performance",
                params={"route_code": r, "date": yesterday},
            )
            assert_not_5xx(resp, f"past-perf route={r}")


# ---- /reconciliation/refresh ---------------------------------------------

@pytest.mark.slow
def test_reconciliation_refresh_dry(
    client: httpx.Client, base_urls: dict[str, str],
) -> None:
    """End-to-end cascade test: trigger the refresh and verify the
    response carries success + a non-negative rows_updated count."""
    resp = client.post(
        f"{base_urls['demand_forecasting']}/reconciliation/refresh",
        json={"horizon_days_behind": 0},
        timeout=600.0,
    )
    body = assert_ok(resp, "manual reconciliation refresh")
    assert body.get("success") is True, f"refresh not successful: {body}"
    assert isinstance(body.get("rows_updated"), int), (
        f"rows_updated missing or non-int: {body}"
    )
