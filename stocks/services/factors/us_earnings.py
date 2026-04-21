"""美股盈利因子: EARNINGS_SURPRISE, EPS_REVISION"""

import logging
from datetime import datetime, timedelta

import numpy as np
import polars as pl

from services.config import LOG_LEVEL
from stocks.services.factors.us_base import USFactorBase

logger = logging.getLogger(__name__)
logger.setLevel(LOG_LEVEL)

_EMPTY = pl.DataFrame(schema={"ticker": pl.Utf8, "factor_value": pl.Float64})


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

    def compute(self, date: str, universe: pl.DataFrame) -> pl.DataFrame:
        tickers = universe["ticker"].to_list()
        date_ts = datetime.strptime(date, "%Y-%m-%d") if isinstance(date, str) else date
        start_ts = date_ts - timedelta(days=self._LOOKBACK_DAYS)

        # 从预加载数据获取
        bulk = self._static_cache.get("_bulk_earnings_surprise")
        if bulk is None or bulk.is_empty():
            logger.debug("EarningsSurprise.compute: 缓存为空")
            return _EMPTY.clone()
        df = bulk.filter(
            (pl.col("date") >= start_ts) & (pl.col("date") <= date_ts)
        )

        if df.is_empty():
            logger.debug("EarningsSurprise.compute: 无盈利惊喜数据")
            return _EMPTY.clone()

        if tickers:
            df = df.filter(pl.col("ticker").is_in(tickers))

        df = df.drop_nulls(subset=["surprise_pct"])
        if df.is_empty():
            logger.debug("EarningsSurprise.compute: surprise_pct 全部为空")
            return _EMPTY.clone()

        # 取每只股票最近一次的 surprise_pct
        df = (
            df.sort("date", descending=True)
            .unique(subset=["ticker"], keep="first")
        )

        result = df.select([
            pl.col("ticker"),
            pl.col("surprise_pct").alias("factor_value"),
        ])
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

    def compute(self, date: str, universe: pl.DataFrame) -> pl.DataFrame:
        tickers = universe["ticker"].to_list()
        date_ts = datetime.strptime(date, "%Y-%m-%d") if isinstance(date, str) else date

        bulk = self._static_cache.get("_bulk_eps_estimate")
        if bulk is None or bulk.is_empty():
            logger.debug("EpsRevision.compute: 缓存为空")
            return _EMPTY.clone()

        df = bulk.clone()
        df = df.with_columns(pl.col("date").cast(pl.Date, strict=False))
        if tickers:
            df = df.filter(pl.col("ticker").is_in(tickers))

        if df.is_empty():
            logger.debug("EpsRevision.compute: 过滤 ticker 后无数据")
            return _EMPTY.clone()

        # 只用已过去的 fiscal period（防前瞻）
        df = df.filter(pl.col("date") <= date_ts)
        if df.is_empty():
            logger.debug("EpsRevision.compute: 无已过去 fiscal period 的 EPS 数据")
            return _EMPTY.clone()

        # 每只股票取最近 2 个 fiscal period
        df = df.sort("date", descending=True)
        df = df.with_columns(
            pl.lit(1).cum_sum().over("ticker").alias("rank") - 1
        )
        recent = (
            df.filter(pl.col("rank") == 0)
            .select(["ticker", "estimated_eps_avg"])
            .rename({"estimated_eps_avg": "estimated_eps_avg_recent"})
        )
        prev = (
            df.filter(pl.col("rank") == 1)
            .select(["ticker", "estimated_eps_avg"])
            .rename({"estimated_eps_avg": "estimated_eps_avg_prev"})
        )

        if recent.is_empty() or prev.is_empty():
            logger.debug("EpsRevision.compute: 不足 2 个 fiscal period")
            return _EMPTY.clone()

        merged = recent.join(prev, on="ticker", how="inner")

        if merged.is_empty():
            return _EMPTY.clone()

        # Revision = (recent - prev) / |prev|
        merged = merged.with_columns(
            pl.when(pl.col("estimated_eps_avg_prev").abs() > 0.01)
            .then(
                (pl.col("estimated_eps_avg_recent") - pl.col("estimated_eps_avg_prev"))
                / pl.col("estimated_eps_avg_prev").abs()
            )
            .otherwise(0.0)
            .alias("factor_value")
        )

        return merged.select(["ticker", "factor_value"])
