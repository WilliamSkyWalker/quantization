//! Read queries for US stock data — SELECT from PostgreSQL.
//!
//! All queries use runtime-checked sqlx::query_as (not compile-time checked)
//! since the DB schema lives in the `quant` schema and compile-time checks
//! would require DATABASE_URL at build time.

use chrono::NaiveDate;
use sqlx::PgPool;
use tracing::debug;

use crate::models::us_stock::*;

// ── Stock Basic ─────────────────────────────────────────────────────────

pub async fn get_all_active_stocks(pool: &PgPool) -> Result<Vec<UsStockBasic>, sqlx::Error> {
    let rows = sqlx::query_as::<_, UsStockBasic>(
        "SELECT * FROM us_stock_basic WHERE is_actively_trading = 1 AND is_etf = 0 AND is_fund = 0"
    )
    .fetch_all(pool)
    .await?;
    debug!("Loaded {} active stocks", rows.len());
    Ok(rows)
}

// ── Daily Prices ────────────────────────────────────────────────────────

pub async fn get_daily_prices(
    pool: &PgPool,
    start: NaiveDate,
    end: NaiveDate,
) -> Result<Vec<UsDailyPrice>, sqlx::Error> {
    let rows = sqlx::query_as::<_, UsDailyPrice>(
        "SELECT * FROM us_daily_price WHERE trade_date >= $1 AND trade_date <= $2 ORDER BY ticker, trade_date"
    )
    .bind(start)
    .bind(end)
    .fetch_all(pool)
    .await?;
    debug!("Loaded {} daily price rows ({} to {})", rows.len(), start, end);
    Ok(rows)
}

pub async fn get_daily_prices_for_ticker(
    pool: &PgPool,
    ticker: &str,
    start: NaiveDate,
    end: NaiveDate,
) -> Result<Vec<UsDailyPrice>, sqlx::Error> {
    sqlx::query_as::<_, UsDailyPrice>(
        "SELECT * FROM us_daily_price WHERE ticker = $1 AND trade_date >= $2 AND trade_date <= $3 ORDER BY trade_date"
    )
    .bind(ticker)
    .bind(start)
    .bind(end)
    .fetch_all(pool)
    .await
}

// ── Index Daily ─────────────────────────────────────────────────────────

pub async fn get_index_daily(
    pool: &PgPool,
    index_code: &str,
    start: NaiveDate,
    end: NaiveDate,
) -> Result<Vec<UsIndexDaily>, sqlx::Error> {
    sqlx::query_as::<_, UsIndexDaily>(
        "SELECT * FROM us_index_daily WHERE index_code = $1 AND trade_date >= $2 AND trade_date <= $3 ORDER BY trade_date"
    )
    .bind(index_code)
    .bind(start)
    .bind(end)
    .fetch_all(pool)
    .await
}

// ── Financial Data ──────────────────────────────────────────────────────

pub async fn get_financials(
    pool: &PgPool,
    start: NaiveDate,
    end: NaiveDate,
) -> Result<Vec<UsFinancialRow>, sqlx::Error> {
    let rows = sqlx::query_as::<_, UsFinancialRow>(
        "SELECT id, ticker, period, date, filing_date, fiscal_year, \
         revenue, gross_profit, operating_income, ebitda, ebit, net_income, \
         eps, eps_diluted, weighted_average_shs_out, \
         research_and_development_expenses, selling_general_and_administrative_expenses, \
         depreciation_and_amortization, interest_expense, income_tax_expense, \
         cash_and_cash_equivalents, net_receivables, inventory, \
         total_current_assets, total_assets, total_current_liabilities, \
         total_liabilities, total_debt, total_stockholders_equity, \
         retained_earnings, property_plant_equipment_net, goodwill, intangible_assets, \
         operating_cash_flow, capital_expenditure, free_cash_flow, \
         dividends_paid, common_stock_repurchased, \
         COALESCE(debt_repayment, 0) - COALESCE(common_stock_issued, 0) + COALESCE(common_stock_repurchased, 0) as net_stock_issuance \
         FROM us_financial_data WHERE filing_date >= $1 AND filing_date <= $2 \
         ORDER BY ticker, filing_date DESC"
    )
    .bind(start)
    .bind(end)
    .fetch_all(pool)
    .await?;
    debug!("Loaded {} financial rows", rows.len());
    Ok(rows)
}

