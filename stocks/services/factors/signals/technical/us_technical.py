"""Technical 因子集合

4 个因子，全部基于 USDailyPrice（价格 + 成交量）：

1. RSI_14         — 14 日 RSI（0, 由 IC 决定）
2. VOLUME_RATIO   — 5 日均量 / 20 日均量，异常放量 (-1)
3. VOLATILITY_21D — 21 日已实现波动率 (-1, 低波动异象)
4. PV_TREND       — Price-Volume Trend，价量背离信号 (0)
"""

import logging

import numpy as np
import pandas as pd

from services.config import LOG_LEVEL
from stocks.services.factors.us_registry import AlphaSignal, register

logger = logging.getLogger(__name__)
logger.setLevel(LOG_LEVEL)


# ---------------------------------------------------------------------------
# 1. RSI (14-day)
# ---------------------------------------------------------------------------


@register
class Rsi14(AlphaSignal):
    """RSI-14 — 14 日相对强弱指标。

    RSI = 100 - 100/(1 + RS)，RS = avg_gain / avg_loss (Wilder EMA)。
    学术上 RSI 有短期反转效应（高 RSI → 超买 → 回调），但也有动量延续。
    设 direction=0 让 IC 决定。
    """

    name = "RSI_14"
    version = "v1"
    category = "technical"
    horizon = "week"
    expected_icir = 0.05
    status = "staging"
    inherent_direction = 0
    data_deps = ["us_daily_price"]
    ic_window_months = 6

    _LOOKBACK_DAYS = 40  # 14 交易日 + buffer
    _PERIOD = 14

    def compute(self, date: str, universe: pd.DataFrame) -> pd.DataFrame:
        tickers = universe["ticker"].tolist()
        if not tickers:
            logger.debug("RSI14: 空 universe")
            return pd.DataFrame(columns=["ticker", "factor_value"])

        bulk_daily = self._static_cache.get("_bulk_daily")
        if bulk_daily is None or bulk_daily.empty:
            logger.warning(f"RSI14({date}): 无预加载数据")
            return pd.DataFrame(columns=["ticker", "factor_value"])
        date_ts = pd.to_datetime(date)
        start_ts = date_ts - pd.Timedelta(days=self._LOOKBACK_DAYS)
        mask = (
            bulk_daily["ticker"].isin(set(tickers))
            & (bulk_daily["trade_date"] >= start_ts)
            & (bulk_daily["trade_date"] <= date_ts)
        )
        hist = bulk_daily.loc[mask, ["ticker", "trade_date", "adj_close"]].dropna(subset=["adj_close"])
        if hist.empty:
            logger.warning(f"RSI14({date}): 无价格数据")
            return pd.DataFrame(columns=["ticker", "factor_value"])

        rows = []
        for ticker, grp in hist.groupby("ticker", sort=False):
            prices = grp["adj_close"].dropna().values
            if len(prices) < self._PERIOD + 1:
                continue

            deltas = np.diff(prices)
            gains = np.where(deltas > 0, deltas, 0.0)
            losses = np.where(deltas < 0, -deltas, 0.0)

            # Wilder EMA: 先取前 N 天均值，再指数平滑
            avg_gain = gains[:self._PERIOD].mean()
            avg_loss = losses[:self._PERIOD].mean()

            for i in range(self._PERIOD, len(gains)):
                avg_gain = (avg_gain * (self._PERIOD - 1) + gains[i]) / self._PERIOD
                avg_loss = (avg_loss * (self._PERIOD - 1) + losses[i]) / self._PERIOD

            if avg_loss < 1e-15:
                rsi = 100.0
            else:
                rs = avg_gain / avg_loss
                rsi = 100.0 - 100.0 / (1.0 + rs)

            rows.append({"ticker": ticker, "factor_value": float(rsi)})

        if not rows:
            logger.warning(f"RSI14({date}): 无有效 ticker")
            return pd.DataFrame(columns=["ticker", "factor_value"])

        out = pd.DataFrame(rows)
        logger.info(f"RSI14({date}): {len(out)} 有值")
        return out


