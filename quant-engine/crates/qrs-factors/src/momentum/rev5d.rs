//! REV_5D: 5-Day Reversal = -1 * cumulative 5-day return

use qrs_core::types::{Date, FactorResult};
use qrs_data::cache::DataCache;

use crate::registry::Factor;

pub struct Rev5D;
inventory::submit! { &Rev5D as &dyn Factor }

impl Factor for Rev5D {
    fn name(&self) -> &'static str { "REV_5D" }
    fn category(&self) -> &'static str { "momentum" }
    fn inherent_direction(&self) -> i8 { 0 }
    fn ic_window_months(&self) -> u32 { 6 }

    fn compute(&self, date: Date, cache: &DataCache) -> FactorResult {
        let mut result = FactorResult::default();
        for (tid, stats) in cache.daily_prices.iter_date(date) {
            let ret5 = stats.cum_ret_5d;
            if ret5.is_finite() {
                result.insert(tid, -ret5); // Reversal: negate
            }
        }
        result
    }
}
