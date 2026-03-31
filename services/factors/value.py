"""
价值因子

实现基于财务估值指标的因子：
    - EP: 市盈率倒数（Earnings-to-Price = TTM净利润 / 总市值）
    - BP: 市净率倒数（Book-to-Price = 每股净资产 / 收盘价）

本地计算，消除前视偏差：
    - EP 使用 TTM 净利润、当日收盘价和总股本计算
    - BP 使用最新季报的 bps 和当日收盘价计算
    - 所有财务数据严格遵守 ann_date <= date（公告日约束）
"""

import pandas as pd
import numpy as np

from services.factors.base import FactorBase


class EPFactor(FactorBase):
    """
    EP 因子（Earnings-to-Price，市盈率倒数）。

    EP = TTM净利润 / (收盘价 × 总股本 × 10000)
    总股本单位为万股，需要 ×10000 转为股，再乘以收盘价得到总市值（元）。
    净利润为负或数据缺失时设为 NaN。
    EP 越高表示越"便宜"。
    """

    name = "EP"
    description = "市盈率倒数，EP = TTM净利润 / 总市值，越高越便宜"

    def compute(self, date: str, universe: pd.DataFrame) -> pd.DataFrame:
        codes = universe["ts_code"].tolist()

        # 获取 TTM 净利润、收盘价、总股本
        df_ttm = self.get_ttm_net_profit(date, codes)
        df_close = self.get_close_on_date(date, codes)
        df_share = self.get_total_share(codes)

        if df_ttm.empty or df_close.empty or df_share.empty:
            return pd.DataFrame(columns=["ts_code", "factor_value"])

        # 合并
        df = df_ttm.merge(df_close, on="ts_code", how="inner")
        df = df.merge(df_share, on="ts_code", how="inner")

        # 计算：EP = TTM净利润 / (收盘价 × 总股本(万股) × 10000)
        market_cap = df["close"] * df["total_share"] * 10000  # 总市值（元）
        df["factor_value"] = np.where(
            (df["ttm_net_profit"].notna()) & (df["ttm_net_profit"] > 0)
            & (market_cap > 0),
            df["ttm_net_profit"] / market_cap,
            np.nan,
        )

        return df[["ts_code", "factor_value"]]


class BPFactor(FactorBase):
    """
    BP 因子（Book-to-Price，市净率倒数）。

    BP = bps / 收盘价
    bps 为负或缺失时设为 NaN。
    BP 越高表示越"便宜"。
    """

    name = "BP"
    description = "市净率倒数，BP = 每股净资产/收盘价，越高越便宜"

    def compute(self, date: str, universe: pd.DataFrame) -> pd.DataFrame:
        codes = universe["ts_code"].tolist()

        # 获取 bps（最新季报）和收盘价
        df_fin = self.get_latest_financial(date, ["bps"], codes)
        df_close = self.get_close_on_date(date, codes)

        if df_fin.empty or df_close.empty:
            return pd.DataFrame(columns=["ts_code", "factor_value"])

        df = df_fin.merge(df_close, on="ts_code", how="inner")

        df["factor_value"] = np.where(
            (df["bps"].notna()) & (df["bps"] > 0) & (df["close"] > 0),
            df["bps"] / df["close"],
            np.nan,
        )

        return df[["ts_code", "factor_value"]]
