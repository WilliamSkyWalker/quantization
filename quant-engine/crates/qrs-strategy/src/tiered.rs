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

/// Build tiered portfolio weights.
/// Returns: HashMap<TickerId, f64> (positive weights summing to ~1.0).
pub fn select_tiered_portfolio(
    date: Date,
    processed_factors: &HashMap<String, FxHashMap<TickerId, f64>>,
    factor_weights: &HashMap<String, f64>,
    cache: &DataCache,
    config: &TieredConfig,
) -> FxHashMap<TickerId, f64> {
    let mut result = FxHashMap::default();

    // === Tier 1: Large Cap Core ($50B+) ===
    let large_cap_picks = select_large_cap(
        date, processed_factors, factor_weights, cache, config,
    );
    let lc_weight = if !large_cap_picks.is_empty() {
        (config.large_cap_pct / large_cap_picks.len() as f64).min(config.max_single_weight)
    } else { 0.0 };
    for &tid in &large_cap_picks {
        result.insert(tid, lc_weight);
    }

    // === Tier 2: Quality IPO ===
    let ipo_picks = select_quality_ipo(
        date, processed_factors, factor_weights, cache, config,
    );
    let ipo_weight = if !ipo_picks.is_empty() {
        (config.ipo_pct / ipo_picks.len() as f64).min(config.max_single_weight)
    } else { 0.0 };
    for &tid in &ipo_picks {
        if !result.contains_key(&tid) { // Avoid double-counting
            result.insert(tid, ipo_weight);
        }
    }

    // === Tier 3: Small Cap Momentum ===
    let small_picks = select_small_cap_momentum(
        date, processed_factors, factor_weights, cache, config,
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

/// Tier 1: Select large cap stocks.
/// Quality + momentum focused: PIOTROSKI_F, ROE_TTM, EARNINGS_SURPRISE, MOM_12M.
fn select_large_cap(
    date: Date,
    factors: &HashMap<String, FxHashMap<TickerId, f64>>,
    _factor_weights: &HashMap<String, f64>,
    cache: &DataCache,
    config: &TieredConfig,
) -> Vec<TickerId> {
    // Universe: $50B+ market cap, traded today
    let candidates: Vec<TickerId> = cache.daily_prices.iter_date(date)
        .filter(|(tid, bar)| {
            bar.close > 0.0 && bar.volume > 0.0
            && cache.get_market_cap(*tid, date).unwrap_or(0.0) >= config.large_cap_min_mcap
        })
        .map(|(tid, _)| tid)
        .collect();

    if candidates.is_empty() { return vec![]; }

    // Large cap scoring: quality + momentum
    let tier1_factors = [
        ("PIOTROSKI_F", 2.0),
        ("ROE_TTM", 1.5),
        ("EARNINGS_SURPRISE", 1.5),
        ("MOM_12M", 1.5),
        ("PROFIT_STB", 1.0),
        ("EV_TO_FCF", 1.0),
        ("BUYBACK_YIELD", 1.0),
    ];

    let mut scored: Vec<(TickerId, f64)> = candidates.iter()
        .filter_map(|&tid| {
            let mut score = 0.0;
            let mut weight_sum = 0.0;
            for &(fname, w) in &tier1_factors {
                if let Some(fvals) = factors.get(fname) {
                    if let Some(&v) = fvals.get(&tid) {
                        if v.is_finite() {
                            score += v * w;
                            weight_sum += w.abs();
                        }
                    }
                }
            }
            if weight_sum > 0.0 {
                Some((tid, score / weight_sum))
            } else {
                None
            }
        })
        .collect();

    scored.sort_by(|a, b| b.1.partial_cmp(&a.1).unwrap_or(std::cmp::Ordering::Equal));
    scored.iter().take(config.large_cap_n).map(|&(t, _)| t).collect()
}

/// Tier 2: Select quality IPO stocks.
/// Growth + analyst focused. Filters: IPO < 2yr, rev growth > 20%, gross margin > 30%, good sector.
fn select_quality_ipo(
    date: Date,
    factors: &HashMap<String, FxHashMap<TickerId, f64>>,
    _factor_weights: &HashMap<String, f64>,
    cache: &DataCache,
    config: &TieredConfig,
) -> Vec<TickerId> {
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

    let mut scored: Vec<(TickerId, f64)> = candidates.iter()
        .filter_map(|&tid| {
            let mut score = 0.0;
            let mut w_sum = 0.0;
            for &(fname, w) in &tier2_factors {
                if let Some(fvals) = factors.get(fname) {
                    if let Some(&v) = fvals.get(&tid) {
                        if v.is_finite() { score += v * w; w_sum += w.abs(); }
                    }
                }
            }
            if w_sum > 0.0 { Some((tid, score / w_sum)) } else { None }
        })
        .collect();

    scored.sort_by(|a, b| b.1.partial_cmp(&a.1).unwrap_or(std::cmp::Ordering::Equal));
    scored.iter().take(config.ipo_n).map(|&(t, _)| t).collect()
}

/// Tier 3: Select small cap momentum stocks.
/// Pure momentum, must be profitable.
fn select_small_cap_momentum(
    date: Date,
    factors: &HashMap<String, FxHashMap<TickerId, f64>>,
    _factor_weights: &HashMap<String, f64>,
    cache: &DataCache,
    config: &TieredConfig,
) -> Vec<TickerId> {
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

    let mut scored: Vec<(TickerId, f64)> = candidates.iter()
        .filter_map(|&tid| {
            let mut score = 0.0;
            let mut w_sum = 0.0;
            for &(fname, w) in &tier3_factors {
                if let Some(fvals) = factors.get(fname) {
                    if let Some(&v) = fvals.get(&tid) {
                        if v.is_finite() { score += v * w; w_sum += w.abs(); }
                    }
                }
            }
            if w_sum > 0.0 { Some((tid, score / w_sum)) } else { None }
        })
        .collect();

    scored.sort_by(|a, b| b.1.partial_cmp(&a.1).unwrap_or(std::cmp::Ordering::Equal));

    // Take top momentum, filter to top 10% only
    let top_pct = (candidates.len() / 10).max(config.small_cap_n);
    scored.iter().take(top_pct.min(config.small_cap_n)).map(|&(t, _)| t).collect()
}
