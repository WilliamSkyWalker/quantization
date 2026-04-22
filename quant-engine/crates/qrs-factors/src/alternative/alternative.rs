//! Alternative factors: ESG_RISK, EMPLOYEE_GROWTH, CONGRESS_NET_BUY,
//! GOV_CONTRACT_FLOW, LOBBY_INTENSITY, REV_CONCENTRATION, GEO_CONCENTRATION, SEGMENT_GROWTH_DISP

use qrs_core::types::{Date, FactorResult};
use qrs_data::cache::DataCache;
use crate::registry::Factor;

// Note: Most alternative factors need specialized tables (esg_rating, employee_count,
// congress_trade, gov_contract, lobbying, revenue_segment) that aren't fully loaded
// in the DataCache builder yet. They'll return empty until those tables are added.

pub struct EsgRisk;
inventory::submit! { &EsgRisk as &dyn Factor }
impl Factor for EsgRisk {
    fn name(&self) -> &'static str { "ESG_RISK" }
    fn category(&self) -> &'static str { "alternative" }
    fn inherent_direction(&self) -> i8 { 0 }
    fn ic_window_months(&self) -> u32 { 36 }
    fn compute(&self, _date: Date, _cache: &DataCache) -> FactorResult {
        // TODO: Needs esg_rating table in DataCache
        FactorResult::default()
    }
}

pub struct EmployeeGrowth;
inventory::submit! { &EmployeeGrowth as &dyn Factor }
impl Factor for EmployeeGrowth {
    fn name(&self) -> &'static str { "EMPLOYEE_GROWTH" }
    fn category(&self) -> &'static str { "alternative" }
    fn inherent_direction(&self) -> i8 { 0 }
    fn ic_window_months(&self) -> u32 { 24 }
    fn compute(&self, _date: Date, _cache: &DataCache) -> FactorResult {
        // TODO: Needs employee_count table in DataCache
        FactorResult::default()
    }
}

pub struct CongressNetBuy;
inventory::submit! { &CongressNetBuy as &dyn Factor }
impl Factor for CongressNetBuy {
    fn name(&self) -> &'static str { "CONGRESS_NET_BUY" }
    fn category(&self) -> &'static str { "alternative" }
    fn inherent_direction(&self) -> i8 { 1 }
    fn ic_window_months(&self) -> u32 { 12 }
    fn compute(&self, _date: Date, _cache: &DataCache) -> FactorResult {
        // TODO: Needs congress_trade table in DataCache
        FactorResult::default()
    }
}

pub struct GovContractFlow;
inventory::submit! { &GovContractFlow as &dyn Factor }
impl Factor for GovContractFlow {
    fn name(&self) -> &'static str { "GOV_CONTRACT_FLOW" }
    fn category(&self) -> &'static str { "alternative" }
    fn inherent_direction(&self) -> i8 { 1 }
    fn ic_window_months(&self) -> u32 { 12 }
    fn compute(&self, _date: Date, _cache: &DataCache) -> FactorResult {
        // TODO: Needs gov_contract table in DataCache
        FactorResult::default()
    }
}

pub struct LobbyIntensity;
inventory::submit! { &LobbyIntensity as &dyn Factor }
impl Factor for LobbyIntensity {
    fn name(&self) -> &'static str { "LOBBY_INTENSITY" }
    fn category(&self) -> &'static str { "alternative" }
    fn inherent_direction(&self) -> i8 { 1 }
    fn ic_window_months(&self) -> u32 { 12 }
    fn compute(&self, _date: Date, _cache: &DataCache) -> FactorResult {
        // TODO: Needs lobbying table in DataCache
        FactorResult::default()
    }
}

pub struct RevConcentration;
inventory::submit! { &RevConcentration as &dyn Factor }
impl Factor for RevConcentration {
    fn name(&self) -> &'static str { "REV_CONCENTRATION" }
    fn category(&self) -> &'static str { "alternative" }
    fn inherent_direction(&self) -> i8 { 0 }
    fn ic_window_months(&self) -> u32 { 24 }
    fn compute(&self, _date: Date, _cache: &DataCache) -> FactorResult {
        // TODO: Needs revenue_segment table in DataCache
        FactorResult::default()
    }
}

pub struct GeoConcentration;
inventory::submit! { &GeoConcentration as &dyn Factor }
impl Factor for GeoConcentration {
    fn name(&self) -> &'static str { "GEO_CONCENTRATION" }
    fn category(&self) -> &'static str { "alternative" }
    fn inherent_direction(&self) -> i8 { 0 }
    fn ic_window_months(&self) -> u32 { 24 }
    fn compute(&self, _date: Date, _cache: &DataCache) -> FactorResult {
        // TODO: Needs revenue_segment table in DataCache
        FactorResult::default()
    }
}

pub struct SegmentGrowthDisp;
inventory::submit! { &SegmentGrowthDisp as &dyn Factor }
impl Factor for SegmentGrowthDisp {
    fn name(&self) -> &'static str { "SEGMENT_GROWTH_DISP" }
    fn category(&self) -> &'static str { "alternative" }
    fn inherent_direction(&self) -> i8 { 0 }
    fn ic_window_months(&self) -> u32 { 24 }
    fn compute(&self, _date: Date, _cache: &DataCache) -> FactorResult {
        // TODO: Needs revenue_segment table in DataCache
        FactorResult::default()
    }
}
