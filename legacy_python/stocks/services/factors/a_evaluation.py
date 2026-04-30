"""
因子有效性评估框架（A股 + 美股通用）

五项评估：
    1. Rank IC / ICIR — 截面 Spearman 相关，衡量因子预测力和稳定性
    2. 分层回测（Quantile Portfolio） — 按因子值分 5 组，多空收益差
    3. IC Decay — IC 随持有期衰减（1/5/10/20/60 天），衡量信号持续性
    4. 换手率分析 — 因子 Top/Bottom 组的周转率
    5. 因子相关性矩阵 — 截面 rank 相关，检测冗余

用法：
    from stocks.services.factors.a_evaluation import FactorEvaluator
    evaluator = FactorEvaluator(db, market="us")
    report = evaluator.run_all(start="2020-01-01", end="2025-12-31")
"""

import logging
from typing import Optional

import numpy as np
import pandas as pd
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

from services.config import LOG_LEVEL, PROJECT_ROOT

logger = logging.getLogger(__name__)
logger.setLevel(LOG_LEVEL)

plt.rcParams["font.sans-serif"] = ["SimHei", "Arial Unicode MS", "DejaVu Sans"]
plt.rcParams["axes.unicode_minus"] = False


# ============================================================
# 核心计算函数（市场无关）
# ============================================================

def calc_ic_series(
    factor_data: pd.DataFrame,
    return_data: pd.DataFrame,
    id_col: str = "ticker",
    method: str = "spearman",
) -> pd.DataFrame:
    """
    计算因子 IC 时间序列。

    Args:
        factor_data: DataFrame[date, {id_col}, factor_value]
        return_data: DataFrame[date, {id_col}, forward_return]
        id_col: 股票 ID 列名（ticker 或 ts_code）
        method: spearman（默认）或 pearson

    Returns:
        DataFrame[date, ic]
    """
    merged = factor_data.merge(return_data, on=["date", id_col], how="inner")
    if merged.empty:
        logger.debug("calc_ic_series: merged 为空")
        return pd.DataFrame(columns=["date", "ic"])

    ic_list = []
    for dt, group in merged.groupby("date"):
        valid = group.dropna(subset=["factor_value", "forward_return"])
        if len(valid) < 30:
            logger.debug(f"calc_ic_series: 日期 {dt} 有效样本不足({len(valid)}<30)，跳过")
            continue
        if valid["factor_value"].nunique() <= 1:
            logger.debug(f"calc_ic_series: 日期 {dt} 因子值为常数，跳过")
            continue
        ic = valid["factor_value"].corr(valid["forward_return"], method=method)
        if not np.isnan(ic):
            ic_list.append({"date": dt, "ic": ic})

    return pd.DataFrame(ic_list)


def calc_ic_summary(ic_series: pd.DataFrame) -> dict:
    """IC 统计摘要：mean, std, ICIR, positive_rate, num_periods。"""
    if ic_series.empty or "ic" not in ic_series.columns:
        logger.debug("calc_ic_summary: IC 序列为空或无 ic 列，返回 NaN")
        return {"ic_mean": np.nan, "ic_std": np.nan, "icir": np.nan,
                "ic_positive_rate": np.nan, "num_periods": 0}
    ic = ic_series["ic"].dropna()
    if len(ic) == 0:
        logger.debug("calc_ic_summary: IC 序列为空，返回 NaN")
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


def calc_quantile_returns(
    factor_data: pd.DataFrame,
    return_data: pd.DataFrame,
    id_col: str = "ticker",
    n_groups: int = 5,
) -> pd.DataFrame:
    """
    分层回测：按因子值分组，计算各组平均收益率。

    Returns:
        DataFrame[date → Q1..Q5]
    """
    merged = factor_data.merge(return_data, on=["date", id_col], how="inner")
    results = []
    for dt, group in merged.groupby("date"):
        valid = group.dropna(subset=["factor_value", "forward_return"])
        if len(valid) < n_groups * 5:
            logger.debug(f"calc_quantile_returns: 日期 {dt} 有效样本不足({len(valid)}<{n_groups*5})，跳过")
            continue
        valid = valid.sort_values("factor_value")
        valid["quantile"] = pd.qcut(
            valid["factor_value"], n_groups, labels=False, duplicates="drop"
        )
        group_ret = valid.groupby("quantile")["forward_return"].mean()
        row = {"date": dt}
        for q in range(n_groups):
            row[f"Q{q + 1}"] = group_ret.get(q, np.nan)
        results.append(row)

    return pd.DataFrame(results).set_index("date") if results else pd.DataFrame()


