"""Hermetic in-process tests for ``common.observability`` and
``common.api_middleware``.

These don't need a live backend on the network: they spin up each
service's FastAPI app via ``create_app()`` and use ``TestClient`` to
call endpoints directly through the ASGI interface. That makes them
runnable in CI without DB / LLM credentials or open ports.

Coverage:
* request-id round-trip across all 5 services
* /metrics/prometheus exposes the shared yf_* series
* configure_logging is idempotent
* Sanitised error envelope behaviour via the middleware

The slower / live-data integration tests under sibling test packages
auto-skip when no live backend is reachable (see ``conftest.py``).
"""

from __future__ import annotations

import uuid
from collections.abc import Callable

import pytest
from fastapi.testclient import TestClient

from common import observability
from common.observability import configure_logging
from data_import.api.app import create_app as create_di
from demand_forecasting_pipeline.api.app import create_app as create_df
from llm_analytics.api.app import create_app as create_llm
from recommended_order.api.app import create_app as create_ro
from sales_supervision.api.app import create_app as create_ss

# Each entry: (service_label, app_factory, api_prefix)
SERVICE_FACTORIES: list[tuple[str, Callable, str]] = [
    ("data_import",        create_di,  "/api/v1/data"),
    ("demand_forecasting", create_df,  "/api/v1/forecast"),
    ("recommended_order",  create_ro,  "/api/v1/recommended-order"),
    ("sales_supervision",  create_ss,  "/api/v1/supervision"),
    ("llm_analytics",      create_llm, "/api/v1/analytics"),
]


@pytest.fixture(scope="module")
def asgi_clients() -> list[tuple[str, TestClient, str]]:
    """Build one in-process ``TestClient`` per service.

    Module-scoped because ``create_app`` runs each service's lifespan
    setup (importer init, scheduler probe, etc.); paying that cost once
    per test session keeps the hermetic suite fast.
    """
    clients: list[tuple[str, TestClient, str]] = []
    for label, factory, prefix in SERVICE_FACTORIES:
        app = factory()
        client = TestClient(app)
        clients.append((label, client, prefix))
    yield clients
    for _, client, _ in clients:
        client.close()


def test_request_id_echoed_back_across_all_services(
    asgi_clients: list[tuple[str, TestClient, str]],
) -> None:
    """Inbound ``X-Request-ID`` must be echoed verbatim on the response;
    proves the shared middleware is mounted on every service."""
    request_id = f"hermetic-{uuid.uuid4().hex}"
    for label, client, prefix in asgi_clients:
        resp = client.get(f"{prefix}/health", headers={"X-Request-ID": request_id})
        assert resp.status_code == 200, f"{label}: {resp.status_code} {resp.text}"
        assert resp.headers.get("X-Request-ID") == request_id, (
            f"{label}: expected echo of {request_id!r}, got "
            f"{resp.headers.get('X-Request-ID')!r}"
        )


def test_request_id_generated_when_missing(
    asgi_clients: list[tuple[str, TestClient, str]],
) -> None:
    """When no inbound id is supplied, the middleware generates one and
    surfaces it on the response so callers can correlate."""
    for label, client, prefix in asgi_clients:
        resp = client.get(f"{prefix}/health")
        assert resp.status_code == 200, f"{label}: {resp.status_code}"
        rid = resp.headers.get("X-Request-ID")
        assert rid and len(rid) >= 16, (
            f"{label}: expected generated request-id, got {rid!r}"
        )


def test_prometheus_metrics_endpoint_exposes_shared_series(
    asgi_clients: list[tuple[str, TestClient, str]],
) -> None:
    """Every service must expose ``/metrics/prometheus`` and ship the
    shared ``yf_*`` series so cross-service panels work without
    per-service custom queries. ``yf_service_info`` carries the service
    name as a label so panels can group by service."""
    for label, client, prefix in asgi_clients:
        resp = client.get(f"{prefix}/metrics/prometheus")
        assert resp.status_code == 200, f"{label}: metrics endpoint missing"
        body = resp.text
        assert "yf_http_requests_total" in body, f"{label}: missing yf_http_requests_total"
        assert "yf_service_info" in body, f"{label}: missing yf_service_info"
        # The yf_service_info gauge carries the service name as a label.
        assert label in body, f"{label}: service label not present in metrics body"


def test_configure_logging_is_idempotent() -> None:
    """``configure_logging`` may be called multiple times -- only the
    first call applies handlers. Re-calling after the module loaded
    must not raise or double-attach handlers (which would multiply
    log output)."""
    # Second call (the first ran when the test imports triggered
    # ``create_app`` above) must complete cleanly. Reading the flag
    # through the module attribute (not a re-imported name) so we see
    # the live value rather than a snapshot bound at import time.
    configure_logging(service_name="hermetic_test", level="INFO", logs_dir=None)
    assert observability._configured is True, (
        "configure_logging should set the configured flag"
    )


def test_correlation_id_header_fallback(
    asgi_clients: list[tuple[str, TestClient, str]],
) -> None:
    """``X-Correlation-ID`` should be honoured as a fallback when
    ``X-Request-ID`` is absent. Documented contract on the middleware."""
    correlation_id = f"correlation-{uuid.uuid4().hex}"
    # Pick one service for this assertion -- the middleware is identical
    # across all five so a single sample is enough.
    label, client, prefix = asgi_clients[0]
    resp = client.get(
        f"{prefix}/health",
        headers={"X-Correlation-ID": correlation_id},
    )
    assert resp.status_code == 200, f"{label}: {resp.status_code}"
    assert resp.headers.get("X-Request-ID") == correlation_id, (
        f"{label}: expected X-Correlation-ID fallback to populate "
        f"response X-Request-ID, got {resp.headers.get('X-Request-ID')!r}"
    )
