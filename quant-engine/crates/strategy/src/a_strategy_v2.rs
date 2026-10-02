//! A-share multi-factor strategy — v2, sentiment-driven (in development).
//!
//! Successor to the archived financial-driven strategy in `a_strategy.rs`
//! (frozen at git tag `a-share-strategy-v1-financial-archive`). See that
//! module's doc comment for the full v1 diagnosis and rationale for this
//! rewrite.
//!
//! Research baseline: lagged money-flow factors, deterministic top-N selection,
//! equal target slots with stock/industry caps, and configurable rebalance cadence.
//! No fallback to the archived financial strategy. Unallocated weight stays cash.

use std::collections::HashMap;

use chrono::NaiveDate;
use rayon::prelude::*;
use rustc_hash::{FxHashMap, FxHashSet};
use tracing::{info, warn};
use quant_factors::a_share::universe::{AUniverseFilter, get_a_clean_universe};

use quant_core::config::{AShareRegimeConfig, AShareStrategyConfig, AShareUniverseConfig};
use quant_factors::a_share::cache::AShareCache;
use quant_factors::a_share::factors_v2::all_factors_v2;

use crate::a_strategy::{aggregate_score_details, category_weights, winsorize_zscore_public, FactorValues, ScoreDetails};

/// Compute v2 (sentiment-driven) composite scores with per-category
/// breakdown, for use by the offline IC / Fama-MacBeth validation scripts.
/// Used by the v2 signal generator and offline factor validation.
///
/// Mirrors `a_strategy::compute_scores_detail` (v1) but sources factors from
/// `factors_v2::all_factors_v2()`. Like v1, factors without a validated
/// static direction (`direction == 0`, currently `LHB_APPEARANCE_FREQ_20D`)
/// are excluded from scoring until IC analysis confirms a sign — they must
/// not silently contribute to the score with an assumed direction.
///
/// Returns: ts_code → (total_score, HashMap<category, cat_score>)
pub fn compute_scores_v2_detail(
    date: NaiveDate,
    cache: &AShareCache,
    universe: Option<&FxHashSet<String>>,
    strategy: &AShareStrategyConfig,
    regime_overrides: Option<&HashMap<String, f64>>,
) -> ScoreDetails {
    let factors = all_factors_v2();
    let deferred_factors: Vec<&str> = factors.iter()
        .filter(|factor| !matches!(factor.direction, -1 | 1))
        .map(|factor| factor.name)
        .collect();
    if !deferred_factors.is_empty() {
        warn!(
            "A-share v2 scores exclude factors without a validated static direction: {}",
            deferred_factors.join(", "),
        );
    }

    let factor_values: FactorValues = factors.par_iter()
        .filter_map(|f| {
            if !matches!(f.direction, -1 | 1) {
                return None;
            }
            let mut raw = (f.compute)(date, cache);
            raw.retain(|code, value| value.is_finite() && universe.is_none_or(|u| u.contains(code)));
            if raw.is_empty() { return None; }
            let processed = winsorize_zscore_public(&raw);
            Some((f.name, f.category, f.direction, processed))
        })
        .collect();

    let weights = category_weights(strategy, regime_overrides);
    aggregate_score_details(&factor_values, universe, &weights, strategy.min_valid_categories)
}

/// Compute v2 composite scores without the per-category breakdown.
///
/// Returns: ts_code → score (higher = better).
pub fn compute_scores_v2(
    date: NaiveDate,
    cache: &AShareCache,
    universe: Option<&FxHashSet<String>>,
    strategy: &AShareStrategyConfig,
    regime_overrides: Option<&HashMap<String, f64>>,
) -> FxHashMap<String, f64> {
    compute_scores_v2_detail(date, cache, universe, strategy, regime_overrides)
        .into_iter()
        .map(|(code, (score, _))| (code, score))
        .collect()
}

/// Generate signals across the cache calendar. Data from the previous trading
/// day is used conservatively; the engine fills at the following day's open.
pub fn generate_signals_v2(
    cache: &AShareCache,
    n_holdings: usize,
    min_score: f64,
    universe_cfg: Option<&AShareUniverseConfig>,
    strategy: &AShareStrategyConfig,
    regime_cfg: Option<&AShareRegimeConfig>,
) -> std::collections::BTreeMap<NaiveDate, FxHashMap<String, f64>> {
    generate_signals_v2_for_dates(cache, &cache.trading_days, n_holdings, min_score,
        universe_cfg, strategy, regime_cfg)
}

