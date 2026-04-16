from .naive import NaiveForecaster
from .moving_average import MovingAverageForecaster
from .croston import CrostonForecaster, CrostonSBAForecaster
from .ets import ETSForecaster
from .linear import LinearForecaster
from .random_forest import RandomForestForecaster
from .gradient_boosting import GradientBoostingForecaster
from .lightgbm_model import LightGBMForecaster, LightGBMQuantileForecaster, _HAS_LGB
from .xgboost_model import XGBoostForecaster, _HAS_XGB
from .two_stage import TwoStageForecaster


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


def is_available(name):
    if name in ("lightgbm", "lightgbm_quantile"):
        return _HAS_LGB
    if name == "xgboost":
        return _HAS_XGB
    return name in REGISTRY


def build_model(name, params=None):
    if name not in REGISTRY:
        raise ValueError("unknown model: {}".format(name))
    return REGISTRY[name](params or {})