# ---------------------------------------------------------------------------
# 2. Volume Ratio (5d / 20d)
# ---------------------------------------------------------------------------


@register
class VolumeRatio(AlphaSignal):
    """Volume Ratio — 5 日均量 / 20 日均量。

    高 volume ratio = 近期异常放量 → 通常伴随价格剧变（信息事件）。
    学术上高 turnover 后续回报偏低（Baker-Stein 2004: high turnover = disagreement）。
    设 direction=-1。
    """

    name = "VOLUME_RATIO"
    version = "v1"
    category = "technical"
    horizon = "week"
    expected_icir = 0.06
    status = "staging"
    inherent_direction = -1
    data_deps = ["us_daily_price"]
    ic_window_months = 6

    _LOOKBACK_DAYS = 35
    _MIN_DAYS = 20

    def compute(self, date: str, universe: pd.DataFrame) -> pd.DataFrame:
        tickers = universe["ticker"].tolist()
        if not tickers:
            logger.debug("VolumeRatio: 空 universe")
            return pd.DataFrame(columns=["ticker", "factor_value"])

        # 直接从 _bulk_daily 切最近 5 天和 20 天成交量，向量化聚合
        bulk_daily = self._static_cache.get("_bulk_daily")
        if bulk_daily is None or bulk_daily.empty:
            logger.warning(f"VolumeRatio({date}): 无预加载数据")
            return pd.DataFrame(columns=["ticker", "factor_value"])

        date_ts = pd.to_datetime(date)
        ticker_set = set(tickers)

        # 20 天窗口
        start_20 = date_ts - pd.Timedelta(days=self._LOOKBACK_DAYS)
        mask_20 = (
            bulk_daily["ticker"].isin(ticker_set)
            & (bulk_daily["trade_date"] >= start_20)
            & (bulk_daily["trade_date"] <= date_ts)
        )
        vol_20 = bulk_daily.loc[mask_20, ["ticker", "trade_date", "volume"]].dropna()
        if vol_20.empty:
            logger.warning(f"VolumeRatio({date}): 无成交量数据")
            return pd.DataFrame(columns=["ticker", "factor_value"])

        vol_20["volume"] = vol_20["volume"].astype(float)

        # 每 ticker 的 20d 均量
        avg_20 = vol_20.groupby("ticker").agg(
            avg_vol_20=("volume", "mean"),
            n_days=("volume", "count"),
        ).reset_index()
        avg_20 = avg_20[avg_20["n_days"] >= self._MIN_DAYS]

        # 5 天窗口（从 20 天数据里取最近 5 天）
        start_5 = date_ts - pd.Timedelta(days=8)
        vol_5 = vol_20[vol_20["trade_date"] >= start_5]
        avg_5 = vol_5.groupby("ticker")["volume"].mean().reset_index()
        avg_5.columns = ["ticker", "avg_vol_5"]

        merged = avg_20.merge(avg_5, on="ticker", how="inner")
        merged = merged[merged["avg_vol_20"] > 1e-10]
        merged["factor_value"] = merged["avg_vol_5"] / merged["avg_vol_20"]

        out = merged[["ticker", "factor_value"]].copy()
        logger.info(f"VolumeRatio({date}): {len(out)} 有值")
        return out


# ---------------------------------------------------------------------------
# 3. Realized Volatility (21-day)
# ---------------------------------------------------------------------------


