//! Position-based T+0 backtest engine for US equities.
//!
//! Execution model:
//! - T+0: signals execute at same-day close price (adj_close)
//! - Buy: adj_close * (1 + slippage)
//! - Sell: adj_close * (1 - slippage)
//! - Cash tracking: sell first, then buy
//! - Daily NAV = (cash + position market value) / initial capital

use std::collections::BTreeMap;

use quant_core::config::Config;
use quant_core::types::{Date, TickerId};
use quant_data::cache::DataCache;
use rustc_hash::FxHashMap;
use tracing::{info, warn};

/// A single trade record.
#[derive(Debug, Clone)]
pub struct TradeRecord {
    pub date: Date,
    pub ticker: TickerId,
    pub direction: String, // BUY, SELL, SHORT, COVER, STOP_COVER
    pub volume: f64,
    pub price: f64,
    pub amount: f64,
    pub fees: f64,
}

/// Per-period turnover record.
#[derive(Debug, Clone)]
pub struct TurnoverRecord {
    pub date: Date,
    pub turnover: f64,
}

/// Performance statistics.
#[derive(Debug, Clone, Default)]
pub struct PerformanceStats {
    pub total_return: f64,
    pub annual_return: f64,
    pub annual_volatility: f64,
    pub sharpe_ratio: f64,
    pub max_drawdown: f64,
    pub max_drawdown_date: String,
    pub calmar_ratio: f64,
    pub win_rate: f64,
    pub total_trades: usize,
    pub avg_turnover: f64,
    pub annual_turnover: f64,
    pub trade_days: usize,
    pub benchmark_total_return: f64,
    pub benchmark_annual_return: f64,
    pub excess_annual_return: f64,
}

/// Backtest result.
pub struct BacktestResult {
    pub nav: Vec<(Date, f64)>,
    pub benchmark_nav: Vec<(Date, f64)>,
    pub trades: Vec<TradeRecord>,
    pub turnover: Vec<TurnoverRecord>,
    pub stats: PerformanceStats,
}

/// Portfolio weight signal: ticker -> weight (positive = long, negative = short).
pub type PortfolioSignal = FxHashMap<TickerId, f64>;

pub struct BacktestEngine {
    pub initial_capital: f64,
    pub slippage: f64,
    pub buy_commission: f64,
    pub sell_commission: f64,
    pub benchmark: String,
    // Risk controls
    pub use_vol_targeting: bool,
    pub target_vol: f64,
    pub vol_lookback_days: usize,
    pub vol_scale_min: f64,
    pub vol_scale_max: f64,
    pub dd_start_threshold: f64,
    pub dd_max_threshold: f64,
    pub dd_min_position: f64,
    pub strategy_mom_window: usize,
    pub strategy_mom_min_scale: f64,
    // Short
    pub short_borrow_fee: f64,
    pub short_stop_loss: f64,
}

impl BacktestEngine {
    pub fn from_config(config: &Config) -> Self {
        Self {
            initial_capital: config.execution.initial_capital,
            slippage: config.execution.slippage,
            buy_commission: config.execution.buy_commission,
            sell_commission: config.execution.sell_commission,
            benchmark: config.universe.benchmark_index.clone(),
            use_vol_targeting: config.risk_controls.use_vol_targeting,
            target_vol: config.risk_controls.target_vol,
            vol_lookback_days: config.risk_controls.vol_lookback_days,
            vol_scale_min: config.risk_controls.vol_scale_min,
            vol_scale_max: config.risk_controls.vol_scale_max,
            dd_start_threshold: config.risk_controls.dd_start_threshold,
            dd_max_threshold: config.risk_controls.dd_max_threshold,
            dd_min_position: config.risk_controls.dd_min_position,
            strategy_mom_window: config.risk_controls.strategy_mom_window,
            strategy_mom_min_scale: config.risk_controls.strategy_mom_min_scale,
            short_borrow_fee: config.short.borrow_fee,
            short_stop_loss: config.short.stop_loss,
        }
    }

