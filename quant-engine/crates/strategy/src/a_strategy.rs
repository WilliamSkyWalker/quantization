//! A-share multi-factor strategy — scoring + portfolio selection.
//!
//! 29 factors across 7 categories, equal-weight within categories,
//! differentiated category weights (quality 1.3, value 0.7, etc).
//! Long-only, equal-weight portfolio, monthly rebalance.

use std::collections::HashMap;

use chrono::NaiveDate;
use rustc_hash::FxHashMap;
use tracing::{debug, info};

use quant_factors::a_share::cache::AShareCache;
use quant_factors::a_share::factors::{all_factors, AFactorResult};

/// A-share category weights (quality-dominant).
fn category_weights() -> HashMap<&'static str, f64> {
    let mut w = HashMap::new();
    w.insert("value", 0.7);
    w.insert("quality", 1.3);
    w.insert("growth", 1.0);
    w.insert("momentum", 0.9);
    w.insert("technical", 0.7);
    w.insert("macro", 0.6);
    w.insert("sentiment", 0.6);
    w
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
/// Returns: ts_code → score (higher = better).
pub fn compute_scores(date: NaiveDate, cache: &AShareCache) -> AFactorResult {
    let factors = all_factors();
    let cat_weights = category_weights();

    // Compute all factors
    let mut factor_values: Vec<(&str, &str, i8, AFactorResult)> = Vec::new();
    for f in &factors {
        let raw = (f.compute)(date, cache);
        if raw.is_empty() { continue; }
        let processed = winsorize_zscore(&raw);
        factor_values.push((f.name, f.category, f.direction, processed));
    }

    // Collect all stock codes
    let mut all_codes: std::collections::HashSet<&str> = std::collections::HashSet::new();
    for (_, _, _, vals) in &factor_values {
        for code in vals.keys() {
            all_codes.insert(code.as_str());
        }
    }

    let mut scores = AFactorResult::new();

    for code in all_codes {
        // Layer 1: intra-category average
        let mut cat_scores: HashMap<&str, f64> = HashMap::new();
        let mut cat_counts: HashMap<&str, usize> = HashMap::new();

        for (_name, category, direction, vals) in &factor_values {
            if let Some(&val) = vals.get(code) {
                if val.is_finite() {
                    let signed = if *direction == -1 { -val } else { val };
                    *cat_scores.entry(category).or_insert(0.0) += signed;
                    *cat_counts.entry(category).or_insert(0) += 1;
                }
            }
        }

        // Average within categories
        for (cat, sum) in &mut cat_scores {
            if let Some(&cnt) = cat_counts.get(cat) {
                if cnt > 0 { *sum /= cnt as f64; }
            }
        }

        // Layer 2: weighted category sum
        let mut total_score = 0.0;
        let mut total_weight = 0.0;

        for (cat, &cat_score) in &cat_scores {
            let w = cat_weights.get(cat).copied().unwrap_or(1.0);
            total_score += cat_score * w;
            total_weight += w;
        }

        if total_weight > 0.0 && cat_scores.len() >= 3 {
            scores.insert(code.to_string(), total_score / total_weight);
        }
    }

    debug!("A-share scores: {} stocks on {date}", scores.len());
    scores
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
pub fn generate_signals(
    cache: &AShareCache,
    n_holdings: usize,
    min_score: f64,
) -> std::collections::BTreeMap<NaiveDate, FxHashMap<String, f64>> {
    use chrono::Datelike;

    let mut signals = std::collections::BTreeMap::new();
    let mut last_ym = (0i32, 0u32);

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
        let scores = compute_scores(date, cache);
        let weights = select_portfolio(&scores, n_holdings, min_score);

        if !weights.is_empty() {
            signals.insert(date, weights);
        }

        if (i + 1) % 12 == 0 || i + 1 == rebalance_dates.len() {
            info!("Signal {}/{}: {} scored, {} selected",
                i + 1, rebalance_dates.len(), scores.len(),
                signals.get(&date).map(|s| s.len()).unwrap_or(0));
        }
    }

    signals
}
