"""
Pydantic request/response schemas for the demand forecasting API.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


# ------------------------------------------------------------------
# Common
# ------------------------------------------------------------------

class PaginationParams(BaseModel):
    limit: int = Field(default=1000, ge=1, le=10000)
    offset: int = Field(default=0, ge=0)


# ------------------------------------------------------------------
# Predictions
# ------------------------------------------------------------------

class PredictionFilters(PaginationParams):
    route_code: Optional[str] = None
    item_code: Optional[str] = None


class PredictionResponse(BaseModel):
    success: bool
    source: str  # "test_predictions" or "future_forecast"
    total: int
    data: List[Dict[str, Any]]


# ------------------------------------------------------------------
# Metrics
# ------------------------------------------------------------------

class MetricsFilters(BaseModel):
    demand_class: Optional[str] = None


class MetricsResponse(BaseModel):
    success: bool
    total: int
    data: List[Dict[str, Any]]


# ------------------------------------------------------------------
# Training summary / Models
# ------------------------------------------------------------------

class TrainingSummaryResponse(BaseModel):
    success: bool
    data: Dict[str, Any]


class ModelFile(BaseModel):
    filename: str
    size_bytes: int
    modified: float
    type: str


class ModelFilesResponse(BaseModel):
    success: bool
    total: int
    files: List[ModelFile]


class PairModelLookupResponse(BaseModel):
    success: bool
    total: int
    data: List[Dict[str, Any]]


# ------------------------------------------------------------------
# Explainability
# ------------------------------------------------------------------

class ExplainabilityFilters(BaseModel):
    route_code: Optional[str] = None
    item_code: Optional[str] = None
    demand_class: Optional[str] = None


class ClassSummaryResponse(BaseModel):
    success: bool
    total_pairs: Optional[int] = None
    classes: Dict[str, int] = {}


class ExplainabilityResponse(BaseModel):
    success: bool
    total: int
    data: List[Dict[str, Any]]


# ------------------------------------------------------------------
# Pipeline
# ------------------------------------------------------------------

class PipelineRunRequest(BaseModel):
    config_path: Optional[str] = Field(default=None, description="Custom config.yaml path")


class PipelineRunResponse(BaseModel):
    success: bool
    message: str
    config: Optional[str] = None


class PipelineStatusResponse(BaseModel):
    pipeline: str
    status: str
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    duration_seconds: float = 0.0
    last_success_duration_seconds: Optional[float] = None
    error: Optional[str] = None
    result: Dict[str, Any] = {}
    steps: Dict[str, str] = {}


class FutureRouteSummaryRow(BaseModel):
    route_code: str
    skus: int = 0
    predicted_qty: float = 0.0
    peak_day: Optional[str] = None


class FutureRouteSummaryResponse(BaseModel):
    success: bool = True
    date: Optional[str] = None
    routes: List[FutureRouteSummaryRow] = []


# ------------------------------------------------------------------
# Health
# ------------------------------------------------------------------

class ArtifactStatus(BaseModel):
    test_predictions: bool = False
    future_forecast: bool = False
    model_metrics: bool = False
    training_summary: bool = False
    pair_model_lookup: bool = False
    pair_classes: bool = False
    pair_explainability: bool = False


class HealthResponse(BaseModel):
    status: str
    artifacts: ArtifactStatus
    pipelines: Dict[str, str]
    config_path: str
    cache_keys: List[str] = []


# ------------------------------------------------------------------
# Summary
# ------------------------------------------------------------------

class ForecastSummaryResponse(BaseModel):
    # ``accuracy_pct`` and ``total_pairs`` are intentionally nullable: a
    # numeric zero would render as "0%" / "0 pairs" in the UI, which is
    # misleading while training is still in flight. ``None`` lets the UI
    # show "—" until the artifacts that back these numbers actually exist.
    accuracy_pct: Optional[float] = None
    total_pairs: Optional[int] = None
    classes: Dict[str, int] = {}
    test_predictions_count: int = 0
    future_forecast_count: int = 0
    last_forecast_date: Optional[str] = None
    training_summary_exists: bool = False
    training_overview: Optional[Dict[str, Any]] = None


# ------------------------------------------------------------------
# Retrain config / history
# ------------------------------------------------------------------

class RetrainConfigResponse(BaseModel):
    """Mirrors AutoRetrainConfig._data + the live drift assessment.

    Field names match retrain_config.json on disk and the webapp's
    `RetrainConfig` interface so the JSON round-trips through the API
    without renaming.
    """
    enabled: bool = False
    frequency_days: int = 14
    auto_inference_after_train: bool = True
    last_auto_retrain: Optional[str] = None
    next_scheduled: Optional[str] = None
    history: List[Dict[str, Any]] = Field(default_factory=list)
    drift: Optional[Dict[str, Any]] = None

    model_config = {"extra": "ignore"}


class RetrainHistoryResponse(BaseModel):
    history: List[Dict[str, Any]] = Field(default_factory=list)
