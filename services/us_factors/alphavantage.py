"""
美股 Alpha Vantage 因子: NEWS_SENTIMENT, IV_SKEW, PUT_CALL_RATIO

数据源：Alpha Vantage API
    - us_news_sentiment: AI 新闻情绪（ticker 级别日聚合）
    - us_options_snapshot: 期权快照（ATM IV + put/call ratio 日聚合）
"""

import logging

import numpy as np
import pandas as pd

from services.config import LOG_LEVEL
from services.us_factors.base import USFactorBase

logger = logging.getLogger(__name__)
logger.setLevel(LOG_LEVEL)

_NEWS_LOOKBACK_DAYS = 14
_OPTIONS_LOOKBACK_DAYS = 5


class NewsSentiment(USFactorBase):
    """News Sentiment: relevance-weighted mean sentiment over trailing 14 days"""
    name = "NEWS_SENTIMENT"
    description = "新闻情绪 (近14天 AI 情绪加权均值)"

    def compute(self, date: str, universe: pd.DataFrame) -> pd.DataFrame:
        tickers = universe["ticker"].tolist()
        date_ts = pd.to_datetime(date)
        start_ts = date_ts - pd.Timedelta(days=_NEWS_LOOKBACK_DAYS)

        try:
            sql = (
                "SELECT ticker, "
                "  SUM(sentiment_score * relevance_score) / SUM(relevance_score) as weighted_sent, "
                "  SUM(article_count) as total_articles "
                "FROM us_news_sentiment "
                "WHERE date >= :start AND date <= :end "
                "GROUP BY ticker"
            )
            df = self.db.query(sql, params={
                "start": start_ts.strftime("%Y-%m-%d"),
                "end": date,
            })
        except Exception as e:
            logger.warning(f"NewsSentiment.compute: 查询失败: {e}")
            return pd.DataFrame(columns=["ticker", "factor_value"])

        if df.empty:
            logger.debug("NewsSentiment.compute: 近14天无新闻情绪数据")
            return pd.DataFrame(columns=["ticker", "factor_value"])

        df = df[df["ticker"].isin(tickers)]
        if df.empty:
            logger.debug("NewsSentiment.compute: universe 内无匹配")
            return pd.DataFrame(columns=["ticker", "factor_value"])

        # 只取至少有 2 篇文章的 ticker（过滤噪音）
        df = df[df["total_articles"] >= 2]
        if df.empty:
            logger.debug("NewsSentiment.compute: 过滤低文章数后无数据")
            return pd.DataFrame(columns=["ticker", "factor_value"])

        df["factor_value"] = pd.to_numeric(df["weighted_sent"], errors="coerce")
        return df[["ticker", "factor_value"]]


class IvSkew(USFactorBase):
    """IV Skew: put IV - call IV (ATM, trailing 5-day average)"""
    name = "IV_SKEW"
    description = "隐含波动率偏斜 (近5天 ATM put_iv - call_iv 均值)"

    def compute(self, date: str, universe: pd.DataFrame) -> pd.DataFrame:
        tickers = universe["ticker"].tolist()
        date_ts = pd.to_datetime(date)
        start_ts = date_ts - pd.Timedelta(days=_OPTIONS_LOOKBACK_DAYS)

        try:
            sql = (
                "SELECT ticker, AVG(iv_skew) as avg_skew "
                "FROM us_options_snapshot "
                "WHERE date >= :start AND date <= :end "
                "  AND iv_skew IS NOT NULL "
                "GROUP BY ticker"
            )
            df = self.db.query(sql, params={
                "start": start_ts.strftime("%Y-%m-%d"),
                "end": date,
            })
        except Exception as e:
            logger.warning(f"IvSkew.compute: 查询失败: {e}")
            return pd.DataFrame(columns=["ticker", "factor_value"])

        if df.empty:
            logger.debug("IvSkew.compute: 近5天无期权 IV skew 数据")
            return pd.DataFrame(columns=["ticker", "factor_value"])

        df = df[df["ticker"].isin(tickers)]
        if df.empty:
            logger.debug("IvSkew.compute: universe 内无匹配")
            return pd.DataFrame(columns=["ticker", "factor_value"])

        df["factor_value"] = pd.to_numeric(df["avg_skew"], errors="coerce")
        return df[["ticker", "factor_value"]]


class PutCallRatio(USFactorBase):
    """Put/Call Ratio: trailing 5-day average put/call volume ratio"""
    name = "PUT_CALL_RATIO"
    description = "看跌/看涨比率 (近5天成交量比均值)"

    def compute(self, date: str, universe: pd.DataFrame) -> pd.DataFrame:
        tickers = universe["ticker"].tolist()
        date_ts = pd.to_datetime(date)
        start_ts = date_ts - pd.Timedelta(days=_OPTIONS_LOOKBACK_DAYS)

        try:
            sql = (
                "SELECT ticker, AVG(put_call_volume_ratio) as avg_pc_ratio "
                "FROM us_options_snapshot "
                "WHERE date >= :start AND date <= :end "
                "  AND put_call_volume_ratio IS NOT NULL "
                "GROUP BY ticker"
            )
            df = self.db.query(sql, params={
                "start": start_ts.strftime("%Y-%m-%d"),
                "end": date,
            })
        except Exception as e:
            logger.warning(f"PutCallRatio.compute: 查询失败: {e}")
            return pd.DataFrame(columns=["ticker", "factor_value"])

        if df.empty:
            logger.debug("PutCallRatio.compute: 近5天无 put/call ratio 数据")
            return pd.DataFrame(columns=["ticker", "factor_value"])

        df = df[df["ticker"].isin(tickers)]
        if df.empty:
            logger.debug("PutCallRatio.compute: universe 内无匹配")
            return pd.DataFrame(columns=["ticker", "factor_value"])

        df["factor_value"] = pd.to_numeric(df["avg_pc_ratio"], errors="coerce")
        return df[["ticker", "factor_value"]]
