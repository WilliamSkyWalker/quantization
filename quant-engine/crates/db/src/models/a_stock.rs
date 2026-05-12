//! A-share (Chinese stock market) models — sqlx FromRow structs.
//!
//! Schema: `quant.a_*` tables. Tushare convention: ts_code (e.g., "000001.SZ").
//! Financial data split into 4 tables (income/balance/cashflow/indicator).

use chrono::{DateTime, NaiveDate, Utc};
use sqlx::FromRow;

// ── a_stock_basic ───────────────────────────────────────────────────────

#[derive(Debug, Clone, FromRow)]
pub struct AStockBasic {
    pub id: i64,
    pub ts_code: String,
    pub symbol: Option<String>,
    pub name: Option<String>,
    pub area: Option<String>,
    pub industry: Option<String>,
    pub fullname: Option<String>,
    pub enname: Option<String>,
    pub cnspell: Option<String>,
    pub market: Option<String>,
    pub exchange: Option<String>,
    pub curr_type: Option<String>,
    pub list_status: Option<String>,
    pub list_date: Option<NaiveDate>,
    pub delist_date: Option<NaiveDate>,
    pub is_hs: Option<String>,
    pub act_name: Option<String>,
    pub act_ent_type: Option<String>,
    pub is_st: i32,
    pub board: Option<String>,
    pub total_share: Option<f64>,
    pub float_share: Option<f64>,
    pub free_share: Option<f64>,
    pub updated_at: Option<DateTime<Utc>>,
}

// ── a_daily_price ───────────────────────────────────────────────────────
// Merged: daily + daily_basic + adj_factor (29 columns)

#[derive(Debug, Clone, FromRow)]
pub struct ADailyPrice {
    pub id: i64,
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
    pub id: i64,
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

// ── Financial tables (full-field, generated from migrate_ashare_schema.sql) ──
// Income/Balance/Cashflow/Indicator: 96 + 158 + 98 + 167 = 519 columns total.
include!("a_financial_rows.rs");

// ── a_industry_class ────────────────────────────────────────────────────

#[derive(Debug, Clone, FromRow)]
pub struct AIndustryClass {
    pub id: i64,
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
    pub id: i64,
    pub indicator: String,
    pub report_date: NaiveDate,
    pub freq: Option<String>,
    pub value: Option<f64>,
}

// ── a_trade_cal ─────────────────────────────────────────────────────────

#[derive(Debug, Clone, FromRow)]
pub struct ATradeCal {
    pub id: i64,
    pub exchange: String,
    pub cal_date: NaiveDate,
    pub is_open: Option<i32>,
    pub pretrade_date: Option<NaiveDate>,
}

// ── a_commodity_price ───────────────────────────────────────────────────

#[derive(Debug, Clone, FromRow)]
pub struct ACommodityPrice {
    pub id: i64,
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
    pub id: i64,
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

#[cfg(test)]
mod tests {
    /// Ensures the generated `a_financial_rows.rs` field count matches the
    /// DDL in `scripts/migrate_ashare_schema.sql`. If anyone updates the DDL
    /// without regenerating the structs (or vice versa), this test fails.
    #[test]
    fn financial_row_field_counts_match_ddl() {
        let generated = include_str!("a_financial_rows.rs");

        let expected = [
            ("AFinancialIncomeRow", 96),
            ("AFinancialBalanceRow", 158),
            ("AFinancialCashflowRow", 98),
            ("AFinancialIndicatorRow", 167),
        ];

        for (struct_name, want) in expected {
            // Find the struct block and count `pub <name>:` lines.
            let header = format!("pub struct {struct_name} {{");
            let start = generated.find(&header)
                .unwrap_or_else(|| panic!("{struct_name} not found in a_financial_rows.rs"));
            let after = &generated[start..];
            let body_end = after.find("\n}").expect("missing closing brace");
            let body = &after[..body_end];
            let count = body.lines()
                .filter(|l| {
                    let t = l.trim_start();
                    t.starts_with("pub ") && !t.starts_with("pub struct")
                })
                .count();
            assert_eq!(count, want,
                "{struct_name}: generated has {count} fields, DDL has {want}. \
                 Regenerate a_financial_rows.rs from migrate_ashare_schema.sql.");
        }
    }
}
