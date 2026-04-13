"""
FMP 数据批量下载器

原则：
- API 返回什么就存什么，不做字段过滤
- 列名 = _camel_to_snake(API字段)，禁止 rename
- 唯一例外：date→trade_date（daily_price unique key）、ticker→index_code/commodity_symbol（非股票）
- 使用 SQLAlchemy ORM 写库
"""

import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta

import pandas as pd
import requests
from tqdm import tqdm

from services.config import (
    FMP_API_KEY, FMP_RATE_LIMIT,
    LOG_LEVEL,
)
from services.data.database import (
    DatabaseManager,
    USStockBasic, USDailyPrice, USFinancialData, USKeyMetric,
    USIndustryClass, USEarningsSurprise, USEpsEstimate,
    USInsiderTrade, USAnalystRecommendation, USCorporateAction,
    USIndexDaily, USCommodityPrice, USMacroIndicator, USSecFiling,
    USCompanyProfile, USHistoricalMarketCap, USSharesFloat,
    USFinancialScore, USFinancialGrowth, USEnterpriseValue,
    USOwnerEarnings, USRevenueSegment, USDCFValuation,
    USStockPeer, USESGRating, USPriceTarget, USInsiderStatistic,
    USEmployeeCount, USIndexConstituent, USSymbolChange, USDelisted,
    USCongressTrade, USPressRelease, USNews,
)

logger = logging.getLogger(__name__)
logger.setLevel(LOG_LEVEL)


# ============================================================
# camelCase → snake_case 转换器
# ============================================================

_ABBREVIATIONS = {
    "EBITDA": "ebitda", "EBIT": "ebit", "EPS": "eps",
    "EBT": "ebt", "ROE": "roe", "ROA": "roa",
    "ROIC": "roic", "ROCE": "roce", "SGA": "sga",
    "OCF": "ocf", "FCF": "fcf", "DCF": "dcf",
    "TTM": "ttm", "IPO": "ipo", "CEO": "ceo",
    "CFO": "cfo", "CIK": "cik", "SIC": "sic",
    "ESG": "esg", "ETF": "etf", "WACC": "wacc",
    "USD": "usd", "PE": "pe", "PB": "pb",
}

_FMP_RENAMES = {"symbol": "ticker"}


def _camel_to_snake(name: str) -> str:
    if name in _FMP_RENAMES:
        return _FMP_RENAMES[name]
    result = name
    for abbr in sorted(_ABBREVIATIONS.keys(), key=len, reverse=True):
        idx = result.find(abbr)
        while idx != -1:
            before = result[idx - 1] if idx > 0 else ""
            after = result[idx + len(abbr):idx + len(abbr) + 1] if idx + len(abbr) < len(result) else ""
            prefix = "_" if before and before.islower() else ""
            suffix = "_" if after and after.islower() else ""
            replacement = prefix + _ABBREVIATIONS[abbr] + suffix
            result = result[:idx] + replacement + result[idx + len(abbr):]
            idx = result.find(abbr)
    result = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", result)
    result = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", result)
    result = result.lower()
    result = re.sub(r"_+", "_", result).strip("_")
    return result


def _fmp_df_to_snake(df: pd.DataFrame) -> pd.DataFrame:
    col_map = {col: _camel_to_snake(col) for col in df.columns}
    return df.rename(columns=col_map)


# ============================================================
# Rate Limiter
# ============================================================

class RateLimiter:
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


# ============================================================
# HTTP 请求 + 重试
# ============================================================

_429_WAITS = [5, 10, 20, 30, 60]


def _request_with_retry(method: str, url: str, max_retries: int = 5, **kwargs) -> requests.Response:
    kwargs.setdefault("timeout", 60)
    for attempt in range(max_retries):
        try:
            resp = requests.request(method, url, **kwargs)
            if resp.status_code == 429:
                wait = _429_WAITS[min(attempt, len(_429_WAITS) - 1)]
                logger.warning(f"Rate limited (429), waiting {wait}s (attempt {attempt+1}/{max_retries})...")
                time.sleep(wait)
                continue
            if resp.status_code >= 500:
                logger.warning(f"Server error ({resp.status_code}), retrying...")
                time.sleep(2 ** attempt)
                continue
            return resp
        except requests.exceptions.RequestException as e:
            logger.warning(f"HTTP 异常 (attempt {attempt+1}/{max_retries}): {e}")
            if attempt == max_retries - 1:
                raise
            time.sleep(2 ** attempt)
    return resp


# ============================================================
# BulkDownloader
# ============================================================

