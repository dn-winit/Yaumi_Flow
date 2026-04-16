# `data/` directory

Local staging area for raw CSVs. Populated by the `data_import` service.

## Files

| File | Source | Used by | Required? |
|------|--------|---------|-----------|
| `sales_recent.csv` | YaumiLive.VW_GET_SALES_DETAILS | demand_forecasting training | **Yes (for training)** |
| `customer_data.csv` | YaumiLive.VW_GET_SALES_DETAILS | (cache only) | No |
| `journey_plan.csv` | YaumiLive.VW_GET_JOURNEYPLAN_DETAILS | (cache only) | No |

## Important

- **All `.csv` files are gitignored** -- never committed.
- **DB is the source of truth.** These files are local cache + training input.
- **Services that need data (recommended_order, sales_supervision) read from DB directly**, not from these files.

## Refresh

Pull fresh data from DB:
```bash
curl -X POST http://localhost:8005/api/v1/data/import-all -d '{"mode":"incremental"}'
```

Or full refresh:
```bash
curl -X POST http://localhost:8005/api/v1/data/import -d '{"dataset":"sales_recent","mode":"full"}'
```
