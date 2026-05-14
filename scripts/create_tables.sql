-- ============================================================
-- Yaumi Flow -- Schema for the [YaumiAIML] database.
--
-- Push direction (single-source-of-truth):
--   * [YaumiAIML]  -- write target. THIS file owns the schema here.
--                    Tables follow the [yf_*] prefix so any object the
--                    Yaumi Flow stack creates is greppable in one shot.
--   * [YaumiLive]  -- read-only source (customer_data, journey_plan,
--                    transaction views). Never written from this stack.
--
-- Conventions
--   * 3-part qualifier dropped: every CREATE / ALTER targets [dbo].[*]
--     under the active database (USE statement below).
--   * Percent / rate columns are typed DECIMAL(8,2) uniformly so the
--     same defensive clamp helper covers every numeric write.
--   * Derivable values are PERSISTED computed columns -- a single
--     source-of-truth expression, no drift between stored and live.
--   * Index names: ix_<table_short>_<col[s]>
--   * Unique constraint names: uq_<table_short>
--   * Foreign key names: fk_<table_short>_<referenced_short>
--
-- This file is idempotent: rerunning it is a no-op on a current schema.
-- The migration block at the bottom carries existing deployments
-- forward to the latest shape.
-- ============================================================

USE [YaumiAIML];
GO

-- ============================================================
-- 1. DEMAND FORECAST  -- demand_forecasting_pipeline
-- ============================================================
IF NOT EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.TABLES
               WHERE TABLE_SCHEMA = 'dbo' AND TABLE_NAME = 'yf_demand_forecast')
CREATE TABLE [dbo].[yf_demand_forecast] (
    id              BIGINT IDENTITY(1,1) PRIMARY KEY,
    trx_date        DATE NOT NULL,
    route_code      NVARCHAR(50) NOT NULL,
    item_code       NVARCHAR(50) NOT NULL,
    item_name       NVARCHAR(255),
    data_split      NVARCHAR(20) NOT NULL,                  -- Test | Forecast
    demand_class    NVARCHAR(50),                           -- smooth | intermittent | erratic | lumpy
    model_used      NVARCHAR(100),
    predicted       FLOAT DEFAULT 0,
    p_demand        FLOAT DEFAULT 0,                        -- probability of demand (0-1)
    qty_if_demand   FLOAT DEFAULT 0,                        -- quantity if demand occurs
    actual_qty      FLOAT DEFAULT 0,                        -- actual (test split only)
    lower_bound     FLOAT DEFAULT 0,                        -- q_10
    upper_bound     FLOAT DEFAULT 0,                        -- q_90
    adi             FLOAT,
    cv2             FLOAT,
    nonzero_ratio   FLOAT,
    mean_qty        FLOAT,
    avg_gap_days    FLOAT,
    created_at      DATETIME DEFAULT GETDATE(),

    INDEX ix_yf_df_date         (trx_date),
    INDEX ix_yf_df_route        (route_code),
    INDEX ix_yf_df_item         (item_code),
    INDEX ix_yf_df_split        (data_split),
    INDEX ix_yf_df_route_date   (route_code, trx_date)
);
GO

