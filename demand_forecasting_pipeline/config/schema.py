"""
Pydantic schema for ``config.yaml``.

The pipeline depends on ~80 distinct knobs spread across 14 top-level
keys. Without an explicit schema, a typo in a routing rule's model
name (``ligthgbm`` instead of ``lightgbm``) silently disables the rule
at runtime; an off-by-one threshold (``adi_intermittent_threshold:
13.2``) silently mis-classifies the entire fleet. The schema below:

  * Validates structure and types up-front so misconfigurations fail
    at boot, not three steps into a one-hour training run.
  * Pins a ``version`` key so downstream releases can detect stale
    configs and refuse to run against them.
  * Cross-validates routing rules: every model name referenced in
    ``allow``/``exclude``/``prefer`` must exist in the model registry,
    and every signal in ``when`` must come from the documented signal
    list (typos surface immediately).

Schema validation is opt-in via ``DF_VALIDATE_CONFIG`` (default: on).
Set ``DF_VALIDATE_CONFIG=0`` to bypass during emergency rollbacks.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# Current schema major.minor. Bump when the YAML contract changes in a
# way that older configs can't satisfy; the loader rejects mismatches
# so a stale config file can't take down a fresh deployment silently.
SUPPORTED_VERSIONS = {"1.0"}

# Operators recognised by the routing rule engine. Mirrors
# ``src/routing/rules.py``; keep in sync with that module.
_ROUTING_OPERATORS = {
    "lt", "lte", "gt", "gte", "eq", "ne",
    "in", "not_in",
    "abs_gte", "abs_lte",
    "between",
    "is_true", "is_false",
}

# Signals the routing engine knows how to materialise from the per-pair
# stats frame. Rules referencing a signal not in this set are valid YAML
# but will silently never fire - exactly the kind of bug this schema
# exists to catch.
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
    """Permit forward-compatible additions in YAML without forcing a
    new schema release for every new knob."""

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

class WhenCfg(_AllowExtra):
    """Loose schema -- the rule body is a dict of {signal: condition}.

    Validated with a model-level check below rather than via field types
    because ``when`` keys are signal names (variable) and values are
    operator dicts (variable shape).
    """

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
    selection_metric: str = "rmse"
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
    # ``auto`` resolves at runtime to ``min(horizon, max(1, horizon//max_lag))``
    # so adaptive feature horizons don't need a manual config bump.
    recursive_iterations: int | Literal["auto"] = "auto"

class FeatureEngineeringCfg(_AllowExtra):
    adaptive_depth: bool = True
    nan_fill: float | None = 0.0
    proximity_nan_fill: float = 9999
    horizon_safe_lags: bool = True

class PipelineConfig(_AllowExtra):
    """Top-level config schema. Unknown keys are preserved (extra='allow')
    so YAML extensions don't require a coupled schema release."""

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
        # Lazy import: registry pulls heavy ML deps and is unavailable
        # during simple syntax checks (e.g. CI's `python -m py_compile`).
        try:
            from demand_forecasting_pipeline.src.models.registry import REGISTRY
            # ``ensemble`` is a meta-model -- composed at training time
            # from already-trained class members, not registered as a
            # Forecaster subclass. Treat it as a known name so config
            # validation doesn't reject a perfectly valid menu.
            known_models = set(REGISTRY.keys()) | {"ensemble"}
        except Exception:
            known_models = None

        # Every model name in models.enabled / fallback_order must be
        # registered. Unknown names silently get filtered by the registry
        # at training time -- catch them here so the failure is loud.
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
    """Validate ``raw`` against ``PipelineConfig`` and return it unchanged
    on success. On failure, raises ``ConfigError`` with a path-prefixed
    message so the offending field is obvious from the boot log.

    The function returns the *raw* dict (not the parsed model) because
    callers downstream still treat the config as a plain ``dict``; the
    schema is a pure gatekeeper.
    """
    try:
        PipelineConfig.model_validate(raw)
    except ConfigError:
        raise
    except Exception as exc:
        raise ConfigError(f"config.yaml schema validation failed: {exc}") from exc
    return raw
