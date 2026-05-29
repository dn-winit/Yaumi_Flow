"""Page-view endpoints -- composite tiles for the webapp.

These pre-aggregate the reconciliation + forecast frames into the exact
shape the dashboard tiles consume. Route-scoped, date-scoped, must
handle empty windows.
"""
from __future__ import annotations

import httpx

from tests.common.helpers import assert_not_5xx


class TestVanLoadPageView:
    """The carry-aware Van Load tile. Heavy aggregation; freshness
    probe runs inline so it must complete fast even on a route with
    long history."""

    def test_happy(
        self, client: httpx.Client, base_urls: dict[str, str],
        primary_route: str, today: str,
    ) -> None:
        resp = client.get(
            f"{base_urls['demand_forecasting']}/page-views/van-load",
            params={"route_code": primary_route, "date": today},
        )
        assert_not_5xx(resp, f"{primary_route}/{today}")

    def test_all_routes(
        self, client: httpx.Client, base_urls: dict[str, str],
        routes: list[str], today: str,
    ) -> None:
        for r in routes:
            resp = client.get(
                f"{base_urls['demand_forecasting']}/page-views/van-load",
                params={"route_code": r, "date": today},
            )
            assert_not_5xx(resp, f"route={r}")


class TestForecastDrawer:
    """Upcoming-plan drawer -- forward-looking forecast window."""

    def test_happy(
        self, client: httpx.Client, base_urls: dict[str, str],
        primary_route: str, today: str,
    ) -> None:
        resp = client.get(
            f"{base_urls['demand_forecasting']}/page-views/forecast-drawer",
            params={"route_code": primary_route, "from_date": today},
        )
        assert_not_5xx(resp, f"{primary_route}/{today}")

    def test_no_route_filter(
        self, client: httpx.Client, base_urls: dict[str, str], today: str,
    ) -> None:
        """``route_code`` is optional -- omitting it should aggregate
        across the configured fleet without 500."""
        resp = client.get(
            f"{base_urls['demand_forecasting']}/page-views/forecast-drawer",
            params={"from_date": today},
        )
        assert_not_5xx(resp, f"fleet-wide {today}")
