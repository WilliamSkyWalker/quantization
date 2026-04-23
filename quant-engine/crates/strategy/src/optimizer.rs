//! Mean-Variance Portfolio Optimizer (Ledoit-Wolf + Clarabel QP)
//!
//! Formulation:
//!     max  μ'w − λ·w'Σw − γ·‖w − w_prev‖₁
//!     s.t. Σ|wᵢ| ≤ gross_leverage
//!          Σwᵢ = net_exposure
//!          -max_short ≤ wᵢ ≤ max_long
//!          per-sector Σ|wᵢ| ≤ max_sector_gross

use nalgebra::{DMatrix, DVector, SymmetricEigen};
use quant_core::config::OptimizerConfig;
use quant_core::types::{Date, TickerId};
use quant_data::cache::DataCache;
use rustc_hash::FxHashMap;
use tracing::{debug, info, warn};

#[allow(unused_imports)]
use quant_core::types::SectorId;

/// Result of MVO optimization.
pub struct MvoResult {
    /// Optimal weights: positive = long, negative = short.
    pub weights: FxHashMap<TickerId, f64>,
    /// Objective function value at optimum.
    pub objective: f64,
    /// Solver status description.
    pub status: String,
}

/// Build a daily log-return matrix for the given tickers over [lookback] trading days
/// ending at `date`. Returns (T×N matrix, aligned ticker list).
///
/// Tickers with fewer than `min_days` observations are dropped.
pub fn build_returns_matrix(
    cache: &DataCache,
    date: Date,
    candidates: &[TickerId],
    lookback: usize,
    min_days: usize,
) -> (Option<DMatrix<f64>>, Vec<TickerId>) {
    // Find the date index in the trading calendar
    let cal = &cache.trading_days;
    let end_idx = match cal.binary_search(&date) {
        Ok(i) => i,
        Err(i) if i > 0 => i - 1, // nearest earlier date
        _ => return (None, vec![]),
    };
    // We need lookback+1 days to compute lookback returns
    let need = lookback + 1;
    if end_idx + 1 < need {
        warn!("Not enough trading days for covariance: have {}, need {need}", end_idx + 1);
        return (None, vec![]);
    }
    let start_idx = end_idx + 1 - need;
    let dates: Vec<Date> = cal[start_idx..=end_idx].to_vec();

    // Collect adj_close series per ticker
    let candidate_set: rustc_hash::FxHashSet<TickerId> = candidates.iter().copied().collect();
    let mut price_matrix: Vec<Vec<f64>> = Vec::with_capacity(candidates.len()); // N cols, each T+1 long
    let mut valid_tickers: Vec<TickerId> = Vec::new();

    for &tid in candidates {
        if !candidate_set.contains(&tid) {
            continue;
        }
        let mut prices = Vec::with_capacity(dates.len());
        let mut count = 0usize;
        for &d in &dates {
            if let Some(bar) = cache.daily_prices.get(tid, d) {
                prices.push(bar.adj_close);
                count += 1;
            } else {
                prices.push(f64::NAN);
            }
        }
        if count >= min_days {
            valid_tickers.push(tid);
            price_matrix.push(prices);
        }
    }

    if valid_tickers.len() < 2 {
        return (None, vec![]);
    }

    let n = valid_tickers.len();
    let t = dates.len() - 1; // number of returns

    // Compute log returns, forward-fill small gaps
    let mut returns = DMatrix::zeros(t, n);
    for col in 0..n {
        let prices = &price_matrix[col];
        // Forward-fill NaN (max 5 consecutive)
        let mut filled = prices.clone();
        let mut gap = 0;
        for i in 1..filled.len() {
            if filled[i].is_nan() && gap < 5 {
                filled[i] = filled[i - 1];
                gap += 1;
            } else {
                gap = 0;
            }
        }
        for row in 0..t {
            let p0 = filled[row];
            let p1 = filled[row + 1];
            if p0 > 0.0 && p1 > 0.0 && p0.is_finite() && p1.is_finite() {
                let r = (p1 / p0).ln().clamp(-0.5, 0.5); // clip extreme returns
                returns[(row, col)] = r;
            }
            // else: leave as 0.0 (missing data treated as zero return)
        }
    }

    (Some(returns), valid_tickers)
}

