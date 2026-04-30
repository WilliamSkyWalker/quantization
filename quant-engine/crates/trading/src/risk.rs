//! Pre-trade risk gate — applied between `plan_orders` and `execute_orders`.
//!
//! Rules (config-driven, all caps from `AShareStrategyConfig`):
//!   1. Single position weight ≤ `max_single_weight`
//!   2. Industry weight ≤ `max_industry_weight` (when industry mapping available)
//!   3. Industry-group weight ≤ `max_industry_group_weight` (TMT / 地产链 / 金融)
//!   4. Cash sufficiency (rough estimate; broker re-checks at fill)
//!
//! Violations downscale or drop the offending buy order rather than abort
//! the whole rebalance — caller logs `RiskAction::Skipped`.

use rustc_hash::FxHashMap;
use tracing::warn;

use quant_backtest::a_exec::{ACostConfig, OrderIntent, QuoteSource, Side};
use quant_core::config::AShareStrategyConfig;
use quant_factors::a_share::cache::AShareCache;

#[derive(Debug, Clone)]
pub struct RiskConfig {
    pub max_single_weight: f64,
    pub max_industry_weight: f64,
    pub max_industry_group_weight: f64,
    /// Group name → list of L1 industry names that count as one bucket.
    pub industry_groups: FxHashMap<String, Vec<String>>,
}

impl RiskConfig {
    pub fn from_strategy(s: &AShareStrategyConfig) -> Self {
        let mut groups = FxHashMap::default();
        groups.insert("地产链".to_string(),
            vec!["房地产".into(), "建筑装饰".into(), "建筑材料".into()]);
        groups.insert("金融".to_string(),
            vec!["银行".into(), "非银金融".into()]);
        groups.insert("TMT".to_string(),
            vec!["计算机".into(), "电子".into(), "通信".into(), "传媒".into()]);
        Self {
            max_single_weight: s.max_single_weight,
            max_industry_weight: s.max_industry_weight,
            max_industry_group_weight: s.max_industry_group_weight,
            industry_groups: groups,
        }
    }
}

#[derive(Debug, Clone, PartialEq)]
pub enum RiskAction {
    Pass,
    Skipped { reason: String },
    Downscaled { from: i64, to: i64, reason: String },
}

#[derive(Debug)]
pub struct RiskReport {
    /// Per-order action, same order as input.
    pub actions: Vec<RiskAction>,
}

impl RiskReport {
    pub fn skipped_count(&self) -> usize {
        self.actions.iter().filter(|a| matches!(a, RiskAction::Skipped { .. })).count()
    }
    pub fn downscaled_count(&self) -> usize {
        self.actions.iter().filter(|a| matches!(a, RiskAction::Downscaled { .. })).count()
    }
}

