//! Ownership factors: DARK_POOL_SHORT, INST_OWNERSHIP_DELTA

use qrs_core::types::{Date, FactorResult};
use qrs_data::cache::DataCache;
use crate::registry::Factor;

/// Dark Pool Short Interest: mean DPI over ~20 trading days
pub struct DarkPoolShort;
inventory::submit! { &DarkPoolShort as &dyn Factor }
impl Factor for DarkPoolShort {
    fn name(&self) -> &'static str { "DARK_POOL_SHORT" }
    fn category(&self) -> &'static str { "ownership" }
    fn inherent_direction(&self) -> i8 { -1 }
    fn ic_window_months(&self) -> u32 { 12 }
    fn compute(&self, date: Date, cache: &DataCache) -> FactorResult {
        let start = date - chrono::Duration::days(35);
        let mut result = FactorResult::default();
        for (&tid, records) in &cache.dark_pool {
            let recent: Vec<f64> = records.iter()
                .filter(|r| r.date >= start && r.date <= date)
                .map(|r| r.dpi)
                .filter(|v| v.is_finite())
                .collect();
            if recent.len() >= 5 {
                let mean = recent.iter().sum::<f64>() / recent.len() as f64;
                if mean.is_finite() { result.insert(tid, mean); }
            }
        }
        result
    }
}

/// Institutional Ownership Delta: change in 13F shares between latest 2 periods
pub struct InstOwnershipDelta;
inventory::submit! { &InstOwnershipDelta as &dyn Factor }
impl Factor for InstOwnershipDelta {
    fn name(&self) -> &'static str { "INST_OWNERSHIP_DELTA" }
    fn category(&self) -> &'static str { "ownership" }
    fn inherent_direction(&self) -> i8 { 1 }
    fn ic_window_months(&self) -> u32 { 12 }
    fn compute(&self, date: Date, cache: &DataCache) -> FactorResult {
        let mut result = FactorResult::default();
        for (&tid, records) in &cache.institutional {
            let recent: Vec<_> = records.iter()
                .filter(|r| r.date <= date)
                .take(2)
                .collect();
            if recent.len() < 2 { continue; }
            let latest = recent[0].number_of_13f_shares;
            let prev = recent[1].number_of_13f_shares;
            if prev.is_finite() && prev.abs() > 1e-6 && latest.is_finite() {
                let delta = (latest - prev) / prev.abs();
                if delta.is_finite() { result.insert(tid, delta); }
            }
        }
        result
    }
}