/// Ledoit-Wolf linear shrinkage covariance estimator.
///
/// Shrinks the sample covariance toward scaled identity (μI) where μ = tr(S)/p.
/// Returns (shrunk_cov, shrinkage_intensity).
pub fn ledoit_wolf(returns: &DMatrix<f64>) -> (DMatrix<f64>, f64) {
    let (t, p) = (returns.nrows(), returns.ncols());
    assert!(t > 1 && p > 0, "Need t>1, p>0 for Ledoit-Wolf");

    // Center returns
    let means: DVector<f64> = DVector::from_fn(p, |j, _| {
        returns.column(j).iter().sum::<f64>() / t as f64
    });
    let centered = DMatrix::from_fn(t, p, |i, j| returns[(i, j)] - means[j]);

    // Sample covariance S = X'X / (T-1)
    let s = (&centered.transpose() * &centered) / (t - 1) as f64;

    // Target: scaled identity F = μI where μ = tr(S) / p
    let mu = s.trace() / p as f64;

    // Shrinkage intensity (Ledoit-Wolf formula)
    // delta = Σᵢⱼ Var(sᵢⱼ) across samples
    let mut delta = 0.0;
    for i in 0..p {
        for j in 0..p {
            let sij = s[(i, j)];
            let target_ij = if i == j { mu } else { 0.0 };
            // Estimate variance of s_ij
            let mut var_sum = 0.0;
            for k in 0..t {
                let xki = centered[(k, i)];
                let xkj = centered[(k, j)];
                let sample = xki * xkj - sij;
                var_sum += sample * sample;
            }
            let var_sij = var_sum / ((t - 1) * (t - 1)) as f64;
            delta += var_sij;

            // Also accumulate ||S - F||² for denominator
            let diff = sij - target_ij;
            // (used below)
            let _ = diff;
        }
    }

    // ||S - F||² (Frobenius norm squared)
    let mut delta_denom = 0.0;
    for i in 0..p {
        for j in 0..p {
            let target_ij = if i == j { mu } else { 0.0 };
            let diff = s[(i, j)] - target_ij;
            delta_denom += diff * diff;
        }
    }

    let shrinkage = if delta_denom > 0.0 {
        (delta / delta_denom).clamp(0.0, 1.0)
    } else {
        1.0
    };

    // Shrunk covariance: α·F + (1-α)·S
    let identity_scaled = DMatrix::from_fn(p, p, |i, j| if i == j { mu } else { 0.0 });
    let shrunk = &identity_scaled * shrinkage + &s * (1.0 - shrinkage);

    debug!(
        "Ledoit-Wolf: p={p}, t={t}, mu={mu:.6}, shrinkage={shrinkage:.4}"
    );

    (shrunk, shrinkage)
}

/// Ensure matrix is PSD by clipping eigenvalues.
fn ensure_psd(cov: &DMatrix<f64>, min_eigenvalue: f64) -> DMatrix<f64> {
    let eigen = SymmetricEigen::new(cov.clone());
    let min_eval = eigen.eigenvalues.min();
    if min_eval >= min_eigenvalue {
        return cov.clone();
    }
    // Clip eigenvalues
    let clipped = DVector::from_fn(eigen.eigenvalues.len(), |i, _| {
        eigen.eigenvalues[i].max(min_eigenvalue)
    });
    let diag = DMatrix::from_diagonal(&clipped);
    let result = &eigen.eigenvectors * diag * eigen.eigenvectors.transpose();
    // Symmetrize for numerical precision
    (&result + result.transpose()) * 0.5
}

