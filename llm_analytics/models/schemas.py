"""
Domain models for LLM analysis responses -- validated via Pydantic.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

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


class PlanningInsights(BaseModel):
    route_summary: str = ""
    priority_customers: List[str] = Field(default_factory=list)
    van_load_alerts: List[str] = Field(default_factory=list)
    opportunities: List[str] = Field(default_factory=list)
    quick_tips: List[str] = Field(default_factory=list)


class PreVisitBriefing(BaseModel):
    briefing: str = ""
    key_items: List[str] = Field(default_factory=list)
    heads_up: str = ""
