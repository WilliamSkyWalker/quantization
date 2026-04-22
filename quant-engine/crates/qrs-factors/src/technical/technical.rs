//! Technical factors: TURN_20D, VOL_20D, SIZE

use qrs_core::types::{Date, FactorResult};
use qrs_data::cache::DataCache;

use crate::registry::Factor;

/// 20-Day Average Dollar Volume (liquidity proxy, log-transformed)
pub struct Turn20D;
inventory::submit! { &Turn20D as &dyn Factor }

impl Factor for Turn20D {
    fn name(&self) -> &'static str { "TURN_20D" }
    fn category(&self) -> &'static str { "technical" }
    fn inherent_direction(&self) -> i8 { 0 }
    fn ic_window_months(&self) -> u32 { 24 }

    fn compute(&self, date: Date, cache: &DataCache) -> FactorResult {
        let mut result = FactorResult::default();
        for (tid, stats) in cache.daily_prices.iter_date(date) {
            let dvol = stats.dvol_20d;
            if dvol.is_finite() && dvol > 0.0 {
                result.insert(tid, (1.0 + dvol).ln());
            }
        }
        result
    }
}

/// 20-Day Volatility (inverse: lower vol = higher factor value)
pub struct Vol20D;
inventory::submit! { &Vol20D as &dyn Factor }

impl Factor for Vol20D {
    fn name(&self) -> &'static str { "VOL_20D" }
    fn category(&self) -> &'static str { "technical" }
    fn inherent_direction(&self) -> i8 { -1 }
    fn ic_window_months(&self) -> u32 { 24 }

    fn compute(&self, date: Date, cache: &DataCache) -> FactorResult {
        let mut result = FactorResult::default();
        for (tid, stats) in cache.daily_prices.iter_date(date) {
            let vol = stats.vol_20d;
            if vol.is_finite() {
                result.insert(tid, -vol); // Inverse: low vol = good
            }
        }
        result
    }
}

/// Size: -log(market_cap) — favors smaller stocks
pub struct Size;
inventory::submit! { &Size as &dyn Factor }

impl Factor for Size {
    fn name(&self) -> &'static str { "SIZE" }
    fn category(&self) -> &'static str { "technical" }
    fn inherent_direction(&self) -> i8 { 0 }
    fn ic_window_months(&self) -> u32 { 36 }

    fn compute(&self, date: Date, cache: &DataCache) -> FactorResult {
        let mut result = FactorResult::default();
        for (&ticker_id, records) in &cache.enterprise_values {
            let mktcap = match records.iter().find(|r| r.date <= date) {
                Some(r) if r.market_cap > 0.0 && r.market_cap.is_finite() => r.market_cap,
                _ => continue,
            };
            result.insert(ticker_id, -mktcap.ln());
        }
        result
    }
}
