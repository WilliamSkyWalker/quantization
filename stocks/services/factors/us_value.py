"""美股价值因子: EP, BP, DIV_YIELD"""

import logging

import numpy as np
import pandas as pd

from services.config import LOG_LEVEL
from stocks.services.factors.us_base import USFactorBase

logger = logging.getLogger(__name__)
logger.setLevel(LOG_LEVEL)


class EP(USFactorBase):
    """Earnings-to-Price: TTM EPS / adj_close"""
    name = "EP"
    description = "盈利收益率 (TTM EPS / 股价)"

    def compute(self, date: str, universe: pd.DataFrame) -> pd.DataFrame:
        tickers = universe["ticker"].tolist()
        ttm_eps = self.get_ttm_value(date, "eps", tickers)
        close = self.get_close_on_date(date, tickers)

        if ttm_eps.empty or close.empty:
            logger.debug("EP.compute: TTM EPS或收盘价数据为空")
            return pd.DataFrame(columns=["ticker", "factor_value"])

        df = ttm_eps.merge(close, on="ticker", how="inner")
        df["factor_value"] = np.where(
            (df["adj_close"] > 0) & df["ttm_value"].notna(),
            df["ttm_value"] / df["adj_close"],
            np.nan,
        )
        return df[["ticker", "factor_value"]]


class BP(USFactorBase):
    """Book-to-Price: total_equity / market_cap"""
    name = "BP"
    description = "账面价值比 (股东权益 / 市值)"

    def compute(self, date: str, universe: pd.DataFrame) -> pd.DataFrame:
        tickers = universe["ticker"].tolist()
        fin = self.get_latest_financial(date, ["total_equity"], tickers)
        mkcap = self.get_market_cap(date, tickers)

        if fin.empty or mkcap.empty:
            logger.debug("BP.compute: 财务数据或市值数据为空")
            return pd.DataFrame(columns=["ticker", "factor_value"])

        df = fin.merge(mkcap, on="ticker", how="inner")
        df["total_equity"] = pd.to_numeric(df["total_equity"], errors="coerce")
        df["factor_value"] = np.where(
            (df["market_cap"] > 0) & df["total_equity"].notna(),
            df["total_equity"] / df["market_cap"],
            np.nan,
        )
        return df[["ticker", "factor_value"]]


class DivYield(USFactorBase):
    """Dividend Yield: trailing 12M dividends / adj_close"""
    name = "DIV_YIELD"
    description = "股息率 (近12个月股息 / 股价)"

    def compute(self, date: str, universe: pd.DataFrame) -> pd.DataFrame:
        tickers = universe["ticker"].tolist()
        divs = self.get_dividends(date, lookback_days=365, universe_tickers=tickers)
        close = self.get_close_on_date(date, tickers)

        if divs.empty or close.empty:
            logger.debug("DivYield.compute: 股息数据或收盘价数据为空")
            return pd.DataFrame(columns=["ticker", "factor_value"])

        df = divs.merge(close, on="ticker", how="inner")
        df["factor_value"] = np.where(
            (df["adj_close"] > 0) & df["total_dividend"].notna(),
            df["total_dividend"] / df["adj_close"],
            np.nan,
        )
        return df[["ticker", "factor_value"]]
