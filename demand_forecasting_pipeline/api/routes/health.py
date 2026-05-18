"""
Health check endpoints.

Three endpoints:
  * ``/health/live``  - always 200 if the process is alive. Kubernetes
                        liveness probe target. Should never fail because
                        of downstream dependencies (DB outages, cache
                        issues, etc.) - killing the pod won't fix those.
  * ``/health/ready`` - 200 only when every critical dependency is
                        green: DB write+read pings, data_import
                        reachability (when configured), artifact
                        presence, last-train freshness. Returns 503 with
                        a structured detail body otherwise. Kubernetes
                        readiness probe target.
  * ``/health``       - legacy summary endpoint (always 200, body shows
                        degraded fields). Kept so existing dashboards
                        and scripts don't break.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any

import httpx
from fastapi import APIRouter, Depends, Response

from demand_forecasting_pipeline.api.dependencies import (
    get_artifact_service,
    get_pipeline_service,
)
from demand_forecasting_pipeline.api.schemas import ArtifactStatus, HealthResponse
from demand_forecasting_pipeline.config.settings import Settings, get_settings
from demand_forecasting_pipeline.services.artifact_service import ArtifactService
from common.db_pool import get_pool
from demand_forecasting_pipeline.services.pipeline_service import PipelineService

router = APIRouter(tags=["health"])


def _ping_db(conn_str: str, *, timeout: int) -> dict[str, Any]:
    """Open a short-timeout connection and run ``SELECT 1``. Returns
    structured status; never raises."""
    if not conn_str:
        return {"ok": False, "reason": "not_configured"}
    started = time.perf_counter()
    try:
        pool = get_pool(
            conn_str,
            max_connections=2,
            connect_timeout=timeout,
            query_timeout=timeout,
            autocommit=True,
        )
        with pool.acquire() as conn:
            cur = conn.cursor()
            cur.execute("SELECT 1")
            cur.fetchone()
        return {
            "ok": True,
            "latency_ms": round((time.perf_counter() - started) * 1000.0, 1),
        }
    except Exception as exc:
        return {
            "ok": False,
            "reason": "db_error",
            "error": repr(exc),
            "latency_ms": round((time.perf_counter() - started) * 1000.0, 1),
        }


def _ping_data_import(base_url: str, *, timeout: float) -> dict[str, Any]:
    """Hit the data_import liveness endpoint. ``base_url`` empty -> skip.

    Only 2xx counts as healthy. 4xx means the upstream is reachable but
    rejecting the request (auth, missing endpoint, mis-routed proxy)
    which we MUST treat as not-ready - readiness must flip 503 so a
    misconfigured deployment can't silently serve stale data.
    """
    if not base_url:
        return {"ok": True, "skipped": True, "reason": "not_configured"}
    started = time.perf_counter()
    try:
        url = base_url.rstrip("/") + "/api/v1/data/health"
        resp = httpx.get(url, timeout=timeout)
        return {
            "ok": 200 <= resp.status_code < 300,
            "status": resp.status_code,
            "latency_ms": round((time.perf_counter() - started) * 1000.0, 1),
        }
    except Exception as exc:
        return {
            "ok": False,
            "reason": "http_error",
            "error": repr(exc),
            "latency_ms": round((time.perf_counter() - started) * 1000.0, 1),
        }


def _last_train_age(
    art_svc: ArtifactService, *, threshold_seconds: int,
) -> dict[str, Any]:
    """Seconds since the last successful training. Reads ``trained_at``
    from training_summary.json; returns ``available=False`` when the
    artifact is absent so callers can decide what to do (fresh
    deployments are legitimately empty)."""
    try:
        summary = art_svc.get_training_summary() or {}
    except Exception as exc:
        return {"available": False, "reason": "read_error", "error": repr(exc)}
    meta = summary.get("metadata") or {}
    ts = meta.get("trained_at")
    if not ts:
        return {"available": False, "reason": "no_metadata"}
    try:
        when = datetime.fromisoformat(str(ts))
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - when).total_seconds()
        return {
            "available": True,
            "trained_at": ts,
            "age_seconds": round(age, 1),
            "fresh": age <= threshold_seconds,
            "threshold_seconds": int(threshold_seconds),
        }
    except Exception as exc:
        return {"available": False, "reason": "bad_timestamp", "error": repr(exc)}


@router.get("/health", response_model=HealthResponse)
def health_check(
    art_svc: ArtifactService = Depends(get_artifact_service),
    pipe_svc: PipelineService = Depends(get_pipeline_service),
):
    """Runtime-health summary -- always 200 unless the process is dead.
    Kept for back-compat; new monitoring targets ``/health/live`` and
    ``/health/ready``.

    ``status`` reflects RUNTIME readiness (the predictions, page-views,
    and reconciliation surfaces can serve requests), NOT training-
    pipeline state. Missing model artifacts are listed in
    ``artifacts`` as informational state -- consumers that care about
    "has training ever completed" inspect that field or call
    ``/health/training-state`` (dedicated endpoint).

    Pre-split, this endpoint reported ``degraded`` whenever any of the
    seven training artifacts was missing -- which made every fresh
    deployment look broken in the dashboard even though predictions,
    reconciliation, and page-views were all healthy. Pipeline error
    statuses still mark the service degraded since those reflect
    runtime concerns.
    """
    settings = get_settings()
    artifacts = art_svc.check_artifacts()

    statuses = pipe_svc.get_all_status()
    pipe_summary = {k: dict(v).get("status", "unknown") for k, v in statuses.items()}
    # ``error`` is the only runtime-fatal pipeline state; ``idle``,
    # ``running``, ``success`` are all healthy. ``unknown`` is treated
    # as healthy because it's the legitimate "never ran" cold-start
    # state, not a failure signal.
    pipelines_runtime_ok = not any(
        s == "error" for s in pipe_summary.values()
    )

    return HealthResponse(
        status="healthy" if pipelines_runtime_ok else "degraded",
        artifacts=ArtifactStatus(**artifacts),
        pipelines=pipe_summary,
        config_path=settings.pipeline_config,
        cache_keys=art_svc.cache_keys,
    )


@router.get("/health/training-state")
def training_state(
    art_svc: ArtifactService = Depends(get_artifact_service),
) -> dict[str, Any]:
    """Dedicated endpoint for "has training run, and is it fresh?".
    Separated from ``/health`` so a fresh deployment with no trained
    model yet (legitimate cold-start) doesn't read as degraded in the
    runtime health dashboard.

    The retrain UI consumes this directly. ``status``:
      * ``trained``    -- all artifacts present
      * ``partial``    -- some artifacts present (mid-training, or
                          older training that doesn't produce every
                          modern artifact)
      * ``untrained``  -- no artifact present yet
    """
    artifacts = art_svc.check_artifacts()
    present = sum(1 for v in artifacts.values() if v)
    total = len(artifacts)
    if present == total:
        status = "trained"
    elif present == 0:
        status = "untrained"
    else:
        status = "partial"
    return {
        "status": status,
        "artifacts_present": present,
        "artifacts_total": total,
        "artifacts": artifacts,
    }


@router.get("/health/live")
def liveness() -> dict[str, Any]:
    """K8s liveness probe - if this returns 200, the process is alive
    enough to handle traffic eventually. Never reports unhealthy on
    downstream issues; killing the pod won't fix those."""
    return {
        "status": "alive",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


@router.get("/health/ready")
def readiness(
    response: Response,
    art_svc: ArtifactService = Depends(get_artifact_service),
    pipe_svc: PipelineService = Depends(get_pipeline_service),
) -> dict[str, Any]:
    """K8s readiness probe - 200 only when every critical dependency
    is green. Returns 503 with a detail body otherwise so the failure
    surface is debuggable from the response."""
    settings: Settings = get_settings()

    artifacts = art_svc.check_artifacts()
    artifacts_ok = all(artifacts.values())

    write_db = _ping_db(
        settings.db.connection_string() if settings.db.configured else "",
        timeout=settings.db.connection_timeout,
    )
    read_db = _ping_db(
        settings.live_connection_string() if settings.live_db_configured else "",
        timeout=settings.db.connection_timeout,
    )
    data_import = _ping_data_import(
        settings.data_import_url,
        timeout=min(
            settings.http_request_timeout_seconds,
            settings.health_probe_timeout_seconds,
        ),
    )
    train_age = _last_train_age(
        art_svc, threshold_seconds=settings.stale_train_threshold_seconds,
    )

    pipe_statuses = pipe_svc.get_all_status()
    pipelines_ok = all(
        dict(v).get("status") in ("idle", "success", "running")
        for v in pipe_statuses.values()
    )

    checks: dict[str, Any] = {
        "artifacts": {"ok": artifacts_ok, "present": artifacts},
        "db_write": write_db,
        "db_read": read_db,
        "data_import": data_import,
        "last_train": train_age,
        "pipelines": {
            "ok": pipelines_ok,
            "statuses": {
                k: dict(v).get("status", "unknown")
                for k, v in pipe_statuses.items()
            },
        },
    }

    must_be_green = (
        artifacts_ok
        and write_db.get("ok", False)
        and read_db.get("ok", False)
        and data_import.get("ok", False)
        and pipelines_ok
        # Train freshness is informational once any successful train
        # exists. A fresh deployment with no trained model yet still
        # passes ready -- /pipeline/train can be called regardless.
        and (not train_age.get("available") or train_age.get("fresh", True))
    )

    body: dict[str, Any] = {
        "status": "ready" if must_be_green else "not_ready",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "checks": checks,
    }
    response.status_code = 200 if must_be_green else 503
    return body
