import os
import numpy as np
import pandas as pd

from ..utils.config_loader import load_config, ensure_dirs
from ..utils.logger import get_logger
from ..utils.io_utils import save_json, save_pickle, save_dataframe, pair_mask
from ..utils.time_utils import period_offset, build_date_range
from ..data_processing.loader import load_raw
from ..data_processing.aggregator import build_panel
from ..data_processing.cleaner import per_pair_outlier_treatment, clip_negative_to_zero
from ..data_processing.splitter import time_based_split
from ..data_processing.validator import validate_input
from ..feature_engineering.classifier import classify_dataset
from ..feature_engineering.explainability import compute_pair_explainability
from ..feature_engineering.builder import build_features
from ..models.registry import build_model, is_available
from ..models.ensemble import weighted_average_ensemble, weights_from_metric
from ..tuning.optuna_tuner import tune_model
from ..evaluation.metrics import compute_all


META_FEATURE_EXCLUDE = {"class"}


def _feature_columns(df, group_keys, date_col, target_col):
    exclude = set(group_keys + [date_col, target_col]) | META_FEATURE_EXCLUDE
    return [c for c in df.columns if c not in exclude and df[c].dtype.kind in "fiub"]


def _drop_inactive_pairs(df, group_keys, date_col, target_col, max_inactivity_periods, granularity):
    nonzero = df[df[target_col] > 0]
    if nonzero.empty:
        return df.iloc[0:0].copy()
    last_sale = nonzero.groupby(group_keys, as_index=False)[date_col].max().rename(
        columns={date_col: "_last_sale"}
    )
    global_max = df[date_col].max()
    cutoff = global_max - period_offset(granularity, max_inactivity_periods)
    active = last_sale[last_sale["_last_sale"] >= cutoff][group_keys]
    return df.merge(active, on=group_keys, how="inner")


def _filter_models_for_class(enabled, cls, fallback_order):
    cls_models = enabled.get(cls, [])
    out = [m for m in cls_models if is_available(m) or m == "ensemble"]
    return out if out else [m for m in fallback_order if is_available(m)]


def _per_pair_evaluate(predictions_by_model, group_keys, date_col, target_col, metric):
    from ..utils.io_utils import ensure_tuple
    pair_best = {}
    for m_name, pred_df in predictions_by_model.items():
        for keys, g in pred_df.groupby(group_keys):
            y = g[target_col].values.astype(float)
            p = g["prediction"].values.astype(float)
            m = compute_all(y, p, [metric])
            score = m.get(metric)
            if score is None or not np.isfinite(score):
                score = float("inf")
            pk = ensure_tuple(keys)
            if pk not in pair_best or score < pair_best[pk][1]:
                pair_best[pk] = (m_name, score)
    return pair_best


def _build_per_pair_predictions(pair_best, predictions_by_model, group_keys, cls):
    records = []
    for pk, (best_model, _) in pair_best.items():
        pred_df = predictions_by_model[best_model]
        pair_pred = pred_df[pair_mask(pred_df, group_keys, pk)].copy()
        pair_pred["class"] = cls
        pair_pred["best_model"] = best_model
        records.append(pair_pred)
    return pd.concat(records, ignore_index=True) if records else pd.DataFrame()


