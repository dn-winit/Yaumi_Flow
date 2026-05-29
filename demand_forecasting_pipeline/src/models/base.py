from __future__ import annotations

from abc import ABC, abstractmethod

import numpy as np


class BaseForecaster(ABC):
    name = "base"
    kind = "ml"  # ml | stat

    def __init__(self, params=None):
        self.params = dict(params or {})
        self.fitted_ = False

    @abstractmethod
    def fit(self, train_df, group_keys, date_col, target_col, feature_cols):
        ...

    @abstractmethod
    def predict(self, test_df, group_keys, date_col, target_col, feature_cols):
        ...

    # ------------------------------------------------------------------
    # sklearn-compatible parameter accessors. Keeping these on the base
    # class means every model in the registry round-trips its params
    # through pickle uniformly, and external tools that expect the
    # sklearn estimator protocol (clone, deep-copy, hyperparameter
    # search outside Optuna) work without per-model adapters.
    # ------------------------------------------------------------------

    def get_params(self, deep: bool = True) -> dict:
        return dict(self.params)

    def set_params(self, **params) -> BaseForecaster:
        self.params.update(params)
        return self


class StatForecaster(BaseForecaster):
    """Base for per-pair statistical forecasters (naive, moving-average,
    Croston, ETS, ...).

    ``fit`` materializes a per-pair history; ``predict`` dispatches to
    :meth:`_predict_pair` once per pair and assigns the resulting forecast
    array to every test row of that pair. Subclasses implement either
    ``_predict_one`` (single-step forecast - replicated across the horizon
    by the default ``_predict_pair``) or override ``_predict_pair`` directly
    when they produce genuine multi-step forecasts (ETS).

    The key production improvement over a naive loop: ``_predict_pair`` is
    called **once per pair**, not once per row. ETS (the expensive model)
    caches its fitted state at fit time, so ``predict`` never refits.
    """

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
        out = test_df[group_keys + [date_col]].copy().reset_index(drop=True)
        if out.empty:
            out["prediction"] = np.array([], dtype=float)
            return out

        out["_pos"] = np.arange(len(out), dtype=int)
        preds = np.zeros(len(out), dtype=float)

        sorted_view = out.sort_values(group_keys + [date_col])
        for keys, g in sorted_view.groupby(group_keys, sort=False):
            keys_t = keys if isinstance(keys, tuple) else (keys,)
            hist = self.history_.get(keys_t, np.array([0.0]))
            n = len(g)
            pair_preds = np.asarray(self._predict_pair(hist, n), dtype=float).ravel()
            if pair_preds.size < n:
                pad = pair_preds[-1] if pair_preds.size else 0.0
                pair_preds = np.concatenate([pair_preds, np.full(n - pair_preds.size, pad)])
            preds[g["_pos"].to_numpy()] = pair_preds[:n]

        out["prediction"] = np.clip(preds, 0.0, None)
        return out.drop(columns=["_pos"])[group_keys + [date_col, "prediction"]]

    def _predict_pair(self, history: np.ndarray, n: int) -> np.ndarray:
        """Default: compute one value via ``_predict_one``, replicate across
        the horizon. Subclasses with genuine multi-step capability (ETS)
        override this for step-varying forecasts."""
        v = self._predict_one(history)
        return np.full(max(n, 1), v, dtype=float)

    @abstractmethod
    def _predict_one(self, history):
        ...
