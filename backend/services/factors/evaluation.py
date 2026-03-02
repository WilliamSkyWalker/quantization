"""
因子有效性评估

提供因子质量检验的核心工具：
    1. IC（Information Coefficient）：因子值与下期收益率的截面 Rank 相关系数
    2. ICIR（IC Information Ratio）：IC 均值 / IC 标准差
    3. 因子分层回测：按因子值分 5 组，观察各组累计收益分化

所有评估均基于截面数据，IC 使用 Spearman 秩相关。
"""

import logging
from typing import Optional

import numpy as np
import pandas as pd
import matplotlib

matplotlib.use("Agg")  # 非交互式后端
import matplotlib.pyplot as plt

from backend.services.config import LOG_LEVEL, PROJECT_ROOT

logger = logging.getLogger(__name__)
logger.setLevel(LOG_LEVEL)

# 中文字体设置
plt.rcParams["font.sans-serif"] = ["SimHei", "Arial Unicode MS", "DejaVu Sans"]
plt.rcParams["axes.unicode_minus"] = False


# ============================================================
# IC 计算
# ============================================================

def calc_ic_series(
    factor_data: pd.DataFrame,
    return_data: pd.DataFrame,
    method: str = "spearman",
) -> pd.DataFrame:
    """
    计算因子 IC 时间序列。

    对每个截面日期，计算因子值与下期收益率的相关系数。

    Args:
        factor_data: 因子数据，包含 date, ts_code, factor_value 三列。
        return_data: 收益率数据，包含 date, ts_code, forward_return 三列。
                     forward_return 是下期（如下一个月）的收益率。
        method: 相关系数方法，"spearman"（默认）或 "pearson"。

    Returns:
        DataFrame，包含 date, ic 两列。
    """
    merged = factor_data.merge(
        return_data, on=["date", "ts_code"], how="inner"
    )

    if merged.empty:
        return pd.DataFrame(columns=["date", "ic"])

    ic_list = []
    for dt, group in merged.groupby("date"):
        valid = group.dropna(subset=["factor_value", "forward_return"])
        if len(valid) < 10:
            continue

        if method == "spearman":
            ic = valid["factor_value"].corr(valid["forward_return"], method="spearman")
        else:
            ic = valid["factor_value"].corr(valid["forward_return"], method="pearson")

        ic_list.append({"date": dt, "ic": ic})

    return pd.DataFrame(ic_list)


def calc_ic_summary(ic_series: pd.DataFrame) -> dict:
    """
    计算 IC 统计摘要。

    Args:
        ic_series: IC 时间序列，包含 date, ic 两列。

    Returns:
        字典，包含：
            - ic_mean: IC 均值
            - ic_std: IC 标准差
            - icir: IC / std（信息比率）
            - ic_positive_rate: IC 为正的比例
            - num_periods: 总期数
    """
    ic = ic_series["ic"].dropna()

    if len(ic) == 0:
        return {"ic_mean": np.nan, "ic_std": np.nan, "icir": np.nan,
                "ic_positive_rate": np.nan, "num_periods": 0}

    ic_mean = ic.mean()
    ic_std = ic.std()
    icir = ic_mean / ic_std if ic_std > 0 else np.nan

    return {
        "ic_mean": round(ic_mean, 4),
        "ic_std": round(ic_std, 4),
        "icir": round(icir, 4),
        "ic_positive_rate": round((ic > 0).mean(), 4),
        "num_periods": len(ic),
    }


# ============================================================
# 因子分层回测
# ============================================================

def calc_quantile_returns(
    factor_data: pd.DataFrame,
    return_data: pd.DataFrame,
    n_groups: int = 5,
) -> pd.DataFrame:
    """
    因子分层回测：按因子值分组，计算各组平均收益率。

    每期将股票按因子值从小到大排序，等分为 n_groups 组，
    计算每组下期平均收益率。

    Args:
        factor_data: 因子数据，包含 date, ts_code, factor_value。
        return_data: 收益率数据，包含 date, ts_code, forward_return。
        n_groups: 分组数量，默认 5（五分位）。

    Returns:
        DataFrame，index=date, columns=["Q1", "Q2", ..., "QN"]，
        每个值为该组该期的平均收益率。
    """
    merged = factor_data.merge(
        return_data, on=["date", "ts_code"], how="inner"
    )

    results = []
    for dt, group in merged.groupby("date"):
        valid = group.dropna(subset=["factor_value", "forward_return"])
        if len(valid) < n_groups * 2:
            continue

        # 按因子值排序分组
        valid = valid.sort_values("factor_value")
        valid["quantile"] = pd.qcut(
            valid["factor_value"], n_groups, labels=False, duplicates="drop"
        )

        group_ret = valid.groupby("quantile")["forward_return"].mean()

        row = {"date": dt}
        for q in range(n_groups):
            label = f"Q{q + 1}"
            row[label] = group_ret.get(q, np.nan)
        results.append(row)

    return pd.DataFrame(results).set_index("date")


def calc_cumulative_quantile_returns(quantile_returns: pd.DataFrame) -> pd.DataFrame:
    """
    将分层收益率转为累计净值。

    Args:
        quantile_returns: 分层收益率 DataFrame（从 calc_quantile_returns 输出）。

    Returns:
        各组累计净值 DataFrame。
    """
    return (1 + quantile_returns).cumprod()


# ============================================================
# 可视化
# ============================================================