class BulkDownloader:
    """FMP 数据批量下载器"""

    def __init__(self, db: DatabaseManager):
        self.db = db
        self._fmp_limiter = RateLimiter(FMP_RATE_LIMIT)

    # --- FMP HTTP helpers ---

    def _fmp_get_json(self, path: str, params: dict = None, version: str = "v3") -> list | dict:
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

    def _fmp_get_stable(self, endpoint: str, params: dict = None) -> list | dict:
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

    # --- 断点续跑 ---

    def _skip_done_tickers(self, table: str, tickers: list[str]) -> list[str]:
        done = self.db.get_import_done_tickers(table)
        remaining = [t for t in tickers if t not in done]
        if done:
            logger.info(f"断点续跑 {table}: 全部 {len(tickers)}, 已完成 {len(done)}, 待跑 {len(remaining)}")
        return remaining

    def _skip_if_table_has_data(self, table: str, min_rows: int = 10) -> bool:
        try:
            with self.db.get_session() as session:
                from sqlalchemy import func
                # 用 raw count 避免全表扫描
                r = self.db.query(f"SELECT COUNT(*) as cnt FROM \"{table}\"")
                cnt = int(r["cnt"].iloc[0])
                if cnt >= min_rows:
                    logger.info(f"{table} 已有 {cnt} 条数据（>={min_rows}），跳过")
                    return True
        except Exception:
            pass
        return False

    def _mark_done(self, table: str, fetch_fn, ticker: str):
        """包装 fetch：成功后标记 import_progress。"""
        count = fetch_fn(ticker)
        if count > 0:
            self.db.mark_import_done(table, ticker)
        return count

    # ============================================================
    # FMP 下载方法
    # ============================================================

    # --- 1. stock_list ---
    def download_fmp_stock_list(self) -> int:
        if self._skip_if_table_has_data("us_stock_basic"):
            return 0
        all_records = []
        for exchange in ["NYSE", "NASDAQ", "AMEX"]:
            data = self._fmp_get_json(
                "stock-screener",
                params={"exchange": exchange, "limit": 20000, "isActivelyTrading": "true"},
            )
            if isinstance(data, list):
                all_records.extend(data)
            logger.info(f"FMP stock-screener {exchange}: {len(data) if isinstance(data, list) else 0} 只")

        if not all_records:
            logger.warning("FMP stock-screener: 无数据")
            return 0

        df = _fmp_df_to_snake(pd.DataFrame(all_records))
        df = df.drop_duplicates(subset=["ticker"])
        self.db.upsert_df(USStockBasic, df, ["ticker"])

        # 同时写 industry_class
        if "sector" in df.columns:
            ind_df = df[["ticker", "sector", "industry"]].dropna(subset=["sector"])
            if not ind_df.empty:
                self.db.upsert_df(USIndustryClass, ind_df, ["ticker"])

        logger.info(f"FMP 全市场股票列表: {len(df)} 只")
        return len(df)

    # --- 2. company_profiles ---
    def download_fmp_company_profiles(self, tickers: list[str] = None) -> int:
        if self._skip_if_table_has_data("us_company_profile", min_rows=5000):
            return 0
        if tickers is None:
            tickers = self.db.get_us_tickers(stocks_only=False)
        if not tickers:
            return 0
        total = 0
        batch_size = 50
        batches = [tickers[i:i + batch_size] for i in range(0, len(tickers), batch_size)]

        def _fetch_batch(batch):
            symbols = ",".join(batch)
            data = self._fmp_get_json(f"profile/{symbols}")
            if not data:
                return 0
            df = _fmp_df_to_snake(pd.DataFrame(data))
            self.db.upsert_df(USCompanyProfile, df, ["ticker"])
            return len(df)

        with ThreadPoolExecutor(max_workers=10) as pool:
            futures = {pool.submit(_fetch_batch, b): b for b in batches}
            for f in tqdm(as_completed(futures), total=len(futures), desc="FMP Company Profiles"):
                try:
                    total += f.result()
                except Exception as e:
                    logger.warning(f"Profile batch 失败: {e}")
        logger.info(f"FMP company profiles 总计: {total} 条")
        return total

    # --- 3. daily_price ---
    def download_fmp_daily_prices(self, start_year: int = 1995) -> int:
        tickers = self.db.get_us_tickers(stocks_only=False)
        if not tickers:
            return 0
        tickers = self._skip_done_tickers("us_daily_price", tickers)
        if not tickers:
            return 0

        end_year = datetime.now().year
        segments = []
        for yr in range(start_year, end_year + 1, 10):
            seg_end = min(yr + 9, end_year)
            segments.append((f"{yr}-01-01", f"{seg_end}-12-31"))

        total = 0

        def _fetch(ticker):
            count = 0
            for seg_start, seg_end in segments:
                data = self._fmp_get_stable(
                    "historical-price-eod/full",
                    params={"symbol": ticker, "from": seg_start, "to": seg_end},
                )
                if not data:
                    continue
                df = _fmp_df_to_snake(pd.DataFrame(data))
                # date→trade_date（unique key 需要）
                if "date" in df.columns:
                    df["trade_date"] = pd.to_datetime(df["date"]).dt.date
                    df = df.drop(columns=["date"])
                df = df[df["trade_date"].notna()]
                if not df.empty:
                    self.db.upsert_df(USDailyPrice, df, ["ticker", "trade_date"])
                    count += len(df)
            return count

        with ThreadPoolExecutor(max_workers=10) as pool:
            futures = {pool.submit(self._mark_done, "us_daily_price", _fetch, t): t for t in tickers}
            for f in tqdm(as_completed(futures), total=len(futures), desc="FMP Daily Prices"):
                try:
                    total += f.result()
                except Exception as e:
                    logger.warning(f"Price 失败 {futures[f]}: {e}")
        logger.info(f"FMP daily prices 总计: {total} 条")
        return total

    # --- 4. historical_market_cap ---
    def download_fmp_historical_market_cap(self, tickers: list[str] = None, limit: int = 5000) -> int:
        if tickers is None:
            tickers = self.db.get_us_tickers(stocks_only=True)
        if not tickers:
            return 0
        tickers = self._skip_done_tickers("us_historical_market_cap", tickers)
        if not tickers:
            return 0
        total = 0

        def _fetch(ticker):
            data = self._fmp_get_stable(
                "historical-market-capitalization",
                params={"symbol": ticker, "limit": limit},
            )
            if not data:
                return 0
            df = _fmp_df_to_snake(pd.DataFrame(data))
            self.db.upsert_df(USHistoricalMarketCap, df, ["ticker", "date"])
            return len(df)

        with ThreadPoolExecutor(max_workers=10) as pool:
            futures = {pool.submit(self._mark_done, "us_historical_market_cap", _fetch, t): t for t in tickers}
            for f in tqdm(as_completed(futures), total=len(futures), desc="FMP Historical Market Cap"):
                try:
                    total += f.result()
                except Exception as e:
                    logger.warning(f"Historical market cap 失败 {futures[f]}: {e}")
        logger.info(f"FMP historical market cap 总计: {total} 条")
        return total

    # --- 5. financial_quarterly (IS+BS+CF 三表合并) ---
    def download_fmp_financial_quarterly(self, tickers: list[str] = None, limit: int = 400) -> int:
        if tickers is None:
            tickers = self.db.get_us_tickers(stocks_only=False)
        if not tickers:
            return 0
        tickers = self._skip_done_tickers("us_financial_data", tickers)
        if not tickers:
            return 0
        total = 0

        def _fetch(ticker):
            # 拉三张表
            is_data = self._fmp_get_stable("income-statement", params={"symbol": ticker, "period": "quarter", "limit": limit})
            bs_data = self._fmp_get_stable("balance-sheet-statement", params={"symbol": ticker, "period": "quarter", "limit": limit})
            cf_data = self._fmp_get_stable("cash-flow-statement", params={"symbol": ticker, "period": "quarter", "limit": limit})

            if not is_data:
                return 0

            is_df = _fmp_df_to_snake(pd.DataFrame(is_data))
            bs_df = _fmp_df_to_snake(pd.DataFrame(bs_data)) if bs_data else pd.DataFrame()
            cf_df = _fmp_df_to_snake(pd.DataFrame(cf_data)) if cf_data else pd.DataFrame()

            # 合并（以 IS 为基础，按 ticker+date 左连接）
            merge_keys = ["ticker", "date"]
            merged = is_df.copy()
            if not bs_df.empty:
                bs_cols = merge_keys + [c for c in bs_df.columns if c not in merged.columns]
                merged = merged.merge(bs_df[bs_cols], on=merge_keys, how="left")
            if not cf_df.empty:
                cf_cols = merge_keys + [c for c in cf_df.columns if c not in merged.columns]
                merged = merged.merge(cf_df[cf_cols], on=merge_keys, how="left")

            # 构造 period_label
            if "fiscal_year" in merged.columns and "period" in merged.columns:
                merged["period"] = merged["fiscal_year"].astype(str) + "-" + merged["period"].astype(str)

            merged = merged.dropna(subset=["ticker", "date"])
            if merged.empty:
                return 0

            self.db.upsert_df(USFinancialData, merged, ["ticker", "period"])
            return len(merged)

        with ThreadPoolExecutor(max_workers=10) as pool:
            futures = {pool.submit(self._mark_done, "us_financial_data", _fetch, t): t for t in tickers}
            for f in tqdm(as_completed(futures), total=len(futures), desc="FMP Quarterly Financials"):
                try:
                    total += f.result()
                except Exception as e:
                    logger.warning(f"Financial 失败 {futures[f]}: {e}")
        logger.info(f"FMP quarterly financials 总计: {total} 条")
        return total

    # --- 6. key_metrics ---
    def download_fmp_key_metrics(self, tickers: list[str] = None, limit: int = 400) -> int:
        if tickers is None:
            tickers = self.db.get_us_tickers(stocks_only=False)
        if not tickers:
            return 0
        tickers = self._skip_done_tickers("us_key_metric", tickers)
        if not tickers:
            return 0
        total = 0

        def _fetch(ticker):
            data = self._fmp_get_stable("key-metrics", params={"symbol": ticker, "period": "quarter", "limit": limit})
            if not data:
                return 0
            df = _fmp_df_to_snake(pd.DataFrame(data))
            self.db.upsert_df(USKeyMetric, df, ["ticker", "date"])
            return len(df)

        with ThreadPoolExecutor(max_workers=10) as pool:
            futures = {pool.submit(self._mark_done, "us_key_metric", _fetch, t): t for t in tickers}
            for f in tqdm(as_completed(futures), total=len(futures), desc="FMP Key Metrics"):
                try:
                    total += f.result()
                except Exception as e:
                    logger.warning(f"Key metrics 失败 {futures[f]}: {e}")
        logger.info(f"FMP key metrics 总计: {total} 条")
        return total

    # --- 7. ratios ---
    def download_fmp_ratios(self, tickers: list[str] = None, limit: int = 400) -> int:
        if tickers is None:
            tickers = self.db.get_us_tickers(stocks_only=False)
        if not tickers:
            return 0
        # ratios 和 key_metrics 共享表，不做断点续跑（key_metrics 已标记）
        total = 0

        def _fetch(ticker):
            data = self._fmp_get_stable("ratios", params={"symbol": ticker, "period": "quarter", "limit": limit})
            if not data:
                return 0
            df = _fmp_df_to_snake(pd.DataFrame(data))
            self.db.upsert_df(USKeyMetric, df, ["ticker", "date"])
            return len(df)

        with ThreadPoolExecutor(max_workers=10) as pool:
            futures = {pool.submit(_fetch, t): t for t in tickers}
            for f in tqdm(as_completed(futures), total=len(futures), desc="FMP Ratios"):
                try:
                    total += f.result()
                except Exception as e:
                    logger.warning(f"Ratios 失败 {futures[f]}: {e}")
        logger.info(f"FMP ratios 总计: {total} 条")
        return total

    # --- 8. financial_growth ---
    def download_fmp_financial_growth(self, tickers: list[str] = None, limit: int = 400) -> int:
        if tickers is None:
            tickers = self.db.get_us_tickers(stocks_only=True)
        if not tickers:
            return 0
        tickers = self._skip_done_tickers("us_financial_growth", tickers)
        if not tickers:
            return 0
        total = 0

        def _fetch(ticker):
            data = self._fmp_get_stable("financial-growth", params={"symbol": ticker, "period": "quarter", "limit": limit})
            if not data:
                return 0
            df = _fmp_df_to_snake(pd.DataFrame(data))
            self.db.upsert_df(USFinancialGrowth, df, ["ticker", "date"])
            return len(df)

        with ThreadPoolExecutor(max_workers=10) as pool:
            futures = {pool.submit(self._mark_done, "us_financial_growth", _fetch, t): t for t in tickers}
            for f in tqdm(as_completed(futures), total=len(futures), desc="FMP Financial Growth"):
                try:
                    total += f.result()
                except Exception as e:
                    logger.warning(f"Financial growth 失败 {futures[f]}: {e}")
        logger.info(f"FMP financial growth 总计: {total} 条")
        return total

    # --- 9. enterprise_values ---
    def download_fmp_enterprise_values(self, tickers: list[str] = None, limit: int = 400) -> int:
        if tickers is None:
            tickers = self.db.get_us_tickers(stocks_only=True)
        if not tickers:
            return 0
        tickers = self._skip_done_tickers("us_enterprise_value", tickers)
        if not tickers:
            return 0
        total = 0

        def _fetch(ticker):
            data = self._fmp_get_stable("enterprise-values", params={"symbol": ticker, "period": "quarter", "limit": limit})
            if not data:
                return 0
            df = _fmp_df_to_snake(pd.DataFrame(data))
            self.db.upsert_df(USEnterpriseValue, df, ["ticker", "date"])
            return len(df)

        with ThreadPoolExecutor(max_workers=10) as pool:
            futures = {pool.submit(self._mark_done, "us_enterprise_value", _fetch, t): t for t in tickers}
            for f in tqdm(as_completed(futures), total=len(futures), desc="FMP Enterprise Values"):
                try:
                    total += f.result()
                except Exception as e:
                    logger.warning(f"Enterprise values 失败 {futures[f]}: {e}")
        logger.info(f"FMP enterprise values 总计: {total} 条")
        return total

    # --- 10. owner_earnings ---
    def download_fmp_owner_earnings(self, tickers: list[str] = None) -> int:
        if tickers is None:
            tickers = self.db.get_us_tickers(stocks_only=True)
        if not tickers:
            return 0
        tickers = self._skip_done_tickers("us_owner_earnings", tickers)
        if not tickers:
            return 0
        total = 0

        def _fetch(ticker):
            data = self._fmp_get_stable("owner-earnings", params={"symbol": ticker})
            if not data:
                return 0
            df = _fmp_df_to_snake(pd.DataFrame(data if isinstance(data, list) else [data]))
            self.db.upsert_df(USOwnerEarnings, df, ["ticker", "date"])
            return len(df)

        with ThreadPoolExecutor(max_workers=10) as pool:
            futures = {pool.submit(self._mark_done, "us_owner_earnings", _fetch, t): t for t in tickers}
            for f in tqdm(as_completed(futures), total=len(futures), desc="FMP Owner Earnings"):
                try:
                    total += f.result()
                except Exception as e:
                    logger.warning(f"Owner earnings 失败 {futures[f]}: {e}")
        logger.info(f"FMP owner earnings 总计: {total} 条")
        return total

    # --- 11. earnings_surprises ---
    def download_fmp_earnings_surprises(self, tickers: list[str] = None) -> int:
        if tickers is None:
            tickers = self.db.get_us_tickers(stocks_only=False)
        if not tickers:
            return 0
        tickers = self._skip_done_tickers("us_earnings_surprise", tickers)
        if not tickers:
            return 0
        total = 0

        def _fetch(ticker):
            data = self._fmp_get_json(f"earnings-surprises/{ticker}", params={"limit": 400})
            if not data:
                return 0
            df = _fmp_df_to_snake(pd.DataFrame(data))
            df = df[df["ticker"].notna()].copy()
            # 计算 surprise
            if "eps_actual" in df.columns and "eps_estimated" in df.columns:
                df["eps_actual"] = pd.to_numeric(df["eps_actual"], errors="coerce")
                df["eps_estimated"] = pd.to_numeric(df["eps_estimated"], errors="coerce")
                df["surprise"] = df["eps_actual"] - df["eps_estimated"]
                df["surprise_pct"] = df.apply(
                    lambda r: r["surprise"] / abs(r["eps_estimated"])
                    if pd.notna(r["eps_estimated"]) and r["eps_estimated"] != 0 else None,
                    axis=1,
                )
            self.db.upsert_df(USEarningsSurprise, df, ["ticker", "date"])
            return len(df)

        with ThreadPoolExecutor(max_workers=10) as pool:
            futures = {pool.submit(self._mark_done, "us_earnings_surprise", _fetch, t): t for t in tickers}
            for f in tqdm(as_completed(futures), total=len(futures), desc="FMP Earnings Surprises"):
                try:
                    total += f.result()
                except Exception as e:
                    logger.warning(f"Earnings 失败 {futures[f]}: {e}")
        logger.info(f"FMP earnings surprises 总计: {total} 条")
        return total

    # --- 12. eps_estimates ---
    def download_fmp_eps_estimates(self, tickers: list[str] = None) -> int:
        if tickers is None:
            tickers = self.db.get_us_tickers(stocks_only=False)
        if not tickers:
            return 0
        tickers = self._skip_done_tickers("us_eps_estimate", tickers)
        if not tickers:
            return 0
        total = 0

        def _fetch(ticker):
            data = self._fmp_get_json(f"analyst-estimates/{ticker}", params={"period": "quarter", "limit": 200})
            if not data:
                return 0
            df = _fmp_df_to_snake(pd.DataFrame(data))
            self.db.upsert_df(USEpsEstimate, df, ["ticker", "date"])
            return len(df)

        with ThreadPoolExecutor(max_workers=10) as pool:
            futures = {pool.submit(self._mark_done, "us_eps_estimate", _fetch, t): t for t in tickers}
            for f in tqdm(as_completed(futures), total=len(futures), desc="FMP EPS Estimates"):
                try:
                    total += f.result()
                except Exception as e:
                    logger.warning(f"EPS estimates 失败 {futures[f]}: {e}")
        logger.info(f"FMP eps estimates 总计: {total} 条")
        return total

    # --- 13. insider_trading ---
    def download_fmp_insider_trading(self, tickers: list[str] = None) -> int:
        if tickers is None:
            tickers = self.db.get_us_tickers(stocks_only=False)
        if not tickers:
            return 0
        tickers = self._skip_done_tickers("us_insider_trade", tickers)
        if not tickers:
            return 0
        total = 0

        def _fetch(ticker):
            count = 0
            page = 0
            while True:
                data = self._fmp_get_json("insider-trading", params={"symbol": ticker, "page": page, "limit": 100}, version="v4")
                if not data:
                    break
                df = _fmp_df_to_snake(pd.DataFrame(data))
                self.db.upsert_df(USInsiderTrade, df, ["ticker", "transaction_date", "reporting_name", "transaction_type"])
                count += len(df)
                if len(data) < 100:
                    break
                page += 1
            return count

        with ThreadPoolExecutor(max_workers=10) as pool:
            futures = {pool.submit(self._mark_done, "us_insider_trade", _fetch, t): t for t in tickers}
            for f in tqdm(as_completed(futures), total=len(futures), desc="FMP Insider Trading"):
                try:
                    total += f.result()
                except Exception as e:
                    logger.warning(f"Insider 失败 {futures[f]}: {e}")
        logger.info(f"FMP insider trading 总计: {total} 条")
        return total

    # --- 14. analyst_grades ---
    def download_fmp_analyst_grades(self, tickers: list[str] = None) -> int:
        if tickers is None:
            tickers = self.db.get_us_tickers(stocks_only=False)
        if not tickers:
            return 0
        tickers = self._skip_done_tickers("us_analyst_recommendation", tickers)
        if not tickers:
            return 0
        total = 0

        def _fetch(ticker):
            data = self._fmp_get_json(f"grade/{ticker}")
            if not data:
                return 0
            df = _fmp_df_to_snake(pd.DataFrame(data))
            df = df[df["date"].notna() & df["new_grade"].notna()]
            if df.empty:
                return 0
            self.db.upsert_df(USAnalystRecommendation, df, ["ticker", "date", "grading_company"])
            return len(df)

        with ThreadPoolExecutor(max_workers=10) as pool:
            futures = {pool.submit(self._mark_done, "us_analyst_recommendation", _fetch, t): t for t in tickers}
            for f in tqdm(as_completed(futures), total=len(futures), desc="FMP Analyst Grades"):
                try:
                    total += f.result()
                except Exception as e:
                    logger.warning(f"Grade 失败 {futures[f]}: {e}")
        logger.info(f"FMP analyst grades 总计: {total} 条")
        return total

    # --- 15. dividends_splits ---
    def download_fmp_dividends_splits(self, tickers: list[str] = None) -> int:
        if tickers is None:
            tickers = self.db.get_us_tickers(stocks_only=False)
        if not tickers:
            return 0
        tickers = self._skip_done_tickers("us_corporate_action", tickers)
        if not tickers:
            return 0
        total = 0

        def _fetch(ticker):
            count = 0
            # Dividends
            div_data = self._fmp_get_stable("dividends", params={"symbol": ticker})
            if div_data and isinstance(div_data, list):
                df = _fmp_df_to_snake(pd.DataFrame(div_data))
                df["action_type"] = "dividend"
                self.db.upsert_df(USCorporateAction, df, ["ticker", "date", "action_type"])
                count += len(df)
            # Splits
            split_data = self._fmp_get_stable("splits", params={"symbol": ticker})
            if split_data and isinstance(split_data, list):
                df = _fmp_df_to_snake(pd.DataFrame(split_data))
                df["action_type"] = "split"
                self.db.upsert_df(USCorporateAction, df, ["ticker", "date", "action_type"])
                count += len(df)
            return count

        with ThreadPoolExecutor(max_workers=10) as pool:
            futures = {pool.submit(self._mark_done, "us_corporate_action", _fetch, t): t for t in tickers}
            for f in tqdm(as_completed(futures), total=len(futures), desc="FMP Dividends & Splits"):
                try:
                    total += f.result()
                except Exception as e:
                    logger.warning(f"Div/Split 失败 {futures[f]}: {e}")
        logger.info(f"FMP dividends & splits 总计: {total} 条")
        return total

    # --- 16. financial_scores ---
    def download_fmp_financial_scores(self, tickers: list[str] = None) -> int:
        if tickers is None:
            tickers = self.db.get_us_tickers(stocks_only=True)
        if not tickers:
            return 0
        tickers = self._skip_done_tickers("us_financial_score", tickers)
        if not tickers:
            return 0
        total = 0

        def _fetch(ticker):
            data = self._fmp_get_stable("financial-scores", params={"symbol": ticker})
            if not data:
                return 0
            df = _fmp_df_to_snake(pd.DataFrame(data if isinstance(data, list) else [data]))
            self.db.upsert_df(USFinancialScore, df, ["ticker"])
            return len(df)

        with ThreadPoolExecutor(max_workers=10) as pool:
            futures = {pool.submit(self._mark_done, "us_financial_score", _fetch, t): t for t in tickers}
            for f in tqdm(as_completed(futures), total=len(futures), desc="FMP Financial Scores"):
                try:
                    total += f.result()
                except Exception as e:
                    logger.warning(f"Financial scores 失败 {futures[f]}: {e}")
        logger.info(f"FMP financial scores 总计: {total} 条")
        return total

    # --- 17. shares_float ---
    def download_fmp_shares_float(self, tickers: list[str] = None) -> int:
        if tickers is None:
            tickers = self.db.get_us_tickers(stocks_only=True)
        if not tickers:
            return 0
        tickers = self._skip_done_tickers("us_shares_float", tickers)
        if not tickers:
            return 0
        total = 0

        def _fetch(ticker):
            data = self._fmp_get_stable("shares-float", params={"symbol": ticker})
            if not data:
                return 0
            df = _fmp_df_to_snake(pd.DataFrame(data if isinstance(data, list) else [data]))
            self.db.upsert_df(USSharesFloat, df, ["ticker", "date"])
            return len(df)

        with ThreadPoolExecutor(max_workers=10) as pool:
            futures = {pool.submit(self._mark_done, "us_shares_float", _fetch, t): t for t in tickers}
            for f in tqdm(as_completed(futures), total=len(futures), desc="FMP Shares Float"):
                try:
                    total += f.result()
                except Exception as e:
                    logger.warning(f"Shares float 失败 {futures[f]}: {e}")
        logger.info(f"FMP shares float 总计: {total} 条")
        return total

    # --- 18. insider_statistics ---
    def download_fmp_insider_statistics(self, tickers: list[str] = None) -> int:
        if tickers is None:
            tickers = self.db.get_us_tickers(stocks_only=True)
        if not tickers:
            return 0
        tickers = self._skip_done_tickers("us_insider_statistic", tickers)
        if not tickers:
            return 0
        total = 0

        def _fetch(ticker):
            data = self._fmp_get_stable("insider-trading/statistics", params={"symbol": ticker})
            if not data:
                return 0
            df = _fmp_df_to_snake(pd.DataFrame(data if isinstance(data, list) else [data]))
            self.db.upsert_df(USInsiderStatistic, df, ["ticker", "year", "quarter"])
            return len(df)

        with ThreadPoolExecutor(max_workers=10) as pool:
            futures = {pool.submit(self._mark_done, "us_insider_statistic", _fetch, t): t for t in tickers}
            for f in tqdm(as_completed(futures), total=len(futures), desc="FMP Insider Statistics"):
                try:
                    total += f.result()
                except Exception as e:
                    logger.warning(f"Insider stats 失败 {futures[f]}: {e}")
        logger.info(f"FMP insider statistics 总计: {total} 条")
        return total

    # --- 19. employee_count ---
    def download_fmp_employee_count(self, tickers: list[str] = None) -> int:
        if tickers is None:
            tickers = self.db.get_us_tickers(stocks_only=True)
        if not tickers:
            return 0
        tickers = self._skip_done_tickers("us_employee_count", tickers)
        if not tickers:
            return 0
        total = 0

        def _fetch(ticker):
            data = self._fmp_get_stable("employee-count", params={"symbol": ticker})
            if not data:
                return 0
            df = _fmp_df_to_snake(pd.DataFrame(data))
            self.db.upsert_df(USEmployeeCount, df, ["ticker", "period_of_report"])
            return len(df)

        with ThreadPoolExecutor(max_workers=10) as pool:
            futures = {pool.submit(self._mark_done, "us_employee_count", _fetch, t): t for t in tickers}
            for f in tqdm(as_completed(futures), total=len(futures), desc="FMP Employee Count"):
                try:
                    total += f.result()
                except Exception as e:
                    logger.warning(f"Employee count 失败 {futures[f]}: {e}")
        logger.info(f"FMP employee count 总计: {total} 条")
        return total

    # --- 20. price_targets ---
    def download_fmp_price_targets(self, tickers: list[str] = None) -> int:
        if tickers is None:
            tickers = self.db.get_us_tickers(stocks_only=True)
        if not tickers:
            return 0
        tickers = self._skip_done_tickers("us_price_target", tickers)
        if not tickers:
            return 0
        total = 0

        def _fetch(ticker):
            data = self._fmp_get_stable("price-target-consensus", params={"symbol": ticker})
            if not data:
                return 0
            df = _fmp_df_to_snake(pd.DataFrame(data if isinstance(data, list) else [data]))
            self.db.upsert_df(USPriceTarget, df, ["ticker"])
            return len(df)

        with ThreadPoolExecutor(max_workers=10) as pool:
            futures = {pool.submit(self._mark_done, "us_price_target", _fetch, t): t for t in tickers}
            for f in tqdm(as_completed(futures), total=len(futures), desc="FMP Price Targets"):
                try:
                    total += f.result()
                except Exception as e:
                    logger.warning(f"Price targets 失败 {futures[f]}: {e}")
        logger.info(f"FMP price targets 总计: {total} 条")
        return total

    # --- 21. esg_ratings ---
    def download_fmp_esg_ratings(self, tickers: list[str] = None) -> int:
        if tickers is None:
            tickers = self.db.get_us_tickers(stocks_only=True)
        if not tickers:
            return 0
        tickers = self._skip_done_tickers("us_esg_rating", tickers)
        if not tickers:
            return 0
        total = 0

        def _fetch(ticker):
            data = self._fmp_get_stable("esg-ratings", params={"symbol": ticker})
            if not data:
                return 0
            df = _fmp_df_to_snake(pd.DataFrame(data if isinstance(data, list) else [data]))
            self.db.upsert_df(USESGRating, df, ["ticker", "fiscal_year"])
            return len(df)

        with ThreadPoolExecutor(max_workers=10) as pool:
            futures = {pool.submit(self._mark_done, "us_esg_rating", _fetch, t): t for t in tickers}
            for f in tqdm(as_completed(futures), total=len(futures), desc="FMP ESG Ratings"):
                try:
                    total += f.result()
                except Exception as e:
                    logger.warning(f"ESG 失败 {futures[f]}: {e}")
        logger.info(f"FMP ESG ratings 总计: {total} 条")
        return total

    # --- 22. dcf_valuations ---
    def download_fmp_dcf_valuations(self, tickers: list[str] = None) -> int:
        if tickers is None:
            tickers = self.db.get_us_tickers(stocks_only=True)
        if not tickers:
            return 0
        tickers = self._skip_done_tickers("us_dcf_valuation", tickers)
        if not tickers:
            return 0
        total = 0

        def _fetch(ticker):
            data = self._fmp_get_stable("discounted-cash-flow", params={"symbol": ticker})
            if not data:
                return 0
            df = _fmp_df_to_snake(pd.DataFrame(data if isinstance(data, list) else [data]))
            df["dcf_type"] = "standard"
            self.db.upsert_df(USDCFValuation, df, ["ticker", "date", "dcf_type"])
            return len(df)

        with ThreadPoolExecutor(max_workers=10) as pool:
            futures = {pool.submit(self._mark_done, "us_dcf_valuation", _fetch, t): t for t in tickers}
            for f in tqdm(as_completed(futures), total=len(futures), desc="FMP DCF Valuations"):
                try:
                    total += f.result()
                except Exception as e:
                    logger.warning(f"DCF 失败 {futures[f]}: {e}")
        logger.info(f"FMP DCF valuations 总计: {total} 条")
        return total

    # --- 23. stock_peers ---
    def download_fmp_stock_peers(self, tickers: list[str] = None) -> int:
        if tickers is None:
            tickers = self.db.get_us_tickers(stocks_only=True)
        if not tickers:
            return 0
        tickers = self._skip_done_tickers("us_stock_peer", tickers)
        if not tickers:
            return 0
        total = 0

        def _fetch(ticker):
            data = self._fmp_get_stable("stock-peers", params={"symbol": ticker})
            if not data:
                return 0
            peers = data[0].get("peersList", []) if isinstance(data, list) and data else []
            if not peers:
                return 0
            records = [{"ticker": ticker, "peer_ticker": p} for p in peers if p]
            df = pd.DataFrame(records)
            self.db.upsert_df(USStockPeer, df, ["ticker", "peer_ticker"])
            return len(df)

        with ThreadPoolExecutor(max_workers=10) as pool:
            futures = {pool.submit(self._mark_done, "us_stock_peer", _fetch, t): t for t in tickers}
            for f in tqdm(as_completed(futures), total=len(futures), desc="FMP Stock Peers"):
                try:
                    total += f.result()
                except Exception as e:
                    logger.warning(f"Peers 失败 {futures[f]}: {e}")
        logger.info(f"FMP stock peers 总计: {total} 条")
        return total

    # --- 24. index_daily ---
    def download_fmp_index_daily(self, start_year: int = 1995) -> int:
        from services.config import US_INDEX_SYMBOLS
        end_year = datetime.now().year
        segments = []
        for yr in range(start_year, end_year + 1, 10):
            seg_end = min(yr + 9, end_year)
            segments.append((f"{yr}-01-01", f"{seg_end}-12-31"))

        total = 0
        for symbol in tqdm(US_INDEX_SYMBOLS, desc="FMP Index Daily"):
            for seg_start, seg_end in segments:
                data = self._fmp_get_stable(
                    "historical-price-eod/full",
                    params={"symbol": symbol, "from": seg_start, "to": seg_end},
                )
                if not data:
                    continue
                df = _fmp_df_to_snake(pd.DataFrame(data))
                # ticker→index_code, date→trade_date
                if "ticker" in df.columns:
                    df["index_code"] = df["ticker"]
                if "date" in df.columns:
                    df["trade_date"] = pd.to_datetime(df["date"]).dt.date
                    df = df.drop(columns=["date"], errors="ignore")
                df = df[df["trade_date"].notna()]
                if not df.empty:
                    self.db.upsert_df(USIndexDaily, df, ["index_code", "trade_date"])
                    total += len(df)
        logger.info(f"FMP index daily 总计: {total} 条")
        return total

    # --- 25. commodity_prices ---
    def download_fmp_commodity_prices(self, start_year: int = 1995) -> int:
        from services.config import US_COMMODITY_SYMBOLS
        _COMMODITY_MAP = {
            "GC=F": "GCUSD", "SI=F": "SIUSD", "CL=F": "CLUSD",
            "BZ=F": "BZUSD", "NG=F": "NGUSD", "HG=F": "HGUSD",
        }
        end_year = datetime.now().year
        segments = []
        for yr in range(start_year, end_year + 1, 10):
            seg_end = min(yr + 9, end_year)
            segments.append((f"{yr}-01-01", f"{seg_end}-12-31"))

        total = 0
        for yf_sym in tqdm(US_COMMODITY_SYMBOLS, desc="FMP Commodities"):
            fmp_sym = _COMMODITY_MAP.get(yf_sym, yf_sym.replace("=F", "USD"))
            for seg_start, seg_end in segments:
                data = self._fmp_get_stable(
                    "historical-price-eod/full",
                    params={"symbol": fmp_sym, "from": seg_start, "to": seg_end},
                )
                if not data:
                    continue
                df = _fmp_df_to_snake(pd.DataFrame(data))
                df["commodity_symbol"] = yf_sym
                if "date" in df.columns:
                    df["trade_date"] = pd.to_datetime(df["date"]).dt.date
                    df = df.drop(columns=["date"], errors="ignore")
                df = df[df["trade_date"].notna()]
                if not df.empty:
                    self.db.upsert_df(USCommodityPrice, df, ["commodity_symbol", "trade_date"])
                    total += len(df)
        logger.info(f"FMP commodity prices 总计: {total} 条")
        return total

    # --- 26. macro ---
    def download_fmp_macro(self) -> int:
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
        total = 0
        for indicator_code, (endpoint, params) in _MACRO_MAP.items():
            data = self._fmp_get_json(endpoint, params=params, version="v4")
            if not data:
                continue
            records = [{"indicator_code": indicator_code, "report_date": item.get("date"), "value": item.get("value")} for item in data]
            if records:
                df = pd.DataFrame(records)
                self.db.upsert_df(USMacroIndicator, df, ["indicator_code", "report_date"])
                total += len(records)

        # Treasury
        treasury_data = self._fmp_get_json("treasury", params={"from": "1995-01-01"}, version="v4")
        if treasury_data:
            for item in treasury_data:
                date = item.get("date")
                for col, code in [("year10", "US_10Y"), ("year2", "US_2Y")]:
                    val = item.get(col)
                    if val is not None:
                        df = pd.DataFrame([{"indicator_code": code, "report_date": date, "value": val}])
                        self.db.upsert_df(USMacroIndicator, df, ["indicator_code", "report_date"])
                        total += 1
                y10, y2 = item.get("year10"), item.get("year2")
                if y10 is not None and y2 is not None:
                    df = pd.DataFrame([{"indicator_code": "US_2Y10Y", "report_date": date, "value": y10 - y2}])
                    self.db.upsert_df(USMacroIndicator, df, ["indicator_code", "report_date"])
                    total += 1

        logger.info(f"FMP macro 总计: {total} 条")
        return total

    # --- 27. index_constituents_history ---
    def download_fmp_index_constituents_history(self) -> int:
        if self._skip_if_table_has_data("us_index_constituent", min_rows=100):
            return 0
        total = 0
        for index_name, path in [("sp500", "historical/sp500_constituent"), ("nasdaq", "historical/nasdaq_constituent"), ("dowjones", "historical/dowjones_constituent")]:
            data = self._fmp_get_json(path)
            if not data:
                continue
            df = _fmp_df_to_snake(pd.DataFrame(data))
            df["index_name"] = index_name
            self.db.upsert_df(USIndexConstituent, df, ["index_name", "ticker", "date"])
            total += len(df)
            logger.info(f"FMP {index_name} 历史成分: {len(df)} 条")
        logger.info(f"FMP index constituents history 总计: {total} 条")
        return total

    # --- 28. delisted ---
    def download_fmp_delisted_companies(self) -> int:
        if self._skip_if_table_has_data("us_delisted", min_rows=50):
            return 0
        data = self._fmp_get_stable("delisted-companies")
        if not data:
            return 0
        df = _fmp_df_to_snake(pd.DataFrame(data))
        self.db.upsert_df(USDelisted, df, ["ticker"])
        logger.info(f"FMP delisted companies 总计: {len(df)} 条")
        return len(df)

    # --- 29. symbol_changes ---
    def download_fmp_symbol_changes(self) -> int:
        if self._skip_if_table_has_data("us_symbol_change", min_rows=50):
            return 0
        data = self._fmp_get_stable("symbol-change")
        if not data:
            return 0
        df = _fmp_df_to_snake(pd.DataFrame(data))
        self.db.upsert_df(USSymbolChange, df, ["old_symbol", "new_symbol", "date"])
        logger.info(f"FMP symbol changes 总计: {len(df)} 条")
        return len(df)

    # --- 30. congress_trading (per-ticker, /stable/senate-trades + /stable/house-trades) ---
    def download_fmp_congress_trading(self, tickers: list[str] = None) -> int:
        if tickers is None:
            tickers = self.db.get_us_tickers(stocks_only=True)
        if not tickers:
            return 0
        tickers = self._skip_done_tickers("us_congress_trade", tickers)
        if not tickers:
            return 0
        total = 0

        def _fetch(ticker):
            count = 0
            for chamber, endpoint in [("senate", "senate-trades"), ("house", "house-trades")]:
                data = self._fmp_get_stable(endpoint, params={"symbol": ticker})
                if not data:
                    continue
                df = _fmp_df_to_snake(pd.DataFrame(data))
                df["source"] = f"fmp_{chamber}"
                self.db.upsert_df(USCongressTrade, df, ["ticker", "transaction_date", "first_name", "last_name", "type"])
                count += len(df)
            return count

        with ThreadPoolExecutor(max_workers=10) as pool:
            futures = {pool.submit(self._mark_done, "us_congress_trade", _fetch, t): t for t in tickers}
            for f in tqdm(as_completed(futures), total=len(futures), desc="FMP Congress Trading"):
                try:
                    total += f.result()
                except Exception as e:
                    logger.warning(f"Congress 失败 {futures[f]}: {e}")
        logger.info(f"FMP congress trading 总计: {total} 条")
        return total

    # ============================================================
    # 全量导入调度
    # ============================================================

    def download_fmp_all(self, start_year: int = 1995) -> dict:
        """FMP 全量下载"""
        results = {}

        # Phase 1: Bulk 端点
        logger.info("=== Phase 1: Bulk 端点 ===")
        results["stock_list"] = self.download_fmp_stock_list()
        results["delisted"] = self.download_fmp_delisted_companies()
        results["symbol_changes"] = self.download_fmp_symbol_changes()

        # Phase 2: Per-ticker 核心数据（有历史序列）
        logger.info("=== Phase 2: Per-ticker 核心数据 ===")
        results["company_profiles"] = self.download_fmp_company_profiles()
        results["prices"] = self.download_fmp_daily_prices(start_year)
        results["historical_market_cap"] = self.download_fmp_historical_market_cap()
        results["financial_quarterly"] = self.download_fmp_financial_quarterly()
        results["key_metrics"] = self.download_fmp_key_metrics()
        results["ratios"] = self.download_fmp_ratios()
        results["financial_growth"] = self.download_fmp_financial_growth()
        results["enterprise_values"] = self.download_fmp_enterprise_values()
        results["owner_earnings"] = self.download_fmp_owner_earnings()

        # Phase 3: Per-ticker 辅助数据
        logger.info("=== Phase 3: Per-ticker 辅助数据 ===")
        results["earnings"] = self.download_fmp_earnings_surprises()
        results["estimates"] = self.download_fmp_eps_estimates()
        results["insider"] = self.download_fmp_insider_trading()
        results["insider_stats"] = self.download_fmp_insider_statistics()
        results["analyst_grades"] = self.download_fmp_analyst_grades()
        results["dividends"] = self.download_fmp_dividends_splits()
        results["esg"] = self.download_fmp_esg_ratings()
        results["employee"] = self.download_fmp_employee_count()

        # Phase 4: 指数/商品/宏观
        logger.info("=== Phase 4: 指数/商品/宏观 ===")
        results["index_daily"] = self.download_fmp_index_daily(start_year)
        results["index_history"] = self.download_fmp_index_constituents_history()
        results["commodities"] = self.download_fmp_commodity_prices(start_year)
        results["macro"] = self.download_fmp_macro()
        results["congress"] = self.download_fmp_congress_trading()

        # 快照数据（仅增量更新时跑）
        # shares_float, financial_scores, price_targets, dcf, peers

        return results
