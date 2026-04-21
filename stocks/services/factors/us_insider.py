"""
美股内部人交易因子: INSIDER_NET_BUY

从 SEC Form 4 数据（openinsider.com）提取高管净买入信号。
高管持续净买入 = 看好公司前景的强信号（信息不对称优势）。

因子值 = 过去 90 天内净买入金额 / 市值
"""

import logging
from datetime import datetime, timedelta

import numpy as np
import polars as pl

from services.config import LOG_LEVEL
from stocks.services.factors.us_base import USFactorBase

logger = logging.getLogger(__name__)
logger.setLevel(LOG_LEVEL)

_LOOKBACK_DAYS = 90

_EMPTY = pl.DataFrame(schema={"ticker": pl.Utf8, "factor_value": pl.Float64})


class InsiderNetBuy(USFactorBase):
    """Insider Net Buy: net insider purchases / market cap over trailing 90 days"""
    name = "INSIDER_NET_BUY"
    description = "内部人净买入 (Form 4，近90天净买入/市值)"

    def compute(self, date: str, universe: pl.DataFrame) -> pl.DataFrame:
        tickers = universe["ticker"].to_list()

        # 尝试从预加载缓存获取（回测模式）
        insider_data = self._get_insider_data(date, tickers)
        if insider_data.is_empty():
            logger.debug("InsiderNetBuy.compute: 无内部人交易数据")
            return _EMPTY.clone()

        # 市值
        mktcap = self.get_market_cap(date, tickers)
        if mktcap.is_empty():
            logger.debug("InsiderNetBuy.compute: 无市值数据")
            return _EMPTY.clone()

        df = insider_data.join(mktcap, on="ticker", how="inner")
        df = df.with_columns(
            pl.when(pl.col("market_cap") > 0)
            .then(pl.col("net_value") / pl.col("market_cap"))
            .otherwise(0.0)
            .alias("factor_value")
        )
        return df.select(["ticker", "factor_value"])

    def _get_insider_data(self, date: str, tickers: list[str]) -> pl.DataFrame:
        """
        获取内部人交易数据（从 us_insider_trade 表）。

        计算近 90 天每个 ticker 的净买入金额：
        - A (Acquisition) = 买入，金额为正
        - D (Disposition) = 卖出，金额为负
        - net_value = SUM(signed_value)
        """
        date_ts = datetime.strptime(date, "%Y-%m-%d") if isinstance(date, str) else date
        start_ts = date_ts - timedelta(days=_LOOKBACK_DAYS)

        # 优先从预加载缓存获取
        bulk_insider = self._static_cache.get("_bulk_insider")
        if bulk_insider is not None and not bulk_insider.is_empty():
            df = bulk_insider.filter(
                (pl.col("filing_date") >= start_ts) & (pl.col("filing_date") <= date_ts)
            )
            if not df.is_empty():
                df = df.filter(pl.col("ticker").is_in(tickers))
                if not df.is_empty():
                    result = df.group_by("ticker").agg(
                        pl.col("net_value").sum().alias("net_value")
                    )
                    return result
            logger.debug(f"_get_insider_data: 预加载缓存中 {date} 近90天无数据")
            return pl.DataFrame(schema={"ticker": pl.Utf8, "net_value": pl.Float64})

        logger.warning("_get_insider_data: 缓存为空，请先调用 preload_for_backtest()")
        return pl.DataFrame(schema={"ticker": pl.Utf8, "net_value": pl.Float64})
