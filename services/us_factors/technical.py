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
            logger.debug("Turn20D.compute: 无rolling预计算数据")
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
            logger.debug("Vol20D.compute: 无rolling预计算数据")
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
            logger.debug("PriceDev60D.compute: 无rolling预计算数据")
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
        price_df = self.get_price_history(date, lookback_days=90, universe_tickers=tickers)
        if price_df.empty:
            logger.debug("Ivol.compute: 无历史价格数据")
            return pd.DataFrame(columns=["ticker", "factor_value"])

        # S&P 500 收益（优先从预加载缓存取）
        bulk_daily = self._static_cache.get("_bulk_daily")
        if bulk_daily is not None and not bulk_daily.empty:
            idx_cache = self._static_cache.get("_bulk_index")
            if idx_cache is not None:
                date_ts = pd.to_datetime(date)
                start_ts = date_ts - pd.Timedelta(days=90)
                idx_df = idx_cache[
                    (idx_cache["trade_date"] >= start_ts) & (idx_cache["trade_date"] <= date_ts)
                ].copy()
            else:
                idx_df = pd.DataFrame()
        else:
            idx_df = pd.DataFrame()

        if idx_df.empty:
            logger.debug("Ivol.compute: 缓存无 S&P 500 数据")
            return pd.DataFrame(columns=["ticker", "factor_value"])

        if idx_df.empty or len(idx_df) < 20:
            logger.debug("Ivol.compute: S&P500指数数据不足")
            return pd.DataFrame(columns=["ticker", "factor_value"])

        idx_df = idx_df.sort_values("trade_date")
        idx_df["close"] = pd.to_numeric(idx_df["close"], errors="coerce")
        idx_df["mkt_ret"] = idx_df["close"].pct_change()
        mkt_series = idx_df.set_index("trade_date")["mkt_ret"].dropna()

        # 个股收益（向量化）
        price_df["adj_close"] = pd.to_numeric(price_df["adj_close"], errors="coerce")
        if price_df["adj_close"].notna().sum() == 0 and "close" in price_df.columns:
            price_df["adj_close"] = pd.to_numeric(price_df["close"], errors="coerce")
        price_df = price_df.sort_values(["ticker", "trade_date"])
        price_df["ret"] = price_df.groupby("ticker")["adj_close"].pct_change()

        # Pivot 成矩阵：行=trade_date, 列=ticker, 值=ret
        ret_pivot = price_df.pivot_table(index="trade_date", columns="ticker", values="ret")
        # 取最近 60 个交易日
        common_dates = ret_pivot.index.intersection(mkt_series.index)
        common_dates = sorted(common_dates)[-60:]
        if len(common_dates) < 20:
            logger.debug(f"Ivol.compute: 公共交易日不足({len(common_dates)}<20)")
            return pd.DataFrame(columns=["ticker", "factor_value"])

        ret_mat = ret_pivot.loc[common_dates]  # (T, N)
        mkt = mkt_series.loc[common_dates].values  # (T,)

        # 向量化 OLS: beta = cov(x,y)/var(x), residual = y - alpha - beta*x
        mkt_dm = mkt - mkt.mean()  # demeaned market
        var_mkt = (mkt_dm ** 2).sum() + 1e-10

        ret_vals = ret_mat.values  # (T, N)
        ret_dm = ret_vals - np.nanmean(ret_vals, axis=0, keepdims=True)

        # beta for each stock: (T,) dot (T, N) / var
        betas = np.nansum(mkt_dm[:, None] * ret_dm, axis=0) / var_mkt  # (N,)
        alphas = np.nanmean(ret_vals, axis=0) - betas * mkt.mean()  # (N,)

        # residuals and std
        predicted = alphas[None, :] + betas[None, :] * mkt[:, None]  # (T, N)
        residuals = ret_vals - predicted
        ivol_values = np.nanstd(residuals, axis=0)  # (N,)

        # 有效样本数检查
        valid_count = np.sum(~np.isnan(ret_vals), axis=0)
        ivol_values[valid_count < 20] = np.nan

        result = pd.DataFrame({
            "ticker": ret_mat.columns,
            "factor_value": -ivol_values,  # 取反：低特质波动率 = 高因子值
        })
        return result[result["factor_value"].notna()].reset_index(drop=True)


class Size(USFactorBase):
    """Size: log(market_cap) — favors smaller stocks (inverse)"""
    name = "SIZE"
    description = "市值因子 (取反，偏好中小盘)"

    def compute(self, date: str, universe: pd.DataFrame) -> pd.DataFrame:
        tickers = universe["ticker"].tolist()
        mkcap = self.get_market_cap(date, tickers)

        if mkcap.empty:
            logger.debug("Size.compute: 无市值数据")
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
            logger.debug("VolPriceDiv.compute: 无rolling预计算数据")
            return pd.DataFrame(columns=["ticker", "factor_value"])

        df = rolling[["cum_ret_20d", "dvol_20d", "volume"]].reset_index()

        # 量价背离 = 成交额20日均值的变化 - 价格20日收益
        # 简化为：rank(dvol_20d) - rank(cum_ret_20d) 方向的信号
        # 正值 = 放量不涨（潜在卖出信号，取反）
        df["vol_rank"] = df["dvol_20d"].rank(pct=True)
        df["ret_rank"] = df["cum_ret_20d"].rank(pct=True)
        df["factor_value"] = -(df["vol_rank"] - df["ret_rank"])
        return df[["ticker", "factor_value"]]