def calc_ic_decay(
    factor_snapshots: dict,
    price_df: pd.DataFrame,
    id_col: str = "ticker",
    horizons: list[int] = None,
) -> pd.DataFrame:
    """
    IC Decay：计算因子值与不同持有期收益的 IC。

    Args:
        factor_snapshots: {date_str: DataFrame[{id_col}, factor_value]}
        price_df: DataFrame[{id_col}, trade_date, close]，已排序
        horizons: 持有期天数列表，默认 [1, 5, 10, 20, 60]

    Returns:
        DataFrame[horizon, ic_mean, ic_std, icir]
    """
    if horizons is None:
        horizons = [1, 5, 10, 20, 60]

    price_df = price_df.copy()
    price_df["trade_date"] = pd.to_datetime(price_df["trade_date"])
    price_df = price_df.sort_values([id_col, "trade_date"])

    # 预建日期索引：{trade_date: DataFrame}，避免重复扫描全量数据
    date_index = {}
    for dt, grp in price_df.groupby("trade_date"):
        date_index[dt] = grp[[id_col, "close"]].copy()
    sorted_dates = sorted(date_index.keys())

    def _find_px_on_or_before(target_dt, lookback_days=5):
        """从日期索引中找目标日期（或前 lookback_days 天内最近的）的价格"""
        if target_dt in date_index:
            return date_index[target_dt]
        cutoff = target_dt - pd.Timedelta(days=lookback_days)
        for dt in reversed(sorted_dates):
            if cutoff <= dt <= target_dt:
                return date_index[dt]
            if dt < cutoff:
                break
        return None

    def _find_px_last_before(start_dt, end_dt):
        """从日期索引中找 (start_dt, end_dt] 范围内每个 ticker 最后一天的价格"""
        candidates = [dt for dt in sorted_dates if start_dt < dt <= end_dt]
        if not candidates:
            return None
        last_dt = candidates[-1]
        return date_index[last_dt]

    results = []
    for h in horizons:
        ic_list = []
        for date_str, fv in factor_snapshots.items():
            date_dt = pd.to_datetime(date_str)
            # 当日价格（索引查找）
            px_now = _find_px_on_or_before(date_dt)
            if px_now is None:
                logger.debug(f"calc_ic_decay: 日期 {date_str} 无当日价格数据，跳过")
                continue

            # h 天后价格（索引查找）
            target_dt = date_dt + pd.Timedelta(days=int(h * 1.5))
            px_fwd = _find_px_last_before(date_dt, target_dt)
            if px_fwd is None:
                logger.debug(f"calc_ic_decay: 日期 {date_str} horizon={h} 无远期价格数据，跳过")
                continue

            px_now_r = px_now.rename(columns={"close": "px_now"})
            px_fwd_r = px_fwd.rename(columns={"close": "px_fwd"})
            ret = px_now_r.merge(px_fwd_r, on=id_col)
            ret["fwd_ret"] = ret["px_fwd"] / ret["px_now"] - 1

            merged = fv.merge(ret[[id_col, "fwd_ret"]], on=id_col)
            merged = merged.dropna(subset=["factor_value", "fwd_ret"])
            if len(merged) < 30:
                logger.debug(f"calc_ic_decay: 日期 {date_str} horizon={h} 有效样本不足({len(merged)}<30)，跳过")
                continue
            if merged["factor_value"].nunique() <= 1:
                logger.debug(f"calc_ic_decay: 日期 {date_str} horizon={h} 因子值为常数，跳过")
                continue

            ic = merged["factor_value"].rank().corr(merged["fwd_ret"].rank())
            if not np.isnan(ic):
                ic_list.append(ic)

        if ic_list:
            arr = np.array(ic_list)
            results.append({
                "horizon": h,
                "ic_mean": round(arr.mean(), 4),
                "ic_std": round(arr.std(), 4),
                "icir": round(arr.mean() / arr.std(), 4) if arr.std() > 0 else np.nan,
            })
        else:
            results.append({"horizon": h, "ic_mean": np.nan, "ic_std": np.nan, "icir": np.nan})

    return pd.DataFrame(results)


