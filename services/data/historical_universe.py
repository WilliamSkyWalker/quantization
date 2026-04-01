"""
历史点位股票池构建

从 Wikipedia S&P 500 变更记录提取所有曾经在指数中的股票（含已退出的），
消除幸存者偏差。

用法：
    from services.data.historical_universe import build_historical_universe
    removed_tickers = build_historical_universe(db)
"""

import logging
from datetime import datetime
from io import StringIO

import pandas as pd
import requests

from services.config import LOG_LEVEL
from services.data.database import DatabaseManager

logger = logging.getLogger(__name__)
logger.setLevel(LOG_LEVEL)

_WIKI_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"


def get_sp500_historical_changes(since: str = "2015-01-01") -> pd.DataFrame:
    """
    从 Wikipedia 获取 S&P 500 历史成分股变更记录。

    Returns:
        DataFrame[date, removed_ticker, removed_name, added_ticker, added_name]
    """
    headers = {"User-Agent": "Mozilla/5.0 (QuantSystem)"}
    resp = requests.get(_WIKI_URL, headers=headers, timeout=15)
    tables = pd.read_html(StringIO(resp.text))

    if len(tables) < 2:
        logger.error("Wikipedia S&P 500 page format changed, cannot find changes table")
        return pd.DataFrame()

    changes = tables[1]
    changes.columns = ["date", "added_ticker", "added_name", "removed_ticker", "removed_name", "reason"]
    changes["date"] = pd.to_datetime(changes["date"], errors="coerce")
    changes = changes[changes["date"] >= since]

    logger.info(f"S&P 500 changes since {since}: {len(changes)} events")
    return changes


def get_removed_tickers(db: DatabaseManager, since: str = "2015-01-01") -> list[str]:
    """
    获取从 S&P 500 中移除且不在当前股票池中的 ticker 列表。

    Returns:
        List of removed tickers (survivorship bias source).
    """
    changes = get_sp500_historical_changes(since)
    if changes.empty:
        logger.debug("get_removed_tickers: S&P 500 变更记录为空")
        return []

    removed = set(changes["removed_ticker"].dropna().unique())
    current = set(db.get_us_tickers())
    missing = sorted(removed - current)

    logger.info(f"Historical removed tickers not in current pool: {len(missing)}")
    return missing


def build_historical_universe(db: DatabaseManager, since: str = "2015-01-01") -> int:
    """
    把历史被移除的 S&P 500 成分股加入 us_stock_basic（标记 is_active=0），
    并下载它们的行情数据。

    Returns:
        新增股票数。
    """
    missing = get_removed_tickers(db, since)
    if not missing:
        logger.info("No missing historical tickers to add")
        return 0

    # 加入 us_stock_basic（is_active=0 标记为历史成分股）
    records = []
    for ticker in missing:
        records.append({
            "ticker": ticker,
            "name": f"[Historical] {ticker}",
            "exchange": "",
            "sector": "",
            "industry": "",
            "ipo_date": None,
            "market_cap": None,
            "country": "US",
            "is_active": 0,  # 标记为非活跃（历史成分股）
        })

    df = pd.DataFrame(records)
    db.upsert_us_stock_basic(df)
    logger.info(f"Added {len(records)} historical tickers to us_stock_basic (is_active=0)")

    return len(records)


def download_historical_prices(db: DatabaseManager, since: str = "2015-01-01") -> int:
    """
    下载历史成分股的日线数据。

    Returns:
        下载记录数。
    """
    from services.data.fmp_downloader import FMPDownloader

    # 获取 is_active=0 的 ticker
    df = db.query("SELECT ticker FROM us_stock_basic WHERE is_active = 0")
    if df.empty:
        logger.debug("download_historical_prices: 无 is_active=0 的历史 ticker")
        return 0

    tickers = df["ticker"].tolist()
    logger.info(f"Downloading prices for {len(tickers)} historical tickers...")

    dl = FMPDownloader(db)
    total = dl.download_daily_prices(tickers=tickers)
    logger.info(f"Historical prices downloaded: {total} records")
    return total


def download_historical_financials(db: DatabaseManager) -> int:
    """
    从 SEC EDGAR 下载历史成分股的财报。

    Returns:
        下载记录数。
    """
    from services.data.edgar_downloader import EdgarDownloader

    df = db.query("SELECT ticker FROM us_stock_basic WHERE is_active = 0")
    if df.empty:
        logger.debug("download_historical_financials: 无 is_active=0 的历史 ticker")
        return 0

    tickers = df["ticker"].tolist()
    logger.info(f"Downloading EDGAR financials for {len(tickers)} historical tickers...")

    dl = EdgarDownloader(db)
    total = dl.download_financials(tickers=tickers, min_date="2010-01-01")
    logger.info(f"Historical financials downloaded: {total} records")
    return total
