//! Advanced quality factors: Ohlson-O, QMJ, CCC, Earnings Persistence, Margin Trend

use quant_core::types::{Date, FactorResult};
use quant_data::cache::DataCache;
use crate::registry::Factor;

/// Helper: get field from latest financial record
fn fin_field(cache: &DataCache, tid: quant_core::types::TickerId, date: Date, field: &str) -> Option<f64> {
    cache.financials.get(&tid)?
        .iter()
        .find(|r| r.filing_date <= date)?
        .fields.get(field)
        .copied()
        .filter(|v| v.is_finite())
}

/// Helper: get N quarters of a field, filtered by filing_date <= date
fn fin_n_quarters(cache: &DataCache, tid: quant_core::types::TickerId, date: Date, field: &str, n: usize) -> Vec<f64> {
    match cache.financials.get(&tid) {
        Some(records) => records.iter()
            .filter(|r| r.filing_date <= date)
            .take(n)
            .filter_map(|r| r.fields.get(field).copied().filter(|v| v.is_finite()))
            .collect(),
        None => vec![],
    }
}

// === OHLSON_O ===
pub struct OhlsonO;
inventory::submit! { &OhlsonO as &dyn Factor }
impl Factor for OhlsonO {
    fn name(&self) -> &'static str { "OHLSON_O" }
    fn category(&self) -> &'static str { "quality" }
    fn inherent_direction(&self) -> i8 { -1 }
    fn ic_window_months(&self) -> u32 { 24 }
    fn compute(&self, date: Date, cache: &DataCache) -> FactorResult {
        let mut result = FactorResult::default();
        for (&tid, records) in &cache.financials {
            let recent: Vec<_> = records.iter().filter(|r| r.filing_date <= date).take(2).collect();
            if recent.len() < 2 { continue; }
            let (now, prev) = (recent[0], recent[1]);
            let f = |r: &quant_data::cache::FinancialRecord, s: &str| r.fields.get(s).copied().filter(|v| v.is_finite());
            let ta = match f(now, "total_assets") { Some(v) if v > 1e6 => v, _ => continue };
            let tl = f(now, "total_liabilities").unwrap_or(0.0);
            let wc = f(now, "total_current_assets").unwrap_or(0.0) - f(now, "total_current_liabilities").unwrap_or(0.0);
            let cl = f(now, "total_current_liabilities").unwrap_or(0.0);
            let ca = f(now, "total_current_assets").unwrap_or(1.0).max(1.0);
            let ni = f(now, "net_income").unwrap_or(0.0);
            let ocf = f(now, "operating_cash_flow").unwrap_or(0.0);
            let ni_prev = f(prev, "net_income").unwrap_or(0.0);
            let oeneg = if tl > ta { 1.0 } else { 0.0 };
            let intwo = if ni < 0.0 && ni_prev < 0.0 { 1.0 } else { 0.0 };
            let chin = if (ni.abs() + ni_prev.abs()) > 1e-6 { (ni - ni_prev) / (ni.abs() + ni_prev.abs()) } else { 0.0 };
            let o = -1.32 - 0.407 * (ta / 1e9).max(1e-10).ln() + 6.03 * (tl / ta)
                - 1.43 * (wc / ta) + 0.076 * (cl / ca) - 1.72 * oeneg
                - 2.37 * (ni / ta) - 1.83 * (ocf / tl.max(1.0)) + 0.285 * intwo - 0.521 * chin;
            if o.is_finite() { result.insert(tid, o); }
        }
        result
    }
}

// === QMJ_LEVERAGE ===
pub struct QmjLeverage;
inventory::submit! { &QmjLeverage as &dyn Factor }
impl Factor for QmjLeverage {
    fn name(&self) -> &'static str { "QMJ_LEVERAGE" }
    fn category(&self) -> &'static str { "quality" }
    fn inherent_direction(&self) -> i8 { -1 }
    fn ic_window_months(&self) -> u32 { 24 }
    fn compute(&self, date: Date, cache: &DataCache) -> FactorResult {
        let mut result = FactorResult::default();
        for (&tid, _) in &cache.financials {
            let debt = fin_field(cache, tid, date, "total_debt");
            let equity = fin_field(cache, tid, date, "total_stockholders_equity")
                .or_else(|| fin_field(cache, tid, date, "total_equity"));
            if let (Some(d), Some(e)) = (debt, equity) {
                if e.abs() > 1e-6 { let v = d / e; if v.is_finite() { result.insert(tid, v); } }
            }
        }
        result
    }
}

