"""
美股 Alpha Vantage 因子: NEWS_SENTIMENT, IV_SKEW, PUT_CALL_RATIO

数据源：Alpha Vantage API（数据需日积累，无历史）
    - us_news_sentiment: AI 新闻情绪
    - us_options_snapshot: 期权快照
"""

import logging

import numpy as np
import pandas as pd

from services.config import LOG_LEVEL
from stocks.services.factors.us_base import USFactorBase

logger = logging.getLogger(__name__)
logger.setLevel(LOG_LEVEL)

_NEWS_LOOKBACK_DAYS = 14
_OPTIONS_LOOKBACK_DAYS = 5


class NewsSentiment(USFactorBase):
    """News Sentiment: relevance-weighted mean sentiment over trailing 14 days"""
    name = "NEWS_SENTIMENT"
    description = "新闻情绪 (近14天 AI 情绪加权均值)"

    def compute(self, date: str, universe: pd.DataFrame) -> pd.DataFrame:
        # TODO: 待 us_news_sentiment 表数据积累后实现 Django ORM 查询
        logger.debug("NewsSentiment.compute: 数据积累中，暂返回空")
        return pd.DataFrame(columns=["ticker", "factor_value"])


class IvSkew(USFactorBase):
    """IV Skew: put IV - call IV (ATM, trailing 5-day average)"""
    name = "IV_SKEW"
    description = "隐含波动率偏斜 (近5天 ATM put_iv - call_iv 均值)"

    def compute(self, date: str, universe: pd.DataFrame) -> pd.DataFrame:
        # TODO: 待 us_options_snapshot 表数据积累后实现 Django ORM 查询
        logger.debug("IvSkew.compute: 数据积累中，暂返回空")
        return pd.DataFrame(columns=["ticker", "factor_value"])


class PutCallRatio(USFactorBase):
    """Put/Call Ratio: trailing 5-day average put/call volume ratio"""
    name = "PUT_CALL_RATIO"
    description = "看跌/看涨比率 (近5天成交量比均值)"

    def compute(self, date: str, universe: pd.DataFrame) -> pd.DataFrame:
        # TODO: 待 us_options_snapshot 表数据积累后实现 Django ORM 查询
        logger.debug("PutCallRatio.compute: 数据积累中，暂返回空")
        return pd.DataFrame(columns=["ticker", "factor_value"])
