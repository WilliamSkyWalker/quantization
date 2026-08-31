//! A-share clean universe filter — port of Python `a_cleaner.get_clean_universe`.
//!
//! Filters applied (in order, all configurable):
//!   1. Listed on date (list_date <= date && (delist_date is None || delist_date > date))
//!   2. Listed at least N days (IPO age cutoff)
//!   3. Not ST/*ST (configurable)
//!   4. Board exclusions (STAR / ChiNext / BSE — configurable)
//!   5. Has price on date && volume > 0 (suspension filter)
//!   6. Total market value >= min_market_cap (from ABar.total_mv 万元 → 元)
//!   7. Rolling N-day average daily turnover >= min_daily_turnover

use chrono::NaiveDate;
use rustc_hash::FxHashSet;
use tracing::debug;

use quant_core::board::{Board, board_from_ts_code};
use quant_core::config::AShareUniverseConfig;

use crate::a_share::cache::AShareCache;

/// Number of trading days to average daily turnover over.
pub const DEFAULT_TURNOVER_LOOKBACK: usize = 20;

/// Filter knob bundle for `get_a_clean_universe`.
#[derive(Debug, Clone)]
pub struct AUniverseFilter {
    pub min_market_cap: f64,
    pub min_daily_turnover: f64,
    pub min_listing_days: i32,
    pub turnover_lookback: usize,
    pub exclude_st: bool,
    pub exclude_star_market: bool,
    pub exclude_chinext: bool,
    pub exclude_bse: bool,
}

impl Default for AUniverseFilter {
    fn default() -> Self {
        let cfg = AShareUniverseConfig::default();
        Self::from_config(&cfg)
    }
}

impl AUniverseFilter {
    /// Build a filter from the A-share universe config block.
    pub fn from_config(cfg: &AShareUniverseConfig) -> Self {
        Self {
            min_market_cap: cfg.min_market_cap,
            min_daily_turnover: cfg.min_daily_turnover,
            min_listing_days: cfg.min_listing_days,
            turnover_lookback: DEFAULT_TURNOVER_LOOKBACK,
            exclude_st: cfg.exclude_st,
            exclude_star_market: cfg.exclude_star_market,
            exclude_chinext: cfg.exclude_chinext,
            exclude_bse: cfg.exclude_bse,
        }
    }
}

/// Get the clean tradeable A-share universe for a given date.
///
/// Returns `ts_code` strings that pass all filters. Caller can intersect with
/// other constraints (e.g. industry whitelist, factor coverage) downstream.
pub fn get_a_clean_universe(
    date: NaiveDate,
    cache: &AShareCache,
    filter: &AUniverseFilter,
) -> FxHashSet<String> {
    let mut universe = FxHashSet::default();
    let mut stats = FilterStats::default();

    for code in &cache.ts_codes {
        stats.total += 1;

        // Need a basics row to evaluate listing / ST flags.
        let info = match cache.basics.get(code) {
            Some(b) => b,
            None => { stats.no_basics += 1; continue; }
        };

        // 1+2. Listing window.
        if !cache.is_listed_on(code, date) {
            stats.delisted_or_unlisted += 1; continue;
        }
        match cache.listing_days_on(code, date) {
            Some(d) if d >= filter.min_listing_days as i64 => {}
            _ => { stats.too_new += 1; continue; }
        }

        // 3. ST filter.
        if filter.exclude_st && info.is_st {
            stats.st_filtered += 1; continue;
        }

        // 4. Board exclusions.
        let board = board_from_ts_code(code);
        let blocked_board = match board {
            Board::StarMarket => filter.exclude_star_market,
            Board::ChiNext => filter.exclude_chinext,
            Board::Bse => filter.exclude_bse,
            Board::Main => false,
        };
        if blocked_board {
            stats.board_filtered += 1; continue;
        }

        // 5. Has price + traded today (volume > 0).
        let bar = match cache.get_bar(code, date) {
            Some(b) => b,
            None => { stats.no_price += 1; continue; }
        };
        if !bar.vol.is_finite() || bar.vol <= 0.0 {
            stats.suspended += 1; continue;
        }
        if !bar.close.is_finite() || bar.close <= 0.0 {
            stats.no_price += 1; continue;
        }

        // 6. Market cap (total_mv is in 万元; convert to 元 to match config units).
        let mktcap_yuan = bar.total_mv * 10_000.0;
        if !mktcap_yuan.is_finite() || mktcap_yuan < filter.min_market_cap {
            stats.too_small += 1; continue;
        }

        // 7. Liquidity (rolling avg amount, 千元 → 元).
        if filter.min_daily_turnover > 0.0 {
            let avg = cache.avg_amount(code, date, filter.turnover_lookback);
            let avg_yuan = avg * 1_000.0;
            if !avg_yuan.is_finite() || avg_yuan < filter.min_daily_turnover {
                stats.illiquid += 1; continue;
            }
        }

        universe.insert(code.clone());
    }

    debug!(
        "A-share universe on {date}: {pass}/{total} \
         (no_basics={no_basics} unlisted={unlisted} new={new} st={st} \
         board={board} no_price={np} susp={susp} small={small} illiq={illiq})",
        pass = universe.len(), total = stats.total,
        no_basics = stats.no_basics, unlisted = stats.delisted_or_unlisted,
        new = stats.too_new, st = stats.st_filtered, board = stats.board_filtered,
        np = stats.no_price, susp = stats.suspended, small = stats.too_small,
        illiq = stats.illiquid,
    );

    universe
}