    /// Run backtest over the given date range with pre-computed signals.
    pub fn run(
        &self,
        signals: &BTreeMap<Date, PortfolioSignal>,
        cache: &DataCache,
        start: Date,
        end: Date,
    ) -> BacktestResult {
        info!("Backtest: {} to {}, {} signals", start, end, signals.len());

        let trade_dates: Vec<Date> = cache
            .trading_days
            .iter()
            .filter(|&&d| d >= start && d <= end)
            .copied()
            .collect();

        if trade_dates.is_empty() {
            warn!("No trading days found in range");
            return BacktestResult {
                nav: vec![],
                benchmark_nav: vec![],
                trades: vec![],
                turnover: vec![],
                stats: PerformanceStats::default(),
            };
        }

        let signal_dates: Vec<Date> = signals.keys().copied().collect();
        let mut signal_idx = 0usize;

        let mut cash = self.initial_capital;
        let mut positions: FxHashMap<TickerId, f64> = FxHashMap::default(); // ticker -> shares
        let mut last_close: FxHashMap<TickerId, f64> = FxHashMap::default();
        let mut short_entry_prices: FxHashMap<TickerId, f64> = FxHashMap::default();

        let mut nav_series: Vec<(Date, f64)> = Vec::with_capacity(trade_dates.len());
        let mut trades: Vec<TradeRecord> = Vec::new();
        let mut turnover_list: Vec<TurnoverRecord> = Vec::new();

        for &today in &trade_dates {
            let mut day_turnover_amount = 0.0f64;

            // === Check signal ===
            if signal_idx < signal_dates.len() && today >= signal_dates[signal_idx] {
                let raw_weights = &signals[&signal_dates[signal_idx]];
                let mut weights = raw_weights.clone();

                // === Risk controls ===
                // 1. Volatility targeting
                if self.use_vol_targeting && nav_series.len() >= self.vol_lookback_days + 1 {
                    let nav_vals: Vec<f64> = nav_series.iter().map(|(_, n)| *n).collect();
                    let recent = &nav_vals[nav_vals.len() - self.vol_lookback_days - 1..];
                    let rets: Vec<f64> = recent.windows(2).map(|w| w[1] / w[0] - 1.0).collect();
                    let n = rets.len() as f64;
                    let mean = rets.iter().sum::<f64>() / n;
                    let var = rets.iter().map(|r| (r - mean).powi(2)).sum::<f64>() / (n - 1.0);
                    let realized_vol = var.sqrt() * (252.0f64).sqrt();
                    if realized_vol > 0.0 {
                        let vol_scale = (self.target_vol / realized_vol)
                            .clamp(self.vol_scale_min, self.vol_scale_max);
                        if vol_scale < 1.0 {
                            for v in weights.values_mut() {
                                *v *= vol_scale;
                            }
                        }
                    }
                }

                // 2. Drawdown response
                if nav_series.len() >= 5 {
                    let nav_vals: Vec<f64> = nav_series.iter().map(|(_, n)| *n).collect();
                    let peak = nav_vals.iter().copied().fold(f64::NEG_INFINITY, f64::max);
                    let current = *nav_vals.last().unwrap();
                    let dd = if peak > 0.0 { (peak - current) / peak } else { 0.0 };
                    if dd >= self.dd_start_threshold {
                        let dd_scale = if dd >= self.dd_max_threshold {
                            self.dd_min_position
                        } else {
                            1.0 - (dd - self.dd_start_threshold)
                                / (self.dd_max_threshold - self.dd_start_threshold)
                                * (1.0 - self.dd_min_position)
                        };
                        for v in weights.values_mut() {
                            *v *= dd_scale;
                        }
                    }
                }

                // === Execute T+0 ===
                let total_value = self.portfolio_value(&positions, &last_close, cash, today, cache);

                // Generate orders
                let mut sell_orders: Vec<(TickerId, f64)> = Vec::new();
                let mut buy_orders: Vec<(TickerId, f64, f64)> = Vec::new(); // (ticker, delta, weight)

                let mut all_tickers: std::collections::HashSet<TickerId> = positions.keys().copied().collect();
                all_tickers.extend(weights.keys());

                for &ticker in &all_tickers {
                    let current_shares = positions.get(&ticker).copied().unwrap_or(0.0);
                    let target_weight = weights.get(&ticker).copied().unwrap_or(0.0);

                    let close_px = match self.get_close(ticker, today, cache) {
                        Some(p) if p > 0.0 => p,
                        _ => continue,
                    };

                    let target_shares = (target_weight * total_value / close_px).floor();
                    let delta = target_shares - current_shares;

                    if delta.abs() < 1.0 {
                        continue;
                    }

                    if delta < 0.0 {
                        sell_orders.push((ticker, delta.abs()));
                    } else {
                        buy_orders.push((ticker, delta, target_weight));
                    }
                }

                // Phase 1: Sell/close positions
                for &(ticker, volume) in &sell_orders {
                    let close_px = match self.get_close(ticker, today, cache) {
                        Some(p) => p,
                        None => continue,
                    };
                    let exec_price = close_px * (1.0 - self.slippage);
                    let amount = volume * exec_price;
                    let fees = amount * self.sell_commission;
                    cash += amount - fees;
                    day_turnover_amount += amount;

                    let current = positions.get(&ticker).copied().unwrap_or(0.0);
                    let new_shares = current - volume;
                    if new_shares.abs() < 0.5 {
                        positions.remove(&ticker);
                        short_entry_prices.remove(&ticker);
                    } else {
                        positions.insert(ticker, new_shares);
                    }

                    trades.push(TradeRecord {
                        date: today,
                        ticker,
                        direction: "SELL".to_string(),
                        volume,
                        price: exec_price,
                        amount,
                        fees,
                    });
                }

                // Phase 2: Buy/open positions
                buy_orders.sort_by(|a, b| b.2.partial_cmp(&a.2).unwrap_or(std::cmp::Ordering::Equal));
                for &(ticker, delta, _weight) in &buy_orders {
                    let close_px = match self.get_close(ticker, today, cache) {
                        Some(p) => p,
                        None => continue,
                    };
                    let exec_price = close_px * (1.0 + self.slippage);
                    let cost_per = exec_price * (1.0 + self.buy_commission);
                    let max_affordable = if cost_per > 0.0 { (cash / cost_per).floor() } else { 0.0 };
                    let actual_vol = delta.min(max_affordable);
                    if actual_vol < 1.0 {
                        continue;
                    }

                    let amount = actual_vol * exec_price;
                    let fees = amount * self.buy_commission;
                    cash -= amount + fees;
                    day_turnover_amount += amount;

                    let current = positions.get(&ticker).copied().unwrap_or(0.0);
                    positions.insert(ticker, current + actual_vol);

                    trades.push(TradeRecord {
                        date: today,
                        ticker,
                        direction: "BUY".to_string(),
                        volume: actual_vol,
                        price: exec_price,
                        amount,
                        fees,
                    });
                }

                // Record turnover
                let turnover = if total_value > 0.0 {
                    day_turnover_amount / total_value / 2.0
                } else {
                    0.0
                };
                turnover_list.push(TurnoverRecord {
                    date: today,
                    turnover,
                });

                // Track short entry prices
                for (&ticker, &weight) in raw_weights {
                    if weight < 0.0 && !short_entry_prices.contains_key(&ticker) {
                        if let Some(px) = self.get_close(ticker, today, cache) {
                            short_entry_prices.insert(ticker, px);
                        }
                    }
                }
                // Remove entry prices for closed shorts
                short_entry_prices.retain(|t, _| {
                    positions.get(t).map(|&s| s < 0.0).unwrap_or(false)
                });

                signal_idx += 1;
            }

            // === Daily short stop-loss check ===
            if self.short_stop_loss > 0.0 {
                let mut to_cover: Vec<(TickerId, f64, f64)> = Vec::new(); // (ticker, shares, price)
                for (&ticker, &shares) in &positions {
                    if shares >= 0.0 { continue; } // Only check shorts
                    let entry = match short_entry_prices.get(&ticker) {
                        Some(&e) => e,
                        None => continue,
                    };
                    let current = match self.get_close(ticker, today, cache) {
                        Some(p) => p,
                        None => continue,
                    };
                    let loss_pct = current / entry - 1.0; // positive = price went up = loss for short
                    if loss_pct >= self.short_stop_loss {
                        to_cover.push((ticker, shares, current));
                    }
                }
                for (ticker, shares, current_px) in to_cover {
                    let cover_vol = shares.abs();
                    let exec_price = current_px * (1.0 + self.slippage);
                    let amount = cover_vol * exec_price;
                    let fees = amount * self.buy_commission;
                    cash -= amount + fees;
                    positions.remove(&ticker);
                    short_entry_prices.remove(&ticker);
                    trades.push(TradeRecord {
                        date: today,
                        ticker,
                        direction: "STOP_COVER".to_string(),
                        volume: cover_vol,
                        price: exec_price,
                        amount,
                        fees,
                    });
                }
            }

            // === Daily NAV ===
            let market_value = self.mark_to_market(&positions, &mut last_close, today, cache);
            let nav = (cash + market_value) / self.initial_capital;
            nav_series.push((today, nav));
        }

        // Benchmark NAV
        let benchmark_nav = self.compute_benchmark_nav(&trade_dates, cache);

        // Stats
        let stats = compute_stats(&nav_series, &benchmark_nav, &trades, &turnover_list);

        info!(
            "Backtest complete: NAV={:.4}, return={:.2}%, trades={}",
            nav_series.last().map(|(_, n)| *n).unwrap_or(0.0),
            stats.total_return * 100.0,
            trades.len()
        );

        BacktestResult {
            nav: nav_series,
            benchmark_nav,
            trades,
            turnover: turnover_list,
            stats,
        }
    }

