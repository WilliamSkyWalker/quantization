//! TSMOM (Time Series Momentum): sign(12M cumulative return) × |12M return|
//! Moskowitz-Ooi-Pedersen 2012

use qrs_core::types::{Date, FactorResult};
use qrs_data::cache::DataCache;

use crate::registry::Factor;

pub struct Tsmom;
inventory::submit! { &Tsmom as &dyn Factor }

impl Factor for Tsmom {
    fn name(&self) -> &'static str { "TSMOM" }
    fn category(&self) -> &'static str { "momentum" }
    fn inherent_direction(&self) -> i8 { 0 }
    fn ic_window_months(&self) -> u32 { 12 }
    fn icir_tier_weight(&self) -> f64 { 2.0 }

    fn compute(&self, date: Date, cache: &DataCache) -> FactorResult {
        let mut result = FactorResult::default();

        // Get tickers on this date from rolling stats
        let tickers_on_date: Vec<_> = cache.daily_prices.keys()
            .filter(|&&(_, d)| d == date)
            .map(|&(tid, _)| tid)
            .collect();

        for tid in tickers_on_date {
            // 12M cumulative return (skip recent 1M)
            let p1m = cache.get_month_end_price(tid, date, 1);
            let p12m = cache.get_month_end_price(tid, date, 12);

            if let (Some(p1), Some(p12)) = (p1m, p12m) {
                if p12 > 0.0 {
                    let ret_12m = p1 / p12 - 1.0;
                    if ret_12m.is_finite() {
                        // TSMOM = sign(ret) × |ret| = ret itself
                        // But the signal strength is the magnitude
                        result.insert(tid, ret_12m);
                    }
                }
            }
        }
        result
    }
}

/// Industry Momentum: stock 12M return minus industry median 12M return
/// Moskowitz-Grinblatt 1999
pub struct IndustryMom;
inventory::submit! { &IndustryMom as &dyn Factor }

impl Factor for IndustryMom {
    fn name(&self) -> &'static str { "INDUSTRY_MOM" }
    fn category(&self) -> &'static str { "momentum" }
    fn inherent_direction(&self) -> i8 { 0 }
    fn ic_window_months(&self) -> u32 { 12 }
    fn icir_tier_weight(&self) -> f64 { 2.0 }

    fn compute(&self, date: Date, cache: &DataCache) -> FactorResult {
        // First compute 12M returns for all tickers
        let mut returns: rustc_hash::FxHashMap<qrs_core::types::TickerId, f64> = Default::default();
        let tickers_on_date: Vec<_> = cache.daily_prices.keys()
            .filter(|&&(_, d)| d == date)
            .map(|&(tid, _)| tid)
            .collect();

        for tid in &tickers_on_date {
            let p1m = cache.get_month_end_price(*tid, date, 1);
            let p12m = cache.get_month_end_price(*tid, date, 12);
            if let (Some(p1), Some(p12)) = (p1m, p12m) {
                if p12 > 0.0 {
                    let ret = p1 / p12 - 1.0;
                    if ret.is_finite() {
                        returns.insert(*tid, ret);
                    }
                }
            }
        }

        if returns.is_empty() {
            return FactorResult::default();
        }

        // Group by sector, compute median
        let mut sector_returns: rustc_hash::FxHashMap<qrs_core::types::SectorId, Vec<f64>> = Default::default();
        for (&tid, &ret) in &returns {
            if let Some(&sid) = cache.sector_map.get(&tid) {
                sector_returns.entry(sid).or_default().push(ret);
            }
        }

        let mut sector_medians: rustc_hash::FxHashMap<qrs_core::types::SectorId, f64> = Default::default();
        for (sid, mut rets) in sector_returns {
            rets.sort_by(|a, b| a.partial_cmp(b).unwrap_or(std::cmp::Ordering::Equal));
            let n = rets.len();
            let median = if n % 2 == 0 {
                (rets[n/2 - 1] + rets[n/2]) / 2.0
            } else {
                rets[n/2]
            };
            sector_medians.insert(sid, median);
        }

        // Factor = stock return - sector median
        let mut result = FactorResult::default();
        for (&tid, &ret) in &returns {
            let sector_med = cache.sector_map.get(&tid)
                .and_then(|sid| sector_medians.get(sid))
                .copied()
                .unwrap_or(0.0);
            let adj_ret = ret - sector_med;
            if adj_ret.is_finite() {
                result.insert(tid, adj_ret);
            }
        }
        result
    }
}
