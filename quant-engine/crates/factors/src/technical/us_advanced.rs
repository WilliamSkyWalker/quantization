//! Advanced technical: RSI_14, VOLUME_RATIO, VOLATILITY_21D, PV_TREND

use quant_core::types::{Date, FactorResult, TickerId};
use quant_data::cache::DataCache;
use crate::registry::Factor;

/// RSI 14-day
pub struct Rsi14;
inventory::submit! { &Rsi14 as &dyn Factor }
impl Factor for Rsi14 {
    fn name(&self) -> &'static str { "RSI_14" }
    fn category(&self) -> &'static str { "technical" }
    fn inherent_direction(&self) -> i8 { 0 }
    fn ic_window_months(&self) -> u32 { 6 }
    fn compute(&self, date: Date, cache: &DataCache) -> FactorResult {
        let start = date - chrono::Duration::days(40);
        let mut result = FactorResult::default();
        let mut ticker_rets: rustc_hash::FxHashMap<TickerId, Vec<f64>> = Default::default();
        for (tid, _d, bar) in cache.daily_prices.iter_date_range(start, date) {
            if bar.change_percent.is_finite() {
                ticker_rets.entry(tid).or_default().push(bar.change_percent / 100.0);
            }
        }
        for (tid, rets) in ticker_rets {
            if rets.len() < 14 { continue; }
            let recent = &rets[rets.len()-14..];
            let avg_gain = recent.iter().filter(|r| **r > 0.0).sum::<f64>() / 14.0;
            let avg_loss = recent.iter().filter(|r| **r < 0.0).map(|r| r.abs()).sum::<f64>() / 14.0;
            if avg_loss > 1e-10 {
                let rs = avg_gain / avg_loss;
                let rsi = 100.0 - 100.0 / (1.0 + rs);
                if rsi.is_finite() { result.insert(tid, rsi); }
            } else if avg_gain > 0.0 {
                result.insert(tid, 100.0);
            }
        }
        result
    }
}

/// Volume Ratio: avg_vol(5D) / avg_vol(20D)
pub struct VolumeRatio;
inventory::submit! { &VolumeRatio as &dyn Factor }
impl Factor for VolumeRatio {
    fn name(&self) -> &'static str { "VOLUME_RATIO" }
    fn category(&self) -> &'static str { "technical" }
    fn inherent_direction(&self) -> i8 { -1 }
    fn ic_window_months(&self) -> u32 { 6 }
    fn compute(&self, date: Date, cache: &DataCache) -> FactorResult {
        let start = date - chrono::Duration::days(35);
        let mut result = FactorResult::default();
        let mut ticker_vols: rustc_hash::FxHashMap<TickerId, Vec<(Date, f64)>> = Default::default();
        for (tid, d, bar) in cache.daily_prices.iter_date_range(start, date) {
            if bar.volume.is_finite() && bar.volume > 0.0 {
                ticker_vols.entry(tid).or_default().push((d, bar.volume));
            }
        }
        for (tid, mut vols) in ticker_vols {
            vols.sort_by_key(|(d, _)| *d);
            if vols.len() < 20 { continue; }
            let vol5: f64 = vols[vols.len()-5..].iter().map(|(_, v)| v).sum::<f64>() / 5.0;
            let vol20: f64 = vols[vols.len()-20..].iter().map(|(_, v)| v).sum::<f64>() / 20.0;
            if vol20 > 1e-6 { let vr = vol5 / vol20; if vr.is_finite() { result.insert(tid, vr); } }
        }
        result
    }
}

/// Volatility 21D: annualized std of daily returns
pub struct Volatility21D;
// Note 2026-04-30: mirror of VOL_20D. Kept — removal hurt 14y backtest.
inventory::submit! { &Volatility21D as &dyn Factor }
impl Factor for Volatility21D {
    fn name(&self) -> &'static str { "VOLATILITY_21D" }
    fn category(&self) -> &'static str { "technical" }
    fn inherent_direction(&self) -> i8 { -1 }
    fn ic_window_months(&self) -> u32 { 12 }
    fn compute(&self, date: Date, cache: &DataCache) -> FactorResult {
        // Use vol_20d from merged rolling stats if available
        let mut result = FactorResult::default();
        for (tid, bar) in cache.daily_prices.iter_date(date) {
            if true && bar.vol_20d.is_finite() {
                let ann_vol = bar.vol_20d * (252.0f64).sqrt();
                if ann_vol.is_finite() { result.insert(tid, ann_vol); }
            }
        }
        result
    }
}

/// PV_TREND: price-volume trend (sum(ret * volume) / avg_volume)
pub struct PvTrend;
inventory::submit! { &PvTrend as &dyn Factor }
impl Factor for PvTrend {
    fn name(&self) -> &'static str { "PV_TREND" }
    fn category(&self) -> &'static str { "technical" }
    fn inherent_direction(&self) -> i8 { 0 }
    fn ic_window_months(&self) -> u32 { 6 }
    fn compute(&self, date: Date, cache: &DataCache) -> FactorResult {
        let start = date - chrono::Duration::days(35);
        let mut result = FactorResult::default();
        let mut ticker_data: rustc_hash::FxHashMap<TickerId, Vec<(f64, f64)>> = Default::default();
        for (tid, _d, bar) in cache.daily_prices.iter_date_range(start, date) {
            if bar.change_percent.is_finite() && bar.volume.is_finite() {
                ticker_data.entry(tid).or_default().push((bar.change_percent / 100.0, bar.volume));
            }
        }
        for (tid, data) in ticker_data {
            if data.len() < 10 { continue; }
            let recent = if data.len() > 21 { &data[data.len()-21..] } else { &data };
            let pvt: f64 = recent.iter().map(|(r, v)| r * v).sum();
            let avg_vol = recent.iter().map(|(_, v)| v).sum::<f64>() / recent.len() as f64;
            if avg_vol > 1e-6 { let v = pvt / avg_vol; if v.is_finite() { result.insert(tid, v); } }
        }
        result
    }
}
