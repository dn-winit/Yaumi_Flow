from abc import ABC, abstractmethod
import numpy as np



class BaseForecaster(ABC):
    name = "base"
    kind = "ml"  # ml | stat

    def __init__(self, params=None):
        self.params = params or {}
        self.fitted_ = False

    @abstractmethod
    def fit(self, train_df, group_keys, date_col, target_col, feature_cols):
        ...

    @abstractmethod
    def predict(self, test_df, group_keys, date_col, target_col, feature_cols):
        ...


class StatForecaster(BaseForecaster):
    kind = "stat"

    def fit(self, train_df, group_keys, date_col, target_col, feature_cols):
        self.history_ = {}
        for keys, g in train_df.groupby(group_keys):
            g = g.sort_values(date_col)
            k = keys if isinstance(keys, tuple) else (keys,)
            self.history_[k] = g[target_col].astype(float).values
        self.fitted_ = True
        return self

    def predict(self, test_df, group_keys, date_col, target_col, feature_cols):
        out = test_df[group_keys + [date_col]].copy()
        preds = []
        for _, row in out.iterrows():
            keys = tuple(row[k] for k in group_keys)
            hist = self.history_.get(keys, np.array([0.0]))
            preds.append(self._predict_one(hist))
        out["prediction"] = np.clip(preds, a_min=0.0, a_max=None)
        return out

    @abstractmethod
    def _predict_one(self, history):
        ...
