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

from backend.services.factors.base import FactorBase


class _MomentumMixin:
    """动量因子通用方法：优先使用预计算的月末价格。"""

    def _compute_momentum(
        self, date: str, universe: pd.DataFrame,
        now_months_ago: int, prev_months_ago: int,
    ) -> pd.DataFrame:
        codes = universe["ts_code"].tolist()
        codes_set = set(codes)

        # 快速路径：使用预计算月末价格
        df_now = self._get_month_end_adj_close(date, now_months_ago, codes_set)
        df_prev = self._get_month_end_adj_close(date, prev_months_ago, codes_set)
        if df_now is None:
            df_now = self.get_month_end_price(date, now_months_ago, codes)
            df_now = df_now.rename(columns={"close": "adj_close"}) if not df_now.empty else df_now
        if df_prev is None:
            df_prev = self.get_month_end_price(date, prev_months_ago, codes)
            df_prev = df_prev.rename(columns={"close": "adj_close"}) if not df_prev.empty else df_prev

        if (df_now is None or df_now.empty) or (df_prev is None or df_prev.empty):
            return pd.DataFrame(columns=["ts_code", "factor_value"])

        df = df_now.merge(df_prev, on="ts_code", suffixes=("_now", "_prev"))
        df["factor_value"] = np.where(
            (df["adj_close_prev"].notna()) & (df["adj_close_prev"] > 0),
            df["adj_close_now"] / df["adj_close_prev"] - 1,
            np.nan,
        )
        return df[["ts_code", "factor_value"]]


class MOM1MFactor(_MomentumMixin, FactorBase):
    """过去 1 个月收益率。"""

    name = "MOM_1M"
    description = "过去1个月收益率"

    def compute(self, date: str, universe: pd.DataFrame) -> pd.DataFrame:
        return self._compute_momentum(date, universe, 0, 1)


class MOM3MFactor(_MomentumMixin, FactorBase):
    """过去 3 个月收益率。"""

    name = "MOM_3M"
    description = "过去3个月收益率"

    def compute(self, date: str, universe: pd.DataFrame) -> pd.DataFrame:
        return self._compute_momentum(date, universe, 0, 3)


class MOM12MFactor(_MomentumMixin, FactorBase):
    """
    过去 12 个月收益率（剔除最近 1 个月）。

    经典 12-1 动量：用 T-1月 的价格 / T-12月 的价格 - 1。
    剔除最近1个月可避免短期反转效应污染长期动量信号。
    """

    name = "MOM_12M"
    description = "过去12个月收益率（剔除最近1月，12-1动量）"

    def compute(self, date: str, universe: pd.DataFrame) -> pd.DataFrame:
        return self._compute_momentum(date, universe, 1, 12)


class ResidualMomentumFactor(FactorBase):
    """
    残差动量因子：个股 20 日累计收益减去所属行业平均累计收益。

    剥离行业 beta 后的个股 alpha 动量，避免因子拥挤。
    """

    name = "RESIDUAL_MOM"
    description = "残差动量，个股收益减行业均值"

    def compute(self, date: str, universe: pd.DataFrame) -> pd.DataFrame:
        codes = universe["ts_code"].tolist()
        codes_set = set(codes)

        try:
            df_industry = self.get_industry_map_cached()
        except Exception:
            return pd.DataFrame(columns=["ts_code", "factor_value"])
        if df_industry.empty:
            return pd.DataFrame(columns=["ts_code", "factor_value"])

        # 快速路径：使用预计算 20 日累计收益
        day = self._get_rolling_for_date(date, codes_set)
        if day is not None and not day.empty and "cum_ret_20d" in day.columns:
            df_cum_ret = day[["cum_ret_20d"]].reset_index()
            df_cum_ret.columns = ["ts_code", "cum_ret"]
            df_cum_ret = df_cum_ret.dropna(subset=["cum_ret"])
        else:
            # 回退到原始逻辑
            df_price = self.get_price_history(
                date, lookback_days=45, universe_codes=codes, columns=["pct_chg"],
            )
            if df_price.empty:
                return pd.DataFrame(columns=["ts_code", "factor_value"])
            df_price["trade_date"] = pd.to_datetime(df_price["trade_date"])
            df_price["pct_chg"] = pd.to_numeric(df_price["pct_chg"], errors="coerce")
            df_price = df_price.sort_values(["ts_code", "trade_date"])
            df_recent = df_price.groupby("ts_code").tail(20)
            df_cum_ret = (
                df_recent.groupby("ts_code")
                .apply(lambda g: (1 + g["pct_chg"] / 100).prod() - 1, include_groups=False)
                .reset_index()
            )
            df_cum_ret.columns = ["ts_code", "cum_ret"]

        df_merged = df_cum_ret.merge(df_industry, on="ts_code", how="left")
        df_merged = df_merged.dropna(subset=["industry_name"])
        ind_mean = df_merged.groupby("industry_name")["cum_ret"].transform("mean")
        df_merged["factor_value"] = df_merged["cum_ret"] - ind_mean

        return df_merged[["ts_code", "factor_value"]]


class ShortReversalFactor(FactorBase):
    """
    过去 5 个交易日的短期反转因子。

    公式：-1 × 近 5 日累计涨跌幅
    利用短期反转效应：近期跌幅大的股票短期内有反弹倾向。
    """

    name = "REV_5D"
    description = "5日短期反转，超跌反弹信号"

    def compute(self, date: str, universe: pd.DataFrame) -> pd.DataFrame:
        codes_set = set(universe["ts_code"].tolist())

        # 快速路径：使用预计算 5 日累计收益
        day = self._get_rolling_for_date(date, codes_set)
        if day is not None and not day.empty and "cum_ret_5d" in day.columns:
            df = day[["cum_ret_5d"]].reset_index()
            df.columns = ["ts_code", "factor_value"]
            df["factor_value"] = -1 * df["factor_value"]
            return df.dropna(subset=["factor_value"])

        # 回退到原始逻辑
        codes = list(codes_set)
        df_price = self.get_price_history(
            date, lookback_days=15, universe_codes=codes, columns=["pct_chg"],
        )
        if df_price.empty:
            return pd.DataFrame(columns=["ts_code", "factor_value"])

        df_price["trade_date"] = pd.to_datetime(df_price["trade_date"])
        df_price["pct_chg"] = pd.to_numeric(df_price["pct_chg"], errors="coerce")
        df_price = df_price.sort_values(["ts_code", "trade_date"])
        df_recent = df_price.groupby("ts_code").tail(5)
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
