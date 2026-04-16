import numpy as np

from .base import BaseForecaster

try:
    import lightgbm as lgb
    _HAS_LGB = True
except Exception:
    _HAS_LGB = False


class LightGBMForecaster(BaseForecaster):
    name = "lightgbm"

    def fit(self, train_df, group_keys, date_col, target_col, feature_cols):
        if not _HAS_LGB:
            raise RuntimeError("lightgbm not available")
        df = train_df.dropna(subset=[target_col]).copy()
        X = df[feature_cols]
        y = df[target_col].values
        params = {
            "n_estimators": int(self.params.get("n_estimators", 400)),
            "learning_rate": float(self.params.get("learning_rate", 0.05)),
            "num_leaves": int(self.params.get("num_leaves", 31)),
            "min_data_in_leaf": int(self.params.get("min_data_in_leaf", 10)),
            "feature_fraction": float(self.params.get("feature_fraction", 0.9)),
            "bagging_fraction": float(self.params.get("bagging_fraction", 0.9)),
            "bagging_freq": 1,
            "objective": self.params.get("objective", "regression"),
            "verbose": -1,
            "random_state": int(self.params.get("random_state", 42)),
        }
        self.model_ = lgb.LGBMRegressor(**params)
        self.model_.fit(X, y)
        self.feature_cols_ = list(feature_cols)
        self.fitted_ = True
        return self

    def predict(self, test_df, group_keys, date_col, target_col, feature_cols):
        df = test_df.copy()
        X = df[feature_cols]
        preds = self.model_.predict(X)
        out = df[group_keys + [date_col]].copy()
        out["prediction"] = np.clip(preds, a_min=0.0, a_max=None)
        return out


class LightGBMQuantileForecaster(BaseForecaster):
    name = "lightgbm_quantile"

    def fit(self, train_df, group_keys, date_col, target_col, feature_cols):
        if not _HAS_LGB:
            raise RuntimeError("lightgbm not available")
        df = train_df.dropna(subset=[target_col]).copy()
        X = df[feature_cols]
        y = df[target_col].values
        quantiles = self.params.get("quantiles", [0.1, 0.9])
        self.quantile_models_ = {}
        for q in quantiles:
            mdl = lgb.LGBMRegressor(
                n_estimators=200, learning_rate=0.05, num_leaves=31,
                objective="quantile", alpha=q, verbose=-1, random_state=42,
            )
            mdl.fit(X, y)
            self.quantile_models_[q] = mdl
        self.fitted_ = True
        return self

    def predict(self, test_df, group_keys, date_col, target_col, feature_cols):
        df = test_df.copy()
        X = df[feature_cols]
        out = df[group_keys + [date_col]].copy()
        for q, mdl in self.quantile_models_.items():
            col = "q_{:.0f}".format(q * 100)
            out[col] = np.clip(mdl.predict(X), a_min=0.0, a_max=None)
        return out
