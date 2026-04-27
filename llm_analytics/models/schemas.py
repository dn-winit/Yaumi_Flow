"""
Domain models for LLM analysis responses -- validated via Pydantic.
"""

from __future__ import annotations

from typing import List

from pydantic import BaseModel, Field


class CustomerAnalysis(BaseModel):
    customer_code: str = ""
    performance_summary: str = ""
    supervisor_instructions: List[str] = Field(default_factory=list)
    strengths: List[str] = Field(default_factory=list)
    weaknesses: List[str] = Field(default_factory=list)


class RouteAnalysis(BaseModel):
    route_code: str = ""
    route_summary: str = ""
    supervisor_priorities: List[str] = Field(default_factory=list)
    high_performers_with_practices: List[str] = Field(default_factory=list)
    critical_issues: List[str] = Field(default_factory=list)


class PreVisitBriefing(BaseModel):
    briefing: str = ""
    key_items: List[str] = Field(default_factory=list)
    heads_up: str = ""
