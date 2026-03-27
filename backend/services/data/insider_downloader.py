"""
SEC Form 4 内部人交易数据下载器

从 openinsider.com 解析 HTML 表格获取近期 Form 4 提交记录。
完全免费，无需 API key。

Usage:
    from backend.services.data.insider_downloader import download_insider_bulk
    n = download_insider_bulk(db, days=365)
"""

import logging
import re
import urllib.request

import pandas as pd
from bs4 import BeautifulSoup

from backend.services.config import LOG_LEVEL
from backend.services.data.database import DatabaseManager

logger = logging.getLogger(__name__)
logger.setLevel(LOG_LEVEL)

_BASE_URL = "http://openinsider.com/screener?s=&o=&pl=&ph=&ll=&lh=&fd={days}&fdr=&td=0&tdr=&feession=&cession=&sidTicker=&tiession=&z=&zb=&za=&export=csv"


def _parse_value(s: str) -> float | None:
    """Parse dollar value like '+$1,234,567' or '-$456,789'."""
    s = s.strip().replace(",", "").replace("$", "").replace("+", "")
    try:
        return float(s)
    except ValueError:
        return None


def download_insider_bulk(db: DatabaseManager, days: int = 365) -> int:
    """从 openinsider.com 批量下载内部人交易数据。"""
    url = _BASE_URL.format(days=days)
    headers = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}

    logger.info(f"Downloading insider data from openinsider.com (last {days} days)...")

    try:
        req = urllib.request.Request(url, headers=headers)
        resp = urllib.request.urlopen(req, timeout=30)
        html = resp.read().decode("utf-8", errors="replace")
    except Exception as e:
        logger.error(f"openinsider.com download failed: {e}")
        return 0

    # Parse HTML table
    soup = BeautifulSoup(html, "lxml")
    table = soup.find("table", class_="tinytable")
    if not table:
        tables = soup.find_all("table")
        table = max(tables, key=lambda t: len(t.find_all("tr"))) if tables else None

    if not table:
        logger.error("No table found on openinsider.com")
        return 0

    rows = table.find_all("tr")
    if len(rows) < 2:
        logger.warning("openinsider table is empty")
        return 0

    # Parse header
    header_cells = rows[0].find_all(["th", "td"])
    headers_text = [c.get_text(strip=True).lower() for c in header_cells]
    logger.info(f"openinsider columns: {headers_text[:10]}")

    # Map columns
    col_idx = {}
    for i, h in enumerate(headers_text):
        if "ticker" in h:
            col_idx["ticker"] = i
        elif "trade date" in h or h == "trading date":
            col_idx["trade_date"] = i
        elif "trade type" in h or h == "type":
            col_idx["trade_type"] = i
        elif "value" in h and "price" not in h:
            col_idx["value"] = i
        elif "filing date" in h:
            col_idx["filing_date"] = i
        elif "insider" in h and "name" in h:
            col_idx["insider_name"] = i

    if "ticker" not in col_idx or "value" not in col_idx:
        logger.error(f"Cannot map columns. Headers: {headers_text}")
        return 0

    our_tickers = set(db.get_us_tickers())

    records = []
    for row in rows[1:]:
        cells = row.find_all("td")
        if len(cells) <= max(col_idx.values()):
            continue

        ticker = cells[col_idx["ticker"]].get_text(strip=True).upper()
        if ticker not in our_tickers:
            continue

        value = _parse_value(cells[col_idx["value"]].get_text(strip=True))
        if value is None or value == 0:
            continue

        trade_type_str = cells[col_idx.get("trade_type", 0)].get_text(strip=True).lower() if "trade_type" in col_idx else ""
        if "sale" in trade_type_str or "sell" in trade_type_str:
            value = -abs(value)
        elif "purchase" in trade_type_str or "buy" in trade_type_str:
            value = abs(value)
        else:
            # Try to infer from value sign
            pass

        trade_date = cells[col_idx.get("trade_date", 0)].get_text(strip=True) if "trade_date" in col_idx else ""
        filing_date = cells[col_idx.get("filing_date", 0)].get_text(strip=True) if "filing_date" in col_idx else trade_date

        records.append({
            "ticker": ticker,
            "trade_date": trade_date,
            "filing_date": filing_date,
            "trade_type": "BUY" if value > 0 else "SELL",
            "net_value": value,
            "insider_name": cells[col_idx.get("insider_name", 0)].get_text(strip=True) if "insider_name" in col_idx else "",
        })

    if not records:
        logger.warning("No matching insider records")
        return 0

    df = pd.DataFrame(records)
    _upsert_insider_data(db, df)
    logger.info(f"Insider data: {len(records)} records ({df['ticker'].nunique()} tickers)")
    return len(records)


def _upsert_insider_data(db: DatabaseManager, df: pd.DataFrame):
    """写入 us_insider_transaction 表。"""
    from sqlalchemy import text

    with db.engine.begin() as conn:
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS us_insider_transaction (
                id INT AUTO_INCREMENT PRIMARY KEY,
                ticker VARCHAR(20) NOT NULL,
                trade_date DATE,
                filing_date DATE,
                trade_type VARCHAR(10),
                net_value FLOAT,
                insider_name VARCHAR(200),
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                INDEX idx_insider_ticker (ticker),
                INDEX idx_insider_date (trade_date)
            )
        """))

    with db.engine.begin() as conn:
        for _, row in df.iterrows():
            try:
                conn.execute(text(
                    "INSERT INTO us_insider_transaction "
                    "(ticker, trade_date, filing_date, trade_type, net_value, insider_name) "
                    "VALUES (:ticker, :td, :fd, :tt, :nv, :name)"
                ), {
                    "ticker": row["ticker"],
                    "td": row["trade_date"] or None,
                    "fd": row["filing_date"] or None,
                    "tt": row["trade_type"],
                    "nv": row["net_value"],
                    "name": row.get("insider_name", ""),
                })
            except Exception:
                pass
