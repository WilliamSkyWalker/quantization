//! Universe filtering: clean stock universe for a given date.
//! Equivalent to Python's get_us_clean_universe().

use qrs_core::types::{Date, TickerId};
use rustc_hash::FxHashSet;
use tracing::debug;

use crate::cache::DataCache;

/// Universe filter configuration.
pub struct UniverseFilter {
    pub min_market_cap: f64,    // $500M default
    pub min_daily_volume: f64,  // $1M daily dollar volume default
    pub min_volume_days: usize, // Number of recent days to average volume over
}

impl Default for UniverseFilter {
    fn default() -> Self {
        Self {
            min_market_cap: 5e8,
            min_daily_volume: 1e6,
            min_volume_days: 20,
        }
    }
}

/// Get clean universe of tickers for a given date.
/// Filters: has price on date, min market cap, min dollar volume.
pub fn get_clean_universe(
    date: Date,
    cache: &DataCache,
    filter: &UniverseFilter,
) -> FxHashSet<TickerId> {
    let mut universe = FxHashSet::default();

    // Collect tickers that traded on this date (have a close price)
    let mut candidates: Vec<(TickerId, f64, f64)> = Vec::new(); // (ticker, close, volume)

    for (&(tid, d), bar) in &cache.daily_prices {
        if d != date {
            continue;
        }
        if !bar.close.is_finite() || bar.close <= 0.0 {
            continue;
        }
        if !bar.volume.is_finite() || bar.volume <= 0.0 {
            continue;
        }
        candidates.push((tid, bar.close, bar.volume));
    }

    for (tid, close, volume) in candidates {
        // Market cap filter (from enterprise_values)
        let mktcap = match cache.get_market_cap(tid, date) {
            Some(m) if m >= filter.min_market_cap => m,
            _ => continue,
        };

        // Dollar volume filter: use dvol_20d from rolling stats if available,
        // otherwise use today's close * volume as approximation
        let dvol = cache.daily_prices.get(&(tid, date))
            .and_then(|bar| {
                if bar.dvol_20d.is_finite() && bar.dvol_20d > 0.0 {
                    Some(bar.dvol_20d)
                } else {
                    Some(close * volume)
                }
            })
            .unwrap_or(0.0);

        if dvol < filter.min_daily_volume {
            continue;
        }

        universe.insert(tid);
    }

    debug!("Universe on {}: {} tickers (from daily prices)", date, universe.len());
    universe
}
