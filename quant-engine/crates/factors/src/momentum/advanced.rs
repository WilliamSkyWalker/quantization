//! Advanced momentum: PRICE_52W_HIGH, FROG_IN_PAN, SUE_PEAD, RESIDUAL_MOM_FF3

use quant_core::types::{Date, FactorResult, TickerId};
use quant_data::cache::DataCache;
use rustc_hash::FxHashMap;
use crate::registry::Factor;

// === PRICE_52W_HIGH ===
// current_price / max(past 252 trading days' prices)

pub struct Price52wHigh;
inventory::submit! { &Price52wHigh as &dyn Factor }

impl Factor for Price52wHigh {
    fn name(&self) -> &'static str { "PRICE_52W_HIGH" }
    fn category(&self) -> &'static str { "momentum" }
    fn inherent_direction(&self) -> i8 { 1 }
    fn ic_window_months(&self) -> u32 { 12 }

    fn compute(&self, date: Date, cache: &DataCache) -> FactorResult {
        let start = date - chrono::Duration::days(380);
        let mut result = FactorResult::default();

        // Collect max price per ticker over lookback
        let mut ticker_highs: FxHashMap<TickerId, (f64, f64)> = FxHashMap::default(); // (max_price, current_price)

        for (tid, d, bar) in cache.daily_prices.iter_date_range(start, date) {
            let price = bar.adj_close;
            if !price.is_finite() || price <= 0.0 { continue; }

            let entry = ticker_highs.entry(tid).or_insert((f64::NEG_INFINITY, 0.0));
            if price > entry.0 { entry.0 = price; }
            if d == date { entry.1 = price; }
        }

        for (tid, (high, current)) in ticker_highs {
            if current > 0.0 && high > 0.0 {
                let ratio = current / high;
                if ratio.is_finite() { result.insert(tid, ratio); }
            }
        }
        result
    }
}

// === FROG_IN_PAN ===
// sign(R_12M) × (pct_negative_days - pct_positive_days) × |R_12M|
// Captures "continuous small gains" vs "one-time spike"

pub struct FrogInPan;
inventory::submit! { &FrogInPan as &dyn Factor }

impl Factor for FrogInPan {
    fn name(&self) -> &'static str { "FROG_IN_PAN" }
    fn category(&self) -> &'static str { "momentum" }
    fn inherent_direction(&self) -> i8 { 1 }
    fn ic_window_months(&self) -> u32 { 12 }

    fn compute(&self, date: Date, cache: &DataCache) -> FactorResult {
        let start = date - chrono::Duration::days(380);
        let mut result = FactorResult::default();

        // Collect daily returns and 12M return per ticker
        let mut ticker_data: FxHashMap<TickerId, (Vec<f64>, f64, f64)> = FxHashMap::default();
        // (daily_returns, first_price, last_price)

        for (tid, d, bar) in cache.daily_prices.iter_date_range(start, date) {
            let ret = bar.change_percent / 100.0;
            if !ret.is_finite() { continue; }
            let price = bar.adj_close;
            if !price.is_finite() || price <= 0.0 { continue; }

            let entry = ticker_data.entry(tid).or_insert((Vec::new(), price, price));
            entry.0.push(ret);
            if d <= start + chrono::Duration::days(5) { entry.1 = price; } // ~first price
            if d == date { entry.2 = price; }
        }

        for (tid, (rets, first_price, last_price)) in ticker_data {
            if rets.len() < 100 || first_price <= 0.0 { continue; }

            let r_12m = last_price / first_price - 1.0;
            if r_12m.abs() < 1e-6 { continue; }

            let n = rets.len() as f64;
            let pct_neg = rets.iter().filter(|r| **r < 0.0).count() as f64 / n;
            let pct_pos = rets.iter().filter(|r| **r > 0.0).count() as f64 / n;

            let fip = r_12m.signum() * (pct_neg - pct_pos) * r_12m.abs();
            if fip.is_finite() { result.insert(tid, fip); }
        }
        result
    }
}

// === SUE_PEAD ===
// Standardized Unexpected Earnings: latest_surprise / std(historical_surprises)
// Only active within 60 days of earnings announcement

pub struct SuePead;
inventory::submit! { &SuePead as &dyn Factor }

impl Factor for SuePead {
    fn name(&self) -> &'static str { "SUE_PEAD" }
    fn category(&self) -> &'static str { "momentum" }
    fn inherent_direction(&self) -> i8 { 1 }
    fn ic_window_months(&self) -> u32 { 6 }

    fn compute(&self, date: Date, cache: &DataCache) -> FactorResult {
        let mut result = FactorResult::default();

        for (&tid, records) in &cache.earnings_surprises {
            let qualifying: Vec<_> = records.iter()
                .filter(|r| r.date <= date)
                .take(8)
                .collect();

            if qualifying.is_empty() { continue; }

            let latest = qualifying[0];
            // 60-day event window: only emit signal if recent
            let days_since = (date - latest.date).num_days();
            if days_since > 60 || days_since < 0 { continue; }

            let latest_surprise = latest.eps_actual - latest.eps_estimated;
            if !latest_surprise.is_finite() { continue; }

            // Historical surprises for standardization
            let hist: Vec<f64> = qualifying[1..].iter()
                .map(|r| r.eps_actual - r.eps_estimated)
                .filter(|v| v.is_finite())
                .collect();

            if hist.len() < 3 { continue; }
            let n = hist.len() as f64;
            let mean = hist.iter().sum::<f64>() / n;
            let std = (hist.iter().map(|v| (v - mean).powi(2)).sum::<f64>() / (n - 1.0)).sqrt();

            if std < 1e-6 { continue; }
            let sue = latest_surprise / std;
            if sue.is_finite() { result.insert(tid, sue); }
        }
        result
    }
}

// === RESIDUAL_MOM_FF3 ===
// Sum of out-of-sample residuals from FF3 regression
// Train on first 60%, predict on last 40%, sum residuals

pub struct ResidualMomFf3;
inventory::submit! { &ResidualMomFf3 as &dyn Factor }

impl Factor for ResidualMomFf3 {
    fn name(&self) -> &'static str { "RESIDUAL_MOM_FF3" }
    fn category(&self) -> &'static str { "momentum" }
    fn inherent_direction(&self) -> i8 { 1 }
    fn ic_window_months(&self) -> u32 { 12 }

    fn compute(&self, date: Date, _cache: &DataCache) -> FactorResult {
        // Requires FF5 daily factor data (Mkt-RF, SMB, HML) which isn't loaded yet.
        // TODO: Load ff5_daily.csv into DataCache, then implement full OLS regression.
        // For now, return empty — this is the most complex factor requiring matrix OLS.
        FactorResult::default()
    }
}
