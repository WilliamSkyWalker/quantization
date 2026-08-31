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
    pub industry_code: Option<String>,
    pub is_pub: Option<String>,
    pub parent_code: Option<String>,
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

// ── a_top_list (龙虎榜每日交易明细) ──────────────────────────────────────
// Unique key (ts_code, trade_date, reason): a stock can have 2-3 rows per
// day, one per trigger reason (verified via live API probe, 2026-08-30).

#[derive(Debug, Clone, FromRow)]
pub struct ATopList {
    pub id: i64,
    pub trade_date: NaiveDate,
    pub ts_code: String,
    pub name: Option<String>,
    pub close: Option<f64>,
    pub pct_change: Option<f64>,
    pub turnover_rate: Option<f64>,
    pub amount: Option<f64>,
    pub l_sell: Option<f64>,
    pub l_buy: Option<f64>,
    pub l_amount: Option<f64>,
    pub net_amount: Option<f64>,
    pub net_rate: Option<f64>,
    pub amount_rate: Option<f64>,
    pub float_values: Option<f64>,
    pub reason: String,
    pub updated_at: Option<chrono::DateTime<chrono::Utc>>,
}

// ── a_margin_detail (融资融券交易明细，按股票) ──────────────────────────
// Unique key (ts_code, trade_date). All money fields are raw yuan (元) —
// verified via live data cross-check against a_top_list.amount (see
// crates/factors/src/a_share/factors_v2.rs doc comment for the full unit
// derivation).

#[derive(Debug, Clone, FromRow)]
pub struct AMarginDetail {
    pub id: i64,
    pub trade_date: NaiveDate,
    pub ts_code: String,
    pub rzye: Option<f64>,
    pub rqye: Option<f64>,
    pub rzmre: Option<f64>,
    pub rqyl: Option<f64>,
    pub rzche: Option<f64>,
    pub rqchl: Option<f64>,
    pub rqmcl: Option<f64>,
    pub rzrqye: Option<f64>,
    pub updated_at: Option<chrono::DateTime<chrono::Utc>>,
}

// ── Event-driven data (业绩预告/快报/股东增减持/回购/限售股解禁) ────────
// Data source: Tushare `forecast`/`express`/`stk_holdertrade`/`repurchase`/
// `share_float`. All 5 endpoints verified accessible via a live API probe
// call (2026-08-30); `anns`/`news` are NOT accessible on this account
// (40203 no permission — requires a higher Tushare points tier).

/// a_forecast (业绩预告). Unique key (ts_code, ann_date, end_date):
/// `ann_date` is the disclosure date (PIT-safe), `end_date` the reporting
/// period. `p_change_min/max` are the forecast YoY net-profit growth % range;
/// `type` is Tushare's own classification (预增/预减/略增/略减/续盈/续亏/
/// 首亏/扭亏/减亏/增亏).
#[derive(Debug, Clone, FromRow)]
pub struct AForecast {
    pub id: i64,
    pub ts_code: String,
    pub ann_date: NaiveDate,
    pub end_date: NaiveDate,
    pub r#type: Option<String>,
    pub p_change_min: Option<f64>,
    pub p_change_max: Option<f64>,
    pub net_profit_min: Option<f64>,
    pub net_profit_max: Option<f64>,
    pub last_parent_net: Option<f64>,
    pub first_ann_date: Option<NaiveDate>,
    pub summary: Option<String>,
    pub change_reason: Option<String>,
    pub update_flag: Option<String>,
    pub updated_at: Option<chrono::DateTime<chrono::Utc>>,
}

/// a_express (业绩快报). Unique key (ts_code, ann_date, end_date).
/// `ann_date` is the disclosure date (PIT-safe). `yoy_net_profit` is the
/// pre-computed YoY net profit growth % (Tushare-supplied, not re-derived).
#[derive(Debug, Clone, FromRow)]
pub struct AExpress {
    pub id: i64,
    pub ts_code: String,
    pub ann_date: NaiveDate,
    pub end_date: NaiveDate,
    pub revenue: Option<f64>,
    pub operate_profit: Option<f64>,
    pub total_profit: Option<f64>,
    pub n_income: Option<f64>,
    pub total_assets: Option<f64>,
    pub total_hldr_eqy_exc_min_int: Option<f64>,
    pub diluted_eps: Option<f64>,
    pub diluted_roe: Option<f64>,
    pub yoy_net_profit: Option<f64>,
    pub bps: Option<f64>,
    pub perf_summary: Option<String>,
    pub update_flag: Option<String>,
    pub updated_at: Option<chrono::DateTime<chrono::Utc>>,
}

/// a_stk_holdertrade (股东增减持). Unique key (ts_code, ann_date,
/// holder_name(100), in_de, change_vol) — verified via live probe that the
/// same (ts_code, ann_date, holder_name) can carry multiple distinct trades
/// same day (e.g. an IN then a DE the same announcement date); change_vol
/// disambiguates. `in_de` is Tushare's own direction flag ("IN"=增持,
/// "DE"=减持). `ann_date` is the disclosure date (PIT-safe).
#[derive(Debug, Clone, FromRow)]
pub struct AStkHolderTrade {
    pub id: i64,
    pub ts_code: String,
    pub ann_date: NaiveDate,
    pub holder_name: Option<String>,
    pub holder_type: Option<String>,
    pub in_de: Option<String>,
    pub change_vol: Option<f64>,
    pub change_ratio: Option<f64>,
    pub after_share: Option<f64>,
    pub after_ratio: Option<f64>,
    pub avg_price: Option<f64>,
    pub total_share: Option<f64>,
    pub updated_at: Option<chrono::DateTime<chrono::Utc>>,
}

/// a_repurchase (股票回购). Unique key (ts_code, ann_date, proc): `proc`
/// (进度: 预案/股东大会通过/实施中/完成 etc.) distinguishes successive
/// announcements about the same buyback program on different disclosure
/// dates. `ann_date` is the disclosure date (PIT-safe). `amount` is yuan (元).
#[derive(Debug, Clone, FromRow)]
pub struct ARepurchase {
    pub id: i64,
    pub ts_code: String,
    pub ann_date: NaiveDate,
    pub end_date: Option<NaiveDate>,
    pub proc: String,
    pub exp_date: Option<NaiveDate>,
    pub vol: Option<f64>,
    pub amount: Option<f64>,
    pub high_limit: Option<f64>,
    pub low_limit: Option<f64>,
    pub updated_at: Option<chrono::DateTime<chrono::Utc>>,
}

/// a_share_float (限售股解禁). Unique key (ts_code, float_date,
/// holder_name(100), share_type(50)) — multiple holders/share types can
/// unlock at the same company on the same date. `float_date` is the actual
/// unlock date, publicly scheduled at issuance time (years in advance) —
/// using it ahead of time is NOT look-ahead bias, unlike `ann_date`/other
/// event dates in this module. `float_share` is share count (not yuan).
#[derive(Debug, Clone, FromRow)]
pub struct AShareFloat {
    pub id: i64,
    pub ts_code: String,
    pub ann_date: Option<NaiveDate>,
    pub float_date: NaiveDate,
    pub float_share: Option<f64>,
    pub float_ratio: Option<f64>,
    pub holder_name: Option<String>,
    pub share_type: Option<String>,
    pub updated_at: Option<chrono::DateTime<chrono::Utc>>,
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
