"""
SEC EDGAR 美股历史财报下载器

从 SEC EDGAR XBRL API 下载 S&P 500 + NASDAQ 100 股票的季度财报，
补充 SimFin 免费版（仅 5 年）的历史缺口。

数据来源：https://data.sec.gov/api/xbrl/companyfacts/
每家公司一个 JSON，包含所有 XBRL 报告数据 + filing date。

Usage:
    dl = EdgarDownloader(db)
    dl.download_financials()  # 下载全量历史财报
"""

import json
import logging
import time
import urllib.request
from typing import Optional

import numpy as np
import pandas as pd

from services.config import LOG_LEVEL
from services.data.database import DatabaseManager

logger = logging.getLogger(__name__)
logger.setLevel(LOG_LEVEL)

_SEC_BASE = "https://data.sec.gov/api/xbrl/companyfacts"
_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
_USER_AGENT = "QuantSystem research@quantsystem.local"
_RATE_LIMIT_DELAY = 0.12  # SEC 要求 10 req/s，保守用 8 req/s

# XBRL 标签映射（每个字段的候选标签，按优先级排序）
_TAG_MAP = {
    "revenue": [
        "RevenueFromContractWithCustomersExcludingAssessedTax",
        "Revenues",
        "SalesRevenueNet",
        "SalesRevenueGoodsNet",
        "RevenueFromContractWithCustomersIncludingAssessedTax",
    ],
    "net_income": [
        "NetIncomeLoss",
        "ProfitLoss",
        "NetIncomeLossAvailableToCommonStockholdersBasic",
    ],
    "total_assets": [
        "Assets",
    ],
    "total_equity": [
        "StockholdersEquity",
        "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
    ],
    "gross_profit": [
        "GrossProfit",
    ],
    "operating_cashflow": [
        "NetCashProvidedByUsedInOperatingActivities",
        "NetCashProvidedByOperatingActivities",
        "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations",
        "NetCashProvidedByOperatingActivitiesContinuingOperations",
    ],
    "capex": [
        "PaymentsToAcquirePropertyPlantAndEquipment",
        "PaymentsToAcquireProductiveAssets",
        "CapitalExpenditureDiscontinuedOperations",
    ],
    "eps": [
        "EarningsPerShareDiluted",
        "EarningsPerShareBasic",
    ],
}


