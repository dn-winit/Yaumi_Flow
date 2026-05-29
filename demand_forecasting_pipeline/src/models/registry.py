from .croston import CrostonForecaster, CrostonSBAForecaster
from .ets import _HAS_SM, ETSForecaster
from .gradient_boosting import GradientBoostingForecaster
from .lightgbm_model import _HAS_LGB, LightGBMForecaster, LightGBMQuantileForecaster
from .linear import LinearForecaster
from .moving_average import MovingAverageForecaster
from .naive import NaiveForecaster
from .random_forest import RandomForestForecaster
from .two_stage import TwoStageForecaster
from .xgboost_model import _HAS_XGB, XGBoostForecaster

REGISTRY = {
    "naive": NaiveForecaster,
    "moving_average": MovingAverageForecaster,
    "croston": CrostonForecaster,
    "croston_sba": CrostonSBAForecaster,
    "ets": ETSForecaster,
    "linear": LinearForecaster,
    "random_forest": RandomForestForecaster,
    "gradient_boosting": GradientBoostingForecaster,
    "lightgbm": LightGBMForecaster,
    "xgboost": XGBoostForecaster,
    "two_stage": TwoStageForecaster,
    "lightgbm_quantile": LightGBMQuantileForecaster,
}


# Models whose import is wrapped in try/except inside the model module
# (statsmodels for ETS, lightgbm for LightGBM, xgboost for XGBoost).
# is_available must consult the matching availability flag so the
# pipeline rejects an unavailable backend at config-load time rather
# than raising mid-pipeline. sklearn-backed models (Linear, RF, GB,
# TwoStage) import unconditionally and would fail at registry import
# time if sklearn were missing -- that's already config-load.
_AVAILABILITY: dict[str, bool] = {
    "ets":               _HAS_SM,
    "lightgbm":          _HAS_LGB,
    "lightgbm_quantile": _HAS_LGB,
    "xgboost":           _HAS_XGB,
}


def is_available(name):
    if name in _AVAILABILITY:
        return _AVAILABILITY[name]
    return name in REGISTRY


def build_model(name, params=None):
    if name not in REGISTRY:
        raise ValueError(f"unknown model: {name}")
    return REGISTRY[name](params or {})
