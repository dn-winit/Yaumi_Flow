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
    total_pairs: int = 0
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
    error: Optional[str] = None
    result: Dict[str, Any] = {}


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
    accuracy_pct: float
    total_pairs: int
    classes: Dict[str, int]
    test_predictions_count: int
    future_forecast_count: int
    last_forecast_date: Optional[str] = None
    training_summary_exists: bool
    training_overview: Optional[Dict[str, Any]] = None
