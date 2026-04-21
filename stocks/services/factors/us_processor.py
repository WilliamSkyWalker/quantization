"""
美股因子处理流水线（polars 版本）

实现截面因子的标准化处理流程：
    1. 去极值（MAD 法）
    2. 行业市值中性化（截面回归取残差）— GICS sector
    3. Z-Score / Rank 标准化

处理顺序固定：去极值 → 中性化 → 标准化
所有处理均为截面操作（同一时间对所有股票）。
"""

import logging

import numpy as np
import polars as pl

from services.config import LOG_LEVEL

logger = logging.getLogger(__name__)
logger.setLevel(LOG_LEVEL)

_industry_dummies_cache: dict[tuple, np.ndarray] = {}


def clear_neutralize_cache() -> None:
    _industry_dummies_cache.clear()


# ============================================================
# 去极值：MAD 法
# ============================================================

def winsorize_mad(values: np.ndarray, n: float = 5.0) -> np.ndarray:
    """MAD 去极值（numpy 数组操作）。"""
    valid = values[~np.isnan(values)]
    if len(valid) == 0:
        return values
    median = np.median(valid)
    mad = np.median(np.abs(valid - median))
    if mad == 0:
        return values
    upper = median + n * 1.4826 * mad
    lower = median - n * 1.4826 * mad
    return np.clip(values, lower, upper)


# ============================================================
# 行业市值中性化（GICS sector）
# ============================================================

def neutralize(
    factor_values: np.ndarray,
    sectors: np.ndarray | None = None,
    ln_mktcap: np.ndarray | None = None,
    mode: str = "full",
    nonlinear_size: bool = False,
) -> np.ndarray:
    """
    行业市值中性化（纯 numpy 操作）。

    mode:
        "full"      — GICS sector 哑变量 + ln(mktcap) 回归
        "size_only" — 仅对 ln(mktcap) 回归，保留行业 Alpha
        "none"      — 跳过中性化
    """
    if mode == "none":
        return factor_values

    valid_mask = ~np.isnan(factor_values)
    if ln_mktcap is not None:
        valid_mask &= ~np.isnan(ln_mktcap)

    n_valid = valid_mask.sum()
    if n_valid < 10:
        logger.warning(f"中性化样本不足({n_valid}只)，跳过中性化")
        return factor_values

    y = factor_values[valid_mask]

    # 构建 X 矩阵
    x_parts = [np.ones((n_valid, 1))]  # intercept

    if mode == "full" and sectors is not None:
        valid_sectors = sectors[valid_mask]
        unique_sectors = np.unique(valid_sectors)
        if len(unique_sectors) > 1:
            # one-hot (drop first)
            for sec in unique_sectors[1:]:
                x_parts.append((valid_sectors == sec).astype(float).reshape(-1, 1))

    if ln_mktcap is not None:
        mc = ln_mktcap[valid_mask].reshape(-1, 1)
        x_parts.append(mc)
        if nonlinear_size:
            x_parts.append(mc ** 2)

    X = np.hstack(x_parts)

    try:
        XtX_inv = np.linalg.pinv(X.T @ X)
        beta = XtX_inv @ X.T @ y
        residuals = y - X @ beta
    except np.linalg.LinAlgError:
        logger.warning("中性化回归求解失败，返回原始因子值")
        return factor_values

    result = factor_values.copy()
    result[valid_mask] = residuals
    return result


# ============================================================
# 标准化
# ============================================================

def rank_percentile(values: np.ndarray) -> np.ndarray:
    """Rank percentile 标准化（numpy）。"""
    valid = ~np.isnan(values)
    n = valid.sum()
    if n <= 1:
        result = np.zeros_like(values)
        result[~valid] = np.nan
        return result
    ranks = np.zeros_like(values)
    ranks[valid] = np.argsort(np.argsort(values[valid])).astype(float) + 1
    uniform = (ranks - 0.5) / n
    result = (uniform - 0.5) * 6.0
    result[~valid] = np.nan
    return result


def zscore(values: np.ndarray) -> np.ndarray:
    """Z-Score 标准化（numpy）。"""
    valid = ~np.isnan(values)
    vals = values[valid]
    if len(vals) == 0:
        return values
    mean = np.mean(vals)
    std = np.std(vals, ddof=1)
    if std == 0 or np.isnan(std):
        return np.zeros_like(values)
    result = values.copy()
    result[valid] = (vals - mean) / std
    return result


# ============================================================
# 完整处理流水线
# ============================================================

def process_factor(
    factor_df: pl.DataFrame,
    industry_df: pl.DataFrame = None,
    mktcap_df: pl.DataFrame = None,
    do_winsorize: bool = True,
    do_neutralize: bool = True,
    do_zscore: bool = True,
    mad_n: float = 5.0,
    neutralize_mode: str = "full",
    nonlinear_size: bool = False,
    standardize_mode: str = "zscore",
) -> pl.DataFrame:
    """
    因子处理完整流水线：去极值 → 中性化 → 标准化。

    Args:
        factor_df: pl.DataFrame[ticker, factor_value]
        industry_df: pl.DataFrame[ticker, sector]（GICS sector）
        mktcap_df: pl.DataFrame[ticker, market_cap]
    """
    df = factor_df.select(["ticker", "factor_value"])
    values = df["factor_value"].to_numpy().astype(np.float64)

    # 1. 去极值
    if do_winsorize:
        values = winsorize_mad(values, n=mad_n)

    # 2. 中性化
    actually_neutralized = False
    effective_mode = neutralize_mode if do_neutralize else "none"

    if effective_mode != "none" and mktcap_df is not None and not mktcap_df.is_empty():
        tickers = df["ticker"].to_list()

        # 获取 ln(mktcap)
        mc_joined = df.select("ticker").join(
            mktcap_df.select(["ticker", "market_cap"]), on="ticker", how="left"
        )
        mc_vals = mc_joined["market_cap"].cast(pl.Float64, strict=False).to_numpy()
        mc_vals = np.where((mc_vals is None) | (np.isnan(mc_vals)) | (mc_vals <= 0), 1.0, mc_vals)
        ln_mc = np.log(mc_vals)

        # 获取 sector
        sectors = None
        if effective_mode == "full" and industry_df is not None and not industry_df.is_empty():
            sec_joined = df.select("ticker").join(
                industry_df.select(["ticker", "sector"]), on="ticker", how="left"
            )
            sectors = sec_joined["sector"].fill_null("Unknown").to_numpy()
        elif effective_mode == "full" and (industry_df is None or industry_df.is_empty()):
            effective_mode = "size_only"

        values = neutralize(values, sectors=sectors, ln_mktcap=ln_mc,
                            mode=effective_mode, nonlinear_size=nonlinear_size)
        actually_neutralized = True

    # 2.5 二次去极值
    if actually_neutralized and do_winsorize:
        values = winsorize_mad(values, n=mad_n)

    # 3. 标准化
    if do_zscore:
        if standardize_mode == "rank":
            values = rank_percentile(values)
        else:
            values = zscore(values)
        values = np.clip(values, -3.0, 3.0)

    return pl.DataFrame({
        "ticker": df["ticker"],
        "factor_value": values,
    })


def process_all_factors(
    factor_dict: dict[str, pl.DataFrame],
    industry_df: pl.DataFrame = None,
    mktcap_df: pl.DataFrame = None,
) -> dict[str, pl.DataFrame]:
    result = {}
    for name, df in factor_dict.items():
        logger.info(f"US 处理因子: {name}")
        result[name] = process_factor(df, industry_df, mktcap_df)
    return result