/// Apply risk rules to a planned order list. Returns (filtered_orders, report).
///
/// `total_value` is the post-rebalance NAV estimate used to translate weights.
/// `current_positions` is pre-rebalance shares (used for accumulating exposure).
pub fn apply<Q: QuoteSource>(
    orders: &[OrderIntent],
    current_positions: &FxHashMap<String, i64>,
    cash: f64,
    total_value: f64,
    quotes: &Q,
    cache: &AShareCache,
    config: &RiskConfig,
    cost: &ACostConfig,
) -> (Vec<OrderIntent>, RiskReport) {
    let mut out = Vec::with_capacity(orders.len());
    let mut actions = Vec::with_capacity(orders.len());

    // Track running exposure in CNY (post-fill) per ts_code, industry, group.
    let mut exposure: FxHashMap<String, f64> = current_value_by_code(current_positions, quotes);
    let mut industry_exp: FxHashMap<String, f64> = aggregate_by_industry(&exposure, cache);
    let mut group_exp: FxHashMap<String, f64> = aggregate_by_group(&industry_exp, &config.industry_groups);
    let mut available_cash = cash;

    let max_single = total_value * config.max_single_weight;
    let max_industry = total_value * config.max_industry_weight;
    let max_group = total_value * config.max_industry_group_weight;

    for order in orders {
        let bar = match quotes.bar(&order.ts_code) {
            Some(b) => b,
            None => {
                actions.push(RiskAction::Skipped { reason: "no quote".into() });
                continue;
            }
        };

        match order.side {
            Side::Sell => {
                // Sells reduce exposure & free cash — never blocked by risk gate.
                let price = bar.open * (1.0 - cost.slippage);
                let proceeds = order.shares as f64 * price;
                available_cash += proceeds * (1.0 - cost.sell_commission - cost.stamp_tax);
                let curr = exposure.get(&order.ts_code).copied().unwrap_or(0.0);
                let new_curr = (curr - order.shares as f64 * bar.close).max(0.0);
                exposure.insert(order.ts_code.clone(), new_curr);
                rebuild_aggregates(&exposure, cache, &config.industry_groups,
                                   &mut industry_exp, &mut group_exp);
                out.push(order.clone());
                actions.push(RiskAction::Pass);
            }
            Side::Buy => {
                let price = bar.open * (1.0 + cost.slippage);
                let want_value = order.shares as f64 * price;
                let curr_single = exposure.get(&order.ts_code).copied().unwrap_or(0.0);

                // Determine industry / group keys.
                let ind = cache.industry.get(&order.ts_code).map(|i| i.industry_name.clone());
                let grp = ind.as_ref().and_then(|n| group_for_industry(n, &config.industry_groups));

                let curr_industry = ind.as_ref()
                    .and_then(|n| industry_exp.get(n).copied()).unwrap_or(0.0);
                let curr_group = grp.as_ref()
                    .and_then(|g| group_exp.get(g).copied()).unwrap_or(0.0);

                // How much can we add given each cap?
                let cap_single = (max_single - curr_single).max(0.0);
                let cap_industry = (max_industry - curr_industry).max(0.0);
                let cap_group = (max_group - curr_group).max(0.0);
                let allowed_value = want_value.min(cap_single).min(cap_industry).min(cap_group);

                let allowed_shares = round_to_lot((allowed_value / price) as i64, cost.lot_size);

                // Cash check (rough: include est. fees).
                let est_cost = allowed_shares as f64 * price * (1.0 + cost.buy_commission);
                let allowed_shares = if est_cost > available_cash {
                    let max_by_cash = ((available_cash / (price * (1.0 + cost.buy_commission))) as i64).max(0);
                    round_to_lot(max_by_cash, cost.lot_size)
                } else {
                    allowed_shares
                };

                if allowed_shares <= 0 {
                    let reason = pick_block_reason(want_value, cap_single, cap_industry, cap_group, available_cash);
                    warn!("risk: {} buy {} shares blocked ({})", order.ts_code, order.shares, reason);
                    actions.push(RiskAction::Skipped { reason });
                    continue;
                }

                if allowed_shares < order.shares {
                    let reason = pick_block_reason(want_value, cap_single, cap_industry, cap_group, available_cash);
                    actions.push(RiskAction::Downscaled {
                        from: order.shares, to: allowed_shares, reason,
                    });
                    out.push(OrderIntent {
                        ts_code: order.ts_code.clone(),
                        side: Side::Buy,
                        shares: allowed_shares,
                    });
                } else {
                    actions.push(RiskAction::Pass);
                    out.push(order.clone());
                }

                // Update running aggregates.
                let actual_shares = out.last().map(|o| o.shares).unwrap_or(0);
                let actual_value = actual_shares as f64 * price;
                *exposure.entry(order.ts_code.clone()).or_insert(0.0) += actual_value;
                if let Some(n) = ind {
                    *industry_exp.entry(n).or_insert(0.0) += actual_value;
                }
                if let Some(g) = grp {
                    *group_exp.entry(g).or_insert(0.0) += actual_value;
                }
                available_cash -= actual_value * (1.0 + cost.buy_commission);
            }
        }
    }

    (out, RiskReport { actions })
}

// ── Helpers ─────────────────────────────────────────────────────────────

fn round_to_lot(shares: i64, lot_size: i64) -> i64 {
    (shares / lot_size) * lot_size
}

fn current_value_by_code<Q: QuoteSource>(
    positions: &FxHashMap<String, i64>,
    quotes: &Q,
) -> FxHashMap<String, f64> {
    let mut m = FxHashMap::default();
    for (code, &shares) in positions {
        let v = quotes.bar(code).map(|b| shares as f64 * b.close).unwrap_or(0.0);
        m.insert(code.clone(), v);
    }
    m
}

