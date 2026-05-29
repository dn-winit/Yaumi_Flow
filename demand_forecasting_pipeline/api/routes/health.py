"""Health check endpoints.

/health/live  -- K8s liveness; always 200 if process alive.
/health/ready -- K8s readiness; 200 only when DB pings, data_import, artifacts,
                 last-train freshness all green. 503 with detail body otherwise.
/health       -- legacy summary endpoint (200 unless dead) for back-compat.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Any

import httpx
from fastapi import APIRouter, Depends, Response

from common.db_pool import get_pool
from demand_forecasting_pipeline.api.dependencies import (
    get_artifact_service,
    get_pipeline_service,
)
from demand_forecasting_pipeline.api.schemas import ArtifactStatus, HealthResponse
from demand_forecasting_pipeline.config.settings import Settings, get_settings
from demand_forecasting_pipeline.services.artifact_service import ArtifactService
from demand_forecasting_pipeline.services.pipeline_service import PipelineService

router = APIRouter(tags=["health"])


def _ping_db(conn_str: str, *, timeout: int) -> dict[str, Any]:
    """Short-timeout SELECT 1; returns structured status, never raises."""
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
    """Hit data_import liveness; empty base_url -> skip. Only 2xx counts as healthy
    (4xx means reachable but mis-routed -- must flip readiness to 503)."""
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
    """Seconds since last successful training (from trained_at in training_summary.json);
    available=False on missing artifact so fresh deployments can pass."""
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
            when = when.replace(tzinfo=UTC)
        age = (datetime.now(UTC) - when).total_seconds()
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
    """Runtime-health summary; reflects RUNTIME readiness only, NOT training state.
    Missing artifacts shown for info; use /health/training-state for training status.
    """
    settings = get_settings()
    artifacts = art_svc.check_artifacts()

    statuses = pipe_svc.get_all_status()
    pipe_summary = {k: dict(v).get("status", "unknown") for k, v in statuses.items()}
    # Only ``error`` is fatal; idle/running/success/unknown are all healthy.
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
    """Has training run, is it fresh? Separated from /health so cold-start deploys
    don't look degraded. status: trained / partial / untrained."""
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
    """K8s liveness probe; 200 if process is alive. Never fails on downstream issues."""
    return {
        "status": "alive",
        "timestamp": datetime.now(UTC).isoformat(),
    }


@router.get("/health/ready")
def readiness(
    response: Response,
    art_svc: ArtifactService = Depends(get_artifact_service),
    pipe_svc: PipelineService = Depends(get_pipeline_service),
) -> dict[str, Any]:
    """K8s readiness; 200 when every critical dep is green, else 503 with detail body."""
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
        # Fresh deploys with no trained model still pass ready (informational once trained).
        and (not train_age.get("available") or train_age.get("fresh", True))
    )

    body: dict[str, Any] = {
        "status": "ready" if must_be_green else "not_ready",
        "timestamp": datetime.now(UTC).isoformat(),
        "checks": checks,
    }
    response.status_code = 200 if must_be_green else 503
    return body