-- ==============================================================
-- yf_sales_transactions: carry chain + actual_sold (past + today)
-- Owned by reconciliation_refresh cron at 03:30 Asia/Dubai.
-- One row per (trx_date, route_code, item_code) -- split-agnostic.
-- ==============================================================
IF NOT EXISTS (SELECT 1 FROM sys.tables WHERE name='yf_sales_transactions' AND schema_id = SCHEMA_ID('dbo'))
CREATE TABLE [dbo].[yf_sales_transactions] (
    id                          BIGINT IDENTITY(1,1) NOT NULL PRIMARY KEY,
    trx_date                    DATE             NOT NULL,
    route_code                  NVARCHAR(50)     NOT NULL,
    item_code                   NVARCHAR(50)     NOT NULL,

    -- Our carry chain (policy-driven, carry-aware fresh clamp)
    opening_stock               FLOAT            NULL,
    fresh_load                  FLOAT            NULL,
    total_van_load              FLOAT            NULL,
    leftover_to_next_day        FLOAT            NULL,

    -- Rep (Yaumi) carry chain (observed: depot allocation + physical closing stock).
    -- Tracked SEPARATELY from the policy chain above -- the depot's allocation is
    -- NOT carry-aware, so yaumi_total_van_load can over-load on items the rep
    -- still has stock of -- diagnostic data only, no derived metric yet.
    yaumi_opening_stock         FLOAT            NULL,
    yaumi_fresh_load            FLOAT            NULL,
    yaumi_total_van_load        FLOAT            NULL,
    yaumi_leftover              FLOAT            NULL,

    -- Reality
    actual_sold                 FLOAT            NULL,

    -- Engine math
    bias_pct                    FLOAT            NULL,
    forecast_corrected          FLOAT            NULL,
    expected_demand             FLOAT            NULL,
    van_load_lower_bound        FLOAT            NULL,
    van_load_upper_bound        FLOAT            NULL,

    -- Envelope diagnostics
    recent_daily_avg            FLOAT            NULL,
    pattern_floor_applied       BIT              NULL,
    pattern_ceiling_applied     BIT              NULL,
    forecast_below_recent       BIT              NULL,
    -- Dormancy guard: True when the (route, item) pair had zero sales
    -- across its route's last N trip days and the engine zeroed
    -- expected_demand for it. NULL on pre-backfill rows.
    forecast_dormant            BIT              NULL,

    -- Audit
    created_at                  DATETIME         NOT NULL DEFAULT GETDATE(),
    updated_at                  DATETIME         NULL,

    CONSTRAINT UX_yf_sales_transactions_natural UNIQUE (trx_date, route_code, item_code),
    INDEX ix_yf_st_route_date (route_code, trx_date),
    INDEX ix_yf_st_date (trx_date)
);
GO

-- ============================================================
-- 2. RECOMMENDED ORDERS  -- recommended_order
-- ============================================================
IF NOT EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.TABLES
               WHERE TABLE_SCHEMA = 'dbo' AND TABLE_NAME = 'yf_recommended_orders')
CREATE TABLE [dbo].[yf_recommended_orders] (
    id                          BIGINT IDENTITY(1,1) PRIMARY KEY,
    trx_date                    DATE NOT NULL,
    route_code                  NVARCHAR(50) NOT NULL,
    customer_code               NVARCHAR(50) NOT NULL,
    customer_name               NVARCHAR(255),
    item_code                   NVARCHAR(50) NOT NULL,
    item_name                   NVARCHAR(255),
    recommended_quantity        INT DEFAULT 0,
    tier                        NVARCHAR(50),               -- MUST_STOCK | SHOULD_STOCK | CONSIDER | MONITOR
    van_load                    INT DEFAULT 0,
    priority_score              DECIMAL(8,2)  DEFAULT 0,
    avg_quantity_per_visit      INT DEFAULT 0,
    days_since_last_purchase    INT DEFAULT 0,
    purchase_cycle_days         DECIMAL(10,2) DEFAULT 0,
    frequency_percent           DECIMAL(8,2)  DEFAULT 0,
    churn_probability           DECIMAL(5,3)  DEFAULT 0,
    pattern_quality             DECIMAL(4,2)  DEFAULT 0,
    purchase_count              INT DEFAULT 0,
    trend_factor                DECIMAL(4,2)  DEFAULT 1.0,
    reason_status               NVARCHAR(100),
    reason_explanation          NVARCHAR(500),
    reason_confidence           INT DEFAULT 0,
    generated_at                DATETIME DEFAULT GETDATE(),
    generated_by                NVARCHAR(100) DEFAULT 'API',

    INDEX ix_yf_ro_date         (trx_date),
    INDEX ix_yf_ro_route        (route_code),
    INDEX ix_yf_ro_customer     (customer_code),
    INDEX ix_yf_ro_route_date   (route_code, trx_date),
    CONSTRAINT uq_yf_ro UNIQUE (trx_date, route_code, customer_code, item_code)
);
GO