#[derive(Debug, Default)]
struct FilterStats {
    total: usize,
    no_basics: usize,
    delisted_or_unlisted: usize,
    too_new: usize,
    st_filtered: usize,
    board_filtered: usize,
    no_price: usize,
    suspended: usize,
    too_small: usize,
    illiquid: usize,
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::a_share::cache::{ABar, AStockInfo};
    use rustc_hash::FxHashMap;

    fn empty_cache() -> AShareCache {
        AShareCache {
            daily: FxHashMap::default(),
            financials: FxHashMap::default(),
            industry: FxHashMap::default(),
            basics: FxHashMap::default(),
            trading_days: vec![],
            index_prices: FxHashMap::default(),
            ts_codes: vec![],
            top_list: FxHashMap::default(),
            margin_detail: FxHashMap::default(),
        }
    }

    fn add_stock(
        cache: &mut AShareCache,
        code: &str,
        list_date: Option<NaiveDate>,
        delist_date: Option<NaiveDate>,
        is_st: bool,
        bars: Vec<(NaiveDate, ABar)>,
    ) {
        cache.ts_codes.push(code.to_string());
        cache.basics.insert(code.to_string(), AStockInfo {
            name: code.to_string(),
            list_date, delist_date, is_st,
            board: None, total_share: None, free_share: None,
        });
        cache.daily.insert(code.to_string(), bars);
    }

    fn bar(amount: f64, vol: f64, close: f64, total_mv_wan: f64) -> ABar {
        ABar {
            open: close, high: close, low: close, close, pre_close: close, pct_chg: 0.0,
            vol, amount, adj_factor: 1.0,
            turnover_rate: 0.0, pe_ttm: 0.0, pb: 0.0, ps_ttm: 0.0, dv_ttm: 0.0,
            total_mv: total_mv_wan, circ_mv: total_mv_wan,
        }
    }

    fn d(s: &str) -> NaiveDate {
        NaiveDate::parse_from_str(s, "%Y-%m-%d").unwrap()
    }

    fn make_bars(start: &str, n: usize, amount: f64, total_mv_wan: f64) -> Vec<(NaiveDate, ABar)> {
        let s = d(start);
        (0..n).map(|i| (s + chrono::Duration::days(i as i64), bar(amount, 1000.0, 10.0, total_mv_wan))).collect()
    }

    #[test]
    fn excludes_delisted() {
        let mut c = empty_cache();
        add_stock(&mut c, "000001.SZ",
            Some(d("2020-01-01")), Some(d("2024-06-01")), false,
            make_bars("2024-07-01", 30, 1e9, 5e5));
        let f = AUniverseFilter::default();
        let u = get_a_clean_universe(d("2024-07-15"), &c, &f);
        assert!(u.is_empty(), "delisted stock should be filtered");
    }

