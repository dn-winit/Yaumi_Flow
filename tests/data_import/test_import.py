"""Import endpoints -- gated as ``slow`` because each pull hits YaumiLive
and writes the CSV mirror. Running them at unbounded frequency would
saturate the warehouse and stomp on the scheduled cron's mirror state.
"""
from __future__ import annotations

import httpx
import pytest

from tests.common.helpers import assert_ok


@pytest.mark.slow
def test_import_single_dataset_incremental(
    client: httpx.Client, base_urls: dict[str, str],
) -> None:
    """``POST /import`` on one well-known small dataset. Verifies the
    incremental path returns a success envelope; doesn't assert row
    counts (those drift as new invoices land)."""
    resp = client.post(
        f"{base_urls['data_import']}/import",
        json={"dataset": "journey_plan", "mode": "incremental"},
        timeout=120.0,
    )
    body = assert_ok(resp, "journey_plan incremental")
    assert body.get("success") is True, f"import did not report success: {body}"


@pytest.mark.slow
def test_import_unknown_dataset_rejects(
    client: httpx.Client, base_urls: dict[str, str],
) -> None:
    """Unknown dataset name must NOT silently write -- the response
    should carry success=False with a structured error."""
    resp = client.post(
        f"{base_urls['data_import']}/import",
        json={"dataset": "no_such_dataset", "mode": "incremental"},
        timeout=30.0,
    )
    # Either 4xx (route/Pydantic rejects) or 2xx with success=False.
    if 200 <= resp.status_code < 300:
        body = resp.json()
        assert body.get("success") is False, (
            f"unknown dataset returned 2xx + success=True: {body}"
        )
    else:
        assert 400 <= resp.status_code < 500, (
            f"unknown dataset should be 4xx, got {resp.status_code}: {resp.text[:200]}"
        )