    fn get_close(&self, ticker: TickerId, date: Date, cache: &DataCache) -> Option<f64> {
        cache.get_close(ticker, date).filter(|p| p.is_finite() && *p > 0.0)
    }


    fn portfolio_value(
        &self,
        positions: &FxHashMap<TickerId, f64>,
        last_close: &FxHashMap<TickerId, f64>,
        cash: f64,
        date: Date,
        cache: &DataCache,
    ) -> f64 {
        let mut total = cash;
        for (&ticker, &shares) in positions {
            let px = self
                .get_close(ticker, date, cache)
                .or_else(|| last_close.get(&ticker).copied())
                .unwrap_or(0.0);
            total += shares * px;
        }
        total
    }

    fn mark_to_market(
        &self,
        positions: &FxHashMap<TickerId, f64>,
        last_close: &mut FxHashMap<TickerId, f64>,
        date: Date,
        cache: &DataCache,
    ) -> f64 {
        let mut market_value = 0.0;
        for (&ticker, &shares) in positions {
            let px = match self.get_close(ticker, date, cache) {
                Some(p) => {
                    last_close.insert(ticker, p);
                    p
                }
                None => last_close.get(&ticker).copied().unwrap_or(0.0),
            };
            market_value += shares * px;
        }
        market_value
    }