-- ============================================================
-- 3. SUPERVISION -- ROUTES  (sales_supervision)
--
-- Naming convention on the qty columns:
--   planned_*  totals across the journey-plan customer set (visited + not)
--   visited_*  totals across the customers actually visited (subset)
-- The two prefixes prevent the silent semantic drift earlier "total_*"
-- naming caused, where the same word meant different things to writer
-- and reader.
-- ============================================================
IF NOT EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.TABLES
               WHERE TABLE_SCHEMA = 'dbo' AND TABLE_NAME = 'yf_supervision_routes')
CREATE TABLE [dbo].[yf_supervision_routes] (
    id                          INT IDENTITY(1,1) PRIMARY KEY,
    session_id                  NVARCHAR(100) NOT NULL UNIQUE,
    route_code                  NVARCHAR(50)  NOT NULL,
    supervision_date            DATE          NOT NULL,
    customers_planned           INT DEFAULT 0,
    customers_visited           INT DEFAULT 0,
    -- Derived: visited / planned * 100 (clamped to 0 when planned = 0).
    customer_completion_rate AS (
        CASE WHEN customers_planned > 0
             THEN ROUND(CAST(customers_visited AS DECIMAL(10,2)) * 100 / customers_planned, 1)
             ELSE CAST(0 AS DECIMAL(10,2))
        END
    ) PERSISTED,
    planned_qty_recommended     INT DEFAULT 0,
    visited_qty_recommended     INT DEFAULT 0,
    visited_qty_actual          INT DEFAULT 0,
    -- Derived: visited_actual / visited_recommended * 100.
    qty_fulfillment_rate AS (
        CASE WHEN visited_qty_recommended > 0
             THEN ROUND(CAST(visited_qty_actual AS DECIMAL(10,2)) * 100 / visited_qty_recommended, 1)
             ELSE CAST(0 AS DECIMAL(10,2))
        END
    ) PERSISTED,
    route_performance_score     DECIMAL(8,2) DEFAULT 0,    -- live SessionMetrics score
    session_status              NVARCHAR(20) DEFAULT 'active',
    session_started_at          DATETIME DEFAULT GETDATE(),
    session_completed_at        DATETIME,
    -- Route-level LLM review (JSON-serialised structured payload from
    -- /analytics/analyze/route). Populated by the dedicated save path,
    -- not the per-visit upsert; stays NULL until a route review fires.
    llm_route_analysis          NVARCHAR(MAX),

    INDEX ix_yf_sr_route_date   (route_code, supervision_date)
);
GO

-- ============================================================
-- 4. SUPERVISION -- CUSTOMERS  (sales_supervision)
-- ============================================================
IF NOT EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.TABLES
               WHERE TABLE_SCHEMA = 'dbo' AND TABLE_NAME = 'yf_supervision_customers')
CREATE TABLE [dbo].[yf_supervision_customers] (
    id                          INT IDENTITY(1,1) PRIMARY KEY,
    session_id                  NVARCHAR(100) NOT NULL,
    customer_code               NVARCHAR(50)  NOT NULL,
    customer_name               NVARCHAR(255),
    visit_sequence              INT DEFAULT 0,
    skus_recommended            INT DEFAULT 0,
    skus_sold                   INT DEFAULT 0,
    sku_coverage_rate           DECIMAL(8,2) DEFAULT 0,    -- live ScoreResult.coverage
    qty_recommended             INT DEFAULT 0,
    qty_actual                  INT DEFAULT 0,
    -- Derived: qty_actual / qty_recommended * 100.
    qty_fulfillment_rate AS (
        CASE WHEN qty_recommended > 0
             THEN ROUND(CAST(qty_actual AS DECIMAL(10,2)) * 100 / qty_recommended, 1)
             ELSE CAST(0 AS DECIMAL(10,2))
        END
    ) PERSISTED,
    customer_accuracy_avg       DECIMAL(8,2) DEFAULT 0,    -- mean of per-item accuracies
    customer_performance_score  DECIMAL(8,2) DEFAULT 0,    -- live ScoreResult.score
    -- Pre-visit briefing produced by /analytics/analyze/pre-visit. Stored
    -- as the structured JSON payload (briefing / key_items / heads_up).
    -- Populated by the dedicated save path; stays NULL when the
    -- supervisor never opens a briefing for this customer.
    llm_pre_visit_briefing      NVARCHAR(MAX),
    -- Post-visit customer review (JSON payload from
    -- /analytics/analyze/customer). Same population rule as briefing.
    llm_performance_analysis    NVARCHAR(MAX),
    record_saved_at             DATETIME DEFAULT GETDATE(),

    INDEX ix_yf_sc_session      (session_id),
    CONSTRAINT uq_yf_sc UNIQUE (session_id, customer_code),
    CONSTRAINT fk_yf_sc_session FOREIGN KEY (session_id)
        REFERENCES [dbo].[yf_supervision_routes](session_id) ON DELETE CASCADE
);
GO