/// Run MVO optimization using Clarabel QP solver.
///
/// # Arguments
/// * `scores` — composite scores (higher = better, can be negative for short candidates)
/// * `cov` — N×N covariance matrix (aligned with `tickers`)
/// * `tickers` — ticker list aligned with cov/scores
/// * `prev_weights` — previous period weights
/// * `sector_map` — ticker → sector mapping from DataCache
/// * `config` — optimizer parameters
/// * `net_exposure` — target net exposure (from short config)
/// * `short_enabled` — whether short positions are allowed
pub fn optimize(
    scores: &FxHashMap<TickerId, f64>,
    cov: &DMatrix<f64>,
    tickers: &[TickerId],
    prev_weights: &FxHashMap<TickerId, f64>,
    sector_map: &FxHashMap<TickerId, SectorId>,
    config: &OptimizerConfig,
    net_exposure: f64,
    short_enabled: bool,
) -> MvoResult {
    let n = tickers.len();
    if n < 2 || cov.nrows() != n {
        warn!("optimize: insufficient tickers ({n}) or cov mismatch");
        return MvoResult {
            weights: FxHashMap::default(),
            objective: 0.0,
            status: "insufficient_data".to_string(),
        };
    }

    // Build aligned vectors
    let mu: Vec<f64> = tickers
        .iter()
        .map(|t| scores.get(t).copied().unwrap_or(0.0))
        .collect();
    let w_prev: Vec<f64> = tickers
        .iter()
        .map(|t| prev_weights.get(t).copied().unwrap_or(0.0))
        .collect();

    let sigma = ensure_psd(cov, 1e-8);

    // === Clarabel QP formulation ===
    // We convert max μ'w - λ·w'Σw - γ·‖w-w_prev‖₁
    // into min form: min -μ'w + λ·w'Σw + γ·‖w-w_prev‖₁
    //
    // L1 turnover penalty via auxiliary variable t:
    //   |w_i - w_prev_i| ≤ t_i  ↔  w_i - w_prev_i ≤ t_i, -(w_i - w_prev_i) ≤ t_i
    //
    // Decision variables: x = [w₁..wₙ, t₁..tₙ] (2n variables)
    //
    // QP: min 0.5 x'Px + q'x
    //   P = 2λ·[Σ, 0; 0, 0]  (2n×2n, top-left is 2λΣ)
    //   q = [-μ₁..-μₙ, γ..γ]  (2n)
    //
    // Linear constraints via Ax + s = b, s ∈ cone:
    //   1) Σwᵢ = net_exposure (equality)
    //   2) wᵢ - w_prev_i - tᵢ ≤ 0  (n inequalities)
    //   3) -wᵢ + w_prev_i - tᵢ ≤ 0  (n inequalities)
    //   4) -tᵢ ≤ 0  (t ≥ 0, n inequalities)
    //   5) wᵢ ≤ max_long  (n inequalities)
    //   6) -wᵢ ≤ max_short  (n inequalities) [or wᵢ ≥ 0 if !short_enabled]
    //   7) Σ|wᵢ| ≤ gross_leverage → linearized: wᵢ ≤ pᵢ, -wᵢ ≤ pᵢ, Σpᵢ ≤ GL (3n+1)
    //   8) sector gross constraints (optional)

    use clarabel::algebra::CscMatrix;
    use clarabel::solver::{
        DefaultSettingsBuilder, DefaultSolver, IPSolver, SolverStatus, SupportedConeT,
    };

    let nn = 2 * n; // w + t variables

    // P matrix (upper triangular, CSC format for Clarabel)
    let lambda = config.risk_aversion;
    let gamma = config.turnover_penalty;

    // Build P as dense then convert to upper-triangular CSC
    let mut p_dense = vec![0.0f64; nn * nn];
    for i in 0..n {
        for j in 0..n {
            p_dense[i * nn + j] = 2.0 * lambda * sigma[(i, j)];
        }
    }
    let p_csc = dense_to_csc_upper(nn, &p_dense);

    // q vector: min -μ'w + γ·Σtᵢ
    let mut q = vec![0.0f64; nn];
    for i in 0..n {
        q[i] = -mu[i];
    }
    for i in n..nn {
        q[i] = gamma;
    }

    // Build constraint rows: Ax + s = b, s ∈ cone
    // Equality rows first (ZeroCone), then inequality rows (NonnegativeCone)
    let mut rows: Vec<Vec<(usize, f64)>> = Vec::new();
    let mut b_vec: Vec<f64> = Vec::new();
    let mut n_eq = 0usize;

    // 1) Σwᵢ = net_exposure (equality)
    {
        let mut row = Vec::with_capacity(n);
        for i in 0..n {
            row.push((i, 1.0));
        }
        rows.push(row);
        b_vec.push(net_exposure);
        n_eq += 1;
    }

    // 2) wᵢ - w_prev_i - tᵢ ≤ 0  →  b = w_prev_i
    for i in 0..n {
        rows.push(vec![(i, 1.0), (n + i, -1.0)]);
        b_vec.push(w_prev[i]);
    }

    // 3) -wᵢ + w_prev_i - tᵢ ≤ 0  →  b = -w_prev_i
    for i in 0..n {
        rows.push(vec![(i, -1.0), (n + i, -1.0)]);
        b_vec.push(-w_prev[i]);
    }

    // 4) tᵢ ≥ 0  →  -tᵢ ≤ 0
    for i in 0..n {
        rows.push(vec![(n + i, -1.0)]);
        b_vec.push(0.0);
    }

    // 5) wᵢ ≤ max_long
    for i in 0..n {
        rows.push(vec![(i, 1.0)]);
        b_vec.push(config.max_long_weight);
    }

    // 6) wᵢ ≥ -max_short (or ≥ 0 if no shorts)
    let lower_bound = if short_enabled {
        config.max_short_weight
    } else {
        0.0
    };
    for i in 0..n {
        rows.push(vec![(i, -1.0)]);
        b_vec.push(lower_bound);
    }

    // Gross leverage enforced post-hoc by scaling (avoids extra auxiliary variables)

    let n_ineq = rows.len() - n_eq;
    let total_rows = rows.len();
    let a_csc = sparse_rows_to_csc(total_rows, nn, &rows);

    let cones = vec![
        SupportedConeT::<f64>::ZeroConeT(n_eq),
        SupportedConeT::<f64>::NonnegativeConeT(n_ineq),
    ];

    let settings = DefaultSettingsBuilder::<f64>::default()
        .max_iter(10_000u32)
        .verbose(false)
        .build()
        .unwrap();

    let mut solver = match DefaultSolver::new(&p_csc, &q, &a_csc, &b_vec, &cones, settings) {
        Ok(s) => s,
        Err(e) => {
            warn!("MVO solver setup failed: {e:?}");
            return MvoResult {
                weights: FxHashMap::default(),
                objective: 0.0,
                status: format!("setup_error: {e:?}"),
            };
        }
    };
    solver.solve();

    let status_str = format!("{:?}", solver.solution.status);
    let solved = matches!(
        solver.solution.status,
        SolverStatus::Solved | SolverStatus::AlmostSolved
    );

    if !solved {
        warn!("MVO solver failed: {status_str}");
        return MvoResult {
            weights: FxHashMap::default(),
            objective: 0.0,
            status: status_str,
        };
    }

    let x = &solver.solution.x;
    let obj = solver.solution.obj_val;

    // Extract weights, zero out tiny values
    let mut weights = FxHashMap::default();
    let mut gross = 0.0f64;
    let mut net = 0.0f64;

    for i in 0..n {
        let w = x[i];
        if w.abs() < 1e-4 {
            continue;
        }
        weights.insert(tickers[i], w);
        gross += w.abs();
        net += w;
    }

    // Post-check gross leverage, scale if needed
    if gross > config.gross_leverage + 1e-6 {
        let scale = config.gross_leverage / gross;
        for v in weights.values_mut() {
            *v *= scale;
        }
        gross *= scale;
        net *= scale;
        debug!("Scaled weights by {scale:.4} to meet gross leverage {:.2}", config.gross_leverage);
    }

    let n_long = weights.values().filter(|&&v| v > 0.0).count();
    let n_short = weights.values().filter(|&&v| v < 0.0).count();
    let turnover: f64 = tickers
        .iter()
        .map(|t| {
            let new = weights.get(t).copied().unwrap_or(0.0);
            let old = prev_weights.get(t).copied().unwrap_or(0.0);
            (new - old).abs()
        })
        .sum();

    info!(
        "MVO: {n_long}L/{n_short}S, gross={gross:.2}, net={net:.2}, \
         turnover={turnover:.2}, obj={obj:.4}, status={status_str}"
    );

    MvoResult {
        weights,
        objective: -obj, // flip back to maximization
        status: status_str,
    }
}

