"""
美股数据下载器（yfinance 版）

使用 yfinance 获取以下数据并存入 MySQL：
    1. S&P 500 + NASDAQ 100 成分股列表
    2. 日线行情（含复权价）
    3. 季度财务数据
    4. GICS 行业分类
    5. 指数日线（S&P 500, NASDAQ, Dow Jones）
    6. 商品期货日线
    7. 分析师评级
    8. SEC 公告（yfinance 不支持，返回 0）
    9. 公司行动（分红/拆股）
"""

import logging
import re
import time
from datetime import datetime, timedelta, UTC

import pandas as pd
from tqdm import tqdm

from backend.services.config import (
    US_DATA_START_DATE,
    US_INDEX_SYMBOLS,
    US_COMMODITY_SYMBOLS,
    US_FALLBACK_TICKERS,
    LOG_LEVEL,
)
from backend.services.data.database import DatabaseManager

logger = logging.getLogger(__name__)
logger.setLevel(LOG_LEVEL)


def _check_yf():
    """检查 yfinance 是否安装，返回模块引用。"""
    try:
        import yfinance as yf
        return yf
    except ImportError:
        raise ImportError(
            "yfinance 未安装，请运行: pip install yfinance"
        )


# ============================================================
# 美股数据下载器
# ============================================================

