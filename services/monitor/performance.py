"""
绩效分析模块

提供策略运行后的绩效追踪和归因分析：
    1. 每日绩效指标计算（对比沪深300基准）
    2. 超额收益分析
    3. 行业归因分析
    4. 滚动指标计算（滚动夏普、滚动IC等）
"""

import logging
from typing import Optional

import numpy as np
import pandas as pd

from services.config import LOG_LEVEL
from services.data.database import DatabaseManager

logger = logging.getLogger(__name__)
logger.setLevel(LOG_LEVEL)


class PerformanceAnalyzer:
    """
    绩效分析器。

    用法:
        analyzer = PerformanceAnalyzer(db)
        metrics = analyzer.calc_metrics(nav_series)
        rolling = analyzer.rolling_metrics(nav_series, window=60)
        attribution = analyzer.industry_attribution(holdings_df, date)
    """

    def __init__(self, db: DatabaseManager, rf_rate: float = 0.02):
        """
        Args:
            db: DatabaseManager 实例。
            rf_rate: 无风险年化利率，默认 2%。
        """
        self.db = db
        self.rf_rate = rf_rate
        self.rf_daily = (1 + rf_rate) ** (1 / 252) - 1

    # ----------------------------------------------------------
    # 核心绩效指标
    # ----------------------------------------------------------

    def calc_metrics(
        self,
        nav: pd.Series,
        benchmark_nav: Optional[pd.Series] = None,
    ) -> dict:
        """
        计算完整绩效指标。

        Args:
            nav: 策略净值 Series（DatetimeIndex）。
            benchmark_nav: 基准净值 Series（可选）。

        Returns:
            绩效指标字典。
        """
        daily_ret = nav.pct_change().dropna()
        n_days = len(daily_ret)
        n_years = n_days / 252

        total_return = nav.iloc[-1] / nav.iloc[0] - 1
        annual_return = (1 + total_return) ** (1 / max(n_years, 0.01)) - 1
        annual_vol = daily_ret.std() * np.sqrt(252)
        sharpe = (annual_return - self.rf_rate) / annual_vol if annual_vol > 0 else 0

        # 最大回撤
        cummax = nav.cummax()
        drawdown = (nav - cummax) / cummax
        max_drawdown = drawdown.min()
        max_dd_start = cummax[:drawdown.idxmin()].idxmax() if pd.notna(drawdown.idxmin()) else None
        max_dd_end = drawdown.idxmin()

        # 回撤恢复天数
        dd_recovery = None
        if max_dd_end is not None:
            recovery_nav = nav[max_dd_end:]
            recovered = recovery_nav[recovery_nav >= cummax.loc[max_dd_end]]
            if len(recovered) > 0:
                dd_recovery = (recovered.index[0] - max_dd_end).days

        calmar = annual_return / abs(max_drawdown) if max_drawdown != 0 else np.nan
        sortino = self._calc_sortino(daily_ret, annual_return)
        win_rate = (daily_ret > 0).mean()

        metrics = {
            "总收益率": total_return,
            "年化收益率": annual_return,
            "年化波动率": annual_vol,
            "夏普比率": sharpe,
            "Sortino比率": sortino,
            "最大回撤": max_drawdown,
            "最大回撤起始": str(max_dd_start) if max_dd_start else "",
            "最大回撤结束": str(max_dd_end) if max_dd_end else "",
            "回撤恢复天数": dd_recovery,
            "Calmar比率": calmar,
            "日胜率": win_rate,
            "交易天数": n_days,
        }

        # 基准对比
        if benchmark_nav is not None and not benchmark_nav.empty:
            common = nav.index.intersection(benchmark_nav.index)
            if len(common) > 10:
                bm = benchmark_nav.loc[common]
                bm_ret = bm.pct_change().dropna()
                strat_ret = nav.loc[common].pct_change().dropna()

                bm_total = bm.iloc[-1] / bm.iloc[0] - 1
                bm_annual = (1 + bm_total) ** (1 / max(n_years, 0.01)) - 1

                # 超额收益
                excess_ret = strat_ret - bm_ret.reindex(strat_ret.index, fill_value=0)
                excess_annual = annual_return - bm_annual
                tracking_error = excess_ret.std() * np.sqrt(252)
                info_ratio = excess_annual / tracking_error if tracking_error > 0 else 0

                metrics.update({
                    "基准年化收益率": bm_annual,
                    "超额年化收益率": excess_annual,
                    "跟踪误差": tracking_error,
                    "信息比率": info_ratio,
                })

        return metrics

    def _calc_sortino(self, daily_ret: pd.Series, annual_return: float) -> float:
        """计算 Sortino 比率（仅考虑下行波动）。"""
        downside = daily_ret[daily_ret < 0]
        if len(downside) == 0:
            return np.nan
        downside_vol = downside.std() * np.sqrt(252)
        if downside_vol == 0:
            return np.nan
        return (annual_return - self.rf_rate) / downside_vol

    # ----------------------------------------------------------
    # 滚动指标
    # ----------------------------------------------------------

    def rolling_metrics(
        self,
        nav: pd.Series,
        window: int = 60,
    ) -> pd.DataFrame:
        """
        计算滚动绩效指标。

        Args:
            nav: 策略净值。
            window: 滚动窗口天数。

        Returns:
            DataFrame，包含滚动夏普、滚动波动率、滚动回撤等。
        """
        daily_ret = nav.pct_change().dropna()

        rolling_vol = daily_ret.rolling(window).std() * np.sqrt(252)
        rolling_mean = daily_ret.rolling(window).mean() * 252
        rolling_sharpe = (rolling_mean - self.rf_rate) / rolling_vol

        # 滚动最大回撤
        rolling_dd = pd.Series(index=nav.index, dtype=float)
        for i in range(window, len(nav)):
            window_nav = nav.iloc[i - window:i + 1]
            peak = window_nav.cummax()
            dd = ((window_nav - peak) / peak).min()
            rolling_dd.iloc[i] = dd

        result = pd.DataFrame({
            "rolling_return": rolling_mean,
            "rolling_volatility": rolling_vol,
            "rolling_sharpe": rolling_sharpe,
            "rolling_max_drawdown": rolling_dd,
        })

        return result

    # ----------------------------------------------------------
    # 行业归因分析
    # ----------------------------------------------------------

    def industry_attribution(
        self,
        holdings: pd.DataFrame,
        start_date: str,
        end_date: str,
    ) -> pd.DataFrame:
        """
        行业归因分析：分析各行业对组合收益的贡献。

        Args:
            holdings: 持仓 DataFrame[ts_code, weight]。
            start_date: 区间起始日。
            end_date: 区间结束日。

        Returns:
            行业归因 DataFrame[industry, weight, return, contribution]。
        """
        try:
            industry_map = self.db.get_industry_map()
        except Exception:
            return pd.DataFrame()

        if industry_map.empty:
            return pd.DataFrame()

        df = holdings.merge(industry_map, on="ts_code", how="left")
        df["industry_name"] = df["industry_name"].fillna("未知")

        # 获取区间收益率
        codes = df["ts_code"].tolist()
        codes_str = "','".join(codes)
        df_price = self.db.query(
            f"SELECT ts_code, trade_date, close FROM daily_price "
            f"WHERE ts_code IN ('{codes_str}') "
            f"AND trade_date >= '{start_date}' "
            f"AND trade_date <= '{end_date}' "
            f"ORDER BY ts_code, trade_date"
        )

        if df_price.empty:
            return pd.DataFrame()

        # 计算区间收益率
        stock_returns = {}
        for code, group in df_price.groupby("ts_code"):
            if len(group) >= 2:
                ret = group["close"].iloc[-1] / group["close"].iloc[0] - 1
                stock_returns[code] = ret

        df["stock_return"] = df["ts_code"].map(stock_returns)
        df["contribution"] = df["weight"] * df["stock_return"]

        # 按行业聚合
        result = (
            df.groupby("industry_name")
            .agg(
                weight=("weight", "sum"),
                avg_return=("stock_return", "mean"),
                contribution=("contribution", "sum"),
                n_stocks=("ts_code", "count"),
            )
            .reset_index()
            .sort_values("contribution", ascending=False)
        )

        return result

    # ----------------------------------------------------------
    # 月度绩效表
    # ----------------------------------------------------------

    @staticmethod
    def monthly_returns(nav: pd.Series) -> pd.DataFrame:
        """
        生成月度收益率表。

        Args:
            nav: 策略净值。

        Returns:
            月度收益率 DataFrame（行=年份，列=月份）。
        """
        daily_ret = nav.pct_change().dropna()
        daily_ret.index = pd.to_datetime(daily_ret.index)

        monthly_ret = daily_ret.resample("ME").apply(lambda x: (1 + x).prod() - 1)
        monthly_ret.index = monthly_ret.index.to_period("M")

        # 转为年x月矩阵
        table = monthly_ret.to_frame("return")
        table["year"] = table.index.year
        table["month"] = table.index.month

        pivot = table.pivot(index="year", columns="month", values="return")
        pivot.columns = [f"{m}月" for m in pivot.columns]

        # 添加年度收益
        annual = nav.resample("YE").apply(lambda x: x.iloc[-1] / x.iloc[0] - 1 if len(x) > 1 else 0)
        annual.index = annual.index.year
        pivot["全年"] = annual

        return pivot
