//! Tiered portfolio construction: 60% large cap + 25% quality IPO + 15% small cap momentum.
//!
//! Each tier has independent universe, factor weights, and position limits.

use std::collections::HashMap;

use qrs_core::types::{Date, TickerId, SectorId};
use qrs_data::cache::DataCache;
use rustc_hash::{FxHashMap, FxHashSet};
use tracing::info;

/// Tier allocation config.
pub struct TieredConfig {
    pub large_cap_pct: f64,      // 0.60
    pub ipo_pct: f64,            // 0.25
    pub small_cap_pct: f64,      // 0.15
    pub large_cap_n: usize,      // 15
    pub ipo_n: usize,            // 7
    pub small_cap_n: usize,      // 4
    pub large_cap_min_mcap: f64, // 50e9
    pub ipo_max_age_days: i64,   // 730 (2 years)
    pub ipo_min_rev_growth: f64, // 0.20 (20%)
    pub ipo_min_gross_margin: f64, // 0.30 (30%)
    pub small_cap_min_mcap: f64, // 500e6
    pub small_cap_max_mcap: f64, // 5e9
    pub small_cap_stop_loss: f64, // 0.15
    pub max_single_weight: f64,  // 0.10
}

impl Default for TieredConfig {
    fn default() -> Self {
        Self {
            large_cap_pct: 0.60,
            ipo_pct: 0.25,
            small_cap_pct: 0.15,
            large_cap_n: 15,
            ipo_n: 7,
            small_cap_n: 4,
            large_cap_min_mcap: 50e9,
            ipo_max_age_days: 730,
            ipo_min_rev_growth: 0.20,
            ipo_min_gross_margin: 0.30,
            small_cap_min_mcap: 500e6,
            small_cap_max_mcap: 5e9,
            small_cap_stop_loss: 0.15,
            max_single_weight: 0.10,
        }
    }
}

/// Good sectors for IPO tier (tech, healthcare, clean energy).
fn is_good_ipo_sector(sector_name: &str) -> bool {
    matches!(sector_name,
        "Technology" | "Healthcare" | "Communication Services"
    )
}

/// Build tiered portfolio with holding stickiness.
///
/// Stickiness rules:
/// - Keep existing holding if score still in top 50% of tier candidates
/// - Only sell if score drops below top 50%
/// - Only buy new stocks from top 10% to fill vacated slots
///
/// Returns: HashMap<TickerId, f64> (positive weights summing to ~1.0).
pub fn select_tiered_portfolio(
    date: Date,
    processed_factors: &HashMap<String, FxHashMap<TickerId, f64>>,
    factor_weights: &HashMap<String, f64>,
    cache: &DataCache,
    config: &TieredConfig,
    prev_holdings: &FxHashSet<TickerId>, // previous period's long holdings
) -> FxHashMap<TickerId, f64> {
    let mut result = FxHashMap::default();

    // === Tier 1: Large Cap Core ($50B+) ===
    let large_cap_picks = select_with_stickiness(
        &select_large_cap_scored(date, processed_factors, factor_weights, cache, config),
        prev_holdings,
        config.large_cap_n,
    );
    let lc_weight = if !large_cap_picks.is_empty() {
        (config.large_cap_pct / large_cap_picks.len() as f64).min(config.max_single_weight)
    } else { 0.0 };
    for &tid in &large_cap_picks {
        result.insert(tid, lc_weight);
    }

    // === Tier 2: Quality IPO ===
    let ipo_picks = select_with_stickiness(
        &select_quality_ipo_scored(date, processed_factors, factor_weights, cache, config),
        prev_holdings,
        config.ipo_n,
    );
    let ipo_weight = if !ipo_picks.is_empty() {
        (config.ipo_pct / ipo_picks.len() as f64).min(config.max_single_weight)
    } else { 0.0 };
    for &tid in &ipo_picks {
        if !result.contains_key(&tid) {
            result.insert(tid, ipo_weight);
        }
    }

    // === Tier 3: Small Cap Momentum ===
    let small_picks = select_with_stickiness(
        &select_small_cap_momentum_scored(date, processed_factors, factor_weights, cache, config),
        prev_holdings,
        config.small_cap_n,
    );
    let sc_weight = if !small_picks.is_empty() {
        (config.small_cap_pct / small_picks.len() as f64).min(config.max_single_weight)
    } else { 0.0 };
    for &tid in &small_picks {
        if !result.contains_key(&tid) {
            result.insert(tid, sc_weight);
        }
    }

    // Normalize to sum = 1.0
    let total: f64 = result.values().sum();
    if total > 0.0 && (total - 1.0).abs() > 0.01 {
        for v in result.values_mut() {
            *v /= total;
        }
    }

    info!(
        "Tiered: {}L/{}I/{}S = {} total, weights sum={:.2}",
        large_cap_picks.len(), ipo_picks.len(), small_picks.len(),
        result.len(), result.values().sum::<f64>(),
    );

    result
}

