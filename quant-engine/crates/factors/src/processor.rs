//! Factor processing pipeline: winsorize → neutralize → standardize.
//! All operations are cross-sectional (same date, all stocks).

use quant_core::types::{SectorId, TickerId};
use rustc_hash::FxHashMap;
use tracing::debug;

/// Configuration for factor processing.
pub struct ProcessConfig {
    pub do_winsorize: bool,
    pub do_neutralize: bool,
    pub do_standardize: bool,
    pub mad_n: f64,
    pub neutralize_mode: String,
    pub nonlinear_size: bool,
    pub standardize_mode: String,
}

impl Default for ProcessConfig {
    fn default() -> Self {
        Self {
            do_winsorize: true,
            do_neutralize: true,
            do_standardize: true,
            mad_n: 5.0,
            neutralize_mode: "full".to_string(),
            nonlinear_size: false,
            standardize_mode: "zscore".to_string(),
        }
    }
}

/// Process a factor cross-section: winsorize → neutralize → standardize.
///
/// - `values`: factor values by ticker (raw compute output)
/// - `sectors`: ticker → GICS sector (for industry neutralization)
/// - `market_caps`: ticker → market cap (for size neutralization)
pub fn process_factor(
    values: &FxHashMap<TickerId, f64>,
    sectors: &FxHashMap<TickerId, SectorId>,
    market_caps: &FxHashMap<TickerId, f64>,
    config: &ProcessConfig,
) -> FxHashMap<TickerId, f64> {
    if values.is_empty() {
        return FxHashMap::default();
    }

    // Collect valid (finite) entries
    let mut tickers: Vec<TickerId> = Vec::new();
    let mut vals: Vec<f64> = Vec::new();
    for (&tid, &v) in values {
        if v.is_finite() {
            tickers.push(tid);
            vals.push(v);
        }
    }

    if vals.is_empty() {
        return FxHashMap::default();
    }

    // 1. Winsorize (MAD method)
    if config.do_winsorize {
        winsorize_mad(&mut vals, config.mad_n);
    }

    // 2. Neutralize
    let mut actually_neutralized = false;
    if config.do_neutralize && config.neutralize_mode != "none" {
        let neutralized = neutralize(&tickers, &vals, sectors, market_caps, &config);
        if let Some(residuals) = neutralized {
            vals = residuals;
            actually_neutralized = true;
        }
    }

    // 2.5. Re-winsorize after neutralization
    if actually_neutralized && config.do_winsorize {
        winsorize_mad(&mut vals, config.mad_n);
    }

    // 3. Standardize
    if config.do_standardize {
        if config.standardize_mode == "rank" {
            rank_percentile(&mut vals);
        } else {
            zscore_standardize(&mut vals);
        }
        // Clip to [-3, 3]
        for v in vals.iter_mut() {
            *v = v.clamp(-3.0, 3.0);
        }
    }

    // Rebuild map
    let mut result = FxHashMap::default();
    for (i, &tid) in tickers.iter().enumerate() {
        if vals[i].is_finite() {
            result.insert(tid, vals[i]);
        }
    }
    result
}

/// MAD winsorization: clip values to median ± n * 1.4826 * MAD.
fn winsorize_mad(vals: &mut [f64], n: f64) {
    if vals.is_empty() {
        return;
    }
    let median = median_of(vals);
    let mad = median_of(
        &vals
            .iter()
            .map(|v| (v - median).abs())
            .collect::<Vec<_>>(),
    );
    if mad == 0.0 {
        return;
    }
    let bound = n * 1.4826 * mad;
    let lower = median - bound;
    let upper = median + bound;
    for v in vals.iter_mut() {
        *v = v.clamp(lower, upper);
    }
}

