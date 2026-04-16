import numpy as np

from sklearn.linear_model import Ridge
from sklearn.preprocessing import StandardScaler
from sklearn.pipeline import Pipeline

from .base import BaseForecaster


class LinearForecaster(BaseForecaster):
    name = "linear"

    def fit(self, train_df, group_keys, date_col, target_col, feature_cols):
        df = train_df.dropna(subset=[target_col]).copy()
        X = df[feature_cols].fillna(0.0).values
        y = df[target_col].values
        alpha = float(self.params.get("alpha", 1.0))
        self.model_ = Pipeline([
            ("scaler", StandardScaler(with_mean=True)),
            ("ridge", Ridge(alpha=alpha)),
        ])
        self.model_.fit(X, y)
        self.fitted_ = True
        return self

    def predict(self, test_df, group_keys, date_col, target_col, feature_cols):
        df = test_df.copy()
        X = df[feature_cols].fillna(0.0).values
        preds = self.model_.predict(X)
        out = df[group_keys + [date_col]].copy()
        out["prediction"] = np.clip(preds, a_min=0.0, a_max=None)
        return out
