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
    pub netprofit_yoy: f64,
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
    /// Membership start date, inclusive. `None` is treated as unbounded.
    pub in_date: Option<NaiveDate>,
    /// Membership end date, inclusive. `None` means the membership remains active.
    pub out_date: Option<NaiveDate>,
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

/// One trading day's aggregated Dragon-Tiger-List (龙虎榜) activity for a
/// stock. Tushare can emit 2-3 rows per stock per day (one per trigger
/// `reason`); this is the SUM of net_amount/l_buy/l_sell/amount across all
/// of that day's reasons, and the amount-weighted mean of `net_rate` (since
/// net_rate is already a ratio — summing ratios across reasons would be
/// wrong, so it's weighted by each row's own `amount`).
#[derive(Debug, Clone)]
pub struct ALhbDay {
    pub net_amount: f64,
    pub l_buy: f64,
    pub l_sell: f64,
    pub amount: f64,    // sum of that day's per-reason `amount` fields
    pub net_rate: f64,  // amount-weighted mean of net_rate across the day's rows
}

/// One trading day's margin (融资融券) detail for a stock. All values raw
/// yuan (元) — see `crates/factors/src/a_share/factors_v2.rs` for the unit
/// derivation vs. `ABar::amount` (thousand yuan).
#[derive(Debug, Clone)]
pub struct AMarginDay {
    pub rzye: f64,   // 融资余额
    pub rzmre: f64,  // 今日融资买入额
}

/// Central A-share data cache.
pub struct AShareCache {
    /// Daily prices: ts_code → sorted Vec<(date, ABar)>
    pub daily: FxHashMap<String, Vec<(NaiveDate, ABar)>>,
    /// Financial indicators: ts_code → sorted Vec<AFinIndicator> (by end_date desc)
    pub financials: FxHashMap<String, Vec<AFinIndicator>>,
    /// Point-in-time industry classifications: ts_code → membership intervals.
    pub industry: FxHashMap<String, Vec<AIndustry>>,
    /// Static stock basics: ts_code → AStockInfo
    pub basics: FxHashMap<String, AStockInfo>,
    /// Trading calendar (sorted)
    pub trading_days: Vec<NaiveDate>,
    /// Index daily: index_code → sorted Vec<(date, close)>
    pub index_prices: FxHashMap<String, Vec<(NaiveDate, f64)>>,
    /// All ts_codes
    pub ts_codes: Vec<String>,
    /// Dragon-Tiger-List (龙虎榜) daily aggregates: ts_code → sorted Vec<(date, ALhbDay)>.
    pub top_list: FxHashMap<String, Vec<(NaiveDate, ALhbDay)>>,
    /// Margin trading detail: ts_code → sorted Vec<(date, AMarginDay)>.
    pub margin_detail: FxHashMap<String, Vec<(NaiveDate, AMarginDay)>>,
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

