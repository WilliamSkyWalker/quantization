"""
US Mean-Variance Optimizer — cvxpy + OSQP

Replaces Top-N + Softmax with convex optimization:
    max  μ'w − λ·w'Σw − γ·||w − w_prev||₁
    s.t. Σ|w_i| ≤ gross_leverage
         Σw_i = net_exposure
         0 ≤ w_i ≤ max_long   (for longs)
         -max_short ≤ w_i ≤ 0 (for shorts)
         sector gross ≤ max_sector_gross

Usage:
    from backtest.services.us_optimizer import USPortfolioOptimizer

    opt = USPortfolioOptimizer()
    weights = opt.optimize(
        scores=scores_series,
        cov_matrix=cov_np,
        cov_tickers=cov_ticker_list,
        prev_weights=prev_dict,
        sector_map=sector_dict,
    )
    # weights: dict[str, float] {ticker: weight} (positive=long, negative=short)
"""

import logging

import cvxpy as cp
import numpy as np
import pandas as pd

from services.config import (
    US_RISK_AVERSION,
    US_TURNOVER_PENALTY,
    US_MAX_LONG_WEIGHT,
    US_MAX_SHORT_WEIGHT,
    US_MAX_SECTOR_GROSS,
    US_NET_EXPOSURE,
    US_GROSS_LEVERAGE,
    US_MIN_SELECT_SCORE,
    LOG_LEVEL,
)

logger = logging.getLogger(__name__)
logger.setLevel(LOG_LEVEL)


