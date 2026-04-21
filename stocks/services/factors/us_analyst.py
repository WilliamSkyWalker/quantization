"""美股分析师因子: US_ANALYST_RATING, US_ANALYST_COVERAGE"""

import logging
from datetime import datetime, timedelta

import numpy as np
import polars as pl

from services.config import LOG_LEVEL
from stocks.services.factors.us_base import USFactorBase

logger = logging.getLogger(__name__)
logger.setLevel(LOG_LEVEL)

# 评级映射（越高越看好）
_RATING_MAP = {
    "Strong Buy": 5.0, "Buy": 4.0, "Outperform": 4.0, "Overweight": 4.0,
    "Market Perform": 3.0, "Hold": 3.0, "Neutral": 3.0, "Equal-Weight": 3.0,
    "Sector Perform": 3.0, "In-Line": 3.0, "Peer Perform": 3.0,
    "Underperform": 2.0, "Underweight": 2.0, "Reduce": 2.0,
    "Sell": 1.0, "Strong Sell": 1.0,
}

# 回看窗口
_LOOKBACK_DAYS = 120

_EMPTY = pl.DataFrame(schema={"ticker": pl.Utf8, "factor_value": pl.Float64})


class USAnalystRating(USFactorBase):
    """Analyst Rating: mean rating score over trailing window"""
    name = "US_ANALYST_RATING"
    description = "分析师共识评级"

    def compute(self, date: str, universe: pl.DataFrame) -> pl.DataFrame:
        tickers = universe["ticker"].to_list()
        date_ts = datetime.strptime(date, "%Y-%m-%d") if isinstance(date, str) else date
        start_ts = date_ts - timedelta(days=_LOOKBACK_DAYS)

        # 从预加载数据获取
        bulk_ar = self._static_cache.get("_bulk_analyst")
        if bulk_ar is None or bulk_ar.is_empty():
            logger.debug("USAnalystRating.compute: 缓存为空")
            return _EMPTY.clone()
        df = bulk_ar.filter(
            (pl.col("date") >= start_ts) & (pl.col("date") <= date_ts)
        )

        if df.is_empty():
            logger.debug("USAnalystRating.compute: 无分析师推荐数据")
            return _EMPTY.clone()

        if tickers:
            df = df.filter(pl.col("ticker").is_in(tickers))

        # 映射评级到数值 — 用 polars replace (old->new map)
        df = df.with_columns(
            pl.col("new_grade").str.strip_chars()
            .replace_strict(_RATING_MAP, default=None)
            .cast(pl.Float64, strict=False)
            .alias("score")
        )
        df = df.drop_nulls(subset=["score"])

        if df.is_empty():
            logger.debug("USAnalystRating.compute: 评级映射后无有效数据")
            return _EMPTY.clone()

        result = df.group_by("ticker").agg(
            pl.col("score").mean().alias("factor_value")
        )
        return result.select(["ticker", "factor_value"])


class USAnalystCoverage(USFactorBase):
    """Analyst Coverage: log(1 + distinct analyst firms) in trailing window"""
    name = "US_ANALYST_COVERAGE"
    description = "分析师覆盖度"

    def compute(self, date: str, universe: pl.DataFrame) -> pl.DataFrame:
        tickers = universe["ticker"].to_list()
        date_ts = datetime.strptime(date, "%Y-%m-%d") if isinstance(date, str) else date
        start_ts = date_ts - timedelta(days=_LOOKBACK_DAYS)

        bulk_ar = self._static_cache.get("_bulk_analyst")
        if bulk_ar is None or bulk_ar.is_empty():
            logger.debug("USAnalystCoverage.compute: 缓存为空")
            return _EMPTY.clone()
        df = bulk_ar.filter(
            (pl.col("date") >= start_ts) & (pl.col("date") <= date_ts)
        )

        if df.is_empty():
            logger.debug("USAnalystCoverage.compute: 无分析师推荐数据")
            return _EMPTY.clone()

        if tickers:
            df = df.filter(pl.col("ticker").is_in(tickers))

        result = df.group_by("ticker").agg(
            pl.col("grading_company").n_unique().alias("n_firms")
        )
        result = result.with_columns(
            pl.col("n_firms").cast(pl.Float64).log1p().alias("factor_value")
        )
        return result.select(["ticker", "factor_value"])
