"""Shared pytest fixtures for the integration suite.

Loads ``.env`` at import time so DB credentials and the route registry are
available to every test. Exposes one ``httpx.Client`` per test session
(reused across requests for connection-pool efficiency), the configured
route fleet, and an optional read-only DB connection that auto-skips
tests when credentials aren't configured.
"""
from __future__ import annotations

import os
import sys
from datetime import date, timedelta
from pathlib import Path

import httpx
import pytest
from dotenv import load_dotenv

# Repo root must be on sys.path so test modules can ``from common...``.
_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

load_dotenv(_REPO_ROOT / ".env")

from common.route_registry import get_route_codes  # noqa: E402


# ---------------------------------------------------------------------------
# Service base URLs
# ---------------------------------------------------------------------------

BASE_URLS = {
    "data_import":        "http://localhost:8005/api/v1/data",
    "recommended_order":  "http://localhost:8001/api/v1/recommended-order",
    "demand_forecasting": "http://localhost:8002/api/v1/forecast",
    "llm_analytics":      "http://localhost:8003/api/v1/analytics",
    "sales_supervision":  "http://localhost:8004/api/v1/supervision",
}


@pytest.fixture(scope="session")
def base_urls() -> dict[str, str]:
    """Mapping of module -> base URL prefix."""
    return BASE_URLS


@pytest.fixture(scope="session")
def client() -> httpx.Client:
    """Shared HTTP client. 30s timeout is generous enough for the slow
    routes (reconciliation reads, page-views aggregations) and tight
    enough to flag a wedged endpoint within a single test.

    Transport-level retries=1 absorbs the stale-keepalive disconnect
    that occasionally surfaces when a test follows a long-running one
    (server-side idle timeout closes the pooled connection between
    calls, then the next request hits a half-closed socket). Without
    this, the affected test reads as a flaky failure even though the
    endpoint itself is healthy. One retry is sufficient because the
    second attempt opens a fresh connection.
    """
    transport = httpx.HTTPTransport(retries=3)
    # ``http2=False`` forces HTTP/1.1. ``Connection: close`` on every
    # request disables keep-alive entirely so a stale pooled
    # connection from a previous test can never trip WinError 10054
    # on the next call -- the cost is ~5-10ms of TCP handshake per
    # request, negligible against the 100+ test suite total. The
    # earlier ``transport=HTTPTransport(retries=3)`` setting helps
    # for connect-time errors but doesn't cover mid-read disconnects,
    # which is the actual failure mode under a busy supervision
    # cron tick. Header is overridable per-call when a test needs
    # to validate keep-alive behaviour explicitly.
    with httpx.Client(
        timeout=30.0,
        follow_redirects=False,
        transport=transport,
        http2=False,
        headers={"Connection": "close"},
    ) as c:
        yield c


@pytest.fixture(scope="session")
def routes() -> list[str]:
    """Configured route fleet (12 routes, env-overridable). Tests use
    these to drive every endpoint that requires a ``route_code``."""
    codes = list(get_route_codes())
    assert codes, "Route registry returned empty list -- check YF_ROUTE_CODES env"
    return codes


@pytest.fixture(scope="session")
def primary_route(routes: list[str]) -> str:
    """First route in the registry. Stable across runs; tests that need
    a single well-known route use this so failures point at a specific
    route's data path rather than a random one."""
    return routes[0]


@pytest.fixture(scope="session")
def today() -> str:
    """ISO date for today (server-local). Most endpoints accept today's
    date because journey plans + recommendations are populated daily."""
    return date.today().strftime("%Y-%m-%d")


@pytest.fixture(scope="session")
def yesterday() -> str:
    """ISO date for yesterday. Used for past-performance and carry-chain
    cross-checks where today's row may not exist yet (early-morning runs)."""
    return (date.today() - timedelta(days=1)).strftime("%Y-%m-%d")


@pytest.fixture(scope="session")
def far_past() -> str:
    """ISO date ~120 days back. Still within the engine's data horizon
    so the response should be empty-but-2xx; recent enough to avoid the
    pre-system-history corner case that's a separate known issue."""
    return (date.today() - timedelta(days=120)).strftime("%Y-%m-%d")


@pytest.fixture(scope="session")
def pre_system_date() -> str:
    """ISO date that predates the system entirely (year 2020). Used by
    a small number of tests that explicitly want to probe the
    ancient-date corner case; most edge-case tests should use
    ``far_past`` instead so they don't fail on a known corner."""
    return "2020-01-01"


@pytest.fixture(scope="session")
def far_future() -> str:
    """ISO date guaranteed to postdate forecast horizon. Edge-case
    counterpart to ``far_past``."""
    return (date.today() + timedelta(days=365)).strftime("%Y-%m-%d")


@pytest.fixture(scope="session")
def unknown_route() -> str:
    """A route_code that doesn't exist in the fleet. Endpoints should
    handle this gracefully (empty result or 404), never 500."""
    return "0000"


# ---------------------------------------------------------------------------
# Optional DB connection -- auto-skip when creds missing
# ---------------------------------------------------------------------------


def _has_db_creds() -> bool:
    """True only when the AIML DB env vars are populated. Used by the
    ``requires_db`` marker so the suite cleanly skips DB cross-checks
    when running in an environment without warehouse access."""
    return bool(os.environ.get("SS_DB_HOST") and os.environ.get("SS_DB_USERNAME"))


@pytest.fixture(scope="session")
def db_conn():
    """Read-only pyodbc connection to YaumiAIML. Tests use this to
    cross-check the persisted state against API responses (e.g. is the
    DB row for a saved visit actually present?).

    Skips the test if creds aren't configured."""
    if not _has_db_creds():
        pytest.skip("AIML DB credentials not set; SS_DB_HOST/SS_DB_USERNAME missing")
    import pyodbc
    from sales_supervision.config.settings import get_settings
    s = get_settings()
    conn = pyodbc.connect(s.db.connection_string(), timeout=15)
    try:
        yield conn
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Auto-skip hooks
# ---------------------------------------------------------------------------


def pytest_collection_modifyitems(config, items):
    """Honour ``requires_db`` / ``requires_live_data`` markers without
    forcing every test author to repeat the ``pytest.skip`` boilerplate.
    """
    skip_db = pytest.mark.skip(reason="AIML DB creds missing (SS_DB_HOST/SS_DB_USERNAME)")
    has_db = _has_db_creds()
    for item in items:
        if "requires_db" in item.keywords and not has_db:
            item.add_marker(skip_db)
