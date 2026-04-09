"""
四家 API 统一批量下载器

数据源：
    - FMP (Financial Modeling Prep) — 财报/metrics/earnings/行情/insider
    - Unusual Whales — 期权 flow/暗池/国会交易/新闻
    - Fiscal.ai — 日频估值比率/业务分部

原则：API 返回什么就存什么，不做字段过滤。
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
    UW_API_KEY, UW_RATE_LIMIT,
    FISCAL_API_KEY, FISCAL_RATE_LIMIT,
    LOG_LEVEL,
)
from services.data.database import DatabaseManager

logger = logging.getLogger(__name__)
logger.setLevel(LOG_LEVEL)


# ============================================================
# 通用 camelCase → snake_case 转换器
# ============================================================

# FMP 常见缩写，转换时保持为整体（不拆成单字母）
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

# symbol → ticker 固定映射
_FMP_RENAMES = {"symbol": "ticker"}


def _camel_to_snake(name: str) -> str:
    """Convert camelCase/PascalCase to snake_case.

    Handles FMP abbreviations correctly:
      evToEBITDA → ev_to_ebitda (not ev_to_e_b_i_t_d_a)
      epsDiluted → eps_diluted
      returnOnEquity → return_on_equity
    """
    # 固定重命名
    if name in _FMP_RENAMES:
        return _FMP_RENAMES[name]

    # 处理连续大写缩写：在缩写和后续小写之间插入下划线
    # "evToEBITDA" → "evTo_EBITDA" → 后续处理
    # "netIncomePerEBT" → "netIncomePer_EBT"
    result = name

    # 1. 将已知缩写替换为 _缩写_ 形式（带边界标记）
    for abbr in sorted(_ABBREVIATIONS.keys(), key=len, reverse=True):
        # 匹配缩写在字符串中的位置
        idx = result.find(abbr)
        while idx != -1:
            before = result[idx - 1] if idx > 0 else ""
            after = result[idx + len(abbr):idx + len(abbr) + 1] if idx + len(abbr) < len(result) else ""
            # 在缩写前后加下划线（如果相邻是小写字母）
            prefix = "_" if before and before.islower() else ""
            suffix = "_" if after and after.islower() else ""
            replacement = prefix + _ABBREVIATIONS[abbr] + suffix
            result = result[:idx] + replacement + result[idx + len(abbr):]
            idx = result.find(abbr)

    # 2. 标准 camelCase 拆分（处理剩余的大写字母）
    result = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", result)
    result = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", result)
    result = result.lower()

    # 3. 清理连续下划线
    result = re.sub(r"_+", "_", result).strip("_")
    return result


def _fmp_df_to_snake(df: pd.DataFrame) -> pd.DataFrame:
    """Convert all camelCase column names to snake_case."""
    col_map = {}
    for col in df.columns:
        new_name = _camel_to_snake(col)
        col_map[col] = new_name
    return df.rename(columns=col_map)


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


_429_WAITS = [5, 10, 20]  # 429 重试等待秒数


def _request_with_retry(method: str, url: str, max_retries: int = 3, **kwargs) -> requests.Response:
    """HTTP request with exponential backoff on 429/5xx."""
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
    # 断点续跑
    # ==============================================================

    def _skip_done_tickers(self, table: str, tickers: list[str],
                            ticker_col: str = "ticker") -> list[str]:
        """断点续跑：查 import_progress 表，排除已完成的 ticker。"""
        try:
            done = self.db.get_import_done_tickers(table)
            remaining = [t for t in tickers if t not in done]
            if done:
                logger.info(f"断点续跑 {table}: 全部 {len(tickers)}, "
                           f"已完成 {len(done)}, 待跑 {len(remaining)}")
            return remaining
        except Exception as e:
            logger.debug(f"_skip_done_tickers ({table}): {e}")
            return tickers

    def _wrap_fetch(self, table: str, fetch_fn, ticker: str):
        """包装 fetch 函数：成功后标记 import_progress。"""
        count = fetch_fn(ticker)
        if count > 0:
            self.db.mark_import_done(table, ticker)
        return count

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
        df = _fmp_df_to_snake(df)
        df = df.rename(columns={"exchange_short_name": "exchange", "company_name": "name"})
        df["is_active"] = 1
        df = df.drop_duplicates(subset=["ticker"])

        # 异步写入 us_stock_basic
        self.db.upsert_us_stock_basic(df)

        # 写入 industry classification
        if "sector" in df.columns:
            ind_df = df[["ticker", "sector", "industry"]].dropna(subset=["sector"])
            if not ind_df.empty:
                self.db.upsert_us_industry_class(ind_df)

        # 等待异步写入完成（后续端点依赖 ticker 列表）
        self.db.flush_writes()
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

    def download_fmp_earnings_surprises_bulk(self, tickers: list[str] = None, limit: int = 400) -> int:
        """FMP per-ticker: earnings surprises → us_earnings_surprise.

        /stable/earnings-surprises 端点，逐 ticker 下载。
        """
        if tickers is None:
            tickers = self.db.get_us_tickers()
        if not tickers:
            logger.warning("download_fmp_earnings_surprises_bulk: 无 ticker")
            return 0
        tickers = self._skip_done_tickers("us_earnings_surprise", tickers)
        if not tickers:
            logger.info("download_fmp_earnings_surprises_bulk: 所有 ticker 已完成")
            return 0

        total = 0

        def _fetch_single(ticker):
            data = self._fmp_get_json(
                f"earnings-surprises/{ticker}",
                params={"limit": limit},
            )
            if not data:
                logger.debug(f"earnings_surprises: {ticker} 无数据")
                return 0
            df = _fmp_df_to_snake(pd.DataFrame(data))
            df = df[df["ticker"].notna()].copy()
            # FMP returns epsActual/epsEstimated → eps_actual/eps_estimated; DB has actual_eps/estimated_eps
            df = df.rename(columns={"eps_actual": "actual_eps", "eps_estimated": "estimated_eps"})
            # Compute derived fields
            if "actual_eps" in df.columns and "estimated_eps" in df.columns:
                df["actual_eps"] = pd.to_numeric(df["actual_eps"], errors="coerce")
                df["estimated_eps"] = pd.to_numeric(df["estimated_eps"], errors="coerce")
                df["surprise"] = df["actual_eps"] - df["estimated_eps"]
                df["surprise_pct"] = df.apply(
                    lambda r: r["surprise"] / abs(r["estimated_eps"]) if pd.notna(r["estimated_eps"]) and r["estimated_eps"] != 0 else None,
                    axis=1,
                )
            if df.empty:
                logger.debug(f"earnings_surprises: {ticker} 转换后无有效数据")
                return 0
            self.db.upsert_us_earnings_surprise(df)
            return len(df)

        with ThreadPoolExecutor(max_workers=6) as pool:
            futures = {pool.submit(self._wrap_fetch, "us_earnings_surprise", _fetch_single, t): t for t in tickers}
            for future in tqdm(as_completed(futures), total=len(futures),
                               desc="FMP Earnings Surprises"):
                try:
                    total += future.result()
                except Exception as e:
                    logger.warning(f"Earnings surprise 失败 {futures[future]}: {e}")

        logger.info(f"FMP earnings surprises 总计: {total} 条")
        return total

    def download_fmp_eps_estimates_bulk(self, tickers: list[str] = None, limit: int = 200) -> int:
        """FMP per-ticker: analyst estimates (EPS consensus), 多线程。
        使用 /api/v3/analyst-estimates 端点。"""
        if tickers is None:
            tickers = self.db.get_us_tickers()
        if not tickers:
            logger.warning("download_fmp_eps_estimates_bulk: 无 ticker")
            return 0
        tickers = self._skip_done_tickers("us_eps_estimate", tickers)
        if not tickers:
            logger.info("download_fmp_eps_estimates_bulk: 所有 ticker 已完成")
            return 0

        total = 0

        def _fetch_estimates_single(ticker):
            data = self._fmp_get_json(
                f"analyst-estimates/{ticker}",
                params={"period": "quarter", "limit": limit},
            )
            if not data:
                logger.debug(f"eps_estimates: {ticker} 无数据")
                return 0
            df = _fmp_df_to_snake(pd.DataFrame(data))
            if df.empty or "ticker" not in df.columns:
                logger.debug(f"eps_estimates: {ticker} 转换后无有效数据")
                return 0
            # DB column names differ from FMP snake_case: rename to match DB
            df = df.rename(columns={
                "estimated_eps_avg": "eps_avg",
                "estimated_eps_low": "eps_low",
                "estimated_eps_high": "eps_high",
                "number_analysts_estimated_eps": "num_analysts",
                "estimated_revenue_avg": "revenue_avg",
                "estimated_net_income_avg": "net_income_avg",
            })
            self.db.upsert_us_eps_estimate(df)
            return len(df)

        with ThreadPoolExecutor(max_workers=6) as pool:
            futures = {pool.submit(self._wrap_fetch, "us_eps_estimate", _fetch_estimates_single, t): t for t in tickers}
            for future in tqdm(as_completed(futures), total=len(futures), desc="FMP EPS Estimates"):
                try:
                    total += future.result()
                except Exception as e:
                    logger.warning(f"EPS estimate 失败 {futures[future]}: {e}")

        logger.info(f"FMP EPS estimates 总计: {total} 条")
        return total

    def download_fmp_income_statement_bulk(self, tickers: list[str] = None, limit: int = 400) -> int:
        """FMP per-ticker: income statement (季度) → us_financial_data.

        使用 /stable/income-statement 端点逐 ticker 下载。
        """
        if tickers is None:
            tickers = self.db.get_us_tickers()
        if not tickers:
            logger.warning("download_fmp_income_statement_bulk: 无 ticker")
            return 0
        tickers = self._skip_done_tickers("us_financial_data", tickers)
        if not tickers:
            logger.info("download_fmp_income_statement_bulk: 所有 ticker 已完成")
            return 0

        total = 0

        def _fetch_single(ticker):
            data = self._fmp_get_stable_json(
                "income-statement",
                params={"symbol": ticker, "period": "quarter", "limit": limit},
            )
            if not data:
                logger.debug(f"income_statement: {ticker} 无数据")
                return 0
            df = _fmp_df_to_snake(pd.DataFrame(data))
            df = df[df["ticker"].notna()].copy()
            # Build period_label
            if "fiscal_year" in df.columns and "period" in df.columns:
                df["period_label"] = df["fiscal_year"].astype(str) + "-" + df["period"].astype(str)
            elif "date" in df.columns:
                df["period_label"] = df["date"].apply(
                    lambda d: f"{str(d)[:4]}-Q{(int(str(d)[5:7])-1)//3+1}" if pd.notna(d) else None
                )
            if "period_label" in df.columns:
                df["period"] = df["period_label"]
                df = df.drop(columns=["period_label"])
            df = df.dropna(subset=["ticker", "date"])
            if df.empty:
                logger.debug(f"income_statement: {ticker} 转换后无有效数据")
                return 0
            self.db.upsert_us_financial_data(df)
            return len(df)

        with ThreadPoolExecutor(max_workers=6) as pool:
            futures = {pool.submit(self._wrap_fetch, "us_financial_data", _fetch_single, t): t for t in tickers}
            for future in tqdm(as_completed(futures), total=len(futures),
                               desc="FMP Income Statement"):
                try:
                    total += future.result()
                except Exception as e:
                    logger.warning(f"Income statement 失败 {futures[future]}: {e}")

        logger.info(f"FMP income statement 总计: {total} 条")
        return total

    def download_fmp_financial_quarterly(self, tickers: list[str] = None, limit: int = 400) -> int:
        """FMP per-ticker: 季度财报 (IS+BS+CF 三表合并) → us_financial_data.

        使用 /stable/ 端点逐 ticker 下载季度数据，合并三张报表后 upsert。
        API 返回什么就存什么，不做字段过滤。

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

            is_df = pd.DataFrame(is_data)

            # 2. Balance Sheet
            bs_data = self._fmp_get_stable_json(
                "balance-sheet-statement",
                params={"symbol": ticker, "period": "quarter", "limit": limit},
            )
            bs_df = pd.DataFrame(bs_data) if bs_data else pd.DataFrame()

            # 3. Cash Flow
            cf_data = self._fmp_get_stable_json(
                "cash-flow-statement",
                params={"symbol": ticker, "period": "quarter", "limit": limit},
            )
            cf_df = pd.DataFrame(cf_data) if cf_data else pd.DataFrame()

            # 合并三表（以 IS 为基础，按 symbol+date 左连接 BS 和 CF）
            merge_keys = ["symbol", "date"]
            merged = is_df.copy()
            if not bs_df.empty:
                bs_cols = merge_keys + [c for c in bs_df.columns if c not in merged.columns]
                merged = merged.merge(bs_df[bs_cols], on=merge_keys, how="left")
            if not cf_df.empty:
                cf_cols = merge_keys + [c for c in cf_df.columns if c not in merged.columns]
                merged = merged.merge(cf_df[cf_cols], on=merge_keys, how="left")

            # 转换 camelCase → snake_case
            merged = _fmp_df_to_snake(merged)

            # 构造 period_label
            if "fiscal_year" in merged.columns and "period" in merged.columns:
                merged["period_label"] = merged["fiscal_year"].astype(str) + "-" + merged["period"].astype(str)
            elif "date" in merged.columns:
                merged["period_label"] = merged["date"].apply(
                    lambda d: f"{str(d)[:4]}-Q{(int(str(d)[5:7])-1)//3+1}" if pd.notna(d) else None
                )
            if "period_label" in merged.columns:
                merged["period"] = merged["period_label"]
                merged = merged.drop(columns=["period_label"])

            merged = merged.dropna(subset=["ticker", "date"])

            # 计算比率字段（API 可能不返回，作为 fallback）
            for col in ["revenue", "gross_profit", "net_income", "total_stockholders_equity",
                         "operating_income"]:
                if col in merged.columns:
                    merged[col] = pd.to_numeric(merged[col], errors="coerce")
            if "gross_profit_ratio" not in merged.columns or merged.get("gross_profit_ratio") is None or merged["gross_profit_ratio"].isna().all():
                if "gross_profit" in merged.columns and "revenue" in merged.columns:
                    merged["gross_profit_ratio"] = merged["gross_profit"] / merged["revenue"].replace(0, pd.NA)
            if "operating_income_ratio" not in merged.columns or merged.get("operating_income_ratio") is None or merged["operating_income_ratio"].isna().all():
                if "operating_income" in merged.columns and "revenue" in merged.columns:
                    merged["operating_income_ratio"] = merged["operating_income"] / merged["revenue"].replace(0, pd.NA)
            if "net_income_ratio" not in merged.columns or merged.get("net_income_ratio") is None or merged["net_income_ratio"].isna().all():
                if "net_income" in merged.columns and "revenue" in merged.columns:
                    merged["net_income_ratio"] = merged["net_income"] / merged["revenue"].replace(0, pd.NA)

            if not merged.empty:
                self.db.upsert_us_financial_data(merged)
                return len(merged)

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

    def download_fmp_key_metrics(self, tickers: list[str] = None, limit: int = 400) -> int:
        """FMP per-ticker: /stable/key-metrics (季度) → us_key_metric.

        API 返回什么就存什么，_fast_bulk_upsert 自动过滤到 DB 列。
        """
        if tickers is None:
            tickers = self.db.get_us_tickers()
        if not tickers:
            logger.warning("download_fmp_key_metrics: 无 ticker")
            return 0
        tickers = self._skip_done_tickers("us_key_metric", tickers)
        if not tickers:
            logger.info("download_fmp_key_metrics: 所有 ticker 已完成")
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
            df = _fmp_df_to_snake(pd.DataFrame(data))
            df = df[df["ticker"].notna()].copy()
            if df.empty:
                logger.debug(f"key_metrics: {ticker} 转换后无有效数据")
                return 0
            self.db.upsert_us_key_metric(df)
            return len(df)

        with ThreadPoolExecutor(max_workers=6) as pool:
            futures = {pool.submit(self._wrap_fetch, "us_key_metric", _fetch_single, t): t for t in tickers}
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

        与 key-metrics 共享同一张表。API 返回什么就存什么。
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
            df = _fmp_df_to_snake(pd.DataFrame(data))
            df = df[df["ticker"].notna()].copy()
            if df.empty:
                logger.debug(f"ratios: {ticker} 转换后无有效数据")
                return 0
            self.db.upsert_us_key_metric(df)
            return len(df)

        with ThreadPoolExecutor(max_workers=6) as pool:
            futures = {pool.submit(_fetch_single, t): t for t in tickers}
            for future in tqdm(as_completed(futures), total=len(futures),
                               desc="FMP Ratios"):
                try:
                    total += future.result()
                except Exception as e:
                    logger.warning(f"Ratios 失败 {futures[future]}: {e}")

        logger.info(f"FMP ratios 总计: {total} 条")
        return total

    def _download_fmp_prices_per_ticker(self, start_year: int, end_year: int) -> int:
        """FMP per-ticker: historical daily prices (多线程).

        使用 /stable/historical-price-eod/full 端点（新版）。
        该端点每次最多返回 5000 行，所以按 10 年分段请求。
        """
        tickers = self.db.get_us_tickers()
        if not tickers:
            logger.warning("无 ticker 可下载行情，请先下载股票列表")
            return 0
        tickers = self._skip_done_tickers("us_daily_price", tickers)
        if not tickers:
            logger.info("_download_fmp_prices_per_ticker: 所有 ticker 已完成")
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
                df = _fmp_df_to_snake(pd.DataFrame(data))
                # DB column: trade_date (not date), change_pct (not change_percent)
                df = df.rename(columns={"date": "trade_date", "change_percent": "change_pct"})
                df = df[df["trade_date"].notna()]
                if not df.empty:
                    self.db.bulk_upsert_us_daily_price(df)
                    count += len(df)
            return count

        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = {pool.submit(self._wrap_fetch, "us_daily_price", _fetch_price_single, t): t for t in tickers}
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
        tickers = self._skip_done_tickers("us_industry_class", tickers)
        if not tickers:
            logger.info("download_fmp_profiles: 所有 ticker 已完成")
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
            df = _fmp_df_to_snake(pd.DataFrame(data))
            df = df[df["ticker"].notna()]
            if df.empty:
                logger.debug(f"_fetch_profile_batch: 转换后无有效数据")
                return 0
            self.db.upsert_us_industry_class(df)
            return len(df)

        with ThreadPoolExecutor(max_workers=4) as pool:
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
        tickers = self._skip_done_tickers("us_insider_trade", tickers)
        if not tickers:
            logger.info("download_fmp_insider_trading: 所有 ticker 已完成")
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
                df = _fmp_df_to_snake(pd.DataFrame(data))
                # FMP has typo: acquistionOrDisposition → acquistion_or_disposition
                # DB column: acquisition_or_disposition
                df = df.rename(columns={"acquistion_or_disposition": "acquisition_or_disposition"})
                if "ticker" not in df.columns:
                    df["ticker"] = ticker
                if not df.empty:
                    self.db.upsert_us_insider_trade(df)
                    count += len(df)
                if len(data) < 100:
                    logger.debug(f"_fetch_insider_single: 结束循环 (len(data) < 100)")

                    break
                page += 1
            return count

        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = {pool.submit(self._wrap_fetch, "us_insider_trade", _fetch_insider_single, t): t for t in tickers}
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
        tickers = self._skip_done_tickers("us_analyst_recommendation", tickers)
        if not tickers:
            logger.info("download_fmp_analyst_grades: 所有 ticker 已完成")
            return 0

        total = 0

        def _fetch_grade_single(ticker):
            data = self._fmp_get_json(f"grade/{ticker}")
            if not data:
                logger.debug(f"analyst_grades: {ticker} 无评级数据")
                return 0
            df = _fmp_df_to_snake(pd.DataFrame(data))
            # Rename FMP fields to match DB columns
            df = df.rename(columns={
                "grading_company": "analyst_company",
                "new_grade": "rating",
            })
            # Truncate date to YYYY-MM-DD
            if "date" in df.columns:
                df["date"] = df["date"].astype(str).str[:10]
            # Filter: must have date and rating
            df = df[df["date"].notna() & df["rating"].notna() & (df["rating"] != "")]
            if df.empty:
                logger.debug(f"analyst_grades: {ticker} 过滤后无有效记录")
                return 0
            if "analyst_company" in df.columns:
                df = df.drop_duplicates(subset=["ticker", "date", "analyst_company"], keep="last")
            self.db.upsert_us_analyst_recommendation(df)
            return len(df)

        with ThreadPoolExecutor(max_workers=6) as pool:
            futures = {pool.submit(self._wrap_fetch, "us_analyst_recommendation", _fetch_grade_single, t): t for t in tickers}
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
        tickers = self._skip_done_tickers("us_corporate_action", tickers)
        if not tickers:
            logger.info("download_fmp_dividends_splits: 所有 ticker 已完成")
            return 0

        total = 0

        def _fetch_div_split_single(ticker):
            count = 0
            # Dividends
            # NOTE: legacy 端点，FMP 已废弃。当前 plan 无可用替代端点。
            # us_corporate_action 表已有 61 万行历史数据，暂不影响使用。
            div_data = self._fmp_get_json(f"historical-price-full/stock_dividend/{ticker}")
            if isinstance(div_data, dict) and "historical" in div_data:
                df = _fmp_df_to_snake(pd.DataFrame(div_data["historical"]))
                df["ticker"] = ticker
                df["action_type"] = "dividend"
                # adjDividend → adj_dividend; use as value, fallback to dividend
                if "adj_dividend" in df.columns:
                    df["value"] = df["adj_dividend"].fillna(df.get("dividend"))
                elif "dividend" in df.columns:
                    df["value"] = df["dividend"]
                if not df.empty:
                    self.db.upsert_us_corporate_action(df)
                    count += len(df)
            # Splits
            split_data = self._fmp_get_json(f"historical-price-full/stock_split/{ticker}")
            if isinstance(split_data, dict) and "historical" in split_data:
                df = _fmp_df_to_snake(pd.DataFrame(split_data["historical"]))
                df["ticker"] = ticker
                df["action_type"] = "split"
                # Compute split ratio from numerator/denominator
                if "numerator" in df.columns and "denominator" in df.columns:
                    df["numerator"] = pd.to_numeric(df["numerator"], errors="coerce").fillna(0)
                    df["denominator"] = pd.to_numeric(df["denominator"], errors="coerce").clip(lower=1)
                    df["value"] = df["numerator"] / df["denominator"]
                if not df.empty:
                    self.db.upsert_us_corporate_action(df)
                    count += len(df)
            return count

        with ThreadPoolExecutor(max_workers=6) as pool:
            futures = {pool.submit(self._wrap_fetch, "us_corporate_action", _fetch_div_split_single, t): t for t in tickers}
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
                df = _fmp_df_to_snake(pd.DataFrame(data))
                # DB uses index_code + trade_date, not ticker + date
                df = df.rename(columns={"ticker": "index_code", "date": "trade_date"})
                if "index_code" not in df.columns:
                    df["index_code"] = symbol
                df = df[df["trade_date"].notna()]
                if not df.empty:
                    self.db.bulk_upsert_us_index_daily(df)
                    total += len(df)

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
                df = _fmp_df_to_snake(pd.DataFrame(data))
                df = df.rename(columns={"ticker": "symbol", "date": "trade_date"})
                # 保持 yfinance 符号兼容
                df["symbol"] = yf_sym
                df = df[df["trade_date"].notna()]
                if not df.empty:
                    self.db.bulk_upsert_us_commodity_price(df)
                    total += len(df)

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

        with ThreadPoolExecutor(max_workers=6) as pool:
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

        with ThreadPoolExecutor(max_workers=6) as pool:
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

        with ThreadPoolExecutor(max_workers=6) as pool:
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
    # FMP 新增数据端点（Sprint 3）
    # ==============================================================

    def download_fmp_company_profiles(self, tickers: list[str] = None) -> int:
        """FMP per-ticker: /stable/profile → us_company_profile (全量公司信息)."""
        if tickers is None:
            tickers = self.db.get_us_tickers(stocks_only=False)
        if not tickers:
            logger.warning("download_fmp_company_profiles: 无 ticker")
            return 0
        tickers = self._skip_done_tickers("us_company_profile", tickers)
        if not tickers:
            logger.info("download_fmp_company_profiles: 所有 ticker 已完成")
            return 0
        total = 0
        batch_size = 50

        def _fetch_batch(batch):
            symbols = ",".join(batch)
            data = self._fmp_get_json(f"profile/{symbols}")
            if not data:
                return 0
            df = _fmp_df_to_snake(pd.DataFrame(data))
            self.db.upsert_us_company_profile(df)
            return len(df)

        batches = [tickers[i:i + batch_size] for i in range(0, len(tickers), batch_size)]
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = {pool.submit(_fetch_batch, b): b for b in batches}
            for future in tqdm(as_completed(futures), total=len(futures), desc="FMP Company Profiles"):
                try:
                    total += future.result()
                except Exception as e:
                    logger.warning(f"Profile batch 失败: {e}")
        logger.info(f"FMP company profiles 总计: {total} 条")
        return total

    def download_fmp_historical_market_cap(self, tickers: list[str] = None, limit: int = 5000) -> int:
        """FMP per-ticker: historical-market-cap → us_historical_market_cap."""
        if tickers is None:
            tickers = self.db.get_us_tickers(stocks_only=True)
        if not tickers:
            logger.warning("download_fmp_historical_market_cap: 无 ticker")
            return 0
        tickers = self._skip_done_tickers("us_historical_market_cap", tickers)
        if not tickers:
            logger.info("download_fmp_historical_market_cap: 所有 ticker 已完成")
            return 0
        total = 0

        def _fetch_single(ticker):
            data = self._fmp_get_stable_json(
                "historical-market-capitalization",
                params={"symbol": ticker, "limit": limit},
            )
            if not data:
                logger.debug(f"historical_market_cap: {ticker} 无数据")
                return 0
            df = _fmp_df_to_snake(pd.DataFrame(data))
            self.db.upsert_us_historical_market_cap(df)
            return len(df)

        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = {pool.submit(self._wrap_fetch, "us_historical_market_cap", _fetch_single, t): t for t in tickers}
            for future in tqdm(as_completed(futures), total=len(futures), desc="FMP Historical Market Cap"):
                try:
                    total += future.result()
                except Exception as e:
                    logger.warning(f"Historical market cap 失败 {futures[future]}: {e}")
        logger.info(f"FMP historical market cap 总计: {total} 条")
        return total

    def download_fmp_shares_float(self, tickers: list[str] = None) -> int:
        """FMP per-ticker: shares-float → us_shares_float."""
        if tickers is None:
            tickers = self.db.get_us_tickers(stocks_only=True)
        if not tickers:
            logger.warning("download_fmp_shares_float: 无 ticker")
            return 0
        tickers = self._skip_done_tickers("us_shares_float", tickers)
        if not tickers:
            logger.info("download_fmp_shares_float: 所有 ticker 已完成")
            return 0
        total = 0

        def _fetch_single(ticker):
            data = self._fmp_get_stable_json("shares-float", params={"symbol": ticker})
            if not data:
                logger.debug(f"shares_float: {ticker} 无数据")
                return 0
            df = _fmp_df_to_snake(pd.DataFrame(data if isinstance(data, list) else [data]))
            self.db.upsert_us_shares_float(df)
            return len(df)

        with ThreadPoolExecutor(max_workers=6) as pool:
            futures = {pool.submit(self._wrap_fetch, "us_shares_float", _fetch_single, t): t for t in tickers}
            for future in tqdm(as_completed(futures), total=len(futures), desc="FMP Shares Float"):
                try:
                    total += future.result()
                except Exception as e:
                    logger.warning(f"Shares float 失败 {futures[future]}: {e}")
        logger.info(f"FMP shares float 总计: {total} 条")
        return total

    def download_fmp_financial_scores(self, tickers: list[str] = None) -> int:
        """FMP per-ticker: financial-scores → us_financial_score."""
        if tickers is None:
            tickers = self.db.get_us_tickers(stocks_only=True)
        if not tickers:
            logger.warning("download_fmp_financial_scores: 无 ticker")
            return 0
        tickers = self._skip_done_tickers("us_financial_score", tickers)
        if not tickers:
            logger.info("download_fmp_financial_scores: 所有 ticker 已完成")
            return 0
        total = 0

        def _fetch_single(ticker):
            data = self._fmp_get_stable_json("financial-scores", params={"symbol": ticker})
            if not data:
                logger.debug(f"financial_scores: {ticker} 无数据")
                return 0
            df = _fmp_df_to_snake(pd.DataFrame(data if isinstance(data, list) else [data]))
            self.db.upsert_us_financial_score(df)
            return len(df)

        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = {pool.submit(self._wrap_fetch, "us_financial_score", _fetch_single, t): t for t in tickers}
            for future in tqdm(as_completed(futures), total=len(futures), desc="FMP Financial Scores"):
                try:
                    total += future.result()
                except Exception as e:
                    logger.warning(f"Financial scores 失败 {futures[future]}: {e}")
        logger.info(f"FMP financial scores 总计: {total} 条")
        return total

    def download_fmp_financial_growth(self, tickers: list[str] = None, limit: int = 400) -> int:
        """FMP per-ticker: financial-growth → us_financial_growth."""
        if tickers is None:
            tickers = self.db.get_us_tickers(stocks_only=True)
        if not tickers:
            logger.warning("download_fmp_financial_growth: 无 ticker")
            return 0
        tickers = self._skip_done_tickers("us_financial_growth", tickers)
        if not tickers:
            logger.info("download_fmp_financial_growth: 所有 ticker 已完成")
            return 0
        total = 0

        def _fetch_single(ticker):
            data = self._fmp_get_stable_json(
                "financial-growth",
                params={"symbol": ticker, "period": "quarter", "limit": limit},
            )
            if not data:
                logger.debug(f"financial_growth: {ticker} 无数据")
                return 0
            df = _fmp_df_to_snake(pd.DataFrame(data))
            self.db.upsert_us_financial_growth(df)
            return len(df)

        with ThreadPoolExecutor(max_workers=6) as pool:
            futures = {pool.submit(self._wrap_fetch, "us_financial_growth", _fetch_single, t): t for t in tickers}
            for future in tqdm(as_completed(futures), total=len(futures), desc="FMP Financial Growth"):
                try:
                    total += future.result()
                except Exception as e:
                    logger.warning(f"Financial growth 失败 {futures[future]}: {e}")
        logger.info(f"FMP financial growth 总计: {total} 条")
        return total

    def download_fmp_enterprise_values(self, tickers: list[str] = None, limit: int = 400) -> int:
        """FMP per-ticker: enterprise-values → us_enterprise_value."""
        if tickers is None:
            tickers = self.db.get_us_tickers(stocks_only=True)
        if not tickers:
            logger.warning("download_fmp_enterprise_values: 无 ticker")
            return 0
        tickers = self._skip_done_tickers("us_enterprise_value", tickers)
        if not tickers:
            logger.info("download_fmp_enterprise_values: 所有 ticker 已完成")
            return 0
        total = 0

        def _fetch_single(ticker):
            data = self._fmp_get_stable_json(
                "enterprise-values",
                params={"symbol": ticker, "period": "quarter", "limit": limit},
            )
            if not data:
                logger.debug(f"enterprise_values: {ticker} 无数据")
                return 0
            df = _fmp_df_to_snake(pd.DataFrame(data))
            self.db.upsert_us_enterprise_value(df)
            return len(df)

        with ThreadPoolExecutor(max_workers=6) as pool:
            futures = {pool.submit(self._wrap_fetch, "us_enterprise_value", _fetch_single, t): t for t in tickers}
            for future in tqdm(as_completed(futures), total=len(futures), desc="FMP Enterprise Values"):
                try:
                    total += future.result()
                except Exception as e:
                    logger.warning(f"Enterprise values 失败 {futures[future]}: {e}")
        logger.info(f"FMP enterprise values 总计: {total} 条")
        return total

    def download_fmp_owner_earnings(self, tickers: list[str] = None) -> int:
        """FMP per-ticker: owner-earnings → us_owner_earnings."""
        if tickers is None:
            tickers = self.db.get_us_tickers(stocks_only=True)
        if not tickers:
            logger.warning("download_fmp_owner_earnings: 无 ticker")
            return 0
        tickers = self._skip_done_tickers("us_owner_earnings", tickers)
        if not tickers:
            logger.info("download_fmp_owner_earnings: 所有 ticker 已完成")
            return 0
        total = 0

        def _fetch_single(ticker):
            data = self._fmp_get_stable_json("owner-earnings", params={"symbol": ticker})
            if not data:
                logger.debug(f"owner_earnings: {ticker} 无数据")
                return 0
            df = _fmp_df_to_snake(pd.DataFrame(data if isinstance(data, list) else [data]))
            self.db.upsert_us_owner_earnings(df)
            return len(df)

        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = {pool.submit(self._wrap_fetch, "us_owner_earnings", _fetch_single, t): t for t in tickers}
            for future in tqdm(as_completed(futures), total=len(futures), desc="FMP Owner Earnings"):
                try:
                    total += future.result()
                except Exception as e:
                    logger.warning(f"Owner earnings 失败 {futures[future]}: {e}")
        logger.info(f"FMP owner earnings 总计: {total} 条")
        return total

    def download_fmp_dcf_valuations(self, tickers: list[str] = None) -> int:
        """FMP per-ticker: dcf → us_dcf_valuation."""
        if tickers is None:
            tickers = self.db.get_us_tickers(stocks_only=True)
        if not tickers:
            logger.warning("download_fmp_dcf_valuations: 无 ticker")
            return 0
        tickers = self._skip_done_tickers("us_dcf_valuation", tickers)
        if not tickers:
            logger.info("download_fmp_dcf_valuations: 所有 ticker 已完成")
            return 0
        total = 0

        def _fetch_single(ticker):
            count = 0
            for dcf_type, endpoint in [("standard", "discounted-cash-flow"), ("levered", "levered-dcf")]:
                data = self._fmp_get_stable_json(endpoint, params={"symbol": ticker})
                if not data:
                    continue
                df = _fmp_df_to_snake(pd.DataFrame(data if isinstance(data, list) else [data]))
                df["dcf_type"] = dcf_type
                self.db.upsert_us_dcf_valuation(df)
                count += len(df)
            return count

        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = {pool.submit(self._wrap_fetch, "us_dcf_valuation", _fetch_single, t): t for t in tickers}
            for future in tqdm(as_completed(futures), total=len(futures), desc="FMP DCF Valuations"):
                try:
                    total += future.result()
                except Exception as e:
                    logger.warning(f"DCF 失败 {futures[future]}: {e}")
        logger.info(f"FMP DCF valuations 总计: {total} 条")
        return total

    def download_fmp_stock_peers(self, tickers: list[str] = None) -> int:
        """FMP per-ticker: peers → us_stock_peer."""
        if tickers is None:
            tickers = self.db.get_us_tickers(stocks_only=True)
        if not tickers:
            logger.warning("download_fmp_stock_peers: 无 ticker")
            return 0
        tickers = self._skip_done_tickers("us_stock_peer", tickers)
        if not tickers:
            logger.info("download_fmp_stock_peers: 所有 ticker 已完成")
            return 0
        total = 0

        def _fetch_single(ticker):
            data = self._fmp_get_stable_json("stock-peers", params={"symbol": ticker})
            if not data:
                logger.debug(f"stock_peers: {ticker} 无数据")
                return 0
            # API returns list of peer tickers
            peers = data[0].get("peersList", []) if isinstance(data, list) and data else []
            if not peers:
                return 0
            records = [{"ticker": ticker, "peer_ticker": p} for p in peers if p]
            df = pd.DataFrame(records)
            self.db.upsert_us_stock_peer(df)
            return len(df)

        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = {pool.submit(self._wrap_fetch, "us_stock_peer", _fetch_single, t): t for t in tickers}
            for future in tqdm(as_completed(futures), total=len(futures), desc="FMP Stock Peers"):
                try:
                    total += future.result()
                except Exception as e:
                    logger.warning(f"Peers 失败 {futures[future]}: {e}")
        logger.info(f"FMP stock peers 总计: {total} 条")
        return total

    def download_fmp_esg_ratings(self, tickers: list[str] = None) -> int:
        """FMP per-ticker: esg-ratings → us_esg_rating."""
        if tickers is None:
            tickers = self.db.get_us_tickers(stocks_only=True)
        if not tickers:
            logger.warning("download_fmp_esg_ratings: 无 ticker")
            return 0
        tickers = self._skip_done_tickers("us_esg_rating", tickers)
        if not tickers:
            logger.info("download_fmp_esg_ratings: 所有 ticker 已完成")
            return 0
        total = 0

        def _fetch_single(ticker):
            data = self._fmp_get_stable_json("esg-rating", params={"symbol": ticker})
            if not data:
                logger.debug(f"esg_ratings: {ticker} 无数据")
                return 0
            df = _fmp_df_to_snake(pd.DataFrame(data if isinstance(data, list) else [data]))
            self.db.upsert_us_esg_rating(df)
            return len(df)

        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = {pool.submit(self._wrap_fetch, "us_esg_rating", _fetch_single, t): t for t in tickers}
            for future in tqdm(as_completed(futures), total=len(futures), desc="FMP ESG Ratings"):
                try:
                    total += future.result()
                except Exception as e:
                    logger.warning(f"ESG 失败 {futures[future]}: {e}")
        logger.info(f"FMP ESG ratings 总计: {total} 条")
        return total

    def download_fmp_price_targets(self, tickers: list[str] = None) -> int:
        """FMP per-ticker: price-target-consensus → us_price_target."""
        if tickers is None:
            tickers = self.db.get_us_tickers(stocks_only=True)
        if not tickers:
            logger.warning("download_fmp_price_targets: 无 ticker")
            return 0
        tickers = self._skip_done_tickers("us_price_target", tickers)
        if not tickers:
            logger.info("download_fmp_price_targets: 所有 ticker 已完成")
            return 0
        total = 0

        def _fetch_single(ticker):
            data = self._fmp_get_stable_json("price-target-consensus", params={"symbol": ticker})
            if not data:
                logger.debug(f"price_targets: {ticker} 无数据")
                return 0
            df = _fmp_df_to_snake(pd.DataFrame(data if isinstance(data, list) else [data]))
            self.db.upsert_us_price_target(df)
            return len(df)

        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = {pool.submit(self._wrap_fetch, "us_price_target", _fetch_single, t): t for t in tickers}
            for future in tqdm(as_completed(futures), total=len(futures), desc="FMP Price Targets"):
                try:
                    total += future.result()
                except Exception as e:
                    logger.warning(f"Price targets 失败 {futures[future]}: {e}")
        logger.info(f"FMP price targets 总计: {total} 条")
        return total

    def download_fmp_insider_statistics(self, tickers: list[str] = None) -> int:
        """FMP per-ticker: insider-trade-statistics → us_insider_statistic."""
        if tickers is None:
            tickers = self.db.get_us_tickers(stocks_only=True)
        if not tickers:
            logger.warning("download_fmp_insider_statistics: 无 ticker")
            return 0
        tickers = self._skip_done_tickers("us_insider_statistic", tickers)
        if not tickers:
            logger.info("download_fmp_insider_statistics: 所有 ticker 已完成")
            return 0
        total = 0

        def _fetch_single(ticker):
            data = self._fmp_get_stable_json("insider-trading/statistics", params={"symbol": ticker})
            if not data:
                logger.debug(f"insider_statistics: {ticker} 无数据")
                return 0
            df = _fmp_df_to_snake(pd.DataFrame(data if isinstance(data, list) else [data]))
            self.db.upsert_us_insider_statistic(df)
            return len(df)

        with ThreadPoolExecutor(max_workers=6) as pool:
            futures = {pool.submit(self._wrap_fetch, "us_insider_statistic", _fetch_single, t): t for t in tickers}
            for future in tqdm(as_completed(futures), total=len(futures), desc="FMP Insider Statistics"):
                try:
                    total += future.result()
                except Exception as e:
                    logger.warning(f"Insider stats 失败 {futures[future]}: {e}")
        logger.info(f"FMP insider statistics 总计: {total} 条")
        return total

    def download_fmp_employee_count(self, tickers: list[str] = None) -> int:
        """FMP per-ticker: historical-employee-count → us_employee_count."""
        if tickers is None:
            tickers = self.db.get_us_tickers(stocks_only=True)
        if not tickers:
            logger.warning("download_fmp_employee_count: 无 ticker")
            return 0
        tickers = self._skip_done_tickers("us_employee_count", tickers)
        if not tickers:
            logger.info("download_fmp_employee_count: 所有 ticker 已完成")
            return 0
        total = 0

        def _fetch_single(ticker):
            data = self._fmp_get_stable_json("employee-count", params={"symbol": ticker})
            if not data:
                logger.debug(f"employee_count: {ticker} 无数据")
                return 0
            df = _fmp_df_to_snake(pd.DataFrame(data))
            self.db.upsert_us_employee_count(df)
            return len(df)

        with ThreadPoolExecutor(max_workers=6) as pool:
            futures = {pool.submit(self._wrap_fetch, "us_employee_count", _fetch_single, t): t for t in tickers}
            for future in tqdm(as_completed(futures), total=len(futures), desc="FMP Employee Count"):
                try:
                    total += future.result()
                except Exception as e:
                    logger.warning(f"Employee count 失败 {futures[future]}: {e}")
        logger.info(f"FMP employee count 总计: {total} 条")
        return total

    def download_fmp_index_constituents_history(self) -> int:
        """FMP: 指数历史成分变更 → us_index_constituent."""
        total = 0
        index_map = {
            "sp500": "historical/sp500_constituent",
            "nasdaq": "historical/nasdaq_constituent",
            "dowjones": "historical/dowjones_constituent",
        }
        for index_name, path in index_map.items():
            data = self._fmp_get_json(path)
            if not data:
                logger.debug(f"index_constituents_history: {index_name} 无数据")
                continue
            df = _fmp_df_to_snake(pd.DataFrame(data))
            df["index_name"] = index_name
            self.db.upsert_us_index_constituent(df)
            total += len(df)
            logger.info(f"FMP {index_name} 历史成分: {len(df)} 条")
        logger.info(f"FMP index constituents history 总计: {total} 条")
        return total

    def download_fmp_delisted_companies(self) -> int:
        """FMP bulk: delisted-companies → us_delisted (一次全量)."""
        data = self._fmp_get_stable_json("delisted-companies")
        if not data:
            logger.warning("download_fmp_delisted_companies: 无数据")
            return 0
        df = _fmp_df_to_snake(pd.DataFrame(data))
        self.db.upsert_us_delisted(df)
        logger.info(f"FMP delisted companies 总计: {len(df)} 条")
        return len(df)

    def download_fmp_symbol_changes(self) -> int:
        """FMP bulk: symbol-changes → us_symbol_change (一次全量)."""
        data = self._fmp_get_stable_json("symbol-change")
        if not data:
            logger.warning("download_fmp_symbol_changes: 无数据")
            return 0
        df = _fmp_df_to_snake(pd.DataFrame(data))
        self.db.upsert_us_symbol_change(df)
        logger.info(f"FMP symbol changes 总计: {len(df)} 条")
        return len(df)

    def download_fmp_senate_trading(self) -> int:
        """FMP: senate + house trading → us_congress_trade."""
        total = 0
        for chamber, endpoint in [("senate", "senate-trading"), ("house", "house-disclosure")]:
            page = 0
            while True:
                data = self._fmp_get_stable_json(endpoint, params={"page": page})
                if not data:
                    logger.debug(f"congress_trading: {chamber} page {page} 无数据，结束")
                    break
                df = _fmp_df_to_snake(pd.DataFrame(data))
                df["source"] = f"fmp_{chamber}"
                self.db.upsert_us_congress_trade(df)
                total += len(df)
                if len(data) < 100:
                    break
                page += 1
        logger.info(f"FMP congress trading 总计: {total} 条")
        return total

    # ==============================================================
    # 全量导入调度
    # ==============================================================

    def download_fmp_all_bulk(self, start_year: int = 1995) -> dict:
        """FMP: 全量下载（所有端点）。"""
        results = {}

        # Phase 1: Bulk 端点（一次全市场）
        logger.info("=== Phase 1: Bulk 端点 ===")
        results["stock_list"] = self.download_fmp_stock_list()
        results["index_constituents"] = self.download_fmp_index_constituents()
        results["delisted"] = self.download_fmp_delisted_companies()
        results["symbol_changes"] = self.download_fmp_symbol_changes()
        results["shares_float"] = self.download_fmp_shares_float()
        self.db.flush_writes()  # Phase 1 写入完成后再继续（后续依赖 ticker 列表）

        # Phase 2: Per-ticker 核心数据（只跑普通股）
        logger.info("=== Phase 2: Per-ticker 核心数据 ===")
        results["company_profiles"] = self.download_fmp_company_profiles()
        results["prices"] = self._download_fmp_prices_per_ticker(start_year, datetime.now().year)
        results["historical_market_cap"] = self.download_fmp_historical_market_cap()
        results["financial_quarterly"] = self.download_fmp_financial_quarterly()
        results["income_statement"] = self.download_fmp_income_statement_bulk()
        results["key_metrics"] = self.download_fmp_key_metrics()
        results["ratios"] = self.download_fmp_ratios()
        results["financial_scores"] = self.download_fmp_financial_scores()
        results["financial_growth"] = self.download_fmp_financial_growth()
        results["enterprise_values"] = self.download_fmp_enterprise_values()
        results["owner_earnings"] = self.download_fmp_owner_earnings()
        self.db.flush_writes()

        # Phase 3: Per-ticker 辅助数据
        logger.info("=== Phase 3: Per-ticker 辅助数据 ===")
        results["earnings_surprises"] = self.download_fmp_earnings_surprises_bulk()
        results["eps_estimates"] = self.download_fmp_eps_estimates_bulk()
        results["profiles"] = self.download_fmp_profiles()
        results["insider"] = self.download_fmp_insider_trading()
        results["insider_statistics"] = self.download_fmp_insider_statistics()
        results["analyst_grades"] = self.download_fmp_analyst_grades()
        results["price_targets"] = self.download_fmp_price_targets()
        results["dividends_splits"] = self.download_fmp_dividends_splits()
        results["dcf_valuations"] = self.download_fmp_dcf_valuations()
        results["stock_peers"] = self.download_fmp_stock_peers()
        results["esg_ratings"] = self.download_fmp_esg_ratings()
        results["employee_count"] = self.download_fmp_employee_count()

        # Phase 4: 指数/商品/宏观
        logger.info("=== Phase 4: 指数/商品/宏观 ===")
        results["index_daily"] = self.download_fmp_index_daily(start_year)
        results["index_history"] = self.download_fmp_index_constituents_history()
        results["commodities"] = self.download_fmp_commodity_prices(start_year)
        results["macro"] = self.download_fmp_macro()
        results["congress"] = self.download_fmp_senate_trading()
        self.db.flush_writes()

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