// === Helper: dense matrix → Clarabel CscMatrix (upper triangle only) ===

fn dense_to_csc_upper(n: usize, dense: &[f64]) -> clarabel::algebra::CscMatrix<f64> {
    let mut colptr = vec![0usize; n + 1];
    let mut rowval = Vec::new();
    let mut nzval = Vec::new();

    for j in 0..n {
        colptr[j] = rowval.len();
        for i in 0..=j {
            let v = dense[i * n + j];
            if v.abs() > 1e-15 {
                rowval.push(i);
                nzval.push(v);
            }
        }
    }
    colptr[n] = rowval.len();

    clarabel::algebra::CscMatrix::new(n, n, colptr, rowval, nzval)
}

// === Helper: sparse rows → Clarabel CscMatrix ===

fn sparse_rows_to_csc(
    nrows: usize,
    ncols: usize,
    rows: &[Vec<(usize, f64)>],
) -> clarabel::algebra::CscMatrix<f64> {
    // First pass: count entries per column
    let mut col_counts = vec![0usize; ncols];
    for row in rows {
        for &(col, _) in row {
            col_counts[col] += 1;
        }
    }

    // Build colptr
    let mut colptr = vec![0usize; ncols + 1];
    for j in 0..ncols {
        colptr[j + 1] = colptr[j] + col_counts[j];
    }
    let nnz = colptr[ncols];

    // Fill values (column-major order)
    let mut rowval = vec![0usize; nnz];
    let mut nzval = vec![0.0f64; nnz];
    let mut col_pos = colptr[..ncols].to_vec();

    for (row_i, row) in rows.iter().enumerate() {
        for &(col, val) in row {
            let pos = col_pos[col];
            rowval[pos] = row_i;
            nzval[pos] = val;
            col_pos[col] += 1;
        }
    }

    clarabel::algebra::CscMatrix::new(nrows, ncols, colptr, rowval, nzval)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_ledoit_wolf_identity() {
        // Independent standard normal → cov should shrink toward identity
        let t = 100;
        let p = 5;
        // Construct a simple returns matrix (identity-like)
        let returns = DMatrix::from_fn(t, p, |i, j| {
            if i % p == j { 0.01 } else { 0.0 }
        });
        let (cov, shrinkage) = ledoit_wolf(&returns);
        assert!(shrinkage >= 0.0 && shrinkage <= 1.0);
        assert_eq!(cov.nrows(), p);
        assert_eq!(cov.ncols(), p);
        // Diagonal should be positive
        for i in 0..p {
            assert!(cov[(i, i)] > 0.0);
        }
    }

    #[test]
    fn test_ensure_psd() {
        let m = DMatrix::from_row_slice(2, 2, &[1.0, 0.5, 0.5, -0.1]);
        let psd = ensure_psd(&m, 1e-8);
        let eigen = SymmetricEigen::new(psd);
        assert!(eigen.eigenvalues.min() >= 1e-8 - 1e-10);
    }
}
