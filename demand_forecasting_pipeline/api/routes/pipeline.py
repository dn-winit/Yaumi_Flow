from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Body, Depends

from demand_forecasting_pipeline.api.dependencies import (
    get_artifact_service,
    get_pipeline_service,
)
from demand_forecasting_pipeline.api.schemas import (
    PipelineRunRequest,
    PipelineRunResponse,
    PipelineStatusResponse,
    ResolvedPipelineStatus,
    ResolvedStep,
)
from demand_forecasting_pipeline.config.settings import get_settings
from demand_forecasting_pipeline.services.artifact_service import ArtifactService
from demand_forecasting_pipeline.services.pipeline_service import PipelineService


def _display_tz() -> ZoneInfo:
    """Display TZ from settings; resolved per call so env overrides take effect live."""
    return ZoneInfo(get_settings().log_timezone)

router = APIRouter(prefix="/pipeline", tags=["pipeline"])

# Pipeline page step strip; ``pipeline_keys`` match the step ids ``on_step`` emits.
# Order mirrors the legacy frontend STEPS table.
_STEPS: list[dict[str, Any]] = [
    {"key": "collection",     "name": "Data collection",       "pipeline_keys": ("data_collection", "collection")},
    {"key": "processing",     "name": "Data processing",       "pipeline_keys": ("data_processing", "processing")},
    {"key": "classification", "name": "Demand classification", "pipeline_keys": ("classification", "demand_classification")},
    {"key": "features",       "name": "Feature engineering",   "pipeline_keys": ("feature_engineering", "features")},
    {"key": "training",       "name": "Model training",        "pipeline_keys": ("training", "train")},
    {"key": "forecast",       "name": "Forecast generation",   "pipeline_keys": ("inference", "forecast")},
    {"key": "publish",        "name": "Publishing results",    "pipeline_keys": ("db_push", "data_import_refresh")},
]

_STATUS_MAP: dict[str, str] = {
    "completed": "completed",
    "success":   "completed",
    "running":   "running",
    "pending":   "running",
    "failed":    "failed",
    "error":     "failed",
    "skipped":   "skipped",
}

# Worst-of-two priority for the publish substeps.
_PUBLISH_PRIORITY: dict[str, int] = {
    "failed":    4,
    "running":   3,
    "completed": 2,
    "skipped":   1,
    "idle":      0,
}


def _map_status(raw: str | None) -> str | None:
    if not raw:
        return None
    return _STATUS_MAP.get(str(raw).lower())


def _fmt_int(n: Any) -> str:
    """Comma-grouped integer (1234 -> '1,234')."""
    try:
        return f"{int(n):,}"
    except (TypeError, ValueError):
        return str(n)


def _fmt_date(iso: str | None) -> str | None:
    if not iso:
        return None
    s = str(iso).strip()
    if not s:
        return None
    # Accept YYYY-MM-DD or ISO datetime; convert TZ-aware inputs first to avoid calendar-roll.
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is not None:
            dt = dt.astimezone(_display_tz())
        return dt.strftime("%d %b %Y")
    except ValueError:
        return s[:10]


def _fmt_datetime(iso: str | None) -> str | None:
    """ISO -> ``DD MMM YYYY, HH:MM`` in display TZ; naive input rendered as-is."""
    if not iso:
        return None
    s = str(iso).strip()
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return s
    if dt.tzinfo is not None:
        dt = dt.astimezone(_display_tz())
    return dt.strftime("%d %b %Y, %H:%M")