-- ============================================================
-- 5. SUPERVISION -- ITEMS  (sales_supervision)
-- ============================================================
IF NOT EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.TABLES
               WHERE TABLE_SCHEMA = 'dbo' AND TABLE_NAME = 'yf_supervision_items')
CREATE TABLE [dbo].[yf_supervision_items] (
    id                          INT IDENTITY(1,1) PRIMARY KEY,
    session_id                  NVARCHAR(100) NOT NULL,
    customer_code               NVARCHAR(50)  NOT NULL,
    item_code                   NVARCHAR(50)  NOT NULL,
    item_name                   NVARCHAR(255),
    original_recommended_qty    INT DEFAULT 0,
    adjusted_recommended_qty    INT DEFAULT 0,
    -- Derived: adjusted - original. Single source of truth for the delta.
    recommendation_adjustment AS (adjusted_recommended_qty - original_recommended_qty) PERSISTED,
    actual_qty                  INT DEFAULT 0,
    was_manually_edited         BIT DEFAULT 0,
    was_item_sold               BIT DEFAULT 0,
    recommendation_tier         NVARCHAR(50),
    priority_score              DECIMAL(10,2) DEFAULT 0,
    van_inventory_qty           INT DEFAULT 0,
    days_since_last_purchase    INT DEFAULT 0,
    purchase_cycle_days         DECIMAL(10,2) DEFAULT 0,
    purchase_frequency_pct      DECIMAL(8,2)  DEFAULT 0,
    record_saved_at             DATETIME DEFAULT GETDATE(),

    INDEX ix_yf_si_session      (session_id),
    INDEX ix_yf_si_customer     (session_id, customer_code),
    CONSTRAINT uq_yf_si UNIQUE (session_id, customer_code, item_code),
    CONSTRAINT fk_yf_si_session FOREIGN KEY (session_id)
        REFERENCES [dbo].[yf_supervision_routes](session_id) ON DELETE CASCADE
);
GO

-- ============================================================
-- Idempotent migrations for existing deployments.
-- Each block checks the live shape before mutating, so this script
-- is safe to re-run. New deployments hit the CREATE TABLE branches
-- above and skip these entirely.
-- ============================================================

-- yf_supervision_routes ---------------------------------------------------

-- Legacy total_qty_* -> visited_qty_*.
IF EXISTS (SELECT 1 FROM sys.columns
           WHERE object_id = OBJECT_ID('[dbo].[yf_supervision_routes]')
             AND name = 'total_qty_recommended')
BEGIN
    EXEC sp_rename '[dbo].[yf_supervision_routes].total_qty_recommended',
                   'visited_qty_recommended', 'COLUMN';
END
GO

IF EXISTS (SELECT 1 FROM sys.columns
           WHERE object_id = OBJECT_ID('[dbo].[yf_supervision_routes]')
             AND name = 'total_qty_actual')
BEGIN
    EXEC sp_rename '[dbo].[yf_supervision_routes].total_qty_actual',
                   'visited_qty_actual', 'COLUMN';
END
GO

-- Legacy total_customers_* -> customers_*.
IF EXISTS (SELECT 1 FROM sys.columns
           WHERE object_id = OBJECT_ID('[dbo].[yf_supervision_routes]')
             AND name = 'total_customers_planned')
