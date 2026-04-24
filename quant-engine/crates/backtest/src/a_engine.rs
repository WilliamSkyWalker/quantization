//! A-share backtest engine — T+1 execution with Chinese market rules.
//!
//! Key differences from US engine:
//! - T+1: signals trigger next-day open execution (not same-day close)
//! - Lot size: 100 shares (整手)
//! - Stamp tax: 0.1% on sell side only
//! - Limit up/down: 10% (20% for ChiNext/STAR)
//! - Commission minimum: 5 CNY per trade

use std::collections::BTreeMap;

use chrono::NaiveDate;
use rustc_hash::FxHashMap;
use tracing::info;

use quant_factors::a_share::cache::{AShareCache, ABar};

const LOT_SIZE: i64 = 100;
const MIN_COMMISSION: f64 = 5.0;

/// A-share execution cost parameters.
#[derive(Debug, Clone)]
pub struct ACostConfig {
    pub buy_commission: f64,   // 0.0003 (万三)
    pub sell_commission: f64,  // 0.0003
    pub stamp_tax: f64,        // 0.001 (千一, sell only)
    pub slippage: f64,         // 0.0005
    pub initial_capital: f64,  // 1_000_000.0
}

impl Default for ACostConfig {
    fn default() -> Self {
        Self {
            buy_commission: 0.0003,
            sell_commission: 0.0003,
            stamp_tax: 0.001,
            slippage: 0.0005,
            initial_capital: 1_000_000.0,
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
pub fn run_backtest(
    signals: &BTreeMap<NaiveDate, APortfolioSignal>,
    cache: &AShareCache,
    config: &ACostConfig,
    benchmark_code: &str,
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

    for &today in trading_days {
        // === T+1 execution: execute yesterday's pending signal ===
        if let Some(target_weights) = pending_signal.take() {
            let total_value = portfolio_value(&positions, cache, today, cash);

            // Phase 1: Sell (reduce/close positions)
            let mut sell_codes: Vec<String> = Vec::new();
            for (code, &_shares) in &positions {
                let target_w = target_weights.get(code).copied().unwrap_or(0.0);
                if target_w <= 0.0 {
                    sell_codes.push(code.clone());
                }
            }
            for code in &sell_codes {
                let shares = positions.get(code).copied().unwrap_or(0);
                if shares <= 0 { continue; }
                if let Some(bar) = cache.get_bar(code, today) {
                    // Check: can we sell? (not limit-down one-char board)
                    if is_limit_down_one_char(bar) { continue; }
                    let exec_price = bar.open * (1.0 - config.slippage);
                    let amount = shares as f64 * exec_price;
                    let fees = calc_sell_fees(amount, config);
                    cash += amount - fees;
                    total_turnover += amount;
                    total_trades += 1;
                    positions.remove(code);
                }
            }

            // Phase 2: Buy (open/increase positions)
            for (code, &target_w) in target_weights {
                if target_w <= 0.0 { continue; }
                let bar = match cache.get_bar(code, today) {
                    Some(b) => b,
                    None => continue,
                };
                // Check: can we buy? (not limit-up one-char board)
                if is_limit_up_one_char(bar) { continue; }

                let current_shares = positions.get(code).copied().unwrap_or(0);
                let target_value = total_value * target_w;
                let exec_price = bar.open * (1.0 + config.slippage);
                if exec_price <= 0.0 { continue; }

                let target_shares = round_to_lot((target_value / exec_price) as i64);
                let delta = target_shares - current_shares;
                if delta <= 0 { continue; }

                let buy_amount = delta as f64 * exec_price;
                let fees = calc_buy_fees(buy_amount, config);
                let total_cost = buy_amount + fees;

                if total_cost > cash { continue; } // not enough cash

                cash -= total_cost;
                total_turnover += buy_amount;
                total_trades += 1;
                *positions.entry(code.clone()).or_insert(0) += delta;
            }
        }

        // === Check for new signal (will execute tomorrow T+1) ===
        while signal_idx < signal_dates.len() && signal_dates[signal_idx] <= today {
            pending_signal = signals.get(&signal_dates[signal_idx]);
            signal_idx += 1;
        }

        // === Daily NAV ===
        let nav = portfolio_value(&positions, cache, today, cash) / config.initial_capital;
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
        "A-share backtest: NAV={:.4}, return={:.2}%, Sharpe={:.2}, DD={:.2}%, trades={}",
        stats.0, stats.1 * 100.0, stats.3, stats.4 * 100.0, total_trades,
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

// ── Helpers ─────────────────────────────────────────────────────────────

fn portfolio_value(
    positions: &FxHashMap<String, i64>,
    cache: &AShareCache,
    date: NaiveDate,
    cash: f64,
) -> f64 {
    let mut value = cash;
    for (code, &shares) in positions {
        if let Some(bar) = cache.get_bar(code, date) {
            value += shares as f64 * bar.close;
        }
    }
    value
}

fn round_to_lot(shares: i64) -> i64 {
    (shares / LOT_SIZE) * LOT_SIZE
}

fn calc_buy_fees(amount: f64, config: &ACostConfig) -> f64 {
    (amount * config.buy_commission).max(MIN_COMMISSION)
}

fn calc_sell_fees(amount: f64, config: &ACostConfig) -> f64 {
    let commission = (amount * config.sell_commission).max(MIN_COMMISSION);
    let stamp = amount * config.stamp_tax;
    commission + stamp
}

/// Detect one-char limit-up (一字涨停): open == high == close, pct_chg >= 9.5%
fn is_limit_up_one_char(bar: &ABar) -> bool {
    let threshold = 9.5; // 10% board ± tolerance
    bar.pct_chg >= threshold
        && (bar.open - bar.high).abs() < 0.01
        && (bar.open - bar.close).abs() < 0.01
}

/// Detect one-char limit-down (一字跌停): open == low == close, pct_chg <= -9.5%
fn is_limit_down_one_char(bar: &ABar) -> bool {
    let threshold = -9.5;
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
