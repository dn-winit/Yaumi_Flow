"""Health + status surface for data_import (8005).

These endpoints back the dashboard's status tiles and the cross-service
freshness probes -- if they're wrong, every downstream service that
checks them at startup misreports its own state.
"""
from __future__ import annotations

import httpx

from tests.common.helpers import assert_ok


def test_health_returns_2xx(client: httpx.Client, base_urls: dict[str, str]) -> None:
    """``/health`` is the only contract every other backend probes at
    boot. Must respond 2xx and surface a recognisable status field."""
    body = assert_ok(client.get(f"{base_urls['data_import']}/health"), "health")
    assert body.get("status"), f"missing status in {body}"


def test_status_lists_every_dataset(client: httpx.Client, base_urls: dict[str, str]) -> None:
    """``/status`` is the per-CSV roll-up: each registered dataset must
    have a state entry. Missing entries indicate a dataset registry
    regression."""
    body = assert_ok(client.get(f"{base_urls['data_import']}/status"), "status")
    # Either flat (each dataset as a top-level key) or nested under
    # "datasets" -- both shapes have appeared during the refactor.
    datasets = body.get("datasets") or body
    expected = {
        "customer_data", "journey_plan", "sales_recent",
        "demand_forecast", "sales_transactions",
    }
    found = {k for k in datasets.keys() if k in expected}
    assert expected.issubset(found), (
        f"datasets missing from /status: {expected - found}; got keys: {sorted(datasets.keys())}"
    )


def test_summary_returns_2xx(client: httpx.Client, base_urls: dict[str, str]) -> None:
    """``/summary`` aggregates dataset stats for the dashboard. Should
    not 500 even with stale CSVs."""
    body = assert_ok(client.get(f"{base_urls['data_import']}/summary"), "summary")
    assert isinstance(body, dict), f"expected dict, got {type(body).__name__}"
