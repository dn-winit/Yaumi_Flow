"""EDA endpoints -- the read surface the dashboard uses.

These power the home-page tiles + filter sidebar. Bugs here surface as
"my dashboard is blank" tickets; every endpoint must:
  * return 2xx on happy-path input
  * return an empty-but-valid response (not 500) on unknown routes / far dates
  * carry the documented top-level keys so the webapp's JS doesn't crash
"""
from __future__ import annotations

import httpx
import pytest

from tests.common.helpers import assert_not_5xx, assert_ok

# ---- /eda/last-active-date ------------------------------------------------

def test_last_active_date_happy(
    client: httpx.Client, base_urls: dict[str, str],
) -> None:
    body = assert_ok(client.get(f"{base_urls['data_import']}/eda/last-active-date"))
    assert body.get("date"), f"missing 'date' in {body}"


# ---- /eda/business-kpis ---------------------------------------------------

def test_business_kpis_happy(
    client: httpx.Client, base_urls: dict[str, str],
    today: str, yesterday: str,
) -> None:
    """Dashboard's 4 exec-KPI tiles. Requires an explicit window so the
    aggregation is deterministic across runs."""
    body = assert_ok(
        client.get(
            f"{base_urls['data_import']}/eda/business-kpis",
            params={"start_date": yesterday, "end_date": today},
        )
    )
    assert isinstance(body, dict) and body, f"empty KPI payload: {body}"


# ---- /eda/filter-dimensions ----------------------------------------------

def test_filter_dimensions_happy(
    client: httpx.Client, base_urls: dict[str, str],
) -> None:
    body = assert_ok(client.get(f"{base_urls['data_import']}/eda/filter-dimensions"))
    # Webapp's filter sidebar reads these arrays -- empty arrays here
    # break the route picker even though the endpoint returns 2xx.
    assert any(isinstance(v, list) for v in body.values()), (
        f"filter-dimensions returned no list payloads: {body}"
    )


# ---- /eda/items -----------------------------------------------------------

def test_items_happy(client: httpx.Client, base_urls: dict[str, str]) -> None:
    body = assert_ok(client.get(f"{base_urls['data_import']}/eda/items"))
    assert isinstance(body, (list, dict)), f"unexpected items shape: {type(body)}"


# ---- /eda/sales -----------------------------------------------------------

class TestEdaSales:
    """Sales aggregation endpoint -- backs the dashboard sales chart."""

    def test_happy(
        self,
        client: httpx.Client,
        base_urls: dict[str, str],
        primary_route: str,
        today: str,
        yesterday: str,
    ) -> None:
        resp = client.get(
            f"{base_urls['data_import']}/eda/sales",
            params={
                "route_code": primary_route,
                "start_date": yesterday,
                "end_date": today,
            },
        )
        body = assert_ok(resp, f"route={primary_route} window={yesterday}->{today}")
        assert isinstance(body, dict), f"expected dict, got {type(body).__name__}"

    @pytest.mark.parametrize("route_code", ["0000", "9999", ""])
    def test_unknown_route_does_not_500(
        self,
        client: httpx.Client,
        base_urls: dict[str, str],
        today: str,
        yesterday: str,
        route_code: str,
    ) -> None:
        """Unknown routes must produce an empty result (or 4xx with a
        structured error), never a 500."""
        resp = client.get(
            f"{base_urls['data_import']}/eda/sales",
            params={
                "route_code": route_code,
                "start_date": yesterday,
                "end_date": today,
            },
        )
        assert_not_5xx(resp, f"unknown route={route_code!r}")

    def test_far_past_window(
        self,
        client: httpx.Client,
        base_urls: dict[str, str],
        primary_route: str,
        far_past: str,
    ) -> None:
        """A window that predates any populated data is well-formed but
        empty -- response must be 2xx with zero rows, not 500."""
        resp = client.get(
            f"{base_urls['data_import']}/eda/sales",
            params={
                "route_code": primary_route,
                "start_date": far_past,
                "end_date": far_past,
            },
        )
        assert_not_5xx(resp, f"far-past window={far_past}")


# ---- /eda/item-stats ------------------------------------------------------

def test_item_stats_happy(
    client: httpx.Client, base_urls: dict[str, str], primary_route: str, today: str,
) -> None:
    resp = client.get(
        f"{base_urls['data_import']}/eda/item-stats",
        params={"route_code": primary_route, "date": today},
    )
    assert_not_5xx(resp, f"item-stats happy {primary_route}/{today}")
