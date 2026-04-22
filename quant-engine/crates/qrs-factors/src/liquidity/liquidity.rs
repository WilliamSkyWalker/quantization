//! Liquidity: AMIHUD_ILLIQ

use qrs_core::types::{Date, FactorResult};
use qrs_data::cache::DataCache;
use crate::registry::Factor;

/// Amihud Illiquidity: log(mean(|return| / dollar_volume)) over ~21 days
pub struct AmihudIlliq;
inventory::submit! { &AmihudIlliq as &dyn Factor }
impl Factor for AmihudIlliq {
    fn name(&self) -> &'static str { "AMIHUD_ILLIQ" }
    fn category(&self) -> &'static str { "liquidity" }
    fn inherent_direction(&self) -> i8 { 0 }
    fn ic_window_months(&self) -> u32 { 24 }
    fn compute(&self, date: Date, cache: &DataCache) -> FactorResult {
        let start = date - chrono::Duration::days(35);
        let mut result = FactorResult::default();
        let mut ticker_ratios: rustc_hash::FxHashMap<qrs_core::types::TickerId, Vec<f64>> = Default::default();
        for (tid, d, bar) in cache.daily_prices.iter_date_range(start, date) {
            let ret = bar.change_percent / 100.0;
            let dvol = bar.close * bar.volume;
            if ret.is_finite() && dvol > 1e-6 {
                ticker_ratios.entry(tid).or_default().push(ret.abs() / dvol);
            }
        }
        for (tid, ratios) in ticker_ratios {
            if ratios.len() < 10 { continue; }
            let mean = ratios.iter().sum::<f64>() / ratios.len() as f64;
            if mean > 0.0 && mean.is_finite() {
                let v = mean.ln();
                if v.is_finite() { result.insert(tid, v); }
            }
        }
        result
    }
}
