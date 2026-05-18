# Yaumi Flow integration tests

API-level integration tests for every backend service. Each module owns a
subfolder; every endpoint has at least a happy-path test plus edge-case
coverage (unknown route, bad date, missing required param, malformed body).

## Layout

```
tests/
├── conftest.py              # Shared fixtures: base URLs, httpx client, db conn, dates
├── pytest.ini               # Test discovery + markers
├── common/
│   └── helpers.py           # assert_2xx, schema helpers, registry-driven inputs
├── data_import/              # port 8005 -- /api/v1/data
├── demand_forecasting/       # port 8002 -- /api/v1/forecast
├── recommended_order/        # port 8001 -- /api/v1/recommended-order
├── sales_supervision/        # port 8004 -- /api/v1/supervision
└── llm_analytics/            # port 8003 -- /api/v1/analytics
```

## Running

```bash
# Run everything (skips @pytest.mark.slow by default)
pytest tests/

# Run one module
pytest tests/data_import/

# Run one file
pytest tests/sales_supervision/test_session_lifecycle.py

# Include slow tests (LLM calls, full reconciliation refresh, etc.)
pytest tests/ -m "slow or not slow"

# Verbose (one line per test)
pytest tests/ -v

# Stop on first failure (good when chasing a regression)
pytest tests/ -x
```

## Markers

* `slow` — calls LLM endpoints or triggers a multi-minute reconciliation
  refresh. Skipped unless explicitly selected.
* `requires_db` — needs a live AIML DB connection. Auto-skips when the
  env vars aren't configured.
* `requires_live_data` — needs today's journey plan + recommendations
  on at least one configured route. Auto-skips when the route has no
  active session.

## Prerequisites

The tests **do not start services** — they assume the backends are
already running on their default ports (3000/8001/8002/8003/8004/8005).
Start them via `python -m <module>` per service, or via the dev
launcher, before running tests.

`.env` is loaded automatically by `conftest.py` so DB credentials and
route registry settings are available.
