-- Migration 0001: add reconciliation columns to yf_demand_forecast.
--
-- The forecasting service serves a "recommended_load" via /predictions/forecast
-- and /reconciliation/recommend that V5_b-corrects the raw model prediction
-- using opening stock and per-pair bias. Until this migration the DB stored
-- only the raw model output, so any consumer that read the table directly
-- (analytics, ad-hoc reporting) saw a different number than the API. This
-- migration closes that gap by persisting the reconciled values alongside
-- the raw prediction.
--
-- Columns added (all NOT NULL with sane defaults so legacy rows stay valid):
--   recommended_load     -- engine output: load to put on the van
--   forecast_corrected   -- prediction / (1 + bias_pct), pre-clamp
--   bias_pct             -- effective bias (post L1b shrinkage) used by engine
--   opening_stock        -- previous day's closing qty fed to the engine
--
-- Idempotent: each ALTER TABLE checks the column doesn't already exist so
-- re-running the migration is safe.
--
-- Apply with sqlcmd:
--   sqlcmd -S <host> -d YaumiAIML -i 0001_add_reconciliation_cols.sql
--
-- Roll back manually if needed:
--   ALTER TABLE [YaumiAIML].[dbo].[yf_demand_forecast] DROP COLUMN recommended_load;
--   ALTER TABLE [YaumiAIML].[dbo].[yf_demand_forecast] DROP COLUMN forecast_corrected;
--   ALTER TABLE [YaumiAIML].[dbo].[yf_demand_forecast] DROP COLUMN bias_pct;
--   ALTER TABLE [YaumiAIML].[dbo].[yf_demand_forecast] DROP COLUMN opening_stock;

USE [YaumiAIML];
GO

IF NOT EXISTS (
    SELECT 1 FROM INFORMATION_SCHEMA.COLUMNS
    WHERE TABLE_SCHEMA='dbo' AND TABLE_NAME='yf_demand_forecast'
      AND COLUMN_NAME='recommended_load'
)
BEGIN
    ALTER TABLE [dbo].[yf_demand_forecast]
        ADD [recommended_load] FLOAT NOT NULL CONSTRAINT DF_yf_recommended_load DEFAULT 0;
END
GO

IF NOT EXISTS (
    SELECT 1 FROM INFORMATION_SCHEMA.COLUMNS
    WHERE TABLE_SCHEMA='dbo' AND TABLE_NAME='yf_demand_forecast'
      AND COLUMN_NAME='forecast_corrected'
)
BEGIN
    ALTER TABLE [dbo].[yf_demand_forecast]
        ADD [forecast_corrected] FLOAT NOT NULL CONSTRAINT DF_yf_forecast_corrected DEFAULT 0;
END
GO

IF NOT EXISTS (
    SELECT 1 FROM INFORMATION_SCHEMA.COLUMNS
    WHERE TABLE_SCHEMA='dbo' AND TABLE_NAME='yf_demand_forecast'
      AND COLUMN_NAME='bias_pct'
)
BEGIN
    ALTER TABLE [dbo].[yf_demand_forecast]
        ADD [bias_pct] FLOAT NOT NULL CONSTRAINT DF_yf_bias_pct DEFAULT 0;
END
GO

IF NOT EXISTS (
    SELECT 1 FROM INFORMATION_SCHEMA.COLUMNS
    WHERE TABLE_SCHEMA='dbo' AND TABLE_NAME='yf_demand_forecast'
      AND COLUMN_NAME='opening_stock'
)
BEGIN
    ALTER TABLE [dbo].[yf_demand_forecast]
        ADD [opening_stock] FLOAT NOT NULL CONSTRAINT DF_yf_opening_stock DEFAULT 0;
END
GO

-- Helpful (non-required) index for the (data_split, trx_date) read pattern
-- used by the API's /predictions/forecast endpoint and the data_import
-- mirror job. INCLUDE keeps the bookmark lookups off the hot path.
IF NOT EXISTS (
    SELECT 1 FROM sys.indexes
    WHERE name = 'IX_yf_demand_forecast_split_date'
      AND object_id = OBJECT_ID('[dbo].[yf_demand_forecast]')
)
BEGIN
    CREATE NONCLUSTERED INDEX IX_yf_demand_forecast_split_date
        ON [dbo].[yf_demand_forecast] ([data_split], [trx_date])
        INCLUDE ([route_code], [item_code], [predicted], [recommended_load]);
END
GO

PRINT 'Migration 0001 applied: reconciliation columns are present.';
