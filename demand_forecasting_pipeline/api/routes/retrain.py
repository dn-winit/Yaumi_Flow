"""Auto-retrain configuration, history, and versioning endpoints."""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from demand_forecasting_pipeline.api.dependencies import (
    get_accuracy_service,
    get_artifact_service,
    get_retrain_config,
)
from demand_forecasting_pipeline.api.schemas import (
    ModelVersionEntry,
    ModelVersionsResponse,
    RetrainConfigResponse,
    RetrainHistoryResponse,
    RollbackRequest,
    RollbackResponse,
)
from demand_forecasting_pipeline.services.accuracy_service import AccuracyService
from demand_forecasting_pipeline.services.artifact_service import ArtifactService
from demand_forecasting_pipeline.services.model_registry import get_model_registry
from demand_forecasting_pipeline.services.retrain_scheduler import (
    AutoRetrainConfig,
    compute_drift_status,
)

router = APIRouter(prefix="/retrain", tags=["retrain"])


class RetrainConfigUpdate(BaseModel):
    enabled: Optional[bool] = None
    frequency_days: Optional[int] = None
    auto_inference_after_train: Optional[bool] = None


@router.get("/config", response_model=RetrainConfigResponse)
def get_config(
    cfg: AutoRetrainConfig = Depends(get_retrain_config),
    artifact_svc: ArtifactService = Depends(get_artifact_service),
    accuracy_svc: AccuracyService = Depends(get_accuracy_service),
):
    """Return current retrain config plus live drift status."""
    data = cfg.get()
    drift = compute_drift_status(artifact_svc, accuracy_svc)
    return RetrainConfigResponse(**data, drift=drift)


@router.post("/config", response_model=RetrainConfigResponse)
def update_config(
    body: RetrainConfigUpdate,
    cfg: AutoRetrainConfig = Depends(get_retrain_config),
    artifact_svc: ArtifactService = Depends(get_artifact_service),
    accuracy_svc: AccuracyService = Depends(get_accuracy_service),
):
    """Partial update of retrain settings; returns the GET /config envelope."""
    cfg.update_settings(
        enabled=body.enabled,
        frequency_days=body.frequency_days,
        auto_inference_after_train=body.auto_inference_after_train,
    )
    data = cfg.get()
    drift = compute_drift_status(artifact_svc, accuracy_svc)
    return RetrainConfigResponse(**data, drift=drift)


@router.get("/history", response_model=RetrainHistoryResponse)
def get_history(cfg: AutoRetrainConfig = Depends(get_retrain_config)):
    """Return retrain history array."""
    return RetrainHistoryResponse(history=cfg.get().get("history", []))


# Model versioning -- snapshot registry + rollback. Each auto-retrain snapshots
# artifacts into versions/<version_id>/ and updates current.json.

@router.get("/versions", response_model=ModelVersionsResponse)
def list_versions():
    """List retained model versions; current floated to top, rest newest-first."""
    registry = get_model_registry()
    versions = registry.list_versions()
    return ModelVersionsResponse(
        total=len(versions),
        current_version_id=registry.current_version_id(),
        versions=[ModelVersionEntry(**v.to_dict()) for v in versions],
    )


@router.post("/rollback", response_model=RollbackResponse)
def rollback(
    body: RollbackRequest,
    artifact_svc: ArtifactService = Depends(get_artifact_service),
):
    """Restore artifacts from a stored version; invalidates the artifact cache.

    400 on malformed version_id, 404 if unknown, 500 on I/O failure. Pointer is
    only updated on successful restore so previous version stays live on failure.
    """
    registry = get_model_registry()
    try:
        restored = registry.rollback(body.version_id, promoted_by="manual-rollback")
    except ValueError as exc:
        # Malformed version_id (path traversal, empty, too long) -- 400 not 404.
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        )
    except FileNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        )
    except OSError as exc:
        # Snapshot found but restore copy failed; pointer untouched so previous version remains active.
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"rollback I/O failure: {exc}",
        )
    # Hard cache reset; mtime-keyed cache would catch up but explicit invalidation guarantees next read serves restored files.
    artifact_svc.invalidate_cache()
    return RollbackResponse(
        success=True,
        restored=ModelVersionEntry(**restored.to_dict()),
    )
