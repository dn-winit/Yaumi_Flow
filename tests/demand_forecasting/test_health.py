"""Health surface for demand_forecasting (8002).

Three endpoints, distinct contracts:
  * /health      -- legacy always-200 summary
  * /health/live -- k8s liveness, always 200 if process is alive
  * /health/ready -- k8s readiness, 503 when any critical dep is red
"""
from __future__ import annotations

import httpx

from tests.common.helpers import assert_ok


def test_health_returns_2xx(client: httpx.Client, base_urls: dict[str, str]) -> None:
    """Legacy summary endpoint -- always 200, body carries the degraded
    state in a structured form."""
    body = assert_ok(client.get(f"{base_urls['demand_forecasting']}/health"))
    assert "status" in body, f"missing status: {body}"
    assert "artifacts" in body, f"missing artifacts: {body}"


def test_health_live_is_200(client: httpx.Client, base_urls: dict[str, str]) -> None:
    """K8s liveness probe -- must never report unhealthy on dep issues."""
    resp = client.get(f"{base_urls['demand_forecasting']}/health/live")
    assert resp.status_code == 200, f"liveness should always 200, got {resp.status_code}"
    body = resp.json()
    assert body.get("status") == "alive", f"expected status=alive, got {body}"


def test_health_ready_returns_structured_body(
    client: httpx.Client, base_urls: dict[str, str],
) -> None:
    """Readiness can be 200 or 503; either way the body must carry the
    per-dependency check breakdown so failures are debuggable."""
    resp = client.get(f"{base_urls['demand_forecasting']}/health/ready")
    assert resp.status_code in (200, 503), (
        f"readiness should be 200 or 503, got {resp.status_code}"
    )
    body = resp.json()
    assert "checks" in body, f"readiness missing 'checks' breakdown: {body}"
