//! Factor analysis: IC computation, Fama-MacBeth regression, factor decay.

use std::collections::HashMap;

use quant_core::types::{Date, TickerId};
use rustc_hash::FxHashMap;

/// Compute Spearman rank correlation (IC) between factor values and forward returns.
pub fn spearman_ic(
    factor_values: &FxHashMap<TickerId, f64>,
    forward_returns: &FxHashMap<TickerId, f64>,
) -> Option<f64> {
    // Collect common tickers
    let mut pairs: Vec<(f64, f64)> = Vec::new();
    for (&tid, &fv) in factor_values {
        if let Some(&ret) = forward_returns.get(&tid) {
            if fv.is_finite() && ret.is_finite() {
                pairs.push((fv, ret));
            }
        }
    }

    if pairs.len() < 30 {
        return None;
    }

    // Compute ranks
    let n = pairs.len();
    let fv_ranks = rank_values(&pairs.iter().map(|(f, _)| *f).collect::<Vec<_>>());
    let ret_ranks = rank_values(&pairs.iter().map(|(_, r)| *r).collect::<Vec<_>>());

    // Spearman correlation = Pearson of ranks
    let nf = n as f64;
    let mean_f = fv_ranks.iter().sum::<f64>() / nf;
    let mean_r = ret_ranks.iter().sum::<f64>() / nf;

    let mut cov = 0.0;
    let mut var_f = 0.0;
    let mut var_r = 0.0;
    for i in 0..n {
        let df = fv_ranks[i] - mean_f;
        let dr = ret_ranks[i] - mean_r;
        cov += df * dr;
        var_f += df * df;
        var_r += dr * dr;
    }

    let denom = (var_f * var_r).sqrt();
    if denom < 1e-10 {
        return None;
    }

    let rho = cov / denom;
    if rho.is_finite() { Some(rho) } else { None }
}

/// Compute forward returns for a given date and horizon.
pub fn compute_forward_returns(
    date: Date,
    horizon_days: usize,
    cache: &quant_data::cache::DataCache,
) -> FxHashMap<TickerId, f64> {
    // Find the trading day `horizon_days` ahead
    let idx = cache.trading_days.partition_point(|&d| d <= date);
    let future_idx = idx + horizon_days;
    if future_idx >= cache.trading_days.len() {
        return FxHashMap::default();
    }
    let future_date = cache.trading_days[future_idx];

    let mut result = FxHashMap::default();
    // For each ticker with a price on `date` and `future_date`
    for (tid, bar) in cache.daily_prices.iter_date(date) {
        if !bar.close.is_finite() || bar.close <= 0.0 {
            continue;
        }
        if let Some(future_bar) = cache.daily_prices.get(tid, future_date) {
            if future_bar.close.is_finite() && future_bar.close > 0.0 {
                let ret = future_bar.close / bar.close - 1.0;
                if ret.is_finite() {
                    result.insert(tid, ret);
                }
            }
        }
    }
    result
}

/// IC summary for a single factor across multiple dates.
#[derive(Debug, Clone)]
pub struct IcSummary {
    pub factor_name: String,
    pub n_months: usize,
    pub mean_ic: f64,
    pub std_ic: f64,
    pub icir: f64,
    pub t_stat: f64,
    pub pct_positive: f64,
}

