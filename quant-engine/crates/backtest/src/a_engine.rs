//! A-share backtest engine — T+1 execution with Chinese market rules.
//!
//! Key differences from US engine:
//! - T+1: signals trigger next-day open execution (not same-day close)
//! - Lot size: 100 shares (整手)
//! - Stamp tax: 0.1% on sell side only
//! - Limit up/down: per-board (主板 ±10% / 创业板·科创板 ±20% / 北交所 ±30% / ST ±5%)
//! - Commission minimum: 5 CNY per trade

use std::collections::BTreeMap;

use chrono::NaiveDate;
use rustc_hash::FxHashMap;
use tracing::{debug, info};

pub use quant_core::board::{Board, board_from_ts_code, limit_pct_for};
use quant_core::config::{AShareConfig, AShareMarketRulesConfig, AShareUniverseConfig};
use quant_factors::a_share::cache::{AShareCache, ABar};
use quant_factors::a_share::universe::{AUniverseFilter, get_a_clean_universe};

/// A-share execution cost parameters.
#[derive(Debug, Clone)]
pub struct ACostConfig {
    pub buy_commission: f64,
    pub sell_commission: f64,
    pub stamp_tax: f64,
    pub slippage: f64,
    pub initial_capital: f64,
    pub min_commission: f64,
    pub lot_size: i64,
    pub market_rules: AShareMarketRulesConfig,
}

impl Default for ACostConfig {
    fn default() -> Self {
        Self::from_a_share(&AShareConfig::default())
    }
}

impl ACostConfig {
    /// Build from a top-level A-share config block.
    pub fn from_a_share(a: &AShareConfig) -> Self {
        Self {
            buy_commission: a.execution.buy_commission,
            sell_commission: a.execution.sell_commission,
            stamp_tax: a.execution.stamp_tax,
            slippage: a.execution.slippage,
            initial_capital: a.execution.initial_capital,
            min_commission: a.execution.min_commission,
            lot_size: a.execution.lot_size,
            market_rules: a.market_rules.clone(),
        }
    }
}

/// A-share backtest result.
pub struct ABacktestResult {
    pub nav: Vec<(NaiveDate, f64)>,
    pub benchmark_nav: Vec<(NaiveDate, f64)>,
    pub total_return: f64,
    pub annual_return: f64,
    pub annual_volatility: f64,
    pub sharpe_ratio: f64,
    pub max_drawdown: f64,
    pub calmar_ratio: f64,
    pub win_rate: f64,
    pub total_trades: usize,
    pub annual_turnover: f64,
}

/// Portfolio signal: ts_code → target weight.
pub type APortfolioSignal = FxHashMap<String, f64>;

