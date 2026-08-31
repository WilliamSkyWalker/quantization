//! A-share execution primitives — pure functions shared by backtest engine
//! and paper/live trader. **Critical invariant**: backtest, paper, and any
//! future live broker must execute through these same primitives so paper
//! results match backtest exactly.

use chrono::NaiveDate;
use rustc_hash::FxHashMap;

use quant_factors::a_share::cache::{AShareCache, ABar};

// Re-exported so downstream crates (trading) don't need to depend on `a_engine` directly.
pub use crate::a_engine::{ACostConfig, is_limit_up_one_char, is_limit_down_one_char};

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Side {
    Buy,
    Sell,
}

impl Side {
    pub fn as_str(&self) -> &'static str {
        match self { Side::Buy => "BUY", Side::Sell => "SELL" }
    }
}

#[derive(Debug, Clone)]
pub struct OrderIntent {
    pub ts_code: String,
    pub side: Side,
    /// Always positive — shares to trade.
    pub shares: i64,
}

#[derive(Debug, Clone)]
pub struct Fill {
    pub ts_code: String,
    pub side: Side,
    pub shares: i64,
    pub price: f64,
    /// shares * price (no fees).
    pub gross: f64,
    pub fees: f64,
}

/// Read-only snapshot of quotes + per-stock flags needed for execution checks.
/// Backtest uses `CachedQuotes`; paper/live trader supplies its own impl from
/// real-time data.
pub trait QuoteSource {
    fn bar(&self, ts_code: &str) -> Option<&ABar>;
    fn is_st(&self, ts_code: &str) -> bool;
}

/// AShareCache + date adapter.
pub struct CachedQuotes<'a> {
    pub cache: &'a AShareCache,
    pub date: NaiveDate,
}

impl<'a> QuoteSource for CachedQuotes<'a> {
    fn bar(&self, ts_code: &str) -> Option<&ABar> {
        self.cache.get_bar(ts_code, self.date)
    }
    fn is_st(&self, ts_code: &str) -> bool {
        self.cache.is_st(ts_code)
    }
}

// ── Cost helpers (shared with a_engine) ─────────────────────────────────

pub fn round_to_lot(shares: i64, lot_size: i64) -> i64 {
    (shares / lot_size) * lot_size
}

pub fn calc_buy_fees(amount: f64, config: &ACostConfig) -> f64 {
    (amount * config.buy_commission).max(config.min_commission)
}

pub fn calc_sell_fees(amount: f64, config: &ACostConfig) -> f64 {
    let commission = (amount * config.sell_commission).max(config.min_commission);
    let stamp = amount * config.stamp_tax;
    commission + stamp
}

// ── Portfolio valuation ─────────────────────────────────────────────────

/// Mark-to-market: cash + Σ(shares × close) using `quotes`.
pub fn portfolio_value<Q: QuoteSource>(
    positions: &FxHashMap<String, i64>,
    quotes: &Q,
    cash: f64,
) -> f64 {
    let mut value = cash;
    for (code, &shares) in positions {
        if let Some(bar) = quotes.bar(code) {
            value += shares as f64 * bar.close;
        }
    }
    value
}

// ── Order planning ──────────────────────────────────────────────────────

/// Plan orders to move from current `positions` to `target_weights`.
/// Returns sells first then buys (so cash is freed up before purchases).
///
/// Skips:
///   - tickers without a bar (no quote → no execution)
///   - target_weights with no positive entry (treated as exit)
pub fn plan_orders<Q: QuoteSource>(
    positions: &FxHashMap<String, i64>,
    target_weights: &FxHashMap<String, f64>,
    total_value: f64,
    quotes: &Q,
    config: &ACostConfig,
) -> Vec<OrderIntent> {
    let mut orders = Vec::new();

    // Phase 1: Sells — exit removed names and reduce overweight positions.
    for (code, &shares) in positions {
        if shares <= 0 {
            continue;
        }
        let target = target_weights.get(code).copied().unwrap_or(0.0);
        let shares_to_sell = if target <= 0.0 {
            shares
        } else {
            quotes.bar(code)
                .filter(|bar| bar.open > 0.0)
                .map(|bar| {
                    let target_shares = round_to_lot(
                        (total_value * target / bar.open) as i64,
                        config.lot_size,
                    );
                    (shares - target_shares).max(0)
                })
                .unwrap_or(0)
        };
        if shares_to_sell == 0 {
            continue;
        }
        // Don't gate on quote here — execute_orders will skip if missing/limit-down.
        orders.push(OrderIntent {
            ts_code: code.clone(),
            side: Side::Sell,
            shares: shares_to_sell,
        });
    }

    // Phase 2: Buys — target_weight > 0, target_shares > current_shares.
    for (code, &target_w) in target_weights {
        if target_w <= 0.0 { continue; }
        let bar = match quotes.bar(code) { Some(b) => b, None => continue };
        let exec_price = bar.open * (1.0 + config.slippage);
        if exec_price <= 0.0 { continue; }
        let target_value = total_value * target_w;
        let target_shares = round_to_lot((target_value / exec_price) as i64, config.lot_size);
        let current = positions.get(code).copied().unwrap_or(0);
        let delta = target_shares - current;
        if delta <= 0 { continue; }
        orders.push(OrderIntent {
            ts_code: code.clone(),
            side: Side::Buy,
            shares: delta,
        });
    }

    orders
}