// === QMJ_EARNINGS_VOL ===
pub struct QmjEarningsVol;
inventory::submit! { &QmjEarningsVol as &dyn Factor }
impl Factor for QmjEarningsVol {
    fn name(&self) -> &'static str { "QMJ_EARNINGS_VOL" }
    fn category(&self) -> &'static str { "quality" }
    fn inherent_direction(&self) -> i8 { -1 }
    fn ic_window_months(&self) -> u32 { 36 }
    fn compute(&self, date: Date, cache: &DataCache) -> FactorResult {
        let mut result = FactorResult::default();
        for (&tid, _) in &cache.financials {
            let vals = fin_n_quarters(cache, tid, date, "net_income", 20);
            if vals.len() < 8 { continue; }
            let n = vals.len() as f64;
            let mean = vals.iter().sum::<f64>() / n;
            let std = (vals.iter().map(|v| (v - mean).powi(2)).sum::<f64>() / (n - 1.0)).sqrt();
            if mean.abs() > 1e-6 && std.is_finite() {
                let cv = std / mean.abs();
                if cv.is_finite() { result.insert(tid, cv); }
            }
        }
        result
    }
}

// === QMJ_ROE_VOL ===
pub struct QmjRoeVol;
inventory::submit! { &QmjRoeVol as &dyn Factor }
impl Factor for QmjRoeVol {
    fn name(&self) -> &'static str { "QMJ_ROE_VOL" }
    fn category(&self) -> &'static str { "quality" }
    fn inherent_direction(&self) -> i8 { -1 }
    fn ic_window_months(&self) -> u32 { 36 }
    fn compute(&self, date: Date, cache: &DataCache) -> FactorResult {
        let mut result = FactorResult::default();
        for (&tid, records) in &cache.financials {
            let roes: Vec<f64> = records.iter()
                .filter(|r| r.filing_date <= date)
                .take(20)
                .filter_map(|r| {
                    let ni = r.fields.get("net_income")?.to_owned();
                    let eq = r.fields.get("total_stockholders_equity")
                        .or(r.fields.get("total_equity"))?.to_owned();
                    if eq.abs() > 1e-6 && ni.is_finite() && eq.is_finite() { Some(ni / eq) } else { None }
                })
                .collect();
            if roes.len() < 8 { continue; }
            let n = roes.len() as f64;
            let mean = roes.iter().sum::<f64>() / n;
            let std = (roes.iter().map(|v| (v - mean).powi(2)).sum::<f64>() / (n - 1.0)).sqrt();
            if std.is_finite() { result.insert(tid, std); }
        }
        result
    }
}

// === QMJ_NET_PAYOUT ===
pub struct QmjNetPayout;
// Note 2026-04-30: identical impl to SHAREHOLDER_YIELD (value/advanced.rs:105).
// Kept — different category (quality vs value) means it contributes to BOTH
// category aggregates, effectively giving net-payout signal extra weight.
inventory::submit! { &QmjNetPayout as &dyn Factor }
impl Factor for QmjNetPayout {
    fn name(&self) -> &'static str { "QMJ_NET_PAYOUT" }
    fn category(&self) -> &'static str { "quality" }
    fn inherent_direction(&self) -> i8 { 1 }
    fn ic_window_months(&self) -> u32 { 24 }
    fn compute(&self, date: Date, cache: &DataCache) -> FactorResult {
        let mut result = FactorResult::default();
        for (&tid, records) in &cache.financials {
            let recent: Vec<_> = records.iter().filter(|r| r.filing_date <= date).take(4).collect();
            if recent.len() < 3 { continue; }
            let divs: f64 = recent.iter().filter_map(|r| r.fields.get("dividends_paid").copied()).filter(|v| v.is_finite()).map(|v| v.abs()).sum();
            let buybacks: f64 = recent.iter().filter_map(|r| r.fields.get("common_stock_repurchased").copied()).filter(|v| v.is_finite()).map(|v| v.abs()).sum();
            let issuance: f64 = recent.iter().filter_map(|r| r.fields.get("net_stock_issuance").copied()).filter(|v| v.is_finite()).sum();
            let mc = match cache.get_market_cap(tid, date) { Some(m) if m > 0.0 => m, _ => continue };
            let payout = (divs + buybacks - issuance.max(0.0)) / mc;
            if payout.is_finite() { result.insert(tid, payout); }
        }
        result
    }
}

