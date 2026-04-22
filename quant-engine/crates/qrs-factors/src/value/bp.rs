//! BP (Book-to-Price): total_equity / market_cap

use qrs_core::types::{Date, FactorResult};
use qrs_data::cache::DataCache;

use crate::registry::Factor;

pub struct Bp;

inventory::submit! { &Bp as &dyn Factor }

impl Factor for Bp {
    fn name(&self) -> &'static str { "BP" }
    fn category(&self) -> &'static str { "value" }
    fn inherent_direction(&self) -> i8 { 0 }
    fn ic_window_months(&self) -> u32 { 30 }

    fn compute(&self, date: Date, cache: &DataCache) -> FactorResult {
        let mut result = FactorResult::default();

        for (&ticker_id, records) in &cache.financials {
            // Latest financial record where filing_date <= date
            let latest = match records.iter().find(|r| r.filing_date <= date) {
                Some(r) => r,
                None => continue,
            };

            let equity = match latest.fields.get("total_stockholders_equity")
                .or_else(|| latest.fields.get("total_equity"))
            {
                Some(&v) if v.is_finite() => v,
                _ => continue,
            };

            let mktcap = match cache.get_market_cap(ticker_id, date) {
                Some(m) if m > 0.0 && m.is_finite() => m,
                _ => continue,
            };

            let bp = equity / mktcap;
            if bp.is_finite() {
                result.insert(ticker_id, bp);
            }
        }

        result
    }
}
