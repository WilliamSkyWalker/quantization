//! A-share (Chinese stock market) models — sqlx FromRow structs.
//!
//! Schema: `quant.a_*` tables. Tushare convention: ts_code (e.g., "000001.SZ").
//! Financial data split into 4 tables (income/balance/cashflow/indicator).

use chrono::{NaiveDate, NaiveDateTime};
use sqlx::FromRow;

// ── a_stock_basic ───────────────────────────────────────────────────────

#[derive(Debug, Clone, FromRow)]
pub struct AStockBasic {
    pub id: i32,
    pub ts_code: String,
    pub name: Option<String>,
    pub area: Option<String>,
    pub industry: Option<String>,
    pub market: Option<String>,
    pub list_date: Option<String>,
    pub list_status: Option<String>,
    pub is_hs: Option<String>,
    pub exchange: Option<String>,
    pub curr_type: Option<String>,
    pub delist_date: Option<String>,
    pub act_name: Option<String>,
    pub act_ent_type: Option<String>,
    pub updated_at: Option<NaiveDateTime>,
}

// ── a_daily_price ───────────────────────────────────────────────────────
// Merged: daily + daily_basic + adj_factor (29 columns)

#[derive(Debug, Clone, FromRow)]
pub struct ADailyPrice {
    pub id: i32,
    pub ts_code: String,
    pub trade_date: NaiveDate,
    pub open: Option<f64>,
    pub high: Option<f64>,
    pub low: Option<f64>,
    pub close: Option<f64>,
    pub pre_close: Option<f64>,
    pub change: Option<f64>,
    pub pct_chg: Option<f64>,
    pub vol: Option<f64>,
    pub amount: Option<f64>,
    pub adj_factor: Option<f64>,
    // daily_basic fields
    pub turnover_rate: Option<f64>,
    pub turnover_rate_f: Option<f64>,
    pub volume_ratio: Option<f64>,
    pub pe: Option<f64>,
    pub pe_ttm: Option<f64>,
    pub pb: Option<f64>,
    pub ps: Option<f64>,
    pub ps_ttm: Option<f64>,
    pub dv_ratio: Option<f64>,
    pub dv_ttm: Option<f64>,
    pub total_share: Option<f64>,
    pub float_share: Option<f64>,
    pub free_share: Option<f64>,
    pub total_mv: Option<f64>,
    pub circ_mv: Option<f64>,
}

// ── a_index_daily ───────────────────────────────────────────────────────

#[derive(Debug, Clone, FromRow)]
pub struct AIndexDaily {
    pub id: i32,
    pub ts_code: String,
    pub trade_date: NaiveDate,
    pub open: Option<f64>,
    pub high: Option<f64>,
    pub low: Option<f64>,
    pub close: Option<f64>,
    pub pre_close: Option<f64>,
    pub change: Option<f64>,
    pub pct_chg: Option<f64>,
    pub vol: Option<f64>,
    pub amount: Option<f64>,
}

// ── a_financial_income (95 columns — key fields only for typed struct) ──

#[derive(Debug, Clone, FromRow)]
pub struct AFinancialIncomeRow {
    pub id: i32,
    pub ts_code: String,
    pub ann_date: Option<String>,
    pub f_ann_date: Option<String>,
    pub end_date: Option<String>,
    pub report_type: Option<String>,
    pub comp_type: Option<String>,
    pub basic_eps: Option<f64>,
    pub diluted_eps: Option<f64>,
    pub total_revenue: Option<f64>,
    pub revenue: Option<f64>,
    pub total_cogs: Option<f64>,
    pub oper_cost: Option<f64>,
    pub sell_exp: Option<f64>,
    pub admin_exp: Option<f64>,
    pub rd_exp: Option<f64>,
    pub operate_profit: Option<f64>,
    pub n_income: Option<f64>,
    pub n_income_attr_p: Option<f64>,
    pub ebit: Option<f64>,
    pub ebitda: Option<f64>,
}

// ── a_financial_indicator (163 columns — key fields) ────────────────────

#[derive(Debug, Clone, FromRow)]
pub struct AFinancialIndicatorRow {
    pub id: i32,
    pub ts_code: String,
    pub ann_date: Option<String>,
    pub end_date: Option<String>,
    pub eps: Option<f64>,
    pub bps: Option<f64>,
    pub roe: Option<f64>,
    pub roe_waa: Option<f64>,
    pub gross_margin: Option<f64>,
    pub netprofit_margin: Option<f64>,
    pub dt_roe: Option<f64>,
    pub roe_yearly: Option<f64>,
    pub roa: Option<f64>,
    pub q_roe: Option<f64>,
    pub q_profit_yoy: Option<f64>,
    pub q_revenue_yoy: Option<f64>,
    pub q_netprofit_yoy: Option<f64>,
    pub profit_dedt: Option<f64>,
    pub current_ratio: Option<f64>,
    pub quick_ratio: Option<f64>,
    pub ocf_to_profit: Option<f64>,
}

// ── a_industry_class ────────────────────────────────────────────────────

#[derive(Debug, Clone, FromRow)]
pub struct AIndustryClass {
    pub id: i32,
    pub ts_code: String,
    pub index_code: Option<String>,
    pub index_name: Option<String>,
    pub industry_name: Option<String>,
    pub src: Option<String>,
    pub level: Option<String>,
    pub in_date: Option<String>,
    pub out_date: Option<String>,
}

// ── a_macro_indicator ───────────────────────────────────────────────────

#[derive(Debug, Clone, FromRow)]
pub struct AMacroIndicator {
    pub id: i32,
    pub indicator: String,
    pub report_date: NaiveDate,
    pub freq: Option<String>,
    pub value: Option<f64>,
}

// ── a_trade_cal ─────────────────────────────────────────────────────────

#[derive(Debug, Clone, FromRow)]
pub struct ATradeCal {
    pub id: i32,
    pub exchange: String,
    pub cal_date: NaiveDate,
    pub is_open: Option<i32>,
    pub pretrade_date: Option<NaiveDate>,
}

// ── a_commodity_price ───────────────────────────────────────────────────

#[derive(Debug, Clone, FromRow)]
pub struct ACommodityPrice {
    pub id: i32,
    pub ts_code: String,
    pub trade_date: NaiveDate,
    pub open: Option<f64>,
    pub high: Option<f64>,
    pub low: Option<f64>,
    pub close: Option<f64>,
    pub pre_close: Option<f64>,
    pub change: Option<f64>,
    pub pct_chg: Option<f64>,
    pub vol: Option<f64>,
    pub amount: Option<f64>,
    pub oi: Option<f64>,
}

// ── a_insider_transaction ───────────────────────────────────────────────

#[derive(Debug, Clone, FromRow)]
pub struct AInsiderTransaction {
    pub id: i32,
    pub ts_code: String,
    pub change_date: Option<NaiveDate>,
    pub holder_name: Option<String>,
    pub holder_type: Option<String>,
    pub change_vol: Option<f64>,
    pub change_amount: Option<f64>,
    pub after_vol: Option<f64>,
    pub after_amount: Option<f64>,
    pub change_reason: Option<String>,
}
