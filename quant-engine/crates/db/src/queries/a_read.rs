//! Read queries for A-share data — SELECT from MySQL.

use chrono::NaiveDate;
use sqlx::MySqlPool;
use tracing::debug;

use crate::models::a_stock::*;

/// Load all A-share basics including delisted (avoid survivorship bias).
pub async fn get_all_a_stocks(pool: &MySqlPool) -> Result<Vec<AStockBasic>, sqlx::Error> {
    let rows = sqlx::query_as::<_, AStockBasic>(
        "SELECT * FROM a_stock_basic"
    ).fetch_all(pool).await?;
    debug!("Loaded {} A-share stocks (including delisted)", rows.len());
    Ok(rows)
}

/// Parallel-sharded load of `a_daily_price` window.
///
/// Sharding by `ts_code` into N buckets lets sqlx decode N partitions
/// concurrently on the tokio multi-thread runtime.
pub async fn get_a_daily_prices(
    pool: &MySqlPool,
    start: NaiveDate,
    end: NaiveDate,
) -> Result<Vec<ADailyPrice>, sqlx::Error> {
    const N_SHARDS: usize = 8;

    let codes: Vec<(String,)> = sqlx::query_as(
        "SELECT DISTINCT ts_code FROM a_daily_price \
         WHERE trade_date >= ? AND trade_date <= ?"
    ).bind(start).bind(end).fetch_all(pool).await?;
    let codes: Vec<String> = codes.into_iter().map(|t| t.0).collect();
    if codes.is_empty() {
        return Ok(Vec::new());
    }

    let per_bucket = codes.len().div_ceil(N_SHARDS);
    let buckets: Vec<Vec<String>> = codes.chunks(per_bucket).map(|c| c.to_vec()).collect();

    let mut tasks = Vec::with_capacity(buckets.len());
    for bucket in buckets {
        let pool = pool.clone();
        tasks.push(tokio::spawn(async move {
            // Build dynamic IN (?, ?, ...) clause for MySQL
            let placeholders: Vec<&str> = bucket.iter().map(|_| "?").collect();
            let in_clause = placeholders.join(",");
            let sql = format!(
                "SELECT * FROM a_daily_price \
                 WHERE ts_code IN ({in_clause}) AND trade_date >= ? AND trade_date <= ? \
                 ORDER BY ts_code, trade_date"
            );
            let mut q = sqlx::query_as::<_, ADailyPrice>(&sql);
            for code in &bucket {
                q = q.bind(code);
            }
            q = q.bind(start).bind(end);
            q.fetch_all(&pool).await
        }));
    }

    let mut all_rows: Vec<ADailyPrice> = Vec::new();
    for t in tasks {
        let part = t.await.expect("tokio join")?;
        all_rows.extend(part);
    }
    Ok(all_rows)
}

pub async fn get_a_index_daily(
    pool: &MySqlPool,
    ts_code: &str,
    start: NaiveDate,
    end: NaiveDate,
) -> Result<Vec<AIndexDaily>, sqlx::Error> {
    sqlx::query_as::<_, AIndexDaily>(
        "SELECT * FROM a_index_daily WHERE ts_code = ? AND trade_date >= ? AND trade_date <= ? ORDER BY trade_date"
    ).bind(ts_code).bind(start).bind(end).fetch_all(pool).await
}

pub async fn get_a_financial_indicators(
    pool: &MySqlPool,
    start: NaiveDate,
    end: NaiveDate,
) -> Result<Vec<AFinancialIndicatorRow>, sqlx::Error> {
    sqlx::query_as::<_, AFinancialIndicatorRow>(
        "SELECT * FROM a_financial_indicator WHERE end_date >= ? AND end_date <= ? \
         ORDER BY ts_code, end_date DESC"
    ).bind(start).bind(end).fetch_all(pool).await
}

