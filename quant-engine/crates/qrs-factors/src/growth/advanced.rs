//! Advanced growth: REVENUE_GROWTH, EARNINGS_GROWTH, RD_INTENSITY, CAPEX_GROWTH, GROSS_MARGIN_CHG

use qrs_core::types::{Date, FactorResult};
use qrs_data::cache::DataCache;
use crate::registry::Factor;

fn yoy(cache: &DataCache, tid: qrs_core::types::TickerId, date: Date, field: &str) -> Option<(f64, f64)> {
    let records = cache.financials.get(&tid)?;
    let recent: Vec<_> = records.iter().filter(|r| r.filing_date <= date).take(5).collect();
    if recent.len() < 5 { return None; }
    let now = recent[0].fields.get(field).copied().filter(|v| v.is_finite())?;
    let yoy = recent[4].fields.get(field).copied().filter(|v| v.is_finite())?;
    Some((now, yoy))
}

macro_rules! yoy_factor {
    ($name:ident, $sname:expr, $field:expr, $dir:expr) => {
        pub struct $name;
        inventory::submit! { &$name as &dyn Factor }
        impl Factor for $name {
            fn name(&self) -> &'static str { $sname }
            fn category(&self) -> &'static str { "growth" }
            fn inherent_direction(&self) -> i8 { $dir }
            fn ic_window_months(&self) -> u32 { 24 }
            fn compute(&self, date: Date, cache: &DataCache) -> FactorResult {
                let mut r = FactorResult::default();
                for &tid in cache.financials.keys() {
                    if let Some((now, prev)) = yoy(cache, tid, date, $field) {
                        if prev.abs() > 1e-6 { let v = (now - prev) / prev.abs(); if v.is_finite() { r.insert(tid, v); } }
                    }
                }
                r
            }
        }
    };
}

yoy_factor!(RevenueGrowth, "REVENUE_GROWTH", "revenue", 1);
yoy_factor!(EarningsGrowth, "EARNINGS_GROWTH", "net_income", 1);
yoy_factor!(CapexGrowth, "CAPEX_GROWTH", "capital_expenditure", 0);

pub struct GrossMarginChg;
inventory::submit! { &GrossMarginChg as &dyn Factor }
impl Factor for GrossMarginChg {
    fn name(&self) -> &'static str { "GROSS_MARGIN_CHG" }
    fn category(&self) -> &'static str { "growth" }
    fn inherent_direction(&self) -> i8 { 1 }
    fn ic_window_months(&self) -> u32 { 24 }
    fn compute(&self, date: Date, cache: &DataCache) -> FactorResult {
        let mut r = FactorResult::default();
        for (&tid, records) in &cache.financials {
            let recent: Vec<_> = records.iter().filter(|r| r.filing_date <= date).take(5).collect();
            if recent.len() < 5 { continue; }
            let gm = |i: usize| -> Option<f64> {
                let gp = recent[i].fields.get("gross_profit")?.to_owned();
                let rev = recent[i].fields.get("revenue")?.to_owned();
                if rev.abs() > 1e-6 { Some(gp / rev) } else { None }
            };
            if let (Some(now), Some(prev)) = (gm(0), gm(4)) {
                let delta = now - prev;
                if delta.is_finite() { r.insert(tid, delta); }
            }
        }
        r
    }
}

pub struct RdIntensity;
inventory::submit! { &RdIntensity as &dyn Factor }
impl Factor for RdIntensity {
    fn name(&self) -> &'static str { "RD_INTENSITY" }
    fn category(&self) -> &'static str { "growth" }
    fn inherent_direction(&self) -> i8 { 1 }
    fn ic_window_months(&self) -> u32 { 24 }
    fn compute(&self, date: Date, cache: &DataCache) -> FactorResult {
        let mut r = FactorResult::default();
        for (&tid, records) in &cache.financials {
            let rec = match records.iter().find(|r| r.filing_date <= date) { Some(r) => r, None => continue };
            let rd = match rec.fields.get("research_and_development_expenses").copied() { Some(v) if v.is_finite() && v > 0.0 => v, _ => continue };
            let rev = match rec.fields.get("revenue").copied() { Some(v) if v.is_finite() && v.abs() > 1e-6 => v, _ => continue };
            let v = rd / rev; if v.is_finite() { r.insert(tid, v); }
        }
        r
    }
}
