@echo off
REM Start all backend services + frontend for Windows local development
REM Usage: scripts\start-all.bat

cd /d "%~dp0\.."

echo Starting Yaumi Flow services...

start "data_import" cmd /c "python -m data_import"
echo   data_import       -^> http://localhost:8005

start "forecast" cmd /c "python -m demand_forecasting_pipeline"
echo   forecast           -^> http://localhost:8002

start "recommended_order" cmd /c "python -m recommended_order"
echo   recommended_order  -^> http://localhost:8001

start "sales_supervision" cmd /c "python -m sales_supervision"
echo   sales_supervision  -^> http://localhost:8004

start "llm_analytics" cmd /c "python -m llm_analytics"
echo   llm_analytics      -^> http://localhost:8003

cd webapp
start "webapp" cmd /c "npm run dev"
echo   webapp             -^> http://localhost:3000

echo.
echo All services started in separate windows.
echo Close each window to stop individual services.
