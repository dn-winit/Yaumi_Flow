"""Health surface for recommended_order (8001)."""
from __future__ import annotations

import httpx

from tests.common.helpers import assert_ok


def test_health_returns_2xx(client: httpx.Client, base_urls: dict[str, str]) -> None:
    body = assert_ok(client.get(f"{base_urls['recommended_order']}/health"))
    assert "status" in body, f"missing status: {body}"


def test_summary_returns_dict(
    client: httpx.Client, base_urls: dict[str, str],
) -> None:
    body = assert_ok(client.get(f"{base_urls['recommended_order']}/summary"))
    assert isinstance(body, dict), f"expected dict: {body}"
