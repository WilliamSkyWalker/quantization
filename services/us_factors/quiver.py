"""
美股 Quiver 另类数据因子: LOBBY_INTENSITY, GOV_CONTRACT, WSB_SENTIMENT

数据源：Quiver Quantitative API
    - us_lobbying: 企业游说活动（金额、频次）
    - us_gov_contract: 政府合同（季度金额）
    - us_wsb_sentiment: WallStreetBets 情绪（提及次数、情绪分数）
"""

import logging

import numpy as np
import pandas as pd

from services.config import LOG_LEVEL
from services.us_factors.base import USFactorBase

logger = logging.getLogger(__name__)
logger.setLevel(LOG_LEVEL)

_LOBBY_LOOKBACK_DAYS = 365
_GOV_LOOKBACK_QUARTERS = 4
_WSB_LOOKBACK_DAYS = 30


class LobbyIntensity(USFactorBase):
    """Lobby Intensity: trailing 12M lobbying spend / market cap"""
    name = "LOBBY_INTENSITY"
    description = "游说力度 (近12月游说支出 / 市值)"

    def compute(self, date: str, universe: pd.DataFrame) -> pd.DataFrame:
        tickers = universe["ticker"].tolist()
        date_ts = pd.to_datetime(date)
        start_ts = date_ts - pd.Timedelta(days=_LOBBY_LOOKBACK_DAYS)

        try:
            sql = (
                "SELECT ticker, SUM(amount) as total_lobby "
                "FROM us_lobbying "
                "WHERE date >= :start AND date <= :end AND amount > 0 "
                "GROUP BY ticker"
            )
            df = self.db.query(sql, params={
                "start": start_ts.strftime("%Y-%m-%d"),
                "end": date,
            })
        except Exception as e:
            logger.warning(f"LobbyIntensity.compute: 查询失败: {e}")
            return pd.DataFrame(columns=["ticker", "factor_value"])

        if df.empty:
            logger.debug("LobbyIntensity.compute: 近12月无游说数据")
            return pd.DataFrame(columns=["ticker", "factor_value"])

        df = df[df["ticker"].isin(tickers)]
        if df.empty:
            logger.debug("LobbyIntensity.compute: universe 内无匹配")
            return pd.DataFrame(columns=["ticker", "factor_value"])

        mktcap = self.get_market_cap(date, tickers)
        if mktcap.empty:
            logger.debug("LobbyIntensity.compute: 无市值数据")
            return pd.DataFrame(columns=["ticker", "factor_value"])

        merged = df.merge(mktcap, on="ticker", how="inner")
        merged["total_lobby"] = pd.to_numeric(merged["total_lobby"], errors="coerce")
        merged["factor_value"] = np.where(
            merged["market_cap"] > 0,
            merged["total_lobby"] / merged["market_cap"],
            np.nan,
        )
        return merged[["ticker", "factor_value"]]


class GovContract(USFactorBase):
    """Gov Contract: trailing 4Q government contract amount / revenue"""
    name = "GOV_CONTRACT"
    description = "政府合同依赖度 (近4季度合同金额 / 收入)"

    def compute(self, date: str, universe: pd.DataFrame) -> pd.DataFrame:
        tickers = universe["ticker"].tolist()
        date_ts = pd.to_datetime(date)
        current_year = date_ts.year
        current_qtr = (date_ts.month - 1) // 3 + 1

        # 近4个季度的 (year, quarter) 组合
        quarters = []
        y, q = current_year, current_qtr
        for _ in range(_GOV_LOOKBACK_QUARTERS):
            quarters.append((y, q))
            q -= 1
            if q == 0:
                q = 4
                y -= 1

        try:
            # 构造 SQL 条件
            conds = " OR ".join(
                f"(year = {y} AND quarter = {q})" for y, q in quarters
            )
            sql = (
                f"SELECT ticker, SUM(amount) as total_contract "
                f"FROM us_gov_contract "
                f"WHERE ({conds}) AND amount > 0 "
                f"GROUP BY ticker"
            )
            df = self.db.query(sql)
        except Exception as e:
            logger.warning(f"GovContract.compute: 查询失败: {e}")
            return pd.DataFrame(columns=["ticker", "factor_value"])

        if df.empty:
            logger.debug("GovContract.compute: 近4季度无政府合同数据")
            return pd.DataFrame(columns=["ticker", "factor_value"])

        df = df[df["ticker"].isin(tickers)]
        if df.empty:
            logger.debug("GovContract.compute: universe 内无匹配")
            return pd.DataFrame(columns=["ticker", "factor_value"])

        # TTM 收入
        ttm_rev = self.get_ttm_value(date, "revenue", tickers)
        if ttm_rev.empty:
            logger.debug("GovContract.compute: 无 TTM 收入数据")
            return pd.DataFrame(columns=["ticker", "factor_value"])

        merged = df.merge(ttm_rev, on="ticker", how="inner")
        merged["total_contract"] = pd.to_numeric(merged["total_contract"], errors="coerce")
        merged["factor_value"] = np.where(
            merged["ttm_value"].abs() > 0,
            merged["total_contract"] / merged["ttm_value"].abs(),
            np.nan,
        )
        return merged[["ticker", "factor_value"]]


class WsbSentiment(USFactorBase):
    """WSB Sentiment: mean sentiment score over trailing 30 days"""
    name = "WSB_SENTIMENT"
    description = "WallStreetBets 情绪 (近30天情绪均值)"

    def compute(self, date: str, universe: pd.DataFrame) -> pd.DataFrame:
        tickers = universe["ticker"].tolist()
        date_ts = pd.to_datetime(date)
        start_ts = date_ts - pd.Timedelta(days=_WSB_LOOKBACK_DAYS)

        try:
            sql = (
                "SELECT ticker, AVG(sentiment) as avg_sentiment, "
                "SUM(mentions) as total_mentions "
                "FROM us_wsb_sentiment "
                "WHERE date >= :start AND date <= :end "
                "GROUP BY ticker"
            )
            df = self.db.query(sql, params={
                "start": start_ts.strftime("%Y-%m-%d"),
                "end": date,
            })
        except Exception as e:
            logger.warning(f"WsbSentiment.compute: 查询失败: {e}")
            return pd.DataFrame(columns=["ticker", "factor_value"])

        if df.empty:
            logger.debug("WsbSentiment.compute: 近30天无 WSB 数据")
            return pd.DataFrame(columns=["ticker", "factor_value"])

        df = df[df["ticker"].isin(tickers)]
        if df.empty:
            logger.debug("WsbSentiment.compute: universe 内无匹配")
            return pd.DataFrame(columns=["ticker", "factor_value"])

        # 用情绪均值作为因子值（提及数作为权重可以后续优化）
        df["factor_value"] = pd.to_numeric(df["avg_sentiment"], errors="coerce")
        return df[["ticker", "factor_value"]]
