-- us_price_target_detail: per-analyst 历史目标价（FMP v4/price-target）
CREATE TABLE IF NOT EXISTS us_price_target_detail (
    id SERIAL PRIMARY KEY,
    ticker VARCHAR(20) NOT NULL,
    published_date TIMESTAMPTZ NOT NULL,
    analyst_company VARCHAR(200),
    analyst_name VARCHAR(200),
    price_target DOUBLE PRECISION,
    adj_price_target DOUBLE PRECISION,
    price_when_posted DOUBLE PRECISION,
    news_title VARCHAR(500),
    news_publisher VARCHAR(200),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_pt_detail_ticker_date_company
    ON us_price_target_detail (ticker, published_date, analyst_company);

CREATE INDEX IF NOT EXISTS ix_pt_detail_published
    ON us_price_target_detail (published_date);

CREATE INDEX IF NOT EXISTS ix_pt_detail_ticker
    ON us_price_target_detail (ticker);
