#!/bin/bash
# Stop all running services
# Usage: bash scripts/stop-all.sh

echo "Stopping all Yaumi Flow services..."

# Kill Python services
pkill -f "python -m data_import" 2>/dev/null || true
pkill -f "python -m demand_forecasting_pipeline" 2>/dev/null || true
pkill -f "python -m recommended_order" 2>/dev/null || true
pkill -f "python -m sales_supervision" 2>/dev/null || true
pkill -f "python -m llm_analytics" 2>/dev/null || true

# Kill vite dev server
pkill -f "vite" 2>/dev/null || true

echo "All services stopped."
