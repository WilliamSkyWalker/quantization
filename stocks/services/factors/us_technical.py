"""美股技术因子: TURN_20D, VOL_20D, PRICE_DEV_60D, IVOL, SIZE, VOL_PRICE_DIV"""

import logging
from datetime import datetime, timedelta

import numpy as np
import polars as pl

from services.config import LOG_LEVEL
from stocks.services.factors.us_base import USFactorBase

logger = logging.getLogger(__name__)
logger.setLevel(LOG_LEVEL)

_EMPTY = pl.DataFrame(schema={"ticker": pl.Utf8, "factor_value": pl.Float64})


class Turn20D(USFactorBase):
    """20-Day Average Dollar Volume (turnover proxy)"""
    name = "TURN_20D"
    description = "20日平均美元成交额 (流动性代理)"

    def compute(self, date: str, universe: pl.DataFrame) -> pl.DataFrame:
        tickers_set = set(universe["ticker"].to_list())
        rolling = self._get_rolling_for_date(date, tickers_set)

        if rolling is None:
            logger.debug("Turn20D.compute: 无rolling预计算数据")
            return _EMPTY.clone()

        df = rolling.select(["ticker", "dvol_20d"])
        # 取 log 使分布更对称
        df = df.with_columns(
            pl.col("dvol_20d").clip(lower_bound=0).log1p().alias("factor_value")
        )
        return df.select(["ticker", "factor_value"])


class Vol20D(USFactorBase):
    """20-Day Volatility (inverse: lower vol = higher factor value)"""
    name = "VOL_20D"
    description = "20日波动率 (取反，低波动优先)"

    def compute(self, date: str, universe: pl.DataFrame) -> pl.DataFrame:
        tickers_set = set(universe["ticker"].to_list())
        rolling = self._get_rolling_for_date(date, tickers_set)

        if rolling is None:
            logger.debug("Vol20D.compute: 无rolling预计算数据")
            return _EMPTY.clone()

        df = rolling.select(["ticker", "vol_20d"])
        df = df.with_columns(
            (-pl.col("vol_20d")).alias("factor_value")
        )
        return df.select(["ticker", "factor_value"])


class PriceDev60D(USFactorBase):
    """Price Deviation from 60D MA (inverse: mean-reversion signal)"""
    name = "PRICE_DEV_60D"
    description = "价格偏离60日均线 (取反，均值回归信号)"

    def compute(self, date: str, universe: pl.DataFrame) -> pl.DataFrame:
        tickers_set = set(universe["ticker"].to_list())
        rolling = self._get_rolling_for_date(date, tickers_set)

        if rolling is None:
            logger.debug("PriceDev60D.compute: 无rolling预计算数据")
            return _EMPTY.clone()

        df = rolling.select(["ticker", "adj_close", "ma60_adj"])
        df = df.with_columns(
            pl.when(pl.col("ma60_adj") > 0)
            .then(-((pl.col("adj_close") - pl.col("ma60_adj")) / pl.col("ma60_adj")))
            .otherwise(None)
            .alias("factor_value")
        )
        return df.select(["ticker", "factor_value"])


