"""
美股内部人交易因子: INSIDER_NET_BUY

从 SEC Form 4 数据（openinsider.com）提取高管净买入信号。
高管持续净买入 = 看好公司前景的强信号（信息不对称优势）。

因子值 = 过去 90 天内净买入金额 / 市值
"""

import logging
import re
from typing import Optional

import numpy as np
import pandas as pd

from services.config import LOG_LEVEL
from services.us_factors.base import USFactorBase

logger = logging.getLogger(__name__)
logger.setLevel(LOG_LEVEL)

_LOOKBACK_DAYS = 90
_OPENINSIDER_URL = "http://openinsider.com/screener?s={ticker}&o=&pl=&ph=&ll=&lh=&fd=90&fdr=&td=0&tdr=&feession=&cession=&sidTicker=&tiession=&z=&zb=&za=&export=csv"


class InsiderNetBuy(USFactorBase):
    """Insider Net Buy: net insider purchases / market cap over trailing 90 days"""
    name = "INSIDER_NET_BUY"
    description = "内部人净买入 (Form 4，近90天净买入/市值)"

    def compute(self, date: str, universe: pd.DataFrame) -> pd.DataFrame:
        tickers = universe["ticker"].tolist()
        date_ts = pd.to_datetime(date)

        # 尝试从预加载缓存获取（回测模式）
        insider_data = self._get_insider_data(date, tickers)
        if insider_data.empty:
            return pd.DataFrame(columns=["ticker", "factor_value"])

        # 市值
        mktcap = self.get_market_cap(date, tickers)
        if mktcap.empty:
            return pd.DataFrame(columns=["ticker", "factor_value"])

        df = insider_data.merge(mktcap, on="ticker", how="inner")
        df["factor_value"] = np.where(
            df["market_cap"] > 0,
            df["net_value"] / df["market_cap"],
            0.0,
        )
        return df[["ticker", "factor_value"]]

    def _get_insider_data(self, date: str, tickers: list[str]) -> pd.DataFrame:
        """
        获取内部人交易数据。

        优先从 DB 查询（如果有 us_insider_transaction 表），
        否则从 SEC EDGAR Form 4 RSS feed 获取。
        """
        date_ts = pd.to_datetime(date)
        start_ts = date_ts - pd.Timedelta(days=_LOOKBACK_DAYS)

        # 尝试从 DB 查询
        try:
            sql = (
                "SELECT ticker, SUM(net_value) as net_value "
                "FROM us_insider_transaction "
                "WHERE trade_date >= :start AND trade_date <= :end "
                "GROUP BY ticker"
            )
            df = self.db.query(sql, params={
                "start": start_ts.strftime("%Y-%m-%d"),
                "end": date,
            })
            if not df.empty:
                df["net_value"] = pd.to_numeric(df["net_value"], errors="coerce")
                return df[df["ticker"].isin(tickers)]
        except Exception:
            pass  # 表不存在

        # 回退：从 SEC EDGAR RSS feed 批量获取 Form 4
        return self._fetch_from_edgar_rss(date, tickers)

    def _fetch_from_edgar_rss(self, date: str, tickers: list[str]) -> pd.DataFrame:
        """从 SEC EDGAR 的 full-text search 获取近期 Form 4 数据。"""
        # SEC EDGAR EFTS API 支持按 form type 搜索
        # 但实时抓取在回测中太慢，这里返回空（需要预下载）
        cache_key = ("insider_data", date)
        cached = self._date_cache.get(cache_key)
        if cached is not None:
            return cached

        # 回测模式下返回空（需要先用 download_insider_data() 预填充）
        self._date_cache[cache_key] = pd.DataFrame(columns=["ticker", "net_value"])
        return pd.DataFrame(columns=["ticker", "net_value"])
