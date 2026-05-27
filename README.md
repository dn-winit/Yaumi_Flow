# Yaumi Flow — Sales Operations Platform

AI-powered demand forecasting, order recommendation, and live sales supervision for FMCG van-sales distribution.

Built for **Yaumi** (Rashed Al Rashed & Sons Group) to optimise route-to-market operations across the UAE.

---

## Table of contents

1. [What it does](#what-it-does)
2. [Architecture](#architecture)
3. [Services](#services)
4. [Webapp](#webapp)
5. [Quick start](#quick-start)
6. [Project structure](#project-structure)
7. [Past performance — how the numbers are computed](#past-performance--how-the-numbers-are-computed)
8. [Source views (YaumiLive)](#source-views-yaumilive)
9. [Key design decisions](#key-design-decisions)
10. [License](#license)

---

## What it does

| Outcome | Surface |
|---|---|
| Predict next-day demand per (route, item) | `Forecasting` page → demand_forecasting_pipeline |
| Recommend customer-level orders for the rep | `Plan` page → recommended_order |
| Score live visits against actual sales | `Visit` page → sales_supervision |
| Compare rep's actual van load against our model | `Past performance` drawer → demand_forecasting_pipeline |
| AI customer briefings + post-visit analysis | LLM panels on `Visit` → llm_analytics |

---

## Architecture

```
┌─────────────┐    ┌──────────────────┐    ┌─────────────────────┐
│  YaumiLive  │───▶│   Data Import    │───▶│  Demand Forecasting │
│ (SQL Server)│    │   :8005          │    │  :8002              │
└─────────────┘    └────────┬─────────┘    └──────────┬──────────┘
                            │                          │
                            ▼                          ▼
                   ┌──────────────────┐    ┌─────────────────────┐
                   │ Sales Supervision│    │ Recommended Orders  │
                   │ :8004            │    │ :8001               │
                   └────────┬─────────┘    └──────────┬──────────┘
                            │                          │
                            ▼                          ▼
                   ┌──────────────────┐    ┌─────────────────────┐
                   │  LLM Analytics   │    │   React Webapp      │
                   │  :8003           │    │   :3000             │
                   └──────────────────┘    └─────────────────────┘
```

**5 FastAPI microservices** + **React / TypeScript / Vite** webapp.

**Single DB reader** — only `data_import` touches `YaumiLive` directly. Other services consume the shared CSVs or call `data_import` over HTTP for live data. No connection-pool contention.

---

## Services

### `:8005` Data Import

- ETL pipeline from `YaumiLive` (SQL Server) → shared CSVs in `data/`
- EDA aggregation (sales overview, business KPIs, forecast-vs-actual rows)
- Live customer/route sales queries with 60-second server cache
- Scheduled incremental import at **03:00 UAE time**

### `:8002` Demand Forecasting

- ML ensemble model trained on historical sales patterns
- Per-item demand prediction with confidence intervals (q10–q90)
- Demand classification (smooth / erratic / intermittent / lumpy)
- Auto-retrain config + drift status (predicted vs `YaumiLive` actuals)
- **Past-performance endpoint** — rep van load vs our recommended van load vs actually sold ([details below](#past-performance--how-the-numbers-are-computed))

### `:8001` Recommended Orders

- 5-generator recommendation engine:
  - **History** — cycle-based analysis of each customer's buying pattern
  - **Peer matching** — lookalike-customer cross-sell via cosine similarity
  - **Basket co-occurrence** — items frequently bought together
  - **Reactivation** — top van items for dormant customers (silent past `dormancy_days`)
  - **Seed** — top van items for first-time / unknown customers
- **Per-route AND per-customer calibration** — no hardcoded business numbers. Each customer carries a profile (HEAVY / MEDIUM / LIGHT basket tier, personal completion gate, personal recency half-life) derived from their own history.
- Engine filters customer history to `TrxDate < target_date` (the "as-of cutoff") so today's already-completed sales never bias today's recommendations.
- Adaptive feedback loop learning from supervision outcomes.
- Per-row explainability (Signals, WhyItem, WhyQuantity, Confidence) + class-aware accuracy (smooth ±10%, intermittent ±20%, erratic ±30%, lumpy ±40%).

### `:8004` Sales Supervision

- Live session management for route supervisors
- Real-time visit scoring against `YaumiLive` actuals (fetched via `data_import`)
- Unsold-item redistribution to remaining planned customers
- Unplanned-visit detection with live polling
- Session save with file + database persistence

### `:8003` LLM Analytics

- Customer analysis, route review, and pre-visit briefings
- Provider-agnostic (Groq / OpenAI / Anthropic) via configurable prompts
- TTL-bounded JSON cache + token-bucket rate limiting
- On-demand analysis triggered from the supervision UI

---

## Webapp

**React 18 + TypeScript + Vite + Tailwind CSS v4**

### Pages

| Page | Purpose |
|---|---|
| **Dashboard** | Business KPIs, sales trends, forecast accuracy, lost-opportunity tile |
| **Forecasting** | Pipeline status (train / inference / forecast / push), model metrics, auto-retrain config + drift |
| **Workflow → Plan** | Warehouse-grouped route grid → click a route → Van Load detail (top-10 chart, items table, *Past performance* + *Upcoming plan* drawers) |
| **Workflow → Visit** | Auto-initialises a live supervision session with planned + unplanned customer tiles, per-customer recording, AI route review and pre-visit briefings. Reachable only via Plan's *"Continue to Visit →"* |
| **Admin** | Data import status, LLM cache control |

### Design system

- Centralised design tokens in `src/theme/tokens.ts` — Yaumi brand crimson + gold
- Semantic Tailwind classes generated from tokens via `tailwind.config.ts`
- Reusable primitives: `Card`, `Badge`, `Button`, `Modal`, `Drawer`, `Tabs`, `Table`, `MetricCard`, `KpiRow`, `ContextStrip`, `HighlightsStrip`, `Skeleton`, `DatePicker`, `CommandPalette`
- Unified chart theming across `LineChart`, `BarChart`, `HorizontalBarChart`, `PieChart` — auto `dd-mm-yyyy` date-axis formatting; daily charts pad to the lookback's `active_dates` and scroll horizontally when the window exceeds the container so every working day stays visible without label collision
- Every rendered date is `dd-mm-yyyy` via `lib/date.ts#fmtDate`; backend transport stays canonical `yyyy-mm-dd`
- Tiered React Query polling (`hooks/refresh.ts`):

  | Tier | Cadence |
  |---|---|
  | live | 45 s |
  | pipeline | 10 s |
  | dashboard | 5 m |
  | windowed | 10 m |
  | static | 1 h |

---

## Quick start

### Prerequisites

- Python 3.11+
- Node.js 18+
- ODBC Driver 17 for SQL Server
- Access to `YaumiLive` and `YaumiAIML` databases

### Setup

```bash
# Clone and enter
cd forecast_new

# Python environment (root requirements.txt aggregates every service)
python -m venv .venv
source .venv/bin/activate            # macOS / Linux / Git Bash
# .venv\Scripts\activate              # Windows cmd / PowerShell
pip install -r requirements.txt

# Webapp dependencies
cd webapp && npm install && cd ..

# Environment
cp .env.example .env
# Edit .env with your database + LLM credentials
```

### Run all services

```bash
python scripts/serve_local.py                # backends + webapp, health-gated startup
python scripts/serve_local.py --skip-webapp  # backends only
```

Ctrl-C stops every subprocess cleanly. If any backend exits unexpectedly, the launcher tears the rest down.

Or run individually:

```bash
python -m data_import                  # :8005
python -m demand_forecasting_pipeline  # :8002
python -m recommended_order            # :8001
python -m sales_supervision            # :8004
python -m llm_analytics                # :8003
cd webapp && npm run dev               # :3000
```

### Production build

```bash
cd webapp && npm run build       # outputs to webapp/dist/
```

### Docker

```bash
docker compose up --build        # all 5 services + nginx-proxied webapp
```

### Testing

```bash
# Default: skip slow tests + tests that need a live DB
pytest tests/

# Per-module
pytest tests/data_import/  tests/sales_supervision/  ...

# Include LLM calls / full reconciliation refreshes
pytest tests/ -m "slow or not slow"

# Verbose + stop on first failure
pytest tests/ -vx
```

Markers (registered in `tests/pytest.ini`):
- `slow` — calls real LLM endpoints or triggers a multi-minute reconciliation
- `requires_db` — needs a live YaumiAIML connection (auto-skip when unset)
- `requires_live_data` — needs today's plan on at least one configured route

CI runs `python -m compileall` + webapp `tsc -b --noEmit` on every push to `staged` / `main`. Pytest, ruff, and end-to-end checks run locally before push (the live DB isn't reachable from GitHub-hosted runners).

### Lint / format

```bash
pip install ruff
ruff check .          # E, F, W, I, B, UP rules from pyproject.toml
ruff format .         # in-place formatter
```

---

## Multi-worker deployment

Every scheduler-bearing service uses **filesystem leader-election** (`common.leader_election`) so under `uvicorn --workers N` only ONE worker per host fires the cron. Followers boot the API but skip the scheduler.

| Service | Lock path (default) | Env override |
|---|---|---|
| `data_import` | `<data-root>/imports/scheduler.lock` | `DI_SCHEDULER_LOCK_PATH` |
| `demand_forecasting` | `<data-root>/forecast/scheduler.lock` | `DF_SCHEDULER_LOCK_PATH` |
| `recommended_order` | `<data-root>/recommendations/scheduler.lock` | `RO_SCHEDULER_LOCK_PATH` |
| `sales_supervision` | `<data-root>/supervision/scheduler.lock` | `SS_SCHEDULER_LOCK_PATH` |

Filesystem-scoped advisory lock (POSIX `flock` / Windows `msvcrt.locking`); kernel releases on process exit. **Multi-host deployments need a distributed lease layered on top** — current lock only coordinates workers within one host. Set `YF_LEADER_LOCK_DISABLE=1` only for single-worker dev / tests where the lock file is unavailable.

The scheduler-audit listener (`common.scheduler_audit.attach_audit`) writes one row per cron fire into `yf_scheduler_log` so operations can confirm crons actually fired on time (independent of job duration).

---

## Project structure

```
forecast_new/
├── data_import/                 # ETL + EDA service
│   ├── api/                     # FastAPI routes + schemas
│   ├── config/                  # Settings + DB config
│   ├── core/                    # Database connectors
│   ├── services/                # EDA + import logic
│   └── scheduler.py             # Cron jobs
│
├── demand_forecasting_pipeline/ # ML forecasting service
│   ├── api/                     # FastAPI routes (pipeline / retrain / metrics / reconciliation)
│   ├── artifacts/               # Trained models + predictions
│   ├── config/                  # Pipeline config (YAML)
│   ├── services/                # Pipeline + accuracy + artifact + retrain + reconciliation
│   └── src/                     # Training + inference
│
├── recommended_order/           # Recommendation engine
│   ├── api/                     # FastAPI routes + schemas
│   ├── config/                  # Safety clamps + settings
│   ├── core/                    # Engine + generators + calibration + explain
│   ├── data/                    # Data manager
│   ├── models/                  # Domain models
│   ├── services/                # Storage + DB push + adoption + planning
│   └── scheduler/               # Cron jobs + calibration
│
├── sales_supervision/           # Live supervision service
│   ├── api/                     # FastAPI routes (health + session lifecycle)
│   ├── config/                  # Scoring constants
│   ├── core/                    # Session + scoring + redistribution
│   ├── models/                  # Session schemas
│   └── services/                # Storage + DB save + live actuals client
│
├── llm_analytics/               # AI analytics service
│   ├── api/                     # FastAPI routes
│   ├── cache/                   # Response caching (gitignored)
│   ├── config/                  # Settings + prompt YAMLs
│   ├── core/                    # Analyzer + client + formatter + prompt loader
│   ├── models/                  # Pydantic schemas
│   └── services/                # Cache + rate limiter
│
├── webapp/                      # React frontend
│   ├── public/                  # Static assets (Yaumi logo)
│   ├── src/
│   │   ├── api/                 # Axios clients per service + shared TIMEOUTS
│   │   ├── components/          # UI primitives + charts + layout
│   │   ├── config/              # Routes, API endpoints, query client, module info
│   │   ├── hooks/               # React Query hooks + refresh tiers
│   │   ├── lib/                 # date / format / colorize helpers
│   │   ├── pages/               # Dashboard / Workflow / Pipeline / Admin / Supervision
│   │   ├── theme/               # Design tokens
│   │   └── types/               # TypeScript interfaces
│   ├── tailwind.config.ts
│   └── vite.config.ts
│
├── scripts/                     # serve_local.py + backfill_*.py + verify_*.py + create_tables.sql
├── data/                        # Shared CSV directory (gitignored content)
├── docker-compose.yml           # Container orchestration
├── Dockerfile.backend           # Shared Python image (5 services)
├── Dockerfile.frontend          # nginx-served webapp build
├── nginx.conf                   # Reverse proxy for the docker-compose web service
├── render.yaml                  # Render Blueprint (one-click multi-service deploy)
└── requirements.txt             # Aggregator of per-service requirements
```

---

## Past performance — how the numbers are computed

The Plan page's *Past performance* drawer compares **what the rep actually did** against **what our model would have recommended**, scoped to the items we forecasted. Every number is auditable end-to-end.

### Item scope (the "anchor")

For each (route, window) we compute the anchor:

```
ANCHOR = { ItemCode  |  exists in demand_forecast.csv
                       AND  RouteCode = route
                       AND  TrxDate   ∈ window
                       AND  Predicted > 0 }
```

Items the rep loaded but we didn't predict are **excluded**. We never claim phantom credit on items we made no call on.

### Window: `(start_date, end_date)`

Every dashboard and drawer surface accepts an arbitrary inclusive date range on the wire (`start_date=YYYY-MM-DD&end_date=YYYY-MM-DD`). Both bounds are ISO-validated server-side with `^\d{4}-\d{2}-\d{2}$`; `start_date > end_date` and out-of-cap ranges return `available=False` with a clear `message`.

| Surface | Default window |
|---|---|
| Dashboard | Trailing 30 calendar days through **today** |
| AccuracyDrawer (past performance) | Trailing 30 calendar days through **lastActiveDate** (from `/eda/last-active-date`) |
| AdoptionDrawer (recommendation adoption) | Trailing 30 calendar days through **lastActiveDate** |

`lastActiveDate = MAX(TrxDate) FROM sales_recent` so the drawer defaults never land on a zero-data weekend or holiday. Days with no activity inside the window contribute zero rows; the daily chart still renders the X-axis tick.

### Rep van load (per day)

```
rep_van_load[d]   =   past_leftover[d]  +  today_allocation[d]
```

| Component | Source | Definition |
|---|---|---|
| `past_leftover[d]` | `VW_GET_CLOSING_STOCK` | `ClosingQty` on `(d − 1)` for items in `ANCHOR`. If no row exists for `(item, d − 1)` we treat opening as **0** — direct, single-day lookup, no forward-fill across multiple days. |
| `today_allocation[d]` | `VW_GET_LOAD_ALLOCATION_DETAILS` | `Σ AllocatedPC` for `(route, d, item ∈ ANCHOR)`. If no row exists for `(item, d)` we treat allocation as **0**. |

**Why "missing = 0" is correct:** the source schema **never logs `ClosingQty = 0`** — when an item sells out, the row simply disappears. A missing row IS the system's way of saying "nothing left." Empirically validated on **21,073 (item, day) cells across all 12 routes × 60 days**:

- **94.4% of missing-closing cells** also satisfy the operational identity for zero leftover (sold ≥ what came onto the truck).
- **Only 5.6% of cells** have unrecorded leftover, dominated by *"allocated but never physically loaded"* depot rows that the 4 source views can't disambiguate.
- **Identity holds 74.7%** of the time when both closings logged (views are reliable when populated).
- Closing logging frequency tracks demand class: lumpy SKUs 81%, smooth SKUs 2% — closings appear exactly when leftover physically exists.

We under-count rather than fabricate. Rep van load is a **floor**, never an over-claim.

### Recommended van load (per day) — V2 reconciliation

The engine layers two corrections on top of the model's raw forecast:

```
L1b  pair-maturity bias shrinkage
     effective_bias = bias_pct × min(1, n_active_days / threshold)
     Pairs with too few sale-days for the bias estimate to be trustworthy
     get a proportionally weaker correction.

L1   bias correction
     P_corrected = Predicted / (1 + effective_bias)
     30-day rolling mean of (Predicted − Actual)/Actual, capped at ±50%.

L4   class-aware quantile loading (newsvendor)
     target = interpolate(q_low, P_corrected, q_high) at class_quantile
     smooth=q50  intermittent=q40  erratic=q35  lumpy=q30
     Lumpy/erratic items deliberately load below the mean.

L2   leftover-aware fresh issuance
     fresh_i  =  max(0,  target_i  −  leftover_i)
     recommended_van_load_total  =  Σ_i (leftover_i + fresh_i)
```

`bias_pct(route, item)` = rolling 30-day mean of `(Predicted − Actual) / Actual`, clipped at ±50%.
`leftover_i` = `ClosingQty[d−1]` for `(route, item)`, 0 if no row.
`q_low` / `q_high` come from the model's `q_10` / `q_90` columns (predictive interval). Missing columns make L4 fall back to `target = P_corrected` for that row (no shift), so the policy degrades cleanly to V5_b on legacy data.

### Actually sold (per day)

```
actual_sold[d]   =   Σ TotalQuantity   from VW_GET_SALES_DETAILS
                     where TrxType = 'SalesInvoice'
                       AND ItemType = 'OrderItem'
                       AND RouteCode = route
                       AND TrxDate = d
                       AND ItemCode ∈ ANCHOR
```

### Recommendation match (window total)

```
recommendation_match_pct  =  min(recommended_van_load_total, actual_sold_total)
                             /  max(recommended_van_load_total, actual_sold_total)
                             ×  100
```

Bounded fill-ratio accuracy. Symmetric (a 2× over-allocation and a 0.5× under-allocation both score 50%), naturally in `[0, 100]` — replaces the WAPE-style formula that pinned the metric to 0% on heavy-leftover days.

### Holding cost saved (window total)

Per item:

```
rep_excess_i  =  max(rep_van_load_i           − sold_i, 0)
our_excess_i  =  max(recommended_van_load_i   − sold_i, 0)

rep_holding   =  Σ_i  rep_excess_i  ×  avg_unit_price_i
our_holding   =  Σ_i  our_excess_i  ×  avg_unit_price_i
holding_savings = rep_holding − our_holding
```

`avg_unit_price_i` is quantity-weighted from `AvgUnitPrice` on `VW_GET_SALES_DETAILS` over the same `(route, anchor, window)` slice. `max(·, 0)` excludes under-allocated items — those are a lost-sales cost, not a holding cost.

### Identity guarantee

For any window and any filter, **chart-sum equals tile-total** for every series:

```
Σ_d  rep_van_load[d]            ==  rep_van_load_total
Σ_d  recommended_van_load[d]    ==  recommended_van_load_total
Σ_d  actual_sold[d]             ==  actual_sold_total
```

Verified by API contract — daily values and aggregate totals are computed from the same anchor-scoped queries.

---

## Source views (YaumiLive)

The platform reads four SQL Server views. **Only `data_import` touches them directly.**

| View | Used for | Key columns |
|---|---|---|
| `VW_GET_SALES_DETAILS` | Sales + returns (single source, split by `TrxType`) | `TrxType`, `ItemType`, `QuantityInPCs`, `UnitPrice`, `TrxDate`, `RouteCode`, `ItemCode` |
| `VW_GET_LOAD_ALLOCATION_DETAILS` | Today's depot-issued allocation | `AllocatedPC`, `TrxDate`, `RouteCode`, `ItemCode` |
| `VW_GET_CLOSING_STOCK` | End-of-day stock per (route, item) | `ClosingQty`, `TrxDate`, `RouteCode`, `ItemCode` |
| `VW_GET_LOAD_REQUEST_DETAILS` | Rep's load request (vs allocation) | (currently unused — reserved for future fulfilment-gap reporting) |

**TrxType vocabulary in `VW_GET_SALES_DETAILS`:**

| TrxType | ItemType | Sign of `QuantityInPCs` | Used as |
|---|---|---|---|
| `SalesInvoice` | `OrderItem` | Positive | `sold` |
| `Bad Return` | `OrderItem` | Negative (sign-flipped) | `bad_return` (damaged write-off) |
| `Good Return` | `OrderItem` | Negative (sign-flipped) | `good_return` (saleable back to depot) |
| `SalesInvoice` | `FreeItem` | Positive | Currently filtered out (promo / sample) |

---

## Key design decisions

- **Single DB reader** — only `data_import` queries `YaumiLive`; other services consume shared CSVs or call `data_import` via HTTP. Eliminates connection pool contention.
- **File-based recommendation store** — one CSV per route-date. `DbPusher` replicates to `YaumiAIML` as a one-way sync. No dual-write race conditions.
- **Data-driven calibration** — all recommendation thresholds (frequency floor, dormancy window, tier cuts, priority weights) are computed per-route from observed data. Zero hardcoded business numbers in the engine.
- **Class-aware composite accuracy** — every accuracy tile (Pipeline baseline, drift recent, Past-performance drawer) routes through `composite_summary()` in `demand_forecasting_pipeline/src/evaluation/metrics.py`. Each item is held to a fair miss tolerance based on its Syntetos-Boylan-Croston demand pattern (smooth ±10%, intermittent ±20%, erratic ±30%, lumpy ±40%); only misses beyond tolerance feed the WAPE numerator. One helper, one tolerance map, mirrored in `webapp/src/lib/format.ts` — no parallel formulas can drift across surfaces.
- **Atomic DB writes** — every push (`recommended_order/db_pusher`, `demand_forecasting_pipeline/db_pusher`, `sales_supervision/db_saver`) runs `DELETE+INSERT` in a single transaction with `try/finally` + explicit rollback. Bulk writes carry `cursor.timeout` so a slow warehouse cannot hang the writer; reads use a separate live timeout pair on `data_import` so interactive supervisor calls stay snappy.
- **Linear Plan → Visit flow** — Visit is reachable only after a route is picked in Plan (URL guard + disabled stepper step + gated keyboard shortcut). No accidental jumps into a stale-context session.
- **One canonical date format** — every backend payload speaks `yyyy-mm-dd`; the UI funnels every rendered date through `lib/date.ts#fmtDate` so the user always sees `dd-mm-yyyy`. Charts auto-detect ISO ticks via `components/charts/formatters.ts`.
- **Centralised request budgets** — `webapp/src/api/client.ts#TIMEOUTS` exposes `default` (30 s) and `heavy` (3 min) so every long-running mutation reads from the same constant.
- **Tiered polling** — React Query hooks share a refresh module with 5 cadence tiers so every metric across every tab stays current without re-fetch storms.
- **Conservative reconstruction over phantom precision** — when source data is incomplete (e.g. missing closing stock rows, oversold cells), we under-claim rather than fabricate. Past-performance leftover specifically uses the *direct* prior-day `ClosingQty` only — no forward-fill, no recovery via identity, no extrapolation. Validated empirically across 21,073 cells (12 routes × 60 days): "missing = 0" is correct for ≥94% of cells, with the residual 5.6% structurally unrecoverable from the 4 available views.

---

## License

Proprietary — Yaumi / Rashed Al Rashed & Sons Group. All rights reserved.
