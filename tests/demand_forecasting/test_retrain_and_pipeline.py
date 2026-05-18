"""Retrain config + Step Functions pipeline status surface.

These don't trigger heavy work on GET; they just read state. POSTs to
``/retrain/config`` and ``/pipeline/{train,inference}`` are marked
slow because they mutate persisted state or kick off external jobs.
"""
from __future__ import annotations

import httpx
import pytest

from tests.common.helpers import assert_ok


def test_retrain_config_get(
    client: httpx.Client, base_urls: dict[str, str],
) -> None:
    """Reads the persisted retrain config (auto-retrain flag, schedule,
    baseline reference)."""
    body = assert_ok(
        client.get(f"{base_urls['demand_forecasting']}/retrain/config")
    )
    assert isinstance(body, dict), f"expected dict: {body}"


def test_retrain_history_get(
    client: httpx.Client, base_urls: dict[str, str],
) -> None:
    body = assert_ok(
        client.get(f"{base_urls['demand_forecasting']}/retrain/history")
    )
    # Either a list of runs or a wrapper dict carrying one.
    assert isinstance(body, (list, dict)), f"unexpected shape: {type(body)}"


def test_pipeline_status_get(
    client: httpx.Client, base_urls: dict[str, str],
) -> None:
    body = assert_ok(
        client.get(f"{base_urls['demand_forecasting']}/pipeline/status")
    )
    assert isinstance(body, dict), f"expected dict: {body}"


def test_pipeline_resolved_status_get(
    client: httpx.Client, base_urls: dict[str, str],
) -> None:
    body = assert_ok(
        client.get(f"{base_urls['demand_forecasting']}/pipeline/resolved-status")
    )
    assert isinstance(body, dict), f"expected dict: {body}"
