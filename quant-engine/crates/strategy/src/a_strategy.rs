//! A-share multi-factor strategy — scoring + portfolio selection.
//!
//! 29 factors across 7 categories, equal-weight within categories,
//! differentiated category weights (quality 1.3, value 0.7, etc).
//! Long-only, equal-weight portfolio, monthly rebalance.

use std::collections::HashMap;

use chrono::NaiveDate;
use rayon::prelude::*;
use rustc_hash::{FxHashMap, FxHashSet};
use tracing::{debug, info, warn};

use quant_core::config::{AShareRegimeConfig, AShareStrategyConfig, AShareUniverseConfig};
use quant_factors::a_share::cache::AShareCache;
use quant_factors::a_share::factors::{all_factors, AFactorResult};
use quant_factors::a_share::universe::{AUniverseFilter, get_a_clean_universe};

type FactorValues = Vec<(&'static str, &'static str, i8, AFactorResult)>;
type ScoreDetails = FxHashMap<String, (f64, FxHashMap<String, f64>)>;

/// A-share regime detection — trend + volatility composite.
///
/// Returns `strength` in [0, 1]: >= 0.8 = bull, <= 0.3 = bear.
fn detect_a_regime(
    cache: &AShareCache,
    index: &str,
    date: NaiveDate,
    ma_window: usize,
) -> f64 {
    let series = match cache.index_prices.get(index) {
        Some(s) => s,
        None => return 0.5,
    };
    let end_pos = match series.partition_point(|(d, _)| *d <= date) {
        0 => return 0.5,
        p => p,
    };

    // Trend: price vs MA
    let ma_n = ma_window.min(end_pos);
    let ma_start = end_pos - ma_n;
    let ma_sum: f64 = series[ma_start..end_pos].iter().map(|(_, p)| p).sum();
    let ma = ma_sum / ma_n as f64;
    let current_price = series[end_pos - 1].1;
    let dev_pct = if ma > 0.0 { (current_price / ma - 1.0) * 100.0 } else { 0.0 };
    let trend = ((dev_pct + 5.0) / 10.0).clamp(0.0, 1.0);

    // Volatility: 20-day realized vol, percentile over trailing 252 days
    let vol_window = 20;
    let lookback = 252.min(end_pos);
    let mut vol_series = Vec::with_capacity(lookback - vol_window);
    for i in vol_window..lookback {
        let idx = end_pos - lookback + i;
        let prev_idx = idx - 1;
        if idx < end_pos && prev_idx < series.len() && idx < series.len() {
            let ret = if series[prev_idx].1 > 0.0 {
                (series[idx].1 / series[prev_idx].1).ln()
            } else {
                0.0
            };
            vol_series.push(ret);
        }
    }
    let mut rolling_vols = Vec::new();
    for i in vol_window..vol_series.len() {
        let window = &vol_series[i - vol_window..i];
        let mean = window.iter().sum::<f64>() / vol_window as f64;
        let var = window.iter().map(|r| (r - mean).powi(2)).sum::<f64>() / (vol_window - 1) as f64;
        rolling_vols.push(var.sqrt() * (252.0_f64).sqrt());
    }
    let current_vol = if vol_series.len() >= vol_window {
        let w = &vol_series[vol_series.len() - vol_window..];
        let mean = w.iter().sum::<f64>() / vol_window as f64;
        let var = w.iter().map(|r| (r - mean).powi(2)).sum::<f64>() / (vol_window - 1) as f64;
        var.sqrt() * (252.0_f64).sqrt()
    } else {
        0.0
    };
    let vol_pct = if rolling_vols.is_empty() {
        0.5
    } else {
        let below = rolling_vols.iter().filter(|&&v| v < current_vol).count();
        below as f64 / rolling_vols.len() as f64
    };
    let vol_score = 1.0 - vol_pct;

    (0.6 * trend + 0.4 * vol_score).clamp(0.0, 1.0)
}

