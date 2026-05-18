"""Health + cache stats surface for llm_analytics (8003).

Fast endpoints -- no LLM calls. Safe to run on every CI tick.
"""
from __future__ import annotations

import httpx

from tests.common.helpers import assert_ok


def test_health_returns_2xx(client: httpx.Client, base_urls: dict[str, str]) -> None:
    """llm_analytics health shape differs from the other services -- it
    reports ``available`` + provider/model info + a cache breakdown."""
    body = assert_ok(client.get(f"{base_urls['llm_analytics']}/health"))
    assert body.get("available") is True, f"analyzer not available: {body}"
    assert body.get("cache"), f"missing cache breakdown: {body}"


def test_cache_stats_returns_2xx(
    client: httpx.Client, base_urls: dict[str, str],
) -> None:
    body = assert_ok(client.get(f"{base_urls['llm_analytics']}/cache/stats"))
    # Cache exposes hit/miss counters; either nested or flat.
    assert isinstance(body, dict), f"expected dict: {body}"


def test_cache_clear_idempotent(
    client: httpx.Client, base_urls: dict[str, str],
) -> None:
    """Clearing an empty cache must succeed without error -- the
    operation is idempotent by design."""
    body = assert_ok(client.post(f"{base_urls['llm_analytics']}/cache/clear"))
    assert isinstance(body, dict), f"expected dict: {body}"
