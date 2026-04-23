//! Factor trait and inventory-based registry.

use quant_core::types::{Date, FactorResult};
use quant_data::cache::DataCache;

/// Core factor computation trait.
/// Every factor implements this. The compute method is called once per
/// rebalance date for the entire cross-section.
pub trait Factor: Send + Sync {
    /// Unique factor name (e.g., "EP", "PIOTROSKI_F").
    fn name(&self) -> &'static str;

    /// Factor category (e.g., "value", "quality").
    fn category(&self) -> &'static str;

    /// Inherent direction: +1 (high=good), -1 (high=bad), 0 (IC-determined).
    fn inherent_direction(&self) -> i8;

    /// Rolling IC window in months.
    fn ic_window_months(&self) -> u32;

    /// ICIR tier weight (T1=2.0, T2=1.0, T3=0.5, flip=0.3).
    fn icir_tier_weight(&self) -> f64 {
        1.0
    }

    /// Compute factor values for all tickers on given date.
    /// Returns HashMap<TickerId, f64> — tickers with no valid value are absent.
    fn compute(&self, date: Date, cache: &DataCache) -> FactorResult;
}

// inventory-based automatic factor registration.
// Each factor file uses: inventory::submit! { &MyFactor as &dyn Factor }
inventory::collect!(&'static dyn Factor);

/// Get all registered factors.
pub fn all_factors() -> Vec<&'static dyn Factor> {
    inventory::iter::<&'static dyn Factor>().copied().collect()
}

/// Get factors filtered by category.
pub fn factors_by_category(category: &str) -> Vec<&'static dyn Factor> {
    all_factors()
        .into_iter()
        .filter(|f| f.category() == category)
        .collect()
}
