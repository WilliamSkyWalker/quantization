//! Size factors: LOG_MARKET_CAP, FREE_FLOAT_PCT

use quant_core::types::{Date, FactorResult};
use quant_data::cache::DataCache;
use crate::registry::Factor;

pub struct LogMarketCap;
inventory::submit! { &LogMarketCap as &dyn Factor }
impl Factor for LogMarketCap {
    fn name(&self) -> &'static str { "LOG_MARKET_CAP" }
    fn category(&self) -> &'static str { "size" }
    fn inherent_direction(&self) -> i8 { 0 }
    fn ic_window_months(&self) -> u32 { 36 }
    fn compute(&self, date: Date, cache: &DataCache) -> FactorResult {
        let mut result = FactorResult::default();
        for (&tid, records) in &cache.enterprise_values {
            if let Some(rec) = records.iter().find(|r| r.date <= date) {
                if rec.market_cap > 0.0 && rec.market_cap.is_finite() {
                    result.insert(tid, rec.market_cap.ln());
                }
            }
        }
        result
    }
}

pub struct FreeFloatPct;
inventory::submit! { &FreeFloatPct as &dyn Factor }
impl Factor for FreeFloatPct {
    fn name(&self) -> &'static str { "FREE_FLOAT_PCT" }
    fn category(&self) -> &'static str { "size" }
    fn inherent_direction(&self) -> i8 { 0 }
    fn ic_window_months(&self) -> u32 { 36 }
    fn compute(&self, _date: Date, cache: &DataCache) -> FactorResult {
        let mut result = FactorResult::default();
        for (&tid, sf) in &cache.shares_float {
            if sf.free_float.is_finite() && sf.free_float > 0.0 {
                result.insert(tid, sf.free_float);
            }
        }
        result
    }
}
