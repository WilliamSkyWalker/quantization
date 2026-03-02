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

from backend.services.config import LOG_LEVEL

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
    mode: str = "full",
    nonlinear_size: bool = False,
) -> pd.Series:
    """
    行业市值中性化。

    对因子值做截面回归取残差 ε 作为中性化后的因子值。

    mode:
        "full"      — 行业哑变量 + ln(mktcap) 回归（原有行为）
        "size_only" — 仅对 ln(mktcap) 回归，保留行业 Alpha
        "none"      — 跳过中性化，直接返回原始因子值

    nonlinear_size:
        True 时在 X 矩阵追加 ln_mktcap² 列，捕捉市值与因子的非线性关系。

    Args:
        factor_df: DataFrame，必须包含 factor_value 列；
                   mode="full" 时还需 industry_col 和 mktcap_col；
                   mode="size_only" 时仅需 mktcap_col。
        industry_col: 行业列名。
        mktcap_col: ln(市值) 列名。
        mode: 中性化模式，"full" / "size_only" / "none"。
        nonlinear_size: 是否添加 ln_mktcap² 非线性项。

    Returns:
        中性化后的因子值 Series（残差），index 与输入对齐。
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

    # 构建回归矩阵 X
    if mode == "full":
        # 行业哑变量 + ln(市值)
        industry_dummies = pd.get_dummies(df[industry_col], prefix="ind", drop_first=True)
        X = pd.concat([industry_dummies, df[[mktcap_col]]], axis=1).astype(float)
    else:
        # size_only: 仅 ln(市值)
        X = df[[mktcap_col]].astype(float).copy()

    # 可选：非线性市值项
    if nonlinear_size:
        X["ln_mktcap_sq"] = X[mktcap_col] ** 2

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

    if mode == "full":
        n_industries = industry_dummies.shape[1] + 1
        logger.debug(f"中性化完成(full): {len(df)} 只股票, {n_industries} 个行业")
    else:
        logger.debug(f"中性化完成(size_only): {len(df)} 只股票, nonlinear={nonlinear_size}")

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
    neutralize_mode: str = "full",
    nonlinear_size: bool = False,
) -> pd.DataFrame:
    """
    因子处理完整流水线：去极值 → 中性化 → 标准化。

    Args:
        factor_df: DataFrame，必须包含 ts_code 和 factor_value 列。
        industry_df: 行业分类 DataFrame，包含 ts_code 和 industry_name 列。
                     中性化 mode="full" 时必需。
        mktcap_df: 市值 DataFrame，包含 ts_code 和 total_mv 列。
                   中性化时必需（mode="full" 或 "size_only"）。
        do_winsorize: 是否去极值。
        do_neutralize: 是否中性化。
        do_zscore: 是否标准化。
        mad_n: MAD 去极值倍数。
        neutralize_mode: 中性化模式 "full" / "size_only" / "none"。
        nonlinear_size: 是否添加 ln_mktcap² 非线性项。

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
    effective_mode = neutralize_mode if do_neutralize else "none"
    if effective_mode != "none" and mktcap_df is not None:
        # size_only 和 full 都需要市值
        df = df.merge(
            mktcap_df[["ts_code", "total_mv"]], on="ts_code", how="left"
        )
        df["total_mv"] = pd.to_numeric(df["total_mv"], errors="coerce")
        df["ln_mktcap"] = np.log(df["total_mv"].fillna(1).clip(lower=1).astype(float))

        # full 模式还需要行业信息
        if effective_mode == "full" and industry_df is not None:
            df = df.merge(
                industry_df[["ts_code", "industry_name"]], on="ts_code", how="left"
            )
        elif effective_mode == "full" and industry_df is None:
            # full 模式但无行业数据，降级为 size_only
            effective_mode = "size_only"
            logger.debug("full 模式缺少行业数据，降级为 size_only")

        df["factor_value"] = neutralize(
            df, mode=effective_mode, nonlinear_size=nonlinear_size
        )

        # 清理临时列
        df = df[["ts_code", "factor_value"]]

    # 3. 标准化
    if do_zscore:
        valid_mask = df["factor_value"].notna()
        df.loc[valid_mask, "factor_value"] = zscore(
            df.loc[valid_mask, "factor_value"]
        )
        # 4. Clip Z-score 到 ±3，防止中性化后残差极端值主导得分
        df["factor_value"] = df["factor_value"].clip(lower=-3.0, upper=3.0)

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