/// Linear interpolation for holdings ratio based on regime strength.
fn holdings_ratio(strength: f64, bear_ratio: f64) -> f64 {
    if strength >= 0.8 { 1.0 }
    else if strength <= 0.3 { bear_ratio }
    else {
        let t = (strength - 0.3) / 0.5;
        bear_ratio + t * (1.0 - bear_ratio)
    }
}

/// Factor processing: winsorize + standardize (z-score).
pub fn winsorize_zscore_public(raw: &AFactorResult) -> AFactorResult {
    winsorize_zscore(raw)
}

/// Regime detection public wrapper.
pub fn detect_a_regime_public(
    cache: &AShareCache,
    index: &str,
    date: chrono::NaiveDate,
    ma_window: usize,
) -> f64 {
    detect_a_regime(cache, index, date, ma_window)
}

/// Factor processing: winsorize + standardize (z-score).
fn winsorize_zscore(raw: &AFactorResult) -> AFactorResult {
    if raw.len() < 5 { return raw.clone(); }

    let vals: Vec<f64> = raw.values().copied().filter(|v| v.is_finite()).collect();
    if vals.len() < 5 { return raw.clone(); }

    // MAD winsorization
    let mut sorted = vals.clone();
    sorted.sort_by(|a, b| a.partial_cmp(b).unwrap());
    let median = sorted[sorted.len() / 2];
    let mut abs_devs: Vec<f64> = sorted.iter().map(|v| (v - median).abs()).collect();
    abs_devs.sort_by(|a, b| a.partial_cmp(b).unwrap());
    let mad = abs_devs[abs_devs.len() / 2] * 1.4826;
    let lower = median - 5.0 * mad;
    let upper = median + 5.0 * mad;

    // Z-score
    let clipped: Vec<f64> = vals.iter().map(|v| v.clamp(lower, upper)).collect();
    let mean = clipped.iter().sum::<f64>() / clipped.len() as f64;
    let var = clipped.iter().map(|v| (v - mean).powi(2)).sum::<f64>() / (clipped.len() - 1) as f64;
    let std = var.sqrt();

    if std < 1e-10 { return raw.clone(); }

    raw.iter()
        .map(|(k, &v)| {
            let w = v.clamp(lower, upper);
            (k.clone(), (w - mean) / std)
        })
        .collect()
}

/// Compute composite scores for all stocks on a date.
///
/// `universe`: when `Some`, only score codes in this set (post-cleaner). Tradable
/// stocks only — delisted, ST, suspended, micro-cap, illiquid are excluded
/// upstream. When `None`, scores every code seen in factor outputs (legacy).
///
/// Returns: ts_code → score (higher = better).
pub fn compute_scores(
    date: NaiveDate,
    cache: &AShareCache,
    universe: Option<&FxHashSet<String>>,
    strategy: &AShareStrategyConfig,
    regime_overrides: Option<&HashMap<String, f64>>,
) -> AFactorResult {
    compute_scores_detail(date, cache, universe, strategy, regime_overrides)
        .into_iter()
        .map(|(code, (score, _))| (code, score))
        .collect()
}

/// Compute scores with per-category breakdown.
///
/// Returns: ts_code → (total_score, HashMap<category, cat_score>)
pub fn compute_scores_detail(
    date: NaiveDate,
    cache: &AShareCache,
    universe: Option<&FxHashSet<String>>,
    strategy: &AShareStrategyConfig,
    regime_overrides: Option<&HashMap<String, f64>>,
) -> ScoreDetails {
    let factors = all_factors();
    let deferred_factors: Vec<&str> = factors.iter()
        .filter(|factor| !matches!(factor.direction, -1 | 1))
        .map(|factor| factor.name)
        .collect();
    if !deferred_factors.is_empty() {
        warn!(
            "A-share scores exclude factors without a validated static direction: {}",
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
            let processed = winsorize_zscore(&raw);
            Some((f.name, f.category, f.direction, processed))
        })
        .collect();

    let category_weights = category_weights(strategy, regime_overrides);
    let results = aggregate_score_details(
        &factor_values,
        universe,
        &category_weights,
        strategy.min_valid_categories,
    );
    debug!("A-share scores: {} stocks on {date}", results.len());
    results
}

