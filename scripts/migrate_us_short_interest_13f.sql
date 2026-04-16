-- ============================================================
-- 新增 us_dark_pool_volume + us_institutional_holder 两张表
-- 用法：psql $PG_URL -f scripts/migrate_us_short_interest_13f.sql
--
-- 注：原计划的 us_short_interest 改用 us_dark_pool_volume（FMP 计划不含
-- short interest 端点，改用 Quiver dark pool 数据作为日频 short volume 代理）。
-- ============================================================

SET search_path TO quant, public;

-- 1. us_dark_pool_volume（Quiver /historical/offexchange/{ticker}）
CREATE TABLE IF NOT EXISTS us_dark_pool_volume (
    id          BIGSERIAL PRIMARY KEY,
    ticker      VARCHAR(20) NOT NULL,
    date        DATE NOT NULL,
    otc_short   DOUBLE PRECISION,  -- 场外短卖股数
    otc_total   DOUBLE PRECISION,  -- 场外总成交股数
    dpi         DOUBLE PRECISION,  -- Dark Pool Indicator (otc_short / otc_total)
    updated_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (ticker, date)
);

CREATE INDEX IF NOT EXISTS idx_us_dark_pool_volume_ticker
    ON us_dark_pool_volume (ticker);
CREATE INDEX IF NOT EXISTS idx_us_dark_pool_volume_date
    ON us_dark_pool_volume (date);


-- 2. us_institutional_holder（FMP /stable/institutional-ownership/symbol-positions-summary）
CREATE TABLE IF NOT EXISTS us_institutional_holder (
    id                              BIGSERIAL PRIMARY KEY,
    ticker                          VARCHAR(20) NOT NULL,
    date                            DATE NOT NULL,  -- quarter end
    investors_holding               INTEGER,
    investors_holding_change        INTEGER,
    number_of_13f_shares            DOUBLE PRECISION,
    number_of_13f_shares_change     DOUBLE PRECISION,
    total_invested                  DOUBLE PRECISION,
    total_invested_change           DOUBLE PRECISION,
    ownership_percent               DOUBLE PRECISION,
    ownership_percent_change        DOUBLE PRECISION,
    new_positions                   INTEGER,
    increased_positions             INTEGER,
    closed_positions                INTEGER,
    reduced_positions               INTEGER,
    total_calls                     DOUBLE PRECISION,
    total_puts                      DOUBLE PRECISION,
    put_call_ratio                  DOUBLE PRECISION,
    updated_at                      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (ticker, date)
);

CREATE INDEX IF NOT EXISTS idx_us_institutional_holder_ticker
    ON us_institutional_holder (ticker);
CREATE INDEX IF NOT EXISTS idx_us_institutional_holder_date
    ON us_institutional_holder (date);
