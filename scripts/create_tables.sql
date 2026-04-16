-- ============================================================
-- Yaumi Flow -- YaumiAIML Table Definitions
-- Run once to create tables. All data pushed FROM our modules.
-- YaumiLive is READ-ONLY (customer_data, journey_plan source).
-- ============================================================

-- 1. DEMAND FORECAST (from demand_forecasting_pipeline)
IF NOT EXISTS (SELECT * FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_NAME = 'yf_demand_forecast')
CREATE TABLE [YaumiAIML].[dbo].[yf_demand_forecast] (
    id              BIGINT IDENTITY(1,1) PRIMARY KEY,
    trx_date        DATE NOT NULL,
    route_code      NVARCHAR(50) NOT NULL,
    item_code       NVARCHAR(50) NOT NULL,
    item_name       NVARCHAR(255),
    data_split      NVARCHAR(20) NOT NULL,        -- Test | Forecast
    demand_class    NVARCHAR(50),                  -- smooth | intermittent | erratic | lumpy
    model_used      NVARCHAR(100),
    predicted       FLOAT DEFAULT 0,
    p_demand        FLOAT DEFAULT 0,               -- probability of demand (0-1)
    qty_if_demand   FLOAT DEFAULT 0,               -- quantity if demand occurs
    actual_qty      FLOAT DEFAULT 0,               -- actual (test split only)
    lower_bound     FLOAT DEFAULT 0,               -- q_10
    upper_bound     FLOAT DEFAULT 0,               -- q_90
    adi             FLOAT,
    cv2             FLOAT,
    nonzero_ratio   FLOAT,
    mean_qty        FLOAT,
    avg_gap_days    FLOAT,
    created_at      DATETIME DEFAULT GETDATE(),

    INDEX ix_yf_df_date (trx_date),
    INDEX ix_yf_df_route (route_code),
    INDEX ix_yf_df_item (item_code),
    INDEX ix_yf_df_split (data_split),
    INDEX ix_yf_df_route_date (route_code, trx_date)
);
GO

-- 2. RECOMMENDED ORDERS (from recommended_order)
IF NOT EXISTS (SELECT * FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_NAME = 'yf_recommended_orders')
CREATE TABLE [YaumiAIML].[dbo].[yf_recommended_orders] (
    id                      BIGINT IDENTITY(1,1) PRIMARY KEY,
    trx_date                DATE NOT NULL,
    route_code              NVARCHAR(50) NOT NULL,
    customer_code           NVARCHAR(50) NOT NULL,
    customer_name           NVARCHAR(255),
    item_code               NVARCHAR(50) NOT NULL,
    item_name               NVARCHAR(255),
    recommended_quantity    INT DEFAULT 0,
    tier                    NVARCHAR(50),              -- MUST_STOCK | SHOULD_STOCK | CONSIDER | MONITOR
    van_load                INT DEFAULT 0,
    priority_score          DECIMAL(6,2) DEFAULT 0,
    avg_quantity_per_visit  INT DEFAULT 0,
    days_since_last_purchase INT DEFAULT 0,
    purchase_cycle_days     DECIMAL(10,2) DEFAULT 0,
    frequency_percent       DECIMAL(6,2) DEFAULT 0,
    churn_probability       DECIMAL(5,3) DEFAULT 0,
    pattern_quality         DECIMAL(4,2) DEFAULT 0,
    purchase_count          INT DEFAULT 0,
    trend_factor            DECIMAL(4,2) DEFAULT 1.0,
    reason_status           NVARCHAR(100),
    reason_explanation      NVARCHAR(500),
    reason_confidence       INT DEFAULT 0,
    generated_at            DATETIME DEFAULT GETDATE(),
    generated_by            NVARCHAR(100) DEFAULT 'API',

    INDEX ix_yf_ro_date (trx_date),
    INDEX ix_yf_ro_route (route_code),
    INDEX ix_yf_ro_customer (customer_code),
    INDEX ix_yf_ro_route_date (route_code, trx_date),
    CONSTRAINT uq_yf_ro UNIQUE (trx_date, route_code, customer_code, item_code)
);
GO

