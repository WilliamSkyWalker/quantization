//! US stock models — sqlx FromRow structs mapping to PostgreSQL tables.
//!
//! Schema: `quant.*` (managed=False, Django does not manage migrations).
//! All float fields are Option<f64> matching PostgreSQL `double precision NULL`.

use chrono::{NaiveDate, NaiveDateTime};
use sqlx::FromRow;

// ── us_stock_basic ──────────────────────────────────────────────────────

#[derive(Debug, Clone, FromRow)]
pub struct UsStockBasic {
    pub id: i32,
    pub ticker: String,
    pub company_name: Option<String>,
    pub market_cap: Option<f64>,
    pub sector: Option<String>,
    pub industry: Option<String>,
    pub beta: Option<f64>,
    pub price: Option<f64>,
    pub last_annual_dividend: Option<f64>,
    pub volume: Option<f64>,
    pub exchange: Option<String>,
    pub exchange_short_name: Option<String>,
    pub country: Option<String>,
    pub is_etf: Option<i32>,
    pub is_fund: Option<i32>,
    pub is_actively_trading: Option<i32>,
    pub updated_at: Option<NaiveDateTime>,
}

// ── us_daily_price ──────────────────────────────────────────────────────

#[derive(Debug, Clone, FromRow)]
pub struct UsDailyPrice {
    pub id: i32,
    pub ticker: String,
    pub trade_date: NaiveDate,
    pub open: Option<f64>,
    pub high: Option<f64>,
    pub low: Option<f64>,
    pub close: Option<f64>,
    pub adj_close: Option<f64>,
    pub volume: Option<f64>,
    pub unadjusted_volume: Option<f64>,
    pub change: Option<f64>,
    pub change_percent: Option<f64>,
    pub vwap: Option<f64>,
    pub label: Option<String>,
    pub change_over_time: Option<f64>,
    pub updated_at: Option<NaiveDateTime>,
}

// ── us_index_daily ──────────────────────────────────────────────────────

#[derive(Debug, Clone, FromRow)]
pub struct UsIndexDaily {
    pub id: i32,
    pub index_code: String,
    pub trade_date: NaiveDate,
    pub open: Option<f64>,
    pub high: Option<f64>,
    pub low: Option<f64>,
    pub close: Option<f64>,
    pub volume: Option<f64>,
    pub adj_close: Option<f64>,
    pub change: Option<f64>,
    pub change_percent: Option<f64>,
    pub vwap: Option<f64>,
    pub updated_at: Option<NaiveDateTime>,
}

// ── us_financial_data (IS+BS+CF merged, ~130 columns) ───────────────────
// Only the columns needed for factor computation are typed explicitly.
// Full table has ~130 float columns; we select * and access by name via queries.

#[derive(Debug, Clone, FromRow)]
pub struct UsFinancialRow {
    pub id: i32,
    pub ticker: String,
    pub period: Option<String>,
    pub date: Option<NaiveDate>,
    pub filing_date: Option<NaiveDate>,
    pub fiscal_year: Option<String>,
    // Income Statement (key fields)
    pub revenue: Option<f64>,
    pub gross_profit: Option<f64>,
    pub operating_income: Option<f64>,
    pub ebitda: Option<f64>,
    pub ebit: Option<f64>,
    pub net_income: Option<f64>,
    pub eps: Option<f64>,
    pub eps_diluted: Option<f64>,
    pub weighted_average_shs_out: Option<f64>,
    pub research_and_development_expenses: Option<f64>,
    pub selling_general_and_administrative_expenses: Option<f64>,
    pub depreciation_and_amortization: Option<f64>,
    pub interest_expense: Option<f64>,
    pub income_tax_expense: Option<f64>,
    // Balance Sheet (key fields)
    pub cash_and_cash_equivalents: Option<f64>,
    pub net_receivables: Option<f64>,
    pub inventory: Option<f64>,
    pub total_current_assets: Option<f64>,
    pub total_assets: Option<f64>,
    pub total_current_liabilities: Option<f64>,
    pub total_liabilities: Option<f64>,
    pub total_debt: Option<f64>,
    pub total_stockholders_equity: Option<f64>,
    pub retained_earnings: Option<f64>,
    pub property_plant_equipment_net: Option<f64>,
    pub goodwill: Option<f64>,
    pub intangible_assets: Option<f64>,
    // Cash Flow (key fields)
    pub operating_cash_flow: Option<f64>,
    pub capital_expenditure: Option<f64>,
    pub free_cash_flow: Option<f64>,
    pub dividends_paid: Option<f64>,
    pub common_stock_repurchased: Option<f64>,
    pub net_stock_issuance: Option<f64>,
}

// ── us_key_metric ───────────────────────────────────────────────────────

