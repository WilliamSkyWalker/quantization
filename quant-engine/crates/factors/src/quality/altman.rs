//! Altman Z-Score (Altman 1968): bankruptcy prediction
//! Z = 1.2*X1 + 1.4*X2 + 3.3*X3 + 0.6*X4 + 1.0*X5

use quant_core::types::{Date, FactorResult};
use quant_data::cache::DataCache;

use crate::registry::Factor;

pub struct AltmanZ;
inventory::submit! { &AltmanZ as &dyn Factor }

impl Factor for AltmanZ {
    fn name(&self) -> &'static str { "ALTMAN_Z" }
    fn category(&self) -> &'static str { "quality" }
    fn inherent_direction(&self) -> i8 { 1 }
    fn ic_window_months(&self) -> u32 { 24 }

    fn compute(&self, date: Date, cache: &DataCache) -> FactorResult {
        let mut result = FactorResult::default();

        for (&tid, records) in &cache.financials {
            let latest = match records.iter().find(|r| r.filing_date <= date) {
                Some(r) => r,
                None => continue,
            };

            let f = |field: &str| -> Option<f64> {
                latest.fields.get(field).copied().filter(|v| v.is_finite())
            };

            let ta = match f("total_assets") {
                Some(v) if v.abs() > 1e-6 => v,
                _ => continue,
            };

            let tca = f("total_current_assets").unwrap_or(0.0);
            let tcl = f("total_current_liabilities").unwrap_or(0.0);
            let re = f("retained_earnings").unwrap_or(0.0);
            let ebit = f("ebit").or_else(|| f("operating_income")).unwrap_or(0.0);
            let revenue = f("revenue").unwrap_or(0.0);
            let tl = f("total_liabilities").unwrap_or(0.0);

            let mktcap = match cache.get_market_cap(tid, date) {
                Some(m) if m > 0.0 => m,
                _ => continue,
            };

            let x1 = (tca - tcl) / ta;           // Working Capital / TA
            let x2 = re / ta;                      // Retained Earnings / TA
            let x3 = ebit / ta;                    // EBIT / TA
            let x4 = if tl.abs() > 1e-6 { mktcap / tl } else { 0.0 }; // MktCap / TL
            let x5 = revenue / ta;                 // Revenue / TA

            let z = 1.2 * x1 + 1.4 * x2 + 3.3 * x3 + 0.6 * x4 + 1.0 * x5;
            if z.is_finite() {
                result.insert(tid, z);
            }
        }
        result
    }
}
