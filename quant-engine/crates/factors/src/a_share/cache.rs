//! A-share data cache — in-memory structures for factor computation.
//!
//! Lighter than US DataCache: A-share daily_price already includes PE/PB/turnover,
//! so we don't need separate key_metric/enterprise_value tables.

use chrono::NaiveDate;
use rustc_hash::FxHashMap;

/// A-share daily bar (includes valuation fields from daily_basic).
#[derive(Debug, Clone)]
pub struct ABar {
    pub open: f64,
    pub high: f64,
    pub low: f64,
    pub close: f64,
    pub pre_close: f64,
    pub pct_chg: f64,
    pub vol: f64,
    pub amount: f64,
    pub adj_factor: f64,
    // Valuation (from daily_basic merge)
    pub turnover_rate: f64,
    pub pe_ttm: f64,
    pub pb: f64,
    pub ps_ttm: f64,
    pub dv_ttm: f64,
    pub total_mv: f64,   // 总市值 (万元)
    pub circ_mv: f64,    // 流通市值 (万元)
}

/// A-share financial indicator record (from fina_indicator table).
#[derive(Debug, Clone)]
pub struct AFinIndicator {
    pub end_date: String,     // YYYYMMDD
    pub ann_date: String,     // YYYYMMDD
    pub eps: f64,
    pub bps: f64,
    pub roe: f64,
    pub gross_margin: f64,
    pub netprofit_margin: f64,
    pub q_profit_yoy: f64,
    pub q_sales_yoy: f64,
    pub q_netprofit_yoy: f64,
    pub current_ratio: f64,
    pub ocf_to_profit: f64,
    pub roa: f64,
    pub quick_ratio: f64,
    pub assets_turn: f64,
    pub debt_to_assets: f64,
}

/// Sector/industry mapping.
#[derive(Debug, Clone)]
pub struct AIndustry {
    pub index_code: String,
    pub industry_name: String,
}

/// Per-stock static info from `a_stock_basic` (used by universe cleaner).
#[derive(Debug, Clone)]
pub struct AStockInfo {
    pub name: String,
    pub list_date: Option<NaiveDate>,
    pub delist_date: Option<NaiveDate>,
    pub is_st: bool,
    pub board: Option<String>,        // 主板 / 创业板 / 科创板 / 北交所
    pub total_share: Option<f64>,     // 万股
    pub free_share: Option<f64>,      // 万股
}

/// Central A-share data cache.
pub struct AShareCache {
    /// Daily prices: ts_code → sorted Vec<(date, ABar)>
    pub daily: FxHashMap<String, Vec<(NaiveDate, ABar)>>,
    /// Financial indicators: ts_code → sorted Vec<AFinIndicator> (by end_date desc)
    pub financials: FxHashMap<String, Vec<AFinIndicator>>,
    /// Industry classification: ts_code → AIndustry
    pub industry: FxHashMap<String, AIndustry>,
    /// Static stock basics: ts_code → AStockInfo
    pub basics: FxHashMap<String, AStockInfo>,
    /// Trading calendar (sorted)
    pub trading_days: Vec<NaiveDate>,
    /// Index daily: index_code → sorted Vec<(date, close)>
    pub index_prices: FxHashMap<String, Vec<(NaiveDate, f64)>>,
    /// All ts_codes
    pub ts_codes: Vec<String>,
}

impl AShareCache {
    /// Get daily bar for a stock on a date.
    pub fn get_bar(&self, ts_code: &str, date: NaiveDate) -> Option<&ABar> {
        let bars = self.daily.get(ts_code)?;
        bars.binary_search_by_key(&date, |(d, _)| *d)
            .ok()
            .map(|i| &bars[i].1)
    }

    /// Get the last N trading days of bars for a stock up to and including date.
    pub fn get_bars_before(&self, ts_code: &str, date: NaiveDate, n: usize) -> Vec<&ABar> {
        let bars = match self.daily.get(ts_code) { Some(b) => b, None => return vec![] };
        let end = bars.partition_point(|(d, _)| *d <= date);
        let start = if end >= n { end - n } else { 0 };
        bars[start..end].iter().map(|(_, b)| b).collect()
    }

    /// Get latest financial indicator on or before a date.
    pub fn get_latest_fin(&self, ts_code: &str, date: NaiveDate) -> Option<&AFinIndicator> {
        let fins = self.financials.get(ts_code)?;
        let date_str = date.format("%Y%m%d").to_string();
        // Find first record where ann_date <= date
        fins.iter().find(|f| f.ann_date <= date_str)
    }

    /// Get N most recent financial indicators before date.
    pub fn get_fin_history(&self, ts_code: &str, date: NaiveDate, n: usize) -> Vec<&AFinIndicator> {
        let fins = match self.financials.get(ts_code) { Some(f) => f, None => return vec![] };
        let date_str = date.format("%Y%m%d").to_string();
        fins.iter().filter(|f| f.ann_date <= date_str).take(n).collect()
    }

    /// Get total market value for a stock on date (万元 → 元).
    pub fn get_market_cap(&self, ts_code: &str, date: NaiveDate) -> Option<f64> {
        self.get_bar(ts_code, date).map(|b| b.total_mv * 10_000.0)
    }

    /// Get active ts_codes on a date (have price data).
    pub fn active_codes_on(&self, date: NaiveDate) -> Vec<&str> {
        self.daily.iter()
            .filter(|(_, bars)| bars.binary_search_by_key(&date, |(d, _)| *d).is_ok())
            .map(|(code, _)| code.as_str())
            .collect()
    }

    /// True if `ts_code` is currently flagged ST/*ST in basics. False if no basics row.
    pub fn is_st(&self, ts_code: &str) -> bool {
        self.basics.get(ts_code).is_some_and(|b| b.is_st)
    }

    /// True if a stock is listed on `date` (list_date <= date && (delist_date.is_none() || delist_date > date)).
    pub fn is_listed_on(&self, ts_code: &str, date: NaiveDate) -> bool {
        let info = match self.basics.get(ts_code) { Some(b) => b, None => return false };
        let listed = info.list_date.is_some_and(|d| d <= date);
        let active = info.delist_date.map_or(true, |d| d > date);
        listed && active
    }

    /// Days since IPO on `date` (None if no list_date or not yet listed).
    pub fn listing_days_on(&self, ts_code: &str, date: NaiveDate) -> Option<i64> {
        let ld = self.basics.get(ts_code)?.list_date?;
        if ld > date { return None; }
        Some((date - ld).num_days())
    }

    /// Average `amount` over the last `n` trading days up to and including `date`.
    /// Returns NaN if no bars in window.
    pub fn avg_amount(&self, ts_code: &str, date: NaiveDate, n: usize) -> f64 {
        let bars = self.get_bars_before(ts_code, date, n);
        if bars.is_empty() { return f64::NAN; }
        let sum: f64 = bars.iter().map(|b| b.amount).sum();
        sum / bars.len() as f64
    }
}
