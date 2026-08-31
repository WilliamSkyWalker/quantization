-- Additive migration: A-share sentiment/behavior data sources for the v2
-- (sentiment-driven) strategy — see quant-engine/crates/strategy/src/a_strategy_v2.rs.
--
-- Sources: Tushare top_list/top_inst (龙虎榜), margin/margin_detail (融资融券),
-- moneyflow_hsgt (沪深港通资金流向). Field names verified via live API probe
-- calls on 2026-08-30 (trade_date=20250210) — NOT copied from documentation,
-- per project rule against guessing column names.
--
-- Does not DROP any existing table. Safe to run against the live database.

CREATE TABLE a_top_list (
  id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
  trade_date DATE NOT NULL,
  ts_code VARCHAR(20) NOT NULL,
  name VARCHAR(100) NULL,
  `close` DOUBLE NULL,
  pct_change DOUBLE NULL,
  turnover_rate DOUBLE NULL,
  amount DOUBLE NULL,
  l_sell DOUBLE NULL,
  l_buy DOUBLE NULL,
  l_amount DOUBLE NULL,
  net_amount DOUBLE NULL,
  net_rate DOUBLE NULL,
  amount_rate DOUBLE NULL,
  float_values DOUBLE NULL,
  reason VARCHAR(200) NOT NULL,
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
);
CREATE UNIQUE INDEX uq_a_top_list_ts_code_trade_date_reason ON a_top_list (ts_code, trade_date, reason);

-- 机构/游资席位成交明细. Unique key includes exalter (席位名) + reason: a stock can
-- be listed under multiple trigger reasons on the same day, and the same seat
-- can appear once per reason (verified duplicates on probe call — (ts_code,
-- trade_date) alone is NOT unique).
CREATE TABLE a_top_inst (
  id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
  trade_date DATE NOT NULL,
  ts_code VARCHAR(20) NOT NULL,
  exalter VARCHAR(300) NOT NULL,
  buy DOUBLE NULL,
  buy_rate DOUBLE NULL,
  sell DOUBLE NULL,
  sell_rate DOUBLE NULL,
  net_buy DOUBLE NULL,
  side VARCHAR(10) NULL,
  reason VARCHAR(200) NOT NULL,
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
);
CREATE UNIQUE INDEX uq_a_top_inst_ts_code_trade_date_exalter_reason ON a_top_inst (ts_code, trade_date, exalter, reason);

-- 融资融券交易汇总（按交易所：SSE/SZSE/BSE）— market-wide, no per-stock dimension.
CREATE TABLE a_margin (
  id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
  trade_date DATE NOT NULL,
  exchange_id VARCHAR(20) NOT NULL,
  rzye DOUBLE NULL,
  rzmre DOUBLE NULL,
  rzche DOUBLE NULL,
  rqye DOUBLE NULL,
  rqmcl DOUBLE NULL,
  rzrqye DOUBLE NULL,
  rqyl DOUBLE NULL,
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
);
CREATE UNIQUE INDEX uq_a_margin_trade_date_exchange_id ON a_margin (trade_date, exchange_id);

-- 融资融券交易明细（按股票）。
CREATE TABLE a_margin_detail (
  id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
  trade_date DATE NOT NULL,
  ts_code VARCHAR(20) NOT NULL,
  rzye DOUBLE NULL,
  rqye DOUBLE NULL,
  rzmre DOUBLE NULL,
  rqyl DOUBLE NULL,
  rzche DOUBLE NULL,
  rqchl DOUBLE NULL,
  rqmcl DOUBLE NULL,
  rzrqye DOUBLE NULL,
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
);
CREATE UNIQUE INDEX uq_a_margin_detail_ts_code_trade_date ON a_margin_detail (ts_code, trade_date);

-- 沪深港通资金流向 — market-wide, one row per trade_date.
CREATE TABLE a_moneyflow_hsgt (
  id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
  trade_date DATE NOT NULL,
  ggt_ss DOUBLE NULL,
  ggt_sz DOUBLE NULL,
  hgt DOUBLE NULL,
  sgt DOUBLE NULL,
  north_money DOUBLE NULL,
  south_money DOUBLE NULL,
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
);
CREATE UNIQUE INDEX uq_a_moneyflow_hsgt_trade_date ON a_moneyflow_hsgt (trade_date);
