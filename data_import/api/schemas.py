"""
API request/response schemas.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class ImportRequest(BaseModel):
    dataset: str = Field(description="customer_data | journey_plan | sales_recent")
    mode: str = Field(default="incremental", description="incremental | full")


class ImportAllRequest(BaseModel):
    mode: str = Field(default="incremental", description="incremental | full")


class ImportResponse(BaseModel):
    success: bool
    dataset: str = ""
    mode: str = ""
    new_rows: int = 0
    total_rows: int = 0
    file: str = ""
    duration_seconds: float = 0.0
    message: str = ""
    error: str = ""


class ImportAllResponse(BaseModel):
    success: bool
    results: Dict[str, Any]


class DatasetInfo(BaseModel):
    exists: bool
    rows: int = 0
    first_date: Optional[str] = None
    last_date: Optional[str] = None
    file: str = ""
    size_mb: float = 0.0


class StatusResponse(BaseModel):
    success: bool
    datasets: Dict[str, DatasetInfo]


class HealthResponse(BaseModel):
    status: str
    db_connected: bool
    data_dir: str
    datasets_available: int


class DataSummaryResponse(BaseModel):
    datasets: Dict[str, DatasetInfo]
    total_rows: int
    db_connected: bool
    last_updated: Optional[str] = None