-- 3. SUPERVISION - ROUTES (from sales_supervision)
IF NOT EXISTS (SELECT * FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_NAME = 'yf_supervision_routes')
CREATE TABLE [YaumiAIML].[dbo].[yf_supervision_routes] (
    id                      INT IDENTITY(1,1) PRIMARY KEY,
    session_id              NVARCHAR(100) NOT NULL UNIQUE,
    route_code              NVARCHAR(50) NOT NULL,
    supervision_date        DATE NOT NULL,
    total_customers_planned INT DEFAULT 0,
    total_customers_visited INT DEFAULT 0,
    customer_completion_rate DECIMAL(6,2) DEFAULT 0,
    total_qty_recommended   INT DEFAULT 0,
    total_qty_actual        INT DEFAULT 0,
    qty_fulfillment_rate    DECIMAL(6,2) DEFAULT 0,
    route_performance_score DECIMAL(6,2) DEFAULT 0,
    session_status          NVARCHAR(20) DEFAULT 'active',
    session_started_at      DATETIME DEFAULT GETDATE(),
    session_completed_at    DATETIME,

    INDEX ix_yf_sr_route_date (route_code, supervision_date)
);
GO

-- 4. SUPERVISION - CUSTOMERS (from sales_supervision)
IF NOT EXISTS (SELECT * FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_NAME = 'yf_supervision_customers')
CREATE TABLE [YaumiAIML].[dbo].[yf_supervision_customers] (
    id                          INT IDENTITY(1,1) PRIMARY KEY,
    session_id                  NVARCHAR(100) NOT NULL,
    customer_code               NVARCHAR(50) NOT NULL,
    visit_sequence              SMALLINT DEFAULT 0,
    total_skus_recommended      INT DEFAULT 0,
    total_skus_sold             INT DEFAULT 0,
    sku_coverage_rate           DECIMAL(6,2) DEFAULT 0,
    total_qty_recommended       INT DEFAULT 0,
    total_qty_actual            INT DEFAULT 0,
    qty_fulfillment_rate        DECIMAL(6,2) DEFAULT 0,
    customer_performance_score  DECIMAL(6,2) DEFAULT 0,
    llm_performance_analysis    NVARCHAR(MAX),
    record_saved_at             DATETIME DEFAULT GETDATE(),

    INDEX ix_yf_sc_session (session_id),
    CONSTRAINT uq_yf_sc UNIQUE (session_id, customer_code),
    CONSTRAINT fk_yf_sc_session FOREIGN KEY (session_id)
        REFERENCES [YaumiAIML].[dbo].[yf_supervision_routes](session_id) ON DELETE CASCADE
);
GO

-- 5. SUPERVISION - ITEMS (from sales_supervision)
IF NOT EXISTS (SELECT * FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_NAME = 'yf_supervision_items')
CREATE TABLE [YaumiAIML].[dbo].[yf_supervision_items] (
    id                          INT IDENTITY(1,1) PRIMARY KEY,
    session_id                  NVARCHAR(100) NOT NULL,
    customer_code               NVARCHAR(50) NOT NULL,
    item_code                   NVARCHAR(50) NOT NULL,
    item_name                   NVARCHAR(255),
    original_recommended_qty    INT DEFAULT 0,
    adjusted_recommended_qty    INT DEFAULT 0,
    recommendation_adjustment   INT DEFAULT 0,
    actual_qty                  INT DEFAULT 0,
    was_manually_edited         BIT DEFAULT 0,
    was_item_sold               BIT DEFAULT 0,
    recommendation_tier         NVARCHAR(50),
    priority_score              DECIMAL(10,2) DEFAULT 0,
    van_inventory_qty           INT DEFAULT 0,
    days_since_last_purchase    INT DEFAULT 0,
    purchase_cycle_days         DECIMAL(10,2) DEFAULT 0,
    purchase_frequency_pct      DECIMAL(6,2) DEFAULT 0,
    record_saved_at             DATETIME DEFAULT GETDATE(),

    INDEX ix_yf_si_session (session_id),
    INDEX ix_yf_si_customer (session_id, customer_code),
    CONSTRAINT uq_yf_si UNIQUE (session_id, customer_code, item_code),
    CONSTRAINT fk_yf_si_session FOREIGN KEY (session_id)
        REFERENCES [YaumiAIML].[dbo].[yf_supervision_routes](session_id) ON DELETE CASCADE
);
GO
