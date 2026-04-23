//! Fama-French 5-Factor Regression + Capture Ratios
//!
//! Regression model:
//!     Rp - Rf = α + β₁(Mkt-RF) + β₂(SMB) + β₃(HML) + β₄(RMW) + β₅(CMA) + ε
//!
//! FF5 daily factor data loaded from a local CSV cache file
//! (exported from Kenneth French Data Library).

use std::collections::HashMap;
use std::path::Path;

use chrono::{Datelike, NaiveDate};
use nalgebra::{DMatrix, DVector};
use tracing::{debug, info, warn};

use quant_core::types::Date;

/// Factor names in the FF5 model.
pub const FACTOR_NAMES: &[&str] = &["Mkt-RF", "SMB", "HML", "RMW", "CMA"];

/// A single day's FF5 factor returns + risk-free rate (all in decimal, not %).
#[derive(Debug, Clone)]
pub struct FF5Day {
    pub date: Date,
    pub mkt_rf: f64,
    pub smb: f64,
    pub hml: f64,
    pub rmw: f64,
    pub cma: f64,
    pub rf: f64,
}

/// FF5 regression result for a period.
#[derive(Debug, Clone)]
pub struct FF5Result {
    pub period: String,
    pub alpha_daily: f64,
    pub alpha_annualized: f64,
    pub alpha_t_stat: f64,
    pub r_squared: f64,
    pub betas: HashMap<String, f64>,
    pub n_obs: usize,
}

/// Up/Down capture ratios vs benchmark.
#[derive(Debug, Clone)]
pub struct CaptureRatios {
    pub up_capture: f64,
    pub down_capture: f64,
    pub capture_ratio: f64, // up / down (higher = better)
    pub n_up_months: usize,
    pub n_down_months: usize,
}

/// Load FF5 daily factor data from a CSV file.
///
/// Expected format (Kenneth French Library daily CSV):
///   date,Mkt-RF,SMB,HML,RMW,CMA,RF
///   2020-01-02,0.0087,-0.0041,...
///
/// Values should already be in decimal (not percentage).
/// If the file uses percentage format, set `pct=true` to divide by 100.
pub fn load_ff5_csv(path: &Path, pct: bool) -> Vec<FF5Day> {
    let content = match std::fs::read_to_string(path) {
        Ok(c) => c,
        Err(e) => {
            warn!("Failed to read FF5 CSV {}: {e}", path.display());
            return vec![];
        }
    };

    let scale = if pct { 0.01 } else { 1.0 };
    let mut days = Vec::new();

    for line in content.lines().skip(1) {
        let parts: Vec<&str> = line.split(',').map(|s| s.trim()).collect();
        if parts.len() < 7 {
            continue;
        }

        // Try YYYYMMDD or YYYY-MM-DD format
        let date = parts[0]
            .parse::<NaiveDate>()
            .or_else(|_| NaiveDate::parse_from_str(parts[0], "%Y%m%d"))
            .ok();
        let date = match date {
            Some(d) => d,
            None => continue,
        };

        let vals: Vec<f64> = parts[1..7]
            .iter()
            .map(|s| s.parse::<f64>().unwrap_or(f64::NAN))
            .collect();
        if vals.iter().any(|v| !v.is_finite()) {
            continue;
        }

        days.push(FF5Day {
            date,
            mkt_rf: vals[0] * scale,
            smb: vals[1] * scale,
            hml: vals[2] * scale,
            rmw: vals[3] * scale,
            cma: vals[4] * scale,
            rf: vals[5] * scale,
        });
    }

    days.sort_by_key(|d| d.date);
    debug!("Loaded {} FF5 daily records from {}", days.len(), path.display());
    days
}

