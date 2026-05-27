"""Pydantic schema for ``config.yaml``: fail-fast structural + cross-rule validation.

Cross-validates routing rules against the model registry and signal list so typos
fail at boot, not deep into a training run. Toggle via ``DF_VALIDATE_CONFIG``.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# Bump when YAML contract breaks; loader rejects mismatches.
SUPPORTED_VERSIONS = {"1.0"}

# Mirrors ``src/routing/rules.py``; keep in sync.
_ROUTING_OPERATORS = {
    "lt", "lte", "gt", "gte", "eq", "ne",
    "in", "not_in",
    "abs_gte", "abs_lte",
    "between",
    "is_true", "is_false",
}

# Signals the routing engine materialises from per-pair stats. Unknown signals
# pass YAML but never fire -- the bug this schema catches.
_ROUTING_SIGNALS = {
    "class",
    "adi", "cv2",
    "n_periods", "n_nonzero_periods", "nonzero_ratio",
    "mean_qty", "max_qty", "std_qty", "sum_qty",
    "nonzero_mean", "nonzero_median", "nonzero_min", "nonzero_std",
    "avg_gap_days",
    "is_new_launch", "is_likely_eol", "suspicious_zero_rows",
    "n_train_rows", "n_test_rows", "n_train_nonzero",
    # Pattern-aware signals (computed by routing.pair_treatment)
    "trend_direction", "trend_slope",
    "seasonal_strength",
    "near_class_boundary",
}

DemandClass = Literal["smooth", "intermittent", "erratic", "lumpy"]

class ConfigError(ValueError):
    """Raised when ``config.yaml`` fails schema validation."""

class _AllowExtra(BaseModel):
    """Permit forward-compatible YAML additions without a schema release."""

    model_config = ConfigDict(extra="allow", arbitrary_types_allowed=True)

class ProjectCfg(_AllowExtra):
    name: str = "demand_forecasting"
    random_seed: int = 42
    log_level: str = "INFO"

class PathsCfg(_AllowExtra):
    raw_data: str
    artifacts_dir: str
    models_dir: str
    predictions_dir: str
    metrics_dir: str
    explainability_dir: str
    logs_dir: str

class CausalCol(_AllowExtra):
    col: str
    agg: str = "mean"

class DataCfg(_AllowExtra):
    forecast_level: list[str] = Field(min_length=1)
    granularity: Literal["D", "W", "M", "Q"] = "D"
    date_col: str
    target_col: str
    meta_cols: list[str] = []
    max_inactivity_periods: int = Field(default=180, ge=0)
    fill_missing_periods: bool = True
    zero_fill_value: float = 0.0
    activity_flag: bool = True
    dtypes: dict[str, str] = {}
    causal_cols: list[CausalCol] = []

class ClassificationCfg(_AllowExtra):
    adi_intermittent_threshold: float = Field(default=1.32, gt=0)
    cv2_erratic_threshold: float = Field(default=0.49, gt=0)
    min_observations_for_classification: int = Field(default=3, ge=1)
    # Drop EOL tail before ADI/CV^2 so dormant mature SKUs aren't mis-classed intermittent.
    exclude_eol_tail: bool = True

class WhenCfg(_AllowExtra):
    """Loose schema: {signal: condition} validated by model-level check below."""

class RoutingRule(_AllowExtra):
    name: str
    priority: int
    when: dict[str, Any] = {}
    allow: list[str] = []
    exclude: list[str] = []
    prefer: list[str] = []
    skip_hp_tuning: bool | None = None
    hp_trials_multiplier: float | None = None

    @model_validator(mode="after")
    def _validate_when(self) -> "RoutingRule":
        for signal, cond in (self.when or {}).items():
            if signal not in _ROUTING_SIGNALS:
                raise ConfigError(
                    f"routing.rules[name={self.name!r}].when references unknown signal "
                    f"{signal!r}. Known signals: {sorted(_ROUTING_SIGNALS)}"
                )
            if isinstance(cond, dict):
                for op in cond.keys():
                    if op not in _ROUTING_OPERATORS:
                        raise ConfigError(
                            f"routing.rules[name={self.name!r}].when[{signal!r}] "
                            f"uses unknown operator {op!r}. "
                            f"Known operators: {sorted(_ROUTING_OPERATORS)}"
                        )
        return self

class RoutingCfg(_AllowExtra):
    rules: list[RoutingRule] = []

class ModelsCfg(_AllowExtra):
    enabled: dict[str, list[str]]
    fallback_order: list[str] = []
    # Single metric name OR class -> metric mapping (with optional ``default``).
    selection_metric: str | dict[str, str] = "wape"
    model_defaults: dict[str, Any] = {}

class TuningCfg(_AllowExtra):
    enabled: bool = True
    n_trials: int = Field(default=25, ge=1)
    timeout_seconds: int = Field(default=600, ge=1)
    direction: Literal["auto", "minimize", "maximize"] = "auto"
    temporal_cv: dict[str, Any] = {}
    models_to_tune: list[str] = []
    search_spaces: dict[str, Any] = {}

class EvaluationCfg(_AllowExtra):
    metrics: list[str] = ["mae", "rmse", "mape", "smape", "bias", "wape"]
    prediction_intervals: dict[str, Any] = {}

class InferenceCfg(_AllowExtra):
    forecast_horizon: int = Field(default=30, ge=1)
    # ``auto`` resolves to ``min(horizon, max(1, horizon//max_lag))`` at runtime.
    recursive_iterations: int | Literal["auto"] = "auto"

class FeatureEngineeringCfg(_AllowExtra):
    adaptive_depth: bool = True
    nan_fill: float | None = 0.0
    proximity_nan_fill: float = 9999
    horizon_safe_lags: bool = True

class PipelineConfig(_AllowExtra):
    """Top-level config schema; unknown keys preserved via extra='allow'."""

    version: str = Field(..., description="Schema version, e.g. '1.0'.")
    project: ProjectCfg = ProjectCfg()
    paths: PathsCfg
    data: DataCfg
    classification: ClassificationCfg = ClassificationCfg()
    routing: RoutingCfg = RoutingCfg()
    models: ModelsCfg
    hyperparameter_tuning: TuningCfg = TuningCfg()
    evaluation: EvaluationCfg = EvaluationCfg()
    inference: InferenceCfg = InferenceCfg()
    feature_engineering: FeatureEngineeringCfg = FeatureEngineeringCfg()

    @field_validator("version")
    @classmethod
    def _check_version(cls, v: str) -> str:
        if v not in SUPPORTED_VERSIONS:
            raise ConfigError(
                f"config.yaml version {v!r} is not supported by this build. "
                f"Supported versions: {sorted(SUPPORTED_VERSIONS)}. "
                f"Update the YAML's `version` key after migrating any breaking changes."
            )
        return v

    @model_validator(mode="after")
    def _cross_validate(self) -> "PipelineConfig":
        # Lazy import: registry pulls heavy ML deps, unavailable for syntax-only checks.
        try:
            from demand_forecasting_pipeline.src.models.registry import REGISTRY
            # ``ensemble`` is a meta-model composed at train time; treat as known.
            known_models = set(REGISTRY.keys()) | {"ensemble"}
        except Exception:
            known_models = None

        # Validate model names in models.enabled / fallback_order against registry.
        if known_models is not None:
            for cls_, names in (self.models.enabled or {}).items():
                for name in names:
                    if name not in known_models:
                        raise ConfigError(
                            f"models.enabled[{cls_}] references unknown model {name!r}. "
                            f"Registered models: {sorted(known_models)}"
                        )
            for name in self.models.fallback_order or []:
                if name not in known_models:
                    raise ConfigError(
                        f"models.fallback_order references unknown model {name!r}. "
                        f"Registered models: {sorted(known_models)}"
                    )

            for rule in self.routing.rules:
                for slot, names in (
                    ("allow", rule.allow),
                    ("exclude", rule.exclude),
                    ("prefer", rule.prefer),
                ):
                    for name in names:
                        if name not in known_models:
                            raise ConfigError(
                                f"routing.rules[name={rule.name!r}].{slot} references "
                                f"unknown model {name!r}. "
                                f"Registered models: {sorted(known_models)}"
                            )

        # forecast_level uniqueness - duplicates would make groupby ambiguous.
        if len(self.data.forecast_level) != len(set(self.data.forecast_level)):
            raise ConfigError(
                f"data.forecast_level must be unique; got {self.data.forecast_level}"
            )

        return self

def validate_config(raw: dict[str, Any]) -> dict[str, Any]:
    """Validate ``raw`` against ``PipelineConfig``; return it unchanged on success.

    Raises ``ConfigError`` with path-prefixed message. Returns the raw dict (not the
    parsed model) since downstream callers still treat config as plain dict.
    """
    try:
        PipelineConfig.model_validate(raw)
    except ConfigError:
        raise
    except Exception as exc:
        raise ConfigError(f"config.yaml schema validation failed: {exc}") from exc
    return raw
