"""
回测显著性检验：PSR / DSR / Bootstrap CI

实现金融基础 skill 第 9 节的统计校正：
- Probabilistic Sharpe Ratio (Mertens 2002)
- Deflated Sharpe Ratio (Bailey-Lopez de Prado 2014)
- Block bootstrap Sharpe 置信区间

公式中所有 SR 均为 **非年化** (per-period)；输出时再年化。
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass
from typing import Iterable

import numpy as np
from scipy import stats

logger = logging.getLogger(__name__)

EULER_MASCHERONI = 0.5772156649015329
TRADING_DAYS_PER_YEAR = 252


@dataclass
class SignificanceReport:
    n_obs: int
    sr_per_period: float
    sr_annualized: float
    skewness: float
    kurtosis_pearson: float
    psr: float
    psr_threshold_annual: float
    dsr: float
    dsr_n_trials: int
    expected_max_sr_annualized: float
    bootstrap_ci_low_annualized: float
    bootstrap_ci_high_annualized: float
    bootstrap_n_iter: int
    bootstrap_block_size: int


def nav_to_returns(nav_json: str | list) -> np.ndarray:
    if isinstance(nav_json, str):
        nav_json = json.loads(nav_json)
    if not nav_json:
        return np.array([], dtype=float)
    nav_values = np.array([float(point["nav"]) for point in nav_json], dtype=float)
    if len(nav_values) < 2:
        return np.array([], dtype=float)
    return np.diff(nav_values) / nav_values[:-1]


def compute_sharpe(returns: np.ndarray) -> float:
    if returns.size == 0:
        return float("nan")
    std = returns.std(ddof=1)
    if std == 0:
        return float("nan")
    return float(returns.mean() / std)


def compute_psr(
    returns: np.ndarray,
    sr_threshold_per_period: float = 0.0,
) -> tuple[float, float, float, float]:
    """Probabilistic Sharpe Ratio (Mertens 2002).

    Returns (psr, sr_per_period, skewness, kurtosis_pearson).
    """
    if returns.size < 3:
        return float("nan"), float("nan"), float("nan"), float("nan")
    sr = compute_sharpe(returns)
    skew = float(stats.skew(returns, bias=False))
    kurt_pearson = float(stats.kurtosis(returns, fisher=False, bias=False))  # normal = 3
    t = returns.size
    sigma_sr = math.sqrt(
        (1.0 - skew * sr + (kurt_pearson - 1.0) / 4.0 * sr * sr) / (t - 1)
    )
    if sigma_sr <= 0 or not math.isfinite(sigma_sr):
        return float("nan"), sr, skew, kurt_pearson
    psr = float(stats.norm.cdf((sr - sr_threshold_per_period) / sigma_sr))
    return psr, sr, skew, kurt_pearson


def expected_max_sharpe_scale(n_trials: int) -> float:
    """E[max Z_N] / σ(SR) 的无量纲缩放因子（Bailey-LDP 2014 Eq. 5 简化形式）。

    乘以 σ(SR) 后才是 per-period 的 E[max SR_N]。
    """
    if n_trials <= 1:
        return 0.0
    log_n = math.log(n_trials)
    return math.sqrt(2.0 * log_n) - EULER_MASCHERONI / math.sqrt(2.0 * log_n)


def compute_dsr(returns: np.ndarray, n_trials: int) -> tuple[float, float, float]:
    """Deflated Sharpe Ratio (Bailey-Lopez de Prado 2014).

    Returns (dsr, expected_max_sr_per_period, sigma_sr_per_period).
    """
    if returns.size < 3:
        return float("nan"), float("nan"), float("nan")
    sr = compute_sharpe(returns)
    skew = float(stats.skew(returns, bias=False))
    kurt_pearson = float(stats.kurtosis(returns, fisher=False, bias=False))
    t = returns.size
    sigma_sr = math.sqrt(
        (1.0 - skew * sr + (kurt_pearson - 1.0) / 4.0 * sr * sr) / (t - 1)
    )
    scale = expected_max_sharpe_scale(n_trials)
    e_max_sr = sigma_sr * scale  # per-period E[max SR_N]
    if sigma_sr <= 0 or not math.isfinite(sigma_sr):
        return float("nan"), e_max_sr, sigma_sr
    dsr = float(stats.norm.cdf((sr - e_max_sr) / sigma_sr))
    return dsr, e_max_sr, sigma_sr


def block_bootstrap_sharpe_ci(
    returns: np.ndarray,
    n_iter: int = 1000,
    block_size: int = 21,
    confidence: float = 0.95,
    seed: int | None = 42,
) -> tuple[float, float]:
    """Non-overlapping block bootstrap of annualized Sharpe.

    Returns (low, high) bounds of confidence interval.
    """
    if returns.size < block_size * 2:
        return float("nan"), float("nan")
    rng = np.random.default_rng(seed)
    n = returns.size
    n_blocks_needed = math.ceil(n / block_size)
    starts = np.arange(0, n - block_size + 1)
    sharpes = np.empty(n_iter, dtype=float)
    for i in range(n_iter):
        chosen = rng.choice(starts, size=n_blocks_needed, replace=True)
        sample = np.concatenate([returns[s : s + block_size] for s in chosen])[:n]
        sr = compute_sharpe(sample)
        sharpes[i] = sr * math.sqrt(TRADING_DAYS_PER_YEAR)
    alpha = 1.0 - confidence
    low = float(np.quantile(sharpes, alpha / 2))
    high = float(np.quantile(sharpes, 1.0 - alpha / 2))
    return low, high


def annualize_sr(sr_per_period: float, periods_per_year: int = TRADING_DAYS_PER_YEAR) -> float:
    return sr_per_period * math.sqrt(periods_per_year)


def deannualize_sr(sr_annual: float, periods_per_year: int = TRADING_DAYS_PER_YEAR) -> float:
    return sr_annual / math.sqrt(periods_per_year)


def run_significance_check(
    returns: np.ndarray,
    n_trials: int = 50,
    sr_threshold_annual: float = 0.0,
    bootstrap_n_iter: int = 1000,
    bootstrap_block_size: int = 21,
) -> SignificanceReport:
    sr_threshold_per_period = deannualize_sr(sr_threshold_annual)
    psr, sr, skew, kurt = compute_psr(returns, sr_threshold_per_period)
    dsr, e_max_sr, _sigma = compute_dsr(returns, n_trials)
    ci_low, ci_high = block_bootstrap_sharpe_ci(
        returns,
        n_iter=bootstrap_n_iter,
        block_size=bootstrap_block_size,
    )
    return SignificanceReport(
        n_obs=int(returns.size),
        sr_per_period=sr,
        sr_annualized=annualize_sr(sr),
        skewness=skew,
        kurtosis_pearson=kurt,
        psr=psr,
        psr_threshold_annual=sr_threshold_annual,
        dsr=dsr,
        dsr_n_trials=n_trials,
        expected_max_sr_annualized=annualize_sr(e_max_sr),
        bootstrap_ci_low_annualized=ci_low,
        bootstrap_ci_high_annualized=ci_high,
        bootstrap_n_iter=bootstrap_n_iter,
        bootstrap_block_size=bootstrap_block_size,
    )


def slice_returns_by_date(
    nav_json: str | list,
    start: str | None = None,
    end: str | None = None,
) -> np.ndarray:
    if isinstance(nav_json, str):
        nav_json = json.loads(nav_json)
    if not nav_json:
        return np.array([], dtype=float)
    points = nav_json
    if start:
        points = [p for p in points if p["date"] >= start]
    if end:
        points = [p for p in points if p["date"] <= end]
    return nav_to_returns(points)