/// Run FF5 regression on strategy daily returns.
///
/// `nav` is a sorted slice of (date, nav_value) from the backtest.
/// `ff5_data` is the loaded FF5 daily factors.
///
/// Returns full-period result plus optional quarterly breakdowns.
pub fn analyze(
    nav: &[(Date, f64)],
    ff5_data: &[FF5Day],
    quarterly: bool,
) -> Vec<FF5Result> {
    if nav.len() < 30 || ff5_data.is_empty() {
        warn!("FF5 analyze: insufficient data (nav={}, ff5={})", nav.len(), ff5_data.len());
        return vec![];
    }

    // Build strategy daily returns
    let mut strat_returns: HashMap<Date, f64> = HashMap::new();
    for i in 1..nav.len() {
        let (date, val) = nav[i];
        let (_, prev_val) = nav[i - 1];
        if prev_val > 0.0 {
            strat_returns.insert(date, val / prev_val - 1.0);
        }
    }

    // Build FF5 lookup
    let ff5_map: HashMap<Date, &FF5Day> = ff5_data.iter().map(|d| (d.date, d)).collect();

    // Merge: only dates present in both
    let mut merged: Vec<(Date, f64, &FF5Day)> = Vec::new();
    for (&date, &ret) in &strat_returns {
        if let Some(&ff5) = ff5_map.get(&date) {
            if ret.is_finite() {
                merged.push((date, ret, ff5));
            }
        }
    }
    merged.sort_by_key(|(d, _, _)| *d);

    if merged.len() < 30 {
        warn!("FF5: only {} overlapping days (need ≥30)", merged.len());
        return vec![];
    }

    let mut results = Vec::new();

    // Full-period regression
    let full = run_regression(&merged, "full");
    info!(
        "FF5 full: α={:.2}% (t={:.2}), R²={:.3}, β_mkt={:.2}",
        full.alpha_annualized * 100.0,
        full.alpha_t_stat,
        full.r_squared,
        full.betas.get("Mkt-RF").unwrap_or(&0.0)
    );
    results.push(full);

    // Quarterly breakdown
    if quarterly {
        // Group by (year, quarter)
        let mut quarters: HashMap<(i32, u32), Vec<(Date, f64, &FF5Day)>> = HashMap::new();
        for &(date, ret, ff5) in &merged {
            let q = ((date.month() - 1) / 3) + 1;
            quarters.entry((date.year(), q)).or_default().push((date, ret, ff5));
        }
        let mut q_keys: Vec<_> = quarters.keys().copied().collect();
        q_keys.sort();

        for (year, q) in q_keys {
            let data = &quarters[&(year, q)];
            if data.len() < 15 {
                continue;
            }
            let period = format!("{year}Q{q}");
            let qresult = run_regression(data, &period);
            results.push(qresult);
        }
    }

    results
}

/// OLS regression: excess_ret ~ intercept + 5 factors
fn run_regression(data: &[(Date, f64, &FF5Day)], period: &str) -> FF5Result {
    let n = data.len();
    let k = 6; // intercept + 5 factors

    // Build y (excess returns) and X (intercept + factors)
    let y = DVector::from_fn(n, |i, _| data[i].1 - data[i].2.rf);
    let x = DMatrix::from_fn(n, k, |i, j| match j {
        0 => 1.0, // intercept
        1 => data[i].2.mkt_rf,
        2 => data[i].2.smb,
        3 => data[i].2.hml,
        4 => data[i].2.rmw,
        5 => data[i].2.cma,
        _ => unreachable!(),
    });

    // OLS: β = (X'X)⁻¹ X'y
    let xtx = x.transpose() * &x;
    let xty = x.transpose() * &y;

    let xtx_inv = match xtx.clone().try_inverse() {
        Some(inv) => inv,
        None => {
            // Fallback: pseudo-inverse via SVD
            let svd = xtx.svd(true, true);
            match svd.pseudo_inverse(1e-10) {
                Ok(pinv) => pinv,
                Err(_) => return FF5Result {
                    period: period.to_string(),
                    alpha_daily: 0.0, alpha_annualized: 0.0, alpha_t_stat: 0.0,
                    r_squared: 0.0, betas: HashMap::new(), n_obs: n,
                },
            }
        }
    };

    let beta = &xtx_inv * xty;
    let residuals = &y - &x * &beta;
    let sigma2 = residuals.dot(&residuals) / (n - k) as f64;

    // Standard errors
    let se = DVector::from_fn(k, |i, _| {
        let v = sigma2 * xtx_inv[(i, i)];
        if v > 0.0 { v.sqrt() } else { f64::INFINITY }
    });

    let alpha = beta[0];
    let alpha_se = se[0];
    let t_stat = if alpha_se > 1e-10 && alpha_se.is_finite() {
        alpha / alpha_se
    } else {
        0.0
    };

    // R²
    let y_mean = y.mean();
    let ss_tot: f64 = y.iter().map(|v| (v - y_mean).powi(2)).sum();
    let ss_res = residuals.dot(&residuals);
    let r_squared = if ss_tot > 0.0 { 1.0 - ss_res / ss_tot } else { 0.0 };

    let mut betas = HashMap::new();
    for (i, &name) in FACTOR_NAMES.iter().enumerate() {
        betas.insert(name.to_string(), beta[i + 1]);
    }

    FF5Result {
        period: period.to_string(),
        alpha_daily: alpha,
        alpha_annualized: alpha * 252.0,
        alpha_t_stat: t_stat,
        r_squared,
        betas,
        n_obs: n,
    }
}

