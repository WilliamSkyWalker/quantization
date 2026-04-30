//! Advanced analyst: DAYS_SINCE_EARNINGS, ANALYST_DISPERSION, PRICE_TARGET_RATIO, REC_CHANGE

use quant_core::types::{Date, FactorResult};
use quant_data::cache::DataCache;
use crate::registry::Factor;

/// Days since most recent earnings announcement
pub struct DaysSinceEarnings;
inventory::submit! { &DaysSinceEarnings as &dyn Factor }
impl Factor for DaysSinceEarnings {
    fn name(&self) -> &'static str { "DAYS_SINCE_EARNINGS" }
    fn category(&self) -> &'static str { "analyst" }
    fn inherent_direction(&self) -> i8 { 0 }
    fn ic_window_months(&self) -> u32 { 6 }
    fn compute(&self, date: Date, cache: &DataCache) -> FactorResult {
        let mut result = FactorResult::default();
        for (&tid, recs) in &cache.earnings_surprises {
            if let Some(rec) = recs.iter().find(|r| r.date <= date) {
                let days = (date - rec.date).num_days() as f64;
                if days >= 0.0 && days <= 200.0 { result.insert(tid, days); }
            }
        }
        result
    }
}

/// Analyst Dispersion: (EPS_high - EPS_low) / |EPS_avg|
pub struct AnalystDispersion;
inventory::submit! { &AnalystDispersion as &dyn Factor }
impl Factor for AnalystDispersion {
    fn name(&self) -> &'static str { "ANALYST_DISPERSION" }
    fn category(&self) -> &'static str { "analyst" }
    fn inherent_direction(&self) -> i8 { -1 }
    fn ic_window_months(&self) -> u32 { 12 }
    fn compute(&self, date: Date, cache: &DataCache) -> FactorResult {
        let mut result = FactorResult::default();
        for (&tid, recs) in &cache.eps_estimates {
            if let Some(rec) = recs.iter().find(|r| r.date <= date) {
                let avg = rec.estimated_eps_avg;
                let hi = rec.estimated_eps_high;
                let lo = rec.estimated_eps_low;
                if avg.is_finite() && avg.abs() > 0.01 && hi.is_finite() && lo.is_finite() {
                    let disp = (hi - lo) / avg.abs();
                    if disp.is_finite() { result.insert(tid, disp); }
                }
            }
        }
        result
    }
}

/// Price Target Ratio: forward EPS / price (simplified)
pub struct PriceTargetRatio;
inventory::submit! { &PriceTargetRatio as &dyn Factor }
impl Factor for PriceTargetRatio {
    fn name(&self) -> &'static str { "PRICE_TARGET_RATIO" }
    fn category(&self) -> &'static str { "analyst" }
    fn inherent_direction(&self) -> i8 { 1 }
    fn ic_window_months(&self) -> u32 { 12 }
    fn compute(&self, date: Date, cache: &DataCache) -> FactorResult {
        let mut result = FactorResult::default();
        // Use forward EPS estimate / price as proxy for price target ratio
        for (&tid, recs) in &cache.eps_estimates {
            if let Some(rec) = recs.iter().find(|r| r.date <= date) {
                let fwd_eps = rec.estimated_eps_avg;
                if !fwd_eps.is_finite() { continue; }
                let close = match cache.get_close(tid, date) {
                    Some(c) if c > 0.0 => c, _ => continue
                };
                let ratio = fwd_eps / close;
                if ratio.is_finite() { result.insert(tid, ratio); }
            }
        }
        result
    }
}

/// Recommendation Change: mean rating level over 90 days
/// Note 2026-04-30: name is misleading (computes mean LEVEL, not change).
/// True delta version (recent_45d_mean - older_45d_mean) was tested and hurt
/// 14y backtest by -0.7%/year — the "level" version functions as a proxy for
/// "current rating quality" and works empirically. Renaming would lie less
/// but the implementation is left as-is to preserve alpha.
pub struct RecChange;
inventory::submit! { &RecChange as &dyn Factor }
impl Factor for RecChange {
    fn name(&self) -> &'static str { "REC_CHANGE" }
    fn category(&self) -> &'static str { "analyst" }
    fn inherent_direction(&self) -> i8 { 1 }
    fn ic_window_months(&self) -> u32 { 6 }
    fn compute(&self, date: Date, cache: &DataCache) -> FactorResult {
        let start = date - chrono::Duration::days(90);
        let mut result = FactorResult::default();
        fn grade_to_score(g: &str) -> Option<f64> {
            match g.trim() {
                "Strong Buy" => Some(5.0), "Buy" | "Outperform" | "Overweight" => Some(4.0),
                "Hold" | "Neutral" | "Market Perform" | "Equal-Weight" | "Sector Perform" => Some(3.0),
                "Underperform" | "Underweight" | "Reduce" => Some(2.0),
                "Sell" | "Strong Sell" => Some(1.0), _ => None,
            }
        }
        for (&tid, recs) in &cache.analyst_recs {
            let recent: Vec<f64> = recs.iter()
                .filter(|r| r.date >= start && r.date <= date)
                .filter_map(|r| grade_to_score(&r.new_grade))
                .collect();
            if recent.len() >= 2 {
                // Average grade level as proxy for recommendation direction
                let mean = recent.iter().sum::<f64>() / recent.len() as f64;
                result.insert(tid, mean);
            }
        }
        result
    }
}