/// Compute IC summary for all factors over a panel of dates.
pub fn compute_ic_panel(
    factor_panel: &HashMap<Date, HashMap<String, FxHashMap<TickerId, f64>>>,
    cache: &quant_data::cache::DataCache,
    horizon_days: usize,
) -> Vec<IcSummary> {
    let factor_names: Vec<String> = {
        let mut names = std::collections::HashSet::new();
        for fmap in factor_panel.values() {
            names.extend(fmap.keys().cloned());
        }
        let mut v: Vec<_> = names.into_iter().collect();
        v.sort();
        v
    };

    let mut ic_series: HashMap<String, Vec<f64>> = HashMap::new();

    let mut dates: Vec<Date> = factor_panel.keys().copied().collect();
    dates.sort();

    for &date in &dates {
        let fwd_rets = compute_forward_returns(date, horizon_days, cache);
        if fwd_rets.len() < 50 {
            continue;
        }

        let fmap = &factor_panel[&date];
        for fname in &factor_names {
            if let Some(fvals) = fmap.get(fname) {
                if let Some(ic) = spearman_ic(fvals, &fwd_rets) {
                    ic_series.entry(fname.clone()).or_default().push(ic);
                }
            }
        }
    }

    let mut summaries: Vec<IcSummary> = Vec::new();
    for fname in &factor_names {
        let ics = ic_series.get(fname).map(|v| v.as_slice()).unwrap_or(&[]);
        let n = ics.len();
        if n < 3 {
            summaries.push(IcSummary {
                factor_name: fname.clone(),
                n_months: n,
                mean_ic: f64::NAN,
                std_ic: f64::NAN,
                icir: f64::NAN,
                t_stat: f64::NAN,
                pct_positive: f64::NAN,
            });
            continue;
        }

        let nf = n as f64;
        let mean = ics.iter().sum::<f64>() / nf;
        let var = ics.iter().map(|ic| (ic - mean).powi(2)).sum::<f64>() / (nf - 1.0);
        let std = var.sqrt();
        let icir = if std > 1e-10 { mean / std } else { f64::NAN };
        let t_stat = if std > 1e-10 { mean / (std / nf.sqrt()) } else { f64::NAN };
        let pct_pos = ics.iter().filter(|ic| **ic > 0.0).count() as f64 / nf;

        summaries.push(IcSummary {
            factor_name: fname.clone(),
            n_months: n,
            mean_ic: mean,
            std_ic: std,
            icir,
            t_stat,
            pct_positive: pct_pos,
        });
    }

    // Sort by |ICIR| descending
    summaries.sort_by(|a, b| {
        b.icir.abs().partial_cmp(&a.icir.abs()).unwrap_or(std::cmp::Ordering::Equal)
    });

    summaries
}

// ======================================================================
// Fama-MacBeth Cross-Sectional Regression
// ======================================================================

/// Fama-MacBeth summary for a single factor.
#[derive(Debug, Clone)]
pub struct FmSummary {
    pub factor_name: String,
    pub n_months: usize,
    pub mean_gamma: f64,
    pub std_gamma: f64,
    pub t_stat: f64,
}