    /// Return the L1 industry membership effective on `date`.
    ///
    /// Historical records can overlap during schema migrations. In that case,
    /// the classification with the latest effective start date takes priority.
    pub fn industry_on(&self, ts_code: &str, date: NaiveDate) -> Option<&AIndustry> {
        self.industry.get(ts_code)?
            .iter()
            .filter(|membership| {
                !membership.industry_name.trim().is_empty()
                    && membership.in_date.is_none_or(|start| start <= date)
                    && membership.out_date.is_none_or(|end| date <= end)
            })
            .max_by_key(|membership| membership.in_date)
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

    /// Last `n` Dragon-Tiger-List (龙虎榜) entries for `ts_code` on or before
    /// `date`. Unlike `get_bars_before`, this is NOT a trading-day-count
    /// window — LHB appearance is sparse (most days have zero rows for a
    /// given stock), so this returns the last `n` entries that *exist*,
    /// however far back in calendar time they fall.
    pub fn lhb_days_before(&self, ts_code: &str, date: NaiveDate, n: usize) -> Vec<(NaiveDate, &ALhbDay)> {
        let days = match self.top_list.get(ts_code) { Some(d) => d, None => return vec![] };
        let end = days.partition_point(|(d, _)| *d <= date);
        let start = if end >= n { end - n } else { 0 };
        days[start..end].iter().map(|(d, v)| (*d, v)).collect()
    }

    /// Count of distinct trading days (per `trading_days`, not calendar days)
    /// in the trailing `n` trading days ending at `date` (inclusive) on
    /// which `ts_code` has at least one Dragon-Tiger-List entry.
    pub fn lhb_appearance_count(&self, ts_code: &str, date: NaiveDate, n: usize) -> usize {
        let end = self.trading_days.partition_point(|d| *d <= date);
        let start = if end >= n { end - n } else { 0 };
        if start >= end { return 0; }
        let window_start = self.trading_days[start];
        let window_end = self.trading_days[end - 1];

        match self.top_list.get(ts_code) {
            Some(days) => days.iter()
                .filter(|(d, _)| *d >= window_start && *d <= window_end)
                .count(),
            None => 0,
        }
    }

    /// Last `n` margin-detail (融资融券) entries for `ts_code` on or before
    /// `date`. Margin detail has ~1 row per trading day per stock, so this
    /// mirrors `get_bars_before`'s trading-day-count windowing semantics.
    pub fn margin_days_before(&self, ts_code: &str, date: NaiveDate, n: usize) -> Vec<(NaiveDate, &AMarginDay)> {
        let days = match self.margin_detail.get(ts_code) { Some(d) => d, None => return vec![] };
        let end = days.partition_point(|(d, _)| *d <= date);
        let start = if end >= n { end - n } else { 0 };
        days[start..end].iter().map(|(d, v)| (*d, v)).collect()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn industry_memberships_are_resolved_point_in_time() {
        let code = "000001.SZ".to_string();
        let old_end = NaiveDate::from_ymd_opt(2021, 6, 15).unwrap();
        let new_start = NaiveDate::from_ymd_opt(2021, 6, 16).unwrap();
        let mut industry = FxHashMap::default();
        industry.insert(code.clone(), vec![
            AIndustry {
                index_code: "801010.SI".into(),
                industry_name: "旧行业".into(),
                in_date: Some(NaiveDate::from_ymd_opt(2014, 1, 1).unwrap()),
                out_date: Some(old_end),
            },
            AIndustry {
                index_code: "801020.SI".into(),
                industry_name: "新行业".into(),
                in_date: Some(new_start),
                out_date: None,
            },
        ]);
        let cache = AShareCache {
            daily: FxHashMap::default(),
            financials: FxHashMap::default(),
            industry,
            basics: FxHashMap::default(),
            trading_days: Vec::new(),
            index_prices: FxHashMap::default(),
            ts_codes: Vec::new(),
            top_list: FxHashMap::default(),
            margin_detail: FxHashMap::default(),
        };

        assert_eq!(
            cache.industry_on(&code, old_end).map(|industry| industry.industry_name.as_str()),
            Some("旧行业"),
        );
        assert_eq!(
            cache.industry_on(&code, new_start).map(|industry| industry.industry_name.as_str()),
            Some("新行业"),
        );
    }

    #[test]
    fn industry_membership_without_name_is_unknown() {
        let code = "000001.SZ".to_string();
        let mut industry = FxHashMap::default();
        industry.insert(code.clone(), vec![AIndustry {
            index_code: "801010.SI".into(),
            industry_name: String::new(),
            in_date: None,
            out_date: None,
        }]);
        let cache = AShareCache {
            daily: FxHashMap::default(),
            financials: FxHashMap::default(),
            industry,
            basics: FxHashMap::default(),
            trading_days: Vec::new(),
            index_prices: FxHashMap::default(),
            ts_codes: Vec::new(),
            top_list: FxHashMap::default(),
            margin_detail: FxHashMap::default(),
        };

        assert!(cache.industry_on(&code, NaiveDate::from_ymd_opt(2024, 1, 1).unwrap()).is_none());
    }
}