pub async fn get_a_financial_income(
    pool: &MySqlPool,
    start: NaiveDate,
    end: NaiveDate,
) -> Result<Vec<AFinancialIncomeRow>, sqlx::Error> {
    sqlx::query_as::<_, AFinancialIncomeRow>(
        "SELECT * FROM a_financial_income WHERE end_date >= ? AND end_date <= ? \
         ORDER BY ts_code, end_date DESC"
    ).bind(start).bind(end).fetch_all(pool).await
}

pub async fn get_a_financial_balance(
    pool: &MySqlPool,
    start: NaiveDate,
    end: NaiveDate,
) -> Result<Vec<AFinancialBalanceRow>, sqlx::Error> {
    sqlx::query_as::<_, AFinancialBalanceRow>(
        "SELECT * FROM a_financial_balance WHERE end_date >= ? AND end_date <= ? \
         ORDER BY ts_code, end_date DESC"
    ).bind(start).bind(end).fetch_all(pool).await
}

pub async fn get_a_financial_cashflow(
    pool: &MySqlPool,
    start: NaiveDate,
    end: NaiveDate,
) -> Result<Vec<AFinancialCashflowRow>, sqlx::Error> {
    sqlx::query_as::<_, AFinancialCashflowRow>(
        "SELECT * FROM a_financial_cashflow WHERE end_date >= ? AND end_date <= ? \
         ORDER BY ts_code, end_date DESC"
    ).bind(start).bind(end).fetch_all(pool).await
}

pub async fn get_a_industry_class(pool: &MySqlPool) -> Result<Vec<AIndustryClass>, sqlx::Error> {
    sqlx::query_as::<_, AIndustryClass>(
        "SELECT * FROM a_industry_class WHERE src = 'SW2021' AND level = 'L1' \
         ORDER BY ts_code, in_date"
    ).fetch_all(pool).await
}

pub async fn get_a_trade_cal(
    pool: &MySqlPool,
    exchange: &str,
    start: NaiveDate,
    end: NaiveDate,
) -> Result<Vec<ATradeCal>, sqlx::Error> {
    sqlx::query_as::<_, ATradeCal>(
        "SELECT * FROM a_trade_cal WHERE exchange = ? AND cal_date >= ? AND cal_date <= ? AND is_open = 1 ORDER BY cal_date"
    ).bind(exchange).bind(start).bind(end).fetch_all(pool).await
}

pub async fn count_a_rows(pool: &MySqlPool, table: &str) -> Result<i64, sqlx::Error> {
    let count: (i64,) = sqlx::query_as(&format!("SELECT COUNT(*) FROM {table}"))
        .fetch_one(pool).await?;
    Ok(count.0)
}

/// Load `a_top_list` (龙虎榜每日交易明细) for a date window. Small table
/// (~90k rows/year) — no sharding needed.
pub async fn get_a_top_list(
    pool: &MySqlPool,
    start: NaiveDate,
    end: NaiveDate,
) -> Result<Vec<ATopList>, sqlx::Error> {
    sqlx::query_as::<_, ATopList>(
        "SELECT * FROM a_top_list WHERE trade_date >= ? AND trade_date <= ? \
         ORDER BY ts_code, trade_date"
    ).bind(start).bind(end).fetch_all(pool).await
}

/// Parallel-sharded load of `a_margin_detail` window (mirrors
/// `get_a_daily_prices` — this table is large, ~1.7M rows/year).
pub async fn get_a_margin_detail(
    pool: &MySqlPool,
    start: NaiveDate,
    end: NaiveDate,
) -> Result<Vec<AMarginDetail>, sqlx::Error> {
    const N_SHARDS: usize = 8;

    let codes: Vec<(String,)> = sqlx::query_as(
        "SELECT DISTINCT ts_code FROM a_margin_detail \
         WHERE trade_date >= ? AND trade_date <= ?"
    ).bind(start).bind(end).fetch_all(pool).await?;
    let codes: Vec<String> = codes.into_iter().map(|t| t.0).collect();
    if codes.is_empty() {
        return Ok(Vec::new());
    }

    let per_bucket = codes.len().div_ceil(N_SHARDS);
    let buckets: Vec<Vec<String>> = codes.chunks(per_bucket).map(|c| c.to_vec()).collect();

    let mut tasks = Vec::with_capacity(buckets.len());
    for bucket in buckets {
        let pool = pool.clone();
        tasks.push(tokio::spawn(async move {
            let placeholders: Vec<&str> = bucket.iter().map(|_| "?").collect();
            let in_clause = placeholders.join(",");
            let sql = format!(
                "SELECT * FROM a_margin_detail \
                 WHERE ts_code IN ({in_clause}) AND trade_date >= ? AND trade_date <= ? \
                 ORDER BY ts_code, trade_date"
            );
            let mut q = sqlx::query_as::<_, AMarginDetail>(&sql);
            for code in &bucket {
                q = q.bind(code);
            }
            q = q.bind(start).bind(end);
            q.fetch_all(&pool).await
        }));
    }

    let mut all_rows: Vec<AMarginDetail> = Vec::new();
    for t in tasks {
        let part = t.await.expect("tokio join")?;
        all_rows.extend(part);
    }
    Ok(all_rows)
}


