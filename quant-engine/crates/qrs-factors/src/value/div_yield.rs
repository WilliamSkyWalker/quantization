//! DIV_YIELD (Dividend Yield): trailing 12M dividends / adj_close

use qrs_core::types::{Date, FactorResult};
use qrs_data::cache::DataCache;

use crate::registry::Factor;

pub struct DivYield;

inventory::submit! { &DivYield as &dyn Factor }

impl Factor for DivYield {
    fn name(&self) -> &'static str { "DIV_YIELD" }
    fn category(&self) -> &'static str { "value" }
    fn inherent_direction(&self) -> i8 { 0 }
    fn ic_window_months(&self) -> u32 { 30 }

    fn compute(&self, date: Date, cache: &DataCache) -> FactorResult {
        let mut result = FactorResult::default();

        for (&ticker_id, _) in &cache.dividends {
            let total_div = cache.get_trailing_dividends(ticker_id, date, 365);
            if total_div <= 0.0 {
                continue;
            }

            let close = match cache.get_close(ticker_id, date) {
                Some(c) if c > 0.0 && c.is_finite() => c,
                _ => continue,
            };

            let dy = total_div / close;
            if dy.is_finite() {
                result.insert(ticker_id, dy);
            }
        }

        result
    }
}