/// Run A-share backtest.
///
/// `signals`: date → {ts_code: weight}. Must be sorted by date.
/// T+1 execution: signal on date D triggers trades at open on date D+1.
///
/// `universe_cfg`: when `Some`, on each rebalance day the engine recomputes
/// the previous trading day's clean universe (avoids same-day close lookahead) and
/// drops any signal weights pointing to codes outside it. Held positions
/// already get force-liquidated when they fall out of `target_weights`
/// (handled by `a_exec::plan_orders`), so this filter only blocks new
/// purchases of newly-untradable names. Pass `None` to keep legacy behavior.
pub fn run_backtest(
    signals: &BTreeMap<NaiveDate, APortfolioSignal>,
    cache: &AShareCache,
    config: &ACostConfig,
    benchmark_code: &str,
    universe_cfg: Option<&AShareUniverseConfig>,
) -> ABacktestResult {
    let trading_days = &cache.trading_days;
    if trading_days.is_empty() || signals.is_empty() {
        return empty_result();
    }

    let _start = *trading_days.first().unwrap();
    let _end = *trading_days.last().unwrap();

    let mut cash = config.initial_capital;
    let mut positions: FxHashMap<String, i64> = FxHashMap::default(); // ts_code → shares
    let mut nav_series: Vec<(NaiveDate, f64)> = Vec::new();
    let mut benchmark_nav: Vec<(NaiveDate, f64)> = Vec::new();
    let mut total_trades = 0usize;
    let mut total_turnover = 0.0f64;
    let mut pending_signal: Option<&APortfolioSignal> = None;

    // Benchmark start price
    let bm_start_price = cache.index_prices.get(benchmark_code)
        .and_then(|v| v.first().map(|(_, p)| *p))
        .unwrap_or(1.0);

    let signal_dates: Vec<NaiveDate> = signals.keys().copied().collect();
    let mut signal_idx = 0;
    let universe_filter = universe_cfg.map(AUniverseFilter::from_config);
    let mut total_dropped = 0usize;

    for (day_idx, &today) in trading_days.iter().enumerate() {
        // A return-preserving share adjustment models reinvested distributions
        // and splits from adj_factor; it is not a cash-dividend ledger.
        for (code, shares) in positions.iter_mut() {
            let Some(bars) = cache.daily.get(code) else { continue; };
            let Ok(pos) = bars.binary_search_by_key(&today, |(d, _)| *d) else { continue; };
            if pos == 0 { continue; }
            let previous = bars[pos - 1].1.adj_factor;
            let current = bars[pos].1.adj_factor;
            let open = bars[pos].1.open;
            if previous > 0.0 && previous.is_finite() && current > 0.0 && current.is_finite()
                && (current / previous - 1.0).abs() > 1e-10 && open.is_finite() && open > 0.0 {
                let adjusted = *shares as f64 * (current / previous);
                *shares = adjusted.floor() as i64;
                cash += (adjusted - *shares as f64) * open;
            }
        }
        let q = OpeningQuotes { cache,
            bars: positions.keys().chain(pending_signal.into_iter().flat_map(|s| s.keys()))
                .filter_map(|code| cache.get_bar(code, today).map(|bar| {
                    let mut open = bar.clone();
                    open.high = bar.open;
                    open.low = bar.open;
                    open.close = bar.open;
                    open.pct_chg = if bar.pre_close > 0.0 { (bar.open / bar.pre_close - 1.0) * 100.0 } else { 0.0 };
                    (code.clone(), open)
                })).collect(),
        };

        // === T+1 execution: execute yesterday's pending signal ===
        if let Some(target_weights) = pending_signal.take() {
            // Defensive: drop weights on codes that fell out of clean universe.
            // (Held positions in those codes still get sold via plan_orders since
            // they won't appear in target_weights.)
            let owned_weights;
            let effective_weights: &APortfolioSignal = if let Some(f) = universe_filter.as_ref() {
                let universe = get_a_clean_universe(trading_days[day_idx.saturating_sub(1)], cache, f);
                let filtered: APortfolioSignal = target_weights.iter()
                    .filter(|(c, _)| universe.contains(c.as_str()))
                    .map(|(c, w)| (c.clone(), *w))
                    .collect();
                let dropped = target_weights.len() - filtered.len();
                if dropped > 0 {
                    debug!("[{}] dropped {} signal weights outside clean universe", today, dropped);
                    total_dropped += dropped;
                }
                owned_weights = filtered;
                &owned_weights
            } else {
                target_weights
            };

            let total_value = marked_value(&positions, cache, today, cash, true);
            let orders = crate::a_exec::plan_orders(&positions, effective_weights, total_value, &q, config);
            let fills = crate::a_exec::execute_orders(&orders, &mut positions, &mut cash, &q, config);
            for f in &fills {
                total_turnover += f.gross;
                total_trades += 1;
            }
        }

        // === Check for new signal (will execute tomorrow T+1) ===
        while signal_idx < signal_dates.len() && signal_dates[signal_idx] <= today {
            pending_signal = signals.get(&signal_dates[signal_idx]);
            signal_idx += 1;
        }

        // === Daily NAV ===
        let nav = marked_value(&positions, cache, today, cash, false) / config.initial_capital;
        nav_series.push((today, nav));

        // Benchmark NAV
        if let Some(idx_prices) = cache.index_prices.get(benchmark_code) {
            if let Ok(pos) = idx_prices.binary_search_by_key(&today, |(d, _)| *d) {
                let bm_nav = idx_prices[pos].1 / bm_start_price;
                benchmark_nav.push((today, bm_nav));
            }
        }
    }

    // === Compute statistics ===
    let stats = compute_stats(&nav_series, &benchmark_nav, total_trades, total_turnover, config.initial_capital);

    info!(
        "A-share backtest: NAV={:.4}, return={:.2}%, Sharpe={:.2}, DD={:.2}%, trades={}, universe-dropped={}",
        stats.0, stats.1 * 100.0, stats.3, stats.4 * 100.0, total_trades, total_dropped,
    );

    ABacktestResult {
        nav: nav_series,
        benchmark_nav,
        total_return: stats.0 - 1.0,
        annual_return: stats.1,
        annual_volatility: stats.2,
        sharpe_ratio: stats.3,
        max_drawdown: stats.4,
        calmar_ratio: if stats.4.abs() > 1e-6 { stats.1 / stats.4 } else { 0.0 },
        win_rate: stats.5,
        total_trades,
        annual_turnover: stats.6,
    }
}