class FMPDownloader:
    """美股数据下载器（yfinance 实现，保留类名兼容 API view 引用）"""

    def __init__(self, db: DatabaseManager):
        self.db = db
        self._start_date = datetime.strptime(US_DATA_START_DATE, "%Y%m%d").strftime("%Y-%m-%d")
        self._yf = _check_yf()

    def _safe_ticker(self, symbol: str):
        """创建 yfinance Ticker，捕获异常返回 None。"""
        try:
            return self._yf.Ticker(symbol)
        except Exception as e:
            logger.warning(f"yfinance Ticker 创建失败 {symbol}: {e}")
            return None

    # ----------------------------------------------------------
    # 股票列表
    # ----------------------------------------------------------

    def download_stock_list(self) -> int:
        """下载 S&P 500 + NASDAQ 100 成分股列表并 upsert。"""
        import requests
        from io import StringIO

        all_tickers = {}

        headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate, br",
            "Connection": "keep-alive",
            "Upgrade-Insecure-Requests": "1",
        }
        session = requests.Session()
        session.headers.update(headers)

        # --- 1. 从 Wikipedia 获取 S&P 500 成分股 ---
        sp500_count = 0
        try:
            response = session.get(
                "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies",
                timeout=15,
            )
            response.raise_for_status()
            tables = pd.read_html(StringIO(response.text), match="Symbol")
            if tables:
                wiki_df = tables[0]
                ticker_col = None
                name_col = None
                sector_col = None
                industry_col = None
                for col in wiki_df.columns:
                    col_lower = str(col).lower()
                    if "symbol" in col_lower or "ticker" in col_lower:
                        ticker_col = col
                    elif "security" in col_lower or "company" in col_lower or "name" in col_lower:
                        name_col = col
                    elif "gics sector" in col_lower or (
                        "sector" in col_lower and "sub" not in col_lower
                    ):
                        sector_col = col
                    elif "gics sub-industry" in col_lower or "sub" in col_lower or "industry" in col_lower:
                        industry_col = col

                if ticker_col:
                    for _, row in wiki_df.iterrows():
                        ticker = str(row.get(ticker_col, "")).strip().replace(".", "-")
                        if not ticker:
                            continue
                        all_tickers[ticker] = {
                            "ticker": ticker,
                            "name": str(row.get(name_col, "")) if name_col else "",
                            "exchange": "NYSE",
                            "sector": str(row.get(sector_col, "")) if sector_col else "",
                            "industry": str(row.get(industry_col, "")) if industry_col else "",
                            "is_active": 1,
                            "country": "US",
                        }
                    sp500_count = len(all_tickers)
                    logger.info(f"S&P 500 成分股: {sp500_count} 只")
                else:
                    logger.warning("Wikipedia S&P 500 表格未找到 Symbol 列")
            else:
                logger.warning("无法从 Wikipedia 获取 S&P 500 成分股表格")
        except Exception as e:
            logger.error(f"Wikipedia S&P 500 页面解析失败: {e}")

        # --- 2. 从 Wikipedia 获取 NASDAQ 100 成分股（合并到 all_tickers）---
        nq100_new = 0
        try:
            response = session.get(
                "https://en.wikipedia.org/wiki/Nasdaq-100",
                timeout=15,
            )
            response.raise_for_status()
            tables = pd.read_html(StringIO(response.text), match="Ticker")
            if tables:
                wiki_df = tables[0]

                # 识别列名（Wikipedia 表格列名可能变化）
                ticker_col = None
                name_col = None
                sector_col = None
                industry_col = None
                for col in wiki_df.columns:
                    col_lower = str(col).lower()
                    if "ticker" in col_lower or "symbol" in col_lower:
                        ticker_col = col
                    elif "company" in col_lower or "name" in col_lower:
                        name_col = col
                    elif "sector" in col_lower and "sub" not in col_lower:
                        sector_col = col
                    elif "sub" in col_lower or "industry" in col_lower:
                        industry_col = col

                if ticker_col:
                    for _, row in wiki_df.iterrows():
                        ticker = str(row.get(ticker_col, "")).strip().replace(".", "-")
                        if not ticker:
                            continue
                        if ticker not in all_tickers:
                            all_tickers[ticker] = {
                                "ticker": ticker,
                                "name": str(row.get(name_col, "")) if name_col else "",
                                "exchange": "NASDAQ",
                                "sector": str(row.get(sector_col, "")) if sector_col else "",
                                "industry": str(row.get(industry_col, "")) if industry_col else "",
                                "is_active": 1,
                                "country": "US",
                            }
                            nq100_new += 1
                    logger.info(f"NASDAQ 100 成分股: 新增 {nq100_new} 只（S&P 500 已有的不重复添加）")
                else:
                    logger.warning("Wikipedia NASDAQ 100 表格未找到 Ticker 列")
            else:
                logger.warning("无法从 Wikipedia 获取 NASDAQ 100 成分股表格")
        except Exception as e:
            logger.error(f"Wikipedia NASDAQ 100 页面解析失败: {e}")

        # --- 3. 兜底：Wikipedia 全部失败时使用 US_FALLBACK_TICKERS ---
        if not all_tickers:
            logger.warning("Wikipedia 抓取全部失败，使用 US_FALLBACK_TICKERS 兜底")
            for ticker in US_FALLBACK_TICKERS:
                all_tickers[ticker] = {
                    "ticker": ticker,
                    "name": "",
                    "exchange": "NASDAQ",
                    "sector": "",
                    "industry": "",
                    "is_active": 1,
                    "country": "US",
                }

        if not all_tickers:
            logger.error("无法获取任何美股代码")
            return 0

        # --- 4. Upsert + Deactivate ---
        df = pd.DataFrame(list(all_tickers.values()))
        self.db.upsert_us_stock_basic(df)
        self.db.deactivate_us_stocks_not_in(set(all_tickers.keys()))
        logger.info(f"美股列表下载完成: 共 {len(all_tickers)} 只（S&P 500: {sp500_count}, NASDAQ 100 新增: {nq100_new}）")
        return len(all_tickers)

    # ----------------------------------------------------------
    # 日线行情
    # ----------------------------------------------------------

    def _download_prices_for(self, tickers: list[str], start: str, end: str, desc: str) -> int:
        """通用日线下载辅助方法。"""
        total = 0
        for ticker in tqdm(tickers, desc=desc):
            t = self._safe_ticker(ticker)
            if t is None:
                continue
            try:
                hist = t.history(start=start, end=end, auto_adjust=False)
                if hist.empty:
                    continue

                records = []
                for date_idx, row in hist.iterrows():
                    records.append({
                        "ticker": ticker,
                        "trade_date": _safe_date_str(date_idx),
                        "open": row.get("Open"),
                        "high": row.get("High"),
                        "low": row.get("Low"),
                        "close": row.get("Close"),
                        "adj_close": row.get("Adj Close"),
                        "volume": row.get("Volume"),
                        "change_pct": None,  # yfinance 不直接提供，DB 层可后续计算
                    })

                if records:
                    df = pd.DataFrame(records)
                    self.db.bulk_upsert_us_daily_price(df)
                    total += len(records)

            except Exception as e:
                logger.warning(f"日线下载失败 {ticker}: {e}")

        return total

    def download_daily_prices(self, tickers: list[str] = None) -> int:
        """全量下载日线数据。"""
        if tickers is None:
            tickers = self.db.get_us_tickers()
        if not tickers:
            logger.warning("无美股代码，请先下载股票列表")
            return 0

        today = datetime.now().strftime("%Y-%m-%d")
        total = self._download_prices_for(tickers, self._start_date, today, "美股日线下载")
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

        total = self._download_prices_for(tickers, from_date, today, "美股日线增量更新")
        logger.info(f"美股日线增量更新完成: {total} 条")
        return total

    # ----------------------------------------------------------
    # 财务数据
    # ----------------------------------------------------------

    def download_financial_data(self, tickers: list[str] = None) -> int:
        """下载季度财报（income_stmt + balance_sheet + cashflow）。"""
        if tickers is None:
            tickers = self.db.get_us_tickers()
        if not tickers:
            return 0

        total = 0
        for ticker in tqdm(tickers, desc="美股财务下载"):
            t = self._safe_ticker(ticker)
            if t is None:
                continue

            try:
                income = t.quarterly_income_stmt
                balance = t.quarterly_balance_sheet
                cashflow = t.quarterly_cashflow
            except Exception as e:
                logger.warning(f"财务数据获取失败 {ticker}: {e}")
                continue

            if income is None or income.empty:
                continue

            # 从 sec_filings 构建 报告期末日 → SEC提交日 映射
            # income_stmt 用标准季末日（如 12-31），实际财年季末可能偏移几天（如 12-27）
            # 因此用 ±10 天近似匹配
            filing_date_map = {}
            try:
                sec_filings = t.sec_filings
                if sec_filings:
                    _sec_entries = []  # [(报告期末日, SEC提交日)]
                    for sf in sec_filings:
                        if sf.get("type") not in ("10-Q", "10-K"):
                            continue
                        sf_date = sf.get("date")  # SEC 提交日
                        if sf_date is None:
                            continue
                        # edgarUrl / exhibits URL 中含报告期末日, 如 aapl-20251227.htm
                        edgar = sf.get("edgarUrl", "")
                        for url in [edgar] + list((sf.get("exhibits") or {}).values()):
                            m = re.search(r"-(\d{8})\.", url)
                            if m:
                                try:
                                    report_end = datetime.strptime(m.group(1), "%Y%m%d").date()
                                    _sec_entries.append((report_end, sf_date))
                                except ValueError:
                                    pass
                                break
                    # 对 income_stmt 每个季末日做近似匹配
                    for col_date in income.columns:
                        target = col_date.date()
                        best = None
                        best_delta = timedelta(days=11)
                        for report_end, sf_date in _sec_entries:
                            delta = abs(target - report_end)
                            if delta < best_delta:
                                best_delta = delta
                                best = sf_date
                        if best is not None:
                            filing_date_map[target] = best
            except Exception as e:
                logger.debug(f"sec_filings 获取失败 {ticker}: {e}")

            # 将 balance/cashflow 转为按日期索引的 dict
            balance_map = {}
            if balance is not None and not balance.empty:
                for col in balance.columns:
                    balance_map[col] = balance[col]

            cashflow_map = {}
            if cashflow is not None and not cashflow.empty:
                for col in cashflow.columns:
                    cashflow_map[col] = cashflow[col]

            records = []
            for col_date in income.columns:
                date_str = col_date.strftime("%Y-%m-%d")
                inc = income[col_date]

                # 从 balance sheet 取同期数据
                bal = balance_map.get(col_date, pd.Series(dtype=float))
                cf = cashflow_map.get(col_date, pd.Series(dtype=float))

                revenue = _safe_get(inc, "Total Revenue")
                gross_profit = _safe_get(inc, "Gross Profit")
                operating_income = _safe_get(inc, "Operating Income")
                net_income = _safe_get(inc, "Net Income")

                # EPS: 优先 Basic EPS，回退 Diluted EPS
                eps = _safe_get(inc, "Basic EPS") or _safe_get(inc, "Diluted EPS")

                # 利润率计算
                gross_margin = None
                if revenue and gross_profit:
                    try:
                        gross_margin = float(gross_profit) / float(revenue) * 100
                    except (ZeroDivisionError, TypeError):
                        pass

                operating_margin = None
                if revenue and operating_income:
                    try:
                        operating_margin = float(operating_income) / float(revenue) * 100
                    except (ZeroDivisionError, TypeError):
                        pass

                # 确定 period（Q1-Q4）
                month = col_date.month
                if month <= 3:
                    period = f"Q1 {col_date.year}"
                elif month <= 6:
                    period = f"Q2 {col_date.year}"
                elif month <= 9:
                    period = f"Q3 {col_date.year}"
                else:
                    period = f"Q4 {col_date.year}"

                # filing_date: 优先 SEC 提交日，兜底用报告期末日
                filing_date = filing_date_map.get(col_date.date(), date_str)

                record = {
                    "ticker": ticker,
                    "period": period,
                    "date": date_str,
                    "filing_date": filing_date,
                    "revenue": revenue,
                    "net_income": net_income,
                    "eps": eps,
                    "gross_margin": gross_margin,
                    "operating_margin": operating_margin,
                    "roe": None,  # yfinance 季报不直接提供
                    "total_assets": _safe_get(bal, "Total Assets"),
                    "total_equity": _safe_get(bal, "Stockholders Equity"),
                    "total_debt": _safe_get(bal, "Total Debt"),
                    "free_cash_flow": _safe_get(cf, "Free Cash Flow"),
                    "pe_ratio": None,  # yfinance 季报不直接提供
                    "pb_ratio": None,  # yfinance 季报不直接提供
                }
                records.append(record)

            if records:
                df = pd.DataFrame(records)
                self.db.upsert_us_financial_data(df)
                total += len(records)

            time.sleep(0.1)

        logger.info(f"美股财务下载完成: {total} 条")
        return total

    def update_financial_data(self) -> int:
        """增量更新财务数据（最近 8 个季度）。"""
        return self.download_financial_data()

    # ----------------------------------------------------------
    # 行业分类
    # ----------------------------------------------------------

    def download_industry_class(self) -> int:
        """下载 GICS 行业分类（从 yfinance info 提取）。"""
        tickers = self.db.get_us_tickers()
        if not tickers:
            return 0

        records = []
        for ticker in tqdm(tickers, desc="美股行业分类"):
            t = self._safe_ticker(ticker)
            if t is None:
                continue
            try:
                info = t.info
                if not info:
                    continue
                records.append({
                    "ticker": ticker,
                    "sector": info.get("sector", ""),
                    "industry": info.get("industry", ""),
                    "sub_industry": "",
                })
            except Exception as e:
                logger.debug(f"行业信息获取失败 {ticker}: {e}")

            time.sleep(0.1)

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

        today = datetime.now().strftime("%Y-%m-%d")
        total = 0
        for symbol in symbols:
            t = self._safe_ticker(symbol)
            if t is None:
                continue
            try:
                hist = t.history(start=self._start_date, end=today, auto_adjust=False)
                if hist.empty:
                    continue

                records = []
                for date_idx, row in hist.iterrows():
                    records.append({
                        "index_code": symbol,
                        "trade_date": _safe_date_str(date_idx),
                        "open": row.get("Open"),
                        "high": row.get("High"),
                        "low": row.get("Low"),
                        "close": row.get("Close"),
                        "volume": row.get("Volume"),
                    })

                if records:
                    df = pd.DataFrame(records)
                    self.db.bulk_upsert_us_index_daily(df)
                    total += len(records)

            except Exception as e:
                logger.warning(f"指数下载失败 {symbol}: {e}")

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
            t = self._safe_ticker(symbol)
            if t is None:
                continue
            try:
                hist = t.history(start=from_date, end=today, auto_adjust=False)
                if hist.empty:
                    continue

                records = []
                for date_idx, row in hist.iterrows():
                    records.append({
                        "index_code": symbol,
                        "trade_date": _safe_date_str(date_idx),
                        "open": row.get("Open"),
                        "high": row.get("High"),
                        "low": row.get("Low"),
                        "close": row.get("Close"),
                        "volume": row.get("Volume"),
                    })

                if records:
                    df = pd.DataFrame(records)
                    self.db.bulk_upsert_us_index_daily(df)
                    total += len(records)

            except Exception as e:
                logger.warning(f"指数增量更新失败 {symbol}: {e}")

        logger.info(f"美股指数增量更新完成: {total} 条")
        return total

    # ----------------------------------------------------------
    # 商品期货
    # ----------------------------------------------------------

    def download_commodity_prices(self) -> int:
        """下载商品期货日线。"""
        today = datetime.now().strftime("%Y-%m-%d")
        total = 0
        for symbol in US_COMMODITY_SYMBOLS:
            t = self._safe_ticker(symbol)
            if t is None:
                continue
            try:
                hist = t.history(start=self._start_date, end=today, auto_adjust=False)
                if hist.empty:
                    continue

                records = []
                for date_idx, row in hist.iterrows():
                    records.append({
                        "symbol": symbol,
                        "trade_date": _safe_date_str(date_idx),
                        "open": row.get("Open"),
                        "high": row.get("High"),
                        "low": row.get("Low"),
                        "close": row.get("Close"),
                        "volume": row.get("Volume"),
                    })

                if records:
                    df = pd.DataFrame(records)
                    self.db.bulk_upsert_us_commodity_price(df)
                    total += len(records)

            except Exception as e:
                logger.warning(f"商品期货下载失败 {symbol}: {e}")

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
            t = self._safe_ticker(symbol)
            if t is None:
                continue
            try:
                hist = t.history(start=from_date, end=today, auto_adjust=False)
                if hist.empty:
                    continue

                records = []
                for date_idx, row in hist.iterrows():
                    records.append({
                        "symbol": symbol,
                        "trade_date": _safe_date_str(date_idx),
                        "open": row.get("Open"),
                        "high": row.get("High"),
                        "low": row.get("Low"),
                        "close": row.get("Close"),
                        "volume": row.get("Volume"),
                    })

                if records:
                    df = pd.DataFrame(records)
                    self.db.bulk_upsert_us_commodity_price(df)
                    total += len(records)

            except Exception as e:
                logger.warning(f"商品期货增量更新失败 {symbol}: {e}")

        logger.info(f"美股商品期货增量更新完成: {total} 条")
        return total

    # ----------------------------------------------------------
    # 分析师评级
    # ----------------------------------------------------------

    def download_analyst_recommendations(self, tickers: list[str] = None) -> int:
        """下载分析师评级（yfinance upgrades_downgrades）。"""
        if tickers is None:
            tickers = self.db.get_us_tickers()
        if not tickers:
            return 0

        total = 0
        for ticker in tqdm(tickers, desc="美股分析师评级"):
            t = self._safe_ticker(ticker)
            if t is None:
                continue

            try:
                ud = t.upgrades_downgrades
                if ud is None or ud.empty:
                    continue

                records = []
                for date_idx, row in ud.iterrows():
                    try:
                        date_str = date_idx.strftime("%Y-%m-%d")
                    except Exception:
                        date_str = str(date_idx)[:10]

                    records.append({
                        "ticker": ticker,
                        "date": date_str,
                        "analyst_company": row.get("Firm", ""),
                        "analyst_name": "",  # yfinance 不提供
                        "rating": row.get("ToGrade", ""),
                        "price_target": None,  # yfinance 不提供
                    })

                if records:
                    df = pd.DataFrame(records)
                    df = df.drop_duplicates(
                        subset=["ticker", "date", "analyst_company"], keep="last"
                    )
                    self.db.upsert_us_analyst_recommendation(df)
                    total += len(df)

            except Exception as e:
                logger.warning(f"分析师评级获取失败 {ticker}: {e}")

            time.sleep(0.1)

        logger.info(f"美股分析师评级下载完成: {total} 条")
        return total

    def update_analyst_recommendations(self) -> int:
        """增量更新分析师评级。"""
        return self.download_analyst_recommendations()

    # ----------------------------------------------------------
    # SEC 公告（yfinance 不支持）
    # ----------------------------------------------------------

    def download_sec_filings(self, tickers: list[str] = None, filing_type: str = None) -> int:
        """SEC filings — yfinance 不支持，返回 0。"""
        logger.info("SEC filings 下载跳过（yfinance 不支持此功能，请使用 SEC EDGAR API）")
        return 0

    def update_sec_filings(self) -> int:
        """增量更新 SEC 公告 — 跳过。"""
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
            t = self._safe_ticker(ticker)
            if t is None:
                continue

            records = []

            # 分红
            try:
                divs = t.dividends
                if divs is not None and not divs.empty:
                    for date_idx, value in divs.items():
                        records.append({
                            "ticker": ticker,
                            "date": _safe_date_str(date_idx),
                            "action_type": "dividend",
                            "label": f"${value:.4f} per share",
                            "value": float(value),
                        })
            except Exception as e:
                logger.debug(f"分红数据获取失败 {ticker}: {e}")

            # 拆股
            try:
                splits = t.splits
                if splits is not None and not splits.empty:
                    for date_idx, value in splits.items():
                        records.append({
                            "ticker": ticker,
                            "date": _safe_date_str(date_idx),
                            "action_type": "split",
                            "label": f"{value:.0f}:1 split",
                            "value": float(value),
                        })
            except Exception as e:
                logger.debug(f"拆股数据获取失败 {ticker}: {e}")

            if records:
                df = pd.DataFrame(records)
                self.db.upsert_us_corporate_action(df)
                total += len(records)

            time.sleep(0.1)

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


# ============================================================
# 辅助函数
# ============================================================

def _safe_get(series: pd.Series, key: str):
    """从 yfinance 财务 Series 中安全取值，找不到返回 None。"""
    if series is None or series.empty:
        return None
    try:
        val = series.get(key)
        if val is not None and pd.notna(val):
            return float(val)
    except (TypeError, ValueError):
        pass
    return None

def _safe_date_str(date_idx):
    """Safely convert date_idx to YYYY-MM-DD string."""
    try:
        if hasattr(date_idx, 'strftime'):
            return date_idx.strftime("%Y-%m-%d")
        return pd.to_datetime(date_idx).strftime("%Y-%m-%d")
    except Exception:
        return str(date_idx)[:10]

