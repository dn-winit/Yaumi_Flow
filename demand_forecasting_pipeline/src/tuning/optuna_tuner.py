import numpy as np
import pandas as pd

try:
    import optuna
    optuna.logging.set_verbosity(optuna.logging.WARNING)
    _HAS_OPTUNA = True
except Exception:
    _HAS_OPTUNA = False

from ..models.registry import build_model
from ..evaluation.metrics import compute_all


def _search_space(trial, model_name):
    if model_name == "lightgbm":
        return {
            "n_estimators": trial.suggest_int("n_estimators", 100, 600),
            "learning_rate": trial.suggest_float("learning_rate", 0.01, 0.2, log=True),
            "num_leaves": trial.suggest_int("num_leaves", 15, 127),
            "min_data_in_leaf": trial.suggest_int("min_data_in_leaf", 5, 40),
            "feature_fraction": trial.suggest_float("feature_fraction", 0.6, 1.0),
            "bagging_fraction": trial.suggest_float("bagging_fraction", 0.6, 1.0),
        }
    if model_name == "xgboost":
        return {
            "n_estimators": trial.suggest_int("n_estimators", 100, 600),
            "learning_rate": trial.suggest_float("learning_rate", 0.01, 0.2, log=True),
            "max_depth": trial.suggest_int("max_depth", 3, 10),
            "subsample": trial.suggest_float("subsample", 0.6, 1.0),
            "colsample_bytree": trial.suggest_float("colsample_bytree", 0.6, 1.0),
        }
    if model_name == "random_forest":
        return {
            "n_estimators": trial.suggest_int("n_estimators", 100, 500),
            "max_depth": trial.suggest_int("max_depth", 4, 20),
            "min_samples_leaf": trial.suggest_int("min_samples_leaf", 1, 10),
        }
    return {}


def _temporal_cv_splits(df, date_col, n_splits):
    dates = sorted(df[date_col].unique())
    n = len(dates)
    if n < n_splits + 2:
        return [(df, df.iloc[0:0])]
    splits = []
    for i in range(n_splits):
        cutoff_idx = n - n_splits + i
        if cutoff_idx < 2:
            continue
        cutoff = dates[cutoff_idx]
        tr = df[df[date_col] < cutoff]
        va = df[df[date_col] == cutoff]
        if not tr.empty and not va.empty:
            splits.append((tr, va))
    return splits if splits else [(df, df.iloc[0:0])]


def tune_model(model_name, train_df, val_df, group_keys, date_col, target_col, feature_cols,
               n_trials=15, timeout=60, metric="rmse", temporal_cv_cfg=None):
    if not _HAS_OPTUNA:
        return {}

    use_temporal_cv = (
        temporal_cv_cfg and temporal_cv_cfg.get("enabled", False) and not train_df.empty
    )

    def objective(trial):
        params = _search_space(trial, model_name)
        try:
            if use_temporal_cv:
                full = pd.concat([train_df, val_df], ignore_index=True) if not val_df.empty else train_df
                splits = _temporal_cv_splits(full, date_col, temporal_cv_cfg.get("n_splits", 3))
                scores = []
                for tr, va in splits:
                    if va.empty:
                        continue
                    mdl = build_model(model_name, params)
                    mdl.fit(tr, group_keys, date_col, target_col, feature_cols)
                    preds = mdl.predict(va, group_keys, date_col, target_col, feature_cols)
                    merged = va[[target_col]].reset_index(drop=True)
                    merged["prediction"] = preds["prediction"].values
                    m = compute_all(merged[target_col].values, merged["prediction"].values, [metric])
                    v = m.get(metric)
                    if v is not None and np.isfinite(v):
                        scores.append(v)
                return float(np.mean(scores)) if scores else float("inf")
            else:
                if val_df is None or val_df.empty:
                    return float("inf")
                mdl = build_model(model_name, params)
                mdl.fit(train_df, group_keys, date_col, target_col, feature_cols)
                preds = mdl.predict(val_df, group_keys, date_col, target_col, feature_cols)
                merged = val_df[group_keys + [date_col, target_col]].merge(
                    preds, on=group_keys + [date_col], how="left"
                )
                merged["prediction"] = merged["prediction"].fillna(0.0)
                m = compute_all(merged[target_col].values, merged["prediction"].values, [metric])
                v = m.get(metric)
                return float("inf") if v is None else float(v)
        except Exception:
            return float("inf")

    study = optuna.create_study(direction="minimize")
    try:
        study.optimize(objective, n_trials=n_trials, timeout=timeout, show_progress_bar=False)
        return study.best_params
    except Exception:
        return {}
