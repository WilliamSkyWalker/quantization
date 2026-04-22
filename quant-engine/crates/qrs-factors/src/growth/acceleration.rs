//! REVENUE_ACCELERATION: 2nd derivative of revenue growth.
//! Measures whether revenue growth is ACCELERATING (positive) or DECELERATING (negative).
//!
//! Calculation:
//!   recent_growth = revenue_TTM(Q0-Q3) / revenue_TTM(Q4-Q7) - 1
//!   prior_growth  = revenue_TTM(Q4-Q7) / revenue_TTM(Q8-Q11) - 1
//!   acceleration  = recent_growth - prior_growth
//!
//! Example: NVDA 2023 — revenue YoY went from +50% to +200% → acceleration = +150pp

use qrs_core::types::{Date, FactorResult};
use qrs_data::cache::DataCache;
use crate::registry::Factor;

pub struct RevenueAcceleration;
inventory::submit! { &RevenueAcceleration as &dyn Factor }

impl Factor for RevenueAcceleration {
    fn name(&self) -> &'static str { "REVENUE_ACCELERATION" }
    fn category(&self) -> &'static str { "growth" }
    fn inherent_direction(&self) -> i8 { 1 }
    fn ic_window_months(&self) -> u32 { 12 }

    fn compute(&self, date: Date, cache: &DataCache) -> FactorResult {
        let mut result = FactorResult::default();

        for (&tid, records) in &cache.financials {
            // Need 12 quarters: Q0-Q3 (current TTM), Q4-Q7 (1Y ago TTM), Q8-Q11 (2Y ago TTM)
            let revenues: Vec<f64> = records.iter()
                .filter(|r| r.filing_date <= date)
                .take(12)
                .filter_map(|r| r.fields.get("revenue").copied().filter(|v| v.is_finite()))
                .collect();

            if revenues.len() < 12 { continue; }

            let ttm_current: f64 = revenues[0..4].iter().sum();
            let ttm_1y_ago: f64 = revenues[4..8].iter().sum();
            let ttm_2y_ago: f64 = revenues[8..12].iter().sum();

            if ttm_1y_ago.abs() < 1e-6 || ttm_2y_ago.abs() < 1e-6 { continue; }

            let recent_growth = ttm_current / ttm_1y_ago.abs() - 1.0;
            let prior_growth = ttm_1y_ago / ttm_2y_ago.abs() - 1.0;

            let acceleration = recent_growth - prior_growth;

            if acceleration.is_finite() {
                result.insert(tid, acceleration);
            }
        }

        result
    }
}
