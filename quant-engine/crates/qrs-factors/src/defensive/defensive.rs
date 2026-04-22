//! Defensive factors: MAX_RET, DOWNSIDE_BETA (BAB requires FF5 data, simplified)

use qrs_core::types::{Date, FactorResult};
use qrs_data::cache::DataCache;
use crate::registry::Factor;

/// MAX_RET: average of top-5 single-day returns over past month (lower = better)
pub struct MaxRet;
inventory::submit! { &MaxRet as &dyn Factor }
impl Factor for MaxRet {
    fn name(&self) -> &'static str { "MAX_RET" }
    fn category(&self) -> &'static str { "defensive" }
    fn inherent_direction(&self) -> i8 { -1 }
    fn ic_window_months(&self) -> u32 { 12 }
    fn compute(&self, date: Date, cache: &DataCache) -> FactorResult {
        let start = date - chrono::Duration::days(35);
        let mut result = FactorResult::default();
        // Group daily returns by ticker
        let mut ticker_rets: rustc_hash::FxHashMap<qrs_core::types::TickerId, Vec<f64>> = Default::default();
        for (&(tid, d), bar) in &cache.daily_prices {
            if d >= start && d <= date && bar.change_percent.is_finite() {
                ticker_rets.entry(tid).or_default().push(bar.change_percent / 100.0);
            }
        }
        for (tid, mut rets) in ticker_rets {
            if rets.len() < 10 { continue; }
            rets.sort_by(|a, b| b.partial_cmp(a).unwrap_or(std::cmp::Ordering::Equal));
            let top5_avg: f64 = rets.iter().take(5).sum::<f64>() / 5.0;
            if top5_avg.is_finite() { result.insert(tid, top5_avg); }
        }
        result
    }
}

/// DOWNSIDE_BETA: beta computed only on market down-days (simplified, using index returns)
pub struct DownsideBeta;
inventory::submit! { &DownsideBeta as &dyn Factor }
impl Factor for DownsideBeta {
    fn name(&self) -> &'static str { "DOWNSIDE_BETA" }
    fn category(&self) -> &'static str { "defensive" }
    fn inherent_direction(&self) -> i8 { -1 }
    fn ic_window_months(&self) -> u32 { 24 }
    fn compute(&self, date: Date, cache: &DataCache) -> FactorResult {
        // Collect index returns for past 252 days
        let start = date - chrono::Duration::days(380);
        let mut idx_rets: Vec<(Date, f64)> = Vec::new();
        let idx_dates: Vec<_> = cache.trading_days.iter()
            .filter(|&&d| d >= start && d <= date)
            .copied()
            .collect();
        for w in idx_dates.windows(2) {
            let (d0, d1) = (w[0], w[1]);
            if let (Some(p0), Some(p1)) = (cache.get_index_close("^GSPC", d0), cache.get_index_close("^GSPC", d1)) {
                if p0 > 0.0 { idx_rets.push((d1, p1 / p0 - 1.0)); }
            }
        }
        if idx_rets.len() < 60 { return FactorResult::default(); }

        let mean_mkt = idx_rets.iter().map(|(_, r)| *r).sum::<f64>() / idx_rets.len() as f64;
        // Down days only
        let down_days: Vec<_> = idx_rets.iter().filter(|(_, r)| *r < mean_mkt).collect();
        if down_days.len() < 30 { return FactorResult::default(); }

        let mkt_down: Vec<f64> = down_days.iter().map(|(_, r)| *r).collect();
        let mkt_down_mean = mkt_down.iter().sum::<f64>() / mkt_down.len() as f64;
        let mkt_down_var = mkt_down.iter().map(|r| (r - mkt_down_mean).powi(2)).sum::<f64>();
        if mkt_down_var < 1e-14 { return FactorResult::default(); }

        let down_dates: std::collections::HashSet<Date> = down_days.iter().map(|(d, _)| *d).collect();
        let mut result = FactorResult::default();

        // For each ticker, compute downside beta
        // We need daily returns per ticker aligned with down days
        let mut ticker_rets: rustc_hash::FxHashMap<qrs_core::types::TickerId, Vec<(Date, f64)>> = Default::default();
        for (&(tid, d), bar) in &cache.daily_prices {
            if d >= start && d <= date && bar.change_percent.is_finite() {
                ticker_rets.entry(tid).or_default().push((d, bar.change_percent / 100.0));
            }
        }

        for (tid, rets) in &ticker_rets {
            let down_stock: Vec<f64> = rets.iter()
                .filter(|(d, _)| down_dates.contains(d))
                .map(|(_, r)| *r)
                .collect();
            if down_stock.len() < 20 { continue; }

            let stock_mean = down_stock.iter().sum::<f64>() / down_stock.len() as f64;
            let mut cov = 0.0;
            let mut count = 0;
            for (d, r) in rets {
                if down_dates.contains(d) {
                    let mkt_r = idx_rets.iter().find(|(dd, _)| dd == d).map(|(_, r)| *r).unwrap_or(0.0);
                    cov += (r - stock_mean) * (mkt_r - mkt_down_mean);
                    count += 1;
                }
            }
            if count < 20 { continue; }
            let beta = cov / mkt_down_var;
            if beta.is_finite() { result.insert(*tid, beta); }
        }
        result
    }
}
