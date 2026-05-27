"""
Settings from environment variables -- provider-agnostic LLM config.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings

from common.settings_base import read_allow_origins as _read_allow_origins

_MODULE_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = {"env_prefix": "LLM_", "extra": "ignore"}

    # Server
    app_name: str = Field(default="LLM Analytics Service")
    host: str = Field(default="0.0.0.0")
    port: int = Field(default=8003, ge=1024, le=65535)
    workers: int = Field(default=1, ge=1)
    log_level: str = Field(default="INFO")
    api_prefix: str = Field(default="/api/v1")

    # LLM Provider -- provider-agnostic
    provider: str = Field(default="groq", description="groq | openai | anthropic")
    api_key: str = Field(default="", description="LLM provider API key")
    model: str = Field(default="llama-3.1-8b-instant", description="Model name/ID")
    temperature: float = Field(default=0.0, ge=0.0, le=2.0)
    # Default ceiling; per-artifact overrides below are what live calls actually use.
    max_tokens: int = Field(default=4096, ge=256, le=32000)
    # Per-artifact ceilings -- briefing rarely exceeds 800 output tokens, customer
    # analysis ~1200, route analysis with 30+ customers can need 4-6k. Sized to
    # fit P99 output without leaving headroom that wastes provider budget if a
    # runaway response loops.
    max_tokens_briefing: int = Field(default=1024, ge=256, le=8192)
    max_tokens_customer_analysis: int = Field(default=1536, ge=512, le=8192)
    max_tokens_route_analysis: int = Field(default=6144, ge=1024, le=16384)
    top_p: float = Field(default=0.1, ge=0.0, le=1.0)
    timeout: int = Field(default=45, ge=5, le=300)
    # 3 retries gives one Retry-After honor + two backoff attempts. Pre-fix
    # value of 2 burned through both attempts on transient JSON-parse blips.
    max_retries: int = Field(default=3, ge=1, le=5)
    seed: int = Field(default=42)

    # Cost telemetry -- the provider (Groq) bills in USD, period. We log USD
    # as ground truth and also surface a converted display amount so reports
    # match local accounting. Defaults below match Groq's published
    # llama-3.1-8b-instant pricing as of 2025-11; override via env when the
    # contract changes or the provider differs.
    price_input_usd_per_million:  float = Field(default=0.05, ge=0.0)
    price_output_usd_per_million: float = Field(default=0.08, ge=0.0)
    # Display currency for the /cost endpoints. The USD figure is ALWAYS
    # reported as the source of truth; this just adds a labelled second
    # column. Set ``cost_display_rate`` to the USD->{display} rate (e.g.
    # 3.6725 for AED on the dirham peg) so the conversion is transparent
    # and auditable.
    cost_display_currency: str   = Field(default="AED")
    cost_display_rate:     float = Field(default=3.6725, ge=0.0)
    # Ring buffer size for the /cost/today endpoint -- bounded so memory
    # never grows with traffic. ~10k calls/day fits in the default 12000.
    cost_buffer_size: int = Field(default=12000, ge=100, le=200000)

    # Prompts
    prompts_dir: str = Field(default=str(_MODULE_ROOT / "config" / "prompts"))

    # Cache
    cache_enabled: bool = Field(default=True)
    cache_dir: str = Field(default=str(_MODULE_ROOT / "cache"))
    cache_ttl_hours: int = Field(default=24, ge=1)

    # Rate limiting
    rate_limit_max_requests: int = Field(default=10, ge=1)
    rate_limit_window_seconds: int = Field(default=60, ge=10)
    # How long ``acquire()`` waits for a free token before failing fast.
    # Sized to be noticeably shorter than ``timeout`` so a starved request
    # never stacks on the budget.
    rate_limit_acquire_timeout_seconds: float = Field(default=5.0, ge=0.5, le=60.0)
    # Token-bucket poll interval. Small enough to feel responsive,
    # large enough that the wait loop is not a CPU sink.
    rate_limit_poll_interval_seconds: float = Field(default=0.1, ge=0.01, le=1.0)

    # Cache lazy-janitor: every N writes, walk one shard and purge
    # expired entries. Round-robins across the 256 shards so the work is
    # bounded (~entries-per-shard) and disk usage stays bounded without
    # ever serialising over a full-tree walk.
    cache_janitor_every_n_writes: int = Field(default=200, ge=10, le=10000)

    # Data limits (prevent oversized prompts)
    max_items_per_customer: int = Field(default=12, ge=1, le=500)
    max_customers_per_route: int = Field(default=50, ge=1, le=500,
        description="Hard cap on route-performance table rows fed into the LLM prompt.")

    # CORS allow-list -- shared ``YF_ALLOW_ORIGINS`` env var.
    allow_origins: list[str] = Field(default_factory=_read_allow_origins)

    @field_validator("log_level")
    @classmethod
    def _validate_log_level(cls, v: str) -> str:
        v = v.upper()
        if v not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError(f"Invalid log_level: {v}")
        return v

    @field_validator("provider")
    @classmethod
    def _validate_provider(cls, v: str) -> str:
        v = v.lower()
        if v not in {"groq", "openai", "anthropic"}:
            raise ValueError(f"Unsupported provider: {v}")
        return v


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    env_file = os.getenv("LLM_ENV_FILE", ".env")
    if Path(env_file).exists():
        return Settings(_env_file=env_file)
    return Settings()
