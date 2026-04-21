"""美股 Quiver 因子: LOBBY_INTENSITY, GOV_CONTRACT, WSB_SENTIMENT"""

import logging

import numpy as np
import pandas as pd
from django.db.models import Sum, Q

from services.config import LOG_LEVEL
from stocks.services.factors.us_base import USFactorBase

logger = logging.getLogger(__name__)
logger.setLevel(LOG_LEVEL)

_LOBBY_LOOKBACK_DAYS = 365
_GOV_LOOKBACK_QUARTERS = 4
_WSB_LOOKBACK_DAYS = 30


class LobbyIntensity(USFactorBase):
    """Lobby Intensity: trailing 12-month lobbying spend / market cap"""
    name = "LOBBY_INTENSITY"
    description = "游说强度 (近12月游说支出 / 市值)"

    def compute(self, date: str, universe: pd.DataFrame) -> pd.DataFrame:
        tickers = universe["ticker"].tolist()
        date_ts = pd.to_datetime(date)
        start_ts = date_ts - pd.Timedelta(days=_LOBBY_LOOKBACK_DAYS)

        try:
            from stocks.models import USLobbying
            qs = (
                USLobbying.objects.filter(
                    date__gte=start_ts, date__lte=date_ts, amount__gt=0,
                )
                .values("ticker")
                .annotate(total_lobby=Sum("amount"))
            )
            df = pd.DataFrame(qs)
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

        quarters = []
        y, q = current_year, current_qtr
        for _ in range(_GOV_LOOKBACK_QUARTERS):
            quarters.append((y, q))
            q -= 1
            if q == 0:
                q = 4
                y -= 1

        try:
            from stocks.models import USGovContract
            q_filter = Q()
            for yr, qt in quarters:
                q_filter |= Q(year=yr, quarter=qt)
            qs = (
                USGovContract.objects.filter(q_filter, amount__gt=0)
                .values("ticker")
                .annotate(total_contract=Sum("amount"))
            )
            df = pd.DataFrame(qs)
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
    """WSB Sentiment: 已废弃（只有 3 个 ticker，无截面区分力）"""
    name = "WSB_SENTIMENT"
    description = "WallStreetBets 情绪（已废弃）"

    def compute(self, date: str, universe: pd.DataFrame) -> pd.DataFrame:
        return pd.DataFrame(columns=["ticker", "factor_value"])
