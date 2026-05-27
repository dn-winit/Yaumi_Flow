from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class CustomerAnalysisRequest(BaseModel):
    customer_code: str
    route_code: str
    date: str
    current_items: List[Dict[str, Any]] = Field(description="Current visit items with actualQuantity/recommendedQuantity")
    performance_score: float = 0.0
    coverage: float = 0.0
    accuracy: float = 0.0


class RouteAnalysisRequest(BaseModel):
    route_code: str
    date: str
    visited_customers: List[Dict[str, Any]] = Field(description="Visited customer summaries")
    total_customers: int = 0
    total_actual: int = 0
    total_recommended: int = 0
    actual_customer_codes: Optional[List[str]] = None


class PreVisitRequest(BaseModel):
    customer_code: str
    customer_name: str = ""
    route_code: str
    date: str
    items: List[Dict[str, Any]] = Field(description="Recommendation items with qty, tier, source, whyItem")


class AnalysisResponse(BaseModel):
    success: bool
    analysis_type: str
    data: Dict[str, Any]


class HealthResponse(BaseModel):
    available: bool
    provider: str
    model: str
    cache: Dict[str, Any]
    prompts: List[str]


class CacheStatsResponse(BaseModel):
    hits: int
    misses: int
    hit_rate: float
    cached_entries: int


class CacheClearResponse(BaseModel):
    success: bool
    cleared: int = 0


class PreviewResponse(BaseModel):
    """Dry-run view of what the analyzer WOULD send to the LLM.

    Lets operators verify the prompt + data are correct without firing a
    paid call. ``estimated_input_tokens`` is a char/4 heuristic; actual
    tokens depend on the tokenizer but the order of magnitude is right.
    """
    artifact:                 str
    system_prompt:            str
    user_prompt:              str
    model:                    str
    provider:                 str
    max_tokens:               int
    estimated_input_tokens:   int
    schema:                   Dict[str, Any]


class CostSummaryResponse(BaseModel):
    """Aggregated LLM spend since ``since`` (defaults to start-of-day UTC)."""
    since:               str
    route_code:          Optional[str] = None
    total_calls:         int
    ok_calls:            int
    degraded_calls:      int
    cache_hits:          int
    cache_hit_rate:      float
    total_input_tokens:  int
    total_output_tokens: int
    total_tokens:        int
    total_elapsed_ms:    float
    avg_elapsed_ms:      float
    cost_usd:            float
    cost_display:        float
    display_currency:    str
    display_rate:        float
    per_artifact:        Dict[str, Dict[str, Any]]
    per_outcome:         Dict[str, int]
