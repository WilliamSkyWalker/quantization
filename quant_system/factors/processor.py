"""
因子处理流水线

实现截面因子的标准化处理流程：
    1. 去极值（MAD 法）
    2. 行业市值中性化（截面回归取残差）
    3. Z-Score 标准化

处理顺序固定：去极值 → 中性化 → 标准化
所有处理均为截面操作（同一时间对所有股票）。
"""

import logging

import numpy as np
import pandas as pd

from config.settings import LOG_LEVEL

logger = logging.getLogger(__name__)
logger.setLevel(LOG_LEVEL)


# ============================================================
# 去极值：MAD 法
# ============================================================

def winsorize_mad(series: pd.Series, n: float = 5.0) -> pd.Series:
    """
    MAD（中位数绝对偏差）去极值。

    计算方法：
        median = 中位数
        MAD = median(|x - median|)
        上界 = median + n * 1.4826 * MAD
        下界 = median - n * 1.4826 * MAD
        1.4826 是正态分布下 MAD 到标准差的换算常数。

    超出上下界的值被截断到边界值。

    Args:
        series: 因子值序列。
        n: MAD 倍数，默认 5（较宽松，保留更多信息）。

    Returns:
        去极值后的序列。
    """
    median = series.median()
    mad = (series - median).abs().median()

    if mad == 0:
        return series

    upper = median + n * 1.4826 * mad
    lower = median - n * 1.4826 * mad

    clipped = series.clip(lower=lower, upper=upper)

    n_clipped = (series != clipped).sum()
    if n_clipped > 0:
        logger.debug(f"MAD去极值: 截断 {n_clipped} 个值 (上界={upper:.4f}, 下界={lower:.4f})")

    return clipped


# ============================================================
# 行业市值中性化
# ============================================================

def neutralize(
    factor_df: pd.DataFrame,
    industry_col: str = "industry_name",
    mktcap_col: str = "ln_mktcap",
) -> pd.Series:
    """
    行业市值中性化。

    对因子值做截面回归：
        factor_value = β0 + Σ βi * Industry_Dummy_i + β_mv * ln(market_cap) + ε
    取残差 ε 作为中性化后的因子值。

    Args:
        factor_df: DataFrame，必须包含以下列：
            - factor_value: 原始因子值
            - industry_col: 行业名称列
            - mktcap_col: ln(市值) 列
        industry_col: 行业列名。
        mktcap_col: ln(市值) 列名。

    Returns:
        中性化后的因子值 Series（残差），index 与输入对齐。
    """
    df = factor_df.dropna(subset=["factor_value", industry_col, mktcap_col]).copy()

    if len(df) < 10:
        logger.warning(f"中性化样本不足({len(df)}只)，跳过中性化")
        return factor_df["factor_value"]

    # 构建行业哑变量
    industry_dummies = pd.get_dummies(df[industry_col], prefix="ind", drop_first=True)

    # 构建回归矩阵 X = [行业哑变量, ln(市值)]
    X = pd.concat([industry_dummies, df[[mktcap_col]]], axis=1).astype(float)
    # 添加截距项
    X.insert(0, "const", 1.0)
    y = df["factor_value"].values

    # OLS 回归（使用 numpy 避免额外依赖）
    try:
        XtX_inv = np.linalg.pinv(X.values.T @ X.values)
        beta = XtX_inv @ X.values.T @ y
        residuals = y - X.values @ beta
    except np.linalg.LinAlgError:
        logger.warning("中性化回归求解失败，返回原始因子值")
        return factor_df["factor_value"]

    # 将残差填回原 DataFrame
    result = factor_df["factor_value"].copy()
    result.loc[df.index] = residuals

    n_industries = industry_dummies.shape[1] + 1  # 含 drop 掉的基准行业
    logger.debug(f"中性化完成: {len(df)} 只股票, {n_industries} 个行业")

    return result


# ============================================================
# Z-Score 标准化
# ============================================================

def zscore(series: pd.Series) -> pd.Series:
    """
    Z-Score 标准化。

    z = (x - mean) / std

    Args:
        series: 因子值序列。

    Returns:
        标准化后的序列。
    """
    mean = series.mean()
    std = series.std()

    if std == 0 or pd.isna(std):
        return series * 0.0  # 全部相同则返回0

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
) -> pd.DataFrame:
    """
    因子处理完整流水线：去极值 → 中性化 → 标准化。

    Args:
        factor_df: DataFrame，必须包含 ts_code 和 factor_value 列。
        industry_df: 行业分类 DataFrame，包含 ts_code 和 industry_name 列。
                     中性化时必需，为 None 则跳过中性化。
        mktcap_df: 市值 DataFrame，包含 ts_code 和 total_mv 列。
                   中性化时必需，为 None 则跳过中性化。
        do_winsorize: 是否去极值。
        do_neutralize: 是否中性化。
        do_zscore: 是否标准化。
        mad_n: MAD 去极值倍数。

    Returns:
        处理后的 DataFrame[ts_code, factor_value]。
    """
    df = factor_df[["ts_code", "factor_value"]].copy()
    initial_count = df["factor_value"].notna().sum()

    # 1. 去极值
    if do_winsorize:
        valid_mask = df["factor_value"].notna()
        df.loc[valid_mask, "factor_value"] = winsorize_mad(
            df.loc[valid_mask, "factor_value"], n=mad_n
        )

    # 2. 中性化
    if do_neutralize and industry_df is not None and mktcap_df is not None:
        # 合并行业和市值信息
        df = df.merge(
            industry_df[["ts_code", "industry_name"]], on="ts_code", how="left"
        )
        df = df.merge(
            mktcap_df[["ts_code", "total_mv"]], on="ts_code", how="left"
        )

        # ln(市值)
        df["total_mv"] = pd.to_numeric(df["total_mv"], errors="coerce")
        df["ln_mktcap"] = np.log(df["total_mv"].fillna(1).clip(lower=1).astype(float))

        df["factor_value"] = neutralize(df)

        # 清理临时列
        df = df[["ts_code", "factor_value"]]

    # 3. 标准化
    if do_zscore:
        valid_mask = df["factor_value"].notna()
        df.loc[valid_mask, "factor_value"] = zscore(
            df.loc[valid_mask, "factor_value"]
        )

    final_count = df["factor_value"].notna().sum()
    logger.debug(f"因子处理完成: {initial_count} -> {final_count} 个有效值")

    return df


# ============================================================
# 批量因子处理
# ============================================================

def process_all_factors(
    factor_dict: dict[str, pd.DataFrame],
    industry_df: pd.DataFrame = None,
    mktcap_df: pd.DataFrame = None,
) -> dict[str, pd.DataFrame]:
    """
    批量处理多个因子。

    Args:
        factor_dict: {因子名: DataFrame[ts_code, factor_value]} 字典。
        industry_df: 行业分类数据。
        mktcap_df: 市值数据。

    Returns:
        {因子名: 处理后的 DataFrame} 字典。
    """
    result = {}
    for name, df in factor_dict.items():
        logger.info(f"处理因子: {name}")
        result[name] = process_factor(df, industry_df, mktcap_df)
    return result
