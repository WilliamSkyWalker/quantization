//! Professional short-selling strategy.
//!
//! Four-layer independent short scoring model:
//!   Layer 1 (0.30): Quality deterioration (ACCRUALS, BENEISH, MARGIN_TREND, EARNINGS_GROWTH)
//!   Layer 2 (0.25): Overvaluation bubble (EV_TO_SALES, PRICE_52W_HIGH, ANALYST_DISPERSION)
//!   Layer 3 (0.25): Smart money exit (EPS_REVISION, INSIDER_NET_BUY, INST_OWNERSHIP_DELTA)
//!   Layer 4 (0.20): Downward momentum (SUE_PEAD, MOM_3M)
//!
//! Risk controls: $10B min cap, 3% max per stock, 20% stop-loss, equal weight.
//! Regime-adaptive: bull → less short, bear → more short.

use qrs_core::types::{Date, TickerId};
use qrs_data::cache::DataCache;
use rustc_hash::{FxHashMap, FxHashSet};
use std::collections::HashMap;

/// Short strategy configuration.
pub struct ShortConfig {
    pub max_positions: usize,       // 8-12
    pub max_single_weight: f64,     // 0.03 (3%)
    pub base_short_exposure: f64,   // 0.20 (20%)
    pub min_market_cap: f64,        // 10e9 ($10B)
    pub min_dollar_volume: f64,     // 50e6 ($50M)
    pub stop_loss_pct: f64,         // 0.20 (20% price rise → close)
    pub trailing_stop_trigger: f64, // 0.10 (10% profit → activate trailing)
    pub trailing_stop_pct: f64,     // 0.50 (give back 50% of profit → close)
    pub time_stop_months: usize,    // 3 months no profit → close
}

impl Default for ShortConfig {
    fn default() -> Self {
        Self {
            max_positions: 10,
            max_single_weight: 0.03,
            base_short_exposure: 0.20,
            min_market_cap: 10e9,
            min_dollar_volume: 50e6,
            stop_loss_pct: 0.20,
            trailing_stop_trigger: 0.10,
            trailing_stop_pct: 0.50,
            time_stop_months: 3,
        }
    }
}

/// Multi-dimensional regime detection for short exposure.
///
/// Combines 4 signals:
///   1. Trend: price vs MA60 + MA200
///   2. Momentum: 60-day return (speed of decline)
///   3. Volatility: 20-day realized vol vs long-term median
///   4. Breadth: % of recent days with negative returns
///
/// Returns scale factor [0.0, 2.0] for short exposure.
pub fn regime_short_scale(
    date: Date,
    cache: &DataCache,
    _ma_window: usize,
) -> f64 {
    let idx = cache.trading_days.partition_point(|&d| d <= date);
    if idx < 252 {
        return 0.0; // Not enough data, no shorts
    }

    let dates = &cache.trading_days[idx.saturating_sub(252)..idx];
    let closes: Vec<f64> = dates.iter()
        .filter_map(|&d| cache.get_index_close("^GSPC", d))
        .collect();
    if closes.len() < 200 {
        return 0.0;
    }

    let n = closes.len();
    let current = closes[n - 1];

    // Signal 1: Trend — price relative to MA60 and MA200
    let ma60 = closes[n.saturating_sub(60)..].iter().sum::<f64>() / closes[n.saturating_sub(60)..].len() as f64;
    let ma200 = closes.iter().sum::<f64>() / n as f64;
    let below_ma60 = current < ma60;
    let below_ma200 = current < ma200;
    let trend_score = match (below_ma60, below_ma200) {
        (false, false) => 0.0,  // Above both MAs: bullish
        (true, false) => 0.3,   // Below MA60 only: early warning
        (false, true) => 0.2,   // Rare: above short MA but below long MA
        (true, true) => 1.0,    // Below both: bearish
    };

    // Signal 2: Momentum — 60-day return
    let ret_60d = if closes[n - 60] > 0.0 { current / closes[n - 60] - 1.0 } else { 0.0 };
    let mom_score = if ret_60d > 0.05 {
        0.0  // Strong positive momentum: no shorts
    } else if ret_60d > -0.05 {
        0.3  // Flat
    } else if ret_60d > -0.15 {
        0.7  // Moderate decline
    } else {
        1.0  // Sharp decline
    };

    // Signal 3: Volatility — 20-day realized vol vs 1-year median vol
    let rets: Vec<f64> = closes.windows(2).map(|w| w[1] / w[0] - 1.0).collect();
    let vol_20d = {
        let recent = &rets[rets.len().saturating_sub(20)..];
        let m = recent.iter().sum::<f64>() / recent.len() as f64;
        (recent.iter().map(|r| (r - m).powi(2)).sum::<f64>() / (recent.len() as f64 - 1.0)).sqrt()
    };
    let vol_1y = {
        let m = rets.iter().sum::<f64>() / rets.len() as f64;
        (rets.iter().map(|r| (r - m).powi(2)).sum::<f64>() / (rets.len() as f64 - 1.0)).sqrt()
    };
    let vol_ratio = if vol_1y > 1e-10 { vol_20d / vol_1y } else { 1.0 };
    let vol_score = if vol_ratio < 0.8 {
        0.0  // Low vol: calm market, don't short
    } else if vol_ratio < 1.2 {
        0.3  // Normal
    } else if vol_ratio < 2.0 {
        0.7  // Elevated vol
    } else {
        1.0  // Extreme vol (panic)
    };

    // Signal 4: Breadth — % of last 20 days negative
    let neg_pct = rets[rets.len().saturating_sub(20)..].iter()
        .filter(|r| **r < 0.0).count() as f64 / 20.0;
    let breadth_score = if neg_pct < 0.40 {
        0.0  // Mostly up days: bullish
    } else if neg_pct < 0.55 {
        0.3  // Balanced
    } else if neg_pct < 0.70 {
        0.7  // Mostly down days
    } else {
        1.0  // Persistent selling
    };

    // Composite: weighted average of 4 signals
    let composite = 0.35 * trend_score + 0.25 * mom_score + 0.20 * vol_score + 0.20 * breadth_score;

    // Map to scale factor:
    // composite 0.0 → scale 0.0 (no shorts in bull)
    // composite 0.3 → scale 0.0 (still not enough evidence)
    // composite 0.5 → scale 0.5 (10% short)
    // composite 0.7 → scale 1.0 (20% short)
    // composite 1.0 → scale 1.5 (30% short)
    if composite < 0.3 {
        0.0  // Bull: no shorts
    } else {
        let raw: f64 = (composite - 0.3) / 0.7 * 1.5;
        raw.clamp(0.0, 1.5)
    }
}

