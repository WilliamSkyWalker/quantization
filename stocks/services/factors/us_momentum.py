"""美股动量因子: MOM_1M, MOM_3M, MOM_12M, REV_5D, RESIDUAL_MOM"""

import logging
from datetime import datetime, timedelta

import numpy as np
import polars as pl

from services.config import LOG_LEVEL
from stocks.services.factors.us_base import USFactorBase

logger = logging.getLogger(__name__)
logger.setLevel(LOG_LEVEL)

_EMPTY = pl.DataFrame(schema={"ticker": pl.Utf8, "factor_value": pl.Float64})


class Mom1M(USFactorBase):
    """1-Month Momentum: adj_close(now) / adj_close(1M ago) - 1"""
    name = "MOM_1M"
    description = "1个月动量"

    def compute(self, date: str, universe: pl.DataFrame) -> pl.DataFrame:
        tickers_set = set(universe["ticker"].to_list())
        current = self._get_rolling_for_date(date, tickers_set)
        prev = self._get_month_end_adj_close(date, 1, tickers_set)

        if current is None or prev is None:
            logger.debug("Mom1M.compute: 当前价格或1月前价格数据缺失")
            return _EMPTY.clone()

        cur = current.select(["ticker", "adj_close"]).rename({"adj_close": "cur_price"})
        prev = prev.rename({"adj_close": "prev_price"})
        df = cur.join(prev, on="ticker", how="inner")
        df = df.with_columns(
            pl.when(pl.col("prev_price") > 0)
            .then(pl.col("cur_price") / pl.col("prev_price") - 1)
            .otherwise(None)
            .alias("factor_value")
        )
        return df.select(["ticker", "factor_value"])


class Mom3M(USFactorBase):
    """3-Month Momentum"""
    name = "MOM_3M"
    description = "3个月动量"

    def compute(self, date: str, universe: pl.DataFrame) -> pl.DataFrame:
        tickers_set = set(universe["ticker"].to_list())
        current = self._get_rolling_for_date(date, tickers_set)
        prev = self._get_month_end_adj_close(date, 3, tickers_set)

        if current is None or prev is None:
            logger.debug("Mom3M.compute: 当前价格或3月前价格数据缺失")
            return _EMPTY.clone()

        cur = current.select(["ticker", "adj_close"]).rename({"adj_close": "cur_price"})
        prev = prev.rename({"adj_close": "prev_price"})
        df = cur.join(prev, on="ticker", how="inner")
        df = df.with_columns(
            pl.when(pl.col("prev_price") > 0)
            .then(pl.col("cur_price") / pl.col("prev_price") - 1)
            .otherwise(None)
            .alias("factor_value")
        )
        return df.select(["ticker", "factor_value"])


class Mom12M(USFactorBase):
    """12-1 Month Momentum: price(1M ago) / price(12M ago) - 1 (skip recent month)"""
    name = "MOM_12M"
    description = "12-1个月动量 (跳过最近1月，避免短期反转)"

    def compute(self, date: str, universe: pl.DataFrame) -> pl.DataFrame:
        tickers_set = set(universe["ticker"].to_list())
        prev_1m = self._get_month_end_adj_close(date, 1, tickers_set)
        prev_12m = self._get_month_end_adj_close(date, 12, tickers_set)

        if prev_1m is None or prev_12m is None:
            logger.debug("Mom12M.compute: 1月前或12月前价格数据缺失")
            return _EMPTY.clone()

        prev_1m = prev_1m.rename({"adj_close": "p1m"})
        prev_12m = prev_12m.rename({"adj_close": "p12m"})
        df = prev_1m.join(prev_12m, on="ticker", how="inner")
        df = df.with_columns(
            pl.when(pl.col("p12m") > 0)
            .then(pl.col("p1m") / pl.col("p12m") - 1)
            .otherwise(None)
            .alias("factor_value")
        )
        return df.select(["ticker", "factor_value"])


class Rev5D(USFactorBase):
    """5-Day Reversal: -1 * cumulative 5-day return"""
    name = "REV_5D"
    description = "5日反转因子"

    def compute(self, date: str, universe: pl.DataFrame) -> pl.DataFrame:
        tickers_set = set(universe["ticker"].to_list())
        rolling = self._get_rolling_for_date(date, tickers_set)

        if rolling is None:
            logger.debug("Rev5D.compute: 无rolling预计算数据")
            return _EMPTY.clone()

        df = rolling.select(["ticker", "cum_ret_5d"])
        df = df.with_columns(
            (-pl.col("cum_ret_5d")).alias("factor_value")
        )
        return df.select(["ticker", "factor_value"])


class ResidualMom(USFactorBase):
    """Residual Momentum: stock 20D return - S&P 500 20D return"""
    name = "RESIDUAL_MOM"
    description = "残差动量 (个股20日收益 - S&P500 20日收益)"

    def compute(self, date: str, universe: pl.DataFrame) -> pl.DataFrame:
        tickers_set = set(universe["ticker"].to_list())
        rolling = self._get_rolling_for_date(date, tickers_set)

        if rolling is None:
            logger.debug("ResidualMom.compute: 无rolling预计算数据")
            return _EMPTY.clone()

        # 获取 S&P 500 同期 20D 收益
        sp500_ret = self._get_sp500_20d_return(date)

        df = rolling.select(["ticker", "cum_ret_20d"])
        df = df.with_columns(
            (pl.col("cum_ret_20d") - sp500_ret).alias("factor_value")
        )
        return df.select(["ticker", "factor_value"])

    def _get_sp500_20d_return(self, date: str) -> float:
        """获取 S&P 500 最近 20 个交易日的累计收益。"""
        cache_key = ("sp500_20d_ret", date)
        cached = self._date_cache.get(cache_key)
        if cached is not None:
            return cached

        date_ts = datetime.strptime(date, "%Y-%m-%d") if isinstance(date, str) else date

        bulk_index = self._static_cache.get("_bulk_index")
        if bulk_index is not None and not bulk_index.is_empty():
            idx_df = (
                bulk_index
                .filter(pl.col("trade_date") <= date_ts)
                .sort("trade_date", descending=True)
                .head(21)
            )
            if idx_df.height >= 2:
                prices = idx_df["close"].cast(pl.Float64).to_numpy()
                ret = float(prices[0] / prices[-1] - 1) if prices[-1] > 0 else 0.0
                self._date_cache[cache_key] = ret
                return ret

        logger.debug("_get_sp500_20d_return: 缓存无 S&P 500 数据，使用默认值 0.0")
        self._date_cache[cache_key] = 0.0
        return 0.0
