"""美股分析师因子: US_ANALYST_RATING, US_ANALYST_COVERAGE"""

import logging

import numpy as np
import pandas as pd

from services.config import LOG_LEVEL
from services.us_factors.base import USFactorBase

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


class USAnalystRating(USFactorBase):
    """Analyst Rating: mean rating score over trailing window"""
    name = "US_ANALYST_RATING"
    description = "分析师共识评级"

    def compute(self, date: str, universe: pd.DataFrame) -> pd.DataFrame:
        tickers = universe["ticker"].tolist()
        date_ts = pd.to_datetime(date)
        start_ts = date_ts - pd.Timedelta(days=_LOOKBACK_DAYS)

        # 从预加载数据获取
        bulk_ar = self._static_cache.get("_bulk_analyst")
        if bulk_ar is not None and not bulk_ar.empty:
            mask = (bulk_ar["date"] >= start_ts) & (bulk_ar["date"] <= date_ts)
            df = bulk_ar[mask].copy()
        else:
            df = self.db.query(
                "SELECT ticker, date, rating FROM us_analyst_recommendation "
                "WHERE date >= :start AND date <= :end",
                params={"start": start_ts.strftime("%Y-%m-%d"), "end": date},
            )

        if df.empty:
            logger.debug("USAnalystRating.compute: 无分析师推荐数据")
            return pd.DataFrame(columns=["ticker", "factor_value"])

        if tickers:
            df = df[df["ticker"].isin(tickers)]

        # 映射评级到数值
        df["score"] = df["rating"].str.strip().map(_RATING_MAP)
        df = df.dropna(subset=["score"])

        if df.empty:
            logger.debug("USAnalystRating.compute: 评级映射后无有效数据")
            return pd.DataFrame(columns=["ticker", "factor_value"])

        result = df.groupby("ticker")["score"].mean().reset_index()
        result.columns = ["ticker", "factor_value"]
        return result


class USAnalystCoverage(USFactorBase):
    """Analyst Coverage: log(1 + distinct analyst firms) in trailing window"""
    name = "US_ANALYST_COVERAGE"
    description = "分析师覆盖度"

    def compute(self, date: str, universe: pd.DataFrame) -> pd.DataFrame:
        tickers = universe["ticker"].tolist()
        date_ts = pd.to_datetime(date)
        start_ts = date_ts - pd.Timedelta(days=_LOOKBACK_DAYS)

        bulk_ar = self._static_cache.get("_bulk_analyst")
        if bulk_ar is not None and not bulk_ar.empty:
            mask = (bulk_ar["date"] >= start_ts) & (bulk_ar["date"] <= date_ts)
            df = bulk_ar[mask].copy()
        else:
            df = self.db.query(
                "SELECT ticker, analyst_company FROM us_analyst_recommendation "
                "WHERE date >= :start AND date <= :end",
                params={"start": start_ts.strftime("%Y-%m-%d"), "end": date},
            )

        if df.empty:
            logger.debug("USAnalystCoverage.compute: 无分析师推荐数据")
            return pd.DataFrame(columns=["ticker", "factor_value"])

        if tickers:
            df = df[df["ticker"].isin(tickers)]

        result = df.groupby("ticker")["analyst_company"].nunique().reset_index()
        result.columns = ["ticker", "n_firms"]
        result["factor_value"] = np.log1p(result["n_firms"])
        return result[["ticker", "factor_value"]]
