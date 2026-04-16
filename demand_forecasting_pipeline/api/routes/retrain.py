"""
Auto-retrain configuration and history endpoints.
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from demand_forecasting_pipeline.api.dependencies import (
    get_accuracy_service,
    get_artifact_service,
    get_retrain_config,
)
from demand_forecasting_pipeline.services.accuracy_service import AccuracyService
from demand_forecasting_pipeline.services.artifact_service import ArtifactService
from demand_forecasting_pipeline.services.retrain_scheduler import (
    AutoRetrainConfig,
    compute_drift_status,
)

router = APIRouter(prefix="/retrain", tags=["retrain"])


class RetrainConfigUpdate(BaseModel):
    enabled: Optional[bool] = None
    frequency_days: Optional[int] = None
    auto_inference_after_train: Optional[bool] = None


@router.get("/config")
def get_config(
    cfg: AutoRetrainConfig = Depends(get_retrain_config),
    artifact_svc: ArtifactService = Depends(get_artifact_service),
    accuracy_svc: AccuracyService = Depends(get_accuracy_service),
):
    """Return current retrain config plus live drift status."""
    data = cfg.get()
    drift = compute_drift_status(artifact_svc, accuracy_svc)
    return {**data, "drift": drift}


@router.post("/config")
def update_config(
    body: RetrainConfigUpdate,
    cfg: AutoRetrainConfig = Depends(get_retrain_config),
):
    """Update retrain settings (partial update)."""
    return cfg.update_settings(
        enabled=body.enabled,
        frequency_days=body.frequency_days,
        auto_inference_after_train=body.auto_inference_after_train,
    )


@router.get("/history")
def get_history(cfg: AutoRetrainConfig = Depends(get_retrain_config)):
    """Return retrain history array."""
    return cfg.get().get("history", [])


@router.get("/drift")
def get_drift(
    artifact_svc: ArtifactService = Depends(get_artifact_service),
    accuracy_svc: AccuracyService = Depends(get_accuracy_service),
):
    """Return live drift analysis (predicted vs YaumiLive actuals)."""
    return compute_drift_status(artifact_svc, accuracy_svc)