fn category_weights(
    strategy: &AShareStrategyConfig,
    regime_overrides: Option<&HashMap<String, f64>>,
) -> HashMap<String, f64> {
    strategy.category_weights.iter()
        .filter_map(|(category, &weight)| {
            if !weight.is_finite() || weight <= 0.0 {
                warn!("A-share score ignores invalid category weight {category}={weight}");
                return None;
            }
            let multiplier = regime_overrides
                .and_then(|overrides| overrides.get(category))
                .copied()
                .unwrap_or(1.0);
            let effective_weight = weight * multiplier;
            if !effective_weight.is_finite() || effective_weight <= 0.0 {
                warn!("A-share score ignores invalid effective category weight {category}={effective_weight}");
                return None;
            }
            Some((category.clone(), effective_weight))
        })
        .collect()
}

fn aggregate_score_details(
    factor_values: &FactorValues,
    universe: Option<&FxHashSet<String>>,
    category_weights: &HashMap<String, f64>,
    min_valid_categories: usize,
) -> ScoreDetails {
    let mut all_codes: FxHashSet<&str> = FxHashSet::default();
    for (_, _, _, values) in factor_values {
        for code in values.keys() {
            if universe.is_none_or(|allowed| allowed.contains(code)) {
                all_codes.insert(code);
            }
        }
    }

    let mut results = ScoreDetails::default();
    for code in all_codes {
        let mut cat_scores: FxHashMap<String, f64> = FxHashMap::default();
        let mut cat_counts: FxHashMap<String, usize> = FxHashMap::default();

        for (_name, category, direction, vals) in factor_values {
            if let Some(&val) = vals.get(code) {
                if val.is_finite() && matches!(*direction, -1 | 1) {
                    let signed = *direction as f64 * val;
                    *cat_scores.entry(category.to_string()).or_insert(0.0) += signed;
                    *cat_counts.entry(category.to_string()).or_insert(0) += 1;
                }
            }
        }

        for (cat, sum) in &mut cat_scores {
            if let Some(&cnt) = cat_counts.get(cat) {
                if cnt > 0 { *sum /= cnt as f64; }
            }
        }

        let mut total_score = 0.0;
        let mut total_weight = 0.0;

        for (cat, &cat_score) in &cat_scores {
            if let Some(&weight) = category_weights.get(cat) {
                total_score += cat_score * weight;
                total_weight += weight;
            }
        }

        let valid_categories = cat_scores.keys()
            .filter(|category| category_weights.contains_key(*category))
            .count();
        if total_weight > 0.0 && valid_categories >= min_valid_categories {
            results.insert(code.to_string(), (total_score / total_weight, cat_scores));
        }
    }

    results
}

/// Select top-N equal-weight portfolio from scores.
///
/// Returns: ts_code → weight (equal weight, sums to 1.0).
pub fn select_portfolio(
    scores: &AFactorResult,
    n_holdings: usize,
    min_score: f64,
) -> FxHashMap<String, f64> {
    let mut sorted: Vec<(&str, f64)> = scores.iter()
        .filter(|(_, s)| **s >= min_score && s.is_finite())
        .map(|(k, &v)| (k.as_str(), v))
        .collect();
    sorted.sort_by(|a, b| b.1.partial_cmp(&a.1).unwrap_or(std::cmp::Ordering::Equal));

    let selected: Vec<&str> = sorted.iter().take(n_holdings).map(|(k, _)| *k).collect();
    let n = selected.len();
    if n == 0 { return FxHashMap::default(); }

    let w = 1.0 / n as f64;
    selected.into_iter().map(|code| (code.to_string(), w)).collect()
}

