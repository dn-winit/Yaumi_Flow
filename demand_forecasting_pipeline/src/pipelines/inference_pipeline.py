import os

import pandas as pd

from ..utils.config_loader import load_config, ensure_dirs
from ..utils.logger import get_logger
from ..utils.io_utils import save_dataframe, load_pickle, load_json, pair_mask
from ..data_processing.loader import load_raw
from ..data_processing.aggregator import build_panel
from ..feature_engineering.builder import build_features
from ..utils.time_utils import period_offset, build_date_range
from ..utils.io_utils import ensure_tuple
from ..models.ensemble import weighted_average_ensemble


def _future_skeleton(history_df, group_keys, date_col, horizon, granularity):
    rows = []
    for keys, g in history_df.groupby(group_keys):
        last = g[date_col].max()
        for h in range(1, horizon + 1):
            d = last + period_offset(granularity, h)
            row = {date_col: d}
            for k, v in zip(group_keys, ensure_tuple(keys)):
                row[k] = v
            rows.append(row)
    return pd.DataFrame(rows)


def run_inference(config_path, on_step=None):
    _step = on_step or (lambda *_: None)

    cfg = load_config(config_path)
    ensure_dirs(cfg)
    logger = get_logger("inference", cfg["paths"]["logs_dir"], cfg["project"]["log_level"])

    date_col = cfg["data"]["date_col"]
    target_col = cfg["data"]["target_col"]
    group_keys = cfg["data"]["forecast_level"]
    meta_cols = cfg["data"].get("meta_cols", [])
    freq = cfg["data"]["granularity"]
    causal_cols = cfg["data"].get("causal_cols", [])
    activity_flag = cfg["data"].get("activity_flag", False)
    horizon = cfg["inference"]["forecast_horizon"]

    _step("data_collection", "running")
    logger.info("Loading data for inference")
    raw = load_raw(cfg["paths"]["raw_data"], date_col)
    all_cols = list(dict.fromkeys(
        group_keys + [date_col, target_col] + (meta_cols or []) +
        [cc["col"] for cc in causal_cols if cc["col"] in raw.columns]
    ))
    raw = raw[[c for c in all_cols if c in raw.columns]].copy()

    agg = build_panel(
        raw, group_keys, date_col, target_col, meta_cols, freq,
        fill_missing=cfg["data"].get("fill_missing_periods", True),
        fill_value=cfg["data"].get("zero_fill_value", 0.0),
        causal_cols=causal_cols,
        activity_flag=activity_flag,
    )

    _step("data_collection", "completed")
    _step("data_processing", "running")

    classes_path = os.path.join(cfg["paths"]["explainability_dir"], "pair_classes.csv")
    if not os.path.exists(classes_path):
        raise RuntimeError("Missing {} - run training first".format(classes_path))
    classes_df = pd.read_csv(classes_path).set_index(group_keys)
    agg = agg.merge(classes_df.reset_index()[group_keys], on=group_keys, how="inner")

    future = _future_skeleton(agg, group_keys, date_col, horizon, freq)
    future[target_col] = 0.0
    meta_present = [c for c in (meta_cols or []) if c in agg.columns]
    causal_present = [cc["col"] for cc in causal_cols if cc["col"] in agg.columns]
    if meta_present or causal_present:
        fill_cols = meta_present + causal_present
        fill_df = agg.sort_values(date_col).groupby(group_keys, as_index=False)[fill_cols].last()
        future = future.merge(fill_df, on=group_keys, how="left")
    if activity_flag and "activity_flag" not in future.columns:
        future["activity_flag"] = 0
    full = pd.concat([agg, future], ignore_index=True, sort=False)
    full_date_range = build_date_range(full[date_col].min(), full[date_col].max(), freq)

    _step("data_processing", "completed")
    _step("feature_engineering", "running")

    feats = build_features(
        full, group_keys, date_col, target_col, classes_df, cfg["feature_engineering"],
        holiday_cfg=cfg.get("holidays"),
        granularity=freq,
        full_date_range=full_date_range,
        target_encoding_source=agg,
    )
    _step("feature_engineering", "completed")

    summary = load_json(os.path.join(cfg["paths"]["artifacts_dir"], "training_summary.json"))
    schema = summary.get("schema", {})
    feature_cols = schema.get("feature_cols") or []
    if not feature_cols:
        raise RuntimeError("training_summary.json missing schema.feature_cols; retrain required")

    missing = [c for c in feature_cols if c not in feats.columns]
    for c in missing:
        feats[c] = 0.0
    if missing:
        logger.info("Added {} missing feature cols".format(len(missing)))

    lookup_path = os.path.join(cfg["paths"]["artifacts_dir"], "pair_model_lookup.csv")
    per_pair = cfg.get("model_selection", {}).get("strategy", "per_class") == "per_pair"
    lookup = None
    if per_pair and os.path.exists(lookup_path):
        lookup = pd.read_csv(lookup_path)
        logger.info("Loaded per-pair model lookup ({} pairs)".format(len(lookup)))

    _step("inference", "running")
    per_class = summary["per_class"]
    out_parts = []

    for cls, info in per_class.items():
        cls_pairs = classes_df[classes_df["class"] == cls].reset_index()[group_keys]
        cls_future = future.merge(cls_pairs, on=group_keys, how="inner")
        if cls_future.empty:
            continue
        cls_feats = feats.merge(cls_future[group_keys + [date_col]], on=group_keys + [date_col], how="inner")

        if lookup is not None:
            cls_lookup = lookup[lookup["class"] == cls]
            models_needed = cls_lookup["best_model"].unique()
        else:
            models_needed = [info["models_trained"][0]] if info.get("models_trained") else ["moving_average"]

        loaded_models = {}
        models_to_load = set(models_needed)
        ens_weights_path = os.path.join(cfg["paths"]["models_dir"], "{}_ensemble_weights.json".format(cls))
        ens_weights = None
        if "ensemble" in models_to_load:
            if os.path.exists(ens_weights_path):
                ens_weights = load_json(ens_weights_path)
                models_to_load.discard("ensemble")
                models_to_load.update(ens_weights.keys())

        for m_name in models_to_load:
            model_path = os.path.join(cfg["paths"]["models_dir"], "{}_{}.pkl".format(cls, m_name))
            if os.path.exists(model_path):
                try:
                    loaded_models[m_name] = load_pickle(model_path)
                except Exception:
                    pass

        for _, pair_row in cls_pairs.iterrows():
            pair_keys = tuple(pair_row[k] for k in group_keys)
            pair_feats = cls_feats[pair_mask(cls_feats, group_keys, pair_keys)]
            if pair_feats.empty:
                continue

            if lookup is not None:
                lk_row = cls_lookup[pair_mask(cls_lookup, group_keys, pair_keys)]
                best = lk_row["best_model"].iloc[0] if not lk_row.empty else "moving_average"
            else:
                best = models_needed[0]

            if best == "ensemble" and ens_weights:
                comp_preds = {}
                for comp_name in ens_weights:
                    comp_mdl = loaded_models.get(comp_name)
                    if comp_mdl is None:
                        continue
                    try:
                        cp = comp_mdl.predict(pair_feats, group_keys, date_col, target_col, feature_cols)
                        comp_preds[comp_name] = cp
                    except Exception:
                        continue
                if comp_preds:
                    preds = weighted_average_ensemble(comp_preds, {k: float(v) for k, v in ens_weights.items() if k in comp_preds})
                else:
                    preds = pair_feats[group_keys + [date_col]].copy()
                    preds["prediction"] = 0.0
            else:
                mdl = loaded_models.get(best)
                if mdl is None:
                    from ..models.moving_average import MovingAverageForecaster
                    mdl = MovingAverageForecaster()
                    mdl.fit(agg[pair_mask(agg, group_keys, pair_keys)], group_keys, date_col, target_col, [])
                    best = "moving_average"
                try:
                    preds = mdl.predict(pair_feats, group_keys, date_col, target_col, feature_cols)
                except Exception:
                    preds = pair_feats[group_keys + [date_col]].copy()
                    preds["prediction"] = 0.0

            if "p_demand" not in preds.columns:
                preds["p_demand"] = (preds["prediction"] > 0).astype(float)
                preds["qty_if_demand"] = preds["prediction"]
            preds["class"] = cls
            preds["model_used"] = best
            out_parts.append(preds)

    forecast = pd.concat(out_parts, ignore_index=True) if out_parts else pd.DataFrame()

    pi_cfg = cfg.get("evaluation", {}).get("prediction_intervals", {})
    if pi_cfg.get("enabled", False) and not forecast.empty:
        qi_parts = []
        for cls in per_class:
            qi_path = os.path.join(cfg["paths"]["models_dir"], "quantile_{}.pkl".format(cls))
            if not os.path.exists(qi_path):
                continue
            try:
                qi_model = load_pickle(qi_path)
                cls_pairs = classes_df[classes_df["class"] == cls].reset_index()[group_keys]
                cls_future_feats = feats.merge(
                    future.merge(cls_pairs, on=group_keys, how="inner")[group_keys + [date_col]],
                    on=group_keys + [date_col], how="inner",
                )
                if cls_future_feats.empty:
                    continue
                qi_pred = qi_model.predict(cls_future_feats, group_keys, date_col, target_col, feature_cols)
                qi_parts.append(qi_pred)
            except Exception as e:
                logger.info("Quantile inference failed for {}: {}".format(cls, e))
        if qi_parts:
            qi_all = pd.concat(qi_parts, ignore_index=True)
            qi_cols = [c for c in qi_all.columns if c.startswith("q_")]
            if qi_cols:
                for qc in qi_cols:
                    qi_all[qc] = qi_all[qc].clip(lower=0.0)
                if "q_10" in qi_cols and "q_90" in qi_cols:
                    qi_all["q_10"] = qi_all[["q_10", "q_90"]].min(axis=1)
                    qi_all["q_90"] = qi_all[["q_10", "q_90"]].max(axis=1)
                forecast = forecast.merge(
                    qi_all[group_keys + [date_col] + qi_cols],
                    on=group_keys + [date_col], how="left",
                )
                pred = forecast["prediction"]
                forecast["q_10"] = forecast[["q_10", "prediction"]].min(axis=1).clip(lower=0.0)
                forecast["q_90"] = forecast[["q_90", "prediction"]].max(axis=1)

    if not forecast.empty:
        expl_path = os.path.join(cfg["paths"]["explainability_dir"], "pair_explainability.csv")
        if os.path.exists(expl_path):
            expl = pd.read_csv(expl_path)
            forecast = forecast.merge(expl, on=group_keys, how="left", suffixes=("", "_expl"))

    save_dataframe(forecast, os.path.join(cfg["paths"]["predictions_dir"], "future_forecast.csv"))
    _step("inference", "completed")
    logger.info("Inference complete: {} rows".format(len(forecast)))
    return forecast
