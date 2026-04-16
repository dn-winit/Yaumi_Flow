#!/bin/bash
# Start all backend services + frontend for local development
# Usage: bash scripts/start-all.sh

set -e

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

# Load unified env
set -a
source .env 2>/dev/null || true
set +a

echo "Starting Yaumi Flow services..."

# Start backends in background
python -m data_import &
echo "  data_import       -> http://localhost:${DI_PORT:-8005}"

python -m demand_forecasting_pipeline &
echo "  forecast           -> http://localhost:${DF_PORT:-8002}"

python -m recommended_order &
echo "  recommended_order  -> http://localhost:${RO_PORT:-8001}"

python -m sales_supervision &
echo "  sales_supervision  -> http://localhost:${SS_PORT:-8004}"

python -m llm_analytics &
echo "  llm_analytics      -> http://localhost:${LLM_PORT:-8003}"

# Start frontend
cd webapp
npm run dev &
echo "  webapp             -> http://localhost:${WEBAPP_PORT:-3000}"

echo ""
echo "All services started. Press Ctrl+C to stop all."
wait