class EdgarDownloader:
    """SEC EDGAR XBRL 历史财报下载器。"""

    def __init__(self, db: DatabaseManager):
        self.db = db
        self._cik_map: dict[str, str] = {}  # ticker → CIK (zero-padded 10 digits)

    def _fetch_json(self, url: str) -> Optional[dict]:
        """从 SEC 下载 JSON，带 User-Agent 和限速。"""
        req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
        try:
            resp = urllib.request.urlopen(req, timeout=30)
            return json.loads(resp.read())
        except Exception as e:
            logger.debug(f"SEC fetch failed: {url} — {e}")
            return None

    def _load_cik_map(self):
        """从 SEC 下载 ticker → CIK 映射。"""
        if self._cik_map:
            return

        logger.info("Loading SEC ticker-CIK mapping...")
        data = self._fetch_json(_TICKERS_URL)
        if not data:
            logger.error("Failed to load SEC ticker-CIK mapping")
            return

        for _, entry in data.items():
            ticker = entry.get("ticker", "").upper()
            cik = str(entry.get("cik_str", ""))
            if ticker and cik:
                self._cik_map[ticker] = cik.zfill(10)

        logger.info(f"Loaded {len(self._cik_map)} ticker-CIK mappings")

    def download_financials(self, tickers: list[str] = None, min_date: str = "2010-01-01") -> int:
        """
        从 SEC EDGAR 下载季度财报，upsert 到 us_financial_data。

        Args:
            tickers: 股票列表（默认从 us_stock_basic 获取）。
            min_date: 最早日期（默认 2010-01-01）。

        Returns:
            写入记录数。
        """
        self._load_cik_map()
        if not self._cik_map:
            logger.warning("download_financials: CIK 映射为空，跳过下载")
            return 0

        if tickers is None:
            tickers = self.db.get_us_tickers()
        if not tickers:
            logger.warning("download_financials: 无 ticker 可下载")
            return 0

        min_dt = pd.to_datetime(min_date)
        total = 0
        failed = 0

        for i, ticker in enumerate(tickers):
            cik = self._cik_map.get(ticker)
            if not cik:
                logger.debug(f"download_financials: 跳过 {ticker} (无 CIK 映射)")
                continue

            url = f"{_SEC_BASE}/CIK{cik}.json"
            data = self._fetch_json(url)
            time.sleep(_RATE_LIMIT_DELAY)

            if not data:
                failed += 1
                logger.debug(f"download_financials: 跳过 {ticker} (API 返回空)")
                continue

            records = self._parse_company_facts(ticker, data, min_dt)
            if records:
                df = pd.DataFrame(records)
                self.db.upsert_us_financial_data(df)
                total += len(records)

            if (i + 1) % 50 == 0:
                logger.info(f"EDGAR progress: {i+1}/{len(tickers)}, {total} records, {failed} failed")

        logger.info(f"EDGAR download done: {total} records from {len(tickers)} tickers ({failed} failed)")
        return total

    def _parse_company_facts(self, ticker: str, data: dict, min_dt: pd.Timestamp) -> list[dict]:
        """解析单家公司的 XBRL facts → 季报记录列表。"""
        facts = data.get("facts", {}).get("us-gaap", {})
        if not facts:
            logger.debug(f"_parse_company_facts: {ticker} 无 us-gaap facts")
            return []

        # 提取每个字段的季度数据
        field_data: dict[str, dict[str, dict]] = {}  # {field: {end_date: {val, filed}}}

        for field_name, tag_candidates in _TAG_MAP.items():
            for tag in tag_candidates:
                if tag not in facts:
                    continue  # 该标签不在 facts 中，尝试下一个
                entries = facts[tag].get("units", {}).get("USD", [])
                if not entries and field_name == "eps":
                    entries = facts[tag].get("units", {}).get("USD/shares", [])
                if not entries:
                    continue  # 该标签无 USD 单位数据，尝试下一个
                quarterly = [e for e in entries if e.get("form") in ("10-Q", "10-K/A")]
                if not quarterly:
                    continue  # 该标签无季度数据，尝试下一个

                if field_name not in field_data:
                    field_data[field_name] = {}
                for e in quarterly:
                    end = e.get("end", "")
                    if end and end not in field_data[field_name]:
                        field_data[field_name][end] = {
                            "val": e.get("val"),
                            "filed": e.get("filed", end),
                            "fp": e.get("fp", ""),
                            "fy": e.get("fy", 0),
                        }
                break  # 用第一个匹配的标签

        # 也取 10-K 年报中的季度数据（有些公司只报年报不报季报的某些字段）
        for field_name, tag_candidates in _TAG_MAP.items():
            if field_name in field_data:
                continue  # 已有数据
            for tag in tag_candidates:
                if tag not in facts:
                    continue  # 该标签不在 facts 中，尝试下一个
                entries = facts[tag].get("units", {}).get("USD", [])
                if not entries and field_name == "eps":
                    entries = facts[tag].get("units", {}).get("USD/shares", [])
                annual = [e for e in entries if e.get("form") == "10-K"]
                if annual:
                    if field_name not in field_data:
                        field_data[field_name] = {}
                    for e in annual:
                        end = e.get("end", "")
                        if end and end not in field_data[field_name]:
                            field_data[field_name][end] = {
                                "val": e.get("val"),
                                "filed": e.get("filed", end),
                                "fp": e.get("fp", ""),
                                "fy": e.get("fy", 0),
                            }
                    break  # 用第一个有年报数据的标签

        if not field_data:
            logger.debug(f"_parse_company_facts: {ticker} 无可解析的字段数据")
            return []

        # 合并所有字段到统一的季度记录
        all_dates = set()
        for fd in field_data.values():
            all_dates.update(fd.keys())

        records = []
        for end_date in sorted(all_dates):
            end_dt = pd.to_datetime(end_date, errors="coerce")
            if pd.isna(end_dt) or end_dt < min_dt:
                continue  # 跳过无效或早于最小日期的记录

            # 确定 filing_date（取所有字段中最晚的 filed 日期）
            filed_dates = []
            for fd in field_data.values():
                if end_date in fd and fd[end_date].get("filed"):
                    filed_dates.append(fd[end_date]["filed"])
            filing_date = max(filed_dates) if filed_dates else end_date

            # 构建 period 字符串
            month = end_dt.month
            year = end_dt.year
            if month <= 3:
                period = f"Q1 {year}"
            elif month <= 6:
                period = f"Q2 {year}"
            elif month <= 9:
                period = f"Q3 {year}"
            else:
                period = f"Q4 {year}"

            revenue = _get_val(field_data, "revenue", end_date)
            net_income = _get_val(field_data, "net_income", end_date)
            total_assets = _get_val(field_data, "total_assets", end_date)
            total_equity = _get_val(field_data, "total_equity", end_date)
            gross_profit = _get_val(field_data, "gross_profit", end_date)
            op_cf = _get_val(field_data, "operating_cashflow", end_date)
            eps = _get_val(field_data, "eps", end_date)

            # 衍生字段
            gross_margin = None
            if revenue and gross_profit and abs(revenue) > 1e-6:
                gross_margin = gross_profit / revenue * 100

            operating_margin = None  # SEC 数据无直接字段

            roe = None
            if net_income is not None and total_equity and abs(total_equity) > 1e-6:
                roe = net_income / total_equity * 100

            capex = _get_val(field_data, "capex", end_date)
            fcf = None
            if op_cf is not None:
                if capex is not None:
                    fcf = op_cf - abs(capex)  # FCF = Operating CF - CapEx
                else:
                    fcf = op_cf  # fallback: FCF ≈ Operating CF

            records.append({
                "ticker": ticker,
                "period": period,
                "date": end_date,
                "filing_date": filing_date,
                "revenue": revenue,
                "net_income": net_income,
                "eps": eps,
                "gross_margin": gross_margin,
                "operating_margin": operating_margin,
                "roe": roe,
                "total_assets": total_assets,
                "total_equity": total_equity,
                "total_debt": None,
                "free_cash_flow": fcf,
                "pe_ratio": None,
                "pb_ratio": None,
            })

        return records


def _get_val(field_data: dict, field: str, end_date: str) -> Optional[float]:
    fd = field_data.get(field, {})
    entry = fd.get(end_date)
    if entry and entry.get("val") is not None:
        try:
            return float(entry["val"])
        except (TypeError, ValueError):
            logger.debug(f"_get_val: 字段 {field} 值转换失败")
            return None
    return None