def calc_turnover(
    factor_snapshots: dict,
    id_col: str = "ticker",
    top_pct: float = 0.2,
) -> dict:
    """
    换手率分析：相邻两期 Top/Bottom 组的变动比例。

    Args:
        factor_snapshots: {date_str: DataFrame[{id_col}, factor_value]}，按日期排序
        top_pct: Top/Bottom 组占比，默认 20%

    Returns:
        {"top_turnover": float, "bottom_turnover": float, "num_periods": int}
    """
    dates = sorted(factor_snapshots.keys())
    if len(dates) < 2:
        logger.debug(f"calc_turnover: 日期数不足({len(dates)}<2)，无法计算换手率")
        return {"top_turnover": np.nan, "bottom_turnover": np.nan, "num_periods": 0}

    top_turns, bottom_turns = [], []
    prev_top, prev_bottom = None, None

    for date_str in dates:
        fv = factor_snapshots[date_str].dropna(subset=["factor_value"])
        if len(fv) < 10:
            logger.debug(f"calc_turnover: 日期 {date_str} 有效样本不足({len(fv)}<10)，跳过")
            continue
        n = max(int(len(fv) * top_pct), 1)
        sorted_fv = fv.sort_values("factor_value")
        bottom_set = set(sorted_fv.head(n)[id_col])
        top_set = set(sorted_fv.tail(n)[id_col])

        if prev_top is not None:
            top_overlap = len(top_set & prev_top) / max(len(top_set), 1)
            bottom_overlap = len(bottom_set & prev_bottom) / max(len(bottom_set), 1)
            top_turns.append(1 - top_overlap)
            bottom_turns.append(1 - bottom_overlap)

        prev_top, prev_bottom = top_set, bottom_set

    return {
        "top_turnover": round(np.mean(top_turns), 4) if top_turns else np.nan,
        "bottom_turnover": round(np.mean(bottom_turns), 4) if bottom_turns else np.nan,
        "num_periods": len(top_turns),
    }


def calc_factor_correlation(
    all_factor_snapshots: dict,
    id_col: str = "ticker",
) -> pd.DataFrame:
    """
    因子相关性矩阵：截面 Spearman rank 相关的时间平均。

    Args:
        all_factor_snapshots: {factor_name: {date_str: DataFrame[{id_col}, factor_value]}}

    Returns:
        相关性矩阵 DataFrame (N×N)
    """
    factor_names = list(all_factor_snapshots.keys())
    if len(factor_names) < 2:
        logger.debug(f"calc_factor_correlation: 因子数不足({len(factor_names)}<2)，返回空")
        return pd.DataFrame()

    # 收集所有日期
    all_dates = set()
    for snaps in all_factor_snapshots.values():
        all_dates |= set(snaps.keys())
    dates = sorted(all_dates)

    corr_accum = np.zeros((len(factor_names), len(factor_names)))
    n_dates = 0

    for date_str in dates:
        # 构建该日期的因子值矩阵
        frames = {}
        for fname in factor_names:
            snaps = all_factor_snapshots[fname]
            if date_str not in snaps:
                logger.debug(f"calc_factor_correlation: 因子 {fname} 在日期 {date_str} 无快照，跳过")
                continue
            fv = snaps[date_str][[id_col, "factor_value"]].dropna()
            if not fv.empty:
                frames[fname] = fv.set_index(id_col)["factor_value"]

        if len(frames) < 2:
            logger.debug(f"calc_factor_correlation: 日期 {date_str} 可用因子不足({len(frames)}<2)，跳过")
            continue

        combined = pd.DataFrame(frames)
        if len(combined.dropna()) < 30:
            logger.debug(f"calc_factor_correlation: 日期 {date_str} 有效样本不足({len(combined.dropna())}<30)，跳过")
            continue

        # 过滤掉常数列（宏观因子等），避免 ConstantInputWarning
        non_const_cols = [c for c in combined.columns if combined[c].nunique() > 1]
        if len(non_const_cols) < 2:
            logger.debug(f"calc_factor_correlation: 日期 {date_str} 非常数因子不足2个，跳过")
            continue
        combined = combined[non_const_cols]

        ranked = combined.rank()
        corr = ranked.corr(method="spearman")

        # 只累加有值的位置
        for i, fi in enumerate(factor_names):
            for j, fj in enumerate(factor_names):
                if fi in corr.columns and fj in corr.columns:
                    val = corr.loc[fi, fj]
                    if not np.isnan(val):
                        corr_accum[i, j] += val

        n_dates += 1

    if n_dates == 0:
        logger.debug("calc_factor_correlation: 无有效日期可计算相关性，返回空")
        return pd.DataFrame()

    corr_avg = corr_accum / n_dates
    return pd.DataFrame(corr_avg, index=factor_names, columns=factor_names).round(4)


# ============================================================
# 可视化
# ============================================================

