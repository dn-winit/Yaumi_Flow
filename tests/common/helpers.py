"""Shared assertion + payload helpers for the integration suite.

Keeps individual tests terse: each test focuses on the contract of its
endpoint, and the boilerplate (status assertions, JSON shape checks,
descriptive failure messages) lives here.
"""
from __future__ import annotations

from collections.abc import Iterable
from typing import Any

import httpx


def assert_ok(resp: httpx.Response, ctx: str = "") -> dict[str, Any]:
    """Assert a 2xx response, parse JSON, return the body.

    ``ctx`` is folded into the failure message so a multi-test file with
    parametrized cases stays diagnosable from the bare pytest output
    (no need to chase the specific call site in the traceback).
    """
    label = f" [{ctx}]" if ctx else ""
    assert 200 <= resp.status_code < 300, (
        f"expected 2xx{label}, got {resp.status_code} "
        f"from {resp.request.method} {resp.request.url} -- "
        f"body: {resp.text[:500]}"
    )
    if not resp.content:
        return {}
    try:
        return resp.json()
    except Exception as exc:
        raise AssertionError(
            f"response{label} is not JSON: {exc}; body[:200] = {resp.text[:200]}"
        )


def assert_status(resp: httpx.Response, expected: int, ctx: str = "") -> None:
    """Assert an exact status code. Useful for testing edge cases where
    we expect a specific error code (404 for unknown session, 422 for
    bad payload), not just any 4xx/5xx."""
    label = f" [{ctx}]" if ctx else ""
    assert resp.status_code == expected, (
        f"expected {expected}{label}, got {resp.status_code} "
        f"from {resp.request.method} {resp.request.url} -- "
        f"body: {resp.text[:500]}"
    )


def assert_keys(body: dict, required: Iterable[str], ctx: str = "") -> None:
    """Assert every key in ``required`` is present in the response body."""
    label = f" [{ctx}]" if ctx else ""
    missing = [k for k in required if k not in body]
    assert not missing, (
        f"response{label} missing required keys: {missing}; "
        f"got keys: {sorted(body.keys())[:20]}"
    )


def assert_not_5xx(resp: httpx.Response, ctx: str = "") -> None:
    """Endpoints should NEVER 5xx on well-formed but semantically-empty
    input (unknown route, far-past date). They should return an empty
    result with a 2xx, or a 4xx with a structured error -- never a 500.
    """
    label = f" [{ctx}]" if ctx else ""
    assert resp.status_code < 500, (
        f"5xx{label} on well-formed input -- got {resp.status_code} "
        f"from {resp.request.method} {resp.request.url} -- "
        f"body: {resp.text[:500]}"
    )