/// Holding stickiness: keep existing holdings unless they drop below top 50%.
/// Fill vacated slots from top 10% of new candidates.
fn select_with_stickiness(
    scored: &[(TickerId, f64)],
    prev_holdings: &FxHashSet<TickerId>,
    target_n: usize,
) -> Vec<TickerId> {
    if scored.is_empty() { return vec![]; }

    let n = scored.len();
    let keep_threshold = 0;  // Disabled: no stickiness (full rebalance each period is optimal)
    let new_threshold = n;  // All candidates eligible

    // Step 1: Keep existing holdings that are still in top 50%
    let mut kept: Vec<TickerId> = Vec::new();
    for (rank, &(tid, _)) in scored.iter().enumerate() {
        if prev_holdings.contains(&tid) && rank < keep_threshold {
            kept.push(tid);
        }
    }

    // Step 2: Fill remaining slots from top 10% (that aren't already kept)
    let slots_remaining = target_n.saturating_sub(kept.len());
    let mut new_picks: Vec<TickerId> = Vec::new();
    for &(tid, _) in scored.iter().take(new_threshold.max(target_n)) {
        if !kept.contains(&tid) {
            new_picks.push(tid);
            if new_picks.len() >= slots_remaining { break; }
        }
    }

    kept.extend(new_picks);
    kept.truncate(target_n);
    kept
}

/// Score a tier's factor model. Returns sorted (TickerId, score) pairs, descending.
fn score_tier(
    candidates: &[TickerId],
    tier_factors: &[(&str, f64)],
    factors: &HashMap<String, FxHashMap<TickerId, f64>>,
) -> Vec<(TickerId, f64)> {
    let mut scored: Vec<(TickerId, f64)> = candidates.iter()
        .filter_map(|&tid| {
            let mut score = 0.0;
            let mut weight_sum = 0.0;
            for &(fname, w) in tier_factors {
                if let Some(fvals) = factors.get(fname) {
                    if let Some(&v) = fvals.get(&tid) {
                        if v.is_finite() {
                            score += v * w;
                            weight_sum += w.abs();
                        }
                    }
                }
            }
            if weight_sum > 0.0 { Some((tid, score / weight_sum)) } else { None }
        })
        .collect();
    scored.sort_by(|a, b| b.1.partial_cmp(&a.1).unwrap_or(std::cmp::Ordering::Equal));
    scored
}

/// Tier 1: Score large cap stocks (returns sorted scored list).
fn select_large_cap_scored(
    date: Date,
    factors: &HashMap<String, FxHashMap<TickerId, f64>>,
    _factor_weights: &HashMap<String, f64>,
    cache: &DataCache,
    config: &TieredConfig,
) -> Vec<(TickerId, f64)> {
    // Universe: $50B+ market cap, traded today
    let candidates: Vec<TickerId> = cache.daily_prices.iter_date(date)
        .filter(|(tid, bar)| {
            bar.close > 0.0 && bar.volume > 0.0
            && cache.get_market_cap(*tid, date).unwrap_or(0.0) >= config.large_cap_min_mcap
        })
        .map(|(tid, _)| tid)
        .collect();

    if candidates.is_empty() { return vec![]; }

    // Large cap scoring: balanced quality + momentum + analyst
    let tier1_factors = [
        // Quality (solid foundation)
        ("PIOTROSKI_F", 1.5),
        ("ROE_TTM", 1.5),
        ("PROFIT_STB", 1.0),
        // Momentum (trend following)
        ("MOM_12M", 2.0),
        ("MOM_3M", 1.0),
        ("PRICE_52W_HIGH", 1.0),
        // Analyst / earnings signals
        ("EARNINGS_SURPRISE", 2.0),
        ("EPS_REVISION", 1.5),
        ("PRICE_TARGET_RATIO", 1.5),
        // Growth acceleration
        ("REVENUE_ACCELERATION", 2.5),  // 2nd derivative — key signal for NVDA-type stocks
        ("REVENUE_GROWTH", 1.5),
        ("EARNINGS_GROWTH", 1.5),
    ];

    score_tier(&candidates, &tier1_factors, factors)
}

