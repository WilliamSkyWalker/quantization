//! Ownership factors: DARK_POOL_SHORT, INST_OWNERSHIP_DELTA

use qrs_core::types::{Date, FactorResult, TickerId};
use qrs_data::cache::DataCache;
use crate::registry::Factor;

// Note: DataCache needs dark_pool_volume and institutional_holder tables.
// These aren't loaded yet in the builder, so these factors will return empty for now.
// TODO: Add dark_pool and institutional_holder loading to builder.

pub struct DarkPoolShort;
inventory::submit! { &DarkPoolShort as &dyn Factor }
impl Factor for DarkPoolShort {
    fn name(&self) -> &'static str { "DARK_POOL_SHORT" }
    fn category(&self) -> &'static str { "ownership" }
    fn inherent_direction(&self) -> i8 { -1 }
    fn ic_window_months(&self) -> u32 { 12 }
    fn compute(&self, _date: Date, _cache: &DataCache) -> FactorResult {
        // TODO: Needs dark_pool_volume table in DataCache
        FactorResult::default()
    }
}

pub struct InstOwnershipDelta;
inventory::submit! { &InstOwnershipDelta as &dyn Factor }
impl Factor for InstOwnershipDelta {
    fn name(&self) -> &'static str { "INST_OWNERSHIP_DELTA" }
    fn category(&self) -> &'static str { "ownership" }
    fn inherent_direction(&self) -> i8 { 1 }
    fn ic_window_months(&self) -> u32 { 12 }
    fn compute(&self, _date: Date, _cache: &DataCache) -> FactorResult {
        // TODO: Needs institutional_holder table with period comparison
        FactorResult::default()
    }
}