    fn compute_benchmark_nav(&self, dates: &[Date], cache: &DataCache) -> Vec<(Date, f64)> {
        let mut result = Vec::with_capacity(dates.len());
        let first_close = dates
            .iter()
            .find_map(|d| cache.get_index_close(&self.benchmark, *d));

        let Some(first) = first_close else {
            return result;
        };

        for &date in dates {
            if let Some(close) = cache.get_index_close(&self.benchmark, date) {
                result.push((date, close / first));
            }
        }
        result
    }
}

fn compute_stats(
    nav: &[(Date, f64)],
    benchmark_nav: &[(Date, f64)],
    trades: &[TradeRecord],
    turnover: &[TurnoverRecord],
) -> PerformanceStats {
    if nav.is_empty() {
        return PerformanceStats::default();
    }

    let daily_navs: Vec<f64> = nav.iter().map(|(_, n)| *n).collect();
    let n_days = daily_navs.len();
    let n_years = n_days as f64 / 252.0;

    let total_return = daily_navs.last().unwrap() / daily_navs[0] - 1.0;
    let annual_return = (1.0 + total_return).powf(1.0 / n_years.max(0.01)) - 1.0;

    // Daily returns
    let daily_rets: Vec<f64> = daily_navs
        .windows(2)
        .map(|w| w[1] / w[0] - 1.0)
        .collect();
    let n_rets = daily_rets.len() as f64;
    let mean_ret = daily_rets.iter().sum::<f64>() / n_rets;
    let var = daily_rets
        .iter()
        .map(|r| (r - mean_ret).powi(2))
        .sum::<f64>()
        / (n_rets - 1.0).max(1.0);
    let annual_vol = var.sqrt() * (252.0f64).sqrt();

    let rf = 0.04;
    let sharpe = if annual_vol > 0.0 {
        (annual_return - rf) / annual_vol
    } else {
        0.0
    };

    // Max drawdown
    let mut peak = f64::NEG_INFINITY;
    let mut max_dd = 0.0f64;
    let mut max_dd_date = nav[0].0;
    for &(date, n) in nav {
        if n > peak {
            peak = n;
        }
        let dd = (peak - n) / peak;
        if dd > max_dd {
            max_dd = dd;
            max_dd_date = date;
        }
    }

    let calmar = if max_dd > 0.0 {
        annual_return / max_dd
    } else {
        0.0
    };

    let win_rate = if !daily_rets.is_empty() {
        daily_rets.iter().filter(|r| **r > 0.0).count() as f64 / daily_rets.len() as f64
    } else {
        0.0
    };

    let avg_turnover = if !turnover.is_empty() {
        turnover.iter().map(|t| t.turnover).sum::<f64>() / turnover.len() as f64
    } else {
        0.0
    };
    let rebalances_per_year = turnover.len() as f64 / n_years.max(0.01);
    let annual_turnover = avg_turnover * rebalances_per_year;

    // Benchmark comparison
    let (bm_total, bm_annual, excess) = if !benchmark_nav.is_empty() {
        let bm_vals: Vec<f64> = benchmark_nav.iter().map(|(_, n)| *n).collect();
        let bm_ret = bm_vals.last().unwrap() / bm_vals[0] - 1.0;
        let bm_ann = (1.0 + bm_ret).powf(1.0 / n_years.max(0.01)) - 1.0;
        (bm_ret, bm_ann, annual_return - bm_ann)
    } else {
        (0.0, 0.0, 0.0)
    };

    PerformanceStats {
        total_return,
        annual_return,
        annual_volatility: annual_vol,
        sharpe_ratio: sharpe,
        max_drawdown: -max_dd,
        max_drawdown_date: max_dd_date.to_string(),
        calmar_ratio: calmar,
        win_rate,
        total_trades: trades.len(),
        avg_turnover,
        annual_turnover,
        trade_days: n_days,
        benchmark_total_return: bm_total,
        benchmark_annual_return: bm_annual,
        excess_annual_return: excess,
    }
}