def run_training(config_path):
    cfg = load_config(config_path)
    ensure_dirs(cfg)
    logger = get_logger("train", cfg["paths"]["logs_dir"], cfg["project"]["log_level"])

    date_col = cfg["data"]["date_col"]
    target_col = cfg["data"]["target_col"]
    group_keys = cfg["data"]["forecast_level"]
    meta_cols = cfg["data"].get("meta_cols", [])
    freq = cfg["data"]["granularity"]
    causal_cols = cfg["data"].get("causal_cols", [])
    activity_flag = cfg["data"].get("activity_flag", False)

    logger.info("Step 1: load and validate raw data")
    raw = load_raw(cfg["paths"]["raw_data"], date_col)
    if cfg.get("validation", {}).get("enabled", False):
        validate_input(raw, cfg)
    all_cols = list(dict.fromkeys(
        group_keys + [date_col, target_col] + (meta_cols or []) +
        [cc["col"] for cc in causal_cols if cc["col"] in raw.columns]
    ))
    raw = raw[[c for c in all_cols if c in raw.columns]].copy()

    logger.info("Step 2-3: build panel at {} per {}".format(freq, group_keys))
    agg = build_panel(
        raw, group_keys, date_col, target_col, meta_cols, freq,
        fill_missing=cfg["data"].get("fill_missing_periods", True),
        fill_value=cfg["data"].get("zero_fill_value", 0.0),
        causal_cols=causal_cols,
        activity_flag=activity_flag,
    )

    logger.info("Step 4: drop inactive pairs")
    agg = _drop_inactive_pairs(agg, group_keys, date_col, target_col, cfg["data"]["max_inactivity_periods"], freq)
    logger.info("Pairs after inactivity filter: {}".format(agg.groupby(group_keys).ngroups))

    if cfg["cleaning"].get("clip_negative_to_zero", True):
        logger.info("Step 5: clip negatives to zero")
        agg = clip_negative_to_zero(agg, target_col)

    test_horizon = cfg["split"]["test_horizon"]
    val_horizon = cfg["split"]["validation_horizon"]
    max_date = agg[date_col].max()
    test_start = max_date - period_offset(freq, test_horizon - 1)
    train_window = agg[agg[date_col] < test_start]

    logger.info("Step 6: segmentation on train window (date < {})".format(test_start.date()))
    classes_df = classify_dataset(
        train_window, group_keys, target_col,
        cfg["classification"]["adi_intermittent_threshold"],
        cfg["classification"]["cv2_erratic_threshold"],
    )

    logger.info("Step 7: per-pair outlier treatment (bounds from train window)")
    agg = per_pair_outlier_treatment(
        agg, group_keys, target_col, cfg["cleaning"].get("per_pair_outlier", {}),
        classes=classes_df, source_df=train_window,
    )

    logger.info("Step 8: explainability metrics")
    expl = compute_pair_explainability(
        train_window, group_keys, target_col,
        cfg["classification"]["adi_intermittent_threshold"],
        cfg["classification"]["cv2_erratic_threshold"],
    )
    save_dataframe(expl, os.path.join(cfg["paths"]["explainability_dir"], "pair_explainability.csv"))

    logger.info("Step 9: build features (adaptive + target encoding + holidays)")
    horizon = cfg.get("inference", {}).get("forecast_horizon", 0)
    full_range_end = agg[date_col].max() + period_offset(freq, horizon)
    full_date_range = build_date_range(agg[date_col].min(), full_range_end, freq)
    feats = build_features(
        agg, group_keys, date_col, target_col, classes_df, cfg["feature_engineering"],
        holiday_cfg=cfg.get("holidays"),
        granularity=freq,
        full_date_range=full_date_range,
        target_encoding_source=train_window,
    )

    train, val, test = time_based_split(feats, date_col, test_horizon, val_horizon, granularity=freq)
    logger.info("Train: {} | Val: {} | Test: {}".format(len(train), len(val), len(test)))

    feature_cols = _feature_columns(train, group_keys, date_col, target_col)
    logger.info("Feature columns ({}): {}".format(len(feature_cols), feature_cols[:10]))

    enabled = cfg["models"]["enabled"]
    fallback = cfg["models"]["fallback_order"]
    selection_metric = cfg["models"]["selection_metric"]
    metrics_names = cfg["evaluation"]["metrics"]
    per_pair_selection = cfg.get("model_selection", {}).get("strategy", "per_class") == "per_pair"
    temporal_cv_cfg = cfg.get("hyperparameter_tuning", {}).get("temporal_cv")
    can_select_on_val = per_pair_selection and not val.empty

    artifacts = {
        "per_class": {},
        "models_path": {},
        "schema": {
            "forecast_level": group_keys,
            "date_col": date_col,
            "target_col": target_col,
            "granularity": freq,
            "feature_cols": feature_cols,
            "meta_cols": [c for c in (meta_cols or []) if c in feats.columns],
        },
    }
    test_pred_records = []
    metrics_records = []
    pair_model_lookup = []

    for cls in classes_df["class"].unique():
        logger.info("=== Class: {} ===".format(cls))
        cls_pairs = classes_df[classes_df["class"] == cls].reset_index()[group_keys]
        cls_train = train.merge(cls_pairs, on=group_keys, how="inner")
        cls_val = val.merge(cls_pairs, on=group_keys, how="inner") if not val.empty else val
        cls_test = test.merge(cls_pairs, on=group_keys, how="inner")
        if cls_train.empty or cls_test.empty:
            logger.info("Skipping class {} (empty)".format(cls))
            continue

        models_to_run = _filter_models_for_class(enabled, cls, fallback)
        logger.info("Models for {}: {}".format(cls, models_to_run))

        cls_val_predictions = {}
        cls_test_predictions = {}
        cls_class_metric = {}

        for m_name in models_to_run:
            if m_name == "ensemble":
                continue
            try:
                model_defaults = cfg["models"].get("model_defaults", {}).get(m_name, {})
                params = dict(model_defaults)
                params["random_state"] = cfg["project"]["random_seed"]
                if (cfg["hyperparameter_tuning"]["enabled"] and
                        m_name in cfg["hyperparameter_tuning"]["models_to_tune"] and
                        not cls_val.empty):
                    logger.info("Tuning {} for {}".format(m_name, cls))
                    params = tune_model(
                        m_name, cls_train, cls_val, group_keys, date_col, target_col, feature_cols,
                        n_trials=cfg["hyperparameter_tuning"]["n_trials"],
                        timeout=cfg["hyperparameter_tuning"]["timeout_seconds"],
                        metric=selection_metric,
                        temporal_cv_cfg=temporal_cv_cfg,
                    )

                if can_select_on_val:
                    mdl_sel = build_model(m_name, params)
                    mdl_sel.fit(cls_train, group_keys, date_col, target_col, feature_cols)
                    val_preds = mdl_sel.predict(cls_val, group_keys, date_col, target_col, feature_cols)
                    val_merged = cls_val[group_keys + [date_col, target_col]].merge(
                        val_preds, on=group_keys + [date_col], how="left"
                    )
                    val_merged["prediction"] = val_merged["prediction"].fillna(0.0)
                    if "p_demand" not in val_merged.columns:
                        val_merged["p_demand"] = (val_merged["prediction"] > 0).astype(float)
                        val_merged["qty_if_demand"] = val_merged["prediction"]
                    cls_val_predictions[m_name] = val_merged

                fit_data = pd.concat([cls_train, cls_val], ignore_index=True) if not cls_val.empty else cls_train
                mdl_full = build_model(m_name, params)
                mdl_full.fit(fit_data, group_keys, date_col, target_col, feature_cols)
                test_preds = mdl_full.predict(cls_test, group_keys, date_col, target_col, feature_cols)
                test_merged = cls_test[group_keys + [date_col, target_col]].merge(
                    test_preds, on=group_keys + [date_col], how="left"
                )
                test_merged["prediction"] = test_merged["prediction"].fillna(0.0)
                if "p_demand" not in test_merged.columns:
                    test_merged["p_demand"] = (test_merged["prediction"] > 0).astype(float)
                    test_merged["qty_if_demand"] = test_merged["prediction"]
                cls_test_predictions[m_name] = test_merged

                m_metrics = compute_all(test_merged[target_col].values, test_merged["prediction"].values, metrics_names)
                logger.info("{} {} test metrics: {}".format(cls, m_name, m_metrics))
                cls_class_metric[m_name] = m_metrics.get(selection_metric)
                metrics_records.append({"class": cls, "model": m_name, **m_metrics})

                model_path = os.path.join(cfg["paths"]["models_dir"], "{}_{}.pkl".format(cls, m_name))
                try:
                    save_pickle(mdl_full, model_path)
                    artifacts["models_path"]["{}__{}".format(cls, m_name)] = model_path
                except Exception as e:
                    logger.info("Failed to pickle {} {}: {}".format(cls, m_name, e))
            except Exception as e:
                logger.info("Model {} failed for class {}: {}".format(m_name, cls, e))

        if not cls_test_predictions:
            for fb in fallback:
                try:
                    mdl = build_model(fb)
                    mdl.fit(cls_train, group_keys, date_col, target_col, feature_cols)
                    preds = mdl.predict(cls_test, group_keys, date_col, target_col, feature_cols)
                    merged = cls_test[group_keys + [date_col, target_col]].merge(
                        preds, on=group_keys + [date_col], how="left"
                    )
                    merged["prediction"] = merged["prediction"].fillna(0.0)
                    cls_test_predictions[fb] = merged
                    if can_select_on_val:
                        vp = mdl.predict(cls_val, group_keys, date_col, target_col, feature_cols)
                        vm = cls_val[group_keys + [date_col, target_col]].merge(
                            vp, on=group_keys + [date_col], how="left"
                        )
                        vm["prediction"] = vm["prediction"].fillna(0.0)
                        cls_val_predictions[fb] = vm
                    break
                except Exception:
                    continue

        if "ensemble" in enabled.get(cls, []) and len(cls_test_predictions) >= 2:
            weights = weights_from_metric(cls_class_metric)
            ens_test = weighted_average_ensemble(
                {k: v[group_keys + [date_col, "prediction"]] for k, v in cls_test_predictions.items()}, weights,
            )
            test_merged = cls_test[group_keys + [date_col, target_col]].merge(
                ens_test, on=group_keys + [date_col], how="left"
            )
            test_merged["prediction"] = test_merged["prediction"].fillna(0.0)
            cls_test_predictions["ensemble"] = test_merged
            m_metrics = compute_all(test_merged[target_col].values, test_merged["prediction"].values, metrics_names)
            logger.info("{} ensemble test metrics: {}".format(cls, m_metrics))
            metrics_records.append({"class": cls, "model": "ensemble", **m_metrics})

            if can_select_on_val and len(cls_val_predictions) >= 2:
                ens_val = weighted_average_ensemble(
                    {k: v[group_keys + [date_col, "prediction"]] for k, v in cls_val_predictions.items()}, weights,
                )
                val_merged = cls_val[group_keys + [date_col, target_col]].merge(
                    ens_val, on=group_keys + [date_col], how="left"
                )
                val_merged["prediction"] = val_merged["prediction"].fillna(0.0)
                cls_val_predictions["ensemble"] = val_merged

            save_json(
                {k: float(v) for k, v in (weights or {}).items()},
                os.path.join(cfg["paths"]["models_dir"], "{}_ensemble_weights.json".format(cls)),
            )
            artifacts["models_path"]["{}__ensemble".format(cls)] = "weights"

        selection_source = cls_val_predictions if can_select_on_val else cls_test_predictions
        if per_pair_selection and len(selection_source) >= 2:
            sel_label = "validation" if can_select_on_val else "test"
            logger.info("Per-pair model selection for {} on {} set".format(cls, sel_label))
            pair_best = _per_pair_evaluate(selection_source, group_keys, date_col, target_col, selection_metric)
            best_pred = _build_per_pair_predictions(pair_best, cls_test_predictions, group_keys, cls)
            for pk, (bm, sc) in pair_best.items():
                row = {group_keys[i]: pk[i] for i in range(len(group_keys))}
                row.update({"class": cls, "best_model": bm, "score": sc})
                pair_model_lookup.append(row)
        else:
            valid_metric = {k: v for k, v in cls_class_metric.items() if v is not None and np.isfinite(v)}
            best_name = min(valid_metric.items(), key=lambda x: x[1])[0] if valid_metric else list(cls_test_predictions.keys())[0]
            logger.info("Best model for {}: {}".format(cls, best_name))
            best_pred = cls_test_predictions[best_name].copy()
            best_pred["class"] = cls
            best_pred["best_model"] = best_name
            for pk in cls_pairs.itertuples(index=False):
                pk_tuple = tuple(pk)
                row = {group_keys[i]: pk_tuple[i] for i in range(len(group_keys))}
                row.update({"class": cls, "best_model": best_name})
                pair_model_lookup.append(row)

        artifacts["per_class"][cls] = {"models_trained": list(cls_test_predictions.keys()), "metrics": cls_class_metric}
        test_pred_records.append(best_pred)

    pi_cfg = cfg.get("evaluation", {}).get("prediction_intervals", {})
    if pi_cfg.get("enabled", False) and is_available("lightgbm_quantile"):
        logger.info("Fitting per-class prediction interval models")
        qi_parts = []
        for cls in classes_df["class"].unique():
            cls_pairs = classes_df[classes_df["class"] == cls].reset_index()[group_keys]
            cls_fit = pd.concat([train, val], ignore_index=True).merge(cls_pairs, on=group_keys, how="inner") if not val.empty else train.merge(cls_pairs, on=group_keys, how="inner")
            cls_test_qi = test.merge(cls_pairs, on=group_keys, how="inner")
            if cls_fit.empty or cls_test_qi.empty:
                continue
            try:
                qi_model = build_model("lightgbm_quantile", {"quantiles": pi_cfg.get("quantiles", [0.1, 0.9])})
                qi_model.fit(cls_fit, group_keys, date_col, target_col, feature_cols)
                qi_pred = qi_model.predict(cls_test_qi, group_keys, date_col, target_col, feature_cols)
                qi_parts.append(qi_pred)
                save_pickle(qi_model, os.path.join(cfg["paths"]["models_dir"], "quantile_{}.pkl".format(cls)))
            except Exception as e:
                logger.info("Quantile model failed for {}: {}".format(cls, e))
        interval_preds = pd.concat(qi_parts, ignore_index=True) if qi_parts else None
    else:
        interval_preds = None

    final_test_pred = pd.concat(test_pred_records, ignore_index=True) if test_pred_records else pd.DataFrame()
    if not final_test_pred.empty:
        final_test_pred = final_test_pred.merge(expl, on=group_keys, how="left", suffixes=("", "_expl"))
        if interval_preds is not None:
            qi_cols = [c for c in interval_preds.columns if c.startswith("q_")]
            if qi_cols:
                qi_merged = interval_preds[group_keys + [date_col] + qi_cols]
                final_test_pred = final_test_pred.merge(qi_merged, on=group_keys + [date_col], how="left")
                for qc in qi_cols:
                    final_test_pred[qc] = final_test_pred[qc].clip(lower=0.0)
                if "q_10" in qi_cols and "q_90" in qi_cols:
                    raw_q10 = final_test_pred["q_10"]
                    raw_q90 = final_test_pred["q_90"]
                    pred = final_test_pred["prediction"]
                    final_test_pred["q_10"] = pd.concat([raw_q10, raw_q90, pred], axis=1).min(axis=1)
                    final_test_pred["q_90"] = pd.concat([raw_q10, raw_q90, pred], axis=1).max(axis=1)
    save_dataframe(final_test_pred, os.path.join(cfg["paths"]["predictions_dir"], "test_predictions.csv"))

    metrics_df = pd.DataFrame(metrics_records)
    save_dataframe(metrics_df, os.path.join(cfg["paths"]["metrics_dir"], "model_metrics.csv"))

    lookup_df = pd.DataFrame(pair_model_lookup)
    save_dataframe(lookup_df, os.path.join(cfg["paths"]["artifacts_dir"], "pair_model_lookup.csv"))
    save_json(artifacts, os.path.join(cfg["paths"]["artifacts_dir"], "training_summary.json"))

    classes_out = classes_df.reset_index()
    save_dataframe(classes_out, os.path.join(cfg["paths"]["explainability_dir"], "pair_classes.csv"))

    logger.info("Training pipeline complete")
    return artifacts