/// OLS neutralization: regress factor values on sector dummies + ln(mktcap), return residuals.
fn neutralize(
    tickers: &[TickerId],
    vals: &[f64],
    sectors: &FxHashMap<TickerId, SectorId>,
    market_caps: &FxHashMap<TickerId, f64>,
    config: &ProcessConfig,
) -> Option<Vec<f64>> {
    let n = vals.len();
    if n < 10 {
        debug!("Neutralize: too few samples ({}), skipping", n);
        return None;
    }

    // Build design matrix X
    // Columns: [const, sector_dummies..., ln_mktcap, (ln_mktcap^2)]
    let mode = &config.neutralize_mode;

    // Collect ln(mktcap) for valid rows
    let mut ln_mktcaps: Vec<f64> = Vec::with_capacity(n);
    let mut valid_mask: Vec<bool> = Vec::with_capacity(n);
    for &tid in tickers {
        let mc = market_caps.get(&tid).copied().unwrap_or(0.0);
        if mc > 0.0 && mc.is_finite() {
            ln_mktcaps.push(mc.ln());
            valid_mask.push(true);
        } else {
            ln_mktcaps.push(0.0);
            valid_mask.push(false);
        }
    }

    // Filter to valid rows (have mktcap)
    let valid_indices: Vec<usize> = (0..n).filter(|&i| valid_mask[i]).collect();
    let nv = valid_indices.len();
    if nv < 10 {
        return None;
    }

    // Determine sector dummies (only in "full" mode)
    let mut unique_sectors: Vec<SectorId> = Vec::new();
    if mode == "full" {
        let mut seen = std::collections::HashSet::new();
        for &idx in &valid_indices {
            if let Some(&sid) = sectors.get(&tickers[idx]) {
                if seen.insert(sid) {
                    unique_sectors.push(sid);
                }
            }
        }
        unique_sectors.sort();
        // Drop first for dummy encoding
        if !unique_sectors.is_empty() {
            unique_sectors.remove(0);
        }
    }

    let n_sector_cols = unique_sectors.len();
    let n_extra = if config.nonlinear_size { 1 } else { 0 };
    let k = 1 + n_sector_cols + 1 + n_extra; // const + dummies + ln_mktcap + (ln_mktcap^2)

    // Build X matrix (row-major: nv x k)
    let mut x_data = vec![0.0f64; nv * k];
    let mut y_data = vec![0.0f64; nv];

    for (row, &idx) in valid_indices.iter().enumerate() {
        let offset = row * k;
        // Constant
        x_data[offset] = 1.0;
        // Sector dummies
        if mode == "full" {
            if let Some(&sid) = sectors.get(&tickers[idx]) {
                for (j, &sector) in unique_sectors.iter().enumerate() {
                    if sid == sector {
                        x_data[offset + 1 + j] = 1.0;
                    }
                }
            }
        }
        // ln(mktcap)
        let lnmc = ln_mktcaps[idx];
        x_data[offset + 1 + n_sector_cols] = lnmc;
        // Nonlinear
        if config.nonlinear_size {
            x_data[offset + 1 + n_sector_cols + 1] = lnmc * lnmc;
        }
        // y
        y_data[row] = vals[idx];
    }

    // OLS: beta = pinv(X'X) @ X'y
    // Simple implementation using nalgebra
    let x = nalgebra::DMatrix::from_row_slice(nv, k, &x_data);
    let y = nalgebra::DVector::from_row_slice(&y_data);

    let xtx = x.transpose() * &x;
    let xty = x.transpose() * &y;

    // Use SVD-based pseudoinverse for numerical stability
    let svd = xtx.svd(true, true);
    let beta = match svd.solve(&xty, 1e-10) {
        Ok(b) => b,
        Err(_) => {
            debug!("Neutralize: SVD solve failed, skipping");
            return None;
        }
    };

    let predicted = &x * &beta;

    // Compute residuals, place back in original order
    let mut residuals = vals.to_vec();
    for (row, &idx) in valid_indices.iter().enumerate() {
        residuals[idx] = y_data[row] - predicted[row];
    }

    Some(residuals)
}