BEGIN
    EXEC sp_rename '[dbo].[yf_supervision_routes].total_customers_planned',
                   'customers_planned', 'COLUMN';
END
GO

IF EXISTS (SELECT 1 FROM sys.columns
           WHERE object_id = OBJECT_ID('[dbo].[yf_supervision_routes]')
             AND name = 'total_customers_visited')
BEGIN
    EXEC sp_rename '[dbo].[yf_supervision_routes].total_customers_visited',
                   'customers_visited', 'COLUMN';
END
GO

-- planned_qty_recommended introduced in the same migration as the rename.
IF NOT EXISTS (SELECT 1 FROM sys.columns
               WHERE object_id = OBJECT_ID('[dbo].[yf_supervision_routes]')
                 AND name = 'planned_qty_recommended')
BEGIN
    ALTER TABLE [dbo].[yf_supervision_routes] ADD planned_qty_recommended INT DEFAULT 0;
END
GO

-- Widen route_performance_score -> DECIMAL(8,2).
IF EXISTS (SELECT 1 FROM sys.columns
           WHERE object_id = OBJECT_ID('[dbo].[yf_supervision_routes]')
             AND name = 'route_performance_score'
             AND (precision <> 8 OR scale <> 2))
BEGIN
    ALTER TABLE [dbo].[yf_supervision_routes]
        ALTER COLUMN route_performance_score DECIMAL(8,2);
END
GO

-- Convert customer_completion_rate from stored to PERSISTED computed.
IF EXISTS (SELECT 1 FROM sys.columns
           WHERE object_id = OBJECT_ID('[dbo].[yf_supervision_routes]')
             AND name = 'customer_completion_rate'
             AND is_computed = 0)
BEGIN
    ALTER TABLE [dbo].[yf_supervision_routes] DROP COLUMN customer_completion_rate;
    ALTER TABLE [dbo].[yf_supervision_routes] ADD customer_completion_rate AS (
        CASE WHEN customers_planned > 0
             THEN ROUND(CAST(customers_visited AS DECIMAL(10,2)) * 100 / customers_planned, 1)
             ELSE CAST(0 AS DECIMAL(10,2))
        END
    ) PERSISTED;
END
GO

-- Convert qty_fulfillment_rate (routes) from stored to PERSISTED computed.
IF EXISTS (SELECT 1 FROM sys.columns
           WHERE object_id = OBJECT_ID('[dbo].[yf_supervision_routes]')
             AND name = 'qty_fulfillment_rate'
             AND is_computed = 0)
BEGIN
    ALTER TABLE [dbo].[yf_supervision_routes] DROP COLUMN qty_fulfillment_rate;
    ALTER TABLE [dbo].[yf_supervision_routes] ADD qty_fulfillment_rate AS (
        CASE WHEN visited_qty_recommended > 0
             THEN ROUND(CAST(visited_qty_actual AS DECIMAL(10,2)) * 100 / visited_qty_recommended, 1)
             ELSE CAST(0 AS DECIMAL(10,2))
        END
    ) PERSISTED;
END
GO

-- yf_supervision_customers ------------------------------------------------

-- customer_name landed after the original schema; back-fill the column.
IF NOT EXISTS (SELECT 1 FROM sys.columns
               WHERE object_id = OBJECT_ID('[dbo].[yf_supervision_customers]')
                 AND name = 'customer_name')
BEGIN
    ALTER TABLE [dbo].[yf_supervision_customers] ADD customer_name NVARCHAR(255);
END
GO

-- customer_accuracy_avg landed after the original schema.
IF NOT EXISTS (SELECT 1 FROM sys.columns
               WHERE object_id = OBJECT_ID('[dbo].[yf_supervision_customers]')
                 AND name = 'customer_accuracy_avg')
BEGIN
    ALTER TABLE [dbo].[yf_supervision_customers] ADD customer_accuracy_avg DECIMAL(8,2) DEFAULT 0;
END
GO

