//! Price Momentum factors: MOM_1M, MOM_3M, MOM_12M

use qrs_core::types::{Date, FactorResult};
use qrs_data::cache::DataCache;

use crate::registry::Factor;

/// 1-Month Momentum: adj_close(now) / adj_close(1M ago) - 1
pub struct Mom1M;
inventory::submit! { &Mom1M as &dyn Factor }

impl Factor for Mom1M {
    fn name(&self) -> &'static str { "MOM_1M" }
    fn category(&self) -> &'static str { "momentum" }
    fn inherent_direction(&self) -> i8 { 0 }
    fn ic_window_months(&self) -> u32 { 12 }

    fn compute(&self, date: Date, cache: &DataCache) -> FactorResult {
        let mut result = FactorResult::default();
        for (&(tid, d), stats) in &cache.rolling_stats {
            if d != date || !stats.adj_close.is_finite() || stats.adj_close <= 0.0 {
                continue;
            }
            if let Some(prev) = cache.get_month_end_price(tid, date, 1) {
                if prev > 0.0 {
                    let mom = stats.adj_close / prev - 1.0;
                    if mom.is_finite() {
                        result.insert(tid, mom);
                    }
                }
            }
        }
        result
    }
}

/// 3-Month Momentum
pub struct Mom3M;
inventory::submit! { &Mom3M as &dyn Factor }

impl Factor for Mom3M {
    fn name(&self) -> &'static str { "MOM_3M" }
    fn category(&self) -> &'static str { "momentum" }
    fn inherent_direction(&self) -> i8 { 0 }
    fn ic_window_months(&self) -> u32 { 12 }

    fn compute(&self, date: Date, cache: &DataCache) -> FactorResult {
        let mut result = FactorResult::default();
        for (&(tid, d), stats) in &cache.rolling_stats {
            if d != date || !stats.adj_close.is_finite() || stats.adj_close <= 0.0 {
                continue;
            }
            if let Some(prev) = cache.get_month_end_price(tid, date, 3) {
                if prev > 0.0 {
                    let mom = stats.adj_close / prev - 1.0;
                    if mom.is_finite() {
                        result.insert(tid, mom);
                    }
                }
            }
        }
        result
    }
}

/// 12-1 Month Momentum: price(1M ago) / price(12M ago) - 1 (skip recent month)
pub struct Mom12M;
inventory::submit! { &Mom12M as &dyn Factor }

impl Factor for Mom12M {
    fn name(&self) -> &'static str { "MOM_12M" }
    fn category(&self) -> &'static str { "momentum" }
    fn inherent_direction(&self) -> i8 { 0 }
    fn ic_window_months(&self) -> u32 { 12 }
    fn icir_tier_weight(&self) -> f64 { 2.0 }

    fn compute(&self, date: Date, cache: &DataCache) -> FactorResult {
        let mut result = FactorResult::default();
        // Collect tickers that have rolling stats on this date
        let tickers_on_date: Vec<_> = cache.rolling_stats.keys()
            .filter(|&&(_, d)| d == date)
            .map(|&(tid, _)| tid)
            .collect();

        for tid in tickers_on_date {
            let p1m = cache.get_month_end_price(tid, date, 1);
            let p12m = cache.get_month_end_price(tid, date, 12);
            if let (Some(p1), Some(p12)) = (p1m, p12m) {
                if p12 > 0.0 {
                    let mom = p1 / p12 - 1.0;
                    if mom.is_finite() {
                        result.insert(tid, mom);
                    }
                }
            }
        }
        result
    }
}
