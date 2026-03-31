"""美股动量因子: MOM_1M, MOM_3M, MOM_12M, REV_5D, RESIDUAL_MOM"""

import logging

import numpy as np
import pandas as pd

from services.config import LOG_LEVEL
from services.us_factors.base import USFactorBase

logger = logging.getLogger(__name__)
logger.setLevel(LOG_LEVEL)


class Mom1M(USFactorBase):
    """1-Month Momentum: adj_close(now) / adj_close(1M ago) - 1"""
    name = "MOM_1M"
    description = "1个月动量"

    def compute(self, date: str, universe: pd.DataFrame) -> pd.DataFrame:
        tickers_set = set(universe["ticker"].tolist())
        current = self._get_rolling_for_date(date, tickers_set)
        prev = self._get_month_end_adj_close(date, 1, tickers_set)

        if current is None or prev is None:
            return pd.DataFrame(columns=["ticker", "factor_value"])

        cur = current[["adj_close"]].reset_index().rename(columns={"adj_close": "cur_price"})
        prev = prev.rename(columns={"adj_close": "prev_price"})
        df = cur.merge(prev, on="ticker", how="inner")
        df["factor_value"] = np.where(
            df["prev_price"] > 0,
            df["cur_price"] / df["prev_price"] - 1,
            np.nan,
        )
        return df[["ticker", "factor_value"]]


class Mom3M(USFactorBase):
    """3-Month Momentum"""
    name = "MOM_3M"
    description = "3个月动量"

    def compute(self, date: str, universe: pd.DataFrame) -> pd.DataFrame:
        tickers_set = set(universe["ticker"].tolist())
        current = self._get_rolling_for_date(date, tickers_set)
        prev = self._get_month_end_adj_close(date, 3, tickers_set)

        if current is None or prev is None:
            return pd.DataFrame(columns=["ticker", "factor_value"])

        cur = current[["adj_close"]].reset_index().rename(columns={"adj_close": "cur_price"})
        prev = prev.rename(columns={"adj_close": "prev_price"})
        df = cur.merge(prev, on="ticker", how="inner")
        df["factor_value"] = np.where(
            df["prev_price"] > 0,
            df["cur_price"] / df["prev_price"] - 1,
            np.nan,
        )
        return df[["ticker", "factor_value"]]


class Mom12M(USFactorBase):
    """12-1 Month Momentum: price(1M ago) / price(12M ago) - 1 (skip recent month)"""
    name = "MOM_12M"
    description = "12-1个月动量 (跳过最近1月，避免短期反转)"

    def compute(self, date: str, universe: pd.DataFrame) -> pd.DataFrame:
        tickers_set = set(universe["ticker"].tolist())
        prev_1m = self._get_month_end_adj_close(date, 1, tickers_set)
        prev_12m = self._get_month_end_adj_close(date, 12, tickers_set)

        if prev_1m is None or prev_12m is None:
            return pd.DataFrame(columns=["ticker", "factor_value"])

        prev_1m = prev_1m.rename(columns={"adj_close": "p1m"})
        prev_12m = prev_12m.rename(columns={"adj_close": "p12m"})
        df = prev_1m.merge(prev_12m, on="ticker", how="inner")
        df["factor_value"] = np.where(
            df["p12m"] > 0,
            df["p1m"] / df["p12m"] - 1,
            np.nan,
        )
        return df[["ticker", "factor_value"]]


class Rev5D(USFactorBase):
    """5-Day Reversal: -1 * cumulative 5-day return"""
    name = "REV_5D"
    description = "5日反转因子"

    def compute(self, date: str, universe: pd.DataFrame) -> pd.DataFrame:
        tickers_set = set(universe["ticker"].tolist())
        rolling = self._get_rolling_for_date(date, tickers_set)

        if rolling is None:
            return pd.DataFrame(columns=["ticker", "factor_value"])

        df = rolling[["cum_ret_5d"]].reset_index()
        df["factor_value"] = -df["cum_ret_5d"]
        return df[["ticker", "factor_value"]]


class ResidualMom(USFactorBase):
    """Residual Momentum: stock 20D return - S&P 500 20D return"""
    name = "RESIDUAL_MOM"
    description = "残差动量 (个股20日收益 - S&P500 20日收益)"

    def compute(self, date: str, universe: pd.DataFrame) -> pd.DataFrame:
        tickers_set = set(universe["ticker"].tolist())
        rolling = self._get_rolling_for_date(date, tickers_set)

        if rolling is None:
            return pd.DataFrame(columns=["ticker", "factor_value"])

        # 获取 S&P 500 同期 20D 收益
        sp500_ret = self._get_sp500_20d_return(date)

        df = rolling[["cum_ret_20d"]].reset_index()
        df["factor_value"] = df["cum_ret_20d"] - sp500_ret
        return df[["ticker", "factor_value"]]

    def _get_sp500_20d_return(self, date: str) -> float:
        """获取 S&P 500 最近 20 个交易日的累计收益。"""
        cache_key = ("sp500_20d_ret", date)
        cached = self._date_cache.get(cache_key)
        if cached is not None:
            return cached

        bulk_daily = self._static_cache.get("_bulk_daily")
        date_ts = pd.to_datetime(date)

        # 尝试从 us_index_daily 获取
        try:
            idx_df = self.db.query(
                "SELECT trade_date, close FROM us_index_daily "
                "WHERE index_code = '^GSPC' AND trade_date <= :date "
                "ORDER BY trade_date DESC LIMIT 21",
                params={"date": date},
            )
            if len(idx_df) >= 2:
                prices = idx_df["close"].astype(float).values
                ret = prices[0] / prices[-1] - 1 if prices[-1] > 0 else 0.0
                self._date_cache[cache_key] = ret
                return ret
        except Exception:
            pass

        self._date_cache[cache_key] = 0.0
        return 0.0