def plot_ic_series(ic_series: pd.DataFrame, factor_name: str, save_dir: str = None):
    """IC 柱状图 + 累计 IC 曲线。"""
    if ic_series.empty:
        return
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(14, 8), sharex=True)
    dates = pd.to_datetime(ic_series["date"])
    ic = ic_series["ic"]

    colors = ["#e74c3c" if v < 0 else "#2ecc71" for v in ic]
    ax1.bar(dates, ic, color=colors, alpha=0.7, width=20)
    ax1.axhline(y=0, color="black", linewidth=0.5)
    ax1.axhline(y=ic.mean(), color="blue", linewidth=1, linestyle="--",
                label=f"IC Mean={ic.mean():.4f}")
    ax1.set_ylabel("IC")
    ax1.set_title(f"{factor_name} — IC Time Series")
    ax1.legend()

    cum_ic = ic.cumsum()
    ax2.plot(dates, cum_ic, color="#3498db", linewidth=1.5)
    ax2.fill_between(dates, 0, cum_ic, alpha=0.1, color="#3498db")
    ax2.axhline(y=0, color="black", linewidth=0.5)
    ax2.set_ylabel("Cumulative IC")
    ax2.set_xlabel("Date")

    plt.tight_layout()
    output_dir = save_dir or str(PROJECT_ROOT / "output")
    import os; os.makedirs(output_dir, exist_ok=True)
    path = f"{output_dir}/ic_{factor_name}.png"
    plt.savefig(path, dpi=150, bbox_inches="tight")
    plt.close()
    logger.info(f"IC 图表已保存: {path}")


def plot_quantile_returns(quantile_returns: pd.DataFrame, factor_name: str, save_dir: str = None):
    """分层净值曲线。"""
    if quantile_returns.empty:
        return
    cum_ret = (1 + quantile_returns).cumprod()

    fig, ax = plt.subplots(figsize=(14, 6))
    colors = ["#e74c3c", "#e67e22", "#f1c40f", "#2ecc71", "#3498db"]
    for i, col in enumerate(cum_ret.columns):
        color = colors[i] if i < len(colors) else None
        ax.plot(cum_ret.index, cum_ret[col], label=col, linewidth=1.5, color=color)

    if "Q1" in cum_ret.columns and f"Q{len(cum_ret.columns)}" in cum_ret.columns:
        ls_col = f"Q{len(cum_ret.columns)}"
        ls_return = quantile_returns[ls_col] - quantile_returns["Q1"]
        ls_cum = (1 + ls_return).cumprod()
        ax.plot(ls_cum.index, ls_cum, label="Long-Short", linewidth=2,
                color="black", linestyle="--")

    ax.set_title(f"{factor_name} — Quantile Returns")
    ax.set_ylabel("Cumulative Return")
    ax.legend(loc="upper left")
    ax.grid(True, alpha=0.3)
    plt.tight_layout()

    output_dir = save_dir or str(PROJECT_ROOT / "output")
    import os; os.makedirs(output_dir, exist_ok=True)
    path = f"{output_dir}/quantile_{factor_name}.png"
    plt.savefig(path, dpi=150, bbox_inches="tight")
    plt.close()
    logger.info(f"分层图表已保存: {path}")


def plot_ic_decay(decay_df: pd.DataFrame, factor_name: str, save_dir: str = None):
    """IC Decay 柱状图。"""
    if decay_df.empty:
        return
    fig, ax = plt.subplots(figsize=(10, 5))
    ax.bar(decay_df["horizon"].astype(str), decay_df["ic_mean"], color="#3498db", alpha=0.8)
    ax.set_xlabel("Holding Period (days)")
    ax.set_ylabel("IC Mean")
    ax.set_title(f"{factor_name} — IC Decay")
    ax.axhline(y=0, color="black", linewidth=0.5)
    ax.grid(True, alpha=0.3)
    plt.tight_layout()

    output_dir = save_dir or str(PROJECT_ROOT / "output")
    import os; os.makedirs(output_dir, exist_ok=True)
    path = f"{output_dir}/ic_decay_{factor_name}.png"
    plt.savefig(path, dpi=150, bbox_inches="tight")
    plt.close()