// Quotes visible at the open; limit checks must not inspect the day's high/close.
struct OpeningQuotes<'a> {
    cache: &'a AShareCache,
    bars: FxHashMap<String, ABar>,
}
impl crate::a_exec::QuoteSource for OpeningQuotes<'_> {
    fn bar(&self, code: &str) -> Option<&ABar> { self.bars.get(code) }
    fn is_st(&self, code: &str) -> bool { self.cache.is_st(code) }
}

fn marked_value(positions: &FxHashMap<String, i64>, cache: &AShareCache,
    date: NaiveDate, cash: f64, at_open: bool) -> f64 {
    cash + positions.iter().map(|(code, shares)| {
        let price = cache.daily.get(code).and_then(|bars| {
            let end = bars.partition_point(|(d, _)| *d <= date);
            if end == 0 { return None; }
            let (d, bar) = &bars[end - 1];
            let p = if at_open && *d == date { bar.open } else { bar.close };
            Some(p)
        }).unwrap_or(0.0);
        *shares as f64 * price
    }).sum::<f64>()
}

// ── Helpers ─────────────────────────────────────────────────────────────
// Cost / lot / portfolio helpers live in `a_exec` — the engine and trader
// share that module to keep paper and backtest results identical.

/// Threshold % (e.g. 9.5 for ±10% board with 0.5% tolerance) used to detect
/// near-limit moves. Returns the up-side cutoff (positive) — flip sign for down.
fn near_limit_pct(board: Board, is_st: bool, rules: &AShareMarketRulesConfig) -> f64 {
    let limit = limit_pct_for(board, is_st, rules);
    (limit - rules.one_char_tolerance) * 100.0
}

/// Detect one-char limit-up (一字涨停): pct_chg >= near-limit && open == high == close.
pub fn is_limit_up_one_char(
    ts_code: &str,
    bar: &ABar,
    is_st: bool,
    rules: &AShareMarketRulesConfig,
) -> bool {
    let threshold = near_limit_pct(board_from_ts_code(ts_code), is_st, rules);
    bar.pct_chg >= threshold
        && (bar.open - bar.high).abs() < 0.01
        && (bar.open - bar.close).abs() < 0.01
}

/// Detect one-char limit-down (一字跌停): pct_chg <= -near-limit && open == low == close.
pub fn is_limit_down_one_char(
    ts_code: &str,
    bar: &ABar,
    is_st: bool,
    rules: &AShareMarketRulesConfig,
) -> bool {
    let threshold = -near_limit_pct(board_from_ts_code(ts_code), is_st, rules);
    bar.pct_chg <= threshold
        && (bar.open - bar.low).abs() < 0.01
        && (bar.open - bar.close).abs() < 0.01
}

fn compute_stats(
    nav: &[(NaiveDate, f64)],
    _bm_nav: &[(NaiveDate, f64)],
    _total_trades: usize,
    total_turnover: f64,
    initial_capital: f64,
) -> (f64, f64, f64, f64, f64, f64, f64) {
    // (final_nav, annual_return, annual_vol, sharpe, max_dd, win_rate, annual_turnover)
    if nav.len() < 2 {
        return (1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0);
    }

    let final_nav = nav.last().unwrap().1;
    let days = (nav.last().unwrap().0 - nav.first().unwrap().0).num_days() as f64;
    let years = days / 365.25;

    let annual_return = if years > 0.0 { final_nav.powf(1.0 / years) - 1.0 } else { 0.0 };

    // Daily returns
    let mut returns = Vec::with_capacity(nav.len() - 1);
    let mut win_count = 0usize;
    for i in 1..nav.len() {
        let r = nav[i].1 / nav[i - 1].1 - 1.0;
        if r > 0.0 { win_count += 1; }
        returns.push(r);
    }

    let mean_r = returns.iter().sum::<f64>() / returns.len() as f64;
    let var = returns.iter().map(|r| (r - mean_r).powi(2)).sum::<f64>() / (returns.len() - 1) as f64;
    let annual_vol = var.sqrt() * (244.0_f64).sqrt(); // A-share ~244 trading days

    let _rf_daily = 0.02 / 244.0; // 2% annual risk-free
    let sharpe = if annual_vol > 1e-6 { (annual_return - 0.02) / annual_vol } else { 0.0 };

    // Max drawdown
    let mut peak = 0.0f64;
    let mut max_dd = 0.0f64;
    for &(_, n) in nav {
        peak = peak.max(n);
        let dd = (peak - n) / peak;
        max_dd = max_dd.max(dd);
    }

    let win_rate = if !returns.is_empty() { win_count as f64 / returns.len() as f64 } else { 0.0 };
    let annual_turnover = if years > 0.0 { total_turnover / initial_capital / years } else { 0.0 };

    (final_nav, annual_return, annual_vol, sharpe, max_dd, win_rate, annual_turnover)
}