/// A separate execution window keeps the full calendar available for warmup.
pub fn generate_signals_v2_for_dates(
    cache: &AShareCache,
    dates: &[NaiveDate],
    n_holdings: usize,
    min_score: f64,
    universe_cfg: Option<&AShareUniverseConfig>,
    strategy: &AShareStrategyConfig,
    regime_cfg: Option<&AShareRegimeConfig>,
) -> std::collections::BTreeMap<NaiveDate, FxHashMap<String, f64>> {
    assert_eq!(strategy.min_valid_categories, 1,
        "A-share v2 currently has one category; set min_valid_categories=1");
    assert!(strategy.rebalance_interval > 0, "rebalance_interval must be positive");
    let filter = universe_cfg.map(AUniverseFilter::from_config);
    let mut signals = std::collections::BTreeMap::new();
    for &date in dates.iter().step_by(strategy.rebalance_interval) {
        let pos = cache.trading_days.partition_point(|d| *d < date);
        if pos == 0 { continue; }
        let as_of = cache.trading_days[pos - 1];
        let universe = filter.as_ref().map(|f| get_a_clean_universe(date, cache, f));
        let scores = compute_scores_v2(as_of, cache, universe.as_ref(), strategy, None);
        let exposure = regime_cfg.filter(|c| c.enabled).map(|cfg| {
            let series = cache.index_prices.get(&cfg.index);
            let history: Vec<f64> = series.into_iter().flatten()
                .filter(|(d, p)| *d <= as_of && p.is_finite() && *p > 0.0)
                .map(|(_, p)| *p).collect();
            let n = cfg.ma_window;
            if n > 0 && history.len() >= n {
                let mean = history[history.len()-n..].iter().sum::<f64>() / n as f64;
                if history[history.len()-1] < mean { cfg.bear_holdings_ratio.clamp(0.0, 1.0) }
                else { 1.0 }
            } else { 1.0 }
        }).unwrap_or(1.0);
        let weights = select_portfolio_v2(&scores, cache, date, n_holdings, min_score, strategy, exposure);
        info!("V2 signal {date} (factors through {as_of}): scored={} selected={} exposure={:.3}",
            scores.len(), weights.len(), weights.values().sum::<f64>());
        // Empty targets must liquidate old holdings instead of silently retaining them.
        signals.insert(date, weights);
    }
    signals
}