    #[test]
    fn excludes_too_new_ipo() {
        let mut c = empty_cache();
        add_stock(&mut c, "000002.SZ",
            Some(d("2024-06-01")), None, false,
            make_bars("2024-06-01", 60, 1e9, 5e5));
        let f = AUniverseFilter::default(); // 180 days
        // 2024-08-01 is < 180 days from 2024-06-01
        let u = get_a_clean_universe(d("2024-08-01"), &c, &f);
        assert!(u.is_empty(), "new IPO should be filtered");
    }

    #[test]
    fn excludes_st_when_flag_on() {
        let mut c = empty_cache();
        add_stock(&mut c, "000003.SZ",
            Some(d("2010-01-01")), None, true,
            make_bars("2024-07-01", 30, 1e9, 5e5));
        let f = AUniverseFilter::default(); // exclude_st = true
        let u = get_a_clean_universe(d("2024-07-15"), &c, &f);
        assert!(u.is_empty(), "ST stock should be filtered");

        let mut f2 = f.clone();
        f2.exclude_st = false;
        let u2 = get_a_clean_universe(d("2024-07-15"), &c, &f2);
        assert_eq!(u2.len(), 1, "ST should pass when exclude_st=false");
    }

    #[test]
    fn excludes_suspended_zero_volume() {
        let mut c = empty_cache();
        let mut bars = make_bars("2024-07-01", 30, 1e9, 5e5);
        // zero out volume on the target day
        let target = d("2024-07-15");
        for (date, b) in bars.iter_mut() {
            if *date == target { b.vol = 0.0; b.amount = 0.0; }
        }
        add_stock(&mut c, "000004.SZ",
            Some(d("2010-01-01")), None, false, bars);
        let f = AUniverseFilter::default();
        let u = get_a_clean_universe(target, &c, &f);
        assert!(u.is_empty(), "suspended (vol=0) should be filtered");
    }

    #[test]
    fn excludes_microcap() {
        let mut c = empty_cache();
        // total_mv 万元 = 1e4, so 元 = 1e8 < 3e9 default min_market_cap
        add_stock(&mut c, "000005.SZ",
            Some(d("2010-01-01")), None, false,
            make_bars("2024-07-01", 30, 1e9, 1e4));
        let f = AUniverseFilter::default();
        let u = get_a_clean_universe(d("2024-07-15"), &c, &f);
        assert!(u.is_empty(), "micro-cap should be filtered");
    }

    #[test]
    fn excludes_illiquid() {
        let mut c = empty_cache();
        // amount is stored in 千元: 1e3 means 1e6 元/天, below the 5e7 threshold.
        add_stock(&mut c, "000006.SZ",
            Some(d("2010-01-01")), None, false,
            make_bars("2024-07-01", 30, 1e3, 5e5));
        let f = AUniverseFilter::default();
        let u = get_a_clean_universe(d("2024-07-15"), &c, &f);
        assert!(u.is_empty(), "illiquid stock should be filtered");
    }

    #[test]
    fn includes_clean_main_board() {
        let mut c = empty_cache();
        // Default config: min_mcap 3e9 元, min_turnover 5e7 元
        // total_mv 万元 = 5e5 → 元 = 5e9 ✓; amount 1e8 元/天 ✓
        add_stock(&mut c, "600519.SH",
            Some(d("2010-01-01")), None, false,
            make_bars("2024-07-01", 30, 1e8, 5e5));
        let f = AUniverseFilter::default();
        let u = get_a_clean_universe(d("2024-07-15"), &c, &f);
        assert_eq!(u.len(), 1);
        assert!(u.contains("600519.SH"));
    }

    #[test]
    fn board_exclusion_chinext() {
        let mut c = empty_cache();
        add_stock(&mut c, "300750.SZ",
            Some(d("2010-01-01")), None, false,
            make_bars("2024-07-01", 30, 1e8, 5e5));
        let mut f = AUniverseFilter::default();
        f.exclude_chinext = true;
        let u = get_a_clean_universe(d("2024-07-15"), &c, &f);
        assert!(u.is_empty(), "chinext should be filtered when exclude_chinext=true");

        f.exclude_chinext = false;
        let u2 = get_a_clean_universe(d("2024-07-15"), &c, &f);
        assert_eq!(u2.len(), 1);
    }
}