// ── Order execution ─────────────────────────────────────────────────────

/// Execute orders against `quotes`, mutating `positions` and `cash`.
/// Returns the fills that succeeded (may be fewer than orders).
///
/// Skip rules (silent — caller logs if needed):
///   - Sell: missing bar, limit-down one-char, position already gone
///   - Buy:  missing bar, limit-up one-char, exec_price <= 0, insufficient cash
pub fn execute_orders<Q: QuoteSource>(
    orders: &[OrderIntent],
    positions: &mut FxHashMap<String, i64>,
    cash: &mut f64,
    quotes: &Q,
    config: &ACostConfig,
) -> Vec<Fill> {
    let mut fills = Vec::new();

    for order in orders {
        let bar = match quotes.bar(&order.ts_code) { Some(b) => b, None => continue };
        let st = quotes.is_st(&order.ts_code);

        match order.side {
            Side::Sell => {
                if is_limit_down_one_char(&order.ts_code, bar, st, &config.market_rules) {
                    continue;
                }
                let held = positions.get(&order.ts_code).copied().unwrap_or(0);
                let shares = order.shares.min(held);
                if shares <= 0 { continue; }
                let price = bar.open * (1.0 - config.slippage);
                let gross = shares as f64 * price;
                let fees = calc_sell_fees(gross, config);
                *cash += gross - fees;
                if shares >= held {
                    positions.remove(&order.ts_code);
                } else {
                    *positions.entry(order.ts_code.clone()).or_insert(0) -= shares;
                }
                fills.push(Fill {
                    ts_code: order.ts_code.clone(),
                    side: Side::Sell,
                    shares, price, gross, fees,
                });
            }
            Side::Buy => {
                if is_limit_up_one_char(&order.ts_code, bar, st, &config.market_rules) {
                    continue;
                }
                let price = bar.open * (1.0 + config.slippage);
                if price <= 0.0 { continue; }
                let gross = order.shares as f64 * price;
                let fees = calc_buy_fees(gross, config);
                let total_cost = gross + fees;
                if total_cost > *cash { continue; }
                *cash -= total_cost;
                *positions.entry(order.ts_code.clone()).or_insert(0) += order.shares;
                fills.push(Fill {
                    ts_code: order.ts_code.clone(),
                    side: Side::Buy,
                    shares: order.shares, price, gross, fees,
                });
            }
        }
    }

    fills
}

#[cfg(test)]
mod tests {
    use super::*;
    use quant_core::config::AShareConfig;
    use quant_factors::a_share::cache::{AShareCache, AStockInfo};

    fn d(s: &str) -> NaiveDate {
        NaiveDate::parse_from_str(s, "%Y-%m-%d").unwrap()
    }

    fn bar(open: f64, close: f64, pct_chg: f64) -> ABar {
        ABar {
            open, high: close.max(open), low: close.min(open), close, pre_close: open,
            pct_chg, vol: 1000.0, amount: 1e7, adj_factor: 1.0,
            turnover_rate: 0.0, pe_ttm: 0.0, pb: 0.0, ps_ttm: 0.0, dv_ttm: 0.0,
            total_mv: 1e6, circ_mv: 1e6,
        }
    }

    fn cache_with(code: &str, date: NaiveDate, b: ABar, is_st: bool) -> AShareCache {
        let mut c = AShareCache {
            daily: FxHashMap::default(),
            financials: FxHashMap::default(),
            industry: FxHashMap::default(),
            basics: FxHashMap::default(),
            trading_days: vec![date],
            index_prices: FxHashMap::default(),
            ts_codes: vec![code.to_string()],
            top_list: FxHashMap::default(),
            margin_detail: FxHashMap::default(),
        };
        c.daily.insert(code.to_string(), vec![(date, b)]);
        c.basics.insert(code.to_string(), AStockInfo {
            name: code.into(), list_date: None, delist_date: None,
            is_st, board: None, total_share: None, free_share: None,
        });
        c
    }