/// Run Fama-MacBeth regression across the factor panel.
///
/// Each month: OLS cross-sectional regression r_i = α + Σ γ_k f_{ik} + ε_i
/// Then: t-test on time-series of γ_k.
pub fn fama_macbeth(
    factor_panel: &HashMap<Date, HashMap<String, FxHashMap<TickerId, f64>>>,
    cache: &quant_data::cache::DataCache,
    horizon_days: usize,
) -> Vec<FmSummary> {
    let factor_names: Vec<String> = {
        let mut names = std::collections::HashSet::new();
        for fmap in factor_panel.values() {
            names.extend(fmap.keys().cloned());
        }
        let mut v: Vec<_> = names.into_iter().collect();
        v.sort();
        v
    };

    // Collect per-month gamma coefficients
    let mut gamma_series: HashMap<String, Vec<f64>> = HashMap::new();

    let mut dates: Vec<Date> = factor_panel.keys().copied().collect();
    dates.sort();

    for &date in &dates {
        let fwd_rets = compute_forward_returns(date, horizon_days, cache);
        if fwd_rets.len() < 100 {
            continue;
        }

        let fmap = &factor_panel[&date];

        // Use all factors with >= 30 tickers overlapping forward returns
        let mut avail_factors: Vec<&str> = Vec::new();
        for fname in &factor_names {
            if let Some(fvals) = fmap.get(fname) {
                let common = fvals.keys().filter(|t| fwd_rets.contains_key(t)).count();
                if common >= 30 {
                    avail_factors.push(fname);
                }
            }
        }

        if avail_factors.len() < 5 {
            continue;
        }

        // Use ALL tickers with forward returns. Missing factor values → 0 (z-score mean).
        let common_tickers: Vec<TickerId> = fwd_rets.keys().copied().collect();
        if common_tickers.len() < 100 {
            continue;
        }

        let n = common_tickers.len();
        let k = avail_factors.len();

        // Build y vector (forward returns)
        let y: Vec<f64> = common_tickers.iter()
            .map(|t| fwd_rets[t])
            .collect();

        // Build X matrix (n × k+1): [const, f1, f2, ...fk]
        // Standardize each factor to z-score for comparability
        let mut x_data = vec![0.0f64; n * (k + 1)];
        for row in 0..n {
            x_data[row * (k + 1)] = 1.0; // constant
        }

        for (j, fname) in avail_factors.iter().enumerate() {
            let fvals = &fmap[*fname];
            // Missing → 0.0 (will become 0 after z-score = cross-sectional mean)
            let raw: Vec<f64> = common_tickers.iter()
                .map(|t| fvals.get(t).copied().unwrap_or(0.0))
                .collect();

            // Z-score standardize
            let mean = raw.iter().sum::<f64>() / n as f64;
            let var = raw.iter().map(|v| (v - mean).powi(2)).sum::<f64>() / (n as f64 - 1.0);
            let std = var.sqrt();

            for (row, &val) in raw.iter().enumerate() {
                x_data[row * (k + 1) + j + 1] = if std > 1e-10 {
                    (val - mean) / std
                } else {
                    0.0
                };
            }
        }

        // OLS: β = (X'X)^{-1} X'y using nalgebra
        let x = nalgebra::DMatrix::from_row_slice(n, k + 1, &x_data);
        let y_vec = nalgebra::DVector::from_row_slice(&y);

        let xtx = x.transpose() * &x;
        let xty = x.transpose() * &y_vec;

        let svd = xtx.svd(true, true);
        let beta = match svd.solve(&xty, 1e-10) {
            Ok(b) => b,
            Err(_) => continue,
        };

        // Extract gamma coefficients (skip intercept at index 0)
        for (j, fname) in avail_factors.iter().enumerate() {
            let gamma = beta[j + 1];
            if gamma.is_finite() {
                gamma_series.entry(fname.to_string()).or_default().push(gamma);
            }
        }
    }

    // Summarize: t-test on gamma series
    let mut summaries: Vec<FmSummary> = Vec::new();
    for fname in &factor_names {
        let gammas = gamma_series.get(fname).map(|v| v.as_slice()).unwrap_or(&[]);
        let n = gammas.len();
        if n < 3 {
            summaries.push(FmSummary {
                factor_name: fname.clone(),
                n_months: n,
                mean_gamma: f64::NAN,
                std_gamma: f64::NAN,
                t_stat: f64::NAN,
            });
            continue;
        }

        let nf = n as f64;
        let mean = gammas.iter().sum::<f64>() / nf;
        let var = gammas.iter().map(|g| (g - mean).powi(2)).sum::<f64>() / (nf - 1.0);
        let std = var.sqrt();
        let t = newey_west_t_stat(gammas, newey_west_lag(horizon_days));

        summaries.push(FmSummary {
            factor_name: fname.clone(),
            n_months: n,
            mean_gamma: mean,
            std_gamma: std,
            t_stat: t,
        });
    }

    summaries.sort_by(|a, b| {
        b.t_stat.abs().partial_cmp(&a.t_stat.abs()).unwrap_or(std::cmp::Ordering::Equal)
    });

    summaries
}

// ======================================================================
// A-share variants (String keys, AShareCache)
// ======================================================================

use quant_factors::a_share::cache::AShareCache;

fn spearman_ic_a(
    factor_values: &std::collections::HashMap<String, f64>,
    forward_returns: &std::collections::HashMap<String, f64>,
) -> Option<f64> {
    let mut pairs: Vec<(f64, f64)> = Vec::new();
    for (tid, &fv) in factor_values {
        if let Some(&ret) = forward_returns.get(tid) {
            if fv.is_finite() && ret.is_finite() {
                pairs.push((fv, ret));
            }
        }
    }
    if pairs.len() < 30 { return None; }

    let n = pairs.len();
    let fv_ranks = rank_values(&pairs.iter().map(|(f, _)| *f).collect::<Vec<_>>());
    let ret_ranks = rank_values(&pairs.iter().map(|(_, r)| *r).collect::<Vec<_>>());

    let nf = n as f64;
    let mean_f = fv_ranks.iter().sum::<f64>() / nf;
    let mean_r = ret_ranks.iter().sum::<f64>() / nf;

    let mut cov = 0.0;
    let mut var_f = 0.0;
    let mut var_r = 0.0;
    for i in 0..n {
        let df = fv_ranks[i] - mean_f;
        let dr = ret_ranks[i] - mean_r;
        cov += df * dr;
        var_f += df * df;
        var_r += dr * dr;
    }
    let denom = (var_f * var_r).sqrt();
    if denom < 1e-10 { return None; }
    let rho = cov / denom;
    if rho.is_finite() { Some(rho) } else { None }
}