/// Z-score standardization.
fn zscore_standardize(vals: &mut [f64]) {
    if vals.is_empty() {
        return;
    }
    let n = vals.len() as f64;
    let mean: f64 = vals.iter().sum::<f64>() / n;
    let variance: f64 = vals.iter().map(|v| (v - mean).powi(2)).sum::<f64>() / (n - 1.0);
    let std = variance.sqrt();
    if std == 0.0 || !std.is_finite() {
        for v in vals.iter_mut() {
            *v = 0.0;
        }
        return;
    }
    for v in vals.iter_mut() {
        *v = (*v - mean) / std;
    }
}

/// Rank percentile standardization: map to [-3, 3].
fn rank_percentile(vals: &mut [f64]) {
    let n = vals.len();
    if n <= 1 {
        for v in vals.iter_mut() {
            *v = 0.0;
        }
        return;
    }

    // Compute ranks (average method)
    let mut indexed: Vec<(usize, f64)> = vals.iter().copied().enumerate().collect();
    indexed.sort_by(|a, b| a.1.partial_cmp(&b.1).unwrap_or(std::cmp::Ordering::Equal));

    let mut ranks = vec![0.0f64; n];
    let mut i = 0;
    while i < n {
        let mut j = i;
        while j < n && indexed[j].1 == indexed[i].1 {
            j += 1;
        }
        // Average rank for ties (1-based)
        let avg_rank = (i + j + 2) as f64 / 2.0;
        for k in i..j {
            ranks[indexed[k].0] = avg_rank;
        }
        i = j;
    }

    // Map to [-3, 3]: uniform = (rank - 0.5) / n, then (uniform - 0.5) * 6
    let nf = n as f64;
    for (idx, v) in vals.iter_mut().enumerate() {
        let uniform = (ranks[idx] - 0.5) / nf;
        *v = (uniform - 0.5) * 6.0;
    }
}

/// Compute median of a slice (non-destructive).
fn median_of(data: &[f64]) -> f64 {
    if data.is_empty() {
        return 0.0;
    }
    let mut sorted = data.to_vec();
    sorted.sort_by(|a, b| a.partial_cmp(b).unwrap_or(std::cmp::Ordering::Equal));
    let n = sorted.len();
    if n % 2 == 0 {
        (sorted[n / 2 - 1] + sorted[n / 2]) / 2.0
    } else {
        sorted[n / 2]
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_winsorize_mad() {
        let mut vals = vec![1.0, 2.0, 3.0, 4.0, 5.0, 100.0];
        winsorize_mad(&mut vals, 5.0);
        // 100.0 should be clipped
        assert!(vals[5] < 100.0);
        // Others should be unchanged or slightly clipped
        assert_eq!(vals[0], 1.0);
    }

    #[test]
    fn test_zscore() {
        let mut vals = vec![1.0, 2.0, 3.0, 4.0, 5.0];
        zscore_standardize(&mut vals);
        // Mean should be ~0
        let mean: f64 = vals.iter().sum::<f64>() / vals.len() as f64;
        assert!(mean.abs() < 1e-10);
        // Std should be ~1
        let var: f64 = vals.iter().map(|v| (v - mean).powi(2)).sum::<f64>() / (vals.len() as f64 - 1.0);
        assert!((var.sqrt() - 1.0).abs() < 1e-10);
    }

    #[test]
    fn test_rank_percentile() {
        let mut vals = vec![10.0, 30.0, 20.0];
        rank_percentile(&mut vals);
        // Rank order: 10 < 20 < 30 → ranks 1, 3, 2
        // Result should be ordered: vals[0] < vals[2] < vals[1]
        assert!(vals[0] < vals[2]);
        assert!(vals[2] < vals[1]);
    }

    #[test]
    fn test_median_of() {
        assert_eq!(median_of(&[1.0, 2.0, 3.0]), 2.0);
        assert_eq!(median_of(&[1.0, 2.0, 3.0, 4.0]), 2.5);
        assert_eq!(median_of(&[5.0]), 5.0);
    }
}