fn aggregate_by_industry(
    exposure: &FxHashMap<String, f64>,
    cache: &AShareCache,
) -> FxHashMap<String, f64> {
    let mut m: FxHashMap<String, f64> = FxHashMap::default();
    for (code, &v) in exposure {
        if let Some(ind) = cache.industry.get(code) {
            *m.entry(ind.industry_name.clone()).or_insert(0.0) += v;
        }
    }
    m
}

fn aggregate_by_group(
    industry_exp: &FxHashMap<String, f64>,
    groups: &FxHashMap<String, Vec<String>>,
) -> FxHashMap<String, f64> {
    let mut m: FxHashMap<String, f64> = FxHashMap::default();
    for (group_name, members) in groups {
        let total: f64 = members.iter()
            .map(|n| industry_exp.get(n).copied().unwrap_or(0.0))
            .sum();
        m.insert(group_name.clone(), total);
    }
    m
}

fn rebuild_aggregates(
    exposure: &FxHashMap<String, f64>,
    cache: &AShareCache,
    groups: &FxHashMap<String, Vec<String>>,
    industry_exp: &mut FxHashMap<String, f64>,
    group_exp: &mut FxHashMap<String, f64>,
) {
    *industry_exp = aggregate_by_industry(exposure, cache);
    *group_exp = aggregate_by_group(industry_exp, groups);
}

fn group_for_industry(industry: &str, groups: &FxHashMap<String, Vec<String>>) -> Option<String> {
    for (g, members) in groups {
        if members.iter().any(|m| m == industry) {
            return Some(g.clone());
        }
    }
    None
}

fn pick_block_reason(want: f64, cap_s: f64, cap_i: f64, cap_g: f64, cash: f64) -> String {
    let est_cost = want;
    if est_cost > cash { return format!("insufficient cash ({:.0} < {:.0})", cash, est_cost); }
    let m = want.min(cap_s).min(cap_i).min(cap_g);
    if (m - cap_s).abs() < 1e-6 { return format!("single-position cap (cap={:.0})", cap_s); }
    if (m - cap_i).abs() < 1e-6 { return format!("industry cap (cap={:.0})", cap_i); }
    if (m - cap_g).abs() < 1e-6 { return format!("industry-group cap (cap={:.0})", cap_g); }
    "ok".to_string()
}

#[cfg(test)]
mod tests {
    use super::*;
    use chrono::NaiveDate;
    use quant_backtest::a_exec::CachedQuotes;
    use quant_factors::a_share::cache::{ABar, AShareCache, AStockInfo, AIndustry};
    use quant_core::config::AShareConfig;

    fn d() -> NaiveDate { NaiveDate::from_ymd_opt(2024, 7, 15).unwrap() }
    fn bar(open: f64, close: f64) -> ABar {
        ABar { open, high: close.max(open), low: close.min(open), close, pre_close: open,
               pct_chg: 0.0, vol: 1000.0, amount: 1e7, adj_factor: 1.0,
               turnover_rate: 0.0, pe_ttm: 0.0, pb: 0.0, ps_ttm: 0.0, dv_ttm: 0.0,
               total_mv: 1e6, circ_mv: 1e6 }
    }
    fn make_cache(specs: &[(&str, &str, ABar)]) -> AShareCache {
        let mut c = AShareCache {
            daily: FxHashMap::default(), financials: FxHashMap::default(),
            industry: FxHashMap::default(), basics: FxHashMap::default(),
            trading_days: vec![d()], index_prices: FxHashMap::default(),
            ts_codes: vec![],
        };
        for (code, ind, b) in specs {
            c.ts_codes.push(code.to_string());
            c.daily.insert(code.to_string(), vec![(d(), b.clone())]);
            c.basics.insert(code.to_string(), AStockInfo {
                name: code.to_string().into(), list_date: None, delist_date: None,
                is_st: false, board: None, total_share: None,
            });
            c.industry.insert(code.to_string(), AIndustry {
                index_code: "X".into(), industry_name: ind.to_string(),
            });
        }
        c
    }

