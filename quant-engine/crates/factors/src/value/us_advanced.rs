//! Advanced value factors: ASSET_GROWTH, NET_OPERATING_ASSETS, COMPOSITE_ISSUANCE,
//! EV_TO_EBIT, EV_TO_FCF, EV_TO_SALES, INTANGIBLE_ADJ_BP, SHAREHOLDER_YIELD

use quant_core::types::{Date, FactorResult};
use quant_data::cache::DataCache;
use crate::registry::Factor;

macro_rules! simple_factor {
    ($name:ident, $sname:expr, $cat:expr, $dir:expr, $ic:expr, $body:expr) => {
        pub struct $name;
        inventory::submit! { &$name as &dyn Factor }
        impl Factor for $name {
            fn name(&self) -> &'static str { $sname }
            fn category(&self) -> &'static str { $cat }
            fn inherent_direction(&self) -> i8 { $dir }
            fn ic_window_months(&self) -> u32 { $ic }
            fn compute(&self, date: Date, cache: &DataCache) -> FactorResult { $body(date, cache) }
        }
    };
}

fn yoy_field(cache: &DataCache, tid: quant_core::types::TickerId, date: Date, field: &str) -> Option<(f64, f64)> {
    let records = cache.financials.get(&tid)?;
    let recent: Vec<_> = records.iter().filter(|r| r.filing_date <= date).take(5).collect();
    if recent.len() < 5 { return None; }
    let now = recent[0].fields.get(field).copied().filter(|v| v.is_finite())?;
    let yoy = recent[4].fields.get(field).copied().filter(|v| v.is_finite())?;
    Some((now, yoy))
}

simple_factor!(AssetGrowth, "ASSET_GROWTH", "value", -1, 24, |date: Date, cache: &DataCache| {
    let mut r = FactorResult::default();
    for &tid in cache.financials.keys() {
        if let Some((now, yoy)) = yoy_field(cache, tid, date, "total_assets") {
            if yoy.abs() > 1e-6 { let v = (now - yoy) / yoy.abs(); if v.is_finite() { r.insert(tid, v); } }
        }
    }
    r
});

simple_factor!(CompositeIssuance, "COMPOSITE_EQUITY_ISSUANCE", "value", -1, 24, |date: Date, cache: &DataCache| {
    let mut r = FactorResult::default();
    for &tid in cache.financials.keys() {
        if let Some((now, yoy)) = yoy_field(cache, tid, date, "weighted_average_shs_out") {
            if yoy > 0.0 && now > 0.0 { let v = (now / yoy).ln(); if v.is_finite() { r.insert(tid, v); } }
        }
    }
    r
});

simple_factor!(NetOperatingAssets, "NET_OPERATING_ASSETS", "value", -1, 24, |date: Date, cache: &DataCache| {
    let mut r = FactorResult::default();
    for (&tid, records) in &cache.financials {
        let rec = match records.iter().find(|r| r.filing_date <= date) { Some(r) => r, None => continue };
        let f = |s: &str| rec.fields.get(s).copied().filter(|v| v.is_finite()).unwrap_or(0.0);
        let ta = f("total_assets"); if ta < 1e-6 { continue; }
        let cash = f("cash_and_cash_equivalents");
        let debt = f("total_debt");
        let equity = f("total_stockholders_equity");
        let noa = (ta - cash - debt - equity) / ta;
        if noa.is_finite() { r.insert(tid, noa); }
    }
    r
});

simple_factor!(EvToEbit, "EV_TO_EBIT", "value", -1, 30, |date: Date, cache: &DataCache| {
    let mut r = FactorResult::default();
    for (&tid, records) in &cache.financials {
        let rec = match records.iter().find(|r| r.filing_date <= date) { Some(r) => r, None => continue };
        let ebit = match rec.fields.get("ebit").or(rec.fields.get("operating_income")).copied() {
            Some(v) if v.is_finite() && v.abs() > 1e-6 => v, _ => continue
        };
        let ev = match cache.enterprise_values.get(&tid).and_then(|rs| rs.iter().find(|r| r.date <= date)) {
            Some(r) if r.enterprise_value.is_finite() && r.enterprise_value > 0.0 => r.enterprise_value, _ => continue
        };
        let v = ev / ebit; if v.is_finite() { r.insert(tid, v); }
    }
    r
});

simple_factor!(EvToFcf, "EV_TO_FCF", "value", -1, 30, |date: Date, cache: &DataCache| {
    let mut r = FactorResult::default();
    for (&tid, records) in &cache.key_metrics {
        if let Some(rec) = records.iter().find(|r| r.date <= date) {
            if let Some(&v) = rec.fields.get("ev_to_free_cash_flow") {
                if v.is_finite() { r.insert(tid, v); }
            }
        }
    }
    r
});

simple_factor!(EvToSales, "EV_TO_SALES", "value", -1, 30, |date: Date, cache: &DataCache| {
    let mut r = FactorResult::default();
    for (&tid, records) in &cache.key_metrics {
        if let Some(rec) = records.iter().find(|r| r.date <= date) {
            if let Some(&v) = rec.fields.get("ev_to_sales") {
                if v.is_finite() { r.insert(tid, v); }
            }
        }
    }
    r
});

simple_factor!(ShareholderYield, "SHAREHOLDER_YIELD", "value", 1, 24, |date: Date, cache: &DataCache| {
    let mut r = FactorResult::default();
    for (&tid, records) in &cache.financials {
        let recent: Vec<_> = records.iter().filter(|r| r.filing_date <= date).take(4).collect();
        if recent.len() < 3 { continue; }
        let divs: f64 = recent.iter().filter_map(|r| r.fields.get("dividends_paid").copied()).filter(|v| v.is_finite()).map(|v| v.abs()).sum();
        let buybacks: f64 = recent.iter().filter_map(|r| r.fields.get("common_stock_repurchased").copied()).filter(|v| v.is_finite()).map(|v| v.abs()).sum();
        let issuance: f64 = recent.iter().filter_map(|r| r.fields.get("net_stock_issuance").copied()).filter(|v| v.is_finite()).sum();
        let mc = match cache.get_market_cap(tid, date) { Some(m) if m > 0.0 => m, _ => continue };
        let sy = (divs + buybacks - issuance.max(0.0)) / mc;
        if sy.is_finite() { r.insert(tid, sy); }
    }
    r
});

simple_factor!(IntangibleAdjBp, "INTANGIBLE_ADJ_BP", "value", 1, 30, |date: Date, cache: &DataCache| {
    let mut r = FactorResult::default();
    for (&tid, records) in &cache.financials {
        let recent: Vec<_> = records.iter().filter(|r| r.filing_date <= date).take(5).collect();
        if recent.is_empty() { continue; }
        let f = |s: &str| recent[0].fields.get(s).copied().filter(|v| v.is_finite()).unwrap_or(0.0);
        let bv = f("total_stockholders_equity");
        let rd = recent.iter().filter_map(|r| r.fields.get("research_and_development_expenses").copied()).filter(|v| v.is_finite()).sum::<f64>();
        let sga = recent.iter().filter_map(|r| r.fields.get("selling_general_and_administrative_expenses").copied()).filter(|v| v.is_finite()).sum::<f64>();
        let mc = match cache.get_market_cap(tid, date) { Some(m) if m > 0.0 => m, _ => continue };
        let adj = (bv + rd + 0.3 * sga) / mc;
        if adj.is_finite() { r.insert(tid, adj); }
    }
    r
});
