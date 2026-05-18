"""Summary + metrics endpoints -- training accuracy + drift surface."""
from __future__ import annotations

import httpx

from tests.common.helpers import assert_ok


def test_summary_get(client: httpx.Client, base_urls: dict[str, str]) -> None:
    """Composite accuracy + holdout splits -- the retrain UI's primary
    read. Must respond 2xx even when no training has run yet (the body
    just carries 'no_metadata' style placeholders)."""
    body = assert_ok(client.get(f"{base_urls['demand_forecasting']}/summary"))
    assert isinstance(body, dict), f"expected dict: {body}"


def test_metrics_models_get(
    client: httpx.Client, base_urls: dict[str, str],
) -> None:
    body = assert_ok(
        client.get(f"{base_urls['demand_forecasting']}/metrics/models")
    )
    assert isinstance(body, (list, dict)), f"unexpected shape: {type(body)}"


def test_explainability_classes_summary_get(
    client: httpx.Client, base_urls: dict[str, str],
) -> None:
    """Demand-class breakdown -- backs the explainability modal."""
    body = assert_ok(
        client.get(f"{base_urls['demand_forecasting']}/explainability/classes/summary")
    )
    assert isinstance(body, (list, dict)), f"unexpected shape: {type(body)}"