def plot_correlation_matrix(corr_df: pd.DataFrame, market: str, save_dir: str = None):
    """因子相关性热力图。"""
    if corr_df.empty:
        return
    fig, ax = plt.subplots(figsize=(max(12, len(corr_df) * 0.6), max(10, len(corr_df) * 0.5)))
    im = ax.imshow(corr_df.values, cmap="RdBu_r", vmin=-1, vmax=1, aspect="auto")
    ax.set_xticks(range(len(corr_df.columns)))
    ax.set_yticks(range(len(corr_df.index)))
    ax.set_xticklabels(corr_df.columns, rotation=45, ha="right", fontsize=8)
    ax.set_yticklabels(corr_df.index, fontsize=8)
    # 标注数值
    for i in range(len(corr_df)):
        for j in range(len(corr_df)):
            val = corr_df.iloc[i, j]
            if not np.isnan(val):
                ax.text(j, i, f"{val:.2f}", ha="center", va="center", fontsize=6,
                        color="white" if abs(val) > 0.5 else "black")
    fig.colorbar(im, ax=ax, shrink=0.8)
    ax.set_title(f"{market.upper()} Factor Correlation Matrix")
    plt.tight_layout()

    output_dir = save_dir or str(PROJECT_ROOT / "output")
    import os; os.makedirs(output_dir, exist_ok=True)
    path = f"{output_dir}/factor_corr_{market}.png"
    plt.savefig(path, dpi=150, bbox_inches="tight")
    plt.close()
    logger.info(f"相关性矩阵已保存: {path}")


# ============================================================
# FactorEvaluator — 统一入口
# ============================================================

