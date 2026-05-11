"""
LightGBM forecasters.

``LightGBMForecaster`` - point-estimate regressor used per (class, pair).
``LightGBMQuantileForecaster`` - fits one model per quantile, used for
prediction intervals. Both read every hyperparameter from ``self.params``
so Optuna and config overrides flow through uniformly.
"""

from __future__ import annotations

import numpy as np

from .base import BaseForecaster

try:
    import lightgbm as lgb
    _HAS_LGB = True
except ImportError:
    _HAS_LGB = False


def _require_lgb() -> None:
    if not _HAS_LGB:
        raise RuntimeError("lightgbm not available - install to use this model")


def _base_params(params: dict, overrides: dict | None = None) -> dict:
    """Assemble the kwargs passed to ``lgb.LGBMRegressor``. Overrides win
    against defaults; user params win against both.

    Determinism: ``deterministic=True`` + ``force_col_wise=True`` +
    single-threaded (default ``num_threads=1``) so identical hyperparams
    on identical data produce identical splits across runs. Without
    these flags multi-threaded LightGBM is order-non-deterministic and
    Optuna trials are non-reproducible. Users wanting wall-clock speed
    can override ``num_threads`` via params.

    Memory: ``free_raw_data=True`` discards the training matrix from the
    Booster after fit, so pickled models stay small at fleet scale.
    """
    defaults = {
        "n_estimators": 400,
        "learning_rate": 0.05,
        "num_leaves": 31,
        "min_data_in_leaf": 10,
        "feature_fraction": 0.9,
        "bagging_fraction": 0.9,
        "bagging_freq": 1,
        "objective": "regression",
        "verbose": -1,
        "random_state": 42,
        "deterministic": True,
        "force_col_wise": True,
        "num_threads": 1,
        "free_raw_data": True,
    }
    resolved = {**defaults, **(overrides or {})}
    for key in list(resolved):
        if key in params:
            resolved[key] = params[key]
    return resolved


class LightGBMForecaster(BaseForecaster):
    name = "lightgbm"

    def fit(self, train_df, group_keys, date_col, target_col, feature_cols):
        _require_lgb()
        df = train_df.dropna(subset=[target_col]).copy()
        X = df[feature_cols]
        y = df[target_col].values
        self.model_ = lgb.LGBMRegressor(**_base_params(self.params))
        self.model_.fit(X, y)
        self.fitted_ = True
        return self

    def predict(self, test_df, group_keys, date_col, target_col, feature_cols):
        X = test_df[feature_cols]
        preds = self.model_.predict(X)
        out = test_df[group_keys + [date_col]].copy()
        out["prediction"] = np.clip(preds, 0.0, None)
        return out


class LightGBMQuantileForecaster(BaseForecaster):
    name = "lightgbm_quantile"

    def fit(self, train_df, group_keys, date_col, target_col, feature_cols):
        _require_lgb()
        df = train_df.dropna(subset=[target_col]).copy()
        X = df[feature_cols]
        y = df[target_col].values
        quantiles = self.params.get("quantiles", [0.1, 0.9])
        # Same params dispatch as the point forecaster - quantile models
        # honour n_estimators / learning_rate / num_leaves / random_state.
        base = _base_params(self.params, overrides={"n_estimators": 200})
        self.quantile_models_: dict = {}
        for q in quantiles:
            mdl = lgb.LGBMRegressor(**{**base, "objective": "quantile", "alpha": float(q)})
            mdl.fit(X, y)
            self.quantile_models_[q] = mdl
        self.fitted_ = True
        return self

    def predict(self, test_df, group_keys, date_col, target_col, feature_cols):
        X = test_df[feature_cols]
        out = test_df[group_keys + [date_col]].copy()
        for q, mdl in self.quantile_models_.items():
            out[f"q_{int(q * 100)}"] = np.clip(mdl.predict(X), 0.0, None)
        return out