/// Compute short scores using the 4-layer model.
/// Returns: (ticker -> short_score) where higher = more shortable.
pub fn compute_short_scores(
    processed_factors: &HashMap<String, FxHashMap<TickerId, f64>>,
    long_scores: &FxHashMap<TickerId, f64>,
    cache: &DataCache,
    date: Date,
    config: &ShortConfig,
) -> FxHashMap<TickerId, f64> {
    // Collect universe of shortable tickers
    let shortable: FxHashSet<TickerId> = cache.daily_prices.iter_date(date)
        .filter(|(tid, bar)| {
            // Min market cap
            let mc = cache.get_market_cap(*tid, date).unwrap_or(0.0);
            if mc < config.min_market_cap { return false; }
            // Min dollar volume
            let dvol = if bar.dvol_20d.is_finite() && bar.dvol_20d > 0.0 {
                bar.dvol_20d
            } else {
                bar.close * bar.volume
            };
            if dvol < config.min_dollar_volume { return false; }
            // Not in top 50% of long scores
            if let Some(&ls) = long_scores.get(tid) {
                if ls > 0.0 { return false; } // Don't short stocks long model likes
            }
            true
        })
        .map(|(tid, _)| tid)
        .collect();

    if shortable.is_empty() {
        return FxHashMap::default();
    }

    // Helper: get z-scored factor value for shortable tickers, default 0
    let get_factor = |name: &str| -> FxHashMap<TickerId, f64> {
        match processed_factors.get(name) {
            Some(fvals) => {
                let vals: Vec<f64> = shortable.iter()
                    .map(|t| fvals.get(t).copied().unwrap_or(0.0))
                    .collect();
                // Already z-scored from processor, just filter to shortable
                shortable.iter().zip(vals.iter())
                    .map(|(&t, &v)| (t, v))
                    .collect()
            }
            None => FxHashMap::default(),
        }
    };

    // Layer 1: Quality Deterioration (0.30)
    // Higher = worse quality = more shortable
    let accruals = get_factor("ACCRUALS");           // high accruals = bad
    let beneish = get_factor("BENEISH_M");           // high M-score = manipulation
    let margin_trend = get_factor("MARGIN_TREND");   // negative = declining margins
    let earnings_growth = get_factor("EARNINGS_GROWTH"); // negative = declining earnings

    // Layer 2: Overvaluation (0.25)
    let ev_sales = get_factor("EV_TO_SALES");        // high = expensive (already -1 direction)
    let high_52w = get_factor("PRICE_52W_HIGH");     // near 1.0 = still at highs
    let dispersion = get_factor("ANALYST_DISPERSION"); // high = disagreement

    // Layer 3: Smart Money Exit (0.25)
    let eps_rev = get_factor("EPS_REVISION");        // negative = downward revision
    let insider = get_factor("INSIDER_NET_BUY");     // negative = insiders selling
    let inst_delta = get_factor("INST_OWNERSHIP_DELTA"); // negative = institutions reducing

    // Layer 4: Downward Momentum (0.20)
    let sue = get_factor("SUE_PEAD");                // negative = earnings miss
    let mom3m = get_factor("MOM_3M");                // negative = falling

    // Compute composite short score per ticker
    let mut scores = FxHashMap::default();

    for &tid in &shortable {
        let mut layer1 = 0.0f64;
        let mut n1 = 0;
        // For short: we WANT high accruals, high beneish, LOW margin_trend, LOW earnings_growth
        if let Some(&v) = accruals.get(&tid) { layer1 += v; n1 += 1; }          // high = shortable
        if let Some(&v) = beneish.get(&tid) { layer1 += v; n1 += 1; }           // high = shortable
        if let Some(&v) = margin_trend.get(&tid) { layer1 += -v; n1 += 1; }     // negate: low margin trend = shortable
        if let Some(&v) = earnings_growth.get(&tid) { layer1 += -v; n1 += 1; }  // negate: negative growth = shortable
        if n1 > 0 { layer1 /= n1 as f64; }

        let mut layer2 = 0.0f64;
        let mut n2 = 0;
        if let Some(&v) = ev_sales.get(&tid) { layer2 += -v; n2 += 1; }         // EV_TO_SALES already -1 dir, negate to get "high=shortable"
        if let Some(&v) = high_52w.get(&tid) { layer2 += v; n2 += 1; }          // high = near 52w high = shortable
        if let Some(&v) = dispersion.get(&tid) { layer2 += v; n2 += 1; }        // high dispersion = shortable
        if n2 > 0 { layer2 /= n2 as f64; }

        let mut layer3 = 0.0f64;
        let mut n3 = 0;
        if let Some(&v) = eps_rev.get(&tid) { layer3 += -v; n3 += 1; }          // negate: negative revision = shortable
        if let Some(&v) = insider.get(&tid) { layer3 += -v; n3 += 1; }          // negate: insider selling = shortable
        if let Some(&v) = inst_delta.get(&tid) { layer3 += -v; n3 += 1; }       // negate: inst reducing = shortable
        if n3 > 0 { layer3 /= n3 as f64; }

        let mut layer4 = 0.0f64;
        let mut n4 = 0;
        if let Some(&v) = sue.get(&tid) { layer4 += -v; n4 += 1; }             // negate: earnings miss = shortable
        if let Some(&v) = mom3m.get(&tid) { layer4 += -v; n4 += 1; }           // negate: falling = shortable
        if n4 > 0 { layer4 /= n4 as f64; }

        // Need at least 2 layers with data
        let layers_valid = [n1 > 0, n2 > 0, n3 > 0, n4 > 0].iter().filter(|&&v| v).count();
        if layers_valid < 2 { continue; }

        let composite = 0.30 * layer1 + 0.25 * layer2 + 0.25 * layer3 + 0.20 * layer4;
        if composite.is_finite() {
            scores.insert(tid, composite);
        }
    }

    scores
}

/// Select top-N short positions with equal weight.
/// Returns: HashMap<TickerId, f64> where values are NEGATIVE weights.
pub fn select_short_portfolio(
    short_scores: &FxHashMap<TickerId, f64>,
    config: &ShortConfig,
    total_short_exposure: f64, // regime-adjusted
) -> FxHashMap<TickerId, f64> {
    if short_scores.is_empty() || total_short_exposure <= 0.0 {
        return FxHashMap::default();
    }

    let mut sorted: Vec<(TickerId, f64)> = short_scores.iter()
        .map(|(&t, &s)| (t, s))
        .filter(|(_, s)| s.is_finite() && *s > 0.0) // Only short positive-scored (= bad stocks)
        .collect();
    sorted.sort_by(|a, b| b.1.partial_cmp(&a.1).unwrap_or(std::cmp::Ordering::Equal));

    let n = sorted.len().min(config.max_positions);
    if n == 0 {
        return FxHashMap::default();
    }

    // Equal weight, capped at max_single_weight
    let per_stock = (total_short_exposure / n as f64).min(config.max_single_weight);

    let mut weights = FxHashMap::default();
    for &(tid, _) in sorted.iter().take(n) {
        weights.insert(tid, -per_stock); // Negative = short
    }

    weights
}
