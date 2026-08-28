//! Earnings factors: EARNINGS_SURPRISE, EPS_REVISION, BUYBACK_YIELD

use quant_core::types::{Date, FactorResult};
use quant_data::cache::DataCache;

use crate::registry::Factor;

/// Earnings Surprise: most recent surprise_pct within 120 days
pub struct EarningsSurprise;
inventory::submit! { &EarningsSurprise as &dyn Factor }

impl Factor for EarningsSurprise {
    fn name(&self) -> &'static str { "EARNINGS_SURPRISE" }
    fn category(&self) -> &'static str { "analyst" }
    fn inherent_direction(&self) -> i8 { 0 }
    fn ic_window_months(&self) -> u32 { 6 }

    fn compute(&self, date: Date, cache: &DataCache) -> FactorResult {
        let start = date - chrono::Duration::days(120);
        let mut result = FactorResult::default();

        for (&tid, recs) in &cache.earnings_surprises {
            // Records sorted by date desc — first matching is most recent
            if let Some(rec) = recs.iter().find(|r| r.date >= start && r.date <= date) {
                let sp = rec.surprise_pct;
                if sp.is_finite() {
                    result.insert(tid, sp);
                }
            }
        }
        result
    }
}

/// EPS Revision: (recent EPS estimate - prior) / |prior|
#[allow(dead_code)]
pub struct EpsRevision;
// DEPRECATED 2026-04-30: misnamed. FMP `us_eps_estimate.date` is the FORECAST
// PERIOD (e.g., 2024-Q1 target = 2024-03-31), not the publication date of the
// estimate. So this factor compares CONSENSUS-EPS for two adjacent periods
// (essentially QoQ earnings growth), NOT the change in consensus over time
// for the same period (true Stickel 1991 EPS revision).
// True EPS_REVISION needs a PIT snapshot table (see MEMORY P3 task
// `EPS point-in-time 快照积累`). Until that exists, disable this signal —
// IC=-0.010 confirms the formula carries no real revision info.
// inventory::submit! { &EpsRevision as &dyn Factor }

impl Factor for EpsRevision {
    fn name(&self) -> &'static str { "EPS_REVISION" }
    fn category(&self) -> &'static str { "analyst" }
    fn inherent_direction(&self) -> i8 { 0 }
    fn ic_window_months(&self) -> u32 { 6 }

    fn compute(&self, date: Date, cache: &DataCache) -> FactorResult {
        let mut result = FactorResult::default();

        for (&tid, recs) in &cache.eps_estimates {
            // Get 2 most recent fiscal periods where date <= current date
            let recent: Vec<_> = recs
                .iter()
                .filter(|r| r.date <= date)
                .take(2)
                .collect();

            if recent.len() < 2 {
                continue;
            }

            let eps_recent = recent[0].estimated_eps_avg;
            let eps_prev = recent[1].estimated_eps_avg;

            if !eps_recent.is_finite() || !eps_prev.is_finite() || eps_prev.abs() < 0.01 {
                continue;
            }

            let revision = (eps_recent - eps_prev) / eps_prev.abs();
            if revision.is_finite() {
                result.insert(tid, revision);
            }
        }
        result
    }
}

/// Buyback Yield: -net_stock_issuance (TTM) / market_cap
/// Negative issuance = repurchase = positive factor
pub struct BuybackYield;
inventory::submit! { &BuybackYield as &dyn Factor }

impl Factor for BuybackYield {
    fn name(&self) -> &'static str { "BUYBACK_YIELD" }
    fn category(&self) -> &'static str { "value" }
    fn inherent_direction(&self) -> i8 { 0 }
    fn ic_window_months(&self) -> u32 { 24 }

    fn compute(&self, date: Date, cache: &DataCache) -> FactorResult {
        let mut result = FactorResult::default();

        for (&tid, records) in &cache.financials {
            let qualifying: Vec<f64> = records
                .iter()
                .filter(|r| r.filing_date <= date)
                .take(4)
                .filter_map(|r| r.fields.get("net_stock_issuance").copied())
                .filter(|v| v.is_finite())
                .collect();

            if qualifying.len() < 3 {
                continue;
            }
            // Negative issuance = repurchase
            let ttm_repurchase: f64 = -qualifying.iter().sum::<f64>();

            let mktcap = match cache.get_market_cap(tid, date) {
                Some(m) if m > 0.0 => m,
                _ => continue,
            };

            let buyback_yield = ttm_repurchase / mktcap;
            if buyback_yield.is_finite() {
                result.insert(tid, buyback_yield);
            }
        }
        result
    }
}