class FactorEvaluator:
    """
    因子评估器，支持 A 股和美股。

    用法：
        evaluator = FactorEvaluator(db, market="us")
        report = evaluator.run_all(start="2020-01-01", end="2025-12-31")
    """

    def __init__(self, db, market: str = "us"):
        self.db = db
        self.market = market
        if market == "us":
            self.id_col = "ticker"
        else:
            self.id_col = "ts_code"

    def _get_factor_map(self) -> dict:
        """返回 {factor_name: factor_class}。"""
        if self.market == "us":
            from stocks.services.factors import (
                value, quality, growth, momentum, technical,
                analyst, accruals, polymarket, earnings, insider, quiver,
                alphavantage,
            )
            return {
                "EP": value.EP, "BP": value.BP, "DIV_YIELD": value.DivYield,
                "BUYBACK_YIELD": accruals.BuybackYield,
                "ROE_TTM": quality.RoeTTM, "GROSS_MARGIN": quality.GrossMargin,
                "PROFIT_STB": quality.ProfitStability, "MARGIN_TREND": quality.MarginTrend,
                "ACCRUALS": accruals.Accruals,
                "NET_PROFIT_YOY": growth.NetProfitYoY, "REVENUE_YOY": growth.RevenueYoY,
                "NET_PROFIT_CAGR_3Y": growth.NetProfitCAGR3Y,
                "MOM_1M": momentum.Mom1M, "MOM_3M": momentum.Mom3M,
                "MOM_12M": momentum.Mom12M, "REV_5D": momentum.Rev5D,
                "RESIDUAL_MOM": momentum.ResidualMom,
                "TURN_20D": technical.Turn20D, "VOL_20D": technical.Vol20D,
                "PRICE_DEV_60D": technical.PriceDev60D, "IVOL": technical.Ivol,
                "SIZE": technical.Size, "VOL_PRICE_DIV": technical.VolPriceDiv,
                # 宏观因子是 Regime 择时信号（截面常数），不适合截面 IC 评估，已移除
                "US_ANALYST_RATING": analyst.USAnalystRating,
                "US_ANALYST_COVERAGE": analyst.USAnalystCoverage,
                "EARNINGS_SURPRISE": earnings.EarningsSurprise,
                "EPS_REVISION": earnings.EpsRevision,
                "INSIDER_NET_BUY": insider.InsiderNetBuy,
                "LOBBY_INTENSITY": quiver.LobbyIntensity,
                "GOV_CONTRACT": quiver.GovContract,
                "WSB_SENTIMENT": quiver.WsbSentiment,
                "NEWS_SENTIMENT": alphavantage.NewsSentiment,
                "IV_SKEW": alphavantage.IvSkew,
                "PUT_CALL_RATIO": alphavantage.PutCallRatio,
            }
        else:
            from stocks.services.factors import (
                value, quality, growth, momentum, technical,
                macro, sentiment, commodity, research, dividend,
            )
            return {
                "EP": value.EPFactor, "BP": value.BPFactor,
                "DIV_YIELD": dividend.DividendYieldFactor,
                "ROE_TTM": quality.ROEFactor,
                "GROSS_MARGIN": quality.GrossMarginFactor,
                "PROFIT_STB": quality.ProfitStabilityFactor,
                "MARGIN_TREND": quality.MarginTrendFactor,
                "NET_PROFIT_YOY": growth.NetProfitYOYFactor,
                "REVENUE_YOY": growth.RevenueYOYFactor,
                "NET_PROFIT_CAGR_3Y": growth.NetProfitCAGR3YFactor,
                "MOM_1M": momentum.MOM1MFactor,
                "MOM_3M": momentum.MOM3MFactor,
                "MOM_12M": momentum.MOM12MFactor,
                "REV_5D": momentum.ShortReversalFactor,
                "IND_MOM": technical.IndustryMomentumFactor,
                "RESIDUAL_MOM": momentum.ResidualMomentumFactor,
                "CMDTY_MOM": commodity.CommodityMomentumFactor,
                "TURN_20D": technical.Turnover20DFactor,
                "VOL_20D": technical.VolatilityFactor,
                "PRICE_DEV_60D": technical.PriceDeviationFactor,
                "SIZE": technical.SizeFactor,
                "VOL_PRICE_DIV": technical.VolPriceDivFactor,
                "MACRO_CYCLE": macro.MacroCycleFactor,
                "MACRO_LIQD": macro.MacroLiquidityFactor,
                "MACRO_INFL": macro.MacroInflationFactor,
                "MACRO_EXTR": macro.MacroExternalFactor,
                "POLICY_SENT": sentiment.SentimentPolicyFactor,
                "POLICY_INTENSITY": sentiment.SentimentIntensityFactor,
                "ANALYST_RATING": research.AnalystRatingFactor,
                "ANALYST_COVERAGE": research.AnalystCoverageFactor,
            }

    def _get_universe(self, date_str: str) -> pd.DataFrame:
        """获取指定日期的股票池。"""
        if self.market == "us":
            from stocks.services.us_cleaner import get_us_clean_universe
            return get_us_clean_universe(date_str)
        else:
            from stocks.services.a_cleaner import get_clean_universe
            return get_clean_universe(self.db, date_str)

    def _preload(self, start: str, end: str):
        """预加载数据（美股 + A 股）。"""
        if self.market == "us":
            from stocks.services.factors.us_base import USFactorBase
            pre_start = (pd.to_datetime(start) - pd.Timedelta(days=400)).strftime("%Y-%m-%d")
            pre_end = (pd.to_datetime(end) + pd.Timedelta(days=90)).strftime("%Y-%m-%d")
            USFactorBase.preload_for_backtest(pre_start, pre_end)
            USFactorBase.precompute_rolling_stats()
        else:
            from stocks.services.factors.a_base import FactorBase as CNFactorBase
            pre_start = (pd.to_datetime(start) - pd.Timedelta(days=400)).strftime("%Y-%m-%d")
            pre_end = (pd.to_datetime(end) + pd.Timedelta(days=90)).strftime("%Y-%m-%d")
            CNFactorBase.preload_for_backtest(None, pre_start, pre_end)
            CNFactorBase.precompute_rolling_stats()

    def _get_eval_dates(self, start: str, end: str, freq_months: int = 1) -> list[str]:
        """生成评估日期列表（每月第3个周五）。"""
        dates = pd.bdate_range(start, end, freq="WOM-3FRI")
        if freq_months > 1:
            dates = dates[::freq_months]
        if len(dates) == 0:
            logger.warning(f"_get_eval_dates: {start}~{end} 无有效评估日期")
            return []
        return [d.strftime("%Y-%m-%d") for d in dates]

    def _get_price_df(self, start: str, end: str) -> pd.DataFrame:
        """获取价格数据用于 IC Decay（只取评估区间 ± 90 天）。"""
        if self.market == "us":
            from stocks.services.factors.us_base import USFactorBase
            bulk = USFactorBase._static_cache.get("_bulk_daily")
            if bulk is not None and not bulk.empty:
                start_dt = pd.to_datetime(start) - pd.Timedelta(days=10)
                end_dt = pd.to_datetime(end) + pd.Timedelta(days=90)
                mask = (bulk["trade_date"] >= start_dt) & (bulk["trade_date"] <= end_dt)
                logger.debug(f"_get_price_df: 从预加载数据切片 {start_dt.date()}~{end_dt.date()}")
                return bulk.loc[mask, ["ticker", "trade_date", "close"]].copy()
            df = self.db.query(
                "SELECT ticker, trade_date, COALESCE(adj_close, close) as close "
                "FROM us_daily_price WHERE trade_date >= :s AND trade_date <= :e",
                params={"s": start, "e": end},
            )
        else:
            from stocks.models import ADailyPrice
            s_d = pd.to_datetime(start).date()
            e_d = pd.to_datetime(end).date()
            qs = ADailyPrice.objects.filter(
                trade_date__gte=s_d, trade_date__lte=e_d,
            ).values("ts_code", "trade_date", "close")
            df = pd.DataFrame(list(qs))
            if not df.empty:
                df["trade_date"] = pd.to_datetime(df["trade_date"])
        return df

    def run_all(
        self,
        start: str = "2020-01-01",
        end: str = "2025-12-31",
        freq_months: int = 1,
        factors: list[str] = None,
        plot: bool = True,
        save_dir: str = None,
    ) -> dict:
        """
        运行全部 5 项评估。

        Args:
            start/end: 评估区间
            freq_months: 采样频率（月），默认 1
            factors: 要评估的因子列表，None=全部
            plot: 是否生成图表
            save_dir: 图表保存目录

        Returns:
            {factor_name: {ic_mean, ic_std, icir, ic_positive_rate, quantile_spread,
                           ic_decay, turnover, ...}}
        """
        factor_map = self._get_factor_map()
        if factors:
            factor_map = {k: v for k, v in factor_map.items() if k in factors}

        logger.info(f"因子评估: {self.market.upper()}, {start}~{end}, {len(factor_map)} 个因子")

        # 预加载
        self._preload(start, end)

        # 评估日期
        eval_dates = self._get_eval_dates(start, end, freq_months)
        if len(eval_dates) < 2:
            logger.warning(f"run_all: 评估日期不足2个({len(eval_dates)})，无法计算前瞻收益，返回空报告")
            return {}
        logger.info(f"评估日期: {len(eval_dates)} 个 ({eval_dates[0]} ~ {eval_dates[-1]})")

        # 价格数据（IC Decay 用）
        price_df = self._get_price_df(start, end)

        # 收集所有因子的截面快照
        all_factor_snapshots = {}  # {factor_name: {date: DataFrame}}
        all_forward_returns = {}   # {date: DataFrame[id_col, forward_return]}

        # 计算前瞻收益
        logger.info("计算前瞻收益...")
        for i, date_str in enumerate(eval_dates[:-1]):
            next_date = eval_dates[i + 1]
            date_dt = pd.to_datetime(date_str)
            next_dt = pd.to_datetime(next_date)

            if self.market == "us":
                from stocks.services.factors.us_base import USFactorBase
                bulk = USFactorBase._static_cache.get("_bulk_daily")
                if bulk is None:
                    logger.debug(f"run_all: 日期 {date_str} 无预加载日线数据，跳过")
                    continue
                # 找最近交易日的价格
                mask1 = (bulk["trade_date"] >= date_dt - pd.Timedelta(days=5)) & \
                        (bulk["trade_date"] <= date_dt)
                px1 = bulk[mask1].sort_values("trade_date").groupby("ticker").tail(1)[["ticker", "close"]]
                mask2 = (bulk["trade_date"] >= next_dt - pd.Timedelta(days=5)) & \
                        (bulk["trade_date"] <= next_dt)
                px2 = bulk[mask2].sort_values("trade_date").groupby("ticker").tail(1)[["ticker", "close"]]
            else:
                from stocks.models import ADailyPrice
                d1 = pd.to_datetime(date_str).date()
                d2 = pd.to_datetime(next_date).date()
                px1 = pd.DataFrame(list(
                    ADailyPrice.objects.filter(trade_date=d1).values("ts_code", "close")
                ))
                px2 = pd.DataFrame(list(
                    ADailyPrice.objects.filter(trade_date=d2).values("ts_code", "close")
                ))

            if px1.empty or px2.empty:
                logger.debug(f"run_all: 日期 {date_str} 价格数据不完整，跳过前瞻收益计算")
                continue

            px1.columns = [self.id_col, "px1"]
            px2.columns = [self.id_col, "px2"]
            ret = px1.merge(px2, on=self.id_col)
            ret["forward_return"] = ret["px2"] / ret["px1"] - 1
            all_forward_returns[date_str] = ret[[self.id_col, "forward_return"]]

        # 预缓存所有日期的 universe（避免多线程重复查 DB）
        logger.info("预缓存股票池...")
        universe_cache = {}
        for date_str in eval_dates[:-1]:
            if date_str not in all_forward_returns:
                logger.debug(f"run_all: 日期 {date_str} 无前瞻收益数据，跳过股票池缓存")
                continue
            universe_cache[date_str] = self._get_universe(date_str)

        # 单因子评估函数
        import threading
        _plot_lock = threading.Lock()

        def _eval_single_factor(fname, fcls):
            logger.info(f"评估因子: {fname}")
            factor = fcls(self.db)
            factor_snapshots = {}
            factor_data_list = []
            return_data_list = []

            for date_str in eval_dates[:-1]:
                if date_str not in all_forward_returns:
                    logger.debug(f"_eval_single_factor({fname}): 日期 {date_str} 无前瞻收益，跳过")
                    continue
                universe = universe_cache.get(date_str)
                if universe is None or universe.empty or len(universe) < 30:
                    logger.debug(f"_eval_single_factor({fname}): 日期 {date_str} 股票池为空或不足30只，跳过")
                    continue

                try:
                    fv = factor.compute(date_str, universe)
                except Exception as e:
                    logger.debug(f"{fname} compute 失败 {date_str}: {e}")
                    continue

                if fv.empty or fv["factor_value"].notna().sum() < 30:
                    logger.debug(f"_eval_single_factor({fname}): 日期 {date_str} 因子有效值不足30，跳过")
                    continue

                factor_snapshots[date_str] = fv[[self.id_col, "factor_value"]].copy()

                fv_ic = fv[[self.id_col, "factor_value"]].copy()
                fv_ic["date"] = date_str
                factor_data_list.append(fv_ic)

                ret_ic = all_forward_returns[date_str].copy()
                ret_ic["date"] = date_str
                return_data_list.append(ret_ic)

            if not factor_data_list:
                logger.debug(f"_eval_single_factor({fname}): 无有效因子数据，跳过评估")
                return fname, {"ic_mean": np.nan, "icir": np.nan, "num_periods": 0}, {}

            factor_data = pd.concat(factor_data_list, ignore_index=True)
            return_data = pd.concat(return_data_list, ignore_index=True)

            # 1. IC / ICIR
            ic_series = calc_ic_series(factor_data, return_data, self.id_col)
            ic_summary = calc_ic_summary(ic_series)

            # 2. 分层回测
            quant_ret = calc_quantile_returns(factor_data, return_data, self.id_col)
            spread = np.nan
            if not quant_ret.empty and "Q1" in quant_ret.columns and "Q5" in quant_ret.columns:
                ls = quant_ret["Q5"] - quant_ret["Q1"]
                spread = round(ls.mean() * 12, 4)

            # 3. IC Decay
            decay_df = calc_ic_decay(factor_snapshots, price_df, self.id_col)

            # 4. 换手率
            turnover = calc_turnover(factor_snapshots, self.id_col)

            result = {
                **ic_summary,
                "quantile_spread_annual": spread,
                "top_turnover": turnover["top_turnover"],
                "bottom_turnover": turnover["bottom_turnover"],
                "ic_decay": decay_df.to_dict("records") if not decay_df.empty else [],
            }

            # 图表（matplotlib 非线程安全，加锁）
            if plot:
                with _plot_lock:
                    plot_ic_series(ic_series, fname, save_dir)
                    plot_quantile_returns(quant_ret, fname, save_dir)
                    plot_ic_decay(decay_df, fname, save_dir)

            logger.info(f"因子 {fname} 完成: IC={ic_summary.get('ic_mean', 'N/A')}, ICIR={ic_summary.get('icir', 'N/A')}")
            return fname, result, factor_snapshots

        # 并行评估所有因子
        report = {}
        from concurrent.futures import ThreadPoolExecutor, as_completed
        n_workers = min(8, len(factor_map))
        logger.info(f"并行评估 {len(factor_map)} 个因子 (workers={n_workers})")

        with ThreadPoolExecutor(max_workers=n_workers) as pool:
            futures = {
                pool.submit(_eval_single_factor, fname, fcls): fname
                for fname, fcls in factor_map.items()
            }
            for future in as_completed(futures):
                fname = futures[future]
                try:
                    fname, result, snapshots = future.result()
                    report[fname] = result
                    if snapshots:
                        all_factor_snapshots[fname] = snapshots
                except Exception as e:
                    logger.warning(f"因子 {fname} 评估失败: {e}")
                    report[fname] = {"ic_mean": np.nan, "icir": np.nan, "num_periods": 0}

        # 5. 因子相关性矩阵
        if len(all_factor_snapshots) >= 2:
            corr_matrix = calc_factor_correlation(all_factor_snapshots, self.id_col)
            if plot:
                plot_correlation_matrix(corr_matrix, self.market, save_dir)
            report["_correlation_matrix"] = corr_matrix
        else:
            report["_correlation_matrix"] = pd.DataFrame()

        return report
