#!/bin/bash
# Start all backend services + frontend for local development
# Usage: bash scripts/start-all.sh

set -e

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

# Each service loads .env itself via pydantic-settings (`_env_file`). We
# intentionally do NOT `source` the file here: bash exports every value
# as a literal string, which Pydantic v2 then re-parses as JSON. List
# fields like ``DF_LIVE_ROUTE_CODES=[9105,...]`` parse to list[int] from
# JSON but the field is typed list[str], so the service refuses to boot
# with a validation error. Pydantic's own dotenv parser handles the same
# .env without that strictness clash, which is why running each service
# one-by-one (without bash pre-loading the env) works while a bulk start
# previously did not.

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
