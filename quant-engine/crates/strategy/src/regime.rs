//! Composite Regime Detector — 4-dimensional market state identification
//!
//! Dimensions:
//!   1. Trend: benchmark price vs MA → [0, 1]
//!   2. Volatility: realized vol percentile → [0, 1]
//!   3. Credit: yield spread signal → [0, 1] (requires macro data, defaults to neutral)
//!   4. Crowding: momentum cross-sectional dispersion percentile → [0, 1]
//!
//! Output:
//!   strength ∈ [0, 1] (1 = bullish, 0 = bearish)
//!   Includes credit veto and crowding penalty.

use quant_core::config::RegimeConfig;
use quant_core::types::Date;
use quant_data::cache::DataCache;
use rustc_hash::FxHashMap;
use tracing::debug;

const TRANSITION_BAND: f64 = 0.05;
const VOL_LOOKBACK: usize = 252;

/// Regime detection result for a single date.
#[derive(Debug, Clone, Copy)]
pub struct RegimeState {
    /// Overall regime strength: 1.0 = full bull, 0.0 = full bear.
    pub strength: f64,
    /// Individual dimension scores.
    pub trend: f64,
    pub vol: f64,
    pub credit: f64,
    pub crowding: f64,
}

/// Detect regime state for a given date using the 4-dimensional composite model.
pub fn detect(cache: &DataCache, config: &RegimeConfig, date: Date) -> RegimeState {
    let trend = trend_score(cache, config, date);
    let vol = vol_score(cache, config, date);
    let credit = credit_score(cache, date);
    let crowding = crowding_score(cache, date);

    // Weighted composite: trend 35%, vol 30%, credit 25%, crowding 10%
    let mut strength = (0.35 * trend + 0.30 * vol + 0.25 * credit + 0.10 * crowding)
        .clamp(0.0, 1.0);

    // Credit veto: inverted spread caps strength to prevent bear-rally traps
    if credit < 0.2 && strength > 0.5 {
        debug!("Credit veto: credit={credit:.2}, strength {strength:.2} → 0.50");
        strength = 0.5;
    }

    // Factor crowding penalty: compressed dispersion → reduce bullish confidence
    if crowding < 0.3 && strength > 0.6 {
        strength *= 0.8;
        debug!("Crowding penalty: crowding={crowding:.2}, strength compressed to {strength:.2}");
    }

    debug!(
        "Regime: date={date}, trend={trend:.2}, vol={vol:.2}, \
         credit={credit:.2}, crowding={crowding:.2}, strength={strength:.2}"
    );

    RegimeState { strength, trend, vol, credit, crowding }
}

/// Dimension 1: Benchmark price vs MA → [0, 1]
///
/// Above MA by >5% → 1.0 (bullish), below by >5% → 0.0, linear interpolation between.
fn trend_score(cache: &DataCache, config: &RegimeConfig, date: Date) -> f64 {
    let index = &config.index;
    let window = config.ma_window;

    // Collect recent index closes
    let cal = &cache.trading_days;
    let end_pos = match cal.binary_search(&date) {
        Ok(i) => i,
        Err(i) if i > 0 => i - 1,
        _ => return 0.5,
    };
    let need = window + 5;
    if end_pos + 1 < need {
        return 0.5;
    }

    let mut prices = Vec::with_capacity(need);
    for i in (0..=end_pos).rev().take(need) {
        let d = cal[i];
        if let Some(&p) = cache.index_prices.get(&(index.clone(), d)) {
            if p > 0.0 && p.is_finite() {
                prices.push(p);
            }
        }
    }

    if prices.len() < window {
        return 0.5;
    }

    let current = prices[0];
    let ma: f64 = prices[..window].iter().sum::<f64>() / window as f64;
    if ma <= 0.0 {
        return 0.5;
    }

    let dev = (current - ma) / ma;
    if dev >= TRANSITION_BAND {
        1.0
    } else if dev <= -TRANSITION_BAND {
        0.0
    } else {
        (dev + TRANSITION_BAND) / (2.0 * TRANSITION_BAND)
    }
}

