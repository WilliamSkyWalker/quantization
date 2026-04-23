//! A-share factor definitions — 29 factors across 7 categories.
//!
//! Each function takes (date, cache) → HashMap<ts_code, f64>.
//! Convention: higher value = more desirable (before direction flip in scoring).

use std::collections::HashMap;
use chrono::NaiveDate;
use super::cache::{AShareCache, ABar};

pub type AFactorResult = HashMap<String, f64>;

/// Factor metadata.
pub struct AFactorDef {
    pub name: &'static str,
    pub category: &'static str,
    pub direction: i8,  // +1 = higher is better, -1 = lower is better
    pub compute: fn(NaiveDate, &AShareCache) -> AFactorResult,
}

/// All 29 A-share factor definitions.
pub fn all_factors() -> Vec<AFactorDef> {
    vec![
        // ── Value (3) ───────────────────────────────────────────────
        AFactorDef { name: "EP", category: "value", direction: 1,
            compute: |date, cache| {
                let mut r = AFactorResult::new();
                for code in cache.active_codes_on(date) {
                    if let Some(bar) = cache.get_bar(code, date) {
                        if bar.pe_ttm.abs() > 0.01 && bar.pe_ttm.is_finite() {
                            r.insert(code.to_string(), 1.0 / bar.pe_ttm);
                        }
                    }
                }
                r
            }},
        AFactorDef { name: "BP", category: "value", direction: 1,
            compute: |date, cache| {
                let mut r = AFactorResult::new();
                for code in cache.active_codes_on(date) {
                    if let Some(bar) = cache.get_bar(code, date) {
                        if bar.pb > 0.01 && bar.pb.is_finite() {
                            r.insert(code.to_string(), 1.0 / bar.pb);
                        }
                    }
                }
                r
            }},
        AFactorDef { name: "DIV_YIELD", category: "value", direction: 1,
            compute: |date, cache| {
                let mut r = AFactorResult::new();
                for code in cache.active_codes_on(date) {
                    if let Some(bar) = cache.get_bar(code, date) {
                        if bar.dv_ttm.is_finite() && bar.dv_ttm > 0.0 {
                            r.insert(code.to_string(), bar.dv_ttm / 100.0); // Tushare 是百分比
                        }
                    }
                }
                r
            }},

        // ── Quality (4) ─────────────────────────────────────────────
        AFactorDef { name: "ROE_TTM", category: "quality", direction: 1,
            compute: |date, cache| {
                let mut r = AFactorResult::new();
                for code in cache.active_codes_on(date) {
                    if let Some(fin) = cache.get_latest_fin(code, date) {
                        if fin.roe.is_finite() { r.insert(code.to_string(), fin.roe); }
                    }
                }
                r
            }},
        AFactorDef { name: "GROSS_MARGIN", category: "quality", direction: 1,
            compute: |date, cache| {
                let mut r = AFactorResult::new();
                for code in cache.active_codes_on(date) {
                    if let Some(fin) = cache.get_latest_fin(code, date) {
                        if fin.gross_margin.is_finite() { r.insert(code.to_string(), fin.gross_margin); }
                    }
                }
                r
            }},
        AFactorDef { name: "PROFIT_STB", category: "quality", direction: 1,
            compute: |date, cache| {
                // Negative of net profit YoY std (lower volatility = higher quality)
                let mut r = AFactorResult::new();
                for code in cache.active_codes_on(date) {
                    let fins = cache.get_fin_history(code, date, 8);
                    if fins.len() < 4 { continue; }
                    let yoys: Vec<f64> = fins.iter()
                        .filter_map(|f| if f.q_netprofit_yoy.is_finite() { Some(f.q_netprofit_yoy) } else { None })
                        .collect();
                    if yoys.len() < 3 { continue; }
                    let mean = yoys.iter().sum::<f64>() / yoys.len() as f64;
                    let var = yoys.iter().map(|v| (v - mean).powi(2)).sum::<f64>() / (yoys.len() - 1) as f64;
                    let std = var.sqrt();
                    if std.is_finite() { r.insert(code.to_string(), -std); }
                }
                r
            }},
        AFactorDef { name: "MARGIN_TREND", category: "quality", direction: 1,
            compute: |date, cache| {
                let mut r = AFactorResult::new();
                for code in cache.active_codes_on(date) {
                    let fins = cache.get_fin_history(code, date, 5);
                    if fins.len() < 2 { continue; }
                    let now = fins[0].gross_margin;
                    let prev = fins.last().unwrap().gross_margin;
                    if now.is_finite() && prev.is_finite() {
                        r.insert(code.to_string(), now - prev);
                    }
                }
                r
            }},

        // ── Growth (3) ──────────────────────────────────────────────
        AFactorDef { name: "NET_PROFIT_YOY", category: "growth", direction: 1,
            compute: |date, cache| {
                factor_from_fin(date, cache, |f| f.q_netprofit_yoy)
            }},
        AFactorDef { name: "REVENUE_YOY", category: "growth", direction: 1,
            compute: |date, cache| {
                factor_from_fin(date, cache, |f| f.q_revenue_yoy)
            }},
        AFactorDef { name: "NET_PROFIT_CAGR_3Y", category: "growth", direction: 1,
            compute: |date, cache| {
                let mut r = AFactorResult::new();
                for code in cache.active_codes_on(date) {
                    let fins = cache.get_fin_history(code, date, 13); // ~3 years quarterly
                    if fins.len() < 12 { continue; }
                    let now_eps = fins[0].eps;
                    let old_eps = fins[11].eps;
                    if now_eps > 0.0 && old_eps > 0.0 && now_eps.is_finite() && old_eps.is_finite() {
                        let cagr = (now_eps / old_eps).powf(1.0 / 3.0) - 1.0;
                        if cagr.is_finite() { r.insert(code.to_string(), cagr); }
                    }
                }
                r
            }},

        // ── Momentum (7) ────────────────────────────────────────────
        AFactorDef { name: "MOM_1M", category: "momentum", direction: 0, // IC-determined
            compute: |date, cache| momentum_factor(date, cache, 20) },
        AFactorDef { name: "MOM_3M", category: "momentum", direction: 0,
            compute: |date, cache| momentum_factor(date, cache, 60) },
        AFactorDef { name: "MOM_12M", category: "momentum", direction: 0,
            compute: |date, cache| momentum_factor(date, cache, 240) },
        AFactorDef { name: "REV_5D", category: "momentum", direction: -1,
            compute: |date, cache| momentum_factor(date, cache, 5) },
        AFactorDef { name: "IND_MOM", category: "momentum", direction: 0,
            compute: |date, cache| {
                // Industry average 20-day momentum
                let mom = momentum_factor(date, cache, 20);
                let mut ind_avg: HashMap<String, (f64, usize)> = HashMap::new();
                for (code, val) in &mom {
                    if let Some(ind) = cache.industry.get(code.as_str()) {
                        let e = ind_avg.entry(ind.industry_name.clone()).or_default();
                        e.0 += val; e.1 += 1;
                    }
                }
                let mut r = AFactorResult::new();
                for (code, _) in &mom {
                    if let Some(ind) = cache.industry.get(code.as_str()) {
                        if let Some((sum, cnt)) = ind_avg.get(&ind.industry_name) {
                            if *cnt > 0 { r.insert(code.clone(), sum / *cnt as f64); }
                        }
                    }
                }
                r
            }},
        AFactorDef { name: "RESIDUAL_MOM", category: "momentum", direction: 0,
            compute: |date, cache| {
                // Simple residual: MOM_12M minus size-predicted component
                let mom12 = momentum_factor(date, cache, 240);
                let mut r = AFactorResult::new();
                for (code, m) in &mom12 {
                    if let Some(bar) = cache.get_bar(code, date) {
                        if bar.total_mv > 0.0 {
                            let ln_size = (bar.total_mv * 10_000.0).ln();
                            // Simple residual: subtract size effect (approximate)
                            r.insert(code.clone(), m - 0.01 * ln_size);
                        }
                    }
                }
                r
            }},
        AFactorDef { name: "CMDTY_MOM", category: "momentum", direction: 0,
            compute: |_date, _cache| {
                // Commodity index momentum — needs commodity price data
                // Placeholder: return empty (will be populated when commodity cache is loaded)
                AFactorResult::new()
            }},

        // ── Technical (5) ───────────────────────────────────────────
        AFactorDef { name: "TURN_20D", category: "technical", direction: -1,
            compute: |date, cache| {
                let mut r = AFactorResult::new();
                for code in cache.active_codes_on(date) {
                    let bars = cache.get_bars_before(code, date, 20);
                    if bars.len() >= 15 {
                        let avg: f64 = bars.iter().map(|b| b.turnover_rate).sum::<f64>() / bars.len() as f64;
                        if avg.is_finite() { r.insert(code.to_string(), avg); }
                    }
                }
                r
            }},
        AFactorDef { name: "VOL_20D", category: "technical", direction: -1,
            compute: |date, cache| {
                let mut r = AFactorResult::new();
                for code in cache.active_codes_on(date) {
                    let bars = cache.get_bars_before(code, date, 20);
                    if bars.len() >= 15 {
                        let rets: Vec<f64> = bars.iter().filter_map(|b| {
                            if b.pct_chg.is_finite() { Some(b.pct_chg / 100.0) } else { None }
                        }).collect();
                        if rets.len() >= 10 {
                            let mean = rets.iter().sum::<f64>() / rets.len() as f64;
                            let var = rets.iter().map(|r| (r - mean).powi(2)).sum::<f64>() / (rets.len() - 1) as f64;
                            let vol = var.sqrt() * (252.0_f64).sqrt();
                            if vol.is_finite() { r.insert(code.to_string(), vol); }
                        }
                    }
                }
                r
            }},
        AFactorDef { name: "PRICE_DEV_60D", category: "technical", direction: -1,
            compute: |date, cache| {
                let mut r = AFactorResult::new();
                for code in cache.active_codes_on(date) {
                    let bars = cache.get_bars_before(code, date, 60);
                    if bars.len() >= 40 {
                        let ma: f64 = bars.iter().map(|b| b.close * b.adj_factor).sum::<f64>() / bars.len() as f64;
                        if let Some(bar) = bars.last() {
                            let adj_close = bar.close * bar.adj_factor;
                            if ma > 0.0 {
                                r.insert(code.to_string(), (adj_close - ma) / ma);
                            }
                        }
                    }
                }
                r
            }},
        AFactorDef { name: "SIZE", category: "technical", direction: -1,
            compute: |date, cache| {
                let mut r = AFactorResult::new();
                for code in cache.active_codes_on(date) {
                    if let Some(bar) = cache.get_bar(code, date) {
                        if bar.total_mv > 0.0 {
                            r.insert(code.to_string(), (bar.total_mv * 10_000.0).ln());
                        }
                    }
                }
                r
            }},
        AFactorDef { name: "VOL_PRICE_DIV", category: "technical", direction: -1,
            compute: |date, cache| {
                // Volume-price divergence: vol trend vs price trend
                let mut r = AFactorResult::new();
                for code in cache.active_codes_on(date) {
                    let bars = cache.get_bars_before(code, date, 20);
                    if bars.len() >= 15 {
                        let n = bars.len() as f64;
                        let vol_trend: f64 = bars.windows(2)
                            .filter_map(|w| {
                                if w[0].vol > 0.0 && w[1].vol > 0.0 {
                                    Some(w[1].vol / w[0].vol - 1.0)
                                } else { None }
                            }).sum::<f64>();
                        let price_trend: f64 = bars.windows(2)
                            .filter_map(|w| {
                                let adj0 = w[0].close * w[0].adj_factor;
                                let adj1 = w[1].close * w[1].adj_factor;
                                if adj0 > 0.0 { Some(adj1 / adj0 - 1.0) } else { None }
                            }).sum::<f64>();
                        let div = vol_trend - price_trend;
                        if div.is_finite() { r.insert(code.to_string(), div); }
                    }
                }
                r
            }},

        // ── Macro (4) ───────────────────────────────────────────────
        // Macro factors are market-wide, same value for all stocks.
        // Implemented as cross-sectional constants (all stocks get same value).
        AFactorDef { name: "MACRO_CYCLE", category: "macro", direction: 0,
            compute: |_date, _cache| AFactorResult::new() }, // TODO: needs macro data
        AFactorDef { name: "MACRO_LIQD", category: "macro", direction: 0,
            compute: |_date, _cache| AFactorResult::new() },
        AFactorDef { name: "MACRO_INFL", category: "macro", direction: 0,
            compute: |_date, _cache| AFactorResult::new() },
        AFactorDef { name: "MACRO_EXTR", category: "macro", direction: 0,
            compute: |_date, _cache| AFactorResult::new() },

        // ── Sentiment (4) ───────────────────────────────────────────
        AFactorDef { name: "POLICY_SENT", category: "sentiment", direction: 0,
            compute: |_date, _cache| AFactorResult::new() }, // TODO: needs scraper data
        AFactorDef { name: "POLICY_INTENSITY", category: "sentiment", direction: 0,
            compute: |_date, _cache| AFactorResult::new() },
        AFactorDef { name: "ANALYST_RATING", category: "sentiment", direction: 1,
            compute: |_date, _cache| AFactorResult::new() }, // TODO: needs research_report
        AFactorDef { name: "ANALYST_COVERAGE", category: "sentiment", direction: 1,
            compute: |_date, _cache| AFactorResult::new() },
    ]
}

// ── Helpers ─────────────────────────────────────────────────────────────

fn factor_from_fin(
    date: NaiveDate,
    cache: &AShareCache,
    field: fn(&super::cache::AFinIndicator) -> f64,
) -> AFactorResult {
    let mut r = AFactorResult::new();
    for code in cache.active_codes_on(date) {
        if let Some(fin) = cache.get_latest_fin(code, date) {
            let val = field(fin);
            if val.is_finite() { r.insert(code.to_string(), val); }
        }
    }
    r
}

fn momentum_factor(date: NaiveDate, cache: &AShareCache, days: usize) -> AFactorResult {
    let mut r = AFactorResult::new();
    for code in cache.active_codes_on(date) {
        let bars = cache.get_bars_before(code, date, days + 1);
        if bars.len() >= days {
            let newest = bars.last().unwrap();
            let oldest = bars[0];
            let adj_new = newest.close * newest.adj_factor;
            let adj_old = oldest.close * oldest.adj_factor;
            if adj_old > 0.0 {
                let ret = adj_new / adj_old - 1.0;
                if ret.is_finite() { r.insert(code.to_string(), ret); }
            }
        }
    }
    r
}