/// Generate monthly rebalance signals over a date range.
///
/// Per-rebalance-date clean universe is computed from `universe_cfg` (cleaner
/// rules: ST / suspended / IPO age / micro-cap / illiquid / board exclusions).
/// Pass `None` for `universe_cfg` to disable the filter (legacy behavior).
pub fn generate_signals(
    cache: &AShareCache,
    n_holdings: usize,
    min_score: f64,
    universe_cfg: Option<&AShareUniverseConfig>,
    strategy: &AShareStrategyConfig,
    regime_cfg: Option<&AShareRegimeConfig>,
) -> std::collections::BTreeMap<NaiveDate, FxHashMap<String, f64>> {
    use chrono::Datelike;

    let mut signals = std::collections::BTreeMap::new();
    let mut last_ym = (0i32, 0u32);
    let filter = universe_cfg.map(AUniverseFilter::from_config);

    // Monthly rebalance: last trading day of each month
    let mut rebalance_dates = Vec::new();
    for &d in cache.trading_days.iter().rev() {
        let ym = (d.year(), d.month());
        if ym != last_ym {
            rebalance_dates.push(d);
            last_ym = ym;
        }
    }
    rebalance_dates.reverse();

    info!("A-share signal generation: {} rebalance dates", rebalance_dates.len());

    for (i, &date) in rebalance_dates.iter().enumerate() {
        let universe = filter.as_ref()
            .map(|f| get_a_clean_universe(date, cache, f));

        // Regime detection: only for position sizing, not factor weight overrides.
        let h_ratio = if let Some(cfg) = regime_cfg {
            if cfg.enabled {
                let strength = detect_a_regime(cache, &cfg.index, date, cfg.ma_window);
                holdings_ratio(strength, cfg.bear_holdings_ratio)
            } else {
                1.0
            }
        } else {
            1.0
        };

        let scores = compute_scores(date, cache, universe.as_ref(), strategy, None);
        let effective_n = if h_ratio < 0.99 {
            (n_holdings as f64 * h_ratio).round().max(1.0) as usize
        } else {
            n_holdings
        };
        let weights = select_portfolio(&scores, effective_n, min_score);

        if !weights.is_empty() {
            signals.insert(date, weights);
        }

        if (i + 1) % 12 == 0 || i + 1 == rebalance_dates.len() {
            let regime_str = regime_cfg
                .filter(|c| c.enabled)
                .map(|c| {
                    let s = detect_a_regime(cache, &c.index, date, c.ma_window);
                    format!(" regime={:.2} ratio={:.2}", s, h_ratio)
                })
                .unwrap_or_default();
            info!("Signal {}/{}: universe={} scored={} selected={}{regime_str}",
                i + 1, rebalance_dates.len(),
                universe.as_ref().map(|u| u.len() as i64).unwrap_or(-1),
                scores.len(),
                signals.get(&date).map(|s| s.len()).unwrap_or(0));
        }
    }

    signals
}

#[cfg(test)]
mod tests {
    use super::*;

    fn factor(name: &'static str, category: &'static str, direction: i8, values: &[(&str, f64)]) -> (
        &'static str,
        &'static str,
        i8,
        AFactorResult,
    ) {
        (
            name,
            category,
            direction,
            values.iter().map(|(code, value)| ((*code).to_string(), *value)).collect(),
        )
    }

    #[test]
    fn aggregation_uses_directions_and_configured_category_coverage() {
        let values = vec![
            factor("positive", "value", 1, &[("A", 1.0), ("B", 1.0)]),
            factor("negative", "quality", -1, &[("A", -2.0), ("B", -2.0)]),
            factor("growth", "growth", 1, &[("A", 3.0), ("B", 3.0)]),
            factor("technical", "technical", 1, &[("A", 4.0)]),
            factor("deferred", "momentum", 0, &[("A", 100.0), ("B", 100.0)]),
        ];
        let weights = HashMap::from([
            ("value".to_string(), 1.0),
            ("quality".to_string(), 1.0),
            ("growth".to_string(), 1.0),
            ("technical".to_string(), 1.0),
            ("momentum".to_string(), 1.0),
        ]);

        let details = aggregate_score_details(&values, None, &weights, 4);

        assert_eq!(details.len(), 1);
        let (score, categories) = details.get("A").expect("A has four valid categories");
        assert_eq!(*score, 2.5);
        assert_eq!(categories.get("quality"), Some(&2.0));
        assert!(!categories.contains_key("momentum"));
        assert!(!details.contains_key("B"));
    }
}
