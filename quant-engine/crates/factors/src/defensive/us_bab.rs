//! Betting Against Beta (BAB) factor — Frazzini & Pedersen 2014
//!
//! BAB_SIGNAL = -β_market (low beta stocks get high signal)
//!
//! β estimated from 252-day rolling OLS of excess returns on Mkt-RF,
//! then Vasicek-shrunk toward 1.0: β_shrunk = 0.6·β_OLS + 0.4·1.0

use quant_core::types::{Date, FactorResult};
use quant_data::cache::DataCache;
use crate::registry::Factor;

const LOOKBACK_DAYS: usize = 252;
const MIN_OBS: usize = 120;
const SHRINKAGE_WEIGHT: f64 = 0.6; // β_shrunk = w·β_OLS + (1-w)·1.0

pub struct BabBeta;
inventory::submit! { &BabBeta as &dyn Factor }

impl Factor for BabBeta {
    fn name(&self) -> &'static str { "BAB_BETA" }
    fn category(&self) -> &'static str { "defensive" }
    fn inherent_direction(&self) -> i8 { 1 } // high signal (low beta) = bullish
    fn ic_window_months(&self) -> u32 { 18 }

    fn compute(&self, date: Date, cache: &DataCache) -> FactorResult {
        let mut result = FactorResult::default();

        // Get benchmark (Mkt-RF proxy): index daily returns
        let cal = &cache.trading_days;
        let end_pos = match cal.binary_search(&date) {
            Ok(i) => i,
            Err(i) if i > 0 => i - 1,
            _ => return result,
        };
        let need = LOOKBACK_DAYS + 5;
        if end_pos + 1 < need {
            return result;
        }
        let start_pos = end_pos + 1 - need;
        let dates: Vec<Date> = cal[start_pos..=end_pos].to_vec();

        // Collect market returns (from index prices)
        let index_key = "^GSPC";
        let mut mkt_returns: Vec<f64> = Vec::with_capacity(dates.len() - 1);
        for i in 1..dates.len() {
            let p0 = cache.index_prices.get(&(index_key.to_string(), dates[i - 1])).copied();
            let p1 = cache.index_prices.get(&(index_key.to_string(), dates[i])).copied();
            match (p0, p1) {
                (Some(prev), Some(curr)) if prev > 0.0 && curr > 0.0 => {
                    mkt_returns.push(curr / prev - 1.0);
                }
                _ => mkt_returns.push(f64::NAN),
            }
        }

        // Market stats
        let valid_mkt: Vec<f64> = mkt_returns.iter().copied().filter(|v| v.is_finite()).collect();
        if valid_mkt.len() < MIN_OBS {
            return result;
        }
        let mkt_mean = valid_mkt.iter().sum::<f64>() / valid_mkt.len() as f64;
        let mkt_var: f64 = valid_mkt.iter().map(|r| (r - mkt_mean).powi(2)).sum();
        if mkt_var < 1e-15 {
            return result;
        }

        // For each ticker: compute beta via Cov(r_stock, r_mkt) / Var(r_mkt)
        let return_dates = &dates[1..]; // dates aligned with mkt_returns
        let active_tickers = cache.active_tickers();

        for tid in active_tickers {
            // Collect stock returns aligned with market
            let mut cov_sum = 0.0;
            let mut count = 0usize;

            for (i, &d) in return_dates.iter().enumerate() {
                let mkt_r = mkt_returns[i];
                if !mkt_r.is_finite() {
                    continue;
                }

                // Get stock return for this day
                let prev_date = dates[i]; // dates[i] corresponds to return_dates[i-1+1] = dates[i+1-1]... actually dates[i] is the prior day
                if let (Some(bar0), Some(bar1)) = (
                    cache.daily_prices.get(tid, prev_date),
                    cache.daily_prices.get(tid, d),
                ) {
                    if bar0.adj_close > 0.0 && bar1.adj_close > 0.0 {
                        let stock_r = bar1.adj_close / bar0.adj_close - 1.0;
                        if stock_r.is_finite() {
                            // Using market return as proxy for excess (no RF available in cache)
                            cov_sum += (stock_r - 0.0) * (mkt_r - mkt_mean);
                            count += 1;
                        }
                    }
                }
            }

            if count < MIN_OBS {
                continue;
            }

            let beta_ols = cov_sum / mkt_var;
            let beta_shrunk = SHRINKAGE_WEIGHT * beta_ols + (1.0 - SHRINKAGE_WEIGHT) * 1.0;

            // BAB signal = -beta (low beta → high signal)
            let bab = -beta_shrunk;
            if bab.is_finite() {
                result.insert(tid, bab);
            }
        }

        result
    }
}
