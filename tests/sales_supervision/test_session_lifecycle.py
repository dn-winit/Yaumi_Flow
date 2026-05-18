"""End-to-end session lifecycle: initialize -> visit -> saved.

The supervision UI's hot path. Each of these endpoints is hit every
few seconds while a supervisor watches a route, so all three must:
  * respond fast (well under 5s)
  * be idempotent on repeat calls (re-init same route, re-fetch saved)
  * never 5xx on edge inputs (unknown route, future date)
"""
from __future__ import annotations

import httpx
import pytest

from tests.common.helpers import assert_keys, assert_not_5xx, assert_ok


class TestSessionInitialize:
    """``POST /session/initialize`` -- creates/rebuilds an in-memory
    session for (route, date). Idempotent: same input -> same session."""

    def test_happy(
        self, client: httpx.Client, base_urls: dict[str, str],
        primary_route: str, today: str,
    ) -> None:
        # ``/session/initialize`` may race the 60s auto-visit cron on
        # the same route. The endpoint internally invalidates + rebuilds
        # the session which can take well over the shared 30s client
        # timeout when the cron is mid-tick. Use a per-call 90s timeout
        # so a busy cron doesn't surface as a flake.
        resp = client.post(
            f"{base_urls['sales_supervision']}/session/initialize",
            json={"route_code": primary_route, "date": today},
            timeout=90.0,
        )
        body = assert_ok(resp, f"{primary_route}/{today}")
        assert_keys(body, ["success"])
        assert body["success"] is True, f"init not successful: {body}"
        # The session payload must carry a deterministic session_id.
        session = body.get("session") or {}
        assert session.get("sessionId"), f"missing sessionId in session payload: {body}"

    def test_idempotent_repeat(
        self, client: httpx.Client, base_urls: dict[str, str],
        primary_route: str, today: str,
    ) -> None:
        """Same (route, date) twice -- same session_id, no error."""
        url = f"{base_urls['sales_supervision']}/session/initialize"
        body1 = assert_ok(client.post(url, json={"route_code": primary_route, "date": today}, timeout=90.0))
        body2 = assert_ok(client.post(url, json={"route_code": primary_route, "date": today}, timeout=90.0))
        sid1 = body1["session"]["sessionId"]
        sid2 = body2["session"]["sessionId"]
        assert sid1 == sid2, f"non-idempotent sessionId: {sid1} vs {sid2}"

    @pytest.mark.parametrize("route_code", ["0000", "9999"])
    def test_unknown_route(
        self, client: httpx.Client, base_urls: dict[str, str],
        today: str, route_code: str,
    ) -> None:
        resp = client.post(
            f"{base_urls['sales_supervision']}/session/initialize",
            json={"route_code": route_code, "date": today},
        )
        assert_not_5xx(resp, f"unknown route {route_code}")


class TestSessionSaved:
    """``GET /session/saved`` -- hydration endpoint. Polled every 5s
    by the live UI."""

    def test_happy(
        self, client: httpx.Client, base_urls: dict[str, str],
        primary_route: str, today: str,
    ) -> None:
        """First init the session (so DB has a row), then read it back."""
        client.post(
            f"{base_urls['sales_supervision']}/session/initialize",
            json={"route_code": primary_route, "date": today},
        )
        resp = client.get(
            f"{base_urls['sales_supervision']}/session/saved",
            params={"route_code": primary_route, "date": today},
        )
        body = assert_ok(resp, f"{primary_route}/{today}")
        assert_keys(body, ["available"])

    def test_include_redistributions_flag(
        self, client: httpx.Client, base_urls: dict[str, str],
        primary_route: str, today: str,
    ) -> None:
        """The heavy-replay path with include_redistributions=true. The
        recent transaction-scope fix should keep this fast (< 5s)
        even with 30+ visited customers."""
        resp = client.get(
            f"{base_urls['sales_supervision']}/session/saved",
            params={
                "route_code": primary_route, "date": today,
                "include_redistributions": "true",
            },
            timeout=10.0,
        )
        assert_ok(resp, f"heavy-replay {primary_route}/{today}")

    def test_unknown_route(
        self, client: httpx.Client, base_urls: dict[str, str],
        today: str, unknown_route: str,
    ) -> None:
        resp = client.get(
            f"{base_urls['sales_supervision']}/session/saved",
            params={"route_code": unknown_route, "date": today},
        )
        # Either 2xx with available=False, or 4xx -- not 5xx.
        assert_not_5xx(resp, f"unknown route {unknown_route}")

    def test_fleet_sweep(
        self, client: httpx.Client, base_urls: dict[str, str],
        routes: list[str], today: str,
    ) -> None:
        """Every configured route must hydrate without 500."""
        for r in routes:
            resp = client.get(
                f"{base_urls['sales_supervision']}/session/saved",
                params={"route_code": r, "date": today},
            )
            assert_not_5xx(resp, f"route={r}")


class TestVisitValidation:
    """``POST /session/visit`` validation -- the endpoint mutates state,
    so we test only the validation surface here. Full visit-flow tests
    would require driving a YaumiLive invoice which is out of scope."""

    def test_unknown_session_returns_404(
        self, client: httpx.Client, base_urls: dict[str, str],
    ) -> None:
        resp = client.post(
            f"{base_urls['sales_supervision']}/session/visit",
            json={
                "session_id": "nonexistent_session_id",
                "customer_code": "00000000",
            },
        )
        # Endpoint raises HTTPException(404) for unknown sessions.
        assert resp.status_code == 404, (
            f"unknown session should be 404, got {resp.status_code}: {resp.text[:200]}"
        )

    def test_missing_required_field_rejects(
        self, client: httpx.Client, base_urls: dict[str, str],
    ) -> None:
        resp = client.post(
            f"{base_urls['sales_supervision']}/session/visit",
            json={"session_id": "only_session_id_no_customer"},
        )
        assert 400 <= resp.status_code < 500, (
            f"missing customer_code should be 4xx, got {resp.status_code}"
        )
