//! Two-layer category scoring model.
//!
//! Layer 1 (Intra-category): weighted average within each category
//! Layer 2 (Inter-category): weighted sum across categories with missing redistribution

use std::collections::HashMap;

use qrs_core::types::TickerId;
use rustc_hash::FxHashMap;
use tracing::debug;

/// Factor category mapping: factor_name -> category
pub type FactorCategoryMap = HashMap<String, String>;

/// Compute composite scores for all tickers.
///
/// Returns: HashMap<TickerId, f64> of final scores (higher = better).
pub fn compute_scores(
    // factor_name -> (ticker -> processed_value)
    processed_factors: &HashMap<String, FxHashMap<TickerId, f64>>,
    // factor_name -> weight (with IC direction baked in)
    factor_weights: &HashMap<String, f64>,
    // factor_name -> category
    factor_categories: &FactorCategoryMap,
    // category -> weight
    category_weights: &HashMap<String, f64>,
    // Minimum valid categories for a stock to get a score
    min_valid_categories: usize,
    // Missing factor threshold and max penalty
    missing_factor_threshold: f64,
    missing_factor_max_penalty: f64,
) -> FxHashMap<TickerId, f64> {
    // Collect all tickers that appear in any factor
    let mut all_tickers: std::collections::HashSet<TickerId> = Default::default();
    for fvals in processed_factors.values() {
        all_tickers.extend(fvals.keys());
    }

    let total_factors = processed_factors.len() as f64;

    let mut result = FxHashMap::default();

    for &ticker in &all_tickers {
        // === Layer 1: Intra-category scores ===
        let mut cat_scores: HashMap<&str, f64> = HashMap::new();
        let mut cat_valid: HashMap<&str, bool> = HashMap::new();
        let mut ticker_factor_count = 0usize;

        for cat in category_weights.keys() {
            let mut weighted_sum = 0.0;
            let mut weight_denom = 0.0;

            for (fname, fvals) in processed_factors {
                let _fcat = match factor_categories.get(fname.as_str()) {
                    Some(c) if c == cat => c,
                    _ => continue,
                };

                let weight = match factor_weights.get(fname.as_str()) {
                    Some(&w) => w,
                    None => continue,
                };

                if let Some(&value) = fvals.get(&ticker) {
                    if value.is_finite() {
                        weighted_sum += value * weight;
                        weight_denom += weight.abs();
                        ticker_factor_count += 1;
                    }
                }
            }

            if weight_denom > 0.0 {
                cat_scores.insert(cat.as_str(), weighted_sum / weight_denom);
                cat_valid.insert(cat.as_str(), true);
            }
        }

        // === Value Trap Penalty ===
        if let (Some(&val_score), Some(&qual_score)) =
            (cat_scores.get("value"), cat_scores.get("quality"))
        {
            if qual_score < -0.5 && val_score > 0.0 {
                let penalty = (1.5 + qual_score).clamp(0.3, 1.0);
                cat_scores.insert("value", val_score * penalty);
            }
        }

        // === Layer 2: Inter-category weighted sum ===
        let valid_cat_count = cat_valid.len();
        if valid_cat_count < min_valid_categories {
            continue; // Not enough categories
        }

        let mut total_score = 0.0;
        let mut total_weight_denom = 0.0;

        for (cat, &cat_weight) in category_weights {
            if let Some(&score) = cat_scores.get(cat.as_str()) {
                total_score += score * cat_weight;
                total_weight_denom += cat_weight.abs();
            }
        }

        if total_weight_denom <= 0.0 {
            continue;
        }

        let mut final_score = total_score / total_weight_denom;

        // === Missing Factor Penalty ===
        let missing_ratio = 1.0 - (ticker_factor_count as f64 / total_factors);
        if missing_ratio > missing_factor_threshold && total_factors > 0.0 {
            let excess = missing_ratio - missing_factor_threshold;
            let penalty = 1.0 - (excess / (1.0 - missing_factor_threshold)) * missing_factor_max_penalty;
            final_score *= penalty.max(0.0);
        }

        if final_score.is_finite() {
            result.insert(ticker, final_score);
        }
    }

    debug!("Scored {} tickers", result.len());
    result
}

/// Select top-N long-only portfolio from scores.
/// Returns: (long_weights, empty_short) as HashMap<TickerId, f64>.
/// Single stock capped at max_single_weight, excess redistributed.
pub fn select_portfolio(
    scores: &FxHashMap<TickerId, f64>,
    long_n: usize,
    temperature: f64,
    max_single_weight: f64, // e.g. 0.10 (10% per stock)
) -> FxHashMap<TickerId, f64> {
    let mut sorted: Vec<(TickerId, f64)> = scores
        .iter()
        .map(|(&t, &s)| (t, s))
        .filter(|(_, s)| s.is_finite() && *s > 0.0)
        .collect();
    sorted.sort_by(|a, b| b.1.partial_cmp(&a.1).unwrap_or(std::cmp::Ordering::Equal));

    let candidates: Vec<(TickerId, f64)> = sorted.into_iter().take(long_n).collect();
    if candidates.is_empty() {
        return FxHashMap::default();
    }

    // Softmax weights, total = 1.0 (fully invested long-only)
    let mut weights = softmax_weights(&candidates, temperature, 1.0);

    // Cap single stock weight, redistribute excess
    let mut iterations = 0;
    loop {
        let mut excess = 0.0;
        let mut uncapped = 0;
        for v in weights.values_mut() {
            if *v > max_single_weight {
                excess += *v - max_single_weight;
                *v = max_single_weight;
            } else {
                uncapped += 1;
            }
        }
        if excess < 1e-6 || uncapped == 0 || iterations > 10 {
            break;
        }
        // Redistribute excess proportionally to uncapped stocks
        let add_each = excess / uncapped as f64;
        for v in weights.values_mut() {
            if *v < max_single_weight {
                *v += add_each;
            }
        }
        iterations += 1;
    }

    weights
}

/// Apply softmax weighting to candidates.
fn softmax_weights(
    candidates: &[(TickerId, f64)],
    temperature: f64,
    total_weight: f64,
) -> FxHashMap<TickerId, f64> {
    let mut result = FxHashMap::default();
    if candidates.is_empty() {
        return result;
    }

    if temperature <= 0.0 {
        // Equal weight
        let w = total_weight / candidates.len() as f64;
        for &(tid, _) in candidates {
            result.insert(tid, w);
        }
        return result;
    }

    // Softmax: w_i = exp(score_i / T) / sum(exp(score_j / T))
    let max_score = candidates
        .iter()
        .map(|(_, s)| *s)
        .fold(f64::NEG_INFINITY, f64::max);

    let exp_scores: Vec<f64> = candidates
        .iter()
        .map(|(_, s)| ((s - max_score) / temperature).exp())
        .collect();

    let sum_exp: f64 = exp_scores.iter().sum();
    if sum_exp <= 0.0 || !sum_exp.is_finite() {
        let w = total_weight / candidates.len() as f64;
        for &(tid, _) in candidates {
            result.insert(tid, w);
        }
        return result;
    }

    for (i, &(tid, _)) in candidates.iter().enumerate() {
        let w = (exp_scores[i] / sum_exp) * total_weight;
        result.insert(tid, w);
    }

    result
}
