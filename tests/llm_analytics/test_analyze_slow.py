"""Happy-path LLM calls -- gated as ``slow`` because each one takes
multi-second latency, consumes model tokens, and warms the persistent
file cache. Run on-demand with ``pytest -m slow`` or full coverage
sweeps; CI smoke tests should rely on the validation tests instead.
"""
from __future__ import annotations

import httpx
import pytest

from tests.common.helpers import assert_ok


@pytest.mark.slow
def test_analyze_pre_visit_happy(
    client: httpx.Client, base_urls: dict[str, str],
    primary_route: str, today: str,
) -> None:
    resp = client.post(
        f"{base_urls['llm_analytics']}/analyze/pre-visit",
        json={
            "customer_code": "TEST_CUST",
            "customer_name": "Test Customer",
            "route_code": primary_route,
            "date": today,
            "items": [
                {"item_code": "TEST_ITEM_1", "recommended_qty": 10},
                {"item_code": "TEST_ITEM_2", "recommended_qty": 5},
            ],
        },
        timeout=180.0,
    )
    body = assert_ok(resp, "pre-visit LLM")
    # Cache marks DEGRADED_KEY responses so the supervision saver skips
    # the DB write; either content is non-empty OR the degraded flag is
    # set -- never silently empty.
    assert body.get("content") or body.get("degraded") or body.get("briefing"), (
        f"pre-visit returned empty payload: {body}"
    )


@pytest.mark.slow
def test_analyze_customer_happy(
    client: httpx.Client, base_urls: dict[str, str],
    primary_route: str, today: str,
) -> None:
    resp = client.post(
        f"{base_urls['llm_analytics']}/analyze/customer",
        json={
            "customer_code": "TEST_CUST",
            "route_code": primary_route,
            "date": today,
            "customer_data": {"customer_name": "Test"},
            "current_items": [
                {
                    "item_code": "I1",
                    "recommended_qty": 5,
                    "actual_qty": 5,
                    "was_sold": True,
                },
            ],
            "performance_score": 90.0,
            "coverage": 100.0,
            "accuracy": 90.0,
        },
        timeout=180.0,
    )
    assert_ok(resp, "customer LLM")


@pytest.mark.slow
def test_analyze_route_happy(
    client: httpx.Client, base_urls: dict[str, str],
    primary_route: str, today: str,
) -> None:
    resp = client.post(
        f"{base_urls['llm_analytics']}/analyze/route",
        json={
            "route_code": primary_route,
            "date": today,
            "visited_customers": 5,
            "total_customers": 10,
            "total_actual": 100,
            "total_recommended": 120,
            "actual_customer_codes": ["TEST_CUST_1", "TEST_CUST_2"],
        },
        timeout=180.0,
    )
    assert_ok(resp, "route LLM")
