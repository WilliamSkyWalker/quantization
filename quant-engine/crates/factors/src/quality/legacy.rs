//! Quality legacy factors: ROE_TTM, GROSS_MARGIN, PROFIT_STB, ACCRUALS

use quant_core::types::{Date, FactorResult};
use quant_data::cache::DataCache;

use crate::registry::Factor;

/// ROE TTM: TTM net_income / latest total_stockholders_equity
pub struct RoeTtm;
inventory::submit! { &RoeTtm as &dyn Factor }

impl Factor for RoeTtm {
    fn name(&self) -> &'static str { "ROE_TTM" }
    fn category(&self) -> &'static str { "quality" }
    fn inherent_direction(&self) -> i8 { 1 }
    fn ic_window_months(&self) -> u32 { 24 }

    fn compute(&self, date: Date, cache: &DataCache) -> FactorResult {
        let mut result = FactorResult::default();

        for (&ticker_id, records) in &cache.financials {
            let recent: Vec<&_> = records
                .iter()
                .filter(|r| r.filing_date <= date)
                .take(4)
                .collect();

            if recent.len() < 3 {
                continue;
            }

            // TTM net income
            let ttm_ni: f64 = recent
                .iter()
                .filter_map(|r| r.fields.get("net_income").copied())
                .filter(|v| v.is_finite())
                .sum();

            // Latest equity
            let equity = recent[0]
                .fields
                .get("total_stockholders_equity")
                .or_else(|| recent[0].fields.get("total_equity"))
                .copied()
                .unwrap_or(f64::NAN);

            if equity.is_finite() && equity.abs() > 1e-6 && ttm_ni.is_finite() {
                let roe = ttm_ni / equity;
                if roe.is_finite() {
                    result.insert(ticker_id, roe);
                }
            }
        }

        result
    }
}

/// Gross Margin: latest gross_profit / revenue
pub struct GrossMargin;
inventory::submit! { &GrossMargin as &dyn Factor }

impl Factor for GrossMargin {
    fn name(&self) -> &'static str { "GROSS_MARGIN" }
    fn category(&self) -> &'static str { "quality" }
    fn inherent_direction(&self) -> i8 { 1 }
    fn ic_window_months(&self) -> u32 { 24 }

    fn compute(&self, date: Date, cache: &DataCache) -> FactorResult {
        let mut result = FactorResult::default();

        for (&ticker_id, records) in &cache.financials {
            let latest = match records.iter().find(|r| r.filing_date <= date) {
                Some(r) => r,
                None => continue,
            };

            let gp = latest.fields.get("gross_profit").copied().unwrap_or(f64::NAN);
            let rev = latest.fields.get("revenue").copied().unwrap_or(f64::NAN);

            if gp.is_finite() && rev.is_finite() && rev.abs() > 1e-6 {
                let margin = gp / rev;
                if margin.is_finite() {
                    result.insert(ticker_id, margin);
                }
            }
        }

        result
    }
}

/// Profit Stability: 1 / (std of last 8 quarters' operating_margin)
/// Higher = more stable = better quality
pub struct ProfitStb;
inventory::submit! { &ProfitStb as &dyn Factor }

impl Factor for ProfitStb {
    fn name(&self) -> &'static str { "PROFIT_STB" }
    fn category(&self) -> &'static str { "quality" }
    fn inherent_direction(&self) -> i8 { 1 }
    fn ic_window_months(&self) -> u32 { 36 }

    fn compute(&self, date: Date, cache: &DataCache) -> FactorResult {
        let mut result = FactorResult::default();

        for (&ticker_id, records) in &cache.financials {
            let margins: Vec<f64> = records
                .iter()
                .filter(|r| r.filing_date <= date)
                .take(8)
                .filter_map(|r| {
                    let oi = r.fields.get("operating_income")?.to_owned();
                    let rev = r.fields.get("revenue")?.to_owned();
                    if rev.abs() > 1e-6 && oi.is_finite() && rev.is_finite() {
                        Some(oi / rev)
                    } else {
                        None
                    }
                })
                .collect();

            if margins.len() < 4 {
                continue;
            }

            let n = margins.len() as f64;
            let mean = margins.iter().sum::<f64>() / n;
            let var = margins.iter().map(|m| (m - mean).powi(2)).sum::<f64>() / (n - 1.0);
            let std = var.sqrt();

            if std > 1e-10 && std.is_finite() {
                result.insert(ticker_id, 1.0 / std); // Inverse: low volatility = stable
            }
        }

        result
    }
}

/// Accruals: (net_income - operating_cash_flow) / total_assets
/// Lower = better quality (cash earnings > accounting earnings)
pub struct Accruals;
inventory::submit! { &Accruals as &dyn Factor }

impl Factor for Accruals {
    fn name(&self) -> &'static str { "ACCRUALS" }
    fn category(&self) -> &'static str { "quality" }
    fn inherent_direction(&self) -> i8 { -1 } // Lower accruals = better
    fn ic_window_months(&self) -> u32 { 24 }

    fn compute(&self, date: Date, cache: &DataCache) -> FactorResult {
        let mut result = FactorResult::default();

        for (&ticker_id, records) in &cache.financials {
            let latest = match records.iter().find(|r| r.filing_date <= date) {
                Some(r) => r,
                None => continue,
            };

            let ni = latest.fields.get("net_income").copied().unwrap_or(f64::NAN);
            let ocf = latest.fields.get("operating_cash_flow").copied().unwrap_or(f64::NAN);
            let ta = latest.fields.get("total_assets").copied().unwrap_or(f64::NAN);

            if ni.is_finite() && ocf.is_finite() && ta.is_finite() && ta.abs() > 1e-6 {
                let accrual = (ni - ocf) / ta;
                if accrual.is_finite() {
                    result.insert(ticker_id, accrual);
                }
            }
        }

        result
    }
}