fn compute_forward_returns_a(
    date: chrono::NaiveDate,
    horizon_days: usize,
    cache: &AShareCache,
) -> std::collections::HashMap<String, f64> {
    // Signals are observed at the signal-date close and can first trade at the
    // next trading-day open. Hold through the close on the horizon-th session.
    let entry_idx = cache.trading_days.partition_point(|&d| d <= date);
    if horizon_days == 0 {
        return std::collections::HashMap::new();
    }
    let future_idx = entry_idx + horizon_days - 1;
    if future_idx >= cache.trading_days.len() {
        return std::collections::HashMap::new();
    }
    let entry_date = cache.trading_days[entry_idx];
    let exit_date = cache.trading_days[future_idx];

    let mut result = std::collections::HashMap::new();
    for (ts_code, bars) in &cache.daily {
        let entry = match bars.binary_search_by_key(&entry_date, |(d, _)| *d) {
            Ok(i) => &bars[i].1,
            Err(_) => continue,
        };
        let exit = match bars.binary_search_by_key(&exit_date, |(d, _)| *d) {
            Ok(i) => &bars[i].1,
            Err(_) => continue,
        };
        let adj_entry = entry.open * entry.adj_factor;
        let adj_exit = exit.close * exit.adj_factor;
        if adj_entry > 0.0 && adj_exit.is_finite() && adj_entry.is_finite() {
            let ret = adj_exit / adj_entry - 1.0;
            if ret.is_finite() {
                result.insert(ts_code.clone(), ret);
            }
        }
    }
    result
}

/// IC analysis for A-share factors.
pub fn compute_ic_panel_a(
    factor_panel: &std::collections::HashMap<chrono::NaiveDate, std::collections::HashMap<String, std::collections::HashMap<String, f64>>>,
    cache: &AShareCache,
    horizon_days: usize,
) -> Vec<IcSummary> {
    let factor_names: Vec<String> = {
        let mut names = std::collections::HashSet::new();
        for fmap in factor_panel.values() {
            names.extend(fmap.keys().cloned());
        }
        let mut v: Vec<_> = names.into_iter().collect();
        v.sort();
        v
    };

    let mut ic_series: HashMap<String, Vec<f64>> = HashMap::new();
    let mut dates: Vec<chrono::NaiveDate> = factor_panel.keys().copied().collect();
    dates.sort();

    for &date in &dates {
        let fwd_rets = compute_forward_returns_a(date, horizon_days, cache);
        if fwd_rets.len() < 50 { continue; }

        let fmap = &factor_panel[&date];
        for fname in &factor_names {
            if let Some(fvals) = fmap.get(fname) {
                if let Some(ic) = spearman_ic_a(fvals, &fwd_rets) {
                    ic_series.entry(fname.clone()).or_default().push(ic);
                }
            }
        }
    }

    let mut summaries: Vec<IcSummary> = Vec::new();
    for fname in &factor_names {
        let ics = ic_series.get(fname).map(|v| v.as_slice()).unwrap_or(&[]);
        let n = ics.len();
        if n < 3 {
            summaries.push(IcSummary {
                factor_name: fname.clone(), n_months: n,
                mean_ic: f64::NAN, std_ic: f64::NAN, icir: f64::NAN,
                t_stat: f64::NAN, pct_positive: f64::NAN,
            });
            continue;
        }
        let nf = n as f64;
        let mean = ics.iter().sum::<f64>() / nf;
        let var = ics.iter().map(|ic| (ic - mean).powi(2)).sum::<f64>() / (nf - 1.0);
        let std = var.sqrt();
        let icir = if std > 1e-10 { mean / std } else { f64::NAN };
        let t_stat = if std > 1e-10 { mean / (std / nf.sqrt()) } else { f64::NAN };
        let pct_pos = ics.iter().filter(|ic| **ic > 0.0).count() as f64 / nf;
        summaries.push(IcSummary {
            factor_name: fname.clone(), n_months: n,
            mean_ic: mean, std_ic: std, icir, t_stat, pct_positive: pct_pos,
        });
    }
    summaries.sort_by(|a, b| b.icir.abs().total_cmp(&a.icir.abs()));
    summaries
}

