"""
FMP (Financial Modeling Prep) 美股数据下载器

负责从 FMP API 获取以下数据并存入 MySQL：
    1. S&P 500 + NASDAQ 100 成分股列表
    2. 日线行情（含复权价）
    3. 季度财务数据
    4. GICS 行业分类
    5. 指数日线（S&P 500, NASDAQ, Dow Jones）
    6. 商品期货日线
    7. 分析师评级
    8. SEC 公告
    9. 公司行动（分红/拆股）
"""

import collections
import logging
import time
import threading
from datetime import datetime, timedelta
from typing import Optional

import pandas as pd
import requests
from tqdm import tqdm

from backend.services.config import (
    FMP_API_KEY,
    FMP_RATE_LIMIT,
    US_DATA_START_DATE,
    US_INDEX_SYMBOLS,
    US_COMMODITY_SYMBOLS,
    LOG_LEVEL,
)
from backend.services.data.database import DatabaseManager

logger = logging.getLogger(__name__)
logger.setLevel(LOG_LEVEL)


# ============================================================
# 限速器
# ============================================================

class FMPRateLimiter:
    """滑动窗口限速器（线程安全），默认 300 req/min。"""

    def __init__(self, max_per_min: int = FMP_RATE_LIMIT):
        self.max_per_min = max_per_min
        self._timestamps: collections.deque = collections.deque()
        self._lock = threading.Lock()

    def acquire(self):
        with self._lock:
            now = time.monotonic()
            while self._timestamps and now - self._timestamps[0] >= 60.0:
                self._timestamps.popleft()

            if len(self._timestamps) >= self.max_per_min:
                wait = 60.0 - (now - self._timestamps[0]) + 0.1
                if wait > 0:
                    logger.debug(f"FMP 限速等待 {wait:.1f}s")
                    time.sleep(wait)
                now = time.monotonic()
                while self._timestamps and now - self._timestamps[0] >= 60.0:
                    self._timestamps.popleft()

            self._timestamps.append(time.monotonic())


# ============================================================
# FMP 下载器
# ============================================================