// ── Enterprise Value ────────────────────────────────────────────────────

pub async fn get_enterprise_values(
    pool: &PgPool,
    start: NaiveDate,
    end: NaiveDate,
) -> Result<Vec<UsEnterpriseValue>, sqlx::Error> {
    sqlx::query_as::<_, UsEnterpriseValue>(
        "SELECT id, ticker, date, market_capitalization, enterprise_value \
         FROM us_enterprise_value WHERE date >= $1 AND date <= $2 \
         ORDER BY ticker, date DESC"
    )
    .bind(start)
    .bind(end)
    .fetch_all(pool)
    .await
}

// ── Industry Classification ─────────────────────────────────────────────

pub async fn get_industry_class(pool: &PgPool) -> Result<Vec<UsIndustryClass>, sqlx::Error> {
    sqlx::query_as::<_, UsIndustryClass>(
        "SELECT * FROM us_industry_class"
    )
    .fetch_all(pool)
    .await
}

// ── Earnings Surprise ───────────────────────────────────────────────────

pub async fn get_earnings_surprises(
    pool: &PgPool,
    start: NaiveDate,
    end: NaiveDate,
) -> Result<Vec<UsEarningsSurprise>, sqlx::Error> {
    sqlx::query_as::<_, UsEarningsSurprise>(
        "SELECT * FROM us_earnings_surprise WHERE date >= $1 AND date <= $2 ORDER BY ticker, date DESC"
    )
    .bind(start)
    .bind(end)
    .fetch_all(pool)
    .await
}

// ── EPS Estimates ───────────────────────────────────────────────────────

pub async fn get_eps_estimates(
    pool: &PgPool,
    start: NaiveDate,
    end: NaiveDate,
) -> Result<Vec<UsEpsEstimate>, sqlx::Error> {
    sqlx::query_as::<_, UsEpsEstimate>(
        "SELECT * FROM us_eps_estimate WHERE date >= $1 AND date <= $2 ORDER BY ticker, date DESC"
    )
    .bind(start)
    .bind(end)
    .fetch_all(pool)
    .await
}

// ── Macro Indicators ────────────────────────────────────────────────────

pub async fn get_macro_indicators(
    pool: &PgPool,
    indicator_code: &str,
    start: NaiveDate,
    end: NaiveDate,
) -> Result<Vec<UsMacroIndicator>, sqlx::Error> {
    sqlx::query_as::<_, UsMacroIndicator>(
        "SELECT * FROM us_macro_indicator WHERE indicator_code = $1 AND report_date >= $2 AND report_date <= $3 ORDER BY report_date DESC"
    )
    .bind(indicator_code)
    .bind(start)
    .bind(end)
    .fetch_all(pool)
    .await
}

// ── Import Progress ─────────────────────────────────────────────────────

pub async fn get_completed_tickers(
    pool: &PgPool,
    table_name: &str,
) -> Result<Vec<String>, sqlx::Error> {
    let rows = sqlx::query_scalar::<_, String>(
        "SELECT ticker FROM import_progress WHERE table_name = $1"
    )
    .bind(table_name)
    .fetch_all(pool)
    .await?;
    Ok(rows)
}

// ── Table counts (for db_status equivalent) ─────────────────────────────

pub async fn count_rows(pool: &PgPool, table: &str) -> Result<i64, sqlx::Error> {
    let count: (i64,) = sqlx::query_as(
        &format!("SELECT COUNT(*) FROM {table}")
    )
    .fetch_one(pool)
    .await?;
    Ok(count.0)
}
