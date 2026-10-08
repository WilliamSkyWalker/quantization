//! Two-asset Cover mixture, learned at close and executed at the next open.
//! Discrete uniform prior over constant-rebalanced experts; not follow-the-leader.
use crate::a_exec::{self, ACostConfig, QuoteSource, Side};
use chrono::{Datelike, NaiveDate};
pub use quant_factors::a_share::cache::ABar;
use rustc_hash::FxHashMap;
use serde::Serialize;

#[derive(Clone)]
pub struct Day {
    pub date: NaiveDate,
    pub bars: [Option<ABar>; 2],
}

#[derive(Clone, Copy)]
pub enum Strategy {
    Universal,
    Fixed(f64, usize),
    Hold(f64),
}

#[derive(Serialize)]
pub struct Point {
    pub date: NaiveDate,
    pub nav: f64,
    pub target_first: f64,
    pub actual_first: f64,
    pub cash: f64,
}

#[derive(Serialize)]
pub struct Result {
    pub total_return: f64,
    pub annual_return: f64,
    pub max_drawdown: f64,
    pub sharpe_zero_rf: f64,
    pub annual_traded_notional_over_nav: f64,
    pub fees: f64,
    pub fills: usize,
    pub blocked_open_days: usize,
    pub missing_pair_days: usize,
    pub yearly_returns: std::collections::BTreeMap<i32, f64>,
    pub points: Vec<Point>,
}

struct Quotes<'a> {
    codes: &'a [String; 2],
    bars: [Option<ABar>; 2],
}
impl QuoteSource for Quotes<'_> {
    fn bar(&self, code: &str) -> Option<&ABar> {
        self.codes
            .iter()
            .position(|c| c == code)
            .and_then(|i| self.bars[i].as_ref())
    }
    fn is_st(&self, _: &str) -> bool {
        false
    }
}

pub fn mixture(logs: &[f64]) -> f64 {
    let max = logs.iter().copied().fold(f64::NEG_INFINITY, f64::max);
    let sum: f64 = logs.iter().map(|x| (x - max).exp()).sum();
    logs.iter()
        .enumerate()
        .map(|(i, x)| i as f64 / (logs.len() - 1) as f64 * (x - max).exp())
        .sum::<f64>()
        / sum
}

pub fn learn(logs: &mut [f64], relatives: [f64; 2]) {
    let n = (logs.len() - 1) as f64;
    for (i, log) in logs.iter_mut().enumerate() {
        let w = i as f64 / n;
        *log += (w * relatives[0] + (1.0 - w) * relatives[1]).ln();
    }
}

pub fn simulate(
    days: &[Day],
    codes: &[String; 2],
    config: &ACostConfig,
    strategy: Strategy,
    grid: usize,
) -> Result {
    simulate_seeded(days, codes, config, strategy, grid, None, None)
}

