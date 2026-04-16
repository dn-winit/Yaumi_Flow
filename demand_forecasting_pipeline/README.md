# Demand Forecasting Pipeline

Modular demand forecasting at any group-key x SKU level (default: Customer Channel x SKU)
with monthly aggregation. Handles intermittent / lumpy / smooth / erratic series differently
based on ADI and CV-squared classification. No hardcoding - everything is driven from
`config/config.yaml`.

## Layout

```
demand_forecasting_pipeline/
  config/
    config.yaml
  src/
    utils/                  config loader, logger, io
    data_processing/        loader, aggregator, per-pair cleaner, splitter
    feature_engineering/    ADI/CV2 classifier, explainability, temporal/lag/rolling/intermittent
    models/                 one file per model (naive, MA, croston, ets, linear, RF, GBM, LGBM, XGB, two-stage, ensemble)
    tuning/                 optuna tuner
    evaluation/             metrics
    pipelines/              train_pipeline.py, inference_pipeline.py
  artifacts/
    models/                 pickled models per class
    predictions/            test_predictions.csv, future_forecast.csv
    metrics/                model_metrics.csv
    explainability/         pair_explainability.csv, pair_classes.csv
    logs/                   pipeline.log
  run_train.py
  run_inference.py
  requirements.txt
```

## How it works

1. Load raw daily sales, select required columns.
2. Aggregate to monthly per (Channel, SKU). Fill missing months with zero so gaps are real.
3. Classify each pair using ADI and CV-squared into smooth / intermittent / erratic / lumpy.
4. Per-pair outlier treatment (skipped for intermittent/lumpy by default).
5. Compute explainability metrics per pair (ADI, CV2, mean/median/min/max/std, gaps).
6. Build features per class - lag, rolling, temporal and intermittent-specific features.
7. Time-based split into train / validation / test.
8. For each class, train its enabled models. Optional Optuna tuning for ML models.
9. Build a weighted ensemble using validation metrics where appropriate.
10. Pick the best model per class by the chosen selection metric. Save artifacts.
11. Inference pipeline reuses trained models to produce future month forecasts.

## Two questions answered

- "When will demand happen": for intermittent / lumpy classes, the two-stage model produces
  a probability of demand (`p_demand`) and a separate quantity if demand happens
  (`qty_if_demand`). The `prediction` column is the gated value.
- "What will the demand be": the quantity prediction itself, with explainability columns
  (ADI, CV2, mean, median, min, max, std, nonzero ratio) merged into the output.

## Run

```
python demand_forecasting_pipeline/run_train.py
python demand_forecasting_pipeline/run_inference.py
```

Edit `config/config.yaml` to switch grouping (e.g. Outlet ID + SKU Code), change horizons,
toggle tuning, change enabled models, adjust ADI/CV2 thresholds, etc. Nothing is hardcoded.
