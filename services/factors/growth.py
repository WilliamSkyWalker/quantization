"""
成长因子

实现基于财务同比增速的成长因子：
    - NET_PROFIT_YOY: 归母净利润 TTM 同比增速
    - REVENUE_YOY: 营收 TTM 同比增速

计算方式：
    指标_YOY = TTM(当期) / TTM(1年前) - 1
    分母 <= 0 时返回 NaN，避免负利润增速误导。
"""

import numpy as np
import pandas as pd

from services.factors.base import FactorBase


class NetProfitYOYFactor(FactorBase):
    """
    归母净利润 TTM 同比增速。

    公式：get_ttm_net_profit(date) / get_ttm_net_profit(date - 1Y) - 1
    分母 <= 0 时返回 NaN。
    """

    name = "NET_PROFIT_YOY"
    description = "归母净利润TTM同比增速"

    def compute(self, date: str, universe: pd.DataFrame) -> pd.DataFrame:
        codes = universe["ts_code"].tolist()

        # 当期 TTM 净利润
        df_now = self.get_ttm_net_profit(date, codes)
        # 1 年前 TTM 净利润
        date_1y_ago = (pd.to_datetime(date) - pd.DateOffset(years=1)).strftime("%Y-%m-%d")
        df_prev = self.get_ttm_net_profit(date_1y_ago, codes)

        if df_now.empty or df_prev.empty:
            return pd.DataFrame(columns=["ts_code", "factor_value"])

        df = df_now.merge(df_prev, on="ts_code", suffixes=("_now", "_prev"))
        df["factor_value"] = np.where(
            (df["ttm_net_profit_prev"].notna()) & (df["ttm_net_profit_prev"] > 0),
            df["ttm_net_profit_now"] / df["ttm_net_profit_prev"] - 1,
            np.nan,
        )

        return df[["ts_code", "factor_value"]]


class NetProfitCAGR3YFactor(FactorBase):
    """
    归母净利润 3 年复合增长率（CAGR）。

    公式：(TTM净利润(当期) / TTM净利润(3年前))^(1/3) - 1
    分母 <= 0 时返回 NaN。IPO < 3 年的股票自动 NaN。
    """

    name = "NET_PROFIT_CAGR_3Y"
    description = "归母净利润3年复合增长率"

    def compute(self, date: str, universe: pd.DataFrame) -> pd.DataFrame:
        codes = universe["ts_code"].tolist()

        # 当期 TTM 净利润
        df_now = self.get_ttm_net_profit(date, codes)
        # 3 年前 TTM 净利润
        date_3y_ago = (pd.to_datetime(date) - pd.DateOffset(years=3)).strftime("%Y-%m-%d")
        df_prev = self.get_ttm_net_profit(date_3y_ago, codes)

        if df_now.empty or df_prev.empty:
            return pd.DataFrame(columns=["ts_code", "factor_value"])

        df = df_now.merge(df_prev, on="ts_code", suffixes=("_now", "_prev"))
        df["factor_value"] = np.where(
            (df["ttm_net_profit_prev"].notna()) & (df["ttm_net_profit_prev"] > 0)
            & (df["ttm_net_profit_now"].notna()) & (df["ttm_net_profit_now"] > 0),
            (df["ttm_net_profit_now"] / df["ttm_net_profit_prev"]) ** (1.0 / 3.0) - 1,
            np.nan,
        )

        return df[["ts_code", "factor_value"]]


class RevenueYOYFactor(FactorBase):
    """
    营收 TTM 同比增速。

    公式：get_ttm_revenue(date) / get_ttm_revenue(date - 1Y) - 1
    分母 <= 0 时返回 NaN。
    """

    name = "REVENUE_YOY"
    description = "营收TTM同比增速"

    def compute(self, date: str, universe: pd.DataFrame) -> pd.DataFrame:
        codes = universe["ts_code"].tolist()

        # 当期 TTM 营收
        df_now = self.get_ttm_revenue(date, codes)
        # 1 年前 TTM 营收
        date_1y_ago = (pd.to_datetime(date) - pd.DateOffset(years=1)).strftime("%Y-%m-%d")
        df_prev = self.get_ttm_revenue(date_1y_ago, codes)

        if df_now.empty or df_prev.empty:
            return pd.DataFrame(columns=["ts_code", "factor_value"])

        df = df_now.merge(df_prev, on="ts_code", suffixes=("_now", "_prev"))
        df["factor_value"] = np.where(
            (df["ttm_revenue_prev"].notna()) & (df["ttm_revenue_prev"] > 0),
            df["ttm_revenue_now"] / df["ttm_revenue_prev"] - 1,
            np.nan,
        )

        return df[["ts_code", "factor_value"]]
