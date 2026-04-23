//! Piotroski F-Score (Piotroski 2000): 9 binary signals, 0-9

use quant_core::types::{Date, FactorResult};
use quant_data::cache::DataCache;

use crate::registry::Factor;

pub struct PiotroskiF;
inventory::submit! { &PiotroskiF as &dyn Factor }

impl Factor for PiotroskiF {
    fn name(&self) -> &'static str { "PIOTROSKI_F" }
    fn category(&self) -> &'static str { "quality" }
    fn inherent_direction(&self) -> i8 { 1 }
    fn ic_window_months(&self) -> u32 { 24 }
    fn icir_tier_weight(&self) -> f64 { 2.0 }

    fn compute(&self, date: Date, cache: &DataCache) -> FactorResult {
        let mut result = FactorResult::default();

        for (&tid, records) in &cache.financials {
            let recent: Vec<_> = records
                .iter()
                .filter(|r| r.filing_date <= date)
                .take(5) // Need current + 4Q ago
                .collect();

            if recent.len() < 5 {
                continue;
            }

            let now = recent[0];
            let yoy = recent[4]; // ~4 quarters ago

            // Helper to get field value
            let f = |rec: &quant_data::cache::FinancialRecord, field: &str| -> Option<f64> {
                rec.fields.get(field).copied().filter(|v| v.is_finite())
            };

            let ta_now = match f(now, "total_assets") {
                Some(v) if v.abs() > 1e-6 => v,
                _ => continue,
            };
            let ta_yoy = match f(yoy, "total_assets") {
                Some(v) if v.abs() > 1e-6 => v,
                _ => continue,
            };

            let roa_now = f(now, "net_income").map(|ni| ni / ta_now);
            let roa_yoy = f(yoy, "net_income").map(|ni| ni / ta_yoy);
            let cfo_now = f(now, "operating_cash_flow");
            let ni_now = f(now, "net_income");

            let ltd_ratio_now = f(now, "long_term_debt").map(|v| v / ta_now);
            let ltd_ratio_yoy = f(yoy, "long_term_debt").map(|v| v / ta_yoy);

            let cr_now = match (f(now, "total_current_assets"), f(now, "total_current_liabilities")) {
                (Some(a), Some(l)) if l.abs() > 1e-6 => Some(a / l),
                _ => None,
            };
            let cr_yoy = match (f(yoy, "total_current_assets"), f(yoy, "total_current_liabilities")) {
                (Some(a), Some(l)) if l.abs() > 1e-6 => Some(a / l),
                _ => None,
            };

            let gm_now = match (f(now, "gross_profit"), f(now, "revenue")) {
                (Some(gp), Some(rev)) if rev.abs() > 1e-6 => Some(gp / rev),
                _ => None,
            };
            let gm_yoy = match (f(yoy, "gross_profit"), f(yoy, "revenue")) {
                (Some(gp), Some(rev)) if rev.abs() > 1e-6 => Some(gp / rev),
                _ => None,
            };

            let at_now = f(now, "revenue").map(|r| r / ta_now);
            let at_yoy = f(yoy, "revenue").map(|r| r / ta_yoy);

            let nsi = f(now, "net_stock_issuance");

            // Score 9 signals
            let mut score = 0i32;
            let mut valid = 0i32;

            // 1. ROA > 0
            if let Some(r) = roa_now { score += (r > 0.0) as i32; valid += 1; }
            // 2. CFO > 0
            if let Some(c) = cfo_now { score += (c > 0.0) as i32; valid += 1; }
            // 3. ΔROA > 0
            if let (Some(n), Some(y)) = (roa_now, roa_yoy) { score += (n > y) as i32; valid += 1; }
            // 4. CFO > NI
            if let (Some(c), Some(n)) = (cfo_now, ni_now) { score += (c > n) as i32; valid += 1; }
            // 5. ΔLTD Ratio ≤ 0
            if let (Some(n), Some(y)) = (ltd_ratio_now, ltd_ratio_yoy) { score += (n <= y) as i32; valid += 1; }
            // 6. ΔCurrent Ratio > 0
            if let (Some(n), Some(y)) = (cr_now, cr_yoy) { score += (n > y) as i32; valid += 1; }
            // 7. No equity issuance
            if let Some(n) = nsi { score += (n <= 0.0) as i32; valid += 1; }
            // 8. ΔGross Margin > 0
            if let (Some(n), Some(y)) = (gm_now, gm_yoy) { score += (n > y) as i32; valid += 1; }
            // 9. ΔAsset Turnover > 0
            if let (Some(n), Some(y)) = (at_now, at_yoy) { score += (n > y) as i32; valid += 1; }

            if valid >= 5 {
                result.insert(tid, score as f64);
            }
        }
        result
    }
}
