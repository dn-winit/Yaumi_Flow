"""
ETS (Exponential Smoothing) forecaster.

The model fits ``statsmodels.ExponentialSmoothing`` **once per pair** at
fit time and caches the fitted results object. ``predict`` then calls
``.forecast(n)`` against the cached fit — no refits on the per-prediction
path. This matters because the previous design refitted the full state-space
model on every test row, producing an O(pairs × horizon) blow-up that
dominated the training/inference budget at daily scale.

``seasonal_periods`` should be set by the caller based on data granularity
(``data.granularity``); a sensible default is injected in the training
pipeline's ``_inject_granularity_aware_defaults`` helper.
"""

from __future__ import annotations

import warnings

import numpy as np

from .base import StatForecaster

try:
    from statsmodels.tsa.holtwinters import ExponentialSmoothing
    _HAS_SM = True
except ImportError:  # pragma: no cover — declared as a dep
    _HAS_SM = False


class ETSForecaster(StatForecaster):
    name = "ets"

    # -- fit ---------------------------------------------------------------

    def fit(self, train_df, group_keys, date_col, target_col, feature_cols):
        super().fit(train_df, group_keys, date_col, target_col, feature_cols)
        self.fit_cache_: dict = {}
        for keys, hist in self.history_.items():
            self.fit_cache_[keys] = self._try_fit_ets(hist)
        return self

    def _try_fit_ets(self, history: np.ndarray):
        if not _HAS_SM or len(history) < 4:
            return None
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                trend = self.params.get("trend", None)
                seasonal = self.params.get("seasonal", None)
                sp = int(self.params.get("seasonal_periods", 7))
                if seasonal and len(history) < 2 * sp:
                    seasonal = None  # not enough history for the requested seasonality
                model = ExponentialSmoothing(
                    history,
                    trend=trend,
                    seasonal=seasonal,
                    seasonal_periods=sp if seasonal else None,
                    initialization_method="estimated",
                )
                return model.fit(optimized=True)
        except Exception:
            return None

    # -- predict -----------------------------------------------------------

    def _predict_pair(self, history: np.ndarray, n: int) -> np.ndarray:
        """Use the cached fit if available; otherwise fall back to the series
        mean. Called once per pair by the base class's ``predict``."""
        # Match the cache entry by history identity — cached state lives at
        # ``self.fit_cache_[pair_key]`` and the base's predict passes the same
        # ``history`` object it stored at fit time.
        fit = self._lookup_fit(history)
        if fit is None:
            fallback = float(np.mean(history)) if len(history) else 0.0
            return np.full(max(n, 1), fallback, dtype=float)
        try:
            return np.asarray(fit.forecast(n), dtype=float)
        except Exception:
            fallback = float(np.mean(history)) if len(history) else 0.0
            return np.full(max(n, 1), fallback, dtype=float)

    def _lookup_fit(self, history: np.ndarray):
        """Locate the pre-fit for this pair. Since ``history_`` is keyed by
        pair and the base's ``predict`` passes exactly that stored array,
        identity comparison hits the cache in O(number_of_pairs)."""
        if not getattr(self, "fit_cache_", None):
            return None
        for keys, hist in self.history_.items():
            if hist is history:
                return self.fit_cache_.get(keys)
        return None

    def _predict_one(self, history):
        # Retained for interface compatibility. Not used on the hot path —
        # fit_cache_ avoids refits.
        fit = self._try_fit_ets(history)
        if fit is None:
            return float(np.mean(history)) if len(history) else 0.0
        try:
            return float(fit.forecast(1)[0])
        except Exception:
            return float(np.mean(history)) if len(history) else 0.0