def _fmt_duration(seconds: float | None) -> str:
    """``Xs`` / ``Xm`` / ``Xm Ys`` -- mirrors the frontend helper."""
    if seconds is None or seconds <= 0:
        return "0s"
    n = float(seconds)
    if n < 60:
        return f"{int(round(n))}s"
    m = int(n // 60)
    s = int(round(n % 60))
    return f"{m}m {s}s" if s > 0 else f"{m}m"


def _resolve_step(
    step: dict[str, Any],
    statuses: dict[str, dict[str, Any]],
) -> tuple[str, dict[str, Any] | None]:
    """First matching step status; returns (status, parent_run_info)."""
    pipeline_keys: tuple[str, ...] = step["pipeline_keys"]
    for pipeline in ("train", "inference"):
        run = statuses.get(pipeline)
        if not run or not run.get("steps"):
            continue
        for k in pipeline_keys:
            mapped = _map_status(run["steps"].get(k))
            if mapped:
                return mapped, run
    # Fallback: top-level pipeline status (idle pipelines).
    for k in pipeline_keys:
        match = statuses.get(k)
        if not match:
            continue
        return _map_status(match.get("status")) or "idle", match
    return "idle", None


def _resolve_publish(
    statuses: dict[str, dict[str, Any]],
) -> tuple[str, dict[str, Any] | None, dict[str, Any]]:
    """Most recent run owns publish display; worst-of-two over cascade substeps."""
    candidates = [
        statuses[p]
        for p in ("train", "inference")
        if statuses.get(p) and statuses[p].get("started_at")
    ]
    candidates.sort(key=lambda r: str(r.get("started_at") or ""), reverse=True)
    if not candidates:
        return "idle", None, {}
    latest = candidates[0]
    sub_keys = ("db_push", "data_import_refresh")
    sub_statuses = [_map_status((latest.get("steps") or {}).get(k)) or "idle" for k in sub_keys]
    status = "idle"
    for s in sub_statuses:
        if _PUBLISH_PRIORITY[s] > _PUBLISH_PRIORITY[status]:
            status = s
    cascade = (latest.get("result") or {}).get("cascade") or {}
    return status, latest, cascade


def _step_metric_text(
    step_key: str,
    summary: dict[str, Any] | None,
    publish_status: str,
    cascade_text: str,
) -> str | None:
    """Plain-language metric line per phase; mirrors the legacy stepMetric() switch."""
    summary = summary or {}
    overview = summary.get("training_overview") or {}

    if step_key == "collection":
        pairs = summary.get("total_pairs")
        routes = overview.get("test_routes")
        show_pairs = isinstance(pairs, (int, float)) and pairs > 0
        show_routes = isinstance(routes, (int, float)) and routes > 0
        if not show_pairs and not show_routes:
            return None
        parts: list[str] = []
        if show_pairs:
            parts.append(f"{_fmt_int(pairs)} item-route pairs")
        if show_routes:
            parts.append(f"{_fmt_int(routes)} routes")
        return " - ".join(parts)

    if step_key == "processing":
        start = _fmt_date(overview.get("test_date_start"))
        end = _fmt_date(overview.get("test_date_end"))
        if not start or not end:
            return None
        return f"Reviewed {start} - {end}"

    if step_key == "classification":
        classes = summary.get("classes") or {}
        total = sum(int(v or 0) for v in classes.values())
        if total <= 0:
            return None
        return f"{_fmt_int(total)} items grouped by buying pattern"

    if step_key == "features":
        feats = overview.get("feature_count")
        if not feats or int(feats) <= 0:
            return None
        return f"{_fmt_int(feats)} predictive signals built"

    if step_key == "training":
        total = overview.get("total_models_trained")
        if not total or int(total) <= 0:
            return None
        winners = overview.get("class_winners") or []
        picks = len(winners) if isinstance(winners, list) else 0
        if picks > 0:
            return (
                f"{_fmt_int(total)} models compared - "
                f"best picked for each of {picks} patterns"
            )
        return f"{_fmt_int(total)} models compared"

    if step_key == "forecast":
        count = summary.get("future_forecast_count")
        if not count or int(count) <= 0:
            return None
        last = _fmt_date(summary.get("last_forecast_date"))
        return (
            f"{_fmt_int(count)} predictions through {last}"
            if last
            else f"{_fmt_int(count)} predictions"
        )

    if step_key == "publish":
        # Echo the cascade line for the metric slot when we have one.
        return cascade_text or None

    return None


def _step_detail_text(
    status: str,
    info: dict[str, Any] | None,
) -> str | None:
    """Static (non-running) detail line; running elapsed timer is client-side."""
    if status == "failed":
        msg = (info or {}).get("error")
        if not msg:
            return None
        m = str(msg).strip()
        return m if len(m) <= 80 else f"{m[:80]}..."

    if status == "running":
        # Client renders ``Running for Xs`` from ``started_at`` + tick.
        return None

    if status == "skipped":
        return None

    # completed / idle: finished_at falling back to started_at.
    info = info or {}
    ts = info.get("finished_at") or info.get("started_at")
    return _fmt_datetime(ts)


def _cascade_text(
    cascade: dict[str, Any],
    status: str,
) -> str:
    """One-line summary of the publish cascade for the publish row."""
    if status == "idle" or not cascade:
        return "Pending - runs after a forecast"
    db = cascade.get("db_push") or {}
    refresh = cascade.get("data_import_refresh") or {}
    if status == "running":
        db_running = (
            not db
            or (not db.get("success") and not db.get("skipped") and not db.get("error"))
        )
        return "Saving forecast to database..." if db_running else "Refreshing downstream apps..."
    if status == "failed":
        if db.get("error"):
            return "Database save failed"
        if refresh.get("error"):
            return "App refresh failed"
        return "Publishing failed"
    # completed
    rows = db.get("rows")
    parts: list[str] = []
    if rows is not None:
        parts.append(f"{_fmt_int(rows)} rows saved")
    if refresh.get("success"):
        parts.append("apps refreshed")
    elif refresh.get("skipped"):
        parts.append("downstream refresh skipped")
    return " - ".join(parts) if parts else "Publishing complete"


@router.post("/train", response_model=PipelineRunResponse)
def trigger_training(
    req: PipelineRunRequest = Body(default_factory=PipelineRunRequest),
    svc: PipelineService = Depends(get_pipeline_service),
):
    """Body optional; default_factory builds a fresh instance per request (no cross-request leak)."""
    result = svc.run_training(req.config_path)
    return PipelineRunResponse(**result)


@router.post("/inference", response_model=PipelineRunResponse)
def trigger_inference(
    req: PipelineRunRequest = Body(default_factory=PipelineRunRequest),
    svc: PipelineService = Depends(get_pipeline_service),
):
    result = svc.run_inference(req.config_path)
    return PipelineRunResponse(**result)


@router.get("/status", response_model=dict[str, PipelineStatusResponse])
def get_all_status(svc: PipelineService = Depends(get_pipeline_service)):
    """Bulk status -- one round-trip for every known pipeline name."""
    return svc.get_all_status()


@router.get("/resolved-status", response_model=ResolvedPipelineStatus)
def get_resolved_pipeline_status(
    pipeline_svc: PipelineService = Depends(get_pipeline_service),
    artifact_svc: ArtifactService = Depends(get_artifact_service),
) -> ResolvedPipelineStatus:
    """Pipeline page composite payload; page renders the list verbatim.

    Only client-side compute: per-second elapsed timer for a running step.
    """
    raw_statuses = pipeline_svc.get_all_status() or {}

    # training_summary powers the metric texts; composed inline so this endpoint owns its view.
    summary_payload: dict[str, Any] = {}
    try:
        ts = artifact_svc.get_training_summary() or {}
        cs = artifact_svc.get_class_summary() or {}
        future_count, last_forecast = artifact_svc.get_future_forecast_meta()
        summary_payload = {
            "training_overview": ts,
            "classes": cs.get("classes") or {},
            "total_pairs": cs.get("total_pairs"),
            "future_forecast_count": int(future_count or 0),
            "last_forecast_date": last_forecast,
        }
    except Exception:  # noqa: BLE001 -- summary is best-effort enrichment
        summary_payload = {}

    publish_status, publish_info, publish_cascade = _resolve_publish(raw_statuses)
    cascade_text = _cascade_text(publish_cascade, publish_status)

    steps_resolved: list[ResolvedStep] = []
    for step in _STEPS:
        if step["key"] == "publish":
            status = publish_status
            info = publish_info
        else:
            status, info = _resolve_step(step, raw_statuses)
        info = info or {}
        steps_resolved.append(
            ResolvedStep(
                key=step["key"],
                name=step["name"],
                status=status,
                started_at=info.get("started_at"),
                finished_at=info.get("finished_at"),
                error=info.get("error"),
                last_success_duration_seconds=info.get("last_success_duration_seconds"),
                metric_text=_step_metric_text(
                    step["key"], summary_payload, publish_status, cascade_text,
                ),
                detail_text=_step_detail_text(status, info),
            )
        )

    any_running = any(
        (run or {}).get("status") == "running" for run in raw_statuses.values()
    )

    return ResolvedPipelineStatus(
        success=True,
        any_running=any_running,
        steps=steps_resolved,
    )