/// Fama-MacBeth regression for A-share factors.
pub fn fama_macbeth_a(
    factor_panel: &std::collections::HashMap<chrono::NaiveDate, std::collections::HashMap<String, std::collections::HashMap<String, f64>>>,
    cache: &AShareCache,
    horizon_days: usize,
) -> Vec<FmSummary> {
    let factor_names: Vec<String> = {
        let mut names = std::collections::HashSet::new();
        for fmap in factor_panel.values() {
            names.extend(fmap.keys().cloned());
        }
        let mut v: Vec<_> = names.into_iter().collect();
        v.sort();
        v
    };

    let mut gamma_series: HashMap<String, Vec<f64>> = HashMap::new();
    let mut dates: Vec<chrono::NaiveDate> = factor_panel.keys().copied().collect();
    dates.sort();

    for &date in &dates {
        let fwd_rets = compute_forward_returns_a(date, horizon_days, cache);
        if fwd_rets.len() < 100 { continue; }

        let fmap = &factor_panel[&date];
        let mut avail_factors: Vec<&str> = Vec::new();
        for fname in &factor_names {
            if let Some(fvals) = fmap.get(fname) {
                let common = fvals.keys().filter(|t| fwd_rets.contains_key(*t)).count();
                if common >= 30 { avail_factors.push(fname); }
            }
        }
        if avail_factors.len() < 5 { continue; }

        // Missing factor observations are not zero-valued observations. Using
        // only complete cases avoids changing a factor's cross-sectional rank
        // by silently imputing its missing values to the mean.
        let common_tickers: Vec<&String> = fwd_rets.keys()
            .filter(|ticker| {
                avail_factors.iter().all(|factor| {
                    fmap[*factor]
                        .get(ticker.as_str())
                        .is_some_and(|value| value.is_finite())
                })
            })
            .collect();
        if common_tickers.len() < 100 { continue; }

        let n = common_tickers.len();
        let k = avail_factors.len();
        let y: Vec<f64> = common_tickers.iter().map(|t| fwd_rets[*t]).collect();

        let mut x_data = vec![0.0f64; n * (k + 1)];
        for row in 0..n { x_data[row * (k + 1)] = 1.0; }

        for (j, fname) in avail_factors.iter().enumerate() {
            let fvals = &fmap[*fname];
            let raw: Vec<f64> = common_tickers.iter()
                .map(|ticker| fvals[ticker.as_str()])
                .collect();
            let mean = raw.iter().sum::<f64>() / n as f64;
            let var = raw.iter().map(|v| (v - mean).powi(2)).sum::<f64>() / (n as f64 - 1.0);
            let std = var.sqrt();
            for (row, &val) in raw.iter().enumerate() {
                x_data[row * (k + 1) + j + 1] = if std > 1e-10 { (val - mean) / std } else { 0.0 };
            }
        }

        let x = nalgebra::DMatrix::from_row_slice(n, k + 1, &x_data);
        let y_vec = nalgebra::DVector::from_row_slice(&y);
        let xtx = x.transpose() * &x;
        let xty = x.transpose() * &y_vec;
        let svd = xtx.svd(true, true);
        let beta = match svd.solve(&xty, 1e-10) { Ok(b) => b, Err(_) => continue };

        for (j, fname) in avail_factors.iter().enumerate() {
            let gamma = beta[j + 1];
            if gamma.is_finite() {
                gamma_series.entry(fname.to_string()).or_default().push(gamma);
            }
        }
    }

    let mut summaries: Vec<FmSummary> = Vec::new();
    for fname in &factor_names {
        let gammas = gamma_series.get(fname).map(|v| v.as_slice()).unwrap_or(&[]);
        let n = gammas.len();
        if n < 3 {
            summaries.push(FmSummary {
                factor_name: fname.clone(), n_months: n,
                mean_gamma: f64::NAN, std_gamma: f64::NAN, t_stat: f64::NAN,
            });
            continue;
        }
        let nf = n as f64;
        let mean = gammas.iter().sum::<f64>() / nf;
        let var = gammas.iter().map(|g| (g - mean).powi(2)).sum::<f64>() / (nf - 1.0);
        let std = var.sqrt();
        let t = newey_west_t_stat(gammas, newey_west_lag(horizon_days));
        summaries.push(FmSummary {
            factor_name: fname.clone(), n_months: n,
            mean_gamma: mean, std_gamma: std, t_stat: t,
        });
    }
    summaries.sort_by(|a, b| b.t_stat.abs().total_cmp(&a.t_stat.abs()));
    summaries
}

