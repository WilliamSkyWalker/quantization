//! Beneish M-Score (Beneish 1999): earnings manipulation detection
//! 8 ratios comparing current vs prior year, higher M = more likely manipulation

use qrs_core::types::{Date, FactorResult};
use qrs_data::cache::DataCache;

use crate::registry::Factor;

pub struct BeneishM;
inventory::submit! { &BeneishM as &dyn Factor }

impl Factor for BeneishM {
    fn name(&self) -> &'static str { "BENEISH_M" }
    fn category(&self) -> &'static str { "quality" }
    fn inherent_direction(&self) -> i8 { -1 } // Lower = better (less manipulation)
    fn ic_window_months(&self) -> u32 { 24 }

    fn compute(&self, date: Date, cache: &DataCache) -> FactorResult {
        let mut result = FactorResult::default();

        for (&tid, records) in &cache.financials {
            let recent: Vec<_> = records
                .iter()
                .filter(|r| r.filing_date <= date)
                .take(5)
                .collect();

            if recent.len() < 5 {
                continue;
            }

            let now = recent[0];
            let yoy = recent[4];

            let f = |rec: &qrs_data::cache::FinancialRecord, field: &str| -> Option<f64> {
                rec.fields.get(field).copied().filter(|v| v.is_finite())
            };

            // Get fields for both periods
            let rev_now = match f(now, "revenue") { Some(v) if v.abs() > 1e-6 => v, _ => continue };
            let rev_yoy = match f(yoy, "revenue") { Some(v) if v.abs() > 1e-6 => v, _ => continue };
            let ta_now = match f(now, "total_assets") { Some(v) if v.abs() > 1e-6 => v, _ => continue };
            let ta_yoy = match f(yoy, "total_assets") { Some(v) if v.abs() > 1e-6 => v, _ => continue };

            let ar_now = f(now, "accounts_receivables").unwrap_or(0.0);
            let ar_yoy = f(yoy, "accounts_receivables").unwrap_or(0.0);
            let gp_now = f(now, "gross_profit").unwrap_or(0.0);
            let gp_yoy = f(yoy, "gross_profit").unwrap_or(0.0);
            let ppe_now = f(now, "property_plant_equipment_net").unwrap_or(0.0);
            let da_now = f(now, "depreciation_and_amortization").unwrap_or(0.0);
            let da_yoy = f(yoy, "depreciation_and_amortization").unwrap_or(0.0);
            let sga_now = f(now, "selling_general_and_administrative_expenses").unwrap_or(0.0);
            let sga_yoy = f(yoy, "selling_general_and_administrative_expenses").unwrap_or(0.0);
            let ni_now = f(now, "net_income").unwrap_or(0.0);
            let ocf_now = f(now, "operating_cash_flow").unwrap_or(0.0);
            let tl_now = f(now, "total_liabilities").unwrap_or(0.0);
            let tca_now = f(now, "total_current_assets").unwrap_or(0.0);
            let _tcl_now = f(now, "total_current_liabilities").unwrap_or(0.0);

            // DSRI: Days Sales in Receivables Index
            let dsri = if ar_yoy.abs() > 1e-6 && rev_yoy.abs() > 1e-6 {
                (ar_now / rev_now) / (ar_yoy / rev_yoy)
            } else { 1.0 };

            // GMI: Gross Margin Index
            let gm_now = gp_now / rev_now;
            let gm_yoy = gp_yoy / rev_yoy;
            let gmi = if gm_now.abs() > 1e-10 { gm_yoy / gm_now } else { 1.0 };

            // AQI: Asset Quality Index
            let aq_now = 1.0 - (tca_now + ppe_now) / ta_now;
            let aq_yoy = 1.0 - (f(yoy, "total_current_assets").unwrap_or(0.0) + f(yoy, "property_plant_equipment_net").unwrap_or(0.0)) / ta_yoy;
            let aqi = if aq_yoy.abs() > 1e-10 { aq_now / aq_yoy } else { 1.0 };

            // SGI: Sales Growth Index
            let sgi = rev_now / rev_yoy;

            // DEPI: Depreciation Index
            let depi_now = if (da_now + ppe_now).abs() > 1e-6 { da_now / (da_now + ppe_now) } else { 0.0 };
            let depi_yoy = if (da_yoy + f(yoy, "property_plant_equipment_net").unwrap_or(0.0)).abs() > 1e-6 {
                da_yoy / (da_yoy + f(yoy, "property_plant_equipment_net").unwrap_or(0.0))
            } else { 0.0 };
            let depi = if depi_now.abs() > 1e-10 { depi_yoy / depi_now } else { 1.0 };

            // SGAI: SGA Index
            let sgai = if (sga_yoy / rev_yoy).abs() > 1e-10 {
                (sga_now / rev_now) / (sga_yoy / rev_yoy)
            } else { 1.0 };

            // LVGI: Leverage Index
            let lev_now = tl_now / ta_now;
            let lev_yoy = f(yoy, "total_liabilities").unwrap_or(0.0) / ta_yoy;
            let lvgi = if lev_yoy.abs() > 1e-10 { lev_now / lev_yoy } else { 1.0 };

            // TATA: Total Accruals to Total Assets
            let tata = (ni_now - ocf_now) / ta_now;

            // M-Score = -4.84 + 0.92*DSRI + 0.528*GMI + 0.404*AQI + 0.892*SGI
            //           + 0.115*DEPI - 0.172*SGAI + 4.679*TATA - 0.327*LVGI
            let m = -4.84 + 0.92 * dsri + 0.528 * gmi + 0.404 * aqi + 0.892 * sgi
                + 0.115 * depi - 0.172 * sgai + 4.679 * tata - 0.327 * lvgi;

            if m.is_finite() {
                result.insert(tid, m);
            }
        }
        result
    }
}
