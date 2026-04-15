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
    QUIVER_API_KEY, QUIVER_RATE_LIMIT,
    LOG_LEVEL,
)
from data.models import (
    USStockBasic, USDailyPrice, USFinancialData, USKeyMetric,
    USIndustryClass, USEarningsSurprise, USEpsEstimate,
    USInsiderTrade, USAnalystRecommendation, USCorporateAction,
    USIndexDaily, USCommodityPrice, USMacroIndicator,
    USCompanyProfile, USSharesFloat,
    USFinancialScore, USFinancialGrowth, USEnterpriseValue,
    USOwnerEarnings, USDCFValuation,
    USStockPeer, USESGRating, USPriceTarget, USInsiderStatistic,
    USEmployeeCount, USIndexConstituent, USSymbolChange, USDelisted,
    USCongressTrade,
    USLobbying, USGovContract,
)
from data.upsert import get_upsert_manager

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

    _WORKERS_BULK = 10
    _WORKERS_INCREMENTAL = 15

    def __init__(self, db=None, incremental: bool = False, **kwargs):
        self._um = get_upsert_manager()
        self._incremental = incremental
        self._workers = self._WORKERS_INCREMENTAL if incremental else self._WORKERS_BULK
        self._fmp_limiter = RateLimiter(FMP_RATE_LIMIT)
        self._quiver_limiter = RateLimiter(QUIVER_RATE_LIMIT)

    def _get_tickers(self, stocks_only: bool = False) -> list[str]:
        """获取美股代码列表（Django ORM）。"""
        qs = USStockBasic.objects.filter(is_actively_trading=1)
        if stocks_only:
            qs = qs.filter(is_etf=0, is_fund=0)
        return list(qs.values_list("ticker", flat=True))

    def _get_ticker_latest(self, model, date_field: str = "date") -> dict[str, str]:
        """
        获取每个 ticker 在指定表中的最新日期。

        时序表（date_field='date'/'trade_date'/'transaction_date'）：返回最新业务日期。
        快照表（date_field='updated_at'）：返回最新更新时间。

        Returns:
            {ticker: 'YYYY-MM-DD'} 字典
        """
        from django.db.models import Max
        qs = model.objects.values("ticker").annotate(latest=Max(date_field))
        return {
            row["ticker"]: str(row["latest"])[:10]
            for row in qs
            if row["latest"] is not None
        }

    def _ticker_needs_update(self, ticker: str, latest_map: dict, stale_days: int = 1) -> str | None:
        """
        判断 ticker 是否需要更新，返回拉取起始日期。

        Args:
            ticker: 股票代码
            latest_map: _get_ticker_latest 返回的字典
            stale_days: 数据超过多少天算过期（默认 1 天）

        Returns:
            需要拉取的起始日期字符串，None 表示不需要更新。
        """
        latest = latest_map.get(ticker)
        if not latest:
            return "1995-01-01"  # 无数据，全量拉
        from_date = (pd.to_datetime(latest) + timedelta(days=1)).strftime("%Y-%m-%d")
        today = datetime.now().strftime("%Y-%m-%d")
        cutoff = (datetime.now() - timedelta(days=stale_days)).strftime("%Y-%m-%d")
        if latest >= cutoff:
            return None  # 数据足够新
        return from_date

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

    # --- Quiver HTTP helper ---

    def _quiver_get_json(self, path: str, params: dict = None) -> list | dict:
        if not QUIVER_API_KEY:
            logger.warning("QUIVER_API_KEY 未设置")
            return []
        self._quiver_limiter.wait()
        url = f"https://api.quiverquant.com/beta/{path}"
        headers = {"Authorization": f"Bearer {QUIVER_API_KEY}", "Accept": "application/json"}
        resp = _request_with_retry("GET", url, headers=headers, params=params)
        if resp.status_code == 404:
            return []  # 该 ticker 无数据
        if resp.status_code != 200:
            logger.warning(f"Quiver {path}: HTTP {resp.status_code}")
            return []
        try:
            data = resp.json()
        except ValueError:
            logger.warning(f"Quiver {path}: 非 JSON 响应")
            return []
        return data if isinstance(data, list) else []

    # --- 断点续跑 ---

    def _skip_done_tickers(self, table: str, tickers: list[str]) -> list[str]:
        if self._incremental:
            logger.info(f"增量模式 {table}: 跳过断点检查, {len(tickers)} tickers")
            return tickers
        done = self._um.get_import_done_tickers(table)
        remaining = [t for t in tickers if t not in done]
        if done:
            logger.info(f"断点续跑 {table}: 全部 {len(tickers)}, 已完成 {len(done)}, 待跑 {len(remaining)}")
        return remaining

    def _skip_if_table_has_data(self, table: str, min_rows: int = 10) -> bool:
        if self._incremental:
            return False
        from django.db import connection
        try:
            with connection.cursor() as cursor:
                cursor.execute(f'SELECT COUNT(*) FROM "{table}"')
                cnt = cursor.fetchone()[0]
                if cnt >= min_rows:
                    logger.info(f"{table} 已有 {cnt} 条数据（>={min_rows}），跳过")
                    return True
        except Exception:
            pass
        return False

    def _mark_done(self, table: str, fetch_fn, ticker: str):
        """包装 fetch：无论有无数据都标记完成，避免重跑浪费 API。"""
        count = fetch_fn(ticker)
        self._um.mark_import_done(table, ticker)
        return count

    # ETF 在这些表中确认无数据（FMP 端点对 ETF 返回空）—— 预标记跳过，节省 API
    _ETF_SKIP_TABLES = [
        "us_owner_earnings",
        "us_employee_count",
        "us_enterprise_value",
        "us_financial_growth",
        "us_esg_rating",
        "us_insider_statistic",
    ]

    def _premark_etfs_no_data(self) -> int:
        """预标记 ETF 为 done — 这些表 ETF 无数据，跳过避免浪费 API 调用。"""
        from datetime import datetime as dt
        etf_tickers = list(
            USStockBasic.objects.filter(is_etf=1).values_list("ticker", flat=True)
        )
        if not etf_tickers:
            logger.info("Phase 0: 无 ETF，跳过预标记")
            return 0
        total = 0
        from data.models import ImportProgress
        for tbl in self._ETF_SKIP_TABLES:
            existing = self._um.get_import_done_tickers(tbl)
            new_tickers = [t for t in etf_tickers if t not in existing]
            if new_tickers:
                ImportProgress.objects.bulk_create(
                    [ImportProgress(table_name=tbl, ticker=t, completed_at=dt.now()) for t in new_tickers],
                    batch_size=2000,
                    ignore_conflicts=True,
                )
                total += len(new_tickers)
                logger.info(f"Phase 0: {tbl} 预标记 ETF +{len(new_tickers)}")
        logger.info(f"Phase 0: 预标记完成，共写入 {total} 条 import_progress")
        return total

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
        self._um.upsert_df(USStockBasic, df, ["ticker"])

        # 同时写 industry_class
        if "sector" in df.columns:
            ind_df = df[["ticker", "sector", "industry"]].dropna(subset=["sector"])
            if not ind_df.empty:
                self._um.upsert_df(USIndustryClass, ind_df, ["ticker"])

        logger.info(f"FMP 全市场股票列表: {len(df)} 只")
        return len(df)

    # --- 2. company_profiles ---
    # NOTE: 不做 _skip_if_table_has_data 检查——profile 是快照数据，每次全量覆盖更新
    def download_fmp_company_profiles(self, tickers: list[str] = None) -> int:
        if tickers is None:
            tickers = self._get_tickers(stocks_only=False)
        if not tickers:
            return 0
        total = 0

        # FIX: 用 stable/profile（DB 列按 stable 字段命名 marketCap/volume 等），v3 profile/{ticker} 字段名是 mktCap/volAvg（不匹配）
        # stable 不支持 batch，per-ticker 调用
        def _fetch(ticker):
            data = self._fmp_get_stable("profile", params={"symbol": ticker})
            if not data:
                return 0
            df = _fmp_df_to_snake(pd.DataFrame(data))
            self._um.upsert_df(USCompanyProfile, df, ["ticker"])
            return len(df)

        with ThreadPoolExecutor(max_workers=self._workers) as pool:
            futures = {pool.submit(_fetch, t): t for t in tickers}
            for f in tqdm(as_completed(futures), total=len(futures), desc="FMP Company Profiles"):
                try:
                    total += f.result()
                except Exception as e:
                    logger.warning(f"Profile 失败 {futures[f]}: {e}")
        logger.info(f"FMP company profiles 总计: {total} 条")
        return total

    # --- 3. daily_price ---
    def download_fmp_daily_prices(self, start_year: int = 1995, incremental: bool = False) -> int:
        tickers = self._get_tickers(stocks_only=False)
        if not tickers:
            return 0

        today = datetime.now().strftime("%Y-%m-%d")

        if self._incremental or incremental:
            latest_map = self._get_ticker_latest(USDailyPrice, "trade_date")
            logger.info(f"增量更新 us_daily_price: {len(tickers)} tickers, DB 已有 {len(latest_map)} tickers 有数据")
        else:
            tickers = self._skip_done_tickers("us_daily_price", tickers)
            if not tickers:
                return 0
            latest_map = None

        total = 0

        def _fetch(ticker):
            count = 0
            if latest_map is not None:
                from_date = self._ticker_needs_update(ticker, latest_map)
                if from_date is None:
                    return 0
                segments = [(from_date, today)]
            else:
                end_year = int(today[:4])
                segments = []
                for yr in range(start_year, end_year + 1, 10):
                    seg_end = min(yr + 9, end_year)
                    segments.append((f"{yr}-01-01", f"{seg_end}-12-31"))

            for seg_start, seg_end in segments:
                data = self._fmp_get_stable(
                    "historical-price-eod/full",
                    params={"symbol": ticker, "from": seg_start, "to": seg_end},
                )
                if not data:
                    continue
                df = _fmp_df_to_snake(pd.DataFrame(data))
                if "date" in df.columns:
                    df["trade_date"] = pd.to_datetime(df["date"]).dt.date
                    df = df.drop(columns=["date"])
                df = df[df["trade_date"].notna()]
                if not df.empty:
                    self._um.upsert_df(USDailyPrice, df, ["ticker", "trade_date"])
                    count += len(df)
            return count

        with ThreadPoolExecutor(max_workers=self._workers) as pool:
            futures = {pool.submit(self._mark_done, "us_daily_price", _fetch, t): t for t in tickers}
            for f in tqdm(as_completed(futures), total=len(futures), desc="FMP Daily Prices"):
                try:
                    total += f.result()
                except Exception as e:
                    logger.warning(f"Price 失败 {futures[f]}: {e}")
        logger.info(f"FMP daily prices 总计: {total} 条")
        return total

    # --- 4. historical_market_cap (DEPRECATED) ---
    # 该端点 Ultimate plan 只返回最近 ~90 天，from/to 参数需要 Enterprise plan（402）。
    # 历史市值数据已切换到 us_enterprise_value.market_capitalization
    # （季度精度，1983-至今全历史，由 download_fmp_enterprise_values 写入）。
    def download_fmp_historical_market_cap(self, tickers: list[str] = None, limit: int = 5000) -> int:
        logger.info("download_fmp_historical_market_cap: 已废弃，数据源切换到 us_enterprise_value.market_capitalization")
        return 0

    # --- 5. financial_quarterly (IS+BS+CF 三表合并) ---
    def download_fmp_financial_quarterly(self, tickers: list[str] = None, limit: int = 400) -> int:
        if tickers is None:
            tickers = self._get_tickers(stocks_only=False)
        if not tickers:
            return 0
        latest_map = None
        if self._incremental:
            latest_map = self._get_ticker_latest(USFinancialData, "date")
            logger.info(f"增量更新 us_financial_data: {len(tickers)} tickers, DB 已有 {len(latest_map)} tickers 有数据")
        else:
            tickers = self._skip_done_tickers("us_financial_data", tickers)
            if not tickers:
                return 0
        total = 0

        def _fetch(ticker):
            if latest_map is not None and self._ticker_needs_update(ticker, latest_map, stale_days=30) is None:
                return 0
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

            self._um.upsert_df(USFinancialData, merged, ["ticker", "period"])
            return len(merged)

        with ThreadPoolExecutor(max_workers=self._workers) as pool:
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
            tickers = self._get_tickers(stocks_only=False)
        if not tickers:
            return 0
        latest_map = None
        if self._incremental:
            latest_map = self._get_ticker_latest(USKeyMetric, "date")
            logger.info(f"增量更新 us_key_metric: {len(tickers)} tickers, DB 已有 {len(latest_map)} tickers 有数据")
        else:
            tickers = self._skip_done_tickers("us_key_metric", tickers)
            if not tickers:
                return 0
        total = 0

        def _fetch(ticker):
            if latest_map is not None and self._ticker_needs_update(ticker, latest_map, stale_days=30) is None:
                return 0
            data = self._fmp_get_stable("key-metrics", params={"symbol": ticker, "period": "quarter", "limit": limit})
            if not data:
                return 0
            df = _fmp_df_to_snake(pd.DataFrame(data))
            self._um.upsert_df(USKeyMetric, df, ["ticker", "date"])
            return len(df)

        with ThreadPoolExecutor(max_workers=self._workers) as pool:
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
            tickers = self._get_tickers(stocks_only=False)
        if not tickers:
            return 0
        latest_map = self._get_ticker_latest(USKeyMetric, "date") if self._incremental else None
        total = 0

        def _fetch(ticker):
            if latest_map is not None and self._ticker_needs_update(ticker, latest_map, stale_days=30) is None:
                return 0
            data = self._fmp_get_stable("ratios", params={"symbol": ticker, "period": "quarter", "limit": limit})
            if not data:
                return 0
            df = _fmp_df_to_snake(pd.DataFrame(data))
            self._um.upsert_df(USKeyMetric, df, ["ticker", "date"])
            return len(df)

        with ThreadPoolExecutor(max_workers=self._workers) as pool:
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
            tickers = self._get_tickers(stocks_only=True)
        if not tickers:
            return 0
        latest_map = None
        if self._incremental:
            latest_map = self._get_ticker_latest(USFinancialGrowth, "date")
            logger.info(f"增量更新 us_financial_growth: {len(tickers)} tickers, DB 已有 {len(latest_map)} tickers 有数据")
        else:
            tickers = self._skip_done_tickers("us_financial_growth", tickers)
            if not tickers:
                return 0
        total = 0

        def _fetch(ticker):
            if latest_map is not None and self._ticker_needs_update(ticker, latest_map, stale_days=30) is None:
                return 0
            data = self._fmp_get_stable("financial-growth", params={"symbol": ticker, "period": "quarter", "limit": limit})
            if not data:
                return 0
            df = _fmp_df_to_snake(pd.DataFrame(data))
            self._um.upsert_df(USFinancialGrowth, df, ["ticker", "date"])
            return len(df)

        with ThreadPoolExecutor(max_workers=self._workers) as pool:
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
            tickers = self._get_tickers(stocks_only=True)
        if not tickers:
            return 0
        latest_map = None
        if self._incremental:
            latest_map = self._get_ticker_latest(USEnterpriseValue, "date")
            logger.info(f"增量更新 us_enterprise_value: {len(tickers)} tickers, DB 已有 {len(latest_map)} tickers 有数据")
        else:
            tickers = self._skip_done_tickers("us_enterprise_value", tickers)
            if not tickers:
                return 0
        total = 0

        def _fetch(ticker):
            if latest_map is not None and self._ticker_needs_update(ticker, latest_map, stale_days=30) is None:
                return 0
            data = self._fmp_get_stable("enterprise-values", params={"symbol": ticker, "period": "quarter", "limit": limit})
            if not data:
                return 0
            df = _fmp_df_to_snake(pd.DataFrame(data))
            self._um.upsert_df(USEnterpriseValue, df, ["ticker", "date"])
            return len(df)

        with ThreadPoolExecutor(max_workers=self._workers) as pool:
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
            tickers = self._get_tickers(stocks_only=True)
        if not tickers:
            return 0
        latest_map = None
        if self._incremental:
            latest_map = self._get_ticker_latest(USOwnerEarnings, "date")
            logger.info(f"增量更新 us_owner_earnings: {len(tickers)} tickers, DB 已有 {len(latest_map)} tickers 有数据")
        else:
            tickers = self._skip_done_tickers("us_owner_earnings", tickers)
            if not tickers:
                return 0
        total = 0

        def _fetch(ticker):
            if latest_map is not None and self._ticker_needs_update(ticker, latest_map, stale_days=30) is None:
                return 0
            data = self._fmp_get_stable("owner-earnings", params={"symbol": ticker})
            if not data:
                return 0
            df = _fmp_df_to_snake(pd.DataFrame(data if isinstance(data, list) else [data]))
            self._um.upsert_df(USOwnerEarnings, df, ["ticker", "date"])
            return len(df)

        with ThreadPoolExecutor(max_workers=self._workers) as pool:
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
            tickers = self._get_tickers(stocks_only=False)
        if not tickers:
            return 0
        latest_map = None
        if self._incremental:
            latest_map = self._get_ticker_latest(USEarningsSurprise, "date")
            logger.info(f"增量更新 us_earnings_surprise: {len(tickers)} tickers, DB 已有 {len(latest_map)} tickers 有数据")
        else:
            tickers = self._skip_done_tickers("us_earnings_surprise", tickers)
            if not tickers:
                return 0
        total = 0

        def _fetch(ticker):
            if latest_map is not None and self._ticker_needs_update(ticker, latest_map, stale_days=7) is None:
                return 0
            # FIX: 用 stable/earnings 端点，v3 earnings-surprises 字段名是 actualEarningResult/estimatedEarning（与 DB 列不匹配）
            data = self._fmp_get_stable("earnings", params={"symbol": ticker, "limit": 400})
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
            self._um.upsert_df(USEarningsSurprise, df, ["ticker", "date"])
            return len(df)

        with ThreadPoolExecutor(max_workers=self._workers) as pool:
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
            tickers = self._get_tickers(stocks_only=False)
        if not tickers:
            return 0
        latest_map = None
        if self._incremental:
            latest_map = self._get_ticker_latest(USEpsEstimate, "date")
            logger.info(f"增量更新 us_eps_estimate: {len(tickers)} tickers, DB 已有 {len(latest_map)} tickers 有数据")
        else:
            tickers = self._skip_done_tickers("us_eps_estimate", tickers)
            if not tickers:
                return 0
        total = 0

        def _fetch(ticker):
            if latest_map is not None and self._ticker_needs_update(ticker, latest_map, stale_days=7) is None:
                return 0
            data = self._fmp_get_json(f"analyst-estimates/{ticker}", params={"period": "quarter", "limit": 200})
            if not data:
                return 0
            df = _fmp_df_to_snake(pd.DataFrame(data))
            self._um.upsert_df(USEpsEstimate, df, ["ticker", "date"])
            return len(df)

        with ThreadPoolExecutor(max_workers=self._workers) as pool:
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
            tickers = self._get_tickers(stocks_only=False)
        if not tickers:
            return 0
        latest_map = None
        if self._incremental:
            latest_map = self._get_ticker_latest(USInsiderTrade, "transaction_date")
            logger.info(f"增量更新 us_insider_trade: {len(tickers)} tickers, DB 已有 {len(latest_map)} tickers 有数据")
        else:
            tickers = self._skip_done_tickers("us_insider_trade", tickers)
            if not tickers:
                return 0
        total = 0

        def _fetch(ticker):
            if latest_map is not None and self._ticker_needs_update(ticker, latest_map, stale_days=7) is None:
                return 0
            count = 0
            page = 0
            while True:
                data = self._fmp_get_json("insider-trading", params={"symbol": ticker, "page": page, "limit": 100}, version="v4")
                if not data:
                    break
                df = _fmp_df_to_snake(pd.DataFrame(data))
                self._um.upsert_df(USInsiderTrade, df, ["ticker", "transaction_date", "reporting_name", "transaction_type"])
                count += len(df)
                if len(data) < 100:
                    break
                page += 1
            return count

        with ThreadPoolExecutor(max_workers=self._workers) as pool:
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
            tickers = self._get_tickers(stocks_only=False)
        if not tickers:
            return 0
        latest_map = None
        if self._incremental:
            latest_map = self._get_ticker_latest(USAnalystRecommendation, "date")
            logger.info(f"增量更新 us_analyst_recommendation: {len(tickers)} tickers, DB 已有 {len(latest_map)} tickers 有数据")
        else:
            tickers = self._skip_done_tickers("us_analyst_recommendation", tickers)
            if not tickers:
                return 0
        total = 0

        def _fetch(ticker):
            if latest_map is not None and self._ticker_needs_update(ticker, latest_map, stale_days=7) is None:
                return 0
            data = self._fmp_get_json(f"grade/{ticker}")
            if not data:
                return 0
            df = _fmp_df_to_snake(pd.DataFrame(data))
            df = df[df["date"].notna() & df["new_grade"].notna()]
            if df.empty:
                return 0
            self._um.upsert_df(USAnalystRecommendation, df, ["ticker", "date", "grading_company"])
            return len(df)

        with ThreadPoolExecutor(max_workers=self._workers) as pool:
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
            tickers = self._get_tickers(stocks_only=False)
        if not tickers:
            return 0
        latest_map = None
        if self._incremental:
            latest_map = self._get_ticker_latest(USCorporateAction, "date")
            logger.info(f"增量更新 us_corporate_action: {len(tickers)} tickers, DB 已有 {len(latest_map)} tickers 有数据")
        else:
            tickers = self._skip_done_tickers("us_corporate_action", tickers)
            if not tickers:
                return 0
        total = 0

        def _fetch(ticker):
            if latest_map is not None and self._ticker_needs_update(ticker, latest_map, stale_days=30) is None:
                return 0
            count = 0
            # Dividends
            div_data = self._fmp_get_stable("dividends", params={"symbol": ticker})
            if div_data and isinstance(div_data, list):
                df = _fmp_df_to_snake(pd.DataFrame(div_data))
                df["action_type"] = "dividend"
                self._um.upsert_df(USCorporateAction, df, ["ticker", "date", "action_type"])
                count += len(df)
            # Splits
            split_data = self._fmp_get_stable("splits", params={"symbol": ticker})
            if split_data and isinstance(split_data, list):
                df = _fmp_df_to_snake(pd.DataFrame(split_data))
                df["action_type"] = "split"
                self._um.upsert_df(USCorporateAction, df, ["ticker", "date", "action_type"])
                count += len(df)
            return count

        with ThreadPoolExecutor(max_workers=self._workers) as pool:
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
            tickers = self._get_tickers(stocks_only=True)
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
            self._um.upsert_df(USFinancialScore, df, ["ticker"])
            return len(df)

        with ThreadPoolExecutor(max_workers=self._workers) as pool:
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
            tickers = self._get_tickers(stocks_only=True)
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
            self._um.upsert_df(USSharesFloat, df, ["ticker", "date"])
            return len(df)

        with ThreadPoolExecutor(max_workers=self._workers) as pool:
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
            tickers = self._get_tickers(stocks_only=True)
        if not tickers:
            return 0
        # 用 updated_at 做增量判断（表按 year+quarter 分，无单一 date 字段）
        latest_map = None
        if self._incremental:
            latest_map = self._get_ticker_latest(USInsiderStatistic, "updated_at")
            logger.info(f"增量更新 us_insider_statistic: {len(tickers)} tickers, DB 已有 {len(latest_map)} tickers 有数据")
        else:
            tickers = self._skip_done_tickers("us_insider_statistic", tickers)
            if not tickers:
                return 0
        total = 0

        def _fetch(ticker):
            if latest_map is not None and self._ticker_needs_update(ticker, latest_map, stale_days=30) is None:
                return 0
            data = self._fmp_get_stable("insider-trading/statistics", params={"symbol": ticker})
            if not data:
                return 0
            df = _fmp_df_to_snake(pd.DataFrame(data if isinstance(data, list) else [data]))
            self._um.upsert_df(USInsiderStatistic, df, ["ticker", "year", "quarter"])
            return len(df)

        with ThreadPoolExecutor(max_workers=self._workers) as pool:
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
            tickers = self._get_tickers(stocks_only=True)
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
            self._um.upsert_df(USEmployeeCount, df, ["ticker", "period_of_report"])
            return len(df)

        with ThreadPoolExecutor(max_workers=self._workers) as pool:
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
            tickers = self._get_tickers(stocks_only=True)
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
            self._um.upsert_df(USPriceTarget, df, ["ticker"])
            return len(df)

        with ThreadPoolExecutor(max_workers=self._workers) as pool:
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
            tickers = self._get_tickers(stocks_only=True)
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
            self._um.upsert_df(USESGRating, df, ["ticker", "fiscal_year"])
            return len(df)

        with ThreadPoolExecutor(max_workers=self._workers) as pool:
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
            tickers = self._get_tickers(stocks_only=True)
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
            self._um.upsert_df(USDCFValuation, df, ["ticker", "date", "dcf_type"])
            return len(df)

        with ThreadPoolExecutor(max_workers=self._workers) as pool:
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
            tickers = self._get_tickers(stocks_only=True)
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
            self._um.upsert_df(USStockPeer, df, ["ticker", "peer_ticker"])
            return len(df)

        with ThreadPoolExecutor(max_workers=self._workers) as pool:
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
                    self._um.upsert_df(USIndexDaily, df, ["index_code", "trade_date"])
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
                    self._um.upsert_df(USCommodityPrice, df, ["commodity_symbol", "trade_date"])
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
                self._um.upsert_df(USMacroIndicator, df, ["indicator_code", "report_date"])
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
                        self._um.upsert_df(USMacroIndicator, df, ["indicator_code", "report_date"])
                        total += 1
                y10, y2 = item.get("year10"), item.get("year2")
                if y10 is not None and y2 is not None:
                    df = pd.DataFrame([{"indicator_code": "US_2Y10Y", "report_date": date, "value": y10 - y2}])
                    self._um.upsert_df(USMacroIndicator, df, ["indicator_code", "report_date"])
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
            self._um.upsert_df(USIndexConstituent, df, ["index_name", "ticker", "date"])
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
        self._um.upsert_df(USDelisted, df, ["ticker"])
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
        self._um.upsert_df(USSymbolChange, df, ["old_symbol", "new_symbol", "date"])
        logger.info(f"FMP symbol changes 总计: {len(df)} 条")
        return len(df)

    # --- 30. congress_trading (per-ticker, /stable/senate-trades + /stable/house-trades) ---
    def download_fmp_congress_trading(self, tickers: list[str] = None) -> int:
        if tickers is None:
            tickers = self._get_tickers(stocks_only=True)
        if not tickers:
            return 0
        latest_map = None
        if self._incremental:
            latest_map = self._get_ticker_latest(USCongressTrade, "transaction_date")
            logger.info(f"增量更新 us_congress_trade: {len(tickers)} tickers, DB 已有 {len(latest_map)} tickers 有数据")
        else:
            tickers = self._skip_done_tickers("us_congress_trade", tickers)
            if not tickers:
                return 0
        total = 0

        def _fetch(ticker):
            if latest_map is not None and self._ticker_needs_update(ticker, latest_map, stale_days=7) is None:
                return 0
            count = 0
            for chamber, endpoint in [("senate", "senate-trades"), ("house", "house-trades")]:
                data = self._fmp_get_stable(endpoint, params={"symbol": ticker})
                if not data:
                    continue
                df = _fmp_df_to_snake(pd.DataFrame(data))
                df["source"] = f"fmp_{chamber}"
                self._um.upsert_df(USCongressTrade, df, ["ticker", "transaction_date", "first_name", "last_name", "type"])
                count += len(df)
            return count

        with ThreadPoolExecutor(max_workers=self._workers) as pool:
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

    # ============================================================
    # Quiver 下载方法
    # ============================================================

    def download_quiver_lobbying(self, tickers: list[str] = None) -> int:
        """Quiver lobbying: per-ticker 历史游说记录"""
        if tickers is None:
            tickers = self._get_tickers(stocks_only=True)
        if not tickers:
            logger.warning("download_quiver_lobbying: 无 ticker 列表，跳过")
            return 0
        latest_map = None
        if self._incremental:
            latest_map = self._get_ticker_latest(USLobbying, "date")
            logger.info(f"增量更新 us_lobbying: {len(tickers)} tickers, DB 已有 {len(latest_map)} tickers 有数据")
        else:
            tickers = self._skip_done_tickers("us_lobbying", tickers)
            if not tickers:
                logger.info("download_quiver_lobbying: 全部 ticker 已完成，跳过")
                return 0
        total = 0

        def _fetch(ticker):
            if latest_map is not None and self._ticker_needs_update(ticker, latest_map, stale_days=30) is None:
                return 0
            data = self._quiver_get_json(f"historical/lobbying/{ticker}")
            if not data:
                return 0
            df = pd.DataFrame(data)
            # 字段重命名: API 返回 PascalCase → DB snake_case
            df = df.rename(columns={
                "Date": "date", "Amount": "amount", "Client": "client",
                "Issue": "issue", "Specific_Issue": "specific_issue",
                "Registrant": "registrant", "Ticker": "ticker",
            })
            df = df[df["date"].notna() & df["ticker"].notna()]
            if df.empty:
                return 0
            # 转类型
            df["date"] = pd.to_datetime(df["date"], errors="coerce").dt.date
            df["amount"] = pd.to_numeric(df["amount"], errors="coerce")
            df = df[df["date"].notna()]
            # 去重：同 (ticker, date, registrant, client) 只留第一条（保留 amount 最大者）
            df = df.sort_values("amount", ascending=False, na_position="last")
            df = df.drop_duplicates(subset=["ticker", "date", "registrant", "client"], keep="first")
            if df.empty:
                return 0
            self._um.upsert_df(USLobbying, df, ["ticker", "date", "registrant", "client"])
            return len(df)

        with ThreadPoolExecutor(max_workers=self._workers) as pool:
            futures = {pool.submit(self._mark_done, "us_lobbying", _fetch, t): t for t in tickers}
            for f in tqdm(as_completed(futures), total=len(futures), desc="Quiver Lobbying"):
                try:
                    total += f.result()
                except Exception as e:
                    logger.warning(f"Lobbying 失败 {futures[f]}: {e}")
        logger.info(f"Quiver lobbying 总计: {total} 条")
        return total

    def download_quiver_gov_contracts(self, tickers: list[str] = None) -> int:
        """Quiver gov contracts: per-ticker 季度政府合同金额"""
        if tickers is None:
            tickers = self._get_tickers(stocks_only=True)
        if not tickers:
            logger.warning("download_quiver_gov_contracts: 无 ticker 列表，跳过")
            return 0
        latest_map = None
        if self._incremental:
            latest_map = self._get_ticker_latest(USGovContract, "updated_at")
            logger.info(f"增量更新 us_gov_contract: {len(tickers)} tickers, DB 已有 {len(latest_map)} tickers 有数据")
        else:
            tickers = self._skip_done_tickers("us_gov_contract", tickers)
            if not tickers:
                logger.info("download_quiver_gov_contracts: 全部 ticker 已完成，跳过")
                return 0
        total = 0

        def _fetch(ticker):
            if latest_map is not None and self._ticker_needs_update(ticker, latest_map, stale_days=30) is None:
                return 0
            data = self._quiver_get_json(f"historical/govcontracts/{ticker}")
            if not data:
                return 0
            df = pd.DataFrame(data)
            df = df.rename(columns={
                "Ticker": "ticker", "Amount": "amount",
                "Qtr": "quarter", "Year": "year",
            })
            df = df[df["ticker"].notna() & df["year"].notna() & df["quarter"].notna()]
            if df.empty:
                return 0
            df["amount"] = pd.to_numeric(df["amount"], errors="coerce")
            df["year"] = pd.to_numeric(df["year"], errors="coerce").astype("Int64")
            df["quarter"] = pd.to_numeric(df["quarter"], errors="coerce").astype("Int64")
            df = df.dropna(subset=["year", "quarter"])
            if df.empty:
                return 0
            self._um.upsert_df(USGovContract, df, ["ticker", "year", "quarter"])
            return len(df)

        with ThreadPoolExecutor(max_workers=self._workers) as pool:
            futures = {pool.submit(self._mark_done, "us_gov_contract", _fetch, t): t for t in tickers}
            for f in tqdm(as_completed(futures), total=len(futures), desc="Quiver GovContracts"):
                try:
                    total += f.result()
                except Exception as e:
                    logger.warning(f"GovContract 失败 {futures[f]}: {e}")
        logger.info(f"Quiver gov_contracts 总计: {total} 条")
        return total

    def download_quiver_all(self) -> dict:
        """Quiver 全量下载"""
        results = {}
        logger.info("=== Quiver: lobbying ===")
        results["lobbying"] = self.download_quiver_lobbying()
        logger.info("=== Quiver: gov_contracts ===")
        results["gov_contracts"] = self.download_quiver_gov_contracts()
        return results

    def download_fmp_all(self, start_year: int = 1995) -> dict:
        """FMP 全量下载"""
        results = {}

        # Phase 1: Bulk 端点（含 stock_list，必须先跑以拿到 ticker 列表）
        logger.info("=== Phase 1: Bulk 端点 ===")
        results["stock_list"] = self.download_fmp_stock_list()
        results["delisted"] = self.download_fmp_delisted_companies()
        results["symbol_changes"] = self.download_fmp_symbol_changes()

        # Phase 1.5: ETF 预标记（必须在 stock_list 之后，per-ticker 之前）
        logger.info("=== Phase 1.5: ETF 预标记（跳过无数据端点）===")
        results["etf_premark"] = self._premark_etfs_no_data()

        # Phase 2: Per-ticker 核心数据（有历史序列）
        logger.info("=== Phase 2: Per-ticker 核心数据 ===")
        results["company_profiles"] = self.download_fmp_company_profiles()
        results["prices"] = self.download_fmp_daily_prices(start_year)
        # historical_market_cap 已废弃（us_enterprise_value 已有全历史季度市值）
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