-- Widen visit_sequence (was SMALLINT in early deploys).
IF EXISTS (SELECT 1 FROM sys.columns
           WHERE object_id = OBJECT_ID('[dbo].[yf_supervision_customers]')
             AND name = 'visit_sequence'
             AND system_type_id = TYPE_ID('smallint'))
BEGIN
    ALTER TABLE [dbo].[yf_supervision_customers] ALTER COLUMN visit_sequence INT;
END
GO

-- Widen percent / score columns to DECIMAL(8,2).
IF EXISTS (SELECT 1 FROM sys.columns
           WHERE object_id = OBJECT_ID('[dbo].[yf_supervision_customers]')
             AND name = 'customer_performance_score'
             AND (precision <> 8 OR scale <> 2))
BEGIN
    ALTER TABLE [dbo].[yf_supervision_customers]
        ALTER COLUMN customer_performance_score DECIMAL(8,2);
END
GO

IF EXISTS (SELECT 1 FROM sys.columns
           WHERE object_id = OBJECT_ID('[dbo].[yf_supervision_customers]')
             AND name = 'customer_accuracy_avg'
             AND (precision <> 8 OR scale <> 2))
BEGIN
    ALTER TABLE [dbo].[yf_supervision_customers]
        ALTER COLUMN customer_accuracy_avg DECIMAL(8,2);
END
GO

IF EXISTS (SELECT 1 FROM sys.columns
           WHERE object_id = OBJECT_ID('[dbo].[yf_supervision_customers]')
             AND name = 'sku_coverage_rate'
             AND (precision <> 8 OR scale <> 2))
BEGIN
    ALTER TABLE [dbo].[yf_supervision_customers]
        ALTER COLUMN sku_coverage_rate DECIMAL(8,2);
END
GO

-- Drop the redundant total_ prefix on customer-level qty / sku columns.
IF EXISTS (SELECT 1 FROM sys.columns
           WHERE object_id = OBJECT_ID('[dbo].[yf_supervision_customers]')
             AND name = 'total_skus_recommended')
BEGIN
    EXEC sp_rename '[dbo].[yf_supervision_customers].total_skus_recommended',
                   'skus_recommended', 'COLUMN';
END
GO

IF EXISTS (SELECT 1 FROM sys.columns
           WHERE object_id = OBJECT_ID('[dbo].[yf_supervision_customers]')
             AND name = 'total_skus_sold')
BEGIN
    EXEC sp_rename '[dbo].[yf_supervision_customers].total_skus_sold',
                   'skus_sold', 'COLUMN';
END
GO

IF EXISTS (SELECT 1 FROM sys.columns
           WHERE object_id = OBJECT_ID('[dbo].[yf_supervision_customers]')
             AND name = 'total_qty_recommended')
BEGIN
    EXEC sp_rename '[dbo].[yf_supervision_customers].total_qty_recommended',
                   'qty_recommended', 'COLUMN';
END
GO

IF EXISTS (SELECT 1 FROM sys.columns
           WHERE object_id = OBJECT_ID('[dbo].[yf_supervision_customers]')
             AND name = 'total_qty_actual')
BEGIN
    EXEC sp_rename '[dbo].[yf_supervision_customers].total_qty_actual',
                   'qty_actual', 'COLUMN';
END
GO

-- Convert qty_fulfillment_rate (customers) from stored to PERSISTED computed.
IF EXISTS (SELECT 1 FROM sys.columns
           WHERE object_id = OBJECT_ID('[dbo].[yf_supervision_customers]')
             AND name = 'qty_fulfillment_rate'
             AND is_computed = 0)
BEGIN
    ALTER TABLE [dbo].[yf_supervision_customers] DROP COLUMN qty_fulfillment_rate;
    ALTER TABLE [dbo].[yf_supervision_customers] ADD qty_fulfillment_rate AS (
        CASE WHEN qty_recommended > 0
             THEN ROUND(CAST(qty_actual AS DECIMAL(10,2)) * 100 / qty_recommended, 1)
             ELSE CAST(0 AS DECIMAL(10,2))
        END
    ) PERSISTED;