class Ivol(USFactorBase):
    """Idiosyncratic Volatility: std of residuals from market regression (inverse)"""
    name = "IVOL"
    description = "特质波动率 (回归市场后残差波动率，取反)"

    def compute(self, date: str, universe: pl.DataFrame) -> pl.DataFrame:
        tickers = universe["ticker"].to_list()
        price_df = self.get_price_history(date, lookback_days=90, universe_tickers=tickers)
        if price_df.is_empty():
            logger.debug("Ivol.compute: 无历史价格数据")
            return _EMPTY.clone()

        # S&P 500 收益（优先从预加载缓存取）
        bulk_daily = self._static_cache.get("_bulk_daily")
        if bulk_daily is not None and not bulk_daily.is_empty():
            idx_cache = self._static_cache.get("_bulk_index")
            if idx_cache is not None:
                date_ts = datetime.strptime(date, "%Y-%m-%d") if isinstance(date, str) else date
                start_ts = date_ts - timedelta(days=90)
                idx_df = idx_cache.filter(
                    (pl.col("trade_date") >= start_ts) & (pl.col("trade_date") <= date_ts)
                )
            else:
                idx_df = pl.DataFrame()
        else:
            idx_df = pl.DataFrame()

        if idx_df.is_empty():
            logger.debug("Ivol.compute: 缓存无 S&P 500 数据")
            return _EMPTY.clone()

        if idx_df.height < 20:
            logger.debug("Ivol.compute: S&P500指数数据不足")
            return _EMPTY.clone()

        idx_df = idx_df.sort("trade_date")
        idx_df = idx_df.with_columns(pl.col("close").cast(pl.Float64, strict=False))
        # 计算市场收益率
        idx_close = idx_df["close"].to_numpy()
        idx_dates = idx_df["trade_date"].to_list()
        mkt_ret_arr = np.empty(len(idx_close))
        mkt_ret_arr[0] = np.nan
        mkt_ret_arr[1:] = idx_close[1:] / idx_close[:-1] - 1
        # 构建 date -> mkt_ret 映射
        mkt_map = {}
        for i, d in enumerate(idx_dates):
            if not np.isnan(mkt_ret_arr[i]):
                mkt_map[d] = mkt_ret_arr[i]

        # 个股收益（向量化）
        price_df = price_df.with_columns(pl.col("adj_close").cast(pl.Float64, strict=False))
        # 如果 adj_close 全为空，回退到 close
        if price_df["adj_close"].null_count() == price_df.height and "close" in price_df.columns:
            price_df = price_df.with_columns(pl.col("close").cast(pl.Float64, strict=False).alias("adj_close"))
        price_df = price_df.sort(["ticker", "trade_date"])

        # 转 pandas 做 pivot（polars pivot 支持有限，这里跨界使用 numpy 更高效）
        # 使用 numpy 手动构建矩阵
        all_tickers = price_df["ticker"].unique().sort().to_list()
        all_dates = sorted(set(price_df["trade_date"].to_list()) & set(mkt_map.keys()))
        # 取最近 60 个交易日
        all_dates = all_dates[-60:]
        if len(all_dates) < 20:
            logger.debug(f"Ivol.compute: 公共交易日不足({len(all_dates)}<20)")
            return _EMPTY.clone()

        # 构建 ticker -> {date -> adj_close} 映射
        ticker_col = price_df["ticker"].to_list()
        date_col = price_df["trade_date"].to_list()
        adj_col = price_df["adj_close"].to_numpy()

        price_map: dict[str, dict] = {}
        for i in range(len(ticker_col)):
            t = ticker_col[i]
            if t not in price_map:
                price_map[t] = {}
            price_map[t][date_col[i]] = adj_col[i]

        # 构建收益率矩阵 (T, N) 和市场收益向量 (T,)
        T = len(all_dates)
        valid_tickers = []
        ret_cols = []

        for tk in all_tickers:
            pm = price_map.get(tk)
            if pm is None:
                continue
            prices_arr = np.array([pm.get(d, np.nan) for d in all_dates])
            ret_arr = np.empty(T)
            ret_arr[0] = np.nan
            mask = (prices_arr[:-1] > 0) & ~np.isnan(prices_arr[:-1]) & ~np.isnan(prices_arr[1:])
            ret_arr[1:] = np.where(mask, prices_arr[1:] / prices_arr[:-1] - 1, np.nan)
            valid_tickers.append(tk)
            ret_cols.append(ret_arr)

        if not valid_tickers:
            return _EMPTY.clone()

        ret_vals = np.column_stack(ret_cols)  # (T, N)
        mkt = np.array([mkt_map.get(d, np.nan) for d in all_dates])  # (T,)

        # 向量化 OLS: beta = cov(x,y)/var(x), residual = y - alpha - beta*x
        mkt_dm = mkt - np.nanmean(mkt)  # demeaned market
        var_mkt = np.nansum(mkt_dm ** 2) + 1e-10

        ret_dm = ret_vals - np.nanmean(ret_vals, axis=0, keepdims=True)

        # beta for each stock: (T,) dot (T, N) / var
        betas = np.nansum(mkt_dm[:, None] * ret_dm, axis=0) / var_mkt  # (N,)
        alphas = np.nanmean(ret_vals, axis=0) - betas * np.nanmean(mkt)  # (N,)

        # residuals and std
        predicted = alphas[None, :] + betas[None, :] * mkt[:, None]  # (T, N)
        residuals = ret_vals - predicted
        ivol_values = np.nanstd(residuals, axis=0)  # (N,)

        # 有效样本数检查
        valid_count = np.sum(~np.isnan(ret_vals), axis=0)
        ivol_values[valid_count < 20] = np.nan

        result = pl.DataFrame({
            "ticker": valid_tickers,
            "factor_value": (-ivol_values).tolist(),  # 取反：低特质波动率 = 高因子值
        })
        return result.filter(pl.col("factor_value").is_not_null())


class Size(USFactorBase):
    """Size: log(market_cap) — favors smaller stocks (inverse)"""
    name = "SIZE"
    description = "市值因子 (取反，偏好中小盘)"

    def compute(self, date: str, universe: pl.DataFrame) -> pl.DataFrame:
        tickers = universe["ticker"].to_list()
        mkcap = self.get_market_cap(date, tickers)

        if mkcap.is_empty():
            logger.debug("Size.compute: 无市值数据")
            return _EMPTY.clone()

        df = mkcap.with_columns(
            pl.when(pl.col("market_cap") > 0)
            .then(-pl.col("market_cap").log())
            .otherwise(None)
            .alias("factor_value")
        )
        return df.select(["ticker", "factor_value"])


class VolPriceDiv(USFactorBase):
    """Volume-Price Divergence: volume increase without price increase"""
    name = "VOL_PRICE_DIV"
    description = "量价背离 (放量不涨)"

    def compute(self, date: str, universe: pl.DataFrame) -> pl.DataFrame:
        tickers_set = set(universe["ticker"].to_list())
        rolling = self._get_rolling_for_date(date, tickers_set)

        if rolling is None:
            logger.debug("VolPriceDiv.compute: 无rolling预计算数据")
            return _EMPTY.clone()

        df = rolling.select(["ticker", "cum_ret_20d", "dvol_20d", "volume"])

        # 量价背离 = 成交额20日均值的变化 - 价格20日收益
        # 简化为：rank(dvol_20d) - rank(cum_ret_20d) 方向的信号
        # 正值 = 放量不涨（潜在卖出信号，取反）
        df = df.with_columns([
            pl.col("dvol_20d").rank().alias("vol_rank"),
            pl.col("cum_ret_20d").rank().alias("ret_rank"),
        ])
        # Normalize ranks to percentile [0, 1]
        n = df.height
        if n > 0:
            df = df.with_columns([
                (pl.col("vol_rank") / n).alias("vol_rank"),
                (pl.col("ret_rank") / n).alias("ret_rank"),
            ])
        df = df.with_columns(
            (-(pl.col("vol_rank") - pl.col("ret_rank"))).alias("factor_value")
        )
        return df.select(["ticker", "factor_value"])
