"""
动量因子

实现基于历史收益率的动量因子：
    - MOM_1M:  过去 1 个月收益率
    - MOM_3M:  过去 3 个月收益率
    - MOM_12M: 过去 12 个月收益率（剔除最近 1 个月，即 12-1 动量）

计算方式：
    MOM_NM = close(T) / close(T-N个月) - 1

    MOM_12M 特殊处理：
    MOM_12M = close(T-1个月) / close(T-12个月) - 1
    剔除最近1个月是经典做法，避免短期反转效应干扰。
"""

import numpy as np
import pandas as pd

from factors.base import FactorBase


class MOM1MFactor(FactorBase):
    """过去 1 个月收益率。"""

    name = "MOM_1M"
    description = "过去1个月收益率"

    def compute(self, date: str, universe: pd.DataFrame) -> pd.DataFrame:
        codes = universe["ts_code"].tolist()

        df_now = self.get_month_end_price(date, 0, codes)
        df_prev = self.get_month_end_price(date, 1, codes)

        if df_now.empty or df_prev.empty:
            return pd.DataFrame(columns=["ts_code", "factor_value"])

        df = df_now.merge(df_prev, on="ts_code", suffixes=("_now", "_prev"))
        df["factor_value"] = np.where(
            (df["close_prev"].notna()) & (df["close_prev"] > 0),
            df["close_now"] / df["close_prev"] - 1,
            np.nan,
        )

        return df[["ts_code", "factor_value"]]


class MOM3MFactor(FactorBase):
    """过去 3 个月收益率。"""

    name = "MOM_3M"
    description = "过去3个月收益率"

    def compute(self, date: str, universe: pd.DataFrame) -> pd.DataFrame:
        codes = universe["ts_code"].tolist()

        df_now = self.get_month_end_price(date, 0, codes)
        df_prev = self.get_month_end_price(date, 3, codes)

        if df_now.empty or df_prev.empty:
            return pd.DataFrame(columns=["ts_code", "factor_value"])

        df = df_now.merge(df_prev, on="ts_code", suffixes=("_now", "_prev"))
        df["factor_value"] = np.where(
            (df["close_prev"].notna()) & (df["close_prev"] > 0),
            df["close_now"] / df["close_prev"] - 1,
            np.nan,
        )

        return df[["ts_code", "factor_value"]]


class MOM12MFactor(FactorBase):
    """
    过去 12 个月收益率（剔除最近 1 个月）。

    经典 12-1 动量：用 T-1月 的价格 / T-12月 的价格 - 1。
    剔除最近1个月可避免短期反转效应污染长期动量信号。
    """

    name = "MOM_12M"
    description = "过去12个月收益率（剔除最近1月，12-1动量）"

    def compute(self, date: str, universe: pd.DataFrame) -> pd.DataFrame:
        codes = universe["ts_code"].tolist()

        # T-1个月 的收盘价
        df_1m = self.get_month_end_price(date, 1, codes)
        # T-12个月 的收盘价
        df_12m = self.get_month_end_price(date, 12, codes)

        if df_1m.empty or df_12m.empty:
            return pd.DataFrame(columns=["ts_code", "factor_value"])

        df = df_1m.merge(df_12m, on="ts_code", suffixes=("_1m", "_12m"))
        df["factor_value"] = np.where(
            (df["close_12m"].notna()) & (df["close_12m"] > 0),
            df["close_1m"] / df["close_12m"] - 1,
            np.nan,
        )

        return df[["ts_code", "factor_value"]]
