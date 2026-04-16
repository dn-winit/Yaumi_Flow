# Yaumi Flow — Sales Operations Platform

AI-powered demand forecasting, order recommendation, and live sales supervision for FMCG van-sales distribution.

Built for **Yaumi** (Rashed Al Rashed & Sons Group) to optimise route-to-market operations across the UAE.

---

## Architecture

```
┌─────────────┐    ┌──────────────────┐    ┌─────────────────────┐
│  YaumiLive  │───▶│   Data Import     │───▶│  Demand Forecasting │
│  (Source DB) │    │   :8005           │    │  :8002              │
└─────────────┘    └────────┬─────────┘    └──────────┬──────────┘
                            │                          │
                            ▼                          ▼
                   ┌──────────────────┐    ┌─────────────────────┐
                   │ Sales Supervision │    │ Recommended Orders   │
                   │ :8004             │    │ :8001                │
                   └────────┬─────────┘    └──────────┬──────────┘
                            │                          │
                            ▼                          ▼
                   ┌──────────────────┐    ┌─────────────────────┐
                   │  LLM Analytics   │    │   React Webapp       │
                   │  :8003           │    │   :3000               │
                   └──────────────────┘    └─────────────────────┘
```

**5 FastAPI microservices** + **React / TypeScript / Vite** webapp, connected via REST APIs with a single-DB-reader architecture (only `data_import` touches YaumiLive directly).

---

## Services

### Data Import `:8005`
- ETL pipeline from YaumiLive (SQL Server) into shared CSVs
- EDA aggregation layer (sales overview, customer overview, business KPIs)
- Live customer/route sales queries with 60-second server cache
- Scheduled incremental import at 03:00 UAE time

### Demand Forecasting `:8002`
- ML ensemble model trained on historical sales patterns
- Per-item demand prediction with confidence intervals (q10–q90)
- Demand classification (smooth / erratic / intermittent / lumpy)
- 30K+ predictions per generation run

### Recommended Orders `:8001`
- 3-generator recommendation engine:
  - **History** — cycle-based analysis of each customer's buying pattern
  - **Peer matching** — lookalike-customer cross-sell via cosine similarity
  - **Basket co-occurrence** — items frequently bought together
- Per-route calibration (all thresholds derived from data, not hardcoded)
- Adaptive feedback loop learning from supervision outcomes
- Per-row explainability (Signals, WhyItem, WhyQuantity)

### Sales Supervision `:8004`
- Live session management for route supervisors
- Real-time visit scoring against YaumiLive actuals
- Unsold-item redistribution to remaining planned customers
- Unplanned-visit detection with live polling
- Session save with file + database persistence

### Analytics `:8003`
- Customer analysis, route review, and planning insights
- Structured prompt templates with configurable provider
- On-demand analysis triggered from the supervision UI

---

## Webapp

React 18 + TypeScript + Vite + Tailwind CSS v4

### Pages
- **Dashboard** — business KPIs, sales trends, customer overview, service health
- **Workflow** — three tabs:
  - **Van Load** — demand forecast with accuracy drawer (WAPE-based, spike-resistant)
  - **Recommended Orders** — per-customer recommendations with adoption drawer
  - **Supervision** — live session with planned/unplanned visit tabs
- **Admin** — data import, pipeline management, cache control

### Design system
- Centralised design tokens (`src/theme/tokens.ts`) — Yaumi brand crimson + gold
- Semantic Tailwind classes generated from tokens via `tailwind.config.ts`
- Reusable primitives: Card, Badge, Button, Modal, Drawer, Tabs, Table, MetricCard, KpiRow, ContextStrip, HighlightsStrip, Skeleton
- Unified chart theming across LineChart, BarChart, PieChart
- Auto-refreshing metrics via tiered React Query polling (live / dashboard / windowed / static)

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

# Python environment
python -m venv .venv
source .venv/bin/activate  # or .venv\Scripts\activate on Windows
pip install -r requirements.txt

# Webapp dependencies
cd webapp && npm install && cd ..

