//! Factor analysis: IC computation, Fama-MacBeth regression, factor decay.

use std::collections::HashMap;

use qrs_core::types::{Date, TickerId};
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
    cache: &qrs_data::cache::DataCache,
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
    cache: &qrs_data::cache::DataCache,
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

/// Compute average rank (1-based, ties get average).
fn rank_values(values: &[f64]) -> Vec<f64> {
    let n = values.len();
    let mut indexed: Vec<(usize, f64)> = values.iter().copied().enumerate().collect();
    indexed.sort_by(|a, b| a.1.partial_cmp(&b.1).unwrap_or(std::cmp::Ordering::Equal));

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