#[derive(Debug, Clone, FromRow)]
pub struct UsKeyMetricRow {
    pub id: i32,
    pub ticker: String,
    pub period: Option<String>,
    pub date: Option<NaiveDate>,
    // Commonly used metrics
    pub pe_ratio: Option<f64>,
    pub price_to_book_value: Option<f64>,
    pub ev_to_sales: Option<f64>,
    pub ev_to_free_cash_flow: Option<f64>,
    pub roe: Option<f64>,
    pub roic: Option<f64>,
    pub current_ratio: Option<f64>,
    pub debt_to_equity: Option<f64>,
    pub dividend_yield: Option<f64>,
    pub payout_ratio: Option<f64>,
}

// ── us_industry_class ───────────────────────────────────────────────────

#[derive(Debug, Clone, FromRow)]
pub struct UsIndustryClass {
    pub id: i32,
    pub ticker: String,
    pub sector: Option<String>,
    pub industry: Option<String>,
}

// ── us_enterprise_value ─────────────────────────────────────────────────

#[derive(Debug, Clone, FromRow)]
pub struct UsEnterpriseValue {
    pub id: i32,
    pub ticker: String,
    pub date: Option<NaiveDate>,
    pub market_capitalization: Option<f64>,
    pub enterprise_value: Option<f64>,
}

// ── us_earnings_surprise ────────────────────────────────────────────────

#[derive(Debug, Clone, FromRow)]
pub struct UsEarningsSurprise {
    pub id: i32,
    pub ticker: String,
    pub date: Option<NaiveDate>,
    pub actual_earning_result: Option<f64>,
    pub estimated_earning: Option<f64>,
}

// ── us_eps_estimate ─────────────────────────────────────────────────────

#[derive(Debug, Clone, FromRow)]
pub struct UsEpsEstimate {
    pub id: i32,
    pub ticker: String,
    pub date: Option<NaiveDate>,
    pub estimated_eps_avg: Option<f64>,
    pub estimated_eps_low: Option<f64>,
    pub estimated_eps_high: Option<f64>,
    pub number_analysts_estimated: Option<f64>,
}

// ── us_analyst_recommendation ───────────────────────────────────────────

#[derive(Debug, Clone, FromRow)]
pub struct UsAnalystRecommendation {
    pub id: i32,
    pub ticker: String,
    pub date: Option<NaiveDate>,
    pub analyst_ratings_buy: Option<f64>,
    pub analyst_ratings_sell: Option<f64>,
    pub analyst_ratings_hold: Option<f64>,
    pub analyst_ratings_strong_buy: Option<f64>,
    pub analyst_ratings_strong_sell: Option<f64>,
}

// ── us_corporate_action (dividends) ─────────────────────────────────────

#[derive(Debug, Clone, FromRow)]
pub struct UsCorporateAction {
    pub id: i32,
    pub ticker: String,
    pub date: Option<NaiveDate>,
    pub dividend: Option<f64>,
    pub adj_dividend: Option<f64>,
    pub declaration_date: Option<NaiveDate>,
    pub record_date: Option<NaiveDate>,
    pub payment_date: Option<NaiveDate>,
}

// ── us_insider_trade ────────────────────────────────────────────────────

#[derive(Debug, Clone, FromRow)]
pub struct UsInsiderTrade {
    pub id: i32,
    pub ticker: String,
    pub filing_date: Option<NaiveDate>,
    pub transaction_type: Option<String>,
    pub securities_transacted: Option<f64>,
    pub price: Option<f64>,
}

// ── us_macro_indicator ──────────────────────────────────────────────────

#[derive(Debug, Clone, FromRow)]
pub struct UsMacroIndicator {
    pub id: i32,
    pub indicator_code: String,
    pub report_date: Option<NaiveDate>,
    pub value: Option<f64>,
    pub updated_at: Option<NaiveDateTime>,
}

// ── us_shares_float ─────────────────────────────────────────────────────

#[derive(Debug, Clone, FromRow)]
pub struct UsSharesFloat {
    pub id: i32,
    pub ticker: String,
    pub free_float: Option<f64>,
    pub float_shares: Option<f64>,
    pub outstanding_shares: Option<f64>,
}

// ── us_dark_pool_volume ─────────────────────────────────────────────────

#[derive(Debug, Clone, FromRow)]
pub struct UsDarkPoolVolume {
    pub id: i32,
    pub ticker: String,
    pub date: Option<NaiveDate>,
    pub short_volume: Option<f64>,
    pub total_volume: Option<f64>,
}

// ── us_institutional_holder ─────────────────────────────────────────────

#[derive(Debug, Clone, FromRow)]
pub struct UsInstitutionalHolder {
    pub id: i32,
    pub ticker: String,
    pub date: Option<NaiveDate>,
    pub number_of_13f_shares: Option<f64>,
}
