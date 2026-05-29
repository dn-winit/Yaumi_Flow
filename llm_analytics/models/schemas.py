"""
Domain models for LLM analysis responses -- the single source of truth.

These models are BOTH (1) the post-call Pydantic validator and (2) the schema
description the LLM sees in its system prompt (rendered via
``llm_analytics.core.schema_render.render_schema_for_prompt``). Field
descriptions appear verbatim to the model -- write them as instructions, not
internal docstrings.

Refusal-phrase validators reject the silent-failure mode where the model
returns "I cannot..." text inside the narrative fields; Pydantic raises,
the analyzer's retry loop catches and re-prompts.
"""

from __future__ import annotations

import re

from pydantic import BaseModel, Field, field_validator

# Phrases that indicate the model refused or gave a non-answer. Tested
# case-insensitively at the START of narrative fields (a legitimate
# briefing wouldn't start with "I cannot..."). Centralised so all three
# narrative artifacts apply the same guard.
_REFUSAL_PREFIXES = (
    "i cannot",
    "i can't",
    "i can not",
    "i am unable",
    "i'm unable",
    "i apologize",
    "i'm sorry",
    "as an ai",
    "as a language model",
    "i don't have",
    "i do not have",
    "unable to provide",
    "analysis not available",
)


def _reject_refusal(value: str, *, min_length: int, field_name: str) -> str:
    """Validator helper: reject empty / too-short / refusal-text narratives.

    Raises ValueError (Pydantic surfaces as ValidationError) so the analyzer
    retry loop re-prompts the model rather than persisting useless text.
    """
    text = (value or "").strip()
    if len(text) < min_length:
        raise ValueError(
            f"{field_name} too short ({len(text)} chars, need >= {min_length})"
        )
    lowered = text.lower()
    for refusal in _REFUSAL_PREFIXES:
        if lowered.startswith(refusal):
            raise ValueError(
                f"{field_name} appears to be a model refusal: {text[:60]!r}"
            )
    return text


class CustomerAnalysis(BaseModel):
    customer_code: str = Field(default="", description="Plain numeric customer code, no prefix")
    performance_summary: str = Field(
        default="",
        description=(
            "2-3 sentences. Opens with 'You scored X.X% - this is ...'. "
            "Cites one strength + one concern with specific item codes."
        ),
    )
    supervisor_instructions: list[str] = Field(
        default_factory=list,
        description=(
            "3-4 actionable instructions. Each cites a real item code, the "
            "actual vs recommended qty, and a specific next-visit action."
        ),
    )
    strengths: list[str] = Field(
        default_factory=list,
        description="2-3 wins, each citing item code + qty + accuracy %.",
    )
    weaknesses: list[str] = Field(
        default_factory=list,
        description="2-3 misses, each citing item code + qty + likely cause.",
    )

    @field_validator("performance_summary")
    @classmethod
    def _check_summary(cls, v: str) -> str:
        return _reject_refusal(v, min_length=30, field_name="performance_summary")


class RouteAnalysis(BaseModel):
    route_code: str = Field(default="", description="Plain route code as provided")
    route_summary: str = Field(
        default="",
        description=(
            "2-3 sentences covering: customers visited vs planned, overall "
            "quantity achievement %, standout customer code, main concern."
        ),
    )
    supervisor_priorities: list[str] = Field(
        default_factory=list,
        description="3-4 tomorrow-focused priorities with scale (N customers, X%) and target.",
    )
    high_performers_with_practices: list[str] = Field(
        default_factory=list,
        description="2-3 UNIQUE customer codes (no duplicates) with what they did right.",
    )
    critical_issues: list[str] = Field(
        default_factory=list,
        description="2-3 patterns affecting 3+ customers, with counts and root cause.",
    )

    @field_validator("route_summary")
    @classmethod
    def _check_summary(cls, v: str) -> str:
        return _reject_refusal(v, min_length=50, field_name="route_summary")

    @field_validator("high_performers_with_practices")
    @classmethod
    def _dedupe_performers(cls, v: list[str]) -> list[str]:
        # Catch the duplicate-customer hallucination at the validator layer so
        # the prompt instruction and the validator can never disagree.
        seen_codes: set = set()
        deduped: list[str] = []
        code_pattern = re.compile(r"\b(\d{6,12})\b")
        for line in v or []:
            codes = code_pattern.findall(line)
            key = codes[0] if codes else line[:40]
            if key in seen_codes:
                continue
            seen_codes.add(key)
            deduped.append(line)
        return deduped


class PreVisitBriefing(BaseModel):
    briefing: str = Field(
        default="",
        description=(
            "5-7 sentence natural paragraph for the salesperson to hear pre-visit. "
            "Weaves in cycle/frequency facts, overdue items, new suggestions, target."
        ),
    )
    key_items: list[str] = Field(
        default_factory=list,
        description=(
            "2-3 one-liners: 'ItemName - N units, key fact'. "
            "Example: 'Bread 500g - 6 units, buys every 2 days and due today'."
        ),
    )
    heads_up: str = Field(
        default="",
        description=(
            "ONE sentence about anything notable (overdue item, new SKU, "
            "trend). If nothing notable: 'Straightforward visit - stick to the plan.'"
        ),
    )

    @field_validator("briefing")
    @classmethod
    def _check_briefing(cls, v: str) -> str:
        return _reject_refusal(v, min_length=80, field_name="briefing")
