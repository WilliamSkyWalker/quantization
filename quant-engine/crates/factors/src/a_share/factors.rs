//! A-share factor definitions — 29 factors across 7 categories.
//!
//! Each function takes (date, cache) → HashMap<ts_code, f64>.
//! Convention: higher value = more desirable (before direction flip in scoring).

use std::collections::HashMap;
use chrono::NaiveDate;
use super::cache::AShareCache;

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
        AFactorDef { name: "PIOTROSKI_F", category: "quality", direction: 1,
            compute: |date, cache| {
                let mut r = AFactorResult::new();
                for code in cache.active_codes_on(date) {
                    if let Some(fin) = cache.get_latest_fin(code, date) {
                        let mut score: f64 = 0.0;
                        if fin.roa.is_finite() && fin.roa > 0.0 { score += 1.0; }
                        if fin.ocf_to_profit.is_finite() && fin.ocf_to_profit > 1.0 { score += 1.0; }
                        if fin.current_ratio.is_finite() && fin.current_ratio > 1.5 { score += 1.0; }
                        if fin.gross_margin.is_finite() && fin.gross_margin > 20.0 { score += 1.0; }
                        if fin.debt_to_assets.is_finite() && fin.debt_to_assets < 50.0 { score += 1.0; }
                        if fin.assets_turn.is_finite() && fin.assets_turn > 0.5 { score += 1.0; }
                        if score > 0.0 { r.insert(code.to_string(), score); }
                    }
                }
                r
            }},
        AFactorDef { name: "ACCRUALS", category: "quality", direction: -1,
            compute: |date, cache| {
                let mut r = AFactorResult::new();
                for code in cache.active_codes_on(date) {
                    let fins = cache.get_fin_history(code, date, 5);
                    if fins.len() < 2 { continue; }
                    let now = fins[0].ocf_to_profit;
                    let prev = fins[1].ocf_to_profit;
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
                factor_from_fin(date, cache, |f| f.q_sales_yoy)
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
        AFactorDef { name: "REVENUE_ACCELERATION", category: "growth", direction: 1,
            compute: |date, cache| {
                let mut r = AFactorResult::new();
                for code in cache.active_codes_on(date) {
                    let fins = cache.get_fin_history(code, date, 5);
                    if fins.len() < 2 { continue; }
                    let now_yoy = fins[0].q_sales_yoy;
                    let prev_yoy = fins[1].q_sales_yoy;
                    if now_yoy.is_finite() && prev_yoy.is_finite() {
                        r.insert(code.to_string(), now_yoy - prev_yoy);
                    }
                }
                r
            }},
        AFactorDef { name: "GROSS_MARGIN_CHG", category: "growth", direction: 1,
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
        AFactorDef { name: "PRICE_52W_HIGH", category: "momentum", direction: 1,
            compute: |date, cache| {
                let mut r = AFactorResult::new();
                for code in cache.active_codes_on(date) {
                    let bars = cache.get_bars_before(code, date, 380);
                    if bars.len() < 200 { continue; }
                    let max_adj: f64 = bars.iter()
                        .map(|b| b.close * b.adj_factor)
                        .filter(|v| v.is_finite() && *v > 0.0)
                        .fold(0.0_f64, |a, b| a.max(b));
                    if let Some(bar) = bars.last() {
                        let cur = bar.close * bar.adj_factor;
                        if max_adj > 0.0 && cur.is_finite() {
                            r.insert(code.to_string(), cur / max_adj);
                        }
                    }
                }
                r
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
                        let _n = bars.len() as f64;
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
        AFactorDef { name: "FREE_FLOAT_PCT", category: "technical", direction: 1,
            compute: |date, cache| {
                let mut r = AFactorResult::new();
                for code in cache.active_codes_on(date) {
                    if let Some(info) = cache.basics.get(code) {
                        if let (Some(free), Some(total)) = (info.free_share, info.total_share) {
                            if total > 0.0 && free.is_finite() && total.is_finite() {
                                r.insert(code.to_string(), free / total);
                            }
                        }
                    }
                }
                r
            }},
        AFactorDef { name: "AMIHUD_ILLIQ", category: "technical", direction: -1,
            compute: |date, cache| {
                let mut r = AFactorResult::new();
                for code in cache.active_codes_on(date) {
                    let bars = cache.get_bars_before(code, date, 21);
                    if bars.len() < 15 { continue; }
                    let illiq: f64 = bars.iter()
                        .filter_map(|b| {
                            let dollar_vol = b.close * b.amount;
                            if dollar_vol > 0.0 && b.pct_chg.is_finite() {
                                Some(b.pct_chg.abs() / dollar_vol)
                            } else { None }
                        })
                        .sum();
                    if illiq.is_finite() && illiq > 0.0 {
                        r.insert(code.to_string(), illiq);
                    }
                }
                r
            }},
        AFactorDef { name: "BAB_BETA", category: "technical", direction: -1,
            compute: |date, cache| {
                let idx = match cache.index_prices.get("000300.SH") {
                    Some(v) => v,
                    None => return AFactorResult::new(),
                };
                let mut r = AFactorResult::new();
                for code in cache.active_codes_on(date) {
                    let bars = cache.get_bars_before(code, date, 252);
                    if bars.len() < 120 { continue; }
                    let idx_end = idx.partition_point(|(d, _)| *d <= date);
                    let idx_start = if idx_end >= bars.len() + 1 { idx_end - bars.len() - 1 } else { 0 };
                    let idx_slice = &idx[idx_start..idx_end];
                    if idx_slice.len() < 60 { continue; }
                    let idx_rets: Vec<f64> = idx_slice.windows(2)
                        .filter_map(|w| if w[0].1 > 0.0 { Some(w[1].1 / w[0].1 - 1.0) } else { None })
                        .collect();
                    let stock_rets: Vec<f64> = bars.windows(2)
                        .filter_map(|w| {
                            let p0 = w[0].close * w[0].adj_factor;
                            let p1 = w[1].close * w[1].adj_factor;
                            if p0 > 0.0 { Some(p1 / p0 - 1.0) } else { None }
                        })
                        .collect();
                    let n = stock_rets.len().min(idx_rets.len());
                    if n < 60 { continue; }
                    let sr = &stock_rets[stock_rets.len()-n..];
                    let ir = &idx_rets[idx_rets.len()-n..];
                    let m = n as f64;
                    let sx: f64 = ir.iter().sum();
                    let sy: f64 = sr.iter().sum();
                    let sxx: f64 = ir.iter().map(|v| v * v).sum();
                    let sxy: f64 = ir.iter().zip(sr.iter()).map(|(x, y)| x * y).sum();
                    let denom = m * sxx - sx * sx;
                    if denom.abs() > 1e-12 {
                        let beta = (m * sxy - sx * sy) / denom;
                        if beta.is_finite() { r.insert(code.to_string(), beta); }
                    }
                }
                r
            }},
        AFactorDef { name: "RSI_14", category: "technical", direction: -1,
            compute: |date, cache| {
                let mut r = AFactorResult::new();
                for code in cache.active_codes_on(date) {
                    let bars = cache.get_bars_before(code, date, 20);
                    if bars.len() < 15 { continue; }
                    let chgs: Vec<f64> = bars.windows(2)
                        .filter_map(|w| {
                            let p0 = w[0].close * w[0].adj_factor;
                            let p1 = w[1].close * w[1].adj_factor;
                            if p0 > 0.0 { Some(p1 / p0 - 1.0) } else { None }
                        })
                        .collect();
                    if chgs.len() < 14 { continue; }
                    let recent = &chgs[chgs.len() - 14..];
                    let avg_gain: f64 = recent.iter().filter(|v| **v > 0.0).map(|v| *v).sum::<f64>() / 14.0;
                    let avg_loss: f64 = recent.iter().filter(|v| **v < 0.0).map(|v| v.abs()).sum::<f64>() / 14.0;
                    if avg_loss < 1e-10 { continue; }
                    let rs = avg_gain / avg_loss;
                    let rsi = 100.0 - 100.0 / (1.0 + rs);
                    if rsi.is_finite() { r.insert(code.to_string(), rsi); }
                }
                r
            }},
        AFactorDef { name: "MAX_RET", category: "technical", direction: -1,
            compute: |date, cache| {
                let mut r = AFactorResult::new();
                for code in cache.active_codes_on(date) {
                    let bars = cache.get_bars_before(code, date, 35);
                    if bars.len() < 20 { continue; }
                    let mut rets: Vec<f64> = bars.iter()
                        .filter_map(|b| if b.pct_chg.is_finite() { Some(b.pct_chg) } else { None })
                        .collect();
                    if rets.len() < 20 { continue; }
                    rets.sort_by(|a, b| b.partial_cmp(a).unwrap_or(std::cmp::Ordering::Equal));
                    let top5_avg = rets[..5].iter().sum::<f64>() / 5.0;
                    if top5_avg.is_finite() { r.insert(code.to_string(), top5_avg); }
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