/// Load `a_forecast` (业绩预告) window, filtered by `ann_date` (disclosure
/// date, PIT-safe) — NOT `end_date` (reporting period).
pub async fn get_a_forecast(
    pool: &MySqlPool,
    start: NaiveDate,
    end: NaiveDate,
) -> Result<Vec<AForecast>, sqlx::Error> {
    sqlx::query_as::<_, AForecast>(
        "SELECT * FROM a_forecast WHERE ann_date >= ? AND ann_date <= ? \
         ORDER BY ts_code, ann_date"
    ).bind(start).bind(end).fetch_all(pool).await
}

/// Load `a_express` (业绩快报) window, filtered by `ann_date` (disclosure
/// date, PIT-safe) — NOT `end_date` (reporting period).
pub async fn get_a_express(
    pool: &MySqlPool,
    start: NaiveDate,
    end: NaiveDate,
) -> Result<Vec<AExpress>, sqlx::Error> {
    sqlx::query_as::<_, AExpress>(
        "SELECT * FROM a_express WHERE ann_date >= ? AND ann_date <= ? \
         ORDER BY ts_code, ann_date"
    ).bind(start).bind(end).fetch_all(pool).await
}

/// Load `a_stk_holdertrade` (股东增减持) window, filtered by `ann_date`
/// (disclosure date, PIT-safe).
pub async fn get_a_stk_holdertrade(
    pool: &MySqlPool,
    start: NaiveDate,
    end: NaiveDate,
) -> Result<Vec<AStkHolderTrade>, sqlx::Error> {
    sqlx::query_as::<_, AStkHolderTrade>(
        "SELECT * FROM a_stk_holdertrade WHERE ann_date >= ? AND ann_date <= ? \
         ORDER BY ts_code, ann_date"
    ).bind(start).bind(end).fetch_all(pool).await
}

/// Load `a_repurchase` (股票回购) window, filtered by `ann_date` (disclosure
/// date, PIT-safe).
pub async fn get_a_repurchase(
    pool: &MySqlPool,
    start: NaiveDate,
    end: NaiveDate,
) -> Result<Vec<ARepurchase>, sqlx::Error> {
    sqlx::query_as::<_, ARepurchase>(
        "SELECT * FROM a_repurchase WHERE ann_date >= ? AND ann_date <= ? \
         ORDER BY ts_code, ann_date"
    ).bind(start).bind(end).fetch_all(pool).await
}

/// Load `a_share_float` (限售股解禁) window, filtered by `float_date` (the
/// actual unlock date — publicly scheduled years in advance at issuance, so
/// using it ahead of time is not look-ahead bias; see model doc comment).
pub async fn get_a_share_float(
    pool: &MySqlPool,
    start: NaiveDate,
    end: NaiveDate,
) -> Result<Vec<AShareFloat>, sqlx::Error> {
    sqlx::query_as::<_, AShareFloat>(
        "SELECT * FROM a_share_float WHERE float_date >= ? AND float_date <= ? \
         ORDER BY ts_code, float_date"
    ).bind(start).bind(end).fetch_all(pool).await
}
