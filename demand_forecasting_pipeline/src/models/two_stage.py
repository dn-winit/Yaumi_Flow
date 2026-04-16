import numpy as np

from sklearn.linear_model import LogisticRegression
from .base import BaseForecaster
from .lightgbm_model import LightGBMForecaster, _HAS_LGB
from .random_forest import RandomForestForecaster


class TwoStageForecaster(BaseForecaster):
    name = "two_stage"

    def __init__(self, params=None):
        super().__init__(params)
        self.threshold_ = float(self.params.get("threshold", 0.5))

    def _make_regressor(self):
        reg_name = self.params.get("regressor", "lightgbm")
        if reg_name == "lightgbm" and _HAS_LGB:
            return LightGBMForecaster(self.params.get("regressor_params", {}))
        return RandomForestForecaster(self.params.get("regressor_params", {}))

    def fit(self, train_df, group_keys, date_col, target_col, feature_cols):
        df = train_df.dropna(subset=[target_col]).copy()
        df["_y_bin"] = (df[target_col] > 0).astype(int)
        X = df[feature_cols].fillna(0.0)
        y_bin = df["_y_bin"].values
        if y_bin.sum() == 0 or y_bin.sum() == len(y_bin):
            self.classifier_ = None
        else:
            self.classifier_ = LogisticRegression(max_iter=1000, class_weight="balanced")
            self.classifier_.fit(X, y_bin)
        nz = df[df[target_col] > 0]
        self.regressor_ = self._make_regressor()
        if len(nz) >= 5:
            self.regressor_.fit(nz, group_keys, date_col, target_col, feature_cols)
        else:
            self.regressor_ = None
            self._fallback_value_ = float(df[target_col].mean()) if len(df) else 0.0
        self.fitted_ = True
        return self

    def predict(self, test_df, group_keys, date_col, target_col, feature_cols):
        df = test_df.copy()
        X = df[feature_cols].fillna(0.0)
        if self.classifier_ is not None:
            p_demand = self.classifier_.predict_proba(X)[:, 1]
        else:
            p_demand = np.ones(len(df))
        if self.regressor_ is not None:
            qty_pred = self.regressor_.predict(df, group_keys, date_col, target_col, feature_cols)["prediction"].values
        else:
            qty_pred = np.full(len(df), getattr(self, "_fallback_value_", 0.0))
        final = np.where(p_demand >= self.threshold_, qty_pred, 0.0)
        out = df[group_keys + [date_col]].copy()
        out["prediction"] = np.clip(final, a_min=0.0, a_max=None)
        out["p_demand"] = p_demand
        out["qty_if_demand"] = qty_pred
        return out
