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

        hist = self.fetch_price_history(
            date, tickers, lookback_days=self._LOOKBACK_DAYS, columns=["adj_close"]
        )
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

        hist = self.fetch_price_history(
            date, tickers, lookback_days=self._LOOKBACK_DAYS, columns=["volume"]
        )
        if hist.empty:
            logger.warning(f"VolumeRatio({date}): 无数据")
            return pd.DataFrame(columns=["ticker", "factor_value"])

        rows = []
        for ticker, grp in hist.groupby("ticker", sort=False):
            vol = grp["volume"].dropna().values
            if len(vol) < self._MIN_DAYS:
                continue

            avg_5 = vol[-5:].mean()
            avg_20 = vol[-20:].mean()
            if avg_20 < 1e-10:
                continue

            rows.append({"ticker": ticker, "factor_value": float(avg_5 / avg_20)})

        if not rows:
            logger.warning(f"VolumeRatio({date}): 无有效 ticker")
            return pd.DataFrame(columns=["ticker", "factor_value"])

        out = pd.DataFrame(rows)
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

        hist = self.fetch_price_history(
            date, tickers, lookback_days=self._LOOKBACK_DAYS, columns=["adj_close"]
        )
        if hist.empty:
            logger.warning(f"Volatility21d({date}): 无数据")
            return pd.DataFrame(columns=["ticker", "factor_value"])

        rows = []
        for ticker, grp in hist.groupby("ticker", sort=False):
            prices = grp["adj_close"].dropna().values
            if len(prices) < self._MIN_DAYS:
                continue

            rets = np.diff(prices) / prices[:-1]
            vol = float(np.std(rets, ddof=1) * np.sqrt(252))
            rows.append({"ticker": ticker, "factor_value": vol})

        if not rows:
            logger.warning(f"Volatility21d({date}): 无有效 ticker")
            return pd.DataFrame(columns=["ticker", "factor_value"])

        out = pd.DataFrame(rows)
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

        hist = self.fetch_price_history(
            date, tickers, lookback_days=self._LOOKBACK_DAYS,
            columns=["adj_close", "volume"],
        )
        if hist.empty:
            logger.warning(f"PVTrend({date}): 无数据")
            return pd.DataFrame(columns=["ticker", "factor_value"])

        rows = []
        for ticker, grp in hist.groupby("ticker", sort=False):
            grp = grp.dropna(subset=["adj_close", "volume"])
            if len(grp) < self._MIN_DAYS:
                continue

            prices = grp["adj_close"].values
            volume = grp["volume"].values

            rets = np.diff(prices) / prices[:-1]
            pvt = np.sum(rets * volume[1:])

            # 归一化：/ 平均成交量
            avg_vol = volume.mean()
            if avg_vol < 1e-10:
                continue

            pvt_norm = pvt / avg_vol
            rows.append({"ticker": ticker, "factor_value": float(pvt_norm)})

        if not rows:
            logger.warning(f"PVTrend({date}): 无有效 ticker")
            return pd.DataFrame(columns=["ticker", "factor_value"])

        out = pd.DataFrame(rows)
        logger.info(f"PVTrend({date}): {len(out)} 有值")
        return out
