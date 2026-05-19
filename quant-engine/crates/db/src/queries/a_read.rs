//! Read queries for A-share data — SELECT from PostgreSQL.

use chrono::NaiveDate;
use sqlx::PgPool;
use tracing::debug;

use crate::models::a_stock::*;

/// Load all A-share basics including delisted (avoid survivorship bias).
/// Universe filter at compute-time uses delist_date to exclude delisted-by-then.
pub async fn get_all_a_stocks(pool: &PgPool) -> Result<Vec<AStockBasic>, sqlx::Error> {
    let rows = sqlx::query_as::<_, AStockBasic>(
        "SELECT * FROM a_stock_basic"
    ).fetch_all(pool).await?;
    debug!("Loaded {} A-share stocks (including delisted)", rows.len());
    Ok(rows)
}

/// Parallel-sharded load of 4-year-ish `a_daily_price` window.
///
/// Single SELECT decodes ~5M rows × 30 cols serially on one thread (~50 min).
/// Sharding by `ts_code` into N buckets (N = pool max_connections) lets sqlx
/// decode N partitions concurrently on the tokio multi-thread runtime.
pub async fn get_a_daily_prices(
    pool: &PgPool,
    start: NaiveDate,
    end: NaiveDate,
) -> Result<Vec<ADailyPrice>, sqlx::Error> {
    const N_SHARDS: usize = 8;

    // Step 1: discover ts_codes with data in the window (index-only scan, fast).
    let codes: Vec<(String,)> = sqlx::query_as(
        "SELECT DISTINCT ts_code FROM a_daily_price \
         WHERE trade_date >= $1 AND trade_date <= $2"
    ).bind(start).bind(end).fetch_all(pool).await?;
    let codes: Vec<String> = codes.into_iter().map(|t| t.0).collect();
    if codes.is_empty() {
        return Ok(Vec::new());
    }

    // Step 2: shard ts_codes into N buckets. div_ceil so the last bucket isn't oversized.
    let per_bucket = codes.len().div_ceil(N_SHARDS);
    let buckets: Vec<Vec<String>> = codes.chunks(per_bucket).map(|c| c.to_vec()).collect();

    // Step 3: spawn N concurrent SELECTs. Each task gets its own pool handle
    // (PgPool is Arc-internally) and owns its bucket vector.
    let mut tasks = Vec::with_capacity(buckets.len());
    for bucket in buckets {
        let pool = pool.clone();
        tasks.push(tokio::spawn(async move {
            sqlx::query_as::<_, ADailyPrice>(
                "SELECT * FROM a_daily_price \
                 WHERE ts_code = ANY($1) AND trade_date >= $2 AND trade_date <= $3 \
                 ORDER BY ts_code, trade_date"
            ).bind(&bucket).bind(start).bind(end).fetch_all(&pool).await
        }));
    }

    // Step 4: collect. Reserve roughly the right capacity to avoid reallocs.
    let mut all_rows: Vec<ADailyPrice> = Vec::new();
    for t in tasks {
        let part = t.await.expect("tokio join")?;
        all_rows.extend(part);
    }
    Ok(all_rows)
}

pub async fn get_a_index_daily(
    pool: &PgPool,
    ts_code: &str,
    start: NaiveDate,
    end: NaiveDate,
) -> Result<Vec<AIndexDaily>, sqlx::Error> {
    sqlx::query_as::<_, AIndexDaily>(
        "SELECT * FROM a_index_daily WHERE ts_code = $1 AND trade_date >= $2 AND trade_date <= $3 ORDER BY trade_date"
    ).bind(ts_code).bind(start).bind(end).fetch_all(pool).await
}

pub async fn get_a_financial_indicators(
    pool: &PgPool,
    start: NaiveDate,
    end: NaiveDate,
) -> Result<Vec<AFinancialIndicatorRow>, sqlx::Error> {
    sqlx::query_as::<_, AFinancialIndicatorRow>(
        "SELECT * FROM a_financial_indicator WHERE end_date >= $1 AND end_date <= $2 \
         ORDER BY ts_code, end_date DESC"
    ).bind(start).bind(end).fetch_all(pool).await
}

pub async fn get_a_financial_income(
    pool: &PgPool,
    start: NaiveDate,
    end: NaiveDate,
) -> Result<Vec<AFinancialIncomeRow>, sqlx::Error> {
    sqlx::query_as::<_, AFinancialIncomeRow>(
        "SELECT * FROM a_financial_income WHERE end_date >= $1 AND end_date <= $2 \
         ORDER BY ts_code, end_date DESC"
    ).bind(start).bind(end).fetch_all(pool).await
}

pub async fn get_a_financial_balance(
    pool: &PgPool,
    start: NaiveDate,
    end: NaiveDate,
) -> Result<Vec<AFinancialBalanceRow>, sqlx::Error> {
    sqlx::query_as::<_, AFinancialBalanceRow>(
        "SELECT * FROM a_financial_balance WHERE end_date >= $1 AND end_date <= $2 \
         ORDER BY ts_code, end_date DESC"
    ).bind(start).bind(end).fetch_all(pool).await
}

pub async fn get_a_financial_cashflow(
    pool: &PgPool,
    start: NaiveDate,
    end: NaiveDate,
) -> Result<Vec<AFinancialCashflowRow>, sqlx::Error> {
    sqlx::query_as::<_, AFinancialCashflowRow>(
        "SELECT * FROM a_financial_cashflow WHERE end_date >= $1 AND end_date <= $2 \
         ORDER BY ts_code, end_date DESC"
    ).bind(start).bind(end).fetch_all(pool).await
}

pub async fn get_a_industry_class(pool: &PgPool) -> Result<Vec<AIndustryClass>, sqlx::Error> {
    sqlx::query_as::<_, AIndustryClass>(
        "SELECT * FROM a_industry_class WHERE src = 'SW2021' AND level = 'L1' AND out_date IS NULL"
    ).fetch_all(pool).await
}

pub async fn get_a_trade_cal(
    pool: &PgPool,
    exchange: &str,
    start: NaiveDate,
    end: NaiveDate,
) -> Result<Vec<ATradeCal>, sqlx::Error> {
    sqlx::query_as::<_, ATradeCal>(
        "SELECT * FROM a_trade_cal WHERE exchange = $1 AND cal_date >= $2 AND cal_date <= $3 AND is_open = 1 ORDER BY cal_date"
    ).bind(exchange).bind(start).bind(end).fetch_all(pool).await
}

pub async fn count_a_rows(pool: &PgPool, table: &str) -> Result<i64, sqlx::Error> {
    let count: (i64,) = sqlx::query_as(&format!("SELECT COUNT(*) FROM {table}"))
        .fetch_one(pool).await?;
    Ok(count.0)
}