fn empty_result() -> ABacktestResult {
    ABacktestResult {
        nav: vec![], benchmark_nav: vec![],
        total_return: 0.0, annual_return: 0.0, annual_volatility: 0.0,
        sharpe_ratio: 0.0, max_drawdown: 0.0, calmar_ratio: 0.0,
        win_rate: 0.0, total_trades: 0, annual_turnover: 0.0,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn rules() -> AShareMarketRulesConfig {
        AShareMarketRulesConfig::default()
    }

    fn make_bar(pct_chg: f64, open: f64, high: f64, low: f64, close: f64) -> ABar {
        ABar {
            open, high, low, close, pre_close: 0.0, pct_chg,
            vol: 0.0, amount: 0.0, adj_factor: 1.0,
            turnover_rate: 0.0, pe_ttm: 0.0, pb: 0.0, ps_ttm: 0.0, dv_ttm: 0.0,
            total_mv: 0.0, circ_mv: 0.0,
        }
    }

    #[test]
    fn board_detection() {
        assert_eq!(board_from_ts_code("600519.SH"), Board::Main);     // 沪市主板
        assert_eq!(board_from_ts_code("000001.SZ"), Board::Main);     // 深市主板
        assert_eq!(board_from_ts_code("002594.SZ"), Board::Main);     // 中小板
        assert_eq!(board_from_ts_code("300750.SZ"), Board::ChiNext);  // 创业板
        assert_eq!(board_from_ts_code("301129.SZ"), Board::ChiNext);  // 创业板新
        assert_eq!(board_from_ts_code("688981.SH"), Board::StarMarket); // 科创板
        assert_eq!(board_from_ts_code("830799.BJ"), Board::Bse);      // 北交所
        assert_eq!(board_from_ts_code("430047.BJ"), Board::Bse);      // 北交所 4 开头
    }

    #[test]
    fn limit_pct_per_board() {
        let r = rules();
        assert_eq!(limit_pct_for(Board::Main, false, &r), 0.10);
        assert_eq!(limit_pct_for(Board::ChiNext, false, &r), 0.20);
        assert_eq!(limit_pct_for(Board::StarMarket, false, &r), 0.20);
        assert_eq!(limit_pct_for(Board::Bse, false, &r), 0.30);
        // ST overrides everything
        assert_eq!(limit_pct_for(Board::Main, true, &r), 0.05);
        assert_eq!(limit_pct_for(Board::ChiNext, true, &r), 0.05);
    }

    #[test]
    fn limit_up_main_board_blocks_at_9_5_pct() {
        let r = rules();
        // 主板 ±10%，one_char_tolerance 0.5% → 阈值 9.5%
        let bar = make_bar(9.6, 11.0, 11.0, 10.0, 11.0);
        assert!(is_limit_up_one_char("600519.SH", &bar, false, &r));
        // 9.4% 不触发
        let bar = make_bar(9.4, 11.0, 11.0, 10.0, 11.0);
        assert!(!is_limit_up_one_char("600519.SH", &bar, false, &r));
    }

    #[test]
    fn limit_up_chinext_uses_19_5_pct() {
        let r = rules();
        // 创业板 ±20% → 阈值 19.5%
        let bar = make_bar(19.6, 12.0, 12.0, 10.0, 12.0);
        assert!(is_limit_up_one_char("300750.SZ", &bar, false, &r));
        // 主板规则用同 9.6% 涨幅会触发，但创业板 9.6% 不会
        let bar = make_bar(9.6, 12.0, 12.0, 10.0, 12.0);
        assert!(!is_limit_up_one_char("300750.SZ", &bar, false, &r));
    }

    #[test]
    fn limit_up_st_uses_4_5_pct() {
        let r = rules();
        // ST ±5% → 阈值 4.5%
        let bar = make_bar(4.6, 10.5, 10.5, 10.0, 10.5);
        assert!(is_limit_up_one_char("600519.SH", &bar, true, &r));
        let bar = make_bar(4.4, 10.5, 10.5, 10.0, 10.5);
        assert!(!is_limit_up_one_char("600519.SH", &bar, true, &r));
    }

    #[test]
    fn limit_up_requires_one_char_shape() {
        let r = rules();
        // 涨幅够但 open != high (非一字板) → 不触发
        let bar = make_bar(9.6, 10.5, 11.0, 10.0, 11.0);
        assert!(!is_limit_up_one_char("600519.SH", &bar, false, &r));
    }

    #[test]
    fn limit_down_bse_uses_29_5_pct() {
        let r = rules();
        // 北交所 ±30% → 阈值 -29.5%
        let bar = make_bar(-29.6, 7.0, 7.0, 7.0, 7.0);
        assert!(is_limit_down_one_char("830799.BJ", &bar, false, &r));
    }

    #[test]
    fn cost_config_derives_from_a_share() {
        let a = AShareConfig::default();
        let cost = ACostConfig::from_a_share(&a);
        assert_eq!(cost.buy_commission, 0.00075);
        assert_eq!(cost.stamp_tax, 0.001);
        assert_eq!(cost.lot_size, 100);
        assert_eq!(cost.min_commission, 5.0);
        assert_eq!(cost.market_rules.main_board_limit, 0.10);
        assert_eq!(cost.market_rules.bse_limit, 0.30);
    }
    fn execution_fixture() -> AShareCache {
        let dates: Vec<_> = (1..=4).map(|n| NaiveDate::from_ymd_opt(2024, 1, n).unwrap()).collect();
        let bars = dates.iter().map(|d| {
            let mut bar = make_bar(0.0, 10.0, 10.0, 10.0, 10.0);
            bar.pre_close = 10.0; bar.vol = 1000.0;
            (*d, bar)
        }).collect();
        AShareCache { daily: [("000001.SZ".into(), bars)].into_iter().collect(),
            financials: FxHashMap::default(), industry: FxHashMap::default(),
            basics: FxHashMap::default(), trading_days: dates.clone(),
            index_prices: [("BM".into(), dates.iter().map(|d| (*d, 100.0)).collect())].into_iter().collect(),
            ts_codes: vec!["000001.SZ".into()], top_list: FxHashMap::default(), margin_detail: FxHashMap::default() }
    }

    #[test]
    fn opening_size_does_not_use_same_day_close() {
        let mut cache = execution_fixture();
        let dates = cache.trading_days.clone();
        let signals = [(dates[0], [("000001.SZ".into(), 0.5)].into_iter().collect()),
            (dates[1], [("000001.SZ".into(), 0.5)].into_iter().collect())].into_iter().collect();
        let cost = ACostConfig::default();
        let original = run_backtest(&signals, &cache, &cost, "BM", None);
        cache.daily.get_mut("000001.SZ").unwrap()[2].1.close = 30.0;
        let changed = run_backtest(&signals, &cache, &cost, "BM", None);
        assert_eq!(original.total_trades, changed.total_trades);
        assert_eq!(original.nav.last(), changed.nav.last(), "only intraday mark changes, not opening trades");
    }

    #[test]
    fn suspended_positions_keep_value_and_splits_preserve_returns() {
        let mut cache = execution_fixture();
        let dates = cache.trading_days.clone();
        let signals = [(dates[0], [("000001.SZ".into(), 0.5)].into_iter().collect())].into_iter().collect();
        let cost = ACostConfig::default();
        let original = run_backtest(&signals, &cache, &cost, "BM", None);
        let bars = cache.daily.get_mut("000001.SZ").unwrap();
        bars.remove(2); // missing quote after purchase
        bars[2].1.open = 5.0; bars[2].1.close = 5.0; bars[2].1.adj_factor = 2.0;
        let changed = run_backtest(&signals, &cache, &cost, "BM", None);
        assert_eq!(original.nav, changed.nav);
    }

}
