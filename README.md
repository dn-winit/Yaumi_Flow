# Yaumi Flow — Sales Operations Platform

AI-powered demand forecasting, order recommendation, and live sales supervision for FMCG van-sales distribution.

Built for **Yaumi** (Rashed Al Rashed & Sons Group) to optimise route-to-market operations across the UAE.

---

## Architecture

```
┌─────────────┐    ┌──────────────────┐    ┌─────────────────────┐
│  YaumiLive  │───▶│   Data Import    │───▶│  Demand Forecasting │
│  (SQL Server)│    │   :8005          │    │  :8002              │
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

**5 FastAPI microservices** + **React / TypeScript / Vite** webapp, connected via REST APIs with a single-DB-reader architecture (only `data_import` touches YaumiLive directly; `sales_supervision` calls `data_import` over HTTP for live actuals).

---

## Services

### Data Import `:8005`
- ETL pipeline from YaumiLive (SQL Server) into shared CSVs
- EDA aggregation layer (sales overview, business KPIs, forecast-vs-actual rows)
- Live customer/route sales queries with 60-second server cache (consumed cross-service by `sales_supervision`)
- Scheduled incremental import at 03:00 UAE time

### Demand Forecasting `:8002`
- ML ensemble model trained on historical sales patterns
- Per-item demand prediction with confidence intervals (q10–q90)
- Demand classification (smooth / erratic / intermittent / lumpy)
- Auto-retrain config + drift status (predicted vs YaumiLive actuals)

### Recommended Orders `:8001`
- 5-generator recommendation engine:
  - **History** — cycle-based analysis of each customer's buying pattern
  - **Peer matching** — lookalike-customer cross-sell via cosine similarity
  - **Basket co-occurrence** — items frequently bought together
  - **Reactivation** — top van items for dormant customers (silent past dormancy_days)
  - **Seed** — top van items for first-time / unknown customers
- **Per-route AND per-customer calibration** — no hardcoded business numbers. Each customer carries a profile (HEAVY / MEDIUM / LIGHT basket tier, personal completion gate, personal recency half-life) derived from their own history; engine filters customer history to `TrxDate < target_date` (the "as-of cutoff") so today's already-completed sales never bias today's recommendations.
- Adaptive feedback loop learning from supervision outcomes
- Per-row explainability (Signals, WhyItem, WhyQuantity, Confidence) + class-aware accuracy (smooth 10% / intermittent 20% / erratic 30% / lumpy 40% tolerance)

### Sales Supervision `:8004`
- Live session management for route supervisors
- Real-time visit scoring against YaumiLive actuals (fetched via `data_import`)
- Unsold-item redistribution to remaining planned customers
- Unplanned-visit detection with live polling
- Session save with file + database persistence

### LLM Analytics `:8003`
- Customer analysis, route review, and pre-visit briefings
- Provider-agnostic (Groq / OpenAI / Anthropic) via configurable prompts
- TTL-bounded JSON cache + token-bucket rate limiting
- On-demand analysis triggered from the supervision UI

---

## Webapp

React 18 + TypeScript + Vite + Tailwind CSS v4

### Pages
- **Dashboard** — business KPIs, sales trends, forecast accuracy, lost-opportunity tile
- **Forecasting** — pipeline status (train / inference / forecast / push), model metrics, auto-retrain config + drift
- **Workflow** — two-step supervisor flow:
  - **Plan** — warehouse-grouped route grid → click a route → Van Load detail (top-10 chart, items table, *Past performance* + *Upcoming plan* drawers)
  - **Visit** — reachable only from Plan's "Continue to Visit →"; auto-initialises a live supervision session with planned + unplanned customer tiles, per-customer recording, AI route review and pre-visit briefings. Same *Past performance* / *Upcoming plan* drawer pair as Plan, so vocabulary stays unified across both steps.
- **Admin** — data import status, LLM cache control

### Design system
- Centralised design tokens (`src/theme/tokens.ts`) — Yaumi brand crimson + gold
- Semantic Tailwind classes generated from tokens via `tailwind.config.ts`
- Reusable primitives: Card, Badge, Button, Modal, Drawer, Tabs, Table, MetricCard, KpiRow, ContextStrip, HighlightsStrip, Skeleton, DatePicker, CommandPalette
- Unified chart theming across LineChart, BarChart, HorizontalBarChart, PieChart with auto dd-mm-yyyy date-axis formatting; daily charts pad to the lookback's `active_dates` and scroll horizontally when the window exceeds the container so every working day stays visible without label collision
- All dates rendered as `dd-mm-yyyy` via `lib/date.ts#fmtDate`; backend transport stays canonical `yyyy-mm-dd`
- Tiered React Query polling (`hooks/refresh.ts`): live 45s · pipeline 10s · dashboard 5m · windowed 10m · static 1h

