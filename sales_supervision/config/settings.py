"""
Settings from environment variables.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings

_MODULE_ROOT = Path(__file__).resolve().parent.parent


def _read_allow_origins() -> list[str]:
    """Read shared ``YF_ALLOW_ORIGINS`` (comma/semicolon or JSON list)."""
    import json
    raw = os.getenv("YF_ALLOW_ORIGINS", "").strip()
    if not raw:
        return ["http://localhost:3000"]
    if raw.startswith("["):
        try:
            parsed = json.loads(raw)
            return [str(x).strip() for x in parsed if str(x).strip()]
        except Exception:
            pass
    return [s.strip() for s in raw.replace(";", ",").split(",") if s.strip()]


class DbSettings(BaseSettings):
    """DB connection for saving supervision sessions to YaumiAIML."""

    model_config = {"env_prefix": "SS_DB_", "extra": "ignore"}

    host: str = Field(default="")
    port: int = Field(default=1433)
    database: str = Field(default="YaumiAIML")
    username: str = Field(default="")
    password: str = Field(default="")
    driver: str = Field(default="{ODBC Driver 17 for SQL Server}")
    connection_timeout: int = Field(default=120, ge=10)
    # Per-query (cursor) timeout. Bounds the longest a save_session
    # write can stall the supervisor UI when the warehouse is slow --
    # without this a hung server keeps the request open indefinitely.
    query_timeout: int = Field(default=60, ge=5)
    # Bulk-write batch size. Items per visit are bounded (~10-20) so this
    # rarely fires more than one chunk, but caps the worst-case payload
    # so an unusual customer can't ship a multi-megabyte batch.
    executemany_chunk_size: int = Field(default=1000, ge=1, le=10000)

    @property
    def configured(self) -> bool:
        return bool(self.host and self.username)

    def connection_string(self) -> str:
        return (
            f"DRIVER={self.driver};"
            f"SERVER={self.host},{self.port};"
            f"DATABASE={self.database};"
            f"UID={self.username};"
            f"PWD={self.password};"
            f"TrustServerCertificate=yes;"
            f"Connection Timeout={self.connection_timeout};"
        )


class Settings(BaseSettings):
    model_config = {"env_prefix": "SS_", "extra": "ignore"}

    # Server
    app_name: str = Field(default="Sales Supervision Service")
    host: str = Field(default="0.0.0.0")
    port: int = Field(default=8004, ge=1024, le=65535)
    workers: int = Field(default=1, ge=1)
    log_level: str = Field(default="INFO")
    api_prefix: str = Field(default="/api/v1")

    # In-memory session registry knobs. Supervisors typically work a
    # single-shift day; the registry caps both the max parallel sessions
    # and how long an idle session is held before reclamation. Both are
    # env-overridable so deployments with longer shifts (e.g. 12-hour
    # depots) can extend the TTL without a code change.
    session_registry_max: int = Field(default=256, ge=16, le=4096,
        description="LRU cap on concurrent in-memory supervision sessions.")
    session_ttl_seconds: int = Field(default=8 * 60 * 60, ge=60 * 60,
        description="Idle TTL before an in-memory session is reclaimed.")

    # Upstream: data_import owns all DB access (single source of truth).
    # We call it over HTTP to fetch live actuals when a visit is processed.
    data_import_url: str = Field(
        default="http://localhost:8005",
        description="Base URL for data_import service",
    )
    data_import_timeout: int = Field(default=15, ge=1)

    # CORS allow-list -- shared ``YF_ALLOW_ORIGINS`` env var.
    allow_origins: list[str] = Field(default_factory=_read_allow_origins)

    # ------------------------------------------------------------------
    # Auto-visit reconciler -- background job that mirrors live YaumiLive
    # invoices into yf_supervision_* without depending on a browser tab
    # being open. All knobs are env-overridable.
    # ------------------------------------------------------------------
    auto_visit_enabled: bool = Field(default=True,
        description="Master switch for the supervision auto-fill background job.")
    auto_visit_poll_seconds: int = Field(default=60, ge=30, le=3600,
        description="Interval between reconciliation ticks. Min 30s to avoid hammering YaumiLive. "
                    "Default 60s -- with the data/LLM phase split, each tick stays sub-10s, so a "
                    "minute-cadence is the right balance between freshness and downstream load.")
    auto_visit_route_codes: list[str] = Field(default_factory=list,
        description="Routes to reconcile each tick. Empty -> falls back to demand-forecasting's live_route_codes (single source of truth).")
    auto_visit_llm_enabled: bool = Field(default=True,
        description="Fire customer + route LLM analyses as part of the reconciler. Set False to suppress LLM cost while keeping DB sync.")
    auto_visit_data_phase_workers: int = Field(default=8, ge=1, le=64,
        description="Concurrency cap for the data-sync phase of reconcile_route. "
                    "Tuned hot enough to overlap HTTP + DB I/O, bounded so a busy "
                    "tick does not flood data_import or the supervision DB.")
    auto_visit_llm_phase_workers: int = Field(default=2, ge=1, le=16,
        description="Concurrency cap for the LLM phase. Stays small because LLM "
                    "providers rate-limit aggressively and per-call latency runs "
                    "multi-second; raising this rarely improves throughput.")

    # Recommended-order client (used by the reconciler to scope a session).
    recommended_order_url: str = Field(default="http://localhost:8001",
        description="Base URL of the recommended_order service.")
    recommended_order_timeout: int = Field(default=30, ge=1)
    recommendation_cache_seconds: int = Field(default=300, ge=0,
        description="Per-(route, date) recommendation cache so repeated ticks "
                    "don't re-fetch the same plan.")
    recommendation_fetch_limit: int = Field(default=10000, ge=1, le=100000)

    # LLM analytics client (best-effort during the reconciler).
    # Default points at the local llm_analytics dev port so the in-flow
    # briefing + analysis pipeline works out of the box. Production
    # deployments override via env to point at an internal hostname or
    # set to "" to disable LLM calls in the reconciler.
    llm_analytics_url: str = Field(default="http://localhost:8003",
        description="Base URL of llm_analytics. Empty -> the reconciler skips all LLM calls.")
    llm_analytics_timeout: int = Field(default=120, ge=10, le=600,
        description="Per-call timeout. LLMs are slow; default is generous so a thinking model doesn't get cut.")

    @field_validator("auto_visit_route_codes", mode="before")
    @classmethod
    def _coerce_auto_routes(cls, v):
        if v is None:
            return []
        if isinstance(v, (list, tuple)):
            return [str(x).strip() for x in v if x is not None and str(x).strip()]
        return v

    # DB (optional -- saves to DB in addition to file)
    db: DbSettings = Field(default_factory=DbSettings)
    route_summary_table: str = Field(default="", description="e.g. [YaumiAIML].[dbo].[yaumi_supervision_route_summary]")
    customer_summary_table: str = Field(default="", description="e.g. [YaumiAIML].[dbo].[yaumi_supervision_customer_summary]")
    item_details_table: str = Field(default="", description="e.g. [YaumiAIML].[dbo].[yaumi_supervision_item_details]")

    @field_validator("log_level")
    @classmethod
    def _validate_log_level(cls, v: str) -> str:
        v = v.upper()
        if v not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError(f"Invalid log_level: {v}")
        return v


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    env_file = os.getenv("SS_ENV_FILE", ".env")
    if Path(env_file).exists():
        return Settings(_env_file=env_file)
    return Settings()