    #[test]
    fn buy_then_sell_round_trip() {
        let date = d("2024-07-15");
        let b = bar(10.0, 10.5, 5.0);
        let cache = cache_with("600519.SH", date, b, false);
        let cfg = ACostConfig::from_a_share(&AShareConfig::default());
        let q = CachedQuotes { cache: &cache, date };

        let mut positions = FxHashMap::default();
        let mut cash = 100_000.0;

        // Buy: target 50% weight
        let mut weights = FxHashMap::default();
        weights.insert("600519.SH".to_string(), 0.5);
        let orders = plan_orders(&positions, &weights, cash, &q, &cfg);
        let fills = execute_orders(&orders, &mut positions, &mut cash, &q, &cfg);
        assert_eq!(fills.len(), 1);
        assert_eq!(fills[0].side, Side::Buy);
        assert!(positions["600519.SH"] >= 100);  // at least 1 lot
        assert!(cash < 100_000.0);

        // Sell all: target 0
        let weights = FxHashMap::default();
        let total = portfolio_value(&positions, &q, cash);
        let orders = plan_orders(&positions, &weights, total, &q, &cfg);
        let fills = execute_orders(&orders, &mut positions, &mut cash, &q, &cfg);
        assert_eq!(fills.len(), 1);
        assert_eq!(fills[0].side, Side::Sell);
        assert!(positions.is_empty());
    }

    #[test]
    fn rebalance_sells_excess_shares_to_target_weight() {
        let date = d("2024-07-15");
        let cache = cache_with("600519.SH", date, bar(10.0, 10.0, 0.0), false);
        let cfg = ACostConfig::from_a_share(&AShareConfig::default());
        let q = CachedQuotes { cache: &cache, date };

        let mut positions = FxHashMap::default();
        positions.insert("600519.SH".to_string(), 1_000);
        let mut cash = 0.0;
        let mut weights = FxHashMap::default();
        weights.insert("600519.SH".to_string(), 0.5);

        let total = portfolio_value(&positions, &q, cash);
        let orders = plan_orders(&positions, &weights, total, &q, &cfg);
        assert_eq!(orders.len(), 1);
        assert_eq!(orders[0].side, Side::Sell);
        assert_eq!(orders[0].shares, 500);

        let fills = execute_orders(&orders, &mut positions, &mut cash, &q, &cfg);
        assert_eq!(fills.len(), 1);
        assert_eq!(positions["600519.SH"], 500);
        assert!(cash > 0.0);
    }

    #[test]
    fn buy_blocked_by_limit_up() {
        let date = d("2024-07-15");
        // 主板 9.6% 涨幅一字板 — 阻止买入
        let b = bar(11.0, 11.0, 9.6);
        let cache = cache_with("600519.SH", date, b, false);
        let cfg = ACostConfig::from_a_share(&AShareConfig::default());
        let q = CachedQuotes { cache: &cache, date };

        let mut positions = FxHashMap::default();
        let mut cash = 100_000.0;
        let mut weights = FxHashMap::default();
        weights.insert("600519.SH".to_string(), 0.5);
        let orders = plan_orders(&positions, &weights, cash, &q, &cfg);
        let fills = execute_orders(&orders, &mut positions, &mut cash, &q, &cfg);
        assert!(fills.is_empty(), "limit-up should block buy");
        assert!(positions.is_empty());
    }

    #[test]
    fn sell_blocked_by_limit_down() {
        let date = d("2024-07-15");
        let b = bar(9.0, 9.0, -9.6);
        let cache = cache_with("600519.SH", date, b, false);
        let cfg = ACostConfig::from_a_share(&AShareConfig::default());
        let q = CachedQuotes { cache: &cache, date };

        let mut positions = FxHashMap::default();
        positions.insert("600519.SH".to_string(), 200);
        let mut cash = 0.0;

        let weights = FxHashMap::default();  // target 0 = sell all
        let total = portfolio_value(&positions, &q, cash);
        let orders = plan_orders(&positions, &weights, total, &q, &cfg);
        let fills = execute_orders(&orders, &mut positions, &mut cash, &q, &cfg);
        assert!(fills.is_empty(), "limit-down should block sell");
        assert_eq!(positions["600519.SH"], 200);
    }

    #[test]
    fn buy_skipped_when_cash_insufficient() {
        let date = d("2024-07-15");
        let b = bar(10.0, 10.0, 0.0);
        let cache = cache_with("600519.SH", date, b, false);
        let cfg = ACostConfig::from_a_share(&AShareConfig::default());
        let q = CachedQuotes { cache: &cache, date };

        let mut positions = FxHashMap::default();
        let mut cash = 50.0;  // way too little
        let mut weights = FxHashMap::default();
        weights.insert("600519.SH".to_string(), 1.0);
        let orders = plan_orders(&positions, &weights, 100_000.0, &q, &cfg);
        let fills = execute_orders(&orders, &mut positions, &mut cash, &q, &cfg);
        assert!(fills.is_empty());
    }
}