def plot_ic_series(
    ic_series: pd.DataFrame,
    factor_name: str = "Factor",
    save_path: Optional[str] = None,
):
    """
    绘制 IC 时间序列柱状图 + 累计 IC 曲线。

    Args:
        ic_series: IC 时间序列。
        factor_name: 因子名称（用于标题）。
        save_path: 保存路径（可选，为 None 则保存到默认位置）。
    """
    if ic_series.empty:
        logger.warning("IC 序列为空，跳过绘图")
        return

    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(14, 8), sharex=True)

    dates = pd.to_datetime(ic_series["date"])
    ic = ic_series["ic"]

    # IC 柱状图
    colors = ["#e74c3c" if v < 0 else "#2ecc71" for v in ic]
    ax1.bar(dates, ic, color=colors, alpha=0.7, width=20)
    ax1.axhline(y=0, color="black", linewidth=0.5)
    ax1.axhline(y=ic.mean(), color="blue", linewidth=1, linestyle="--",
                label=f"IC Mean={ic.mean():.4f}")
    ax1.set_ylabel("IC")
    ax1.set_title(f"{factor_name} - IC Time Series")
    ax1.legend()

    # 累计IC
    cum_ic = ic.cumsum()
    ax2.plot(dates, cum_ic, color="#3498db", linewidth=1.5)
    ax2.fill_between(dates, 0, cum_ic, alpha=0.1, color="#3498db")
    ax2.axhline(y=0, color="black", linewidth=0.5)
    ax2.set_ylabel("Cumulative IC")
    ax2.set_xlabel("Date")

    plt.tight_layout()

    if save_path is None:
        output_dir = PROJECT_ROOT / "output"
        output_dir.mkdir(exist_ok=True)
        save_path = str(output_dir / f"ic_{factor_name}.png")

    plt.savefig(save_path, dpi=150, bbox_inches="tight")
    plt.close()
    logger.info(f"IC 图表已保存: {save_path}")


def plot_quantile_returns(
    quantile_returns: pd.DataFrame,
    factor_name: str = "Factor",
    save_path: Optional[str] = None,
):
    """
    绘制因子分层净值曲线。

    Args:
        quantile_returns: 分层收益率 DataFrame。
        factor_name: 因子名称。
        save_path: 保存路径。
    """
    if quantile_returns.empty:
        logger.warning("分层收益为空，跳过绘图")
        return

    cum_ret = calc_cumulative_quantile_returns(quantile_returns)

    fig, ax = plt.subplots(figsize=(14, 6))

    colors = ["#e74c3c", "#e67e22", "#f1c40f", "#2ecc71", "#3498db"]
    for i, col in enumerate(cum_ret.columns):
        color = colors[i] if i < len(colors) else None
        ax.plot(cum_ret.index, cum_ret[col], label=col, linewidth=1.5, color=color)

    # 画多空组合（Q5 - Q1）
    if "Q1" in cum_ret.columns and f"Q{len(cum_ret.columns)}" in cum_ret.columns:
        ls_col = f"Q{len(cum_ret.columns)}"
        ls_return = quantile_returns[ls_col] - quantile_returns["Q1"]
        ls_cum = (1 + ls_return).cumprod()
        ax.plot(ls_cum.index, ls_cum, label="Long-Short", linewidth=2,
                color="black", linestyle="--")

    ax.set_title(f"{factor_name} - Quantile Returns")
    ax.set_ylabel("Cumulative Return")
    ax.set_xlabel("Date")
    ax.legend(loc="upper left")
    ax.grid(True, alpha=0.3)

    plt.tight_layout()

    if save_path is None:
        output_dir = PROJECT_ROOT / "output"
        output_dir.mkdir(exist_ok=True)
        save_path = str(output_dir / f"quantile_{factor_name}.png")

    plt.savefig(save_path, dpi=150, bbox_inches="tight")
    plt.close()
    logger.info(f"分层回测图表已保存: {save_path}")


# ============================================================
# 综合评估报告
# ============================================================

def evaluate_factor(
    factor_name: str,
    factor_data: pd.DataFrame,
    return_data: pd.DataFrame,
    n_groups: int = 5,
    plot: bool = True,
) -> dict:
    """
    综合评估单个因子的有效性。

    执行完整的因子评估流程：IC → ICIR → 分层回测 → 可视化。

    Args:
        factor_name: 因子名称。
        factor_data: 因子数据，包含 date, ts_code, factor_value。
        return_data: 收益率数据，包含 date, ts_code, forward_return。
        n_groups: 分层组数。
        plot: 是否生成图表。

    Returns:
        评估结果字典，包含 IC 统计和各组收益。
    """
    logger.info(f"=== 评估因子: {factor_name} ===")

    # IC 分析
    ic_series = calc_ic_series(factor_data, return_data)
    ic_summary = calc_ic_summary(ic_series)

    logger.info(
        f"IC: mean={ic_summary['ic_mean']}, std={ic_summary['ic_std']}, "
        f"ICIR={ic_summary['icir']}, positive_rate={ic_summary['ic_positive_rate']}"
    )

    # 分层回测
    quantile_returns = calc_quantile_returns(factor_data, return_data, n_groups)

    # 各组年化收益
    group_annual_returns = {}
    if not quantile_returns.empty:
        for col in quantile_returns.columns:
            cum = (1 + quantile_returns[col]).prod()
            n_periods = len(quantile_returns)
            annual = cum ** (12 / max(n_periods, 1)) - 1  # 假设月频
            group_annual_returns[col] = round(annual, 4)

    # 可视化
    if plot:
        plot_ic_series(ic_series, factor_name)
        plot_quantile_returns(quantile_returns, factor_name)

    result = {
        "factor_name": factor_name,
        **ic_summary,
        "group_annual_returns": group_annual_returns,
    }

    return result
