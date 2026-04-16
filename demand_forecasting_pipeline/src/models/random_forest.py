import numpy as np
from sklearn.ensemble import RandomForestRegressor
from .base import BaseForecaster


class RandomForestForecaster(BaseForecaster):
    name = "random_forest"

    def fit(self, train_df, group_keys, date_col, target_col, feature_cols):
        df = train_df.dropna(subset=[target_col]).copy()
        X = df[feature_cols].fillna(0.0).values
        y = df[target_col].values
        params = {
            "n_estimators": int(self.params.get("n_estimators", 200)),
            "max_depth": self.params.get("max_depth", None),
            "min_samples_leaf": int(self.params.get("min_samples_leaf", 2)),
            "n_jobs": -1,
            "random_state": int(self.params.get("random_state", 42)),
        }
        self.model_ = RandomForestRegressor(**params)
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
