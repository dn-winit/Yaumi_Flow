"""Filter options + analytics endpoints -- the dashboard sidebar +
adoption/upcoming drawers."""
from __future__ import annotations

import httpx
import pytest

from tests.common.helpers import assert_not_5xx, assert_ok


def test_filter_options_get(
    client: httpx.Client, base_urls: dict[str, str], today: str,
) -> None:
    """Route picker grid: each configured route returns a journey
    count + diagnosis."""
    resp = client.get(
        f"{base_urls['recommended_order']}/filter-options",
        params={"date": today},
    )
    body = assert_ok(resp, f"filter-options {today}")
    assert isinstance(body, dict), f"expected dict: {body}"


class TestAnalyticsAdoption:
    def test_happy(
        self, client: httpx.Client, base_urls: dict[str, str],
        primary_route: str, today: str, yesterday: str,
    ) -> None:
        resp = client.get(
            f"{base_urls['recommended_order']}/analytics/adoption",
            params={
                "start_date": yesterday,
                "end_date": today,
                "route_code": primary_route,
            },
        )
        assert_not_5xx(resp, f"adoption {primary_route}")

    def test_no_route_filter(
        self, client: httpx.Client, base_urls: dict[str, str],
        today: str, yesterday: str,
    ) -> None:
        """Cross-fleet adoption -- omit ``route_code``."""
        resp = client.get(
            f"{base_urls['recommended_order']}/analytics/adoption",
            params={"start_date": yesterday, "end_date": today},
        )
        assert_not_5xx(resp, "fleet-wide adoption")


class TestAnalyticsUpcoming:
    def test_happy(
        self, client: httpx.Client, base_urls: dict[str, str],
        primary_route: str, today: str,
    ) -> None:
        resp = client.get(
            f"{base_urls['recommended_order']}/analytics/upcoming",
            params={"route_code": primary_route, "today": today, "days": 7},
        )
        assert_not_5xx(resp, f"upcoming {primary_route}")

    def test_default_window(
        self, client: httpx.Client, base_urls: dict[str, str], today: str,
    ) -> None:
        """No ``days`` param -- service default should kick in."""
        resp = client.get(
            f"{base_urls['recommended_order']}/analytics/upcoming",
            params={"today": today},
        )
        assert_not_5xx(resp, "default upcoming window")