/// Seed the next-open strategy exclusively with the preceding training window.
pub fn simulate_seeded(
    days: &[Day],
    codes: &[String; 2],
    config: &ACostConfig,
    strategy: Strategy,
    grid: usize,
    seed: Option<&[f64]>,
    prior_adj: Option<[f64; 2]>,
) -> Result {
    assert!(days.len() >= 2 && grid >= 2);
    let mut logs = seed.map_or_else(|| vec![0.0; grid], |s| s.to_vec());
    assert_eq!(logs.len(), grid);
    let mut positions = FxHashMap::default();
    let mut cash = config.initial_capital;
    let mut last_adj = prior_adj.unwrap_or([0.0; 2]);
    let mut last_factor = [0.0; 2];
    let mut last_raw = [0.0; 2];
    let mut points = Vec::new();
    let (mut peak, mut prev_nav) = (cash, cash);
    let (mut dd, mut fees, mut turnover) = (0.0_f64, 0.0, 0.0);
    let (mut fills_count, mut blocked, mut missing) = (0, 0, 0);
    let mut daily = Vec::new();
    let mut initialized = false;
    let mut yearly = std::collections::BTreeMap::new();
    let mut target = match strategy {
        Strategy::Universal => mixture(&logs),
        Strategy::Fixed(w, _) | Strategy::Hold(w) => w,
    };
    for (t, day) in days.iter().enumerate() {
        // Adjustment-factor changes approximate total-return reinvestment. These
        // synthetic share credits are explicitly disclosed, not actual dividends.
        for i in 0..2 {
            if let Some(b) = &day.bars[i] {
                if last_factor[i] > 0.0 {
                    let held = positions.get(&codes[i]).copied().unwrap_or(0) as f64;
                    let adjusted = held * b.adj_factor / last_factor[i];
                    positions.insert(codes[i].clone(), adjusted.floor() as i64);
                    cash += adjusted.fract() * b.open;
                }
                last_factor[i] = b.adj_factor;
            }
        }
        let open_nav = cash
            + (0..2)
                .map(|i| {
                    positions.get(&codes[i]).copied().unwrap_or(0) as f64
                        * day.bars[i].as_ref().map_or(last_raw[i], |b| b.open)
                })
                .sum::<f64>();
        let rebalance = match strategy {
            Strategy::Universal => true,
            Strategy::Fixed(_, n) => t % n == 0,
            Strategy::Hold(_) => !initialized,
        };
        let complete = day.bars.iter().all(Option::is_some);
        if !complete {
            missing += 1;
        }
        // Conservative open-only 5% gate on both legs, covering historical ST
        // without using today's high/low/close to decide an opening fill.
        let blocked_open = day
            .bars
            .iter()
            .flatten()
            .any(|b| (b.open / b.pre_close - 1.0).abs() >= 0.045);
        if rebalance && complete && blocked_open {
            blocked += 1;
        }
        if rebalance && complete && !blocked_open {
            initialized = true;
            let mut bars = day.bars.clone();
            for b in bars.iter_mut().flatten() {
                b.high = b.open;
                b.low = b.open;
                b.close = b.open;
                b.pct_chg = (b.open / b.pre_close - 1.0) * 100.0;
            }
            let quotes = Quotes { codes, bars };
            let weights = FxHashMap::from_iter([
                (codes[0].clone(), target),
                (codes[1].clone(), 1.0 - target),
            ]);
            let orders = a_exec::plan_orders(&positions, &weights, open_nav, &quotes, config);
            // Shared executor rejects unaffordable full orders; trim buy lots
            // beforehand so one expensive second leg does not remain all cash.
            for mut order in orders {
                if order.side == Side::Buy {
                    let price = quotes.bar(&order.ts_code).unwrap().open * (1.0 + config.slippage);
                    while order.shares > 0
                        && order.shares as f64 * price
                            + a_exec::calc_buy_fees(order.shares as f64 * price, config)
                            > cash
                    {
                        order.shares -= config.lot_size;
                    }
                }
                if order.shares <= 0 {
                    continue;
                }
                for fill in
                    a_exec::execute_orders(&[order], &mut positions, &mut cash, &quotes, config)
                {
                    turnover += fill.gross / open_nav;
                    fees += fill.fees;
                    fills_count += 1;
                }
            }
        }
        let mut adj = last_adj;
        for i in 0..2 {
            if let Some(b) = &day.bars[i] {
                last_raw[i] = b.close;
                adj[i] = b.close * b.adj_factor;
            }
        }
        let nav = cash
            + (0..2)
                .map(|i| positions.get(&codes[i]).copied().unwrap_or(0) as f64 * last_raw[i])
                .sum::<f64>();
        assert!(nav.is_finite() && nav > 0.0 && cash >= -1e-6);
        let ret = nav / prev_nav - 1.0;
        daily.push(ret);
        *yearly.entry(day.date.year()).or_insert(1.0) *= 1.0 + ret;
        peak = peak.max(nav);
        dd = dd.min(nav / peak - 1.0);
        points.push(Point {
            date: day.date,
            nav,
            target_first: target,
            actual_first: positions.get(&codes[0]).copied().unwrap_or(0) as f64 * last_raw[0] / nav,
            cash,
        });
        // Today's close is used only for tomorrow's target. Missing quotes are
        // marked stale; no pair rebalance is attempted on a missing-quote day.
        if matches!(strategy, Strategy::Universal) && last_adj.iter().all(|x| *x > 0.0) {
            learn(&mut logs, [adj[0] / last_adj[0], adj[1] / last_adj[1]]);
        }
        if matches!(strategy, Strategy::Universal) {
            target = mixture(&logs);
        }
        last_adj = adj;
        prev_nav = nav;
    }
    let years = (days.last().unwrap().date - days[0].date).num_days() as f64 / 365.25;
    let mean = daily.iter().sum::<f64>() / daily.len() as f64;
    let variance = daily.iter().map(|r| (r - mean).powi(2)).sum::<f64>() / (daily.len() - 1) as f64;
    for r in yearly.values_mut() {
        *r -= 1.0;
    }
    Result {
        total_return: prev_nav / config.initial_capital - 1.0,
        annual_return: (prev_nav / config.initial_capital).powf(1.0 / years) - 1.0,
        max_drawdown: dd,
        sharpe_zero_rf: if variance > 0.0 {
            mean / variance.sqrt() * 252_f64.sqrt()
        } else {
            0.0
        },
        annual_traded_notional_over_nav: turnover / years,
        fees,
        fills: fills_count,
        blocked_open_days: blocked,
        missing_pair_days: missing,
        yearly_returns: yearly,
        points,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn mixture_equals_average_expert_wealth_and_uses_past_only() {
        let mut logs = vec![0.0; 101];
        let mut wealth = 1.0;
        for r in [[2.0, 1.0], [0.5, 1.0], [1.3, 0.8], [0.9, 1.2]] {
            let w = mixture(&logs);
            wealth *= w * r[0] + (1.0 - w) * r[1];
            learn(&mut logs, r);
        }
        let experts = logs.iter().map(|l| l.exp()).sum::<f64>() / 101.0;
        assert!((wealth - experts).abs() < 1e-12);
        assert!((1.5 * 0.75 - 1.125_f64).abs() < 1e-12);
    }
    #[test]
    fn stable_for_large_log_wealth() {
        assert!((mixture(&[10000.0, 10000.0, 10000.0]) - 0.5).abs() < 1e-12);
    }

    #[test]
    fn training_seed_controls_first_open_without_future_close() {
        let codes = ["600036.SH".into(), "600900.SH".into()];
        let mut seed = vec![0.0; 101];
        learn(&mut seed, [2.0, 0.5]);
        let mut days = fixture();
        let a = simulate_seeded(
            &days,
            &codes,
            &free(),
            Strategy::Universal,
            101,
            Some(&seed),
            Some([10.0, 10.0]),
        );
        days[0].bars[0].as_mut().unwrap().close = 12.0;
        let b = simulate_seeded(
            &days,
            &codes,
            &free(),
            Strategy::Universal,
            101,
            Some(&seed),
            Some([10.0, 10.0]),
        );
        assert!((a.points[0].target_first - mixture(&seed)).abs() < 1e-12);
        assert_eq!(a.points[0].cash, b.points[0].cash);
        assert_eq!(a.points[0].target_first, b.points[0].target_first);
        assert!(b.points[1].target_first > a.points[1].target_first);
    }

    fn bar(open: f64, close: f64, pre_close: f64) -> ABar {
        ABar {
            open,
            close,
            pre_close,
            high: open.max(close),
            low: open.min(close),
            pct_chg: (close / pre_close - 1.0) * 100.0,
            vol: 10000.0,
            amount: 100000.0,
            adj_factor: 1.0,
            turnover_rate: 0.0,
            pe_ttm: 0.0,
            pb: 0.0,
            ps_ttm: 0.0,
            dv_ttm: 0.0,
            total_mv: 0.0,
            circ_mv: 0.0,
        }
    }
    fn fixture() -> Vec<Day> {
        (0..5)
            .map(|i| Day {
                date: NaiveDate::from_ymd_opt(2020, 1, 6 + i).unwrap(),
                bars: [Some(bar(10.0, 10.0, 10.0)), Some(bar(10.0, 10.0, 10.0))],
            })
            .collect()
    }
    fn free() -> ACostConfig {
        let mut c = ACostConfig::default();
        c.initial_capital = 100000.0;
        c.buy_commission = 0.0;
        c.sell_commission = 0.0;
        c.stamp_tax = 0.0;
        c.slippage = 0.0;
        c.min_commission = 0.0;
        c
    }
    #[test]
    fn future_close_cannot_change_current_target_or_open_trade() {
        let codes = ["600036.SH".into(), "600900.SH".into()];
        let a = fixture();
        let mut b = a.clone();
        b[2].bars[0].as_mut().unwrap().close = 20.0;
        let x = simulate(&a, &codes, &free(), Strategy::Universal, 101);
        let y = simulate(&b, &codes, &free(), Strategy::Universal, 101);
        assert_eq!(x.points[2].target_first, y.points[2].target_first);
        assert_eq!(x.points[2].cash, y.points[2].cash);
        assert!(y.points[3].target_first > x.points[3].target_first);
        assert!((x.total_return).abs() < 1e-12);
    }
    #[test]
    fn missing_quotes_preserve_held_value_and_delay_initial_purchase() {
        let codes = ["600036.SH".into(), "600900.SH".into()];
        let mut days = fixture();
        days[2].bars[0] = None;
        let result = simulate(&days, &codes, &free(), Strategy::Universal, 101);
        assert_eq!(result.missing_pair_days, 1);
        assert!((result.points[2].nav - 100000.0).abs() < 1e-8);
        days[0].bars[0] = Some(bar(11.0, 11.0, 10.0));
        let hold = simulate(&days, &codes, &free(), Strategy::Hold(0.5), 101);
        assert_eq!(hold.points[0].cash, 100000.0);
        assert!(hold.points[1].cash < 100000.0);
    }
    #[test]
    fn synthetic_split_preserves_value_and_costs_reduce_wealth() {
        let codes = ["600036.SH".into(), "600900.SH".into()];
        let mut days = fixture();
        for day in &mut days[2..] {
            let mut b = bar(5.0, 5.0, 5.0);
            b.adj_factor = 2.0;
            day.bars[0] = Some(b);
        }
        let zero = simulate(&days, &codes, &free(), Strategy::Hold(0.5), 101);
        assert!(zero.total_return.abs() < 1e-12);
        let mut cost = free();
        cost.buy_commission = 0.001;
        cost.min_commission = 5.0;
        let net = simulate(&days, &codes, &cost, Strategy::Hold(0.5), 101);
        assert!(net.total_return < zero.total_return && net.fees > 0.0);
    }
}