fn select_portfolio_v2(
    scores: &FxHashMap<String, f64>, cache: &AShareCache, date: NaiveDate,
    n_holdings: usize, min_score: f64, strategy: &AShareStrategyConfig, exposure: f64,
) -> FxHashMap<String, f64> {
    let mut weights = FxHashMap::default();
    if n_holdings == 0 { return weights; }
    let slot = (exposure / n_holdings as f64).min(strategy.max_single_weight);
    if !slot.is_finite() || slot <= 0.0 { return weights; }
    let mut ranked: Vec<_> = scores.iter().filter(|(_, s)| s.is_finite() && **s >= min_score).collect();
    ranked.sort_by(|a, b| b.1.total_cmp(a.1).then_with(|| a.0.cmp(b.0)));
    let mut industries: FxHashMap<String, f64> = FxHashMap::default();
    for (code, _) in ranked {
        let industry = cache.industry_on(code, date).map(|i| i.index_code.as_str()).unwrap_or("UNKNOWN");
        let used = industries.get(industry).copied().unwrap_or(0.0);
        let weight = slot.min((strategy.max_industry_weight - used).max(0.0));
        if weight <= 1e-9 { continue; }
        weights.insert(code.clone(), weight);
        *industries.entry(industry.to_string()).or_default() += weight;
        if weights.len() == n_holdings { break; }
    }
    weights
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn no_prior_day_produces_no_signal() {
        let cache = AShareCache {
            daily: FxHashMap::default(),
            financials: FxHashMap::default(),
            industry: FxHashMap::default(),
            basics: FxHashMap::default(),
            trading_days: vec![NaiveDate::from_ymd_opt(2024, 1, 31).unwrap()],
            index_prices: FxHashMap::default(),
            ts_codes: vec![],
            top_list: FxHashMap::default(),
            margin_detail: FxHashMap::default(),
        };
        let strategy = AShareStrategyConfig::default();
        let signals = generate_signals_v2(&cache, 20, 0.0, None, &strategy, None);
        assert!(signals.is_empty());
    }
    fn fixture() -> AShareCache {
        use quant_factors::a_share::cache::{ABar, ALhbDay, AIndustry};
        let dates: Vec<_> = (1..=25).map(|n| NaiveDate::from_ymd_opt(2024, 1, n).unwrap()).collect();
        let mut cache = AShareCache {
            daily: FxHashMap::default(), financials: FxHashMap::default(),
            industry: FxHashMap::default(), basics: FxHashMap::default(),
            trading_days: dates.clone(), index_prices: FxHashMap::default(),
            ts_codes: vec!["A".into(), "B".into(), "C".into()],
            top_list: FxHashMap::default(), margin_detail: FxHashMap::default(),
        };
        for (i, code) in ["A", "B", "C"].iter().enumerate() {
            let bar = ABar { open: 10.0, high: 11.0, low: 9.0, close: 10.0,
                pre_close: 10.0, vol: 1000.0, amount: 1e6, adj_factor: 1.0,
                pct_chg: 0.0, turnover_rate: 1.0, pe_ttm: 10.0, pb: 1.0,
                ps_ttm: 1.0, dv_ttm: 0.0, total_mv: 1e6, circ_mv: 1e6 };
            cache.daily.insert((*code).into(), dates.iter().map(|d| (*d, bar.clone())).collect());
            cache.top_list.insert((*code).into(), vec![(dates[1], ALhbDay {
                net_amount: 1.0, l_buy: 1.0, l_sell: 0.0, amount: 1.0, net_rate: i as f64,
            })]);
            cache.industry.insert((*code).into(), vec![AIndustry {
                index_code: if *code == "C" { "other" } else { "same" }.into(),
                industry_name: "test".into(), in_date: None, out_date: None,
            }]);
        }
        cache
    }

    #[test]
    fn capped_selection_is_deterministic_and_retains_cash() {
        let cache = fixture();
        let scores = [("B", 1.0), ("A", 1.0), ("C", 0.5)].into_iter()
            .map(|(c, s)| (c.to_string(), s)).collect();
        let mut cfg = AShareStrategyConfig::default();
        cfg.max_single_weight = 0.4; cfg.max_industry_weight = 0.5;
        let weights = select_portfolio_v2(&scores, &cache, cache.trading_days[2], 3, 0.0, &cfg, 1.0);
        assert!((weights["A"] - 1.0/3.0).abs() < 1e-9);
        assert!((weights["A"] + weights["B"] - 0.5).abs() < 1e-9);
        assert!(weights.values().sum::<f64>() < 1.0);
        assert!(weights.values().all(|w| *w <= cfg.max_single_weight));
    }

    #[test]
    fn signals_lag_inputs_and_emit_cash_when_observations_expire() {
        let mut cache = fixture();
        let mut cfg = AShareStrategyConfig::default();
        cfg.rebalance_interval = 10;
        let window = cache.trading_days[2..].to_vec();
        let first = generate_signals_v2_for_dates(&cache, &window, 20, 0.0, None, &cfg, None);
        assert!(!first[&window[0]].is_empty());
        assert!(first[&window[10]].is_empty(), "stale LHB records must not sustain a position");
        cache.top_list.get_mut("A").unwrap().push((window[0],
            quant_factors::a_share::cache::ALhbDay { net_amount: 1e9, l_buy: 1e9,
                l_sell: 0.0, amount: 1e9, net_rate: 1e9 }));
        let changed = generate_signals_v2_for_dates(&cache, &window, 20, 0.0, None, &cfg, None);
        assert_eq!(first[&window[0]], changed[&window[0]], "same-day input must not affect lagged signal");
    }

}
