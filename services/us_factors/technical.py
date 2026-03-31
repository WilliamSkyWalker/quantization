"""美股技术因子: TURN_20D, VOL_20D, PRICE_DEV_60D, IVOL, SIZE, VOL_PRICE_DIV"""

import logging

import numpy as np
import pandas as pd

from services.config import LOG_LEVEL
from services.us_factors.base import USFactorBase

logger = logging.getLogger(__name__)
logger.setLevel(LOG_LEVEL)


class Turn20D(USFactorBase):
    """20-Day Average Dollar Volume (turnover proxy)"""
    name = "TURN_20D"
    description = "20日平均美元成交额 (流动性代理)"

    def compute(self, date: str, universe: pd.DataFrame) -> pd.DataFrame:
        tickers_set = set(universe["ticker"].tolist())
        rolling = self._get_rolling_for_date(date, tickers_set)

        if rolling is None:
            return pd.DataFrame(columns=["ticker", "factor_value"])

        df = rolling[["dvol_20d"]].reset_index()
        # 取 log 使分布更对称
        df["factor_value"] = np.log1p(df["dvol_20d"].clip(lower=0))
        return df[["ticker", "factor_value"]]


class Vol20D(USFactorBase):
    """20-Day Volatility (inverse: lower vol = higher factor value)"""
    name = "VOL_20D"
    description = "20日波动率 (取反，低波动优先)"

    def compute(self, date: str, universe: pd.DataFrame) -> pd.DataFrame:
        tickers_set = set(universe["ticker"].tolist())
        rolling = self._get_rolling_for_date(date, tickers_set)

        if rolling is None:
            return pd.DataFrame(columns=["ticker", "factor_value"])

        df = rolling[["vol_20d"]].reset_index()
        df["factor_value"] = -df["vol_20d"]
        return df[["ticker", "factor_value"]]


class PriceDev60D(USFactorBase):
    """Price Deviation from 60D MA (inverse: mean-reversion signal)"""
    name = "PRICE_DEV_60D"
    description = "价格偏离60日均线 (取反，均值回归信号)"

    def compute(self, date: str, universe: pd.DataFrame) -> pd.DataFrame:
        tickers_set = set(universe["ticker"].tolist())
        rolling = self._get_rolling_for_date(date, tickers_set)

        if rolling is None:
            return pd.DataFrame(columns=["ticker", "factor_value"])

        df = rolling[["adj_close", "ma60_adj"]].reset_index()
        df["factor_value"] = np.where(
            df["ma60_adj"] > 0,
            -((df["adj_close"] - df["ma60_adj"]) / df["ma60_adj"]),
            np.nan,
        )
        return df[["ticker", "factor_value"]]


class Ivol(USFactorBase):
    """Idiosyncratic Volatility: std of residuals from market regression (inverse)"""
    name = "IVOL"
    description = "特质波动率 (回归市场后残差波动率，取反)"

    def compute(self, date: str, universe: pd.DataFrame) -> pd.DataFrame:
        tickers = universe["ticker"].tolist()
        # 获取 60 日个股收益和 S&P 500 收益
        price_df = self.get_price_history(date, lookback_days=90, universe_tickers=tickers)
        if price_df.empty:
            return pd.DataFrame(columns=["ticker", "factor_value"])

        # S&P 500 收益
        try:
            idx_df = self.db.query(
                "SELECT trade_date, close FROM us_index_daily "
                "WHERE index_code = '^GSPC' AND trade_date <= :date "
                "ORDER BY trade_date DESC LIMIT 65",
                params={"date": date},
            )
            if idx_df.empty or len(idx_df) < 20:
                return pd.DataFrame(columns=["ticker", "factor_value"])
            idx_df = idx_df.sort_values("trade_date")
            idx_df["mkt_ret"] = idx_df["close"].astype(float).pct_change()
            mkt_rets = idx_df.set_index("trade_date")["mkt_ret"]
        except Exception:
            return pd.DataFrame(columns=["ticker", "factor_value"])

        # 个股收益
        price_df["adj_close"] = pd.to_numeric(price_df["adj_close"], errors="coerce")
        price_df = price_df.sort_values(["ticker", "trade_date"])
        price_df["ret"] = price_df.groupby("ticker")["adj_close"].pct_change()

        results = []
        for ticker, grp in price_df.groupby("ticker"):
            grp = grp.dropna(subset=["ret"]).tail(60)
            if len(grp) < 20:
                results.append({"ticker": ticker, "factor_value": np.nan})
                continue
            # 合并市场收益
            merged = grp.set_index("trade_date")[["ret"]].join(mkt_rets, how="inner")
            merged = merged.dropna()
            if len(merged) < 20:
                results.append({"ticker": ticker, "factor_value": np.nan})
                continue
            # OLS 回归: ret_i = alpha + beta * mkt_ret + epsilon
            x = merged["mkt_ret"].values
            y = merged["ret"].values
            x_mean = x.mean()
            y_mean = y.mean()
            beta = np.sum((x - x_mean) * (y - y_mean)) / (np.sum((x - x_mean) ** 2) + 1e-10)
            alpha = y_mean - beta * x_mean
            residuals = y - (alpha + beta * x)
            ivol = np.std(residuals)
            # 取反：低特质波动率 = 高因子值
            results.append({"ticker": ticker, "factor_value": -ivol})

        return pd.DataFrame(results)


class Size(USFactorBase):
    """Size: log(market_cap) — favors smaller stocks (inverse)"""
    name = "SIZE"
    description = "市值因子 (取反，偏好中小盘)"

    def compute(self, date: str, universe: pd.DataFrame) -> pd.DataFrame:
        tickers = universe["ticker"].tolist()
        mkcap = self.get_market_cap(date, tickers)

        if mkcap.empty:
            return pd.DataFrame(columns=["ticker", "factor_value"])

        df = mkcap.copy()
        df["factor_value"] = np.where(
            df["market_cap"] > 0,
            -np.log(df["market_cap"]),
            np.nan,
        )
        return df[["ticker", "factor_value"]]


class VolPriceDiv(USFactorBase):
    """Volume-Price Divergence: volume increase without price increase"""
    name = "VOL_PRICE_DIV"
    description = "量价背离 (放量不涨)"

    def compute(self, date: str, universe: pd.DataFrame) -> pd.DataFrame:
        tickers_set = set(universe["ticker"].tolist())
        rolling = self._get_rolling_for_date(date, tickers_set)

        if rolling is None:
            return pd.DataFrame(columns=["ticker", "factor_value"])

        df = rolling[["cum_ret_20d", "dvol_20d", "volume"]].reset_index()

        # 量价背离 = 成交额20日均值的变化 - 价格20日收益
        # 简化为：rank(dvol_20d) - rank(cum_ret_20d) 方向的信号
        # 正值 = 放量不涨（潜在卖出信号，取反）
        df["vol_rank"] = df["dvol_20d"].rank(pct=True)
        df["ret_rank"] = df["cum_ret_20d"].rank(pct=True)
        df["factor_value"] = -(df["vol_rank"] - df["ret_rank"])
        return df[["ticker", "factor_value"]]
