"""
美股内部人交易因子: INSIDER_NET_BUY

从 SEC Form 4 数据（openinsider.com）提取高管净买入信号。
高管持续净买入 = 看好公司前景的强信号（信息不对称优势）。

因子值 = 过去 90 天内净买入金额 / 市值
"""

import logging

import numpy as np
import pandas as pd

from services.config import LOG_LEVEL
from services.us_factors.base import USFactorBase

logger = logging.getLogger(__name__)
logger.setLevel(LOG_LEVEL)

_LOOKBACK_DAYS = 90


class InsiderNetBuy(USFactorBase):
    """Insider Net Buy: net insider purchases / market cap over trailing 90 days"""
    name = "INSIDER_NET_BUY"
    description = "内部人净买入 (Form 4，近90天净买入/市值)"

    def compute(self, date: str, universe: pd.DataFrame) -> pd.DataFrame:
        tickers = universe["ticker"].tolist()
        date_ts = pd.to_datetime(date)

        # 尝试从预加载缓存获取（回测模式）
        insider_data = self._get_insider_data(date, tickers)
        if insider_data.empty:
            logger.debug("InsiderNetBuy.compute: 无内部人交易数据")
            return pd.DataFrame(columns=["ticker", "factor_value"])

        # 市值
        mktcap = self.get_market_cap(date, tickers)
        if mktcap.empty:
            logger.debug("InsiderNetBuy.compute: 无市值数据")
            return pd.DataFrame(columns=["ticker", "factor_value"])

        df = insider_data.merge(mktcap, on="ticker", how="inner")
        df["factor_value"] = np.where(
            df["market_cap"] > 0,
            df["net_value"] / df["market_cap"],
            0.0,
        )
        return df[["ticker", "factor_value"]]

    def _get_insider_data(self, date: str, tickers: list[str]) -> pd.DataFrame:
        """
        获取内部人交易数据（从 us_insider_trade 表）。

        计算近 90 天每个 ticker 的净买入金额：
        - A (Acquisition) = 买入，金额为正
        - D (Disposition) = 卖出，金额为负
        - net_value = SUM(signed_value)
        """
        date_ts = pd.to_datetime(date)
        start_ts = date_ts - pd.Timedelta(days=_LOOKBACK_DAYS)

        # 优先从预加载缓存获取
        bulk_insider = self._static_cache.get("_bulk_insider")
        if bulk_insider is not None and not bulk_insider.empty:
            mask = (bulk_insider["filing_date"] >= start_ts) & \
                   (bulk_insider["filing_date"] <= date_ts)
            df = bulk_insider[mask].copy()
            if not df.empty:
                df = df[df["ticker"].isin(tickers)]
                if not df.empty:
                    result = df.groupby("ticker")["net_value"].sum().reset_index()
                    return result
            logger.debug(f"_get_insider_data: 预加载缓存中 {date} 近90天无数据")
            return pd.DataFrame(columns=["ticker", "net_value"])

        logger.warning("_get_insider_data: 缓存为空，请先调用 preload_for_backtest()")
        return pd.DataFrame(columns=["ticker", "net_value"])