// === CASH_CONV_CYCLE ===
pub struct CashConvCycle;
inventory::submit! { &CashConvCycle as &dyn Factor }
impl Factor for CashConvCycle {
    fn name(&self) -> &'static str { "CASH_CONV_CYCLE" }
    fn category(&self) -> &'static str { "quality" }
    fn inherent_direction(&self) -> i8 { -1 }
    fn ic_window_months(&self) -> u32 { 24 }
    fn compute(&self, date: Date, cache: &DataCache) -> FactorResult {
        let mut result = FactorResult::default();
        for (&tid, records) in &cache.key_metrics {
            if let Some(rec) = records.iter().find(|r| r.date <= date) {
                if let Some(&ccc) = rec.fields.get("cash_conversion_cycle") {
                    if ccc.is_finite() { result.insert(tid, ccc); }
                }
            }
        }
        result
    }
}

// === EARNINGS_PERSISTENCE ===
pub struct EarningsPersistence;
inventory::submit! { &EarningsPersistence as &dyn Factor }
impl Factor for EarningsPersistence {
    fn name(&self) -> &'static str { "EARNINGS_PERSISTENCE" }
    fn category(&self) -> &'static str { "quality" }
    fn inherent_direction(&self) -> i8 { 1 }
    fn ic_window_months(&self) -> u32 { 36 }
    fn compute(&self, date: Date, cache: &DataCache) -> FactorResult {
        let mut result = FactorResult::default();
        for (&tid, _) in &cache.financials {
            let eps = fin_n_quarters(cache, tid, date, "eps", 8);
            if eps.len() < 6 { continue; }
            // AR(1) correlation: corr(eps[t], eps[t-1])
            let n = eps.len() - 1;
            let x: Vec<f64> = eps[..n].to_vec();
            let y: Vec<f64> = eps[1..].to_vec();
            let mx = x.iter().sum::<f64>() / n as f64;
            let my = y.iter().sum::<f64>() / n as f64;
            let mut cov = 0.0; let mut vx = 0.0; let mut vy = 0.0;
            for i in 0..n { cov += (x[i]-mx)*(y[i]-my); vx += (x[i]-mx).powi(2); vy += (y[i]-my).powi(2); }
            let d = (vx * vy).sqrt();
            if d > 1e-10 { let r = cov / d; if r.is_finite() { result.insert(tid, r); } }
        }
        result
    }
}

// === MARGIN_TREND ===
pub struct MarginTrend;
inventory::submit! { &MarginTrend as &dyn Factor }
impl Factor for MarginTrend {
    fn name(&self) -> &'static str { "MARGIN_TREND" }
    fn category(&self) -> &'static str { "quality" }
    fn inherent_direction(&self) -> i8 { 1 }
    fn ic_window_months(&self) -> u32 { 24 }
    fn compute(&self, date: Date, cache: &DataCache) -> FactorResult {
        let mut result = FactorResult::default();
        for (&tid, records) in &cache.financials {
            let recent: Vec<_> = records.iter().filter(|r| r.filing_date <= date).take(2).collect();
            if recent.len() < 2 { continue; }
            let gm = |r: &quant_data::cache::FinancialRecord| -> Option<f64> {
                let gp = r.fields.get("gross_profit")?.to_owned();
                let rev = r.fields.get("revenue")?.to_owned();
                if rev.abs() > 1e-6 && gp.is_finite() { Some(gp / rev) } else { None }
            };
            if let (Some(now), Some(prev)) = (gm(recent[0]), gm(recent[1])) {
                let delta = now - prev;
                if delta.is_finite() { result.insert(tid, delta); }
            }
        }
        result
    }
}