class FMPDownloader:
    """FMP (Financial Modeling Prep) 美股数据下载器"""

    BASE_URL = "https://financialmodelingprep.com"

    def __init__(self, db: DatabaseManager):
        self.db = db
        self.api_key = FMP_API_KEY
        self.limiter = FMPRateLimiter()
        self._start_date = datetime.strptime(US_DATA_START_DATE, "%Y%m%d").strftime("%Y-%m-%d")

    def _get(self, path: str, params: dict = None) -> list | dict | None:
        """发起 FMP API GET 请求。"""
        if not self.api_key:
            logger.error("FMP_API_KEY 未配置")
            return None

        self.limiter.acquire()
        url = f"{self.BASE_URL}{path}"
        p = {"apikey": self.api_key}
        if params:
            p.update(params)

        try:
            resp = requests.get(url, params=p, timeout=30)
            resp.raise_for_status()
            data = resp.json()
            if isinstance(data, dict) and "Error Message" in data:
                logger.warning(f"FMP API 错误: {data['Error Message']}")
                return None
            return data
        except requests.RequestException as e:
            logger.warning(f"FMP 请求失败 {path}: {e}")
            return None

    # ----------------------------------------------------------
    # 股票列表
    # ----------------------------------------------------------

    def download_stock_list(self) -> int:
        """下载 S&P 500 + NASDAQ 100 成分股列表，合并去重后 upsert。"""
        all_tickers = {}

        # S&P 500
        sp500 = self._get("/api/v3/sp500_constituent")
        if sp500:
            for item in sp500:
                ticker = item.get("symbol", "")
                if ticker:
                    all_tickers[ticker] = {
                        "ticker": ticker,
                        "name": item.get("name", ""),
                        "exchange": item.get("exchange", ""),
                        "sector": item.get("sector", ""),
                        "industry": item.get("subSector", "") or item.get("industry", ""),
                        "is_active": 1,
                    }

        # NASDAQ 100
        nasdaq = self._get("/api/v3/nasdaq_constituent")
        if nasdaq:
            for item in nasdaq:
                ticker = item.get("symbol", "")
                if ticker and ticker not in all_tickers:
                    all_tickers[ticker] = {
                        "ticker": ticker,
                        "name": item.get("name", ""),
                        "exchange": item.get("exchange", ""),
                        "sector": item.get("sector", ""),
                        "industry": item.get("subSector", "") or item.get("industry", ""),
                        "is_active": 1,
                    }

        if not all_tickers:
            logger.warning("未获取到成分股数据")
            return 0

        # 补充 profile 信息（IPO 日期、市值）—— 批量接口
        tickers_list = list(all_tickers.keys())
        batch_size = 50
        for i in range(0, len(tickers_list), batch_size):
            batch = tickers_list[i:i + batch_size]
            symbols_str = ",".join(batch)
            profiles = self._get(f"/api/v3/profile/{symbols_str}")
            if profiles:
                for p in profiles:
                    t = p.get("symbol", "")
                    if t in all_tickers:
                        all_tickers[t]["market_cap"] = p.get("mktCap")
                        all_tickers[t]["country"] = p.get("country", "US")
                        ipo = p.get("ipoDate")
                        if ipo:
                            try:
                                all_tickers[t]["ipo_date"] = pd.to_datetime(ipo).date()
                            except Exception:
                                pass

        df = pd.DataFrame(list(all_tickers.values()))
        self.db.upsert_us_stock_basic(df)
        logger.info(f"美股列表下载完成: {len(df)} 只")
        return len(df)

    # ----------------------------------------------------------
    # 日线行情
    # ----------------------------------------------------------

    def download_daily_prices(self, tickers: list[str] = None) -> int:
        """全量下载日线数据。"""
        if tickers is None:
            tickers = self.db.get_us_tickers()
        if not tickers:
            logger.warning("无美股代码，请先下载股票列表")
            return 0

        total = 0
        for ticker in tqdm(tickers, desc="美股日线下载"):
            data = self._get(
                f"/api/v3/historical-price-full/{ticker}",
                {"from": self._start_date},
            )
            if not data or "historical" not in data:
                continue

            records = []
            for row in data["historical"]:
                records.append({
                    "ticker": ticker,
                    "trade_date": row.get("date"),
                    "open": row.get("open"),
                    "high": row.get("high"),
                    "low": row.get("low"),
                    "close": row.get("close"),
                    "adj_close": row.get("adjClose"),
                    "volume": row.get("volume"),
                    "change_pct": row.get("changePercent"),
                })

            if records:
                df = pd.DataFrame(records)
                self.db.bulk_upsert_us_daily_price(df)
                total += len(records)

        logger.info(f"美股日线下载完成: {total} 条")
        return total

    def update_daily_prices(self) -> int:
        """增量更新日线（从 DB 最新日期开始）。"""
        tickers = self.db.get_us_tickers()
        if not tickers:
            return 0

        latest = self.db.get_latest_us_trade_date()
        if latest:
            from_date = (pd.to_datetime(latest) + timedelta(days=1)).strftime("%Y-%m-%d")
        else:
            from_date = self._start_date

        today = datetime.now().strftime("%Y-%m-%d")
        if from_date > today:
            logger.info("美股日线已是最新")
            return 0

        total = 0
        for ticker in tqdm(tickers, desc="美股日线增量更新"):
            data = self._get(
                f"/api/v3/historical-price-full/{ticker}",
                {"from": from_date, "to": today},
            )
            if not data or "historical" not in data:
                continue

            records = []
            for row in data["historical"]:
                records.append({
                    "ticker": ticker,
                    "trade_date": row.get("date"),
                    "open": row.get("open"),
                    "high": row.get("high"),
                    "low": row.get("low"),
                    "close": row.get("close"),
                    "adj_close": row.get("adjClose"),
                    "volume": row.get("volume"),
                    "change_pct": row.get("changePercent"),
                })

            if records:
                df = pd.DataFrame(records)
                self.db.bulk_upsert_us_daily_price(df)
                total += len(records)

        logger.info(f"美股日线增量更新完成: {total} 条")
        return total

    # ----------------------------------------------------------
    # 财务数据
    # ----------------------------------------------------------

    def download_financial_data(self, tickers: list[str] = None) -> int:
        """下载季度财报（income-statement + key-metrics 合并）。"""
        if tickers is None:
            tickers = self.db.get_us_tickers()
        if not tickers:
            return 0

        total = 0
        for ticker in tqdm(tickers, desc="美股财务下载"):
            # 利润表
            income = self._get(
                f"/api/v3/income-statement/{ticker}",
                {"period": "quarter", "limit": 40},
            )
            # 关键指标
            metrics = self._get(
                f"/api/v3/key-metrics/{ticker}",
                {"period": "quarter", "limit": 40},
            )

            if not income:
                continue

            # 将 metrics 转为 dict[period] 便于合并
            metrics_map = {}
            if metrics:
                for m in metrics:
                    p = m.get("period", "")
                    if p:
                        metrics_map[p] = m

            records = []
            for row in income:
                period = row.get("period", "")
                date_str = row.get("date", "")
                if not period or not date_str:
                    continue

                metric = metrics_map.get(period, {})
                revenue = row.get("revenue")
                gross_profit = row.get("grossProfit")

                record = {
                    "ticker": ticker,
                    "period": period,
                    "date": date_str,
                    "filing_date": row.get("fillingDate") or row.get("filingDate"),
                    "revenue": revenue,
                    "net_income": row.get("netIncome"),
                    "eps": row.get("eps"),
                    "gross_margin": (gross_profit / revenue * 100) if revenue and gross_profit else None,
                    "operating_margin": (row.get("operatingIncome", 0) / revenue * 100) if revenue and row.get("operatingIncome") else None,
                    "roe": metric.get("roe"),
                    "total_assets": metric.get("totalAssets") or row.get("totalAssets"),
                    "total_equity": metric.get("totalEquity") or row.get("totalEquity"),
                    "total_debt": metric.get("totalDebt"),
                    "free_cash_flow": metric.get("freeCashFlow"),
                    "pe_ratio": metric.get("peRatio"),
                    "pb_ratio": metric.get("pbRatio"),
                }
                records.append(record)

            if records:
                df = pd.DataFrame(records)
                self.db.upsert_us_financial_data(df)
                total += len(records)

        logger.info(f"美股财务下载完成: {total} 条")
        return total

    def update_financial_data(self) -> int:
        """增量更新财务数据（最近 8 个季度）。"""
        return self.download_financial_data()

    # ----------------------------------------------------------
    # 行业分类
    # ----------------------------------------------------------

    def download_industry_class(self) -> int:
        """下载 GICS 行业分类（从 stock profile 提取）。"""
        tickers = self.db.get_us_tickers()
        if not tickers:
            return 0

        records = []
        batch_size = 50
        for i in tqdm(range(0, len(tickers), batch_size), desc="美股行业分类"):
            batch = tickers[i:i + batch_size]
            symbols_str = ",".join(batch)
            profiles = self._get(f"/api/v3/profile/{symbols_str}")
            if not profiles:
                continue
            for p in profiles:
                records.append({
                    "ticker": p.get("symbol", ""),
                    "sector": p.get("sector", ""),
                    "industry": p.get("industry", ""),
                    "sub_industry": "",  # FMP profile 无 sub-industry
                })

        if records:
            df = pd.DataFrame(records)
            self.db.upsert_us_industry_class(df)

        logger.info(f"美股行业分类下载完成: {len(records)} 条")
        return len(records)

    # ----------------------------------------------------------
    # 指数数据
    # ----------------------------------------------------------

    def download_index_daily(self, symbols: list[str] = None) -> int:
        """下载指数日线（默认 ^GSPC, ^IXIC, ^DJI）。"""
        if symbols is None:
            symbols = US_INDEX_SYMBOLS

        total = 0
        for symbol in symbols:
            data = self._get(
                f"/api/v3/historical-price-full/{symbol}",
                {"from": self._start_date},
            )
            if not data or "historical" not in data:
                continue

            records = []
            for row in data["historical"]:
                records.append({
                    "index_code": symbol,
                    "trade_date": row.get("date"),
                    "open": row.get("open"),
                    "high": row.get("high"),
                    "low": row.get("low"),
                    "close": row.get("close"),
                    "volume": row.get("volume"),
                })

            if records:
                df = pd.DataFrame(records)
                self.db.bulk_upsert_us_index_daily(df)
                total += len(records)

        logger.info(f"美股指数下载完成: {total} 条")
        return total

    def update_index_daily(self) -> int:
        """增量更新指数日线。"""
        try:
            result = self.db.query(
                "SELECT MAX(trade_date) as max_date FROM us_index_daily"
            )
            latest = result["max_date"].iloc[0]
            if pd.notna(latest):
                from_date = (pd.to_datetime(str(latest)) + timedelta(days=1)).strftime("%Y-%m-%d")
            else:
                from_date = self._start_date
        except Exception:
            from_date = self._start_date

        today = datetime.now().strftime("%Y-%m-%d")
        if from_date > today:
            logger.info("美股指数已是最新")
            return 0

        total = 0
        for symbol in US_INDEX_SYMBOLS:
            data = self._get(
                f"/api/v3/historical-price-full/{symbol}",
                {"from": from_date, "to": today},
            )
            if not data or "historical" not in data:
                continue

            records = []
            for row in data["historical"]:
                records.append({
                    "index_code": symbol,
                    "trade_date": row.get("date"),
                    "open": row.get("open"),
                    "high": row.get("high"),
                    "low": row.get("low"),
                    "close": row.get("close"),
                    "volume": row.get("volume"),
                })

            if records:
                df = pd.DataFrame(records)
                self.db.bulk_upsert_us_index_daily(df)
                total += len(records)

        logger.info(f"美股指数增量更新完成: {total} 条")
        return total

    # ----------------------------------------------------------
    # 商品期货
    # ----------------------------------------------------------

    def download_commodity_prices(self) -> int:
        """下载商品期货日线。"""
        total = 0
        for symbol in US_COMMODITY_SYMBOLS:
            data = self._get(
                f"/api/v3/historical-price-full/{symbol}",
                {"from": self._start_date},
            )
            if not data or "historical" not in data:
                continue

            records = []
            for row in data["historical"]:
                records.append({
                    "symbol": symbol,
                    "trade_date": row.get("date"),
                    "open": row.get("open"),
                    "high": row.get("high"),
                    "low": row.get("low"),
                    "close": row.get("close"),
                    "volume": row.get("volume"),
                })

            if records:
                df = pd.DataFrame(records)
                self.db.bulk_upsert_us_commodity_price(df)
                total += len(records)

        logger.info(f"美股商品期货下载完成: {total} 条")
        return total

    def update_commodity_prices(self) -> int:
        """增量更新商品期货。"""
        try:
            result = self.db.query(
                "SELECT MAX(trade_date) as max_date FROM us_commodity_price"
            )
            latest = result["max_date"].iloc[0]
            if pd.notna(latest):
                from_date = (pd.to_datetime(str(latest)) + timedelta(days=1)).strftime("%Y-%m-%d")
            else:
                from_date = self._start_date
        except Exception:
            from_date = self._start_date

        today = datetime.now().strftime("%Y-%m-%d")
        if from_date > today:
            logger.info("美股商品期货已是最新")
            return 0

        total = 0
        for symbol in US_COMMODITY_SYMBOLS:
            data = self._get(
                f"/api/v3/historical-price-full/{symbol}",
                {"from": from_date, "to": today},
            )
            if not data or "historical" not in data:
                continue

            records = []
            for row in data["historical"]:
                records.append({
                    "symbol": symbol,
                    "trade_date": row.get("date"),
                    "open": row.get("open"),
                    "high": row.get("high"),
                    "low": row.get("low"),
                    "close": row.get("close"),
                    "volume": row.get("volume"),
                })

            if records:
                df = pd.DataFrame(records)
                self.db.bulk_upsert_us_commodity_price(df)
                total += len(records)

        logger.info(f"美股商品期货增量更新完成: {total} 条")
        return total

    # ----------------------------------------------------------
    # 分析师评级
    # ----------------------------------------------------------

    def download_analyst_recommendations(self, tickers: list[str] = None) -> int:
        """下载分析师评级和目标价。"""
        if tickers is None:
            tickers = self.db.get_us_tickers()
        if not tickers:
            return 0

        total = 0
        for ticker in tqdm(tickers, desc="美股分析师评级"):
            data = self._get(f"/api/v3/analyst-stock-recommendations/{ticker}")
            if not data:
                continue

            records = []
            for row in data:
                date_str = row.get("date", "")
                if not date_str:
                    continue
                records.append({
                    "ticker": ticker,
                    "date": date_str,
                    "analyst_company": row.get("analystCompany", ""),
                    "analyst_name": row.get("analystName", ""),
                    "rating": row.get("newGrade") or row.get("rating", ""),
                    "price_target": row.get("priceTarget"),
                })

            # 补充 price-target 数据
            pt_data = self._get(f"/api/v3/price-target/{ticker}")
            if pt_data:
                for row in pt_data:
                    date_str = row.get("publishedDate", "")[:10]
                    if not date_str:
                        continue
                    records.append({
                        "ticker": ticker,
                        "date": date_str,
                        "analyst_company": row.get("analystCompany", ""),
                        "analyst_name": row.get("analystName", ""),
                        "rating": "",
                        "price_target": row.get("priceTarget"),
                    })

            if records:
                df = pd.DataFrame(records)
                # 去重（同 ticker+date+analyst_company 取最后一条）
                df = df.drop_duplicates(subset=["ticker", "date", "analyst_company"], keep="last")
                self.db.upsert_us_analyst_recommendation(df)
                total += len(df)

        logger.info(f"美股分析师评级下载完成: {total} 条")
        return total

    def update_analyst_recommendations(self) -> int:
        """增量更新分析师评级。"""
        return self.download_analyst_recommendations()

    # ----------------------------------------------------------
    # SEC 公告
    # ----------------------------------------------------------

    def download_sec_filings(self, tickers: list[str] = None, filing_type: str = None) -> int:
        """下载 SEC filings。"""
        if tickers is None:
            tickers = self.db.get_us_tickers()
        if not tickers:
            return 0

        total = 0
        for ticker in tqdm(tickers, desc="美股 SEC 公告"):
            params = {"limit": 100}
            if filing_type:
                params["type"] = filing_type

            data = self._get(f"/api/v3/sec_filings/{ticker}", params)
            if not data:
                continue

            records = []
            for row in data:
                filing_date = row.get("fillingDate", "") or row.get("filingDate", "")
                if not filing_date:
                    continue
                records.append({
                    "ticker": ticker,
                    "filing_date": filing_date[:10],
                    "type": row.get("type", ""),
                    "title": (row.get("title") or "")[:500],
                    "url": (row.get("finalLink") or row.get("link", ""))[:500],
                })

            if records:
                df = pd.DataFrame(records)
                self.db.upsert_us_sec_filing(df)
                total += len(records)

        logger.info(f"美股 SEC 公告下载完成: {total} 条")
        return total

    def update_sec_filings(self) -> int:
        """增量更新 SEC 公告。"""
        return self.download_sec_filings()

    # ----------------------------------------------------------
    # 公司行动（分红/拆股）
    # ----------------------------------------------------------

    def download_corporate_actions(self, tickers: list[str] = None) -> int:
        """下载分红和拆股历史。"""
        if tickers is None:
            tickers = self.db.get_us_tickers()
        if not tickers:
            return 0

        total = 0
        for ticker in tqdm(tickers, desc="美股公司行动"):
            records = []

            # 分红
            div_data = self._get(f"/api/v3/historical-price-full/stock_dividend/{ticker}")
            if div_data and "historical" in div_data:
                for row in div_data["historical"]:
                    date_str = row.get("date", "")
                    if not date_str:
                        continue
                    records.append({
                        "ticker": ticker,
                        "date": date_str,
                        "action_type": "dividend",
                        "label": row.get("label", ""),
                        "value": row.get("dividend"),
                    })

            # 拆股
            split_data = self._get(f"/api/v3/historical-price-full/stock_split/{ticker}")
            if split_data and "historical" in split_data:
                for row in split_data["historical"]:
                    date_str = row.get("date", "")
                    if not date_str:
                        continue
                    records.append({
                        "ticker": ticker,
                        "date": date_str,
                        "action_type": "split",
                        "label": row.get("label", ""),
                        "value": row.get("numerator"),
                    })

            if records:
                df = pd.DataFrame(records)
                self.db.upsert_us_corporate_action(df)
                total += len(records)

        logger.info(f"美股公司行动下载完成: {total} 条")
        return total

    def update_corporate_actions(self) -> int:
        """增量更新公司行动。"""
        return self.download_corporate_actions()

    # ----------------------------------------------------------
    # 全量下载
    # ----------------------------------------------------------

    def download_all(self) -> dict:
        """一键全量下载所有美股数据。"""
        results = {}
        results["stock_list"] = self.download_stock_list()
        results["daily_prices"] = self.download_daily_prices()
        results["financial_data"] = self.download_financial_data()
        results["industry_class"] = self.download_industry_class()
        results["index_daily"] = self.download_index_daily()
        results["commodity_prices"] = self.download_commodity_prices()
        results["analyst"] = self.download_analyst_recommendations()
        results["sec_filings"] = self.download_sec_filings()
        results["corporate_actions"] = self.download_corporate_actions()
        return results