/// Dimension 2: Realized volatility percentile → [0, 1]
///
/// Uses rolling 20-day realized vol of the benchmark index,
/// then computes its percentile over the past year.
/// Low vol percentile → bullish (1.0), high vol percentile → bearish (0.0).
fn vol_score(cache: &DataCache, config: &RegimeConfig, date: Date) -> f64 {
    let index = &config.index;
    let cal = &cache.trading_days;

    let end_pos = match cal.binary_search(&date) {
        Ok(i) => i,
        Err(i) if i > 0 => i - 1,
        _ => return 0.5,
    };

    let need = VOL_LOOKBACK + 25; // enough for 252 rolling windows of 20-day vol
    if end_pos + 1 < need {
        return 0.5;
    }

    // Collect index returns
    let mut returns = Vec::with_capacity(need);
    let mut prev_price = f64::NAN;
    for i in (0..=end_pos).rev().take(need) {
        let d = cal[i];
        if let Some(&p) = cache.index_prices.get(&(index.clone(), d)) {
            if p > 0.0 && p.is_finite() {
                if prev_price.is_finite() {
                    // Note: we're iterating newest-first, so return = ln(newer/older)
                    returns.push((prev_price / p).ln());
                }
                prev_price = p;
            }
        }
    }
    // Reverse so returns[0] = oldest
    returns.reverse();

    if returns.len() < VOL_LOOKBACK {
        return 0.5;
    }

    // Compute rolling 20-day realized vol
    let vol_window = 20;
    let mut vols = Vec::with_capacity(returns.len() - vol_window + 1);
    for start in 0..=(returns.len() - vol_window) {
        let slice = &returns[start..start + vol_window];
        let mean = slice.iter().sum::<f64>() / vol_window as f64;
        let var = slice.iter().map(|r| (r - mean).powi(2)).sum::<f64>() / (vol_window - 1) as f64;
        vols.push(var.sqrt() * (252.0_f64).sqrt()); // annualize
    }

    if vols.len() < 20 {
        return 0.5;
    }

    let current_vol = *vols.last().unwrap();
    let pct = vols.iter().filter(|&&v| v <= current_vol).count() as f64 / vols.len() as f64;

    // Low percentile = low vol = bullish → 1.0
    // High percentile = high vol = bearish → 0.0
    if pct <= 0.2 {
        1.0
    } else if pct >= 0.8 {
        0.0
    } else {
        1.0 - (pct - 0.2) / 0.6
    }
}

/// Dimension 3: Credit spread → [0, 1]
///
/// Requires macro indicator data (10Y-2Y spread).
/// Currently returns neutral 0.5 when running from parquet cache.
/// Will be fully functional once DB layer is connected (Phase 1).
fn credit_score(_cache: &DataCache, _date: Date) -> f64 {
    // TODO: Phase 1 — read from macro indicators table via DB
    // For now, neutral (does not bias regime detection)
    0.5
}

/// Dimension 4: Momentum cross-sectional dispersion → [0, 1]
///
/// High dispersion = factor signals are differentiated = factors working → bullish
/// Low dispersion = crowded = factors losing efficacy → bearish
fn crowding_score(cache: &DataCache, date: Date) -> f64 {
    let cal = &cache.trading_days;
    let end_pos = match cal.binary_search(&date) {
        Ok(i) => i,
        Err(i) if i > 0 => i - 1,
        _ => return 0.5,
    };

    if end_pos < 60 {
        return 0.5;
    }

    // Sample momentum dispersion every 5 trading days over past year
    let sample_step = 5;
    let max_samples = 50;
    let mut dispersions = Vec::with_capacity(max_samples);

    let mut idx = end_pos;
    while dispersions.len() < max_samples && idx >= 20 {
        let d = cal[idx];
        // Collect 20-day returns for all stocks on this date
        let mut rets = Vec::new();
        for (tid, bar) in cache.daily_prices.iter_date(d) {
            if bar.cum_ret_20d.is_finite() {
                rets.push(bar.cum_ret_20d);
            }
        }
        if rets.len() > 20 {
            let mean = rets.iter().sum::<f64>() / rets.len() as f64;
            let var = rets.iter().map(|r| (r - mean).powi(2)).sum::<f64>() / (rets.len() - 1) as f64;
            dispersions.push(var.sqrt());
        }
        if idx < sample_step {
            break;
        }
        idx -= sample_step;
    }

    if dispersions.len() < 10 {
        return 0.5;
    }

    // Current dispersion = most recent (first computed, which was at end_pos)
    let current = dispersions[0];
    let pct = dispersions.iter().filter(|&&v| v <= current).count() as f64 / dispersions.len() as f64;

    // High dispersion percentile = factors working = bullish
    if pct >= 0.8 {
        1.0
    } else if pct <= 0.2 {
        0.0
    } else {
        (pct - 0.2) / 0.6
    }
}

/// Map regime strength to holdings ratio.
/// Returns the fraction of normal position sizing to use.
///
/// bull (≥0.8) → 1.0, bear (≤0.3) → bear_ratio, linear between.
pub fn holdings_ratio(strength: f64, bear_ratio: f64) -> f64 {
    if strength >= 0.8 {
        1.0
    } else if strength <= 0.3 {
        bear_ratio
    } else {
        let t = (strength - 0.3) / 0.5;
        bear_ratio + t * (1.0 - bear_ratio)
    }
}
