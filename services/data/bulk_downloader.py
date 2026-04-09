"""
四家 API 统一批量下载器

数据源：
    - FMP (Financial Modeling Prep) — 财报/metrics/earnings/行情/insider
    - Unusual Whales — 期权 flow/暗池/国会交易/新闻
    - Fiscal.ai — 日频估值比率/业务分部

所有 bulk 端点按年下载 CSV，自动重试 + rate limit。
"""

import io
import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import pandas as pd
import requests
from tqdm import tqdm

from services.config import (
    FMP_API_KEY, FMP_RATE_LIMIT, FMP_BULK_INTERVAL,
    UW_API_KEY, UW_RATE_LIMIT,
    FISCAL_API_KEY, FISCAL_RATE_LIMIT,
    LOG_LEVEL,
)
from services.data.database import DatabaseManager

logger = logging.getLogger(__name__)
logger.setLevel(LOG_LEVEL)


# ============================================================
# Rate Limiter
# ============================================================

class RateLimiter:
    """Thread-safe token-bucket rate limiter."""
    def __init__(self, calls_per_minute: int):
        import threading
        self._interval = 60.0 / max(calls_per_minute, 1)
        self._last_call = 0.0
        self._lock = threading.Lock()

    def wait(self):
        with self._lock:
            now = time.time()
            elapsed = now - self._last_call
            if elapsed < self._interval:
                time.sleep(self._interval - elapsed)
            self._last_call = time.time()


def _request_with_retry(method: str, url: str, max_retries: int = 3, **kwargs) -> requests.Response:
    """HTTP request with exponential backoff on 429/5xx."""
    kwargs.setdefault("timeout", 60)
    for attempt in range(max_retries):
        try:
            resp = requests.request(method, url, **kwargs)
            if resp.status_code == 429:
                wait = 2 ** (attempt + 2)  # 4s, 8s, 16s
                logger.warning(f"Rate limited (429), waiting {wait}s (attempt {attempt+1}/{max_retries})...")
                time.sleep(wait)
                continue
            if resp.status_code >= 500:
                logger.warning(f"Server error ({resp.status_code}), retrying...")
                time.sleep(2 ** attempt)
                continue
            return resp
        except requests.exceptions.RequestException as e:
            logger.warning(f"HTTP 请求异常 (attempt {attempt+1}/{max_retries}): {e}")
            if attempt == max_retries - 1:
                raise
            time.sleep(2 ** attempt)
    return resp


# ============================================================
# BulkDownloader
# ============================================================