END
GO

-- yf_supervision_items ----------------------------------------------------

-- Widen purchase_frequency_pct -> DECIMAL(8,2).
IF EXISTS (SELECT 1 FROM sys.columns
           WHERE object_id = OBJECT_ID('[dbo].[yf_supervision_items]')
             AND name = 'purchase_frequency_pct'
             AND (precision <> 8 OR scale <> 2))
BEGIN
    ALTER TABLE [dbo].[yf_supervision_items]
        ALTER COLUMN purchase_frequency_pct DECIMAL(8,2);
END
GO

-- Convert recommendation_adjustment from stored to PERSISTED computed.
IF EXISTS (SELECT 1 FROM sys.columns
           WHERE object_id = OBJECT_ID('[dbo].[yf_supervision_items]')
             AND name = 'recommendation_adjustment'
             AND is_computed = 0)
BEGIN
    ALTER TABLE [dbo].[yf_supervision_items] DROP COLUMN recommendation_adjustment;
    ALTER TABLE [dbo].[yf_supervision_items]
        ADD recommendation_adjustment AS (adjusted_recommended_qty - original_recommended_qty) PERSISTED;
END
GO

-- LLM payload columns (added after the original schema). Each holds the
-- JSON-serialised structured response from the corresponding analytics
-- endpoint so re-opening a session restores the same UI without re-
-- calling the LLM.
IF NOT EXISTS (SELECT 1 FROM sys.columns
               WHERE object_id = OBJECT_ID('[dbo].[yf_supervision_customers]')
                 AND name = 'llm_pre_visit_briefing')
BEGIN
    ALTER TABLE [dbo].[yf_supervision_customers] ADD llm_pre_visit_briefing NVARCHAR(MAX);
END
GO

IF NOT EXISTS (SELECT 1 FROM sys.columns
               WHERE object_id = OBJECT_ID('[dbo].[yf_supervision_routes]')
                 AND name = 'llm_route_analysis')
BEGIN
    ALTER TABLE [dbo].[yf_supervision_routes] ADD llm_route_analysis NVARCHAR(MAX);
END
GO

-- Reconciliation columns are NO LONGER on yf_demand_forecast.
-- They moved to yf_sales_transactions (see CREATE TABLE above) and are
-- written by reconciliation_refresh at 03:30 Asia/Dubai. Migrations
-- 0001-0002 are kept in the migrations folder for audit; this fresh-
-- deploy script no longer re-applies them.

-- yf_sales_transactions: idempotent add of the rep (Yaumi) carry chain
-- columns. Tracked alongside the policy chain so rep-side analytics
-- read from a single row.
IF NOT EXISTS (SELECT 1 FROM sys.columns
               WHERE object_id = OBJECT_ID('[dbo].[yf_sales_transactions]')
                 AND name = 'yaumi_opening_stock')
BEGIN
    ALTER TABLE [dbo].[yf_sales_transactions] ADD
        yaumi_opening_stock  FLOAT NULL,
        yaumi_fresh_load     FLOAT NULL,
        yaumi_total_van_load FLOAT NULL,
        yaumi_leftover       FLOAT NULL;
END
GO

-- yf_sales_transactions: idempotent add of the dormancy-guard diagnostic.
-- ``forecast_dormant`` is set True by the daily reconciliation cron when
-- a (route, item) pair has had zero sales across its route's trailing N
-- trip days; the engine zeroes expected_demand for it so no fresh load
-- is recommended (the carry chain still flows through). N is settings-
-- driven (``DF_DORMANCY_ZERO_SALE_THRESHOLD_TRIP_DAYS``). NULL on rows
-- written before the column existed; the API serializer treats NULL as
-- "not yet evaluated" so legacy rows remain consumable.
IF NOT EXISTS (SELECT 1 FROM sys.columns
               WHERE object_id = OBJECT_ID('[dbo].[yf_sales_transactions]')
                 AND name = 'forecast_dormant')
BEGIN
    ALTER TABLE [dbo].[yf_sales_transactions] ADD
        forecast_dormant BIT NULL;
END
GO
