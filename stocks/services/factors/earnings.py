"""美股盈利因子: EARNINGS_SURPRISE, EPS_REVISION"""

import logging

import numpy as np
import pandas as pd

from services.config import LOG_LEVEL
from stocks.services.factors.base import USFactorBase

logger = logging.getLogger(__name__)
logger.setLevel(LOG_LEVEL)


class EarningsSurprise(USFactorBase):
    """
    Earnings Surprise: 最近一次盈利惊喜百分比。

    正向因子：surprise_pct > 0 → 实际 EPS 超预期 → 看多。
    Post-earnings announcement drift (PEAD) 是最强截面异象之一。

    数据来源: FMP API → us_earnings_surprise 表
    """
    name = "EARNINGS_SURPRISE"
    description = "盈利惊喜百分比 (actual - estimated) / |estimated|"

    # 回看窗口：只取最近 120 天内公布的最近一次 earnings
    _LOOKBACK_DAYS = 120

    def compute(self, date: str, universe: pd.DataFrame) -> pd.DataFrame:
        tickers = universe["ticker"].tolist()
        date_ts = pd.to_datetime(date)
        start_ts = date_ts - pd.Timedelta(days=self._LOOKBACK_DAYS)

        # 从预加载数据获取
        bulk = self._static_cache.get("_bulk_earnings_surprise")
        if bulk is None or bulk.empty:
            logger.debug("EarningsSurprise.compute: 缓存为空")
            return pd.DataFrame(columns=["ticker", "factor_value"])
        mask = (bulk["date"] >= start_ts) & (bulk["date"] <= date_ts)
        df = bulk[mask].copy()

        if df.empty:
            logger.debug("EarningsSurprise.compute: 无盈利惊喜数据")
            return pd.DataFrame(columns=["ticker", "factor_value"])

        if tickers:
            df = df[df["ticker"].isin(tickers)]

        df = df.dropna(subset=["surprise_pct"])
        if df.empty:
            logger.debug("EarningsSurprise.compute: surprise_pct 全部为空")
            return pd.DataFrame(columns=["ticker", "factor_value"])

        # 取每只股票最近一次的 surprise_pct
        df = df.sort_values("date", ascending=False).drop_duplicates(
            subset=["ticker"], keep="first"
        )

        result = df[["ticker", "surprise_pct"]].copy()
        result.columns = ["ticker", "factor_value"]
        return result


class EpsRevision(USFactorBase):
    """
    EPS Revision: 比较最近两个已过去 fiscal period 的 EPS 共识变化方向。

    正向因子：分析师上调 EPS → 看多。

    防前瞻：只使用 date <= current_date 的已过去 fiscal period 数据。
    FMP analyst-estimates 的 date 是 fiscal period end date，无快照时间戳，
    因此只能假设"已过去的 fiscal period 的共识是当时可获取的"。

    TODO: 接入 point-in-time 数据源（Refinitiv IBES / 每日快照积累）后，
    改为比较同一 fiscal period 在不同 snapshot_date 的共识变化。
    """
    name = "EPS_REVISION"
    description = "EPS 共识预期修正 (最近季 vs 上一季，仅用已过去 fiscal period)"

    def compute(self, date: str, universe: pd.DataFrame) -> pd.DataFrame:
        tickers = universe["ticker"].tolist()
        date_ts = pd.to_datetime(date)

        bulk = self._static_cache.get("_bulk_eps_estimate")
        if bulk is None or bulk.empty:
            logger.debug("EpsRevision.compute: 缓存为空")
            return pd.DataFrame(columns=["ticker", "factor_value"])

        df = bulk.copy()
        df["date"] = pd.to_datetime(df["date"])
        if tickers:
            df = df[df["ticker"].isin(tickers)]

        if df.empty:
            logger.debug("EpsRevision.compute: 过滤 ticker 后无数据")
            return pd.DataFrame(columns=["ticker", "factor_value"])

        # 只用已过去的 fiscal period（防前瞻）
        df = df[df["date"] <= date_ts].copy()
        if df.empty:
            logger.debug("EpsRevision.compute: 无已过去 fiscal period 的 EPS 数据")
            return pd.DataFrame(columns=["ticker", "factor_value"])

        # 每只股票取最近 2 个 fiscal period
        df = df.sort_values("date", ascending=False)
        df["rank"] = df.groupby("ticker").cumcount()
        recent = df[df["rank"] == 0][["ticker", "estimated_eps_avg"]].copy()
        prev = df[df["rank"] == 1][["ticker", "estimated_eps_avg"]].copy()

        if recent.empty or prev.empty:
            logger.debug("EpsRevision.compute: 不足 2 个 fiscal period")
            return pd.DataFrame(columns=["ticker", "factor_value"])

        merged = recent.merge(
            prev, on="ticker", suffixes=("_recent", "_prev"),
        )

        if merged.empty:
            return pd.DataFrame(columns=["ticker", "factor_value"])

        # Revision = (recent - prev) / |prev|
        merged["factor_value"] = np.where(
            merged["estimated_eps_avg_prev"].abs() > 0.01,
            (merged["estimated_eps_avg_recent"] - merged["estimated_eps_avg_prev"])
            / merged["estimated_eps_avg_prev"].abs(),
            0.0,
        )

        return merged[["ticker", "factor_value"]]
