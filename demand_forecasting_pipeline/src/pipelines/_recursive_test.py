"""
Recursive multi-step evaluator.

Production inference predicts a horizon of ``H`` periods using recursive
multi-step refinement: predict t+1, fold the prediction back into the
panel, rebuild features, predict t+2 using lags that now reference the
prediction, and so on. Reporting test metrics from a single direct-
prediction pass would over-state production accuracy: training-time
test scores would assume every lag at t+k references a real actual,
which is a regime production never sees.

This module runs the recursive setup against the held-out test window
using the SAME val+train-fit model that the existing direct-prediction
metrics use -- so the two metric rows are paired ("here is your model
in 1-step mode, here it is in production-like recursive mode").

The val+train-fit model is the right choice for both regimes:
  * direct test predictions already use it (train_pipeline.py: mdl_full
    is fit on cls_train + cls_val before refit-on-full-data);
  * recursive predictions use it too, so the only thing that changes
    between the two metric rows is the regime, not the model.

The function is pure compute - takes a captured model dict and the
already-engineered test feature frame, returns a predictions frame.
The caller scores those predictions against actuals exactly the same
way it scores the direct ones, so the two metrics rows are apples-to-
apples.
"""

from __future__ import annotations

import logging
from typing import Any

import numpy as np
import pandas as pd

from ..feature_engineering.builder import build_features

logger = logging.getLogger(__name__)