    #[test]
    fn single_position_cap_downscales() {
        let cache = make_cache(&[("600519.SH", "食品饮料", bar(100.0, 100.0))]);
        let q = CachedQuotes { cache: &cache, date: d() };
        let cost = ACostConfig::from_a_share(&AShareConfig::default());
        let cfg = RiskConfig::from_strategy(&AShareConfig::default().strategy);
        let positions = FxHashMap::default();
        let cash = 1_000_000.0;
        let total_value = 1_000_000.0;

        // Try to buy 5000 shares = 500_000 yuan = 50% (cap is 12%)
        let orders = vec![OrderIntent {
            ts_code: "600519.SH".into(), side: Side::Buy, shares: 5000,
        }];
        let (out, report) = apply(&orders, &positions, cash, total_value, &q, &cache, &cfg, &cost);
        assert_eq!(out.len(), 1);
        assert!(out[0].shares < 5000, "should downscale to single-cap");
        // 12% of 1M = 120K → at price ~100, ~1200 shares (rounded to 100 lot)
        assert!(out[0].shares <= 1200);
        assert_eq!(report.downscaled_count(), 1);
    }

    #[test]
    fn industry_cap_blocks_concentrated_buy() {
        let cache = make_cache(&[
            ("000001.SZ", "银行", bar(10.0, 10.0)),
            ("600036.SH", "银行", bar(40.0, 40.0)),
        ]);
        let q = CachedQuotes { cache: &cache, date: d() };
        let cost = ACostConfig::from_a_share(&AShareConfig::default());
        let cfg = RiskConfig::from_strategy(&AShareConfig::default().strategy);
        // Existing position: 18% in 银行 already
        let mut positions = FxHashMap::default();
        positions.insert("000001.SZ".to_string(), 18000); // 18000 * 10 = 180K = 18%
        let cash = 1_000_000.0 - 180_000.0;
        let total_value = 1_000_000.0;

        // Try to add another 银行 position → industry cap is 20%, only 2% left
        let orders = vec![OrderIntent {
            ts_code: "600036.SH".into(), side: Side::Buy, shares: 1000,  // 40K = 4%
        }];
        let (out, _) = apply(&orders, &positions, cash, total_value, &q, &cache, &cfg, &cost);
        // Should downscale to ~2% = 20K = 500 shares, rounded to 500 lots = 500
        assert!(!out.is_empty());
        assert!(out[0].shares <= 500, "got {} shares, expected ≤500 due to industry cap", out[0].shares);
    }

    #[test]
    fn sells_pass_unmodified() {
        let cache = make_cache(&[("600519.SH", "食品饮料", bar(100.0, 100.0))]);
        let q = CachedQuotes { cache: &cache, date: d() };
        let cost = ACostConfig::from_a_share(&AShareConfig::default());
        let cfg = RiskConfig::from_strategy(&AShareConfig::default().strategy);
        let mut positions = FxHashMap::default();
        positions.insert("600519.SH".to_string(), 1000);

        let orders = vec![OrderIntent {
            ts_code: "600519.SH".into(), side: Side::Sell, shares: 1000,
        }];
        let (out, report) = apply(&orders, &positions, 0.0, 1_000_000.0, &q, &cache, &cfg, &cost);
        assert_eq!(out.len(), 1);
        assert_eq!(out[0].shares, 1000);
        assert_eq!(report.skipped_count(), 0);
    }

    #[test]
    fn cash_insufficient_skips() {
        let cache = make_cache(&[("600519.SH", "食品饮料", bar(100.0, 100.0))]);
        let q = CachedQuotes { cache: &cache, date: d() };
        let cost = ACostConfig::from_a_share(&AShareConfig::default());
        let cfg = RiskConfig::from_strategy(&AShareConfig::default().strategy);
        let positions = FxHashMap::default();

        // Want to buy but only 500 yuan cash
        let orders = vec![OrderIntent {
            ts_code: "600519.SH".into(), side: Side::Buy, shares: 100,
        }];
        let (out, report) = apply(&orders, &positions, 500.0, 1_000_000.0, &q, &cache, &cfg, &cost);
        assert!(out.is_empty() || out[0].shares == 0);
        assert_eq!(report.skipped_count() + report.downscaled_count(), 1);
    }
}