class USPortfolioOptimizer:
    """
    Mean-Variance portfolio optimizer with turnover penalty.

    Formulation:
        max  μ'w − λ·w'Σw − γ·Σ|w_i − w_prev_i|
        s.t. sum(|w|) ≤ gross_leverage
             sum(w) = net_exposure
             -max_short ≤ w_i ≤ max_long
             per-sector gross exposure ≤ max_sector_gross
    """

    def __init__(
        self,
        risk_aversion: float = US_RISK_AVERSION,
        turnover_penalty: float = US_TURNOVER_PENALTY,
        max_long: float = US_MAX_LONG_WEIGHT,
        max_short: float = US_MAX_SHORT_WEIGHT,
        max_sector_gross: float = US_MAX_SECTOR_GROSS,
        net_exposure: float = US_NET_EXPOSURE,
        gross_leverage: float = US_GROSS_LEVERAGE,
        min_score: float = US_MIN_SELECT_SCORE,
    ):
        self.risk_aversion = risk_aversion
        self.turnover_penalty = turnover_penalty
        self.max_long = max_long
        self.max_short = max_short
        self.max_sector_gross = max_sector_gross
        self.net_exposure = net_exposure
        self.gross_leverage = gross_leverage
        self.min_score = min_score

    def optimize(
        self,
        scores: pd.Series,
        cov_matrix: np.ndarray,
        cov_tickers: list[str],
        prev_weights: dict[str, float] | None = None,
        sector_map: dict[str, str] | None = None,
        short_enabled: bool = True,
    ) -> dict[str, float]:
        """
        Run MVO optimization.

        Args:
            scores: Series indexed by ticker, values are composite scores.
            cov_matrix: N×N covariance matrix (aligned with cov_tickers).
            cov_tickers: Ticker list aligned with cov_matrix rows/cols.
            prev_weights: Previous period weights {ticker: weight}.
            sector_map: {ticker: sector_name} for sector constraints.
            short_enabled: Whether to allow short positions.

        Returns:
            Dict {ticker: weight}. Positive = long, negative = short.
            Empty dict if optimization fails.
        """
        if scores.empty:
            logger.warning("USPortfolioOptimizer: 空 scores")
            return {}

        # Align: only use tickers that have both score and covariance data
        cov_set = set(cov_tickers)
        common_tickers = [t for t in scores.index if t in cov_set]

        if len(common_tickers) < 2:
            logger.warning(
                f"USPortfolioOptimizer: score/cov 交集不足 ({len(common_tickers)})"
            )
            return {}

        # Build aligned arrays
        ticker_to_cov_idx = {t: i for i, t in enumerate(cov_tickers)}
        idx = [ticker_to_cov_idx[t] for t in common_tickers]
        n = len(common_tickers)

        mu = scores.loc[common_tickers].values.astype(np.float64)
        sigma = cov_matrix[np.ix_(idx, idx)].astype(np.float64)

        # Regularize: ensure positive semi-definite
        sigma = self._ensure_psd(sigma)

        # Previous weights vector
        w_prev = np.zeros(n)
        if prev_weights:
            for i, t in enumerate(common_tickers):
                w_prev[i] = prev_weights.get(t, 0.0)

        # === cvxpy formulation ===
        w = cp.Variable(n)

        # Objective: maximize alpha - risk - turnover cost
        alpha_term = mu @ w
        risk_term = self.risk_aversion * cp.quad_form(w, sigma)
        turnover_term = self.turnover_penalty * cp.norm1(w - w_prev)
        objective = cp.Maximize(alpha_term - risk_term - turnover_term)

        # Constraints
        constraints = []

        # 1. Net exposure
        constraints.append(cp.sum(w) == self.net_exposure)

        # 2. Gross leverage
        constraints.append(cp.norm1(w) <= self.gross_leverage)

        # 3. Per-stock bounds
        if short_enabled:
            constraints.append(w >= -self.max_short)
            constraints.append(w <= self.max_long)
        else:
            constraints.append(w >= 0)
            constraints.append(w <= self.max_long)

        # 4. Sector constraints
        if sector_map:
            sectors: dict[str, list[int]] = {}
            for i, t in enumerate(common_tickers):
                sec = sector_map.get(t, "Unknown")
                sectors.setdefault(sec, []).append(i)

            for sec, indices in sectors.items():
                if len(indices) > 0:
                    sector_w = w[indices]
                    constraints.append(
                        cp.norm1(sector_w) <= self.max_sector_gross
                    )

        # Solve
        prob = cp.Problem(objective, constraints)
        try:
            prob.solve(solver=cp.OSQP, warm_start=True, max_iter=10000, eps_abs=1e-5, eps_rel=1e-5)
        except cp.SolverError:
            logger.warning("OSQP failed, trying SCS")
            try:
                prob.solve(solver=cp.SCS, max_iters=10000)
            except cp.SolverError:
                logger.error("USPortfolioOptimizer: 所有求解器都失败")
                return {}

        if prob.status not in ("optimal", "optimal_inaccurate"):
            logger.warning(f"USPortfolioOptimizer: 求解状态 {prob.status}")
            return {}

        w_opt = w.value
        if w_opt is None:
            logger.warning("USPortfolioOptimizer: 求解结果为 None")
            return {}

        # Post-process: zero out tiny weights
        w_opt[np.abs(w_opt) < 1e-4] = 0.0

        # Build result dict
        result = {}
        for i, t in enumerate(common_tickers):
            if w_opt[i] != 0.0:
                result[t] = float(w_opt[i])

        n_long = sum(1 for v in result.values() if v > 0)
        n_short = sum(1 for v in result.values() if v < 0)
        gross = sum(abs(v) for v in result.values())
        net = sum(result.values())
        turnover = sum(abs(result.get(t, 0.0) - prev_weights.get(t, 0.0))
                       for t in set(list(result.keys()) + list((prev_weights or {}).keys())))

        logger.info(
            f"MVO: {n_long}L/{n_short}S, gross={gross:.2f}, net={net:.2f}, "
            f"turnover={turnover:.2f}, obj={prob.value:.4f}, status={prob.status}"
        )

        return result

    @staticmethod
    def _ensure_psd(sigma: np.ndarray, min_eigenvalue: float = 1e-8) -> np.ndarray:
        """Ensure matrix is positive semi-definite by clipping eigenvalues."""
        eigenvalues, eigenvectors = np.linalg.eigh(sigma)
        if eigenvalues.min() < min_eigenvalue:
            eigenvalues = np.maximum(eigenvalues, min_eigenvalue)
            sigma = eigenvectors @ np.diag(eigenvalues) @ eigenvectors.T
            # Symmetrize (numerical precision)
            sigma = (sigma + sigma.T) / 2
        return sigma