/// Tier 2: Score quality IPO stocks (returns sorted scored list).
fn select_quality_ipo_scored(
    date: Date,
    factors: &HashMap<String, FxHashMap<TickerId, f64>>,
    _factor_weights: &HashMap<String, f64>,
    cache: &DataCache,
    config: &TieredConfig,
) -> Vec<(TickerId, f64)> {
    let cutoff_date = date - chrono::Duration::days(config.ipo_max_age_days);

    // Candidates: traded today + have financials + recently listed
    let mut candidates: Vec<TickerId> = Vec::new();

    for (tid, bar) in cache.daily_prices.iter_date(date) {
        if bar.close <= 0.0 || bar.volume <= 0.0 { continue; }
        let mc = cache.get_market_cap(tid, date).unwrap_or(0.0);
        if mc < 500e6 { continue; } // At least $500M

        // Check if "new" — first price date in our data is recent
        // Approximation: check if we have < 2 years of price data
        let has_old_data = cache.daily_prices.get(tid, cutoff_date).is_some();
        if has_old_data { continue; } // Has data before cutoff = not new enough

        // Fundamental filters
        let rev_growth = factors.get("REVENUE_GROWTH")
            .and_then(|f| f.get(&tid).copied())
            .unwrap_or(0.0);
        let gross_margin = factors.get("GROSS_MARGIN")
            .and_then(|f| f.get(&tid).copied())
            .unwrap_or(0.0);

        if rev_growth < config.ipo_min_rev_growth { continue; }
        if gross_margin < config.ipo_min_gross_margin { continue; }

        // Sector filter
        if let Some(&sid) = cache.sector_map.get(&tid) {
            let sector_name = cache.sector_interner.resolve(sid);
            if !is_good_ipo_sector(sector_name) { continue; }
        } else {
            continue; // No sector info = skip
        }

        candidates.push(tid);
    }

    if candidates.is_empty() { return vec![]; }

    // Score by growth + analyst signals
    let tier2_factors = [
        ("REVENUE_GROWTH", 2.0),
        ("EARNINGS_GROWTH", 1.5),
        ("EPS_REVISION", 1.5),
        ("US_ANALYST_COVERAGE", 1.0),
        ("MOM_3M", 1.0),
    ];

    score_tier(&candidates, &tier2_factors, factors)
}

/// Tier 3: Score small cap momentum stocks (returns sorted scored list).
fn select_small_cap_momentum_scored(
    date: Date,
    factors: &HashMap<String, FxHashMap<TickerId, f64>>,
    _factor_weights: &HashMap<String, f64>,
    cache: &DataCache,
    config: &TieredConfig,
) -> Vec<(TickerId, f64)> {
    // Universe: $500M-$5B, traded, profitable (net_income > 0)
    let candidates: Vec<TickerId> = cache.daily_prices.iter_date(date)
        .filter(|(tid, bar)| {
            if bar.close <= 0.0 || bar.volume <= 0.0 { return false; }
            let mc = cache.get_market_cap(*tid, date).unwrap_or(0.0);
            if mc < config.small_cap_min_mcap || mc > config.small_cap_max_mcap { return false; }
            // Must be profitable (latest quarter net_income > 0)
            if let Some(recs) = cache.financials.get(tid) {
                if let Some(latest) = recs.iter().find(|r| r.filing_date <= date) {
                    if let Some(&ni) = latest.fields.get("net_income") {
                        return ni > 0.0;
                    }
                }
            }
            false
        })
        .map(|(tid, _)| tid)
        .collect();

    if candidates.is_empty() { return vec![]; }

    // Score by pure momentum
    let tier3_factors = [
        ("MOM_12M", 2.0),
        ("MOM_3M", 1.5),
        ("PRICE_52W_HIGH", 1.5),
        ("SUE_PEAD", 1.0),
        ("FROG_IN_PAN", 1.0),
    ];

    score_tier(&candidates, &tier3_factors, factors)
}
