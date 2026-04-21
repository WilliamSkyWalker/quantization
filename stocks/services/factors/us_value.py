"""美股价值因子: EP, BP, DIV_YIELD"""

import logging

import numpy as np
import polars as pl

from services.config import LOG_LEVEL
from stocks.services.factors.us_base import USFactorBase

logger = logging.getLogger(__name__)
logger.setLevel(LOG_LEVEL)

_EMPTY = pl.DataFrame(schema={"ticker": pl.Utf8, "factor_value": pl.Float64})


class EP(USFactorBase):
    """Earnings-to-Price: TTM EPS / adj_close"""
    name = "EP"
    description = "盈利收益率 (TTM EPS / 股价)"

    def compute(self, date: str, universe: pl.DataFrame) -> pl.DataFrame:
        tickers = universe["ticker"].to_list()
        ttm_eps = self.get_ttm_value(date, "eps", tickers)
        close = self.get_close_on_date(date, tickers)

        if ttm_eps.is_empty() or close.is_empty():
            logger.debug("EP.compute: TTM EPS或收盘价数据为空")
            return _EMPTY.clone()

        df = ttm_eps.join(close, on="ticker", how="inner")
        df = df.with_columns(
            pl.when(
                (pl.col("adj_close") > 0) & pl.col("ttm_value").is_not_null()
            )
            .then(pl.col("ttm_value") / pl.col("adj_close"))
            .otherwise(None)
            .alias("factor_value")
        )
        return df.select(["ticker", "factor_value"])


class BP(USFactorBase):
    """Book-to-Price: total_equity / market_cap"""
    name = "BP"
    description = "账面价值比 (股东权益 / 市值)"

    def compute(self, date: str, universe: pl.DataFrame) -> pl.DataFrame:
        tickers = universe["ticker"].to_list()
        fin = self.get_latest_financial(date, ["total_equity"], tickers)
        mkcap = self.get_market_cap(date, tickers)

        if fin.is_empty() or mkcap.is_empty():
            logger.debug("BP.compute: 财务数据或市值数据为空")
            return _EMPTY.clone()

        df = fin.join(mkcap, on="ticker", how="inner")
        df = df.with_columns(
            pl.col("total_equity").cast(pl.Float64, strict=False)
        )
        df = df.with_columns(
            pl.when(
                (pl.col("market_cap") > 0) & pl.col("total_equity").is_not_null()
            )
            .then(pl.col("total_equity") / pl.col("market_cap"))
            .otherwise(None)
            .alias("factor_value")
        )
        return df.select(["ticker", "factor_value"])


class DivYield(USFactorBase):
    """Dividend Yield: trailing 12M dividends / adj_close"""
    name = "DIV_YIELD"
    description = "股息率 (近12个月股息 / 股价)"

    def compute(self, date: str, universe: pl.DataFrame) -> pl.DataFrame:
        tickers = universe["ticker"].to_list()
        divs = self.get_dividends(date, lookback_days=365, universe_tickers=tickers)
        close = self.get_close_on_date(date, tickers)

        if divs.is_empty() or close.is_empty():
            logger.debug("DivYield.compute: 股息数据或收盘价数据为空")
            return _EMPTY.clone()

        df = divs.join(close, on="ticker", how="inner")
        df = df.with_columns(
            pl.when(
                (pl.col("adj_close") > 0) & pl.col("total_dividend").is_not_null()
            )
            .then(pl.col("total_dividend") / pl.col("adj_close"))
            .otherwise(None)
            .alias("factor_value")
        )
        return df.select(["ticker", "factor_value"])
