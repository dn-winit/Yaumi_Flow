"""XGBoost forecaster. All hyperparameters flow through ``self.params``."""

from __future__ import annotations

import numpy as np

from .base import BaseForecaster

try:
    import xgboost as xgb
    _HAS_XGB = True
except ImportError:
    _HAS_XGB = False


def _require_xgb() -> None:
    if not _HAS_XGB:
        raise RuntimeError("xgboost not available — install to use this model")


class XGBoostForecaster(BaseForecaster):
    name = "xgboost"

    def fit(self, train_df, group_keys, date_col, target_col, feature_cols):
        _require_xgb()
        df = train_df.dropna(subset=[target_col]).copy()
        X = df[feature_cols]
        y = df[target_col].values
        resolved = {
            "n_estimators": int(self.params.get("n_estimators", 400)),
            "learning_rate": float(self.params.get("learning_rate", 0.05)),
            "max_depth": int(self.params.get("max_depth", 6)),
            "subsample": float(self.params.get("subsample", 0.9)),
            "colsample_bytree": float(self.params.get("colsample_bytree", 0.9)),
            "objective": self.params.get("objective", "reg:squarederror"),
            "random_state": int(self.params.get("random_state", 42)),
            "verbosity": 0,
            "n_jobs": -1,
        }
        self.model_ = xgb.XGBRegressor(**resolved)
        self.model_.fit(X, y)
        self.fitted_ = True
        return self

    def predict(self, test_df, group_keys, date_col, target_col, feature_cols):
        X = test_df[feature_cols]
        preds = self.model_.predict(X)
        out = test_df[group_keys + [date_col]].copy()
        out["prediction"] = np.clip(preds, 0.0, None)
        return out
