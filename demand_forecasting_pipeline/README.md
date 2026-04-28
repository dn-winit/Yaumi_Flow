# Demand Forecasting Pipeline

Modular demand forecasting at any group-key x SKU level (default: `RouteCode` x `ItemCode`)
at daily granularity. Each (route, item) pair is classified into smooth / intermittent /
erratic / lumpy from ADI and CV² and then routed to a class-appropriate model family.
No hardcoding -- everything is driven from `config/config.yaml`.

## Boot

```
python -m demand_forecasting_pipeline       # FastAPI service on :8002
```

The service is the only entry point. Training and inference are triggered through the
HTTP API; nothing in this package is a runnable script.

## API surface

```
GET  /health                              service + artifact status
GET  /summary                             dashboard KPIs (accuracy, class mix, last forecast)
GET  /predictions/test                    test-set rows (with filters + pagination)
GET  /predictions/forecast                future-forecast rows
GET  /predictions/forecast/route-summary  per-route aggregates for the current forecast
GET  /metrics/models                      per-class model performance
GET  /explainability/classes/summary      pair counts by demand class
POST /pipeline/train                      run the training pipeline (background thread)
POST /pipeline/inference                  run inference + push to YaumiAIML
GET  /pipeline/status                     current state of train + inference
GET  /retrain/config                      auto-retrain settings + live drift
POST /retrain/config                      update auto-retrain settings
GET  /retrain/history                     last 10 auto-retrain runs
```

## Layout

```
demand_forecasting_pipeline/
  __main__.py                 single boot path -> uvicorn -> FastAPI
  observability.py            structured logging (structlog + stdlib)
  config/
    config.yaml               every ML knob, holiday/Hijri/salary regimes, routing rules
    settings.py               server + path settings (env-driven)
  api/
    app.py                    FastAPI factory, lifespan + auto-retrain timer
    dependencies.py           singleton wiring for services
    routes/                   one router per surface (health, summary, predictions, ...)
    schemas.py                pydantic request/response shapes
  services/
    pipeline_service.py       background train/inference + auto-push to YaumiAIML
    artifact_service.py       cached reads from artifacts/ for the API
    accuracy_service.py       cross-DB predicted-vs-actual comparison
    db_pusher.py              writes future_forecast.csv -> YaumiAIML.yf_demand_forecast
    retrain_scheduler.py      AutoRetrainConfig + drift detection + check_and_retrain
    cache.py                  TTL cache used by ArtifactService
    storage/                  filesystem read/write of artifacts (single backend)
  src/
    data_processing/          loader, validator, aggregator, lifecycle, anomaly, quality, cleaner, splitter, split_audit
    feature_engineering/      ADI/CV² classifier, calendar / holiday / Hijri / salary-cycle / lag / rolling / target-encoding
    models/                   naive, MA, croston (+ SBA), ETS, linear, RF, GBM, LGBM, XGB, two-stage, ensemble
    routing/                  per-pair model routing (router, rules, signals)
    evaluation/               metrics, conformal calibration
    pipelines/                train_pipeline.py, inference_pipeline.py
    tuning/                   optuna tuner
    utils/                    config loader, logger shim, io, time helpers
  artifacts/
    models/                   pickled per-class models + ensemble weights + quantile models
    predictions/              test_predictions.csv, future_forecast.csv
    metrics/                  model_metrics.csv
    explainability/           pair_classes.csv, pair_explainability.csv
    logs/                     pipeline.log
  data/
    retrain_config.json       persisted auto-retrain state
```

## How it works

1. Load raw daily sales, select required columns, run input validation.
2. Aggregate to the configured granularity per (RouteCode, ItemCode). Fill missing
   periods with zero so gaps are real.
3. Assign lifecycle flags (new launch, likely EOL) and detect suspicious zero runs.
4. Per-pair outlier treatment (skipped for flagged rows and intermittent demand).
5. Classify each pair (ADI, CV²) into smooth / intermittent / erratic / lumpy.
6. Build features per class -- lag, rolling, temporal, calendar (holiday + Hijri +
   salary cycle), intermittent-specific, plus target-encoded categorical features.
7. Time-based split into train / validation / test, audited per pair.
8. Per-pair routing rules pick the model menu (e.g. naive for tiny history,
   prefer Croston-SBA for very sparse intermittent, prefer LightGBM for heavy-tail erratic).
9. For each class, train its allowed models with optional Optuna tuning.
10. Per-pair model selection from validation metrics; ensemble where allowed.
11. Conformal calibration on the validation set produces per-pair quantile offsets.
12. Inference pipeline rebuilds features against the future skeleton and runs the
    chosen model per pair, emitting `prediction`, `p_demand`, `qty_if_demand`,
    `q_10`, `q_90`, plus explainability columns.
13. After a successful inference run, `pipeline_service` pushes
    `future_forecast.csv` to `YaumiAIML.yf_demand_forecast`. The downstream
    `data_import` service mirrors the table into `data/demand_forecast.csv` for
    `recommended_order` to consume.

## Two questions answered

- *When will demand happen* -- for intermittent / lumpy classes, the two-stage model
  produces a probability of demand (`p_demand`) and a quantity if demand happens
  (`qty_if_demand`). The `prediction` column is the gated value.
- *What will the demand be* -- the point prediction plus a calibrated `[q_10, q_90]`
  band, with explainability columns (`adi`, `cv2`, `mean_qty`, `nonzero_ratio`,
  `avg_gap_days`, ...) merged into every forecast row.
