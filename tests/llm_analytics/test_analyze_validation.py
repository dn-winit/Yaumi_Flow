"""Pydantic-validation surface for the three /analyze endpoints.

These tests verify request-body validation WITHOUT triggering the
actual LLM call (which is slow + costs money). They send malformed
bodies and assert 4xx responses. Happy-path LLM calls are marked
``slow`` in :file:`test_analyze_slow.py`.
"""
from __future__ import annotations

import httpx
import pytest

from tests.common.helpers import assert_not_5xx


@pytest.mark.parametrize("path", [
    "analyze/customer",
    "analyze/route",
    "analyze/pre-visit",
])
def test_empty_body_rejected(
    client: httpx.Client, base_urls: dict[str, str], path: str,
) -> None:
    """Empty JSON must fail Pydantic validation -- 4xx."""
    resp = client.post(
        f"{base_urls['llm_analytics']}/{path}",
        json={},
    )
    assert 400 <= resp.status_code < 500, (
        f"{path}: empty body should be 4xx, got {resp.status_code}"
    )


def test_customer_missing_route_rejected(
    client: httpx.Client, base_urls: dict[str, str],
) -> None:
    resp = client.post(
        f"{base_urls['llm_analytics']}/analyze/customer",
        json={
            "customer_code": "X",
            # missing route_code, date, customer_data, current_items
        },
    )
    assert 400 <= resp.status_code < 500, (
        f"missing fields should be 4xx, got {resp.status_code}"
    )


def test_route_missing_date_rejected(
    client: httpx.Client, base_urls: dict[str, str], primary_route: str,
) -> None:
    resp = client.post(
        f"{base_urls['llm_analytics']}/analyze/route",
        json={
            "route_code": primary_route,
            # missing date, visited_customers
        },
    )
    assert 400 <= resp.status_code < 500, (
        f"missing fields should be 4xx, got {resp.status_code}"
    )


def test_pre_visit_missing_items_rejected(
    client: httpx.Client, base_urls: dict[str, str],
    primary_route: str, today: str,
) -> None:
    resp = client.post(
        f"{base_urls['llm_analytics']}/analyze/pre-visit",
        json={
            "customer_code": "X",
            "route_code": primary_route,
            "date": today,
            # missing items
        },
    )
    assert 400 <= resp.status_code < 500, (
        f"missing items should be 4xx, got {resp.status_code}"
    )
