//! A-share multi-factor strategy — v2, sentiment-driven (in development).
//!
//! Successor to the archived financial-driven strategy in `a_strategy.rs`
//! (frozen at git tag `a-share-strategy-v1-financial-archive`). See that
//! module's doc comment for the full v1 diagnosis and rationale for this
//! rewrite.
//!
//! **Status: stub.** Selection logic is intentionally empty pending the
//! sentiment data pipeline (see TODOs `download-a-share-sentiment-data`,
//! `implement-a-share-sentiment-factors`). `generate_signals_v2` currently
//! returns no signals for any date — this is deliberate so the backtest
//! CLI path can be switched over to the v2 entry point immediately, without
//! v1's financial factors silently continuing to drive live results while
//! v2 is designed. Do not fall back to `a_strategy::generate_signals`
//! internally; build v2 selection logic directly in this module.

use std::collections::HashMap;

use chrono::NaiveDate;
use rayon::prelude::*;
use rustc_hash::{FxHashMap, FxHashSet};
use tracing::warn;

use quant_core::config::{AShareRegimeConfig, AShareStrategyConfig, AShareUniverseConfig};
use quant_factors::a_share::cache::AShareCache;
use quant_factors::a_share::factors_v2::all_factors_v2;

use crate::a_strategy::{aggregate_score_details, category_weights, winsorize_zscore_public, FactorValues, ScoreDetails};

/// Compute v2 (sentiment-driven) composite scores with per-category
/// breakdown, for use by the offline IC / Fama-MacBeth validation scripts.
/// Not yet wired into `generate_signals_v2` — see module doc for why.
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
            let raw = (f.compute)(date, cache);
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

/// Generate monthly rebalance signals over a date range — v2 entry point.
///
/// Mirrors the signature of `a_strategy::generate_signals` (v1) so the CLI
/// backtest path can swap between them with a one-line change. Currently
/// returns an empty signal map for every rebalance date: no sentiment
/// factors are implemented yet, so there is nothing to select on.
pub fn generate_signals_v2(
    _cache: &AShareCache,
    _n_holdings: usize,
    _min_score: f64,
    _universe_cfg: Option<&AShareUniverseConfig>,
    _strategy: &AShareStrategyConfig,
    _regime_cfg: Option<&AShareRegimeConfig>,
) -> std::collections::BTreeMap<NaiveDate, FxHashMap<String, f64>> {
    std::collections::BTreeMap::new()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn generate_signals_v2_returns_empty_until_sentiment_factors_exist() {
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
        assert!(signals.is_empty(), "v2 stub must not select anything yet");
    }
}