/// Compute monthly up/down capture ratios vs benchmark.
///
/// Up capture = (mean strategy return in up months) / (mean benchmark return in up months)
/// Down capture = (mean strategy return in down months) / (mean benchmark return in down months)
pub fn capture_ratios(
    strategy_nav: &[(Date, f64)],
    benchmark_nav: &[(Date, f64)],
) -> CaptureRatios {
    // Compute monthly returns for both
    let strat_monthly = monthly_returns(strategy_nav);
    let bench_monthly = monthly_returns(benchmark_nav);

    // Merge on (year, month)
    let mut up_strat = Vec::new();
    let mut up_bench = Vec::new();
    let mut dn_strat = Vec::new();
    let mut dn_bench = Vec::new();

    for (&ym, &sr) in &strat_monthly {
        if let Some(&br) = bench_monthly.get(&ym) {
            if br > 0.0 {
                up_strat.push(sr);
                up_bench.push(br);
            } else if br < 0.0 {
                dn_strat.push(sr);
                dn_bench.push(br);
            }
        }
    }

    let up_capture = if !up_bench.is_empty() {
        let mean_s: f64 = up_strat.iter().sum::<f64>() / up_strat.len() as f64;
        let mean_b: f64 = up_bench.iter().sum::<f64>() / up_bench.len() as f64;
        if mean_b.abs() > 1e-10 { mean_s / mean_b } else { 0.0 }
    } else {
        0.0
    };

    let down_capture = if !dn_bench.is_empty() {
        let mean_s: f64 = dn_strat.iter().sum::<f64>() / dn_strat.len() as f64;
        let mean_b: f64 = dn_bench.iter().sum::<f64>() / dn_bench.len() as f64;
        if mean_b.abs() > 1e-10 { mean_s / mean_b } else { 0.0 }
    } else {
        0.0
    };

    let capture_ratio = if down_capture.abs() > 1e-10 {
        up_capture / down_capture
    } else {
        0.0
    };

    info!(
        "Capture ratios: up={up_capture:.2}, down={down_capture:.2}, \
         ratio={capture_ratio:.2} ({}/{} months)",
        up_bench.len(), dn_bench.len()
    );

    CaptureRatios {
        up_capture,
        down_capture,
        capture_ratio,
        n_up_months: up_bench.len(),
        n_down_months: dn_bench.len(),
    }
}

/// Compute monthly returns from daily NAV series.
fn monthly_returns(nav: &[(Date, f64)]) -> HashMap<(i32, u32), f64> {
    // Group by (year, month), take last NAV of each month
    let mut month_end: HashMap<(i32, u32), f64> = HashMap::new();
    for &(date, val) in nav {
        let key = (date.year(), date.month());
        month_end.insert(key, val); // last value wins (nav is sorted)
    }

    // Sort months and compute returns
    let mut keys: Vec<_> = month_end.keys().copied().collect();
    keys.sort();

    let mut returns = HashMap::new();
    for i in 1..keys.len() {
        let prev = month_end[&keys[i - 1]];
        let curr = month_end[&keys[i]];
        if prev > 0.0 {
            returns.insert(keys[i], curr / prev - 1.0);
        }
    }

    returns
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_ols_simple() {
        // y = 0.01 + 1.0 * x + noise
        let data: Vec<(Date, f64, FF5Day)> = (0..100)
            .map(|i| {
                let x = i as f64 * 0.001;
                let y = 0.0001 + 1.0 * x;
                let date = NaiveDate::from_ymd_opt(2020, 1, 1).unwrap()
                    + chrono::Duration::days(i);
                (
                    date,
                    y + 0.0001, // tiny rf to make excess = y
                    FF5Day {
                        date,
                        mkt_rf: x,
                        smb: 0.0,
                        hml: 0.0,
                        rmw: 0.0,
                        cma: 0.0,
                        rf: 0.0001,
                    },
                )
            })
            .collect();

        let refs: Vec<(Date, f64, &FF5Day)> = data
            .iter()
            .map(|(d, r, ff)| (*d, *r, ff))
            .collect();

        let result = run_regression(&refs, "test");
        assert!(result.r_squared > 0.99);
        assert!((result.betas["Mkt-RF"] - 1.0).abs() < 0.01);
    }

    #[test]
    fn test_monthly_returns() {
        let nav = vec![
            (NaiveDate::from_ymd_opt(2020, 1, 31).unwrap(), 100.0),
            (NaiveDate::from_ymd_opt(2020, 2, 28).unwrap(), 110.0),
            (NaiveDate::from_ymd_opt(2020, 3, 31).unwrap(), 105.0),
        ];
        let rets = monthly_returns(&nav);
        assert!((rets[&(2020, 2)] - 0.10).abs() < 1e-6);
        assert!((rets[&(2020, 3)] - (-5.0 / 110.0)).abs() < 1e-6);
    }
}