def recursive_test_predict(
    *,
    val_fit_models_by_class: dict[str, dict[str, Any]],
    classes_df: pd.DataFrame,
    panel_history: pd.DataFrame,
    test_feats: pd.DataFrame,
    full_date_range,
    fe_cfg: dict,
    holiday_cfg: dict | None,
    hijri_cfg: dict | None,
    salary_cycle_cfg: dict | None,
    target_encoding_artifact: tuple[pd.DataFrame, float] | None,
    group_keys: list[str],
    date_col: str,
    target_col: str,
    granularity: str,
    forecast_horizon: int,
    feature_cols_by_class: dict[str, list[str]],
    pair_routes: dict[tuple, Any] | None = None,
) -> pd.DataFrame:
    """Run recursive multi-step prediction across the test window.

    Args:
      val_fit_models_by_class: ``{cls -> {model_name -> fitted_model}}``.
        These are the SAME models used to compute the direct-prediction
        test metrics; capture them before the production refit overwrites.
      classes_df: indexed by ``group_keys``, holds ``class`` per pair.
      panel_history: train + val panel (target column is ground truth);
        the recursive loop appends predictions onto a copy of this.
      test_feats: feature-engineered test window. Used to find which
        pairs have test rows and on which dates.
      full_date_range / fe_cfg / holiday_cfg / ... : forwarded to
        ``build_features`` when we rebuild features after each fold.
      target_encoding_artifact: persisted training-time encoding map
        (the production-grade contract -- never recompute).
      feature_cols_by_class: per-class feature column list as written
        to training_summary.json.
      pair_routes: optional per-pair best-model lookup (training has
        this; respect it so each pair is scored against its own winner).

    Returns:
      DataFrame with columns ``group_keys + [date_col, prediction, class,
      best_model]``, one row per (pair, test_date). Empty frame if the
      test window is empty or no models were captured.
    """
    if test_feats.empty or not val_fit_models_by_class:
        return pd.DataFrame()

    test_dates = sorted(test_feats[date_col].dropna().unique())
    if not test_dates:
        return pd.DataFrame()

    # Pair tuple -> class string via MultiIndex; cleaner than itertuples+zip
    # and behaves identically when group_keys has length 1 or >1.
    pair_to_class = (
        classes_df.reset_index()
        .set_index(group_keys)["class"]
        .astype(str)
        .to_dict()
    )

    panel = panel_history.copy()
    out_parts: list[pd.DataFrame] = []

    # Walk forward one date at a time. After each prediction step we
    # fold the predictions into the working panel so the next feature
    # rebuild observes them as lag inputs -- the same trick inference
    # uses, applied here so the test scores come from the same regime.
    for step_idx, current_date in enumerate(test_dates):
        # Rebuild features over [history_so_far + this_test_date]. We
        # restrict to "up to current_date" so future test rows are not
        # visible and we never leak forward.
        slice_panel = panel[panel[date_col] <= current_date]
        if slice_panel.empty:
            continue

        try:
            feats_slice = build_features(
                slice_panel, group_keys, date_col, target_col, classes_df, fe_cfg,
                holiday_cfg=holiday_cfg, hijri_cfg=hijri_cfg,
                salary_cycle_cfg=salary_cycle_cfg,
                granularity=granularity,
                full_date_range=full_date_range,
                target_encoding_artifact=target_encoding_artifact,
                forecast_horizon=forecast_horizon,
            )
        except Exception as exc:
            logger.warning(
                "recursive_test_predict: feature rebuild failed at step %d (%s); "
                "stopping recursion", step_idx + 1, exc,
            )
            break

        # Only the rows at exactly current_date are predicted now.
        cur = feats_slice[feats_slice[date_col] == current_date]
        if cur.empty:
            continue

        step_preds: list[pd.DataFrame] = []
        for cls, models_by_name in val_fit_models_by_class.items():
            cls_feature_cols = feature_cols_by_class.get(cls) or []
            cls_pair_keys = [
                pk for pk, c in pair_to_class.items() if c == cls
            ]
            cls_cur = cur[
                cur.set_index(group_keys).index.isin(cls_pair_keys)
            ] if cls_pair_keys else cur.iloc[0:0]
            if cls_cur.empty:
                continue
            for pk in cls_pair_keys:
                pair_rows = cls_cur[
                    np.logical_and.reduce(
                        [cls_cur[k].astype(str) == str(v) for k, v in zip(group_keys, pk)]
                    )
                ]
                if pair_rows.empty:
                    continue
                # Pick model: pair-specific winner if a routing/lookup is
                # supplied; otherwise the first model in the captured dict
                # for the class.
                best_name = None
                if pair_routes and pk in pair_routes:
                    best_name = getattr(pair_routes[pk], "best_model", None)
                if best_name is None or best_name not in models_by_name:
                    best_name = next(iter(models_by_name.keys()))
                mdl = models_by_name.get(best_name)
                if mdl is None:
                    continue
                try:
                    pred = mdl.predict(
                        pair_rows, group_keys, date_col, target_col, cls_feature_cols,
                    )
                except Exception as exc:
                    logger.warning(
                        "recursive_test_predict: %s/%s predict failed for pair %s: %s",
                        cls, best_name, pk, exc,
                    )
                    continue
                pred = pred.copy()
                pred["class"] = cls
                pred["best_model"] = best_name
                step_preds.append(pred)

        if not step_preds:
            continue
        step_df = pd.concat(step_preds, ignore_index=True)
        out_parts.append(step_df)

        # Fold predictions back into the working panel. ONLY at
        # current_date and ONLY for pairs we actually predicted, so a
        # silent-pair stays at its prior value (whatever the panel had).
        fold = (
            step_df[group_keys + [date_col, "prediction"]]
            .rename(columns={"prediction": "_pred_target"})
        )
        merge_keys = group_keys + [date_col]
        merged = panel.merge(fold, on=merge_keys, how="left")
        mask = (merged[date_col] == current_date) & merged["_pred_target"].notna()
        if mask.any():
            merged.loc[mask, target_col] = pd.to_numeric(
                merged.loc[mask, "_pred_target"], errors="coerce",
            ).astype(float).values
        panel = merged.drop(columns=["_pred_target"])

    if not out_parts:
        return pd.DataFrame()
    return pd.concat(out_parts, ignore_index=True)