# Environment
cp .env.example .env
# Edit .env with your database credentials
```

### Run all services

```bash
bash scripts/start-all.sh
```

Or individually:

```bash
python -m data_import              # :8005
python -m demand_forecasting_pipeline  # :8002
python -m recommended_order        # :8001
python -m sales_supervision        # :8004
python -m llm_analytics            # :8003
cd webapp && npm run dev           # :3000
```

### Production build

```bash
cd webapp && npm run build   # outputs to webapp/dist/
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
│   └── scheduler/               # Cron jobs
│
├── demand_forecasting_pipeline/ # ML forecasting service
│   ├── api/                     # FastAPI routes
│   ├── artifacts/               # Trained models + predictions
│   ├── config/                  # Pipeline config (YAML)
│   └── src/                     # Training + inference
│
├── recommended_order/           # Recommendation engine
│   ├── api/                     # FastAPI routes + schemas
│   ├── config/                  # Safety clamps + settings
│   ├── core/                    # Engine + generators + calibration
│   │   ├── engine.py            # Orchestrator
│   │   ├── generators.py        # History / peer / basket / seed
│   │   ├── calibration.py       # Per-route data-driven thresholds
│   │   ├── explain.py           # Signal + Explanation builder
│   │   ├── feedback.py          # Adaptive feedback loop
│   │   ├── priority.py          # Adaptive priority scoring
│   │   ├── quantity.py          # Recency-weighted qty sizing
│   │   ├── cycle.py             # Purchase cycle detection
│   │   └── trend.py             # Trend analysis
│   ├── data/                    # Data manager
│   ├── models/                  # Domain models
│   ├── services/                # Storage + DB push
│   └── scheduler/               # Cron jobs + calibration
│
├── sales_supervision/           # Live supervision service
│   ├── api/                     # FastAPI routes
│   ├── config/                  # Scoring constants
│   ├── core/                    # Session + scoring + redistribution
│   ├── models/                  # Session schemas
│   └── services/                # Storage + live actuals client
│
├── analytics/                   # AI analytics service
│   ├── api/                     # FastAPI routes
│   ├── core/                    # Analysis client + prompt loader
│   └── prompts/                 # Structured prompt templates
│
├── webapp/                      # React frontend
│   ├── public/                  # Static assets (Yaumi logo)
│   ├── src/
│   │   ├── api/                 # API client modules
│   │   ├── components/          # UI primitives + charts
│   │   ├── hooks/               # React Query hooks + refresh tiers
│   │   ├── lib/                 # Format + colorize + date helpers
│   │   ├── pages/               # Dashboard / Workflow / Admin
│   │   ├── theme/               # Design tokens
│   │   └── types/               # TypeScript interfaces
│   ├── tailwind.config.ts
│   └── vite.config.ts
│
├── scripts/                     # Start/stop helpers
├── data/                        # Shared CSV directory
├── docker-compose.yml           # Container orchestration
├── Dockerfile.backend
├── Dockerfile.frontend
└── requirements.txt
```

---

## Key design decisions

- **Single DB reader**: only `data_import` queries YaumiLive; other services consume shared CSVs or call `data_import` via HTTP. Eliminates connection pool contention.
- **File-based recommendation store**: one CSV per route-date. `DbPusher` replicates to YaumiAIML as a one-way sync. No dual-write race conditions.
- **Data-driven calibration**: all recommendation thresholds (frequency floor, dormancy window, tier cuts, priority weights) are computed per-route from observed data. Zero hardcoded business numbers in the engine.
- **WAPE over MAPE**: forecast accuracy uses weighted absolute percentage error, which is robust to demand spikes and low-volume days.
- **Tiered polling**: React Query hooks use a shared refresh module (`hooks/refresh.ts`) with 5 cadence tiers (live 45s, dashboard 5m, windowed 10m, static 1h, pipeline 10s) so every metric across every tab stays current.

---

## License

Proprietary — Yaumi / Rashed Al Rashed & Sons Group. All rights reserved.
