"""
A 股绩效分析模块（Django ORM 版）

迁自 services/monitor/performance.py。提供：
    1. 核心绩效指标（总/年化收益 / Sharpe / Sortino / MDD / Calmar / 胜率）
    2. 滚动指标（滚动 Sharpe / 波动率 / 回撤）
    3. 行业归因（对各申万 L1 行业的贡献）
    4. 月度收益表
"""

import logging
from typing import Optional

import numpy as np
import pandas as pd

from services.config import LOG_LEVEL
from stocks.models import ADailyPrice, AIndustryClass

logger = logging.getLogger(__name__)
logger.setLevel(LOG_LEVEL)


class PerformanceAnalyzer:
    """A 股绩效分析器。"""

    def __init__(self, db=None, rf_rate: float = 0.02):
        self.db = db
        self.rf_rate = rf_rate
        self.rf_daily = (1 + rf_rate) ** (1 / 252) - 1

    # ----------------------------------------------------------
    # 核心指标
    # ----------------------------------------------------------

    def calc_metrics(
        self,
        nav: pd.Series,
        benchmark_nav: Optional[pd.Series] = None,
    ) -> dict:
        daily_ret = nav.pct_change().dropna()
        n_days = len(daily_ret)
        n_years = n_days / 252

        total_return = nav.iloc[-1] / nav.iloc[0] - 1
        annual_return = (1 + total_return) ** (1 / max(n_years, 0.01)) - 1
        annual_vol = daily_ret.std() * np.sqrt(252)
        sharpe = (annual_return - self.rf_rate) / annual_vol if annual_vol > 0 else 0

        cummax = nav.cummax()
        drawdown = (nav - cummax) / cummax
        max_drawdown = drawdown.min()
        max_dd_start = cummax[:drawdown.idxmin()].idxmax() if pd.notna(drawdown.idxmin()) else None
        max_dd_end = drawdown.idxmin()

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

        if benchmark_nav is not None and not benchmark_nav.empty:
            common = nav.index.intersection(benchmark_nav.index)
            if len(common) > 10:
                bm = benchmark_nav.loc[common]
                bm_ret = bm.pct_change().dropna()
                strat_ret = nav.loc[common].pct_change().dropna()

                bm_total = bm.iloc[-1] / bm.iloc[0] - 1
                bm_annual = (1 + bm_total) ** (1 / max(n_years, 0.01)) - 1

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

    def rolling_metrics(self, nav: pd.Series, window: int = 60) -> pd.DataFrame:
        daily_ret = nav.pct_change().dropna()

        rolling_vol = daily_ret.rolling(window).std() * np.sqrt(252)
        rolling_mean = daily_ret.rolling(window).mean() * 252
        rolling_sharpe = (rolling_mean - self.rf_rate) / rolling_vol

        rolling_dd = pd.Series(index=nav.index, dtype=float)
        for i in range(window, len(nav)):
            window_nav = nav.iloc[i - window:i + 1]
            peak = window_nav.cummax()
            dd = ((window_nav - peak) / peak).min()
            rolling_dd.iloc[i] = dd

        return pd.DataFrame({
            "rolling_return": rolling_mean,
            "rolling_volatility": rolling_vol,
            "rolling_sharpe": rolling_sharpe,
            "rolling_max_drawdown": rolling_dd,
        })

    # ----------------------------------------------------------
    # 行业归因
    # ----------------------------------------------------------

    def industry_attribution(
        self,
        holdings: pd.DataFrame,
        start_date: str,
        end_date: str,
    ) -> pd.DataFrame:
        """行业归因：按申万 L1 分组统计贡献。"""
        try:
            rows = list(
                AIndustryClass.objects.filter(
                    src="SW2021", level="L1", out_date__isnull=True,
                ).values("ts_code", "index_name")
            )
            industry_map = pd.DataFrame(rows)
            if not industry_map.empty:
                industry_map = industry_map.rename(columns={"index_name": "industry_name"})
        except Exception as e:
            logger.warning(f"industry_attribution: 获取行业映射失败: {e}")
            return pd.DataFrame()

        if industry_map.empty:
            return pd.DataFrame()

        df = holdings.merge(industry_map, on="ts_code", how="left")
        df["industry_name"] = df["industry_name"].fillna("未知")

        codes = df["ts_code"].tolist()
        if not codes:
            return pd.DataFrame()

        s_d = pd.to_datetime(start_date).date()
        e_d = pd.to_datetime(end_date).date()
        price_rows = list(
            ADailyPrice.objects.filter(
                ts_code__in=codes, trade_date__gte=s_d, trade_date__lte=e_d,
            ).order_by("ts_code", "trade_date").values("ts_code", "trade_date", "close")
        )
        if not price_rows:
            return pd.DataFrame()
        df_price = pd.DataFrame(price_rows)

        stock_returns = {}
        for code, group in df_price.groupby("ts_code"):
            if len(group) >= 2:
                ret = group["close"].iloc[-1] / group["close"].iloc[0] - 1
                stock_returns[code] = ret

        df["stock_return"] = df["ts_code"].map(stock_returns)
        df["contribution"] = df["weight"] * df["stock_return"]

        return (
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

    # ----------------------------------------------------------
    # 月度绩效
    # ----------------------------------------------------------

    @staticmethod
    def monthly_returns(nav: pd.Series) -> pd.DataFrame:
        daily_ret = nav.pct_change().dropna()
        daily_ret.index = pd.to_datetime(daily_ret.index)

        monthly_ret = daily_ret.resample("ME").apply(lambda x: (1 + x).prod() - 1)
        monthly_ret.index = monthly_ret.index.to_period("M")

        table = monthly_ret.to_frame("return")
        table["year"] = table.index.year
        table["month"] = table.index.month

        pivot = table.pivot(index="year", columns="month", values="return")
        pivot.columns = [f"{m}月" for m in pivot.columns]

        annual = nav.resample("YE").apply(lambda x: x.iloc[-1] / x.iloc[0] - 1 if len(x) > 1 else 0)
        annual.index = annual.index.year
        pivot["全年"] = annual
        return pivot
