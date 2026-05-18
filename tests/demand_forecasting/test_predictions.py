"""Forecast prediction endpoints -- raw model output surface.

Used by the training-summary UI and the route-grid tile. Less hot than
reconciliation but must stay route-scoped and handle empty data without
500.
"""
from __future__ import annotations

import httpx
import pytest

from tests.common.helpers import assert_not_5xx, assert_ok


def test_predictions_test_happy(
    client: httpx.Client, base_urls: dict[str, str], primary_route: str,
) -> None:
    """/predictions/test -- model's hold-out test predictions for a route."""
    resp = client.get(
        f"{base_urls['demand_forecasting']}/predictions/test",
        params={"route_code": primary_route},
    )
    assert_not_5xx(resp, f"test predictions {primary_route}")


def test_predictions_forecast_route_summary(
    client: httpx.Client, base_urls: dict[str, str], today: str,
) -> None:
    """/predictions/forecast/route-summary -- home-tile fleet summary."""
    resp = client.get(
        f"{base_urls['demand_forecasting']}/predictions/forecast/route-summary",
        params={"date": today},
    )
    body = assert_ok(resp, f"route-summary {today}")
    assert isinstance(body, (list, dict)), f"unexpected shape: {type(body)}"


def test_predictions_forecast_happy(
    client: httpx.Client, base_urls: dict[str, str],
    primary_route: str, today: str,
) -> None:
    resp = client.get(
        f"{base_urls['demand_forecasting']}/predictions/forecast",
        params={"route_code": primary_route, "date": today},
    )
    assert_not_5xx(resp, f"forecast {primary_route}/{today}")


@pytest.mark.parametrize("route_code", ["0000", "9999"])
def test_predictions_forecast_unknown_route(
    client: httpx.Client, base_urls: dict[str, str],
    today: str, route_code: str,
) -> None:
    resp = client.get(
        f"{base_urls['demand_forecasting']}/predictions/forecast",
        params={"route_code": route_code, "date": today},
    )
    assert_not_5xx(resp, f"unknown route {route_code}")