---

## Quick start

### Prerequisites
- Python 3.11+
- Node.js 18+
- ODBC Driver 17 for SQL Server
- Access to YaumiLive and YaumiAIML databases

### Setup

```bash
# Clone and enter
cd forecast_new

# Python environment (the root requirements.txt aggregates every service)
python -m venv .venv
source .venv/bin/activate   # or .venv\Scripts\activate on Windows
pip install -r requirements.txt

# Webapp dependencies
cd webapp && npm install && cd ..

# Environment
cp .env.example .env
# Edit .env with your database + LLM credentials
```

### Run all services

```bash
bash scripts/start-all.sh        # Linux / macOS / Git Bash
scripts\start-all.bat            # Windows cmd
```

Or individually:

```bash
python -m data_import                  # :8005
python -m demand_forecasting_pipeline  # :8002
python -m recommended_order            # :8001
python -m sales_supervision            # :8004
python -m llm_analytics                # :8003
cd webapp && npm run dev               # :3000
```

Stop everything:

```bash
bash scripts/stop-all.sh
```

### Production build

```bash
cd webapp && npm run build       # outputs to webapp/dist/
```

### Docker

```bash
docker compose up --build        # all 5 services + nginx-proxied webapp
```

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
│   ├── api/                     # FastAPI routes (pipeline / retrain / metrics)
│   ├── artifacts/               # Trained models + predictions
│   ├── config/                  # Pipeline config (YAML)
│   ├── services/                # Pipeline + accuracy + artifact + retrain
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
├── scripts/                     # start-all / stop-all + create_tables.sql
├── data/                        # Shared CSV directory (gitignored content)
├── docker-compose.yml           # Container orchestration
├── Dockerfile.backend           # Shared Python image (5 services)
├── Dockerfile.frontend          # nginx-served webapp build
├── nginx.conf                   # Reverse proxy for the docker-compose web service
├── render.yaml                  # Render Blueprint (one-click multi-service deploy)
├── railway.json                 # Railway deployment hint
├── Procfile                     # Single-process Heroku-style fallback
└── requirements.txt             # Aggregator of per-service requirements
```

---

## Key design decisions

- **Single DB reader** — only `data_import` queries YaumiLive; other services consume shared CSVs or call `data_import` via HTTP. Eliminates connection pool contention.
- **File-based recommendation store** — one CSV per route-date. `DbPusher` replicates to YaumiAIML as a one-way sync. No dual-write race conditions.
- **Data-driven calibration** — all recommendation thresholds (frequency floor, dormancy window, tier cuts, priority weights) are computed per-route from observed data. Zero hardcoded business numbers in the engine.
- **Class-aware composite accuracy** — every accuracy tile (Pipeline baseline, drift recent, Past-performance drawer) routes through `composite_summary()` in `demand_forecasting_pipeline/src/evaluation/metrics.py`. Each item is held to a fair miss tolerance based on its Syntetos-Boylan-Croston demand pattern (smooth ±10%, intermittent ±20%, erratic ±30%, lumpy ±40%); only misses beyond tolerance feed the WAPE numerator. One helper, one tolerance map, mirrored in `webapp/src/lib/format.ts` — no parallel formulas can drift across surfaces.
- **Atomic DB writes** — every push (`recommended_order/db_pusher`, `demand_forecasting_pipeline/db_pusher`, `sales_supervision/db_saver`) runs DELETE+INSERT in a single transaction with try/finally + explicit rollback. Bulk writes carry `cursor.timeout` so a slow warehouse cannot hang the writer; reads use a separate live timeout pair on `data_import` so interactive supervisor calls stay snappy.
- **Linear Plan → Visit flow** — Visit is reachable only after a route is picked in Plan (URL guard + disabled stepper step + gated keyboard shortcut). No accidental jumps into a stale-context session.
- **One canonical date format** — every backend payload speaks `yyyy-mm-dd`; the UI funnels every rendered date through `lib/date.ts#fmtDate` so the user always sees `dd-mm-yyyy`. Charts auto-detect ISO ticks via `components/charts/formatters.ts`.
- **Centralised request budgets** — `webapp/src/api/client.ts#TIMEOUTS` exposes `default` (30 s) and `heavy` (3 min) so every long-running mutation reads from the same constant.
- **Tiered polling** — React Query hooks share a refresh module with 5 cadence tiers so every metric across every tab stays current without re-fetch storms.

---

## License

Proprietary — Yaumi / Rashed Al Rashed & Sons Group. All rights reserved.
