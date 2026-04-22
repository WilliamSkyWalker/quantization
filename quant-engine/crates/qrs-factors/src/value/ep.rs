//! EP (Earnings-to-Price): TTM EPS / adj_close

use qrs_core::types::{Date, FactorResult};
use qrs_data::cache::DataCache;

use crate::registry::Factor;

pub struct Ep;

inventory::submit! { &Ep as &dyn Factor }

impl Factor for Ep {
    fn name(&self) -> &'static str { "EP" }
    fn category(&self) -> &'static str { "value" }
    fn inherent_direction(&self) -> i8 { 0 }
    fn ic_window_months(&self) -> u32 { 30 }

    fn compute(&self, date: Date, cache: &DataCache) -> FactorResult {
        let mut result = FactorResult::default();

        for (&ticker_id, records) in &cache.financials {
            // TTM EPS: sum of latest 4 quarters' eps where filing_date <= date
            let qualifying: Vec<f64> = records
                .iter()
                .filter(|r| r.filing_date <= date)
                .take(4)
                .filter_map(|r| r.fields.get("eps").copied())
                .filter(|v| v.is_finite())
                .collect();

            if qualifying.len() < 3 {
                continue;
            }
            let ttm_eps: f64 = qualifying.iter().sum();

            // Get close price on date
            let close = match cache.get_close(ticker_id, date) {
                Some(c) if c > 0.0 && c.is_finite() => c,
                _ => continue,
            };

            let ep = ttm_eps / close;
            if ep.is_finite() {
                result.insert(ticker_id, ep);
            }
        }

        result
    }
}
