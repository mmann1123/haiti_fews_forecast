-- FEWS NET Haiti Price Database Schema
-- Database: DuckDB
-- Purpose: Store and track market price data from FEWS NET API

-- ============================================================
-- DIMENSION TABLES
-- ============================================================

-- Sequences for auto-incrementing IDs
CREATE SEQUENCE IF NOT EXISTS seq_markets_id START 1;
CREATE SEQUENCE IF NOT EXISTS seq_products_id START 1;
CREATE SEQUENCE IF NOT EXISTS seq_units_id START 1;
CREATE SEQUENCE IF NOT EXISTS seq_sources_id START 1;
CREATE SEQUENCE IF NOT EXISTS seq_prices_id START 1;
CREATE SEQUENCE IF NOT EXISTS seq_imports_id START 1;

-- Markets dimension table
CREATE TABLE IF NOT EXISTS markets (
    id INTEGER PRIMARY KEY DEFAULT nextval('seq_markets_id'),
    fews_id INTEGER UNIQUE NOT NULL,     -- market_id from API (e.g., 57830)
    fnid VARCHAR UNIQUE NOT NULL,        -- FEWS NET ID (e.g., HT0000M001)
    name VARCHAR NOT NULL,               -- Market name
    admin_1 VARCHAR,                     -- Department (e.g., Nord)
    admin_2 VARCHAR,                     -- Commune (e.g., Cap Haitien)
    country_code VARCHAR DEFAULT 'HT',
    latitude DOUBLE,
    longitude DOUBLE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Products dimension table
CREATE TABLE IF NOT EXISTS products (
    id INTEGER PRIMARY KEY DEFAULT nextval('seq_products_id'),
    name VARCHAR NOT NULL,               -- Product name (e.g., Beans (Black))
    cpcv2 VARCHAR,                       -- CPC v2 code (e.g., R01701AC)
    cpcv2_description VARCHAR,           -- Description
    product_source VARCHAR,              -- Local or Import
    is_staple_food BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(name, product_source)
);

-- Units dimension table
CREATE TABLE IF NOT EXISTS units (
    id INTEGER PRIMARY KEY DEFAULT nextval('seq_units_id'),
    name VARCHAR NOT NULL UNIQUE,        -- e.g., 6_lb, 175_g, 350_g
    unit_type VARCHAR,                   -- e.g., Weight
    common_unit VARCHAR,                 -- e.g., kg (standardized)
    conversion_factor DOUBLE             -- To convert to common_unit
);

-- Data sources dimension table
CREATE TABLE IF NOT EXISTS data_sources (
    id INTEGER PRIMARY KEY DEFAULT nextval('seq_sources_id'),
    fews_id INTEGER UNIQUE,              -- datasourceorganization from API
    name VARCHAR NOT NULL,               -- e.g., CNSA/FEWS NET, Haiti
    document_name VARCHAR
);

-- ============================================================
-- FACT TABLE
-- ============================================================

-- Price observations fact table
CREATE TABLE IF NOT EXISTS price_observations (
    id INTEGER PRIMARY KEY DEFAULT nextval('seq_prices_id'),
    market_id INTEGER NOT NULL,
    product_id INTEGER NOT NULL,
    unit_id INTEGER NOT NULL,
    source_id INTEGER,

    -- Date fields
    period_date DATE NOT NULL,           -- End of period (e.g., 2005-01-31)
    start_date DATE,                     -- Start of period

    -- Price data
    price_type VARCHAR DEFAULT 'Retail',
    currency VARCHAR DEFAULT 'HTG',
    value DOUBLE NOT NULL,               -- Price in local currency

    -- Standardized price
    exchange_rate DOUBLE,                -- HTG to USD
    common_unit_price DOUBLE,            -- Price per kg
    common_currency_price DOUBLE,        -- Price in USD

    -- Metadata
    collection_status VARCHAR,           -- e.g., Published
    fews_dataseries_id INTEGER,          -- dataseries from API

    -- Timestamps
    api_modified_at TIMESTAMP,           -- 'modified' from API
    imported_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

    -- Foreign keys
    FOREIGN KEY (market_id) REFERENCES markets(id),
    FOREIGN KEY (product_id) REFERENCES products(id),
    FOREIGN KEY (unit_id) REFERENCES units(id),
    FOREIGN KEY (source_id) REFERENCES data_sources(id),

    -- Unique constraint to prevent duplicates
    UNIQUE(market_id, product_id, unit_id, period_date, price_type)
);

-- ============================================================
-- TRACKING TABLE
-- ============================================================

-- Import log for tracking sync operations
CREATE TABLE IF NOT EXISTS import_log (
    id INTEGER PRIMARY KEY DEFAULT nextval('seq_imports_id'),
    import_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    records_fetched INTEGER,
    records_inserted INTEGER,
    records_updated INTEGER,
    records_skipped INTEGER,
    date_range_start DATE,
    date_range_end DATE,
    status VARCHAR,                      -- success, failed, partial
    error_message VARCHAR
);

-- ============================================================
-- INDEXES
-- ============================================================

CREATE INDEX IF NOT EXISTS idx_price_obs_date ON price_observations(period_date);
CREATE INDEX IF NOT EXISTS idx_price_obs_market ON price_observations(market_id);
CREATE INDEX IF NOT EXISTS idx_price_obs_product ON price_observations(product_id);
CREATE INDEX IF NOT EXISTS idx_price_obs_market_product ON price_observations(market_id, product_id);

-- ============================================================
-- VIEWS (Optional convenience views)
-- ============================================================

-- View: Latest prices with market and product names
CREATE OR REPLACE VIEW v_latest_prices AS
SELECT
    m.name AS market,
    m.admin_1 AS department,
    p.name AS product,
    p.product_source,
    u.name AS unit,
    po.period_date,
    po.value AS price_htg,
    po.common_currency_price AS price_usd,
    po.exchange_rate
FROM price_observations po
JOIN markets m ON po.market_id = m.id
JOIN products p ON po.product_id = p.id
JOIN units u ON po.unit_id = u.id
WHERE po.period_date = (SELECT MAX(period_date) FROM price_observations);

-- View: Price time series with computed changes
CREATE OR REPLACE VIEW v_price_timeseries AS
SELECT
    m.name AS market,
    p.name AS product,
    u.name AS unit,
    po.period_date,
    po.value AS price,
    po.common_currency_price AS price_usd,
    LAG(po.value, 1) OVER w AS price_1m_ago,
    LAG(po.value, 12) OVER w AS price_1y_ago,
    (po.value - LAG(po.value, 1) OVER w) / NULLIF(LAG(po.value, 1) OVER w, 0) * 100 AS pct_change_1m,
    (po.value - LAG(po.value, 12) OVER w) / NULLIF(LAG(po.value, 12) OVER w, 0) * 100 AS pct_change_1y,
    AVG(po.value) OVER (
        PARTITION BY po.market_id, po.product_id, po.unit_id
        ORDER BY po.period_date
        ROWS BETWEEN 11 PRECEDING AND CURRENT ROW
    ) AS moving_avg_12m
FROM price_observations po
JOIN markets m ON po.market_id = m.id
JOIN products p ON po.product_id = p.id
JOIN units u ON po.unit_id = u.id
WINDOW w AS (PARTITION BY po.market_id, po.product_id, po.unit_id ORDER BY po.period_date);
