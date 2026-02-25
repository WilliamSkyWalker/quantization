"""
质量因子

实现基于财务质量的因子：
    - ROE_TTM: 净资产收益率（滚动12个月）
    - GROSS_MARGIN: 销售毛利率

使用公告日期获取最新可用的财务数据，防止未来函数。
"""

import numpy as np
import pandas as pd

from factors.base import FactorBase


class ROEFactor(FactorBase):
    """
    ROE_TTM 因子。

    取截止计算日已公告的最新一期 ROE_TTM 值。
    ROE 越高表示盈利能力越强。
    """

    name = "ROE_TTM"
    description = "净资产收益率TTM，越高盈利能力越强"

    def compute(self, date: str, universe: pd.DataFrame) -> pd.DataFrame:
        codes = universe["ts_code"].tolist()

        df_fin = self.get_latest_financial(date, ["roe_ttm"], codes)

        if df_fin.empty:
            return pd.DataFrame(columns=["ts_code", "factor_value"])

        df_fin["factor_value"] = pd.to_numeric(df_fin["roe_ttm"], errors="coerce")

        return df_fin[["ts_code", "factor_value"]]


class GrossMarginFactor(FactorBase):
    """
    毛利率因子。

    取截止计算日已公告的最新一期销售毛利率。
    毛利率越高表示产品定价能力和竞争优势越强。
    """

    name = "GROSS_MARGIN"
    description = "销售毛利率，越高竞争优势越强"

    def compute(self, date: str, universe: pd.DataFrame) -> pd.DataFrame:
        codes = universe["ts_code"].tolist()

        df_fin = self.get_latest_financial(date, ["gross_margin"], codes)

        if df_fin.empty:
            return pd.DataFrame(columns=["ts_code", "factor_value"])

        df_fin["factor_value"] = pd.to_numeric(
            df_fin["gross_margin"], errors="coerce"
        )

        return df_fin[["ts_code", "factor_value"]]