/// Compute average rank (1-based, ties get average).
fn rank_values(values: &[f64]) -> Vec<f64> {
    let n = values.len();
    let mut indexed: Vec<(usize, f64)> = values.iter().copied().enumerate().collect();
    indexed.sort_by(|a, b| a.1.total_cmp(&b.1));

    let mut ranks = vec![0.0; n];
    let mut i = 0;
    while i < n {
        let mut j = i;
        while j < n && indexed[j].1 == indexed[i].1 {
            j += 1;
        }
        let avg_rank = (i + j + 2) as f64 / 2.0;
        for k in i..j {
            ranks[indexed[k].0] = avg_rank;
        }
        i = j;
    }
    ranks
}

/// Newey-West t statistic for a time series mean, using Bartlett weights.
///
/// A factor's forward-return windows can overlap across adjacent rebalance
/// dates, so the Fama-MacBeth gamma series is not assumed independent.
fn newey_west_t_stat(values: &[f64], requested_lag: usize) -> f64 {
    let n = values.len();
    if n < 2 {
        return f64::NAN;
    }

    let mean = values.iter().sum::<f64>() / n as f64;
    let lag = requested_lag.min(n - 1);
    let centered: Vec<f64> = values.iter().map(|value| value - mean).collect();
    let mut long_run_variance = centered.iter().map(|value| value * value).sum::<f64>() / n as f64;

    for offset in 1..=lag {
        let autocovariance = centered[offset..].iter()
            .zip(&centered[..n - offset])
            .map(|(right, left)| right * left)
            .sum::<f64>() / n as f64;
        let weight = 1.0 - offset as f64 / (lag + 1) as f64;
        long_run_variance += 2.0 * weight * autocovariance;
    }

    let standard_error = (long_run_variance / n as f64).sqrt();
    if standard_error.is_finite() && standard_error > 1e-10 {
        mean / standard_error
    } else {
        f64::NAN
    }
}

fn newey_west_lag(horizon_days: usize) -> usize {
    horizon_days.div_ceil(21).max(1)
}

#[cfg(test)]
mod tests {
    use super::*;
    use chrono::NaiveDate;
    use quant_factors::a_share::cache::{ABar, AShareCache};
    use rustc_hash::FxHashMap;

    fn date(day: u32) -> NaiveDate {
        NaiveDate::from_ymd_opt(2024, 7, day).unwrap()
    }

    fn bar(open: f64, close: f64) -> ABar {
        ABar {
            open, high: open.max(close), low: open.min(close), close, pre_close: close,
            pct_chg: 0.0, vol: 1.0, amount: 1.0, adj_factor: 1.0,
            turnover_rate: 0.0, pe_ttm: 0.0, pb: 0.0, ps_ttm: 0.0, dv_ttm: 0.0,
            total_mv: 0.0, circ_mv: 0.0,
        }
    }

    #[test]
    fn a_share_forward_return_starts_at_next_open() {
        let signal_date = date(1);
        let mut daily = FxHashMap::default();
        daily.insert(
            "000001.SZ".to_string(),
            vec![
                (signal_date, bar(9.0, 9.0)),
                (date(2), bar(10.0, 11.0)),
                (date(3), bar(11.0, 12.0)),
            ],
        );
        let cache = AShareCache {
            daily,
            financials: FxHashMap::default(),
            industry: FxHashMap::default(),
            basics: FxHashMap::default(),
            trading_days: vec![signal_date, date(2), date(3)],
            index_prices: FxHashMap::default(),
            ts_codes: vec!["000001.SZ".to_string()],
        };

        let returns = compute_forward_returns_a(signal_date, 2, &cache);
        let value = returns.get("000001.SZ").expect("return is available");
        assert!((value - 0.2).abs() < 1e-12);
    }

    #[test]
    fn newey_west_t_stat_accounts_for_serial_correlation() {
        let values = [0.01, 0.02, 0.03, 0.04, 0.05, 0.06];
        let iid_t = {
            let mean = values.iter().sum::<f64>() / values.len() as f64;
            let variance = values.iter()
                .map(|value| (value - mean).powi(2))
                .sum::<f64>() / (values.len() - 1) as f64;
            mean / (variance.sqrt() / (values.len() as f64).sqrt())
        };
        let nw_t = newey_west_t_stat(&values, 1);

        assert!(nw_t.is_finite());
        assert!(nw_t < iid_t);
        assert_eq!(newey_west_lag(21), 1);
        assert_eq!(newey_west_lag(42), 2);
    }
}
