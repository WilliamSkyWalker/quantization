//! Growth factors: NET_PROFIT_YOY, REVENUE_YOY, NET_PROFIT_CAGR_3Y

use quant_core::types::{Date, FactorResult};
use quant_data::cache::DataCache;

use crate::registry::Factor;

/// Helper: compute YoY growth for a financial field.
/// Takes 8 most recent quarters (4 current TTM + 4 prior TTM).
fn yoy_growth(field: &str, date: Date, cache: &DataCache) -> FactorResult {
    let mut result = FactorResult::default();

    for (&ticker_id, records) in &cache.financials {
        let qualifying: Vec<f64> = records
            .iter()
            .filter(|r| r.filing_date <= date)
            .take(8)
            .filter_map(|r| r.fields.get(field).copied())
            .filter(|v| v.is_finite())
            .collect();

        if qualifying.len() < 8 {
            continue;
        }

        let current_ttm: f64 = qualifying[..4].iter().sum();
        let prior_ttm: f64 = qualifying[4..8].iter().sum();

        if prior_ttm.abs() < 1e-10 || !current_ttm.is_finite() || !prior_ttm.is_finite() {
            continue;
        }

        let growth = current_ttm / prior_ttm.abs() - 1.0;
        if growth.is_finite() {
            result.insert(ticker_id, growth);
        }
    }

    result
}

/// Net Profit YoY: TTM net_income now / TTM net_income 1Y ago - 1
pub struct NetProfitYoY;
inventory::submit! { &NetProfitYoY as &dyn Factor }

impl Factor for NetProfitYoY {
    fn name(&self) -> &'static str { "NET_PROFIT_YOY" }
    fn category(&self) -> &'static str { "growth" }
    fn inherent_direction(&self) -> i8 { 0 }
    fn ic_window_months(&self) -> u32 { 24 }

    fn compute(&self, date: Date, cache: &DataCache) -> FactorResult {
        yoy_growth("net_income", date, cache)
    }
}

/// Revenue YoY: TTM revenue now / TTM revenue 1Y ago - 1
pub struct RevenueYoY;
inventory::submit! { &RevenueYoY as &dyn Factor }

impl Factor for RevenueYoY {
    fn name(&self) -> &'static str { "REVENUE_YOY" }
    fn category(&self) -> &'static str { "growth" }
    fn inherent_direction(&self) -> i8 { 0 }
    fn ic_window_months(&self) -> u32 { 24 }

    fn compute(&self, date: Date, cache: &DataCache) -> FactorResult {
        yoy_growth("revenue", date, cache)
    }
}

/// Net Profit CAGR 3Y: (TTM now / TTM 3Y ago)^(1/3) - 1
pub struct NetProfitCagr3Y;
inventory::submit! { &NetProfitCagr3Y as &dyn Factor }

impl Factor for NetProfitCagr3Y {
    fn name(&self) -> &'static str { "NET_PROFIT_CAGR_3Y" }
    fn category(&self) -> &'static str { "growth" }
    fn inherent_direction(&self) -> i8 { 0 }
    fn ic_window_months(&self) -> u32 { 36 }

    fn compute(&self, date: Date, cache: &DataCache) -> FactorResult {
        let mut result = FactorResult::default();

        for (&ticker_id, records) in &cache.financials {
            let qualifying: Vec<f64> = records
                .iter()
                .filter(|r| r.filing_date <= date)
                .take(16) // 4 current + 12 gap = 3Y prior TTM at Q12-Q15
                .filter_map(|r| r.fields.get("net_income").copied())
                .filter(|v| v.is_finite())
                .collect();

            if qualifying.len() < 16 {
                continue;
            }

            let current_ttm: f64 = qualifying[..4].iter().sum();
            let prior_ttm: f64 = qualifying[12..16].iter().sum();

            if current_ttm <= 0.0 || prior_ttm <= 0.0 {
                continue;
            }

            let cagr = (current_ttm / prior_ttm).powf(1.0 / 3.0) - 1.0;
            if cagr.is_finite() {
                result.insert(ticker_id, cagr);
            }
        }

        result
    }
}
