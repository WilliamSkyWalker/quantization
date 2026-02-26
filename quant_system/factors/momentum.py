"""
动量因子

实现基于历史收益率的动量因子：
    - MOM_1M:  过去 1 个月收益率
    - MOM_3M:  过去 3 个月收益率
    - MOM_12M: 过去 12 个月收益率（剔除最近 1 个月，即 12-1 动量）
    - REV_5D:  过去 5 个交易日短期反转（-1 × 累计涨跌幅）

计算方式：
    MOM_NM = close(T) / close(T-N个月) - 1

    MOM_12M 特殊处理：
    MOM_12M = close(T-1个月) / close(T-12个月) - 1
    剔除最近1个月是经典做法，避免短期反转效应干扰。

    REV_5D = -1 × (近5日累计收益率)
    利用短期反转效应，暴跌期捕捉超跌反弹。
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


class ResidualMomentumFactor(FactorBase):
    """
    残差动量因子：个股 20 日累计收益减去所属行业平均累计收益。

    剥离行业 beta 后的个股 alpha 动量，避免因子拥挤。
    """

    name = "RESIDUAL_MOM"
    description = "残差动量，个股收益减行业均值"

    def compute(self, date: str, universe: pd.DataFrame) -> pd.DataFrame:
        codes = universe["ts_code"].tolist()

        # 获取行业映射
        try:
            df_industry = self.db.get_industry_map()
        except Exception:
            return pd.DataFrame(columns=["ts_code", "factor_value"])

        if df_industry.empty:
            return pd.DataFrame(columns=["ts_code", "factor_value"])

        # 取近 20 个交易日的 pct_chg
        df_price = self.get_price_history(
            date, lookback_days=45,
            universe_codes=codes,
            columns=["pct_chg"],
        )

        if df_price.empty:
            return pd.DataFrame(columns=["ts_code", "factor_value"])

        df_price["trade_date"] = pd.to_datetime(df_price["trade_date"])
        df_price["pct_chg"] = pd.to_numeric(df_price["pct_chg"], errors="coerce")
        df_price = df_price.sort_values(["ts_code", "trade_date"])

        # 每只股票取最近 20 个交易日
        df_recent = df_price.groupby("ts_code").tail(20)

        # 计算每只股票的 20 日累计收益率
        df_cum_ret = (
            df_recent.groupby("ts_code")
            .apply(lambda g: (1 + g["pct_chg"] / 100).prod() - 1, include_groups=False)
            .reset_index()
        )
        df_cum_ret.columns = ["ts_code", "cum_ret"]

        # 合并行业信息
        df_merged = df_cum_ret.merge(df_industry, on="ts_code", how="left")
        df_merged = df_merged.dropna(subset=["industry_name"])

        # 按行业计算平均累计收益
        ind_mean = df_merged.groupby("industry_name")["cum_ret"].transform("mean")

        # 残差动量 = 个股收益 - 行业均值
        df_merged["factor_value"] = df_merged["cum_ret"] - ind_mean

        return df_merged[["ts_code", "factor_value"]]


class ShortReversalFactor(FactorBase):
    """
    过去 5 个交易日的短期反转因子。

    公式：-1 × 近 5 日累计涨跌幅
    利用短期反转效应：近期跌幅大的股票短期内有反弹倾向。
    暴跌期（如 2025 年 3 月）该因子可捕捉超跌反弹机会。
    """

    name = "REV_5D"
    description = "5日短期反转，超跌反弹信号"

    def compute(self, date: str, universe: pd.DataFrame) -> pd.DataFrame:
        codes = universe["ts_code"].tolist()

        df_price = self.get_price_history(
            date, lookback_days=15,
            universe_codes=codes,
            columns=["pct_chg"],
        )

        if df_price.empty:
            return pd.DataFrame(columns=["ts_code", "factor_value"])

        df_price["trade_date"] = pd.to_datetime(df_price["trade_date"])
        df_price["pct_chg"] = pd.to_numeric(df_price["pct_chg"], errors="coerce")
        df_price = df_price.sort_values(["ts_code", "trade_date"])

        # 每只股票取最近 5 个交易日
        df_recent = (
            df_price.groupby("ts_code")
            .tail(5)
        )

        # 累计收益率 = prod(1 + r) - 1，然后乘以 -1（反转信号）
        df_rev = (
            df_recent.groupby("ts_code")
            .apply(
                lambda g: -1 * ((1 + g["pct_chg"] / 100).prod() - 1),
                include_groups=False,
            )
            .reset_index()
        )
        df_rev.columns = ["ts_code", "factor_value"]

        return df_rev
