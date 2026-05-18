"""Health surface for sales_supervision (8004)."""
from __future__ import annotations

import httpx

from tests.common.helpers import assert_keys, assert_ok


def test_health_returns_2xx(client: httpx.Client, base_urls: dict[str, str]) -> None:
    body = assert_ok(client.get(f"{base_urls['sales_supervision']}/health"))
    assert_keys(body, [
        "status", "db_configured",
        "last_reconcile_epoch", "reconcile_lag_seconds", "reconcile_stale",
    ])


def test_health_reports_db_configured(
    client: httpx.Client, base_urls: dict[str, str],
) -> None:
    """Tests run against a deployment with the DB configured; if this
    flips false, supervision can't write at all and every other test
    will fail. Surface it loudly here."""
    body = assert_ok(client.get(f"{base_urls['sales_supervision']}/health"))
    assert body["db_configured"] is True, (
        f"DB not configured -- check SS_DB_HOST/SS_DB_USERNAME env: {body}"
    )


def test_reconcile_lag_within_threshold(
    client: httpx.Client, base_urls: dict[str, str],
) -> None:
    """Per-route heartbeat keeps lag well under 2x poll. If lag exceeds
    a generous 5x poll, the cron is wedged."""
    body = assert_ok(client.get(f"{base_urls['sales_supervision']}/health"))
    lag = body.get("reconcile_lag_seconds")
    if lag is None:
        # First-ever boot: no tick has fired yet. Acceptable here; the
        # next periodic check will catch a wedge.
        return
    assert lag < 300, f"reconcile lag too high ({lag}s) -- cron may be wedged: {body}"