@register
class Volatility21d(AlphaSignal):
    """21-day Realized Volatility — 短期已实现波动率。

    σ = std(daily_returns) × √252（年化）。
    低波动异象 (Ang et al. 2006): 高 idio vol 股票后续回报更低。
    反向因子。
    """

    name = "VOLATILITY_21D"
    version = "v1"
    category = "technical"
    horizon = "month"
    expected_icir = 0.10
    status = "staging"
    inherent_direction = -1
    data_deps = ["us_daily_price"]
    ic_window_months = 12

    _LOOKBACK_DAYS = 35
    _MIN_DAYS = 15

    def compute(self, date: str, universe: pd.DataFrame) -> pd.DataFrame:
        tickers = universe["ticker"].tolist()
        if not tickers:
            logger.debug("Volatility21d: 空 universe")
            return pd.DataFrame(columns=["ticker", "factor_value"])

        # 直接用预算好的 _rolling_indexed 里的 vol_20d（年化）
        rolling = self._get_rolling_for_date(date, set(tickers))
        if rolling is None or rolling.empty:
            logger.warning(f"Volatility21d({date}): 无 rolling 数据")
            return pd.DataFrame(columns=["ticker", "factor_value"])

        df = rolling[["vol_20d"]].dropna().reset_index()
        df["factor_value"] = df["vol_20d"] * np.sqrt(252)
        out = df[["ticker", "factor_value"]].copy()
        logger.info(f"Volatility21d({date}): {len(out)} 有值")
        return out


# ---------------------------------------------------------------------------
# 4. Price-Volume Trend (PVT)
# ---------------------------------------------------------------------------


@register
class PriceVolumeTrend(AlphaSignal):
    """Price-Volume Trend — 价量趋势。

    PVT = Σ [ (P_t - P_{t-1}) / P_{t-1} × Volume_t ]  (过去 21 天)

    正 PVT = 价涨量增（趋势确认）; 负 PVT = 价跌量增（恐慌抛售）。
    归一化：PVT / 20 日均量 → 无量纲。
    方向不定，由 IC 决定。
    """

    name = "PV_TREND"
    version = "v1"
    category = "technical"
    horizon = "week"
    expected_icir = 0.05
    status = "staging"
    inherent_direction = 0
    data_deps = ["us_daily_price"]
    ic_window_months = 6

    _LOOKBACK_DAYS = 35
    _MIN_DAYS = 15

    def compute(self, date: str, universe: pd.DataFrame) -> pd.DataFrame:
        tickers = universe["ticker"].tolist()
        if not tickers:
            logger.debug("PVTrend: 空 universe")
            return pd.DataFrame(columns=["ticker", "factor_value"])

        # 直接从 _bulk_daily 切片（避免 fetch_price_history 开销）
        bulk_daily = self._static_cache.get("_bulk_daily")
        if bulk_daily is None or bulk_daily.empty:
            logger.warning(f"PVTrend({date}): 无预加载数据")
            return pd.DataFrame(columns=["ticker", "factor_value"])

        date_ts = pd.to_datetime(date)
        start_ts = date_ts - pd.Timedelta(days=self._LOOKBACK_DAYS)
        mask = (
            bulk_daily["ticker"].isin(set(tickers))
            & (bulk_daily["trade_date"] >= start_ts)
            & (bulk_daily["trade_date"] <= date_ts)
        )
        hist = bulk_daily.loc[mask, ["ticker", "adj_close", "volume"]].dropna()
        if hist.empty:
            logger.warning(f"PVTrend({date}): 无数据")
            return pd.DataFrame(columns=["ticker", "factor_value"])

        # 向量化：groupby 算收益 × 成交量
        hist = hist.sort_values(["ticker", "adj_close"])
        hist["ret"] = hist.groupby("ticker")["adj_close"].pct_change()
        hist["ret_x_vol"] = hist["ret"] * hist["volume"]

        agg = hist.dropna(subset=["ret"]).groupby("ticker").agg(
            pvt_sum=("ret_x_vol", "sum"),
            avg_vol=("volume", "mean"),
            n_days=("ret", "count"),
        ).reset_index()

        agg = agg[(agg["n_days"] >= self._MIN_DAYS) & (agg["avg_vol"] > 1e-10)]
        agg["factor_value"] = agg["pvt_sum"] / agg["avg_vol"]

        out = agg[["ticker", "factor_value"]].copy()
        if out.empty:
            logger.warning(f"PVTrend({date}): 无有效 ticker")
            return pd.DataFrame(columns=["ticker", "factor_value"])

        out = pd.DataFrame(rows)
        logger.info(f"PVTrend({date}): {len(out)} 有值")
        return out