class BulkDownloader:
    """四家 API 统一批量下载器"""

    def __init__(self, db: DatabaseManager):
        self.db = db
        self._fmp_limiter = RateLimiter(FMP_RATE_LIMIT)
        self._uw_limiter = RateLimiter(UW_RATE_LIMIT)
        self._fiscal_limiter = RateLimiter(FISCAL_RATE_LIMIT)

    # ==============================================================
    # FMP Helpers
    # ==============================================================

    def _fmp_get_json(self, path: str, params: dict = None, version: str = "v3") -> list | dict:
        """FMP JSON API call (per-ticker endpoints)."""
        if not FMP_API_KEY:
            logger.warning("FMP_API_KEY 未设置")
            return []
        self._fmp_limiter.wait()
        url = f"https://financialmodelingprep.com/api/{version}/{path}"
        p = {"apikey": FMP_API_KEY}
        if params:
            p.update(params)
        resp = _request_with_retry("GET", url, params=p)
        if resp.status_code != 200:
            logger.warning(f"FMP {path}: HTTP {resp.status_code}")
            return []
        data = resp.json()
        if isinstance(data, dict) and "Error Message" in data:
            logger.warning(f"FMP error: {data['Error Message']}")
            return []
        return data

    def _fmp_get_stable_json(self, endpoint: str, params: dict = None) -> list | dict:
        """FMP stable JSON API call (/stable/xxx)."""
        if not FMP_API_KEY:
            logger.warning("FMP_API_KEY 未设置")
            return []
        self._fmp_limiter.wait()
        url = f"https://financialmodelingprep.com/stable/{endpoint}"
        p = {"apikey": FMP_API_KEY}
        if params:
            p.update(params)
        resp = _request_with_retry("GET", url, params=p)
        if resp.status_code != 200:
            logger.warning(f"FMP stable {endpoint}: HTTP {resp.status_code}")
            return []
        data = resp.json()
        if isinstance(data, dict) and "Error Message" in data:
            logger.warning(f"FMP stable error: {data['Error Message']}")
            return []
        return data

    def _fmp_get_bulk_csv(self, endpoint: str, params: dict = None) -> pd.DataFrame:
        """FMP bulk CSV endpoint — /stable/xxx-bulk?year=..."""
        if not FMP_API_KEY:
            logger.warning("FMP_API_KEY 未设置")
            return pd.DataFrame()
        url = f"https://financialmodelingprep.com/stable/{endpoint}"
        p = {"apikey": FMP_API_KEY}
        if params:
            p.update(params)
        resp = _request_with_retry("GET", url, params=p)
        if resp.status_code != 200:
            logger.warning(f"FMP bulk {endpoint}: HTTP {resp.status_code}")
            return pd.DataFrame()
        text = resp.text.strip()
        if not text or text.startswith("{"):
            logger.warning(f"FMP bulk {endpoint} 非CSV响应: {text[:200]}")
            return pd.DataFrame()
        try:
            return pd.read_csv(io.StringIO(text))
        except Exception as e:
            logger.warning(f"FMP bulk CSV 解析失败 {endpoint}: {e}")
            return pd.DataFrame()

    def _fmp_bulk_by_year(self, endpoint: str, years: list[int],
                           extra_params: dict = None, desc: str = "",
                           on_data=None) -> int:
        """按年循环调用 FMP bulk 端点，拉到一年立即回调写入。
        on_data: callable(df) -> int，每年数据就绪时回调，返回写入条数。
        """
        total = 0
        for year in tqdm(years, desc=desc or f"FMP {endpoint}"):
            params = {"year": year}
            if extra_params:
                params.update(extra_params)

            for attempt in range(3):
                df = self._fmp_get_bulk_csv(endpoint, params)
                if not df.empty:
                    if on_data:
                        total += on_data(df)
                    logger.debug(f"_fmp_bulk_by_year: 结束循环 (total += on_data(df))")

                    break
                if attempt < 2:
                    wait = FMP_BULK_INTERVAL * (attempt + 2)
                    logger.info(f"  year={year} 空结果, 等待 {wait}s 重试...")
                    time.sleep(wait)

            time.sleep(FMP_BULK_INTERVAL)
        return total

    # ==============================================================
    # FMP Bulk Downloads
    # ==============================================================

    def download_fmp_stock_list(self) -> int:
        """下载全市场美股列表 (FMP stock-screener)."""
        all_records = []
        for exchange in ["NYSE", "NASDAQ", "AMEX"]:
            data = self._fmp_get_json(
                "stock-screener",
                params={"exchange": exchange, "limit": 20000, "isActivelyTrading": "true"},
            )
            if isinstance(data, list):
                all_records.extend(data)
            else:
                logger.warning(f"FMP stock-screener {exchange}: 返回非列表类型 {type(data)}")
            logger.info(f"FMP stock-screener {exchange}: {len(data) if isinstance(data, list) else 0} 只")

        if not all_records:
            logger.warning("FMP stock-screener: 所有交易所返回空数据")
            return 0

        df = pd.DataFrame(all_records)
        # 先删掉原始 exchange 列（长名如 "New York Stock Exchange"），保留 exchangeShortName
        if "exchange" in df.columns and "exchangeShortName" in df.columns:
            df = df.drop(columns=["exchange"])
        col_map = {
            "symbol": "ticker", "companyName": "name",
            "marketCap": "market_cap", "sector": "sector",
            "industry": "industry", "exchangeShortName": "exchange",
            "country": "country",
        }
        df = df.rename(columns=col_map)
        df["is_active"] = 1
        keep = [c for c in ["ticker", "name", "market_cap", "sector", "industry",
                             "exchange", "country", "is_active"] if c in df.columns]
        df = df[keep].drop_duplicates(subset=["ticker"])

        # Upsert to us_stock_basic
        self.db.upsert_us_stock_basic(df)

        # Also upsert industry classification
        ind_df = df[["ticker", "sector", "industry"]].dropna(subset=["sector"])
        if not ind_df.empty:
            self.db.upsert_us_industry_class(ind_df)

        logger.info(f"FMP 全市场股票列表: {len(df)} 只")
        return len(df)

    def download_fmp_index_constituents(self) -> int:
        """下载 SP500 + NASDAQ 100 当前成分 + 历史变更。"""
        total = 0

        # SP500 current
        sp500 = self._fmp_get_json("sp500_constituent")
        logger.info(f"FMP SP500 当前成分: {len(sp500)} 只")
        total += len(sp500)

        # SP500 historical changes
        sp500_hist = self._fmp_get_json("historical/sp500_constituent")
        logger.info(f"FMP SP500 历史变更: {len(sp500_hist)} 条")
        total += len(sp500_hist)

        # NASDAQ 100 current
        ndx100 = self._fmp_get_json("nasdaq_constituent")
        logger.info(f"FMP NASDAQ 100 当前成分: {len(ndx100)} 只")
        total += len(ndx100)

        # NASDAQ 100 historical changes
        ndx100_hist = self._fmp_get_json("historical/nasdaq_constituent")
        logger.info(f"FMP NASDAQ 100 历史变更: {len(ndx100_hist)} 条")
        total += len(ndx100_hist)

        # Merge all tickers into us_stock_basic (ensure they exist)
        all_tickers = set()
        for item in sp500 + ndx100:
            sym = item.get("symbol")
            if sym:
                all_tickers.add(sym)
        # Also add removed tickers from historical changes (survivorship bias)
        for item in sp500_hist + ndx100_hist:
            removed = item.get("removedTicker")
            if removed:
                all_tickers.add(removed)
            added = item.get("symbol")
            if added:
                all_tickers.add(added)

        # Mark index membership
        sp500_set = {item["symbol"] for item in sp500 if item.get("symbol")}
        ndx100_set = {item["symbol"] for item in ndx100 if item.get("symbol")}

        records = []
        for item in sp500 + ndx100:
            sym = item.get("symbol")
            if not sym:
                logger.debug(f"download_fmp_index_constituents: 跳过 (not sym)")

                continue
            records.append({
                "ticker": sym,
                "name": item.get("name"),
                "sector": item.get("sector"),
                "is_active": 1,
            })
        if records:
            df = pd.DataFrame(records).drop_duplicates(subset=["ticker"])
            self.db.upsert_us_stock_basic(df)

        logger.info(f"SP500+NASDAQ100 合并: {len(all_tickers)} 只 (含历史退出)")
        return total

    def download_fmp_earnings_surprises_bulk(self, start_year: int = 1995, end_year: int = None) -> int:
        """FMP bulk: earnings surprises by year, 每年立即写入。"""
        if end_year is None:
            end_year = datetime.now().year
        years = list(range(start_year, end_year + 1))

        def _on_data(df):
            col_map = {"symbol": "ticker", "epsActual": "actual_eps", "epsEstimated": "estimated_eps"}
            df = df.rename(columns=col_map)
            df = df[df["ticker"].notna()].copy()
            df["surprise"] = df["actual_eps"] - df["estimated_eps"]
            df["surprise_pct"] = df.apply(
                lambda r: r["surprise"] / abs(r["estimated_eps"]) if r["estimated_eps"] != 0 else None,
                axis=1,
            )
            keep = ["ticker", "date", "actual_eps", "estimated_eps", "surprise", "surprise_pct"]
            self.db.upsert_us_earnings_surprise(df[keep])
            return len(df)

        total = self._fmp_bulk_by_year("earnings-surprises-bulk", years,
                                        desc="FMP Earnings Surprises", on_data=_on_data)
        logger.info(f"FMP earnings surprises bulk 总计: {total} 条")
        return total

    def download_fmp_eps_estimates_bulk(self, start_year: int = 1995, end_year: int = None) -> int:
        """FMP per-ticker: analyst estimates (EPS consensus), 多线程。
        Bulk 端点不稳定，改用 per-ticker /api/v3/analyst-estimates。"""
        tickers = self.db.get_us_tickers()
        if not tickers:
            logger.debug(f"download_fmp_eps_estimates_bulk: 空返回 (not tickers)")

            return 0

        total = 0

        def _fetch_estimates_single(ticker):
            data = self._fmp_get_json(
                f"analyst-estimates/{ticker}",
                params={"period": "quarter", "limit": 200},
            )
            if not data:
                logger.debug(f"_fetch_estimates_single: 空返回 (not data)")

                return 0
            records = []
            for item in data:
                eps_avg = item.get("estimatedEpsAvg")
                if eps_avg is None:
                    logger.debug(f"_fetch_estimates_single: 跳过 (eps_avg is None)")

                    continue
                records.append({
                    "ticker": ticker,
                    "date": item["date"],
                    "eps_avg": eps_avg,
                    "eps_low": item.get("estimatedEpsLow"),
                    "eps_high": item.get("estimatedEpsHigh"),
                    "num_analysts": item.get("numberAnalystsEstimatedEps"),
                    "revenue_avg": item.get("estimatedRevenueAvg"),
                    "net_income_avg": item.get("estimatedNetIncomeAvg"),
                })
            if records:
                df = pd.DataFrame(records)
                self.db.upsert_us_eps_estimate(df)
            return len(records)

        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = {pool.submit(_fetch_estimates_single, t): t for t in tickers}
            for future in tqdm(as_completed(futures), total=len(futures), desc="FMP EPS Estimates"):
                try:
                    total += future.result()
                except Exception as e:
                    logger.warning(f"EPS estimate 失败 {futures[future]}: {e}")

        logger.info(f"FMP EPS estimates 总计: {total} 条")
        return total

    def download_fmp_income_statement_bulk(self, start_year: int = 1995, end_year: int = None) -> int:
        """FMP bulk: income statement by year → us_financial_data, 每年立即写入。"""
        if end_year is None:
            end_year = datetime.now().year
        years = list(range(start_year, end_year + 1))

        def _on_data(df):
            df = df.rename(columns={"symbol": "ticker"})
            df = df[df["ticker"].notna()].copy()
            if "period" in df.columns and "fiscalYear" in df.columns:
                df["period_label"] = df["fiscalYear"].astype(str) + "-" + df["period"]
            elif "date" in df.columns:
                df["period_label"] = df["date"].apply(
                    lambda d: f"{str(d)[:4]}-Q{(int(str(d)[5:7])-1)//3+1}" if pd.notna(d) else None
                )
            result = pd.DataFrame({
                "ticker": df["ticker"],
                "period": df.get("period_label"),
                "date": df["date"],
                "filing_date": df.get("filingDate", df.get("fillingDate", df.get("acceptedDate"))),
                "revenue": df.get("revenue"),
                "cost_of_revenue": df.get("costOfRevenue"),
                "gross_profit": df.get("grossProfit"),
                "operating_income": df.get("operatingIncome"),
                "net_income": df.get("netIncome"),
                "eps": df.get("eps"),
                "eps_diluted": df.get("epsDiluted", df.get("epsdiluted")),
                "ebitda": df.get("ebitda"),
                "gross_margin": df.get("grossProfitRatio"),
                "operating_margin": df.get("operatingIncomeRatio"),
                "net_margin": df.get("netIncomeRatio"),
                "rd_expenses": df.get("researchAndDevelopmentExpenses"),
                "sga_expenses": df.get("sellingGeneralAndAdministrativeExpenses"),
                "weighted_avg_shares": df.get("weightedAverageShsOutDil", df.get("weightedAverageShsOut")),
            })
            result = result.dropna(subset=["ticker", "date"])
            if not result.empty:
                self.db.upsert_us_financial_data(result)
                return len(result)
            logger.debug(f"_on_data: 空返回 (return len(result))")

            return 0

        total = self._fmp_bulk_by_year(
            "income-statement-bulk", years,
            extra_params={"period": "quarter"},
            desc="FMP Income Statement", on_data=_on_data,
        )
        logger.info(f"FMP income statement bulk 总计: {total} 条")
        return total

    def download_fmp_financial_quarterly(self, tickers: list[str] = None, limit: int = 400) -> int:
        """FMP per-ticker: 季度财报 (IS+BS+CF 三表合并) → us_financial_data.

        使用 /stable/ 端点逐 ticker 下载季度数据，合并三张报表后 upsert。
        替代已失效的 bulk 端点。

        Args:
            tickers: 要下载的 ticker 列表，None=全部
            limit: 每个 ticker 最多拉取的季度数（默认 400 ~ 100年）
        """
        if tickers is None:
            tickers = self.db.get_us_tickers()
        if not tickers:
            logger.warning("download_fmp_financial_quarterly: 无 ticker 可下载")
            return 0

        total = 0

        def _fetch_single(ticker):
            # 1. Income Statement
            is_data = self._fmp_get_stable_json(
                "income-statement",
                params={"symbol": ticker, "period": "quarter", "limit": limit},
            )
            if not is_data:
                logger.debug(f"financial_quarterly: {ticker} IS 无数据")
                return 0

            # 构建 IS DataFrame
            is_df = pd.DataFrame(is_data)
            is_df = is_df.rename(columns={"symbol": "ticker"})

            # 2. Balance Sheet
            bs_data = self._fmp_get_stable_json(
                "balance-sheet-statement",
                params={"symbol": ticker, "period": "quarter", "limit": limit},
            )
            bs_df = pd.DataFrame(bs_data) if bs_data else pd.DataFrame()
            if not bs_df.empty:
                bs_df = bs_df.rename(columns={"symbol": "ticker"})

            # 3. Cash Flow
            cf_data = self._fmp_get_stable_json(
                "cash-flow-statement",
                params={"symbol": ticker, "period": "quarter", "limit": limit},
            )
            cf_df = pd.DataFrame(cf_data) if cf_data else pd.DataFrame()
            if not cf_df.empty:
                cf_df = cf_df.rename(columns={"symbol": "ticker"})

            # 合并三表（以 IS 为基础，按 ticker+date 左连接 BS 和 CF）
            merge_keys = ["ticker", "date"]
            merged = is_df.copy()
            if not bs_df.empty:
                bs_cols = merge_keys + [c for c in bs_df.columns if c not in merged.columns]
                merged = merged.merge(bs_df[bs_cols], on=merge_keys, how="left")
            if not cf_df.empty:
                cf_cols = merge_keys + [c for c in cf_df.columns if c not in merged.columns]
                merged = merged.merge(cf_df[cf_cols], on=merge_keys, how="left")

            # 构造 period_label
            if "fiscalYear" in merged.columns and "period" in merged.columns:
                merged["period_label"] = merged["fiscalYear"].astype(str) + "-" + merged["period"].astype(str)
            elif "date" in merged.columns:
                merged["period_label"] = merged["date"].apply(
                    lambda d: f"{str(d)[:4]}-Q{(int(str(d)[5:7])-1)//3+1}" if pd.notna(d) else None
                )

            # 映射到 us_financial_data 表字段
            result = pd.DataFrame({
                "ticker": merged["ticker"],
                "period": merged.get("period_label"),
                "date": merged["date"],
                "filing_date": merged.get("filingDate", merged.get("acceptedDate")),
                "revenue": merged.get("revenue"),
                "cost_of_revenue": merged.get("costOfRevenue"),
                "gross_profit": merged.get("grossProfit"),
                "operating_income": merged.get("operatingIncome"),
                "net_income": merged.get("netIncome"),
                "eps": merged.get("eps"),
                "eps_diluted": merged.get("epsDiluted"),
                "ebitda": merged.get("ebitda"),
                "gross_margin": merged.get("grossProfitRatio"),
                "operating_margin": merged.get("operatingIncomeRatio"),
                "net_margin": merged.get("netIncomeRatio"),
                "rd_expenses": merged.get("researchAndDevelopmentExpenses"),
                "sga_expenses": merged.get("sellingGeneralAndAdministrativeExpenses"),
                "weighted_avg_shares": merged.get("weightedAverageShsOutDil",
                                                   merged.get("weightedAverageShsOut")),
                # BS 字段
                "total_assets": merged.get("totalAssets"),
                "total_equity": merged.get("totalStockholdersEquity",
                                           merged.get("totalEquity")),
                "total_debt": merged.get("totalDebt"),
                "total_current_assets": merged.get("totalCurrentAssets"),
                "total_current_liabilities": merged.get("totalCurrentLiabilities"),
                "cash_and_equivalents": merged.get("cashAndCashEquivalents"),
                "net_receivables": merged.get("netReceivables"),
                "inventory": merged.get("inventory"),
                "long_term_debt": merged.get("longTermDebt"),
                "retained_earnings": merged.get("retainedEarnings"),
                # CF 字段
                "operating_cash_flow": merged.get("operatingCashFlow",
                                                   merged.get("netCashProvidedByOperatingActivities")),
                "capital_expenditure": merged.get("capitalExpenditure"),
                "free_cash_flow": merged.get("freeCashFlow"),
                "dividends_paid": merged.get("commonDividendsPaid",
                                              merged.get("netDividendsPaid")),
                "share_repurchased": merged.get("commonStockRepurchased"),
            })
            result = result.dropna(subset=["ticker", "date"])

            # FMP stable API 不再返回比率字段，需自行计算
            for col in ["revenue", "gross_profit", "net_income", "total_equity",
                         "operating_income"]:
                if col in result.columns:
                    result[col] = pd.to_numeric(result[col], errors="coerce")
            if result["gross_margin"].isna().all() and "gross_profit" in result.columns:
                result["gross_margin"] = result["gross_profit"] / result["revenue"].replace(0, pd.NA)
            if result["operating_margin"].isna().all() and "operating_income" in result.columns:
                result["operating_margin"] = result["operating_income"] / result["revenue"].replace(0, pd.NA)
            if result["net_margin"].isna().all() and "net_income" in result.columns:
                result["net_margin"] = result["net_income"] / result["revenue"].replace(0, pd.NA)
            if "roe" not in result.columns or result.get("roe") is None or result["roe"].isna().all():
                result["roe"] = result["net_income"] / result["total_equity"].replace(0, pd.NA)

            if not result.empty:
                self.db.upsert_us_financial_data(result)
                return len(result)

            logger.debug(f"financial_quarterly: {ticker} 合并后无有效数据")
            return 0

        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = {pool.submit(_fetch_single, t): t for t in tickers}
            for future in tqdm(as_completed(futures), total=len(futures),
                               desc="FMP Quarterly Financials"):
                try:
                    total += future.result()
                except Exception as e:
                    logger.warning(f"Financial 失败 {futures[future]}: {e}")

        logger.info(f"FMP quarterly financials 总计: {total} 条")
        return total

    # camelCase → snake_case 映射（FMP key-metrics + ratios 端点共用）
    _KEY_METRIC_COL_MAP = {
        "symbol": "ticker", "period": "period",
        "calendarYear": "calendar_year",
        "marketCap": "market_cap", "enterpriseValue": "enterprise_value",
        "peRatio": "pe_ratio", "pbRatio": "pb_ratio",
        "priceToSalesRatio": "ps_ratio",
        "priceToFreeCashFlowsRatio": "price_to_fcf",
        "priceEarningsToGrowthRatio": "peg_ratio",
        "evToEBITDA": "ev_to_ebitda", "evToSales": "ev_to_sales",
        "evToFreeCashFlow": "ev_to_fcf",
        "evToOperatingCashFlow": "ev_to_ocf",
        "enterpriseValueOverEBITDA": "ev_to_ebitda",
        "returnOnEquity": "roe", "returnOnAssets": "roa",
        "returnOnInvestedCapital": "roic", "returnOnCapitalEmployed": "roce",
        "grossProfitMargin": "gross_profit_margin",
        "operatingProfitMargin": "operating_profit_margin",
        "netProfitMargin": "net_profit_margin",
        "pretaxProfitMargin": "pretax_profit_margin",
        "effectiveTaxRate": "effective_tax_rate",
        "currentRatio": "current_ratio", "quickRatio": "quick_ratio",
        "cashRatio": "cash_ratio",
        "debtToEquityRatio": "debt_to_equity",
        "debtToEquity": "debt_to_equity",
        "debtToAssets": "debt_to_assets",
        "debtToAssetsRatio": "debt_to_assets",
        "totalDebtToCapitalization": "debt_to_capitalization",
        "longTermDebtToCapitalization": "lt_debt_to_capitalization",
        "interestCoverage": "interest_coverage",
        "netDebtToEBITDA": "net_debt_to_ebitda",
        "interestDebtPerShare": "interest_debt_per_share",
        "debtRatio": "debt_ratio",
        "freeCashFlowPerShare": "free_cash_flow_per_share",
        "freeCashFlowYield": "fcf_yield",
        "earningsYield": "earnings_yield",
        "dividendYield": "dividend_yield",
        "dividendPerShare": "dividend_per_share",
        "payoutRatio": "payout_ratio",
        "dividendPayoutRatio": "payout_ratio",
        "bookValuePerShare": "book_value_per_share",
        "cashPerShare": "cash_per_share",
        "tangibleBookValuePerShare": "tangible_book_value_per_share",
        "revenuePerShare": "revenue_per_share",
        "netIncomePerShare": "net_income_per_share",
        "operatingCashFlowPerShare": "operating_cash_flow_per_share",
        "shareholdersEquityPerShare": "shareholders_equity_per_share",
        "inventoryTurnover": "inventory_turnover",
        "receivablesTurnover": "receivables_turnover",
        "payablesTurnover": "payables_turnover",
        "fixedAssetTurnover": "fixed_asset_turnover",
        "assetTurnover": "asset_turnover",
        "operatingCycle": "operating_cycle",
        "cashConversionCycle": "cash_conversion_cycle",
        "daysOfSalesOutstanding": "days_sales_outstanding",
        "daysOfInventoryOutstanding": "days_inventory_outstanding",
        "daysOfPayablesOutstanding": "days_payables_outstanding",
        "capexToRevenue": "capex_to_revenue",
        "capexToDepreciation": "capex_to_depreciation",
        "capexToOperatingCashFlow": "capex_to_ocf",
        "capexPerShare": "capex_per_share",
        "stockBasedCompensationToRevenue": "sbc_to_revenue",
        "incomeQuality": "income_quality",
        "grahamNumber": "graham_number",
        "grahamNetNet": "graham_net_net",
        "workingCapital": "working_capital",
        "tangibleAssetValue": "tangible_asset_value",
        "netCurrentAssetValue": "net_current_asset_value",
        "investedCapital": "invested_capital",
        "averageReceivables": "average_receivables",
        "averagePayables": "average_payables",
        "averageInventory": "average_inventory",
        "researchAndDevelopementToRevenue": "rd_to_revenue",
        "researchAndDdevelopementToRevenue": "rd_to_revenue",
        "intangiblesToTotalAssets": "intangibles_to_total_assets",
        "salesGeneralAndAdministrativeToRevenue": "sga_to_revenue",
        # ratios 端点特有
        "priceToEarningsRatio": "pe_ratio",
        "priceToBookRatio": "pb_ratio",
        "priceToSaleRatio": "ps_ratio",
        "priceToOperatingCashFlowsRatio": "price_to_ocf",
        "shortTermCoverageRatios": "short_term_coverage",
        "operatingCashFlowSalesRatio": "ocf_to_sales",
        "freeCashFlowOperatingCashFlowRatio": "fcf_to_ocf",
        "cashFlowCoverageRatios": "cash_flow_coverage",
        "cashFlowToDebtRatio": "cash_flow_to_debt",
        "companyEquityMultiplier": "equity_multiplier",
        "ebtPerEbit": "ebt_per_ebit",
        "priceToEarningsToGrowthRatio": "peg_ratio",
        "longTermDebtToTotalAsset": "lt_debt_to_total_asset",
    }

    def _map_key_metric_df(self, df: pd.DataFrame) -> pd.DataFrame:
        """统一映射 FMP key-metrics / ratios 的 camelCase → snake_case。"""
        df = df.rename(columns={k: v for k, v in self._KEY_METRIC_COL_MAP.items() if k in df.columns})
        df = df[df["ticker"].notna()].copy()
        # 获取 us_key_metric 表的实际列，只保留 DB 有的列
        try:
            table_cols_df = self.db.query("SHOW COLUMNS FROM us_key_metric")
            valid_cols = set(table_cols_df["Field"].tolist()) - {"id", "updated_at"}
        except Exception:
            valid_cols = None
        if valid_cols:
            keep = [c for c in df.columns if c in valid_cols]
            df = df[keep]
        return df

    def download_fmp_key_metrics(self, tickers: list[str] = None, limit: int = 400) -> int:
        """FMP per-ticker: /stable/key-metrics (季度) → us_key_metric.

        替代已废弃的 key-metrics-bulk 端点。全量导入 API 返回字段。
        """
        if tickers is None:
            tickers = self.db.get_us_tickers()
        if not tickers:
            logger.warning("download_fmp_key_metrics: 无 ticker")
            return 0

        total = 0

        def _fetch_single(ticker):
            data = self._fmp_get_stable_json(
                "key-metrics",
                params={"symbol": ticker, "period": "quarter", "limit": limit},
            )
            if not data:
                logger.debug(f"key_metrics: {ticker} 无数据")
                return 0
            df = pd.DataFrame(data)
            df = self._map_key_metric_df(df)
            if df.empty:
                logger.debug(f"key_metrics: {ticker} 映射后无有效数据")
                return 0
            self.db.upsert_us_key_metric(df)
            return len(df)

        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = {pool.submit(_fetch_single, t): t for t in tickers}
            for future in tqdm(as_completed(futures), total=len(futures),
                               desc="FMP Key Metrics"):
                try:
                    total += future.result()
                except Exception as e:
                    logger.warning(f"Key metrics 失败 {futures[future]}: {e}")

        logger.info(f"FMP key metrics 总计: {total} 条")
        return total

    def download_fmp_ratios(self, tickers: list[str] = None, limit: int = 400) -> int:
        """FMP per-ticker: /stable/ratios (季度) → us_key_metric.

        替代已废弃的 ratios-bulk 端点。与 key-metrics 共享同一张表。
        """
        if tickers is None:
            tickers = self.db.get_us_tickers()
        if not tickers:
            logger.warning("download_fmp_ratios: 无 ticker")
            return 0

        total = 0

        def _fetch_single(ticker):
            data = self._fmp_get_stable_json(
                "ratios",
                params={"symbol": ticker, "period": "quarter", "limit": limit},
            )
            if not data:
                logger.debug(f"ratios: {ticker} 无数据")
                return 0
            df = pd.DataFrame(data)
            df = self._map_key_metric_df(df)
            if df.empty:
                logger.debug(f"ratios: {ticker} 映射后无有效数据")
                return 0
            self.db.upsert_us_key_metric(df)
            return len(df)

        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = {pool.submit(_fetch_single, t): t for t in tickers}
            for future in tqdm(as_completed(futures), total=len(futures),
                               desc="FMP Ratios"):
                try:
                    total += future.result()
                except Exception as e:
                    logger.warning(f"Ratios 失败 {futures[future]}: {e}")

        logger.info(f"FMP ratios 总计: {total} 条")
        return total

    def download_fmp_eod_bulk(self, start_year: int = 2015, end_year: int = None) -> int:
        """FMP bulk: EOD prices by date. 如果 bulk 端点不可用，自动回退 per-ticker。"""
        if end_year is None:
            end_year = datetime.now().year

        # Test if bulk EOD endpoint works
        test_df = self._fmp_get_bulk_csv("historical-price-eod/bulk", {"date": "2025-01-15"})
        if test_df.empty:
            logger.info("FMP EOD bulk 端点不可用，使用 per-ticker 下载")
            return self._download_fmp_prices_per_ticker(start_year, end_year)

        # Bulk works — download by date (generate trading dates)
        total = 0
        start_date = datetime(start_year, 1, 1)
        end_date = datetime(end_year, 12, 31)
        # For bulk EOD we'd need to iterate every trading day - too many requests
        # Prefer per-ticker for price data
        logger.info("FMP EOD bulk 按日下载效率低，改用 per-ticker")
        return self._download_fmp_prices_per_ticker(start_year, end_year)

    def _download_fmp_prices_per_ticker(self, start_year: int, end_year: int) -> int:
        """FMP per-ticker: historical daily prices (多线程).

        使用 /stable/historical-price-eod/full 端点（新版）。
        该端点每次最多返回 5000 行，所以按 10 年分段请求。
        """
        tickers = self.db.get_us_tickers()
        if not tickers:
            logger.warning("无 ticker 可下载行情，请先下载股票列表")
            return 0

        # 按 10 年分段，确保每段不超过 5000 行
        segments = []
        for yr in range(start_year, end_year + 1, 10):
            seg_end = min(yr + 9, end_year)
            segments.append((f"{yr}-01-01", f"{seg_end}-12-31"))

        total = 0

        def _fetch_price_single(ticker):
            count = 0
            for seg_start, seg_end in segments:
                data = self._fmp_get_stable_json(
                    "historical-price-eod/full",
                    params={"symbol": ticker, "from": seg_start, "to": seg_end},
                )
                if not data:
                    logger.debug(f"_fetch_price_single: {ticker} 段 {seg_start}~{seg_end} 无数据，跳过")
                    continue
                rows = [{
                    "ticker": item.get("symbol", ticker),
                    "trade_date": item["date"],
                    "open": item.get("open"), "high": item.get("high"),
                    "low": item.get("low"), "close": item.get("close"),
                    "adj_close": item.get("adjClose"),
                    "volume": item.get("volume"),
                    "change_pct": item.get("changePercent"),
                    "vwap": item.get("vwap"),
                    "unadjusted_volume": item.get("unadjustedVolume"),
                } for item in data if item.get("date")]
                if rows:
                    df = pd.DataFrame(rows)
                    self.db.bulk_upsert_us_daily_price(df)
                    count += len(rows)
            return count

        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = {pool.submit(_fetch_price_single, t): t for t in tickers}
            for future in tqdm(as_completed(futures), total=len(futures), desc="FMP Daily Prices"):
                try:
                    total += future.result()
                except Exception as e:
                    logger.warning(f"Price 失败 {futures[future]}: {e}")

        logger.info(f"FMP daily prices 总计: {total} 条")
        return total

    def download_fmp_profiles(self, tickers: list[str] = None) -> int:
        """FMP per-ticker: company profiles (GICS sector/industry, 多线程)."""
        if tickers is None:
            tickers = self.db.get_us_tickers()
        if not tickers:
            logger.debug(f"download_fmp_profiles: 空返回 (not tickers)")

            return 0

        total = 0
        batch_size = 50
        batches = [tickers[i:i + batch_size] for i in range(0, len(tickers), batch_size)]

        def _fetch_profile_batch(batch):
            symbols = ",".join(batch)
            data = self._fmp_get_json(f"profile/{symbols}")
            if not data:
                logger.debug(f"_fetch_profile_batch: 空返回 (not data)")

                return 0
            records = [{"ticker": item.get("symbol"), "sector": item.get("sector"),
                        "industry": item.get("industry")} for item in data]
            if records:
                df = pd.DataFrame(records).dropna(subset=["ticker"])
                self.db.upsert_us_industry_class(df)
                return len(df)
            logger.debug(f"_fetch_profile_batch: 空返回 (return len(df))")

            return 0

        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = {pool.submit(_fetch_profile_batch, b): b for b in batches}
            for future in tqdm(as_completed(futures), total=len(futures), desc="FMP Profiles"):
                try:
                    total += future.result()
                except Exception as e:
                    logger.warning(f"Profile batch 失败: {e}")

        logger.info(f"FMP profiles 总计: {total} 条")
        return total

    def download_fmp_insider_trading(self, tickers: list[str] = None) -> int:
        """FMP per-ticker: insider trading (Form 4, 多线程)."""
        if tickers is None:
            tickers = self.db.get_us_tickers()
        if not tickers:
            logger.debug(f"download_fmp_insider_trading: 空返回 (not tickers)")

            return 0

        total = 0

        def _fetch_insider_single(ticker):
            count = 0
            page = 0
            while True:
                data = self._fmp_get_json(
                    "insider-trading",
                    params={"symbol": ticker, "page": page, "limit": 100},
                    version="v4",
                )
                if not data:
                    logger.debug(f"_fetch_insider_single: 结束循环 (not data)")

                    break
                records = [{
                    "ticker": ticker,
                    "filing_date": item.get("filingDate"),
                    "transaction_date": item.get("transactionDate"),
                    "reporting_name": item.get("reportingName"),
                    "type_of_owner": item.get("typeOfOwner"),
                    "transaction_type": item.get("transactionType"),
                    "acquisition_or_disposition": item.get("acquistionOrDisposition"),
                    "securities_transacted": item.get("securitiesTransacted"),
                    "price": item.get("price"),
                    "securities_owned": item.get("securitiesOwned"),
                    "security_name": item.get("securityName"),
                    "form_type": item.get("formType"),
                    "link": item.get("link"),
                } for item in data]
                if records:
                    df = pd.DataFrame(records)
                    self.db.upsert_us_insider_trade(df)
                    count += len(records)
                if len(data) < 100:
                    logger.debug(f"_fetch_insider_single: 结束循环 (len(data) < 100)")

                    break
                page += 1
            return count

        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = {pool.submit(_fetch_insider_single, t): t for t in tickers}
            for future in tqdm(as_completed(futures), total=len(futures), desc="FMP Insider Trading"):
                try:
                    total += future.result()
                except Exception as e:
                    logger.warning(f"Insider 失败 {futures[future]}: {e}")

        logger.info(f"FMP insider trading 总计: {total} 条")
        return total

    def download_fmp_analyst_grades(self, tickers: list[str] = None) -> int:
        """FMP per-ticker: 分析师评级变更 (v3/grade) → us_analyst_recommendation.

        替代旧 yfinance upgrades_downgrades，字段更完整（gradingCompany + newGrade + previousGrade）。
        """
        if tickers is None:
            tickers = self.db.get_us_tickers()
        if not tickers:
            logger.warning("download_fmp_analyst_grades: 无 ticker")
            return 0

        total = 0

        def _fetch_grade_single(ticker):
            data = self._fmp_get_json(f"grade/{ticker}")
            if not data:
                logger.debug(f"analyst_grades: {ticker} 无评级数据")
                return 0
            records = [{
                "ticker": item.get("symbol", ticker),
                "date": item.get("date", "")[:10],
                "analyst_company": item.get("gradingCompany", ""),
                "analyst_name": "",
                "rating": item.get("newGrade", ""),
                "price_target": None,
            } for item in data if item.get("date") and item.get("newGrade")]
            if not records:
                logger.debug(f"analyst_grades: {ticker} 过滤后无有效记录")
                return 0
            df = pd.DataFrame(records)
            df = df.drop_duplicates(subset=["ticker", "date", "analyst_company"], keep="last")
            self.db.upsert_us_analyst_recommendation(df)
            return len(df)

        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = {pool.submit(_fetch_grade_single, t): t for t in tickers}
            for future in tqdm(as_completed(futures), total=len(futures),
                               desc="FMP Analyst Grades"):
                try:
                    total += future.result()
                except Exception as e:
                    logger.warning(f"Grade 失败 {futures[future]}: {e}")

        logger.info(f"FMP analyst grades 总计: {total} 条")
        return total

    def download_fmp_dividends_splits(self, tickers: list[str] = None) -> int:
        """FMP per-ticker: dividends + splits (多线程)."""
        if tickers is None:
            tickers = self.db.get_us_tickers()
        if not tickers:
            logger.debug(f"download_fmp_dividends_splits: 空返回 (not tickers)")

            return 0

        total = 0

        def _fetch_div_split_single(ticker):
            count = 0
            # Dividends
            # NOTE: legacy 端点，FMP 已废弃。当前 plan 无可用替代端点。
            # us_corporate_action 表已有 61 万行历史数据，暂不影响使用。
            div_data = self._fmp_get_json(f"historical-price-full/stock_dividend/{ticker}")
            if isinstance(div_data, dict) and "historical" in div_data:
                records = [{
                    "ticker": ticker, "date": h.get("date"),
                    "action_type": "dividend", "label": h.get("label"),
                    "value": h.get("adjDividend") or h.get("dividend"),
                } for h in div_data["historical"]]
                if records:
                    self.db.upsert_us_corporate_action(pd.DataFrame(records))
                    count += len(records)
            # Splits
            split_data = self._fmp_get_json(f"historical-price-full/stock_split/{ticker}")
            if isinstance(split_data, dict) and "historical" in split_data:
                records = [{
                    "ticker": ticker, "date": h.get("date"),
                    "action_type": "split", "label": h.get("label"),
                    "value": h.get("numerator", 0) / max(h.get("denominator", 1), 1),
                } for h in split_data["historical"]]
                if records:
                    self.db.upsert_us_corporate_action(pd.DataFrame(records))
                    count += len(records)
            return count

        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = {pool.submit(_fetch_div_split_single, t): t for t in tickers}
            for future in tqdm(as_completed(futures), total=len(futures), desc="FMP Dividends & Splits"):
                try:
                    total += future.result()
                except Exception as e:
                    logger.warning(f"Div/Split 失败 {futures[future]}: {e}")

        logger.info(f"FMP dividends & splits 总计: {total} 条")
        return total

    def download_fmp_index_daily(self, start_year: int = 1995) -> int:
        """FMP: index daily prices (S&P 500, NASDAQ, Dow, Russell 1000).

        使用 /stable/historical-price-eod/full 端点，按 10 年分段。
        """
        from services.config import US_INDEX_SYMBOLS
        end_year = datetime.now().year
        segments = []
        for yr in range(start_year, end_year + 1, 10):
            seg_end = min(yr + 9, end_year)
            segments.append((f"{yr}-01-01", f"{seg_end}-12-31"))

        total = 0
        for symbol in tqdm(US_INDEX_SYMBOLS, desc="FMP Index Daily"):
            for seg_start, seg_end in segments:
                data = self._fmp_get_stable_json(
                    "historical-price-eod/full",
                    params={"symbol": symbol, "from": seg_start, "to": seg_end},
                )
                if not data:
                    logger.debug(f"download_fmp_index_daily: {symbol} 段 {seg_start}~{seg_end} 无数据，跳过")
                    continue
                rows = [{
                    "index_code": item.get("symbol", symbol),
                    "trade_date": item["date"],
                    "open": item.get("open"),
                    "high": item.get("high"),
                    "low": item.get("low"),
                    "close": item.get("close"),
                    "volume": item.get("volume"),
                } for item in data if item.get("date")]
                if rows:
                    df = pd.DataFrame(rows)
                    self.db.bulk_upsert_us_index_daily(df)
                    total += len(rows)

        logger.info(f"FMP index daily 总计: {total} 条")
        return total

    # yfinance → FMP commodity symbol mapping
    _COMMODITY_MAP = {
        "GC=F": "GCUSD", "SI=F": "SIUSD", "CL=F": "CLUSD",
        "BZ=F": "BZUSD", "NG=F": "NGUSD", "HG=F": "HGUSD",
        "ZC=F": "ZCUSX", "ZS=F": "ZSUSX", "ZW=F": "WEAT",
    }

    def download_fmp_commodity_prices(self, start_year: int = 1995) -> int:
        """FMP: commodity futures prices.

        使用 /stable/historical-price-eod/full 端点，按 10 年分段。
        """
        from services.config import US_COMMODITY_SYMBOLS
        end_year = datetime.now().year
        segments = []
        for yr in range(start_year, end_year + 1, 10):
            seg_end = min(yr + 9, end_year)
            segments.append((f"{yr}-01-01", f"{seg_end}-12-31"))

        total = 0
        for yf_sym in tqdm(US_COMMODITY_SYMBOLS, desc="FMP Commodities"):
            fmp_sym = self._COMMODITY_MAP.get(yf_sym, yf_sym.replace("=F", "USD"))
            for seg_start, seg_end in segments:
                data = self._fmp_get_stable_json(
                    "historical-price-eod/full",
                    params={"symbol": fmp_sym, "from": seg_start, "to": seg_end},
                )
                if not data:
                    logger.debug(f"download_fmp_commodity_prices: {fmp_sym} 段 {seg_start}~{seg_end} 无数据，跳过")
                    continue
                rows = [{
                    "symbol": yf_sym,  # 保持 yfinance 符号兼容
                    "trade_date": item["date"],
                    "open": item.get("open"),
                    "high": item.get("high"),
                    "low": item.get("low"),
                    "close": item.get("close"),
                    "volume": item.get("volume"),
                } for item in data if item.get("date")]
                if rows:
                    df = pd.DataFrame(rows)
                    self.db.bulk_upsert_us_commodity_price(df)
                    total += len(rows)

        logger.info(f"FMP commodity prices 总计: {total} 条")
        return total

    # FMP economic indicator → FRED series mapping
    _MACRO_MAP = {
        "US_GDP": ("economic", {"name": "GDP"}),
        "US_CPI_YOY": ("economic", {"name": "CPI"}),
        "US_UNEMP": ("economic", {"name": "unemploymentRate"}),
        "US_FED_RATE": ("economic", {"name": "federalFundsRate"}),
        "US_NONFARM": ("economic", {"name": "nonFarmPayroll"}),
        "US_RETAIL": ("economic", {"name": "retailSales"}),
        "US_IND_PROD": ("economic", {"name": "industrialProductionTotalIndex"}),
        "US_HOUSING": ("economic", {"name": "housingStarts"}),
        "US_INIT_CLAIMS": ("economic", {"name": "initialClaims"}),
    }

    def download_fmp_macro(self) -> int:
        """FMP v4: economic indicators (替代 FRED 部分指标)."""
        total = 0

        # 1. Economic indicators
        for indicator_code, (endpoint, params) in self._MACRO_MAP.items():
            data = self._fmp_get_json(endpoint, params=params, version="v4")
            if not data:
                logger.debug(f"download_fmp_macro: 跳过 (not data)")

                continue
            rows = []
            for item in data:
                rows.append({
                    "indicator_code": indicator_code,
                    "report_date": item.get("date"),
                    "value": item.get("value"),
                })
            if rows:
                df = pd.DataFrame(rows)
                self.db.upsert_us_macro_indicator(df)
                total += len(rows)
            logger.info(f"FMP macro {indicator_code}: {len(rows)} 条")

        # 2. Treasury yields (contains 1m to 30y)
        treasury_data = self._fmp_get_json(
            "treasury", params={"from": "1995-01-01"}, version="v4",
        )
        if treasury_data:
            # Convert to individual indicators
            for item in treasury_data:
                date = item.get("date")
                for col, code in [("year10", "US_10Y"), ("year2", "US_2Y")]:
                    val = item.get(col)
                    if val is not None:
                        rows = [{"indicator_code": code, "report_date": date, "value": val}]
                        df = pd.DataFrame(rows)
                        self.db.upsert_us_macro_indicator(df)
                        total += 1
                # Compute 10Y-2Y spread
                y10 = item.get("year10")
                y2 = item.get("year2")
                if y10 is not None and y2 is not None:
                    rows = [{"indicator_code": "US_2Y10Y", "report_date": date, "value": y10 - y2}]
                    df = pd.DataFrame(rows)
                    self.db.upsert_us_macro_indicator(df)
                    total += 1
            logger.info(f"FMP treasury yields: {len(treasury_data)} 日")

        logger.info(f"FMP macro 总计: {total} 条")
        return total

    # ==============================================================
    # Unusual Whales
    # ==============================================================

    def _uw_get(self, path: str, params: dict = None) -> list | dict:
        """Unusual Whales API call."""
        if not UW_API_KEY:
            logger.warning("UW_API_KEY 未设置")
            return []
        self._uw_limiter.wait()
        url = f"https://api.unusualwhales.com{path}"
        headers = {"Authorization": f"Bearer {UW_API_KEY}", "Accept": "application/json"}
        resp = _request_with_retry("GET", url, headers=headers, params=params)
        if resp.status_code != 200:
            logger.warning(f"UW {path}: HTTP {resp.status_code}")
            return []
        data = resp.json()
        if isinstance(data, dict) and "data" in data:
            return data["data"]
        return data if isinstance(data, list) else []

    def download_uw_options_flow(self, limit: int = 5000) -> int:
        """Unusual Whales: options flow alerts."""
        data = self._uw_get("/api/option-trades/flow-alerts", params={"limit": limit})
        if not data:
            logger.debug(f"download_uw_options_flow: 空返回 (not data)")

            return 0

        records = []
        for item in data:
            records.append({
                "ticker": item.get("ticker") or item.get("underlying_symbol"),
                "alert_id": str(item.get("id", "")),
                "date": item.get("created_at") or item.get("date"),
                "contract_type": item.get("put_call") or item.get("option_type"),
                "strike": item.get("strike"),
                "expiry": item.get("expires_at") or item.get("expiry"),
                "premium": item.get("total_premium") or item.get("premium"),
                "volume": item.get("volume"),
                "open_interest": item.get("open_interest"),
                "sentiment": item.get("sentiment"),
            })

        if records:
            df = pd.DataFrame(records).dropna(subset=["ticker"])
            self.db.upsert_us_options_flow(df)

        logger.info(f"UW options flow: {len(records)} 条")
        return len(records)

    def download_uw_dark_pool(self) -> int:
        """Unusual Whales: dark pool trades."""
        data = self._uw_get("/api/darkpool/recent")
        if not data:
            logger.warning("download_uw_dark_pool: API 返回空数据")
            return 0

        records = []
        for item in data:
            records.append({
                "ticker": item.get("ticker") or item.get("symbol"),
                "date": item.get("executed_at") or item.get("tracking_timestamp") or item.get("date"),
                "price": item.get("price"),
                "size": item.get("size") or item.get("volume"),
                "notional": item.get("premium") or item.get("notional_value") or item.get("notional"),
            })

        if records:
            df = pd.DataFrame(records).dropna(subset=["ticker"])
            self.db.upsert_us_dark_pool(df)

        logger.info(f"UW dark pool: {len(records)} 条")
        return len(records)

    def download_uw_congress_trades(self) -> int:
        """Unusual Whales: congressional trades."""
        data = self._uw_get("/api/congress/recent-trades")
        if not data:
            logger.warning("download_uw_congress_trades: API 返回空数据")
            return 0

        records = []
        for item in data:
            first = item.get("firstName") or item.get("first_name", "")
            last = item.get("lastName") or item.get("last_name", "")
            records.append({
                "ticker": item.get("ticker") or item.get("symbol"),
                "politician": f"{first} {last}".strip(),
                "office": item.get("office") or item.get("chamber"),
                "transaction_date": item.get("transactionDate") or item.get("transaction_date"),
                "disclosure_date": item.get("dateRecieved") or item.get("disclosure_date"),
                "trade_type": item.get("type") or item.get("transaction_type"),
                "amount": item.get("amount"),
                "asset_description": item.get("assetDescription") or item.get("asset_description"),
            })

        if records:
            df = pd.DataFrame(records).dropna(subset=["politician"])
            self.db.upsert_us_congress_trade(df)

        logger.info(f"UW congress trades: {len(records)} 条")
        return len(records)

    def download_uw_news(self) -> int:
        """Unusual Whales: news headlines."""
        data = self._uw_get("/api/news/headlines")
        if not data:
            logger.warning("download_uw_news: API 返回空数据")
            return 0

        records = []
        for item in data:
            tickers_list = item.get("tickers") or item.get("symbols") or []
            if isinstance(tickers_list, list):
                tickers_str = ",".join(str(t) for t in tickers_list)
            else:
                tickers_str = str(tickers_list)
            records.append({
                "source": "uw",
                "title": item.get("title") or item.get("headline"),
                "url": item.get("url") or item.get("link"),
                "published_at": item.get("published_at") or item.get("date"),
                "tickers": tickers_str,
                "summary": item.get("summary") or item.get("description"),
                "sentiment": item.get("sentiment"),
            })

        if records:
            df = pd.DataFrame(records).dropna(subset=["title"])
            self.db.upsert_us_news(df)

        logger.info(f"UW news: {len(records)} 条")
        return len(records)

    def download_uw_all(self) -> dict:
        """Unusual Whales: 全量下载。"""
        results = {}
        results["options_flow"] = self.download_uw_options_flow()
        results["dark_pool"] = self.download_uw_dark_pool()
        results["congress"] = self.download_uw_congress_trades()
        results["news"] = self.download_uw_news()
        return results

    # ==============================================================
    # Fiscal.ai
    # ==============================================================

    # Fiscal.ai 日频估值比率 ID 映射
    _FISCAL_RATIO_IDS = [
        "ratio_price_to_earnings",      # PE
        "ratio_price_to_book",          # PB
        "ratio_price_to_sales",         # PS
        "ratio_ev_to_ebitda",           # EV/EBITDA
        "calculated_dividend_yield",    # 股息率
        "calculated_market_cap",        # 市值
        "calculated_tev",               # 企业价值
    ]

    def _fiscal_get(self, path: str, params: dict = None) -> list | dict:
        """Fiscal.ai API call — apiKey 作为 query parameter。"""
        if not FISCAL_API_KEY:
            logger.warning("FISCAL_API_KEY 未设置")
            return []
        self._fiscal_limiter.wait()
        url = f"https://api.fiscal.ai/v1{path}"
        p = {"apiKey": FISCAL_API_KEY}
        if params:
            p.update(params)
        resp = _request_with_retry("GET", url, params=p,
                                    headers={"Content-Type": "application/json"})
        if resp.status_code != 200:
            logger.warning(f"Fiscal {path}: HTTP {resp.status_code}")
            return []
        data = resp.json()
        if isinstance(data, dict) and "errors" in data:
            logger.warning(f"Fiscal error: {data['errors']}")
            return []
        return data

    def download_fiscal_daily_ratios(self, tickers: list[str] = None) -> int:
        """Fiscal.ai: daily valuation ratios (PE/PB/PS/EV 等)."""
        if tickers is None:
            tickers = self.db.get_us_tickers()
        if not tickers:
            logger.debug(f"download_fiscal_daily_ratios: 空返回 (not tickers)")

            return 0

        ratio_ids = ",".join(self._FISCAL_RATIO_IDS)
        total = 0

        for ticker in tqdm(tickers, desc="Fiscal Daily Ratios"):
            # Fiscal.ai companyKey 格式: EXCHANGE_TICKER (e.g. NASDAQ_MSFT)
            # 需要查 exchange，先试 NASDAQ，再试 NYSE
            data = None
            for exchange in ["NASDAQ", "NYSE", "AMEX"]:
                company_key = f"{exchange}_{ticker}"
                result = self._fiscal_get(
                    "/company/ratios",
                    params={"companyKey": company_key, "ratioId": ratio_ids},
                )
                if result and isinstance(result, dict) and "data" in result:
                    data = result
                    logger.debug(f"download_fiscal_daily_ratios: 结束循环 (data = result)")

                    break
                elif result and isinstance(result, list) and len(result) > 0:
                    data = {"data": result}
                    logger.debug(f"download_fiscal_daily_ratios: 结束循环 (data = {'data': result})")

                    break

            if not data or "data" not in data:
                logger.debug(f"download_fiscal_daily_ratios: 跳过 (not data or 'data' not in data)")

                continue

            for period in data["data"]:
                values = period.get("metricValues", {})
                if not values:
                    logger.debug(f"download_fiscal_daily_ratios: 跳过 (not values)")

                    continue
                records = [{
                    "ticker": ticker,
                    "date": period.get("reportDate"),
                    "pe_ratio": values.get("ratio_price_to_earnings"),
                    "pb_ratio": values.get("ratio_price_to_book"),
                    "ps_ratio": values.get("ratio_price_to_sales"),
                    "ev_to_ebitda": values.get("ratio_ev_to_ebitda"),
                    "dividend_yield": values.get("calculated_dividend_yield"),
                    "market_cap": values.get("calculated_market_cap"),
                    "enterprise_value": values.get("calculated_tev"),
                }]
                df = pd.DataFrame(records).dropna(subset=["date"])
                if not df.empty:
                    self.db.upsert_us_daily_ratio(df)
                    total += len(df)

        logger.info(f"Fiscal daily ratios 总计: {total} 条")
        return total

    def download_fiscal_all(self) -> dict:
        """Fiscal.ai: 全量下载。"""
        results = {}
        results["daily_ratios"] = self.download_fiscal_daily_ratios()
        return results

    # ==============================================================
    # Quiver Quantitative
    # ==============================================================

    def _quiver_get_json(self, path: str) -> list:
        """Quiver API call。"""
        from services.config import QUIVER_API_KEY, QUIVER_RATE_LIMIT
        if not QUIVER_API_KEY:
            logger.warning("QUIVER_API_KEY 未设置")
            return []
        if not hasattr(self, "_quiver_limiter"):
            self._quiver_limiter = RateLimiter(QUIVER_RATE_LIMIT)
        self._quiver_limiter.wait()
        url = f"https://api.quiverquant.com/beta/{path}"
        headers = {"Authorization": f"Token {QUIVER_API_KEY}", "Accept": "application/json"}
        resp = _request_with_retry("GET", url, headers=headers, timeout=60)
        if resp.status_code != 200:
            logger.warning(f"Quiver {path}: HTTP {resp.status_code}")
            return []
        data = resp.json()
        if not isinstance(data, list):
            logger.warning(f"Quiver {path}: 非列表响应")
            return []
        return data

    def download_quiver_lobbying(self, tickers: list[str] = None) -> int:
        """Quiver per-ticker: 游说活动 → us_lobbying."""
        if tickers is None:
            tickers = self.db.get_us_tickers()
        if not tickers:
            logger.warning("download_quiver_lobbying: 无 ticker")
            return 0

        total = 0

        def _fetch_single(ticker):
            data = self._quiver_get_json(f"historical/lobbying/{ticker}")
            if not data:
                logger.debug(f"quiver_lobbying: {ticker} 无数据")
                return 0
            records = [{
                "ticker": item.get("Ticker", ticker),
                "date": item.get("Date", "")[:10],
                "amount": pd.to_numeric(item.get("Amount"), errors="coerce"),
                "client": item.get("Client", ""),
                "registrant": item.get("Registrant", ""),
                "issue": item.get("Issue", ""),
            } for item in data if item.get("Date")]
            if not records:
                logger.debug(f"quiver_lobbying: {ticker} 过滤后无有效记录")
                return 0
            df = pd.DataFrame(records)
            df = df.dropna(subset=["ticker", "date"])
            if not df.empty:
                self.db.upsert_us_lobbying(df)
                return len(df)
            return 0

        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = {pool.submit(_fetch_single, t): t for t in tickers}
            for future in tqdm(as_completed(futures), total=len(futures),
                               desc="Quiver Lobbying"):
                try:
                    total += future.result()
                except Exception as e:
                    logger.warning(f"Lobbying 失败 {futures[future]}: {e}")

        logger.info(f"Quiver lobbying 总计: {total} 条")
        return total

    def download_quiver_gov_contracts(self, tickers: list[str] = None) -> int:
        """Quiver per-ticker: 政府合同 → us_gov_contract."""
        if tickers is None:
            tickers = self.db.get_us_tickers()
        if not tickers:
            logger.warning("download_quiver_gov_contracts: 无 ticker")
            return 0

        total = 0

        def _fetch_single(ticker):
            data = self._quiver_get_json(f"historical/govcontracts/{ticker}")
            if not data:
                logger.debug(f"quiver_gov_contracts: {ticker} 无数据")
                return 0
            records = [{
                "ticker": item.get("Ticker", ticker),
                "year": item.get("Year"),
                "quarter": item.get("Qtr"),
                "amount": pd.to_numeric(item.get("Amount"), errors="coerce"),
            } for item in data if item.get("Year") and item.get("Qtr")]
            if not records:
                logger.debug(f"quiver_gov_contracts: {ticker} 过滤后无有效记录")
                return 0
            df = pd.DataFrame(records)
            df = df.dropna(subset=["ticker", "year", "quarter"])
            if not df.empty:
                self.db.upsert_us_gov_contract(df)
                return len(df)
            return 0

        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = {pool.submit(_fetch_single, t): t for t in tickers}
            for future in tqdm(as_completed(futures), total=len(futures),
                               desc="Quiver Gov Contracts"):
                try:
                    total += future.result()
                except Exception as e:
                    logger.warning(f"GovContract 失败 {futures[future]}: {e}")

        logger.info(f"Quiver gov contracts 总计: {total} 条")
        return total

    def download_quiver_wsb_sentiment(self, tickers: list[str] = None) -> int:
        """Quiver per-ticker: WallStreetBets 情绪 → us_wsb_sentiment."""
        if tickers is None:
            tickers = self.db.get_us_tickers()
        if not tickers:
            logger.warning("download_quiver_wsb_sentiment: 无 ticker")
            return 0

        total = 0

        def _fetch_single(ticker):
            data = self._quiver_get_json(f"historical/wallstreetbets/{ticker}")
            if not data:
                logger.debug(f"quiver_wsb: {ticker} 无数据")
                return 0
            records = [{
                "ticker": item.get("Ticker", ticker),
                "date": item.get("Date", "")[:10],
                "mentions": item.get("Mentions"),
                "rank": item.get("Rank"),
                "sentiment": item.get("Sentiment"),
            } for item in data if item.get("Date")]
            if not records:
                logger.debug(f"quiver_wsb: {ticker} 过滤后无有效记录")
                return 0
            df = pd.DataFrame(records)
            df = df.dropna(subset=["ticker", "date"])
            if not df.empty:
                self.db.upsert_us_wsb_sentiment(df)
                return len(df)
            return 0

        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = {pool.submit(_fetch_single, t): t for t in tickers}
            for future in tqdm(as_completed(futures), total=len(futures),
                               desc="Quiver WSB Sentiment"):
                try:
                    total += future.result()
                except Exception as e:
                    logger.warning(f"WSB 失败 {futures[future]}: {e}")

        logger.info(f"Quiver WSB sentiment 总计: {total} 条")
        return total

    def download_quiver_all(self) -> dict:
        """Quiver: 全量下载。"""
        results = {}
        results["lobbying"] = self.download_quiver_lobbying()
        results["gov_contracts"] = self.download_quiver_gov_contracts()
        results["wsb_sentiment"] = self.download_quiver_wsb_sentiment()
        return results

    # ==============================================================
    # Alpha Vantage
    # ==============================================================

    def _av_get_json(self, params: dict) -> dict:
        """Alpha Vantage API call."""
        from services.config import ALPHAVANTAGE_API_KEY, ALPHAVANTAGE_RATE_LIMIT
        if not ALPHAVANTAGE_API_KEY:
            logger.warning("ALPHAVANTAGE_API_KEY 未设置")
            return {}
        if not hasattr(self, "_av_limiter"):
            self._av_limiter = RateLimiter(ALPHAVANTAGE_RATE_LIMIT)
        self._av_limiter.wait()
        params["apikey"] = ALPHAVANTAGE_API_KEY
        resp = _request_with_retry("GET", "https://www.alphavantage.co/query", params=params, timeout=60)
        if resp.status_code != 200:
            logger.warning(f"AlphaVantage: HTTP {resp.status_code}")
            return {}
        data = resp.json()
        if "Error Message" in data or "Note" in data:
            logger.warning(f"AlphaVantage error: {data.get('Error Message', data.get('Note', ''))}")
            return {}
        return data

    def download_av_news_sentiment(self, tickers: list[str] = None, time_from: str = None) -> int:
        """Alpha Vantage: 新闻情绪 → us_news_sentiment (按 ticker 聚合到日级别)。

        NEWS_SENTIMENT 端点返回文章级别数据，每篇文章含多个 ticker 的情绪分。
        聚合策略：同一 ticker 同一天的所有文章取加权平均（relevance_score 加权）。
        """
        if tickers is None:
            tickers = self.db.get_us_tickers()
        if not tickers:
            logger.warning("download_av_news_sentiment: 无 ticker")
            return 0

        total = 0
        ticker_set = set(tickers)

        def _fetch_single(ticker):
            params = {
                "function": "NEWS_SENTIMENT",
                "tickers": ticker,
                "limit": 200,
                "sort": "LATEST",
            }
            if time_from:
                params["time_from"] = time_from

            data = self._av_get_json(params)
            if not data or "feed" not in data:
                logger.debug(f"av_news_sentiment: {ticker} 无 feed")
                return 0

            # 解析每篇文章中该 ticker 的情绪分
            records = []
            for article in data["feed"]:
                pub_date = str(article.get("time_published", ""))[:8]
                if len(pub_date) == 8:
                    pub_date = f"{pub_date[:4]}-{pub_date[4:6]}-{pub_date[6:8]}"
                else:
                    continue

                for ts in article.get("ticker_sentiment", []):
                    t = ts.get("ticker", "")
                    if t != ticker:
                        continue
                    try:
                        records.append({
                            "ticker": t,
                            "date": pub_date,
                            "sentiment_score": float(ts.get("ticker_sentiment_score", 0)),
                            "relevance_score": float(ts.get("relevance_score", 0)),
                        })
                    except (ValueError, TypeError):
                        continue

            if not records:
                logger.debug(f"av_news_sentiment: {ticker} 无有效记录")
                return 0

            # 按 ticker+date 聚合（relevance 加权平均）
            df = pd.DataFrame(records)
            df["weighted_sent"] = df["sentiment_score"] * df["relevance_score"]

            agg = df.groupby(["ticker", "date"]).agg(
                weighted_sent_sum=("weighted_sent", "sum"),
                relevance_sum=("relevance_score", "sum"),
                article_count=("sentiment_score", "count"),
            ).reset_index()

            agg["sentiment_score"] = agg["weighted_sent_sum"] / agg["relevance_sum"].clip(lower=0.01)
            agg["relevance_score"] = agg["relevance_sum"] / agg["article_count"]
            result = agg[["ticker", "date", "sentiment_score", "relevance_score", "article_count"]]

            self.db.upsert_us_news_sentiment(result)
            return len(result)

        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = {pool.submit(_fetch_single, t): t for t in tickers}
            for future in tqdm(as_completed(futures), total=len(futures),
                               desc="AV News Sentiment"):
                try:
                    total += future.result()
                except Exception as e:
                    logger.warning(f"News 失败 {futures[future]}: {e}")

        logger.info(f"AV news sentiment 总计: {total} 条")
        return total

    def download_av_options_snapshot(self, tickers: list[str] = None) -> int:
        """Alpha Vantage: 期权快照 → us_options_snapshot (聚合 ATM IV + put/call ratio)。

        HISTORICAL_OPTIONS 端点返回完整期权链，这里聚合为每日快照：
        - ATM call/put IV 均值
        - IV skew = put_iv - call_iv
        - put/call volume ratio + OI ratio
        """
        if tickers is None:
            tickers = self.db.get_us_tickers()
        if not tickers:
            logger.warning("download_av_options_snapshot: 无 ticker")
            return 0

        total = 0

        def _fetch_single(ticker):
            data = self._av_get_json({
                "function": "HISTORICAL_OPTIONS",
                "symbol": ticker,
            })
            if not data or "data" not in data or not data["data"]:
                logger.debug(f"av_options: {ticker} 无数据")
                return 0

            df = pd.DataFrame(data["data"])
            for col in ["strike", "implied_volatility", "volume", "open_interest", "delta"]:
                if col in df.columns:
                    df[col] = pd.to_numeric(df[col], errors="coerce")

            if df.empty or "date" not in df.columns:
                logger.debug(f"av_options: {ticker} DataFrame 为空")
                return 0

            # 筛选 ATM（|delta| 在 0.3~0.7 之间）
            df_atm = df[(df["delta"].abs() >= 0.3) & (df["delta"].abs() <= 0.7)].copy()
            if df_atm.empty:
                # 退而求其次：取所有有 IV 的
                df_atm = df[df["implied_volatility"].notna()].copy()

            if df_atm.empty:
                logger.debug(f"av_options: {ticker} 无 ATM 数据")
                return 0

            # 按日期聚合
            snapshots = []
            for dt, grp in df_atm.groupby("date"):
                calls = grp[grp["type"] == "call"]
                puts = grp[grp["type"] == "put"]

                avg_call_iv = calls["implied_volatility"].mean() if not calls.empty else None
                avg_put_iv = puts["implied_volatility"].mean() if not puts.empty else None
                iv_skew = (avg_put_iv - avg_call_iv) if avg_put_iv and avg_call_iv else None

                call_vol = calls["volume"].sum()
                put_vol = puts["volume"].sum()
                pc_vol_ratio = put_vol / call_vol if call_vol > 0 else None

                call_oi = calls["open_interest"].sum()
                put_oi = puts["open_interest"].sum()
                pc_oi_ratio = put_oi / call_oi if call_oi > 0 else None

                snapshots.append({
                    "ticker": ticker,
                    "date": dt,
                    "avg_call_iv": avg_call_iv,
                    "avg_put_iv": avg_put_iv,
                    "iv_skew": iv_skew,
                    "put_call_volume_ratio": pc_vol_ratio,
                    "put_call_oi_ratio": pc_oi_ratio,
                    "total_volume": int(grp["volume"].sum()),
                    "total_open_interest": int(grp["open_interest"].sum()),
                })

            if snapshots:
                result_df = pd.DataFrame(snapshots)
                self.db.upsert_us_options_snapshot(result_df)
                return len(snapshots)

            logger.debug(f"av_options: {ticker} 聚合后无快照")
            return 0

        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = {pool.submit(_fetch_single, t): t for t in tickers}
            for future in tqdm(as_completed(futures), total=len(futures),
                               desc="AV Options Snapshot"):
                try:
                    total += future.result()
                except Exception as e:
                    logger.warning(f"Options 失败 {futures[future]}: {e}")

        logger.info(f"AV options snapshot 总计: {total} 条")
        return total

    def download_av_all(self) -> dict:
        """Alpha Vantage: 全量下载。"""
        results = {}
        results["news_sentiment"] = self.download_av_news_sentiment()
        results["options_snapshot"] = self.download_av_options_snapshot()
        return results

    # ==============================================================
    # 全量导入调度
    # ==============================================================

    def download_fmp_all_bulk(self, start_year: int = 1995) -> dict:
        """FMP: 全量下载 (bulk + per-ticker)。"""
        results = {}
        # Bulk by year
        results["stock_list"] = self.download_fmp_stock_list()
        results["index_constituents"] = self.download_fmp_index_constituents()
        results["earnings_surprises"] = self.download_fmp_earnings_surprises_bulk(start_year)
        results["eps_estimates"] = self.download_fmp_eps_estimates_bulk(start_year)
        results["income_statement"] = self.download_fmp_income_statement_bulk(start_year)
        results["key_metrics"] = self.download_fmp_key_metrics()
        results["ratios"] = self.download_fmp_ratios()
        # Per-ticker
        results["prices"] = self._download_fmp_prices_per_ticker(start_year, datetime.now().year)
        results["profiles"] = self.download_fmp_profiles()
        results["insider"] = self.download_fmp_insider_trading()
        results["dividends_splits"] = self.download_fmp_dividends_splits()
        results["index_daily"] = self.download_fmp_index_daily(start_year)
        results["commodities"] = self.download_fmp_commodity_prices(start_year)
        results["macro"] = self.download_fmp_macro()
        return results

    def download_fmp_all_per_ticker(self, start_year: int = 2015) -> dict:
        """FMP: 全量 per-ticker 下载。"""
        results = {}
        results["prices"] = self._download_fmp_prices_per_ticker(start_year, datetime.now().year)
        results["profiles"] = self.download_fmp_profiles()
        results["insider"] = self.download_fmp_insider_trading()
        results["dividends_splits"] = self.download_fmp_dividends_splits()
        results["index_daily"] = self.download_fmp_index_daily(start_year)
        results["commodities"] = self.download_fmp_commodity_prices(start_year)
        results["macro"] = self.download_fmp_macro()
        return results

    def download_all(self, start_year: int = 1995) -> dict:
        """四家 API 全量导入。"""
        results = {}

        logger.info("=" * 60)
        logger.info("Phase 1: FMP Bulk (按年批量)")
        logger.info("=" * 60)
        results["fmp_bulk"] = self.download_fmp_all_bulk(start_year)

        logger.info("=" * 60)
        logger.info("Phase 2: FMP Per-ticker (行情/insider/分红)")
        logger.info("=" * 60)
        results["fmp_ticker"] = self.download_fmp_all_per_ticker(start_year)

        logger.info("=" * 60)
        logger.info("Phase 3: Unusual Whales")
        logger.info("=" * 60)
        results["uw"] = self.download_uw_all()

        logger.info("=" * 60)
        logger.info("Phase 4: Fiscal.ai")
        logger.info("=" * 60)
        results["fiscal"] = self.download_fiscal_all()

        return results
