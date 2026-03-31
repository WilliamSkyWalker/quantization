"""
美股因子处理流水线

实现截面因子的标准化处理流程：
    1. 去极值（MAD 法）
    2. 行业市值中性化（截面回归取残差）— GICS sector
    3. Z-Score / Rank 标准化

处理顺序固定：去极值 → 中性化 → 标准化
所有处理均为截面操作（同一时间对所有股票）。
"""

import logging

import numpy as np
import pandas as pd

from services.config import LOG_LEVEL

logger = logging.getLogger(__name__)
logger.setLevel(LOG_LEVEL)

_industry_dummies_cache: dict[tuple, pd.DataFrame] = {}


def clear_neutralize_cache() -> None:
    _industry_dummies_cache.clear()


# ============================================================
# 去极值：MAD 法
# ============================================================

def winsorize_mad(series: pd.Series, n: float = 5.0) -> pd.Series:
    median = series.median()
    mad = (series - median).abs().median()
    if mad == 0:
        return series
    upper = median + n * 1.4826 * mad
    lower = median - n * 1.4826 * mad
    return series.clip(lower=lower, upper=upper)


# ============================================================
# 行业市值中性化（GICS sector）
# ============================================================

def neutralize(
    factor_df: pd.DataFrame,
    industry_col: str = "sector",
    mktcap_col: str = "ln_mktcap",
    mode: str = "full",
    nonlinear_size: bool = False,
) -> pd.Series:
    """
    行业市值中性化。

    mode:
        "full"      — GICS sector 哑变量 + ln(mktcap) 回归
        "size_only" — 仅对 ln(mktcap) 回归，保留行业 Alpha
        "none"      — 跳过中性化
    """
    if mode == "none":
        return factor_df["factor_value"]

    required_cols = ["factor_value", mktcap_col]
    if mode == "full":
        required_cols.append(industry_col)

    df = factor_df.dropna(subset=required_cols).copy()
    if len(df) < 10:
        logger.warning(f"中性化样本不足({len(df)}只)，跳过中性化")
        return factor_df["factor_value"]

    if mode == "full":
        idx_key = (tuple(df.index), "dummies")
        cached = _industry_dummies_cache.get(idx_key)
        if cached is not None:
            industry_dummies = cached
        else:
            industry_dummies = pd.get_dummies(df[industry_col], prefix="sec", drop_first=True)
            _industry_dummies_cache[idx_key] = industry_dummies
        X = pd.concat([industry_dummies, df[[mktcap_col]]], axis=1).astype(float)
    else:
        X = df[[mktcap_col]].astype(float).copy()

    if nonlinear_size:
        X["ln_mktcap_sq"] = X[mktcap_col] ** 2

    X.insert(0, "const", 1.0)
    y = df["factor_value"].values

    try:
        XtX_inv = np.linalg.pinv(X.values.T @ X.values)
        beta = XtX_inv @ X.values.T @ y
        residuals = y - X.values @ beta
    except np.linalg.LinAlgError:
        logger.warning("中性化回归求解失败，返回原始因子值")
        return factor_df["factor_value"]

    result = factor_df["factor_value"].copy()
    result.loc[df.index] = residuals
    return result


# ============================================================
# 标准化
# ============================================================

def rank_percentile(series: pd.Series) -> pd.Series:
    n = series.count()
    if n <= 1:
        return series * 0.0
    ranks = series.rank(method="average")
    uniform = (ranks - 0.5) / n
    return (uniform - 0.5) * 6.0


def zscore(series: pd.Series) -> pd.Series:
    mean = series.mean()
    std = series.std()
    if std == 0 or pd.isna(std):
        return series * 0.0
    return (series - mean) / std


# ============================================================
# 完整处理流水线
# ============================================================

def process_factor(
    factor_df: pd.DataFrame,
    industry_df: pd.DataFrame = None,
    mktcap_df: pd.DataFrame = None,
    do_winsorize: bool = True,
    do_neutralize: bool = True,
    do_zscore: bool = True,
    mad_n: float = 5.0,
    neutralize_mode: str = "full",
    nonlinear_size: bool = False,
    standardize_mode: str = "zscore",
) -> pd.DataFrame:
    """
    因子处理完整流水线：去极值 → 中性化 → 标准化。

    Args:
        factor_df: DataFrame[ticker, factor_value]
        industry_df: DataFrame[ticker, sector]（GICS sector）
        mktcap_df: DataFrame[ticker, market_cap]
    """
    df = factor_df[["ticker", "factor_value"]].copy()

    # 1. 去极值
    if do_winsorize:
        valid_mask = df["factor_value"].notna()
        df.loc[valid_mask, "factor_value"] = winsorize_mad(
            df.loc[valid_mask, "factor_value"], n=mad_n
        )

    # 2. 中性化
    actually_neutralized = False
    effective_mode = neutralize_mode if do_neutralize else "none"
    if effective_mode != "none" and mktcap_df is not None:
        df = df.merge(
            mktcap_df[["ticker", "market_cap"]], on="ticker", how="left"
        )
        df["market_cap"] = pd.to_numeric(df["market_cap"], errors="coerce")
        df["ln_mktcap"] = np.log(df["market_cap"].fillna(1).clip(lower=1).astype(float))

        if effective_mode == "full" and industry_df is not None:
            df = df.merge(
                industry_df[["ticker", "sector"]], on="ticker", how="left"
            )
        elif effective_mode == "full" and industry_df is None:
            effective_mode = "size_only"

        df["factor_value"] = neutralize(
            df, industry_col="sector", mode=effective_mode,
            nonlinear_size=nonlinear_size,
        )
        actually_neutralized = True
        df = df[["ticker", "factor_value"]]

    # 2.5 二次去极值
    if actually_neutralized and do_winsorize:
        valid_mask = df["factor_value"].notna()
        df.loc[valid_mask, "factor_value"] = winsorize_mad(
            df.loc[valid_mask, "factor_value"], n=mad_n
        )

    # 3. 标准化
    if do_zscore:
        valid_mask = df["factor_value"].notna()
        if standardize_mode == "rank":
            df.loc[valid_mask, "factor_value"] = rank_percentile(
                df.loc[valid_mask, "factor_value"]
            )
        else:
            df.loc[valid_mask, "factor_value"] = zscore(
                df.loc[valid_mask, "factor_value"]
            )
        df["factor_value"] = df["factor_value"].clip(lower=-3.0, upper=3.0)

    return df


def process_all_factors(
    factor_dict: dict[str, pd.DataFrame],
    industry_df: pd.DataFrame = None,
    mktcap_df: pd.DataFrame = None,
) -> dict[str, pd.DataFrame]:
    result = {}
    for name, df in factor_dict.items():
        logger.info(f"US 处理因子: {name}")
        result[name] = process_factor(df, industry_df, mktcap_df)
    return result
