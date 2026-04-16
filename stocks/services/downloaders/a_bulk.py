"""
A 股数据批量下载器（Tushare + AkShare）

1:1 对标 us_bulk.py:BulkDownloader 的模式：
    - 类封装（UpsertManager + RateLimiter + ThreadPoolExecutor）
    - 每个端点多线程 + 断点续跑（_skip_done_tickers / _mark_done）
    - 增量判断（_get_ticker_latest / _ticker_needs_update）
    - download_tushare_all() 按 phase 顺序调各方法

原则：
    - API 返回什么就存什么，不指定 fields=，不做列裁剪
    - 列名 = Tushare 原名（snake_case）
    - 财报拆 4 表（income / balance / cashflow / indicator）
    - 使用 Django ORM（UpsertManager）写库
"""

import collections
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from typing import Optional

import pandas as pd
import requests
import tushare as ts
from tqdm import tqdm

from services.config import (
    COMMODITY_EXCHANGE_MAP,
    COMMODITY_SYMBOLS,
    DATA_START_DATE,
    LOG_LEVEL,
    ST_KEYWORDS,
    TUSHARE_MAX_RETRIES,
    TUSHARE_RATE_LIMIT,
    TUSHARE_RETRY_WAIT,
    TUSHARE_TOKEN,
)
from stocks.models import (
    ACommodityPrice,
    ADailyPrice,
    AFinancialBalance,
    AFinancialCashflow,
    AFinancialIncome,
    AFinancialIndicator,
    AIndexDaily,
    AIndustryClass,
    AMacroIndicator,
    AInsiderTrade,
    AResearchReport,
    AStockBasic,
    ATradeCal,
    ImportProgress,
)
from stocks.services.upsert import get_upsert_manager

logger = logging.getLogger(__name__)
logger.setLevel(LOG_LEVEL)


# ============================================================
# Tushare Rate Limiter（滑动窗口，对标 us_bulk.py:RateLimiter）
# ============================================================

class TushareRateLimiter:
    """每 60 秒窗口内最多 max_per_min 次请求，超出自动 sleep。"""

    def __init__(self, max_per_min: int = TUSHARE_RATE_LIMIT):
        self.max_per_min = max_per_min
        self._timestamps: collections.deque = collections.deque()
        self._lock = threading.Lock()

    def wait(self):
        with self._lock:
            now = time.monotonic()
            while self._timestamps and now - self._timestamps[0] >= 60.0:
                self._timestamps.popleft()
            if len(self._timestamps) >= self.max_per_min:
                wait = 60.0 - (now - self._timestamps[0]) + 0.1
                if wait > 0:
                    logger.debug(f"tushare 限速等待 {wait:.1f}s")
                    time.sleep(wait)
                now = time.monotonic()
                while self._timestamps and now - self._timestamps[0] >= 60.0:
                    self._timestamps.popleft()
            self._timestamps.append(time.monotonic())


def _tushare_call(pro, method: str, limiter: TushareRateLimiter, **kwargs) -> pd.DataFrame:
    """统一 Tushare API 调用（限速 + 重试），不指定 fields= → 返回全字段。"""
    api_func = getattr(pro, method)
    for attempt in range(1, TUSHARE_MAX_RETRIES + 1):
        limiter.wait()
        try:
            df = api_func(**kwargs)
            return df if df is not None else pd.DataFrame()
        except Exception as e:
            err_msg = str(e).lower()
            if "最多访问" in err_msg or "exceed" in err_msg or "freq" in err_msg:
                logger.warning(f"{method} 触发限流 (第{attempt}次)，等待 {TUSHARE_RETRY_WAIT}s")
                time.sleep(TUSHARE_RETRY_WAIT)
            elif attempt < TUSHARE_MAX_RETRIES:
                logger.warning(f"{method} 调用失败 (第{attempt}次): {e}")
                time.sleep(2)
            else:
                raise RuntimeError(f"{method}({kwargs}) 重试 {TUSHARE_MAX_RETRIES} 次后仍失败: {e}")
    logger.debug(f"_tushare_call: {method} 超过最大重试，返回空 DataFrame")
    return pd.DataFrame()


# ============================================================
# 辅助函数
# ============================================================

def _detect_board(ts_code: str) -> str:
    code = ts_code.split(".")[0]
    if code.startswith(("00", "60")):
        return "主板"
    if code.startswith("30"):
        return "创业板"
    if code.startswith("68"):
        return "科创板"
    return "其他"


def _is_limit(pct_chg, board: str, direction: str) -> bool:
    if pd.isna(pct_chg):
        return False
    limit = 20.0 if board in ("创业板", "科创板") else 10.0
    threshold = limit - 0.05
    return pct_chg >= threshold if direction == "up" else pct_chg <= -threshold


def _safe_float(val) -> float | None:
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return None
    try:
        return float(val)
    except (ValueError, TypeError):
        return None


def _date_col_to_date(df: pd.DataFrame, cols: tuple[str, ...]):
    for col in cols:
        if col in df.columns:
            df[col] = pd.to_datetime(df[col], errors="coerce")
            df[col] = df[col].apply(lambda x: x.date() if pd.notna(x) else None)


# ============================================================
# AShareBulkDownloader — 对标 us_bulk.py:BulkDownloader
# ============================================================

class AShareBulkDownloader:
    """
    A 股批量下载器（Tushare + AkShare）。

    用法:
        dl = AShareBulkDownloader()
        dl.download_tushare_all("20150101")             # 全量
        dl.download_tushare_stock_list()                 # 单端点
        dl.download_tushare_income()                     # 单端点（多线程 + 断点）

    增量模式:
        dl = AShareBulkDownloader(incremental=True)
        dl.download_tushare_all()                        # 增量
    """

    _WORKERS_BULK = 10
    _WORKERS_INCREMENTAL = 15

    def __init__(self, db=None, incremental: bool = False, start_date: str = DATA_START_DATE, **kwargs):
        self._um = get_upsert_manager()
        self._incremental = incremental
        self._workers = self._WORKERS_INCREMENTAL if incremental else self._WORKERS_BULK
        self._limiter = TushareRateLimiter(TUSHARE_RATE_LIMIT)
        self._sd = start_date
        if not TUSHARE_TOKEN:
            raise RuntimeError("TUSHARE_TOKEN 未配置，检查 .env")
        self._pro = ts.pro_api(TUSHARE_TOKEN)

    # ----------------------------------------------------------
    # 通用工具（对标 us_bulk.py 的 _get_tickers / _get_ticker_latest / _mark_done）
    # ----------------------------------------------------------

    def _get_tickers(self, active_only: bool = True) -> list[str]:
        """从 a_stock_basic 取 ts_code 列表。"""
        qs = AStockBasic.objects.all()
        if active_only:
            qs = qs.filter(list_status="L")
        return list(qs.values_list("ts_code", flat=True))

    def _get_ticker_latest(self, model, date_field: str = "end_date") -> dict[str, str]:
        """每个 ts_code 在指定表中的最新日期。"""
        from django.db.models import Max
        qs = model.objects.values("ts_code").annotate(latest=Max(date_field))
        return {
            row["ts_code"]: str(row["latest"])[:10]
            for row in qs
            if row["latest"] is not None
        }

    def _ticker_needs_update(self, ts_code: str, latest_map: dict, stale_days: int = 1) -> str | None:
        """判断 ts_code 是否需要更新，返回起始日期。None = 不需要。"""
        latest = latest_map.get(ts_code)
        if not latest:
            return "19900101"
        from_date = (pd.to_datetime(latest) + timedelta(days=1)).strftime("%Y%m%d")
        cutoff = (datetime.now() - timedelta(days=stale_days)).strftime("%Y-%m-%d")
        if latest >= cutoff:
            return None
        return from_date

    def _skip_done_tickers(self, table: str, tickers: list[str]) -> list[str]:
        """断点续跑：跳过 import_progress 已标记完成的 ticker。"""
        if self._incremental:
            logger.info(f"增量模式 {table}: 跳过断点检查, {len(tickers)} tickers")
            return tickers
        done = self._um.get_import_done_tickers(table)
        remaining = [t for t in tickers if t not in done]
        if done:
            logger.info(f"断点续跑 {table}: 全部 {len(tickers)}, 已完成 {len(done)}, 待跑 {len(remaining)}")
        return remaining

    def _skip_if_table_has_data(self, table: str, min_rows: int = 10) -> bool:
        """非增量模式下，表已有数据则跳过。"""
        if self._incremental:
            return False
        from django.db import connection
        try:
            with connection.cursor() as cursor:
                cursor.execute(f'SELECT COUNT(*) FROM "{table}"')
                cnt = cursor.fetchone()[0]
                if cnt >= min_rows:
                    logger.info(f"{table} 已有 {cnt} 条（>={min_rows}），跳过")
                    return True
        except Exception:
            pass
        return False

    def _mark_done(self, table: str, fetch_fn, ts_code: str):
        """包装 fetch：无论有无数据都标记完成，避免重跑浪费 API。"""
        count = fetch_fn(ts_code)
        self._um.mark_import_done(table, ts_code)
        return count

    # ----------------------------------------------------------
    # 1. stock_list — 股票列表（Tushare stock_basic 全字段）
    # ----------------------------------------------------------

    def download_tushare_stock_list(self) -> int:
        if self._skip_if_table_has_data("a_stock_basic"):
            return 0

        logger.info("下载 A 股股票列表 (stock_basic, 全字段)...")
        df_listed = _tushare_call(self._pro, "stock_basic", self._limiter, list_status="L")
        df_delisted = _tushare_call(self._pro, "stock_basic", self._limiter, list_status="D")
        df = pd.concat([df_listed, df_delisted], ignore_index=True)

        if df.empty:
            logger.error("stock_basic 返回空，检查 TUSHARE_TOKEN")
            return 0

        df = df[df["ts_code"].str.match(r"^(00|30|60|68)")].copy()
        logger.info(f"获取到 {len(df)} 只沪深 A 股")

        for col in ("list_date", "delist_date"):
            if col in df.columns:
                df[col] = pd.to_datetime(df[col], errors="coerce")
                df[col] = df[col].apply(lambda x: x.date() if pd.notna(x) else None)

        df["is_st"] = df["name"].apply(
            lambda n: 1 if any(kw in str(n) for kw in ST_KEYWORDS) else 0
        )
        df["board"] = df["ts_code"].apply(_detect_board)

        self._um.upsert_df(AStockBasic, df, ["ts_code"])
        self._um.flush()
        n_st = int(df["is_st"].sum()) if "is_st" in df.columns else 0
        n_del = int(df["delist_date"].notna().sum()) if "delist_date" in df.columns else 0
        logger.info(f"A 股股票列表: {len(df)} 只 (ST {n_st} / 退市 {n_del})")
        return len(df)

    # ----------------------------------------------------------
    # 2. trade_cal — 交易日历
    # ----------------------------------------------------------

    def download_tushare_trade_cal(self, start_date: str | None = None) -> int:
        sd = start_date or "19900101"
        end_date = datetime.now().strftime("%Y%m%d")
        logger.info(f"下载交易日历 SSE {sd}~{end_date}")
        df = _tushare_call(self._pro, "trade_cal", self._limiter,
                           exchange="SSE", start_date=sd, end_date=end_date)
        if df.empty:
            logger.warning("trade_cal 返回空")
            return 0
        _date_col_to_date(df, ("cal_date", "pretrade_date"))
        self._um.upsert_df(ATradeCal, df, ["exchange", "cal_date"])
        self._um.flush()
        logger.info(f"交易日历: {len(df)} 条")
        return len(df)

    # ----------------------------------------------------------
    # 3. daily_prices — 日线行情（按交易日多线程）
    #    Tushare 特殊：daily(trade_date=d) 返回全市场，非 per-ticker
    #    并行粒度 = 交易日（对标 US 的 per-ticker 并行）
    # ----------------------------------------------------------

    def download_tushare_daily_prices(self, start_date: str | None = None) -> int:
        sd = start_date or self._sd
        end_date = datetime.now().strftime("%Y%m%d")

        # 获取交易日列表
        trade_dates = self._get_open_trade_dates(sd, end_date)
        if not trade_dates:
            logger.warning(f"区间 {sd}~{end_date} 无交易日")
            return 0

        # 断点续跑：检查 DB 中已有哪些交易日（按日期去重）
        if not self._incremental:
            existing_dates = set(
                ADailyPrice.objects.filter(
                    trade_date__gte=pd.to_datetime(sd).date(),
                ).values_list("trade_date", flat=True).distinct()
            )
            existing_strs = {d.strftime("%Y%m%d") for d in existing_dates}
            remaining = [d for d in trade_dates if d not in existing_strs]
            if len(remaining) < len(trade_dates):
                logger.info(f"断点续跑 a_daily_price: {len(trade_dates)} 天, 已有 {len(trade_dates)-len(remaining)}, 待跑 {len(remaining)}")
            trade_dates = remaining

        if not trade_dates:
            logger.info("a_daily_price 全部交易日已下载")
            return 0

        logger.info(f"下载日线行情: {len(trade_dates)} 个交易日, {sd}~{end_date}, {self._workers} 线程")
        total = 0

        def _fetch_date(td: str) -> int:
            df = self._fetch_daily_by_date(td)
            if df is None or df.empty:
                return 0
            self._um.upsert_df(ADailyPrice, df, ["ts_code", "trade_date"])
            return len(df)

        with ThreadPoolExecutor(max_workers=self._workers) as pool:
            futures = {pool.submit(_fetch_date, td): td for td in trade_dates}
            for f in tqdm(as_completed(futures), total=len(futures), desc="Tushare Daily Prices"):
                try:
                    total += f.result()
                except Exception as e:
                    logger.warning(f"日线 {futures[f]} 失败: {e}")

        self._um.flush()
        logger.info(f"日线行情完成: {total} 条")
        return total

    def _fetch_daily_by_date(self, trade_date: str) -> Optional[pd.DataFrame]:
        """单日全市场：daily + daily_basic + adj_factor 合并。"""
        df_daily = _tushare_call(self._pro, "daily", self._limiter, trade_date=trade_date)
        if df_daily.empty:
            logger.debug(f"{trade_date} daily 空")
            return None

        df_basic = _tushare_call(self._pro, "daily_basic", self._limiter, trade_date=trade_date)
        df_adj = _tushare_call(self._pro, "adj_factor", self._limiter, trade_date=trade_date)

        df_daily = df_daily[df_daily["ts_code"].str.match(r"^(00|30|60|68)")].copy()
        if df_daily.empty:
            logger.debug(f"{trade_date} 筛选沪深后空")
            return None

        if not df_basic.empty:
            drop_cols = [c for c in ("close", "trade_date") if c in df_basic.columns]
            df_basic = df_basic.drop(columns=drop_cols)
            df_daily = df_daily.merge(df_basic, on="ts_code", how="left")

        if not df_adj.empty:
            df_adj = df_adj[["ts_code", "adj_factor"]]
            df_daily = df_daily.merge(df_adj, on="ts_code", how="left")

        # 涨跌停标记
        df_daily["board"] = df_daily["ts_code"].apply(_detect_board)
        df_daily["is_limit_up"] = df_daily.apply(
            lambda r: 1 if _is_limit(r.get("pct_chg"), r["board"], "up") else 0, axis=1
        )
        df_daily["is_limit_down"] = df_daily.apply(
            lambda r: 1 if _is_limit(r.get("pct_chg"), r["board"], "down") else 0, axis=1
        )
        df_daily = df_daily.drop(columns=["board"])
        df_daily["trade_date"] = pd.to_datetime(df_daily["trade_date"]).dt.date

        return df_daily

    def _get_open_trade_dates(self, start_date: str, end_date: str) -> list[str]:
        """从 a_trade_cal 取开市日；无缓存则回退 API。"""
        dates = list(
            ATradeCal.objects.filter(
                exchange="SSE", is_open=1,
                cal_date__gte=pd.to_datetime(start_date).date(),
                cal_date__lte=pd.to_datetime(end_date).date(),
            ).order_by("cal_date").values_list("cal_date", flat=True)
        )
        if dates:
            return [d.strftime("%Y%m%d") for d in dates]
        logger.debug("ATradeCal 无缓存，从 API 取")
        df = _tushare_call(self._pro, "trade_cal", self._limiter,
                           exchange="SSE", start_date=start_date, end_date=end_date)
        if df.empty:
            return []
        df = df[df["is_open"] == 1].sort_values("cal_date")
        return df["cal_date"].tolist()

    # ----------------------------------------------------------
    # 4~7. 财报四表（per-ticker 多线程 + 断点续跑）
    # ----------------------------------------------------------

    def _download_financial_endpoint(
        self, method: str, model_class, table_name: str,
        unique_keys: list[str], tickers: list[str] | None = None,
    ) -> int:
        """
        通用财报端点下载（对标 us_bulk.py 的 download_fmp_financial_quarterly 模式）。

        per-ticker 多线程 + _mark_done 断点 + 增量 stale 判断。
        """
        if tickers is None:
            tickers = self._get_tickers(active_only=True)
        if not tickers:
            logger.warning(f"{method}: ticker 列表空")
            return 0

        latest_map = None
        if self._incremental:
            date_field = "ann_date" if "ann_date" in [f.column for f in model_class._meta.get_fields() if hasattr(f, "column")] else "end_date"
            latest_map = self._get_ticker_latest(model_class, date_field)
            logger.info(f"增量 {table_name}: {len(tickers)} tickers, DB 已有 {len(latest_map)}")
        else:
            tickers = self._skip_done_tickers(table_name, tickers)
            if not tickers:
                return 0

        logger.info(f"下载 {method}: {len(tickers)} 只, {self._workers} 线程")
        total = 0

        def _fetch(ts_code: str) -> int:
            if latest_map is not None and self._ticker_needs_update(ts_code, latest_map, stale_days=30) is None:
                return 0
            try:
                df = _tushare_call(self._pro, method, self._limiter, ts_code=ts_code)
            except Exception as e:
                logger.warning(f"{method}({ts_code}) 失败: {e}")
                return 0
            if df.empty:
                return 0
            _date_col_to_date(df, ("ann_date", "f_ann_date", "end_date"))
            df = df.drop_duplicates(subset=unique_keys, keep="last")
            self._um.upsert_df(model_class, df, unique_keys)
            return len(df)

        with ThreadPoolExecutor(max_workers=self._workers) as pool:
            futures = {pool.submit(self._mark_done, table_name, _fetch, t): t for t in tickers}
            for f in tqdm(as_completed(futures), total=len(futures), desc=f"Tushare {method}"):
                try:
                    total += f.result()
                except Exception as e:
                    logger.warning(f"{method} 失败 {futures[f]}: {e}")

        self._um.flush()
        logger.info(f"{method} 完成: {total} 条")
        return total

    def download_tushare_income(self, tickers: list[str] | None = None) -> int:
        return self._download_financial_endpoint(
            "income", AFinancialIncome, "a_financial_income",
            ["ts_code", "end_date", "report_type"], tickers,
        )

    def download_tushare_balancesheet(self, tickers: list[str] | None = None) -> int:
        return self._download_financial_endpoint(
            "balancesheet", AFinancialBalance, "a_financial_balance",
            ["ts_code", "end_date", "report_type"], tickers,
        )

    def download_tushare_cashflow(self, tickers: list[str] | None = None) -> int:
        return self._download_financial_endpoint(
            "cashflow", AFinancialCashflow, "a_financial_cashflow",
            ["ts_code", "end_date", "report_type"], tickers,
        )

    def download_tushare_fina_indicator(self, tickers: list[str] | None = None) -> int:
        return self._download_financial_endpoint(
            "fina_indicator", AFinancialIndicator, "a_financial_indicator",
            ["ts_code", "end_date"], tickers,
        )

    # ----------------------------------------------------------
    # 8. industry — 行业分类（申万 L1 + L2，批量非 per-ticker）
    # ----------------------------------------------------------

    def download_tushare_industry(self, src: str = "SW2021") -> int:
        logger.info(f"下载行业分类 {src}")
        total = 0
        for level in ("L1", "L2"):
            df_idx = _tushare_call(self._pro, "index_classify", self._limiter, level=level, src=src)
            if df_idx.empty:
                logger.warning(f"index_classify {src} {level} 空")
                continue
            logger.info(f"{src} {level}: {len(df_idx)} 个行业")
            for _, row in tqdm(df_idx.iterrows(), total=len(df_idx), desc=f"{src}-{level}"):
                index_code = row["index_code"]
                index_name = row.get("industry_name") or row.get("name")
                try:
                    df_mem = _tushare_call(self._pro, "index_member", self._limiter, index_code=index_code)
                except Exception as e:
                    logger.warning(f"index_member {index_code} 失败: {e}")
                    continue
                if df_mem.empty:
                    continue
                df_mem = df_mem.rename(columns={"con_code": "ts_code"})
                df_mem["src"] = src
                df_mem["level"] = level
                df_mem["index_code"] = index_code
                df_mem["index_name"] = index_name
                for col in ("in_date", "out_date"):
                    if col in df_mem.columns:
                        df_mem[col] = pd.to_datetime(df_mem[col], errors="coerce")
                        df_mem[col] = df_mem[col].apply(lambda x: x.date() if pd.notna(x) else None)
                keep = ["ts_code", "src", "level", "index_code", "index_name", "in_date", "out_date", "is_new"]
                df_mem = df_mem[[c for c in keep if c in df_mem.columns]]
                self._um.upsert_df(AIndustryClass, df_mem, ["ts_code", "src", "level"])
                total += len(df_mem)
        self._um.flush()
        logger.info(f"行业分类完成: {total} 条")
        return total

    # ----------------------------------------------------------
    # 9. index — 指数日线（多线程 per-index）
    # ----------------------------------------------------------

    def download_tushare_index(self, start_date: str | None = None) -> int:
        from services.config import INDUSTRY_INDEX_MAP

        sd = start_date or self._sd
        end_date = datetime.now().strftime("%Y%m%d")

        index_codes = ["000300.SH", "000001.SH", "399001.SZ", "399006.SZ", "000688.SH"]
        if INDUSTRY_INDEX_MAP:
            index_codes.extend(INDUSTRY_INDEX_MAP.values())
        index_codes = list(dict.fromkeys(index_codes))

        logger.info(f"下载指数日线: {len(index_codes)} 个, {sd}~{end_date}, {self._workers} 线程")
        total = 0

        def _fetch(code: str) -> int:
            method = "sw_daily" if code.endswith(".SI") else "index_daily"
            df = _tushare_call(self._pro, method, self._limiter,
                               ts_code=code, start_date=sd, end_date=end_date)
            if df.empty:
                logger.debug(f"指数 {code} 无数据")
                return 0
            df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.date
            self._um.upsert_df(AIndexDaily, df, ["ts_code", "trade_date"])
            return len(df)

        with ThreadPoolExecutor(max_workers=self._workers) as pool:
            futures = {pool.submit(_fetch, c): c for c in index_codes}
            for f in tqdm(as_completed(futures), total=len(futures), desc="Tushare Index"):
                try:
                    total += f.result()
                except Exception as e:
                    logger.warning(f"指数 {futures[f]} 失败: {e}")

        self._um.flush()
        logger.info(f"指数日线完成: {total} 条")
        return total

    # ----------------------------------------------------------
    # 10. commodity — 商品期货主力合约（多线程 per-symbol）
    # ----------------------------------------------------------

    def download_tushare_commodity(self, start_date: str | None = None) -> int:
        sd = start_date or self._sd
        end_date = datetime.now().strftime("%Y%m%d")
        symbols = COMMODITY_SYMBOLS

        logger.info(f"下载商品期货: {len(symbols)} 品种, {sd}~{end_date}")
        total = 0

        def _fetch(symbol: str) -> int:
            exchange = COMMODITY_EXCHANGE_MAP.get(symbol)
            if not exchange:
                logger.warning(f"{symbol}: 无交易所映射")
                return 0
            mapping = _tushare_call(self._pro, "fut_mapping", self._limiter,
                                    ts_code=f"{symbol}.{exchange}",
                                    start_date=sd, end_date=end_date)
            if mapping.empty:
                logger.debug(f"{symbol}: fut_mapping 空")
                return 0
            mapping["trade_date"] = pd.to_datetime(mapping["trade_date"])
            mapping = mapping.sort_values("trade_date")
            all_dfs = []
            for contract_code, grp in mapping.groupby("mapping_ts_code"):
                seg_start = grp["trade_date"].min().strftime("%Y%m%d")
                seg_end = grp["trade_date"].max().strftime("%Y%m%d")
                daily = _tushare_call(self._pro, "fut_daily", self._limiter,
                                      ts_code=contract_code, start_date=seg_start, end_date=seg_end)
                if daily.empty:
                    continue
                daily["trade_date"] = pd.to_datetime(daily["trade_date"])
                valid_dates = set(grp["trade_date"].tolist())
                daily = daily[daily["trade_date"].isin(valid_dates)]
                if not daily.empty:
                    all_dfs.append(daily)
            if not all_dfs:
                return 0
            result = pd.concat(all_dfs, ignore_index=True)
            result = result.drop_duplicates(subset=["trade_date"], keep="first").sort_values("trade_date")
            result["ts_code"] = f"{symbol}.{exchange}"
            result["name"] = symbol
            result["trade_date"] = result["trade_date"].dt.date
            self._um.upsert_df(ACommodityPrice, result, ["ts_code", "trade_date"])
            return len(result)

        with ThreadPoolExecutor(max_workers=min(self._workers, 5)) as pool:
            futures = {pool.submit(_fetch, s): s for s in symbols}
            for f in tqdm(as_completed(futures), total=len(futures), desc="Tushare Commodity"):
                try:
                    total += f.result()
                except Exception as e:
                    logger.warning(f"商品 {futures[f]} 失败: {e}")

        self._um.flush()
        logger.info(f"商品期货完成: {total} 条")
        return total

    # ----------------------------------------------------------
    # 11. macro — 宏观指标（8 子端点，小数据量）
    # ----------------------------------------------------------

    def download_tushare_macro(self, start_date: str | None = None) -> dict:
        sd = start_date or self._sd
        end_date = datetime.now().strftime("%Y%m%d")
        logger.info(f"下载宏观指标 {sd}~{end_date}")
        results = {}
        for key, fn in [
            ("shibor", self._dl_shibor),
            ("lpr", self._dl_lpr),
            ("cpi", self._dl_cpi),
            ("ppi", self._dl_ppi),
            ("pmi", self._dl_pmi),
            ("money", self._dl_money),
            ("gdp", self._dl_gdp),
            ("us_tycr", self._dl_us_tycr),
        ]:
            try:
                results[key] = fn(sd, end_date)
                logger.info(f"  {key}: {results[key]} 条")
            except Exception as e:
                logger.warning(f"  {key} 失败: {e}")
                results[key] = 0
        self._um.flush()
        logger.info(f"宏观指标完成: {sum(results.values())} 条")
        return results

    def _flush_macro(self, records: list[dict]) -> int:
        if not records:
            return 0
        df = pd.DataFrame(records)
        if "report_date" in df.columns:
            df["report_date"] = pd.to_datetime(df["report_date"]).dt.date
        self._um.upsert_df(AMacroIndicator, df, ["indicator", "report_date", "freq"])
        return len(df)

    def _month_to_date(self, m):
        return (pd.to_datetime(str(m) + "01") + pd.offsets.MonthEnd(0))

    def _dl_shibor(self, sd, ed):
        df = _tushare_call(self._pro, "shibor", self._limiter, start_date=sd, end_date=ed)
        if df.empty: return 0
        recs = []
        for _, r in df.iterrows():
            d = r["date"]
            if pd.notna(r.get("3m")):
                recs.append({"indicator": "SHIBOR_3M", "report_date": d, "freq": "D", "value": float(r["3m"])})
            if pd.notna(r.get("on")):
                recs.append({"indicator": "SHIBOR_ON", "report_date": d, "freq": "D", "value": float(r["on"])})
        return self._flush_macro(recs)

    def _dl_lpr(self, sd, ed):
        df = _tushare_call(self._pro, "shibor_lpr", self._limiter, start_date=sd, end_date=ed)
        if df.empty: return 0
        recs = []
        for _, r in df.iterrows():
            d = r["date"]
            for ind, col in [("LPR_1Y", "1y"), ("LPR_5Y", "5y")]:
                if col in df.columns and pd.notna(r.get(col)):
                    recs.append({"indicator": ind, "report_date": d, "freq": "D", "value": float(r[col])})
        return self._flush_macro(recs)

    def _dl_cpi(self, sd, ed):
        df = _tushare_call(self._pro, "cn_cpi", self._limiter, start_m=sd[:6], end_m=ed[:6])
        if df.empty: return 0
        recs = []
        for _, r in df.iterrows():
            d = self._month_to_date(r["month"])
            for ind, col in [("CPI_YOY", "nt_yoy"), ("CPI_MOM", "nt_mom")]:
                if col in df.columns and pd.notna(r.get(col)):
                    recs.append({"indicator": ind, "report_date": d, "freq": "M", "value": float(r[col])})
        return self._flush_macro(recs)

    def _dl_ppi(self, sd, ed):
        df = _tushare_call(self._pro, "cn_ppi", self._limiter, start_m=sd[:6], end_m=ed[:6])
        if df.empty: return 0
        recs = []
        for _, r in df.iterrows():
            d = self._month_to_date(r["month"])
            for ind, col in [("PPI_YOY", "ppi_yoy"), ("PPI_MP_YOY", "ppi_mp_yoy")]:
                if col in df.columns and pd.notna(r.get(col)):
                    recs.append({"indicator": ind, "report_date": d, "freq": "M", "value": float(r[col])})
        return self._flush_macro(recs)

    def _dl_pmi(self, sd, ed):
        df = _tushare_call(self._pro, "cn_pmi", self._limiter, start_m=sd[:6], end_m=ed[:6])
        if df.empty: return 0
        recs = []
        for _, r in df.iterrows():
            d = self._month_to_date(r["month"])
            for ind, col in [("PMI_MFG", "pmi010000"), ("PMI_NONMFG", "pmi020100")]:
                if col in df.columns and pd.notna(r.get(col)):
                    recs.append({"indicator": ind, "report_date": d, "freq": "M", "value": float(r[col])})
        return self._flush_macro(recs)

    def _dl_money(self, sd, ed):
        df = _tushare_call(self._pro, "cn_m", self._limiter, start_m=sd[:6], end_m=ed[:6])
        if df.empty: return 0
        recs = []
        for _, r in df.iterrows():
            d = self._month_to_date(r["month"])
            for ind, col in [("M1_YOY", "m1_yoy"), ("M2_YOY", "m2_yoy")]:
                if col in df.columns and pd.notna(r.get(col)):
                    recs.append({"indicator": ind, "report_date": d, "freq": "M", "value": float(r[col])})
        return self._flush_macro(recs)

    def _dl_gdp(self, sd, ed):
        df = _tushare_call(self._pro, "cn_gdp", self._limiter)
        if df.empty: return 0
        recs = []
        for _, r in df.iterrows():
            q = str(r.get("quarter", ""))
            if len(q) != 6 or q[4] != "Q":
                continue
            month = {1: 3, 2: 6, 3: 9, 4: 12}.get(int(q[5]))
            if month is None:
                continue
            d = pd.Timestamp(year=int(q[:4]), month=month, day=1) + pd.offsets.MonthEnd(0)
            if d < pd.Timestamp(sd) or d > pd.Timestamp(ed):
                continue
            if pd.notna(r.get("gdp_yoy")):
                recs.append({"indicator": "GDP_YOY", "report_date": d, "freq": "Q", "value": float(r["gdp_yoy"])})
        return self._flush_macro(recs)

    def _dl_us_tycr(self, sd, ed):
        df = _tushare_call(self._pro, "us_tycr", self._limiter, start_date=sd, end_date=ed)
        if df.empty: return 0
        recs = []
        for _, r in df.iterrows():
            d = r["date"]
            for ind, col in [("US_TYCR_10Y", "y10"), ("US_TYCR_2Y", "y2")]:
                if pd.notna(r.get(col)):
                    recs.append({"indicator": ind, "report_date": d, "freq": "D", "value": float(r[col])})
        return self._flush_macro(recs)

    # ----------------------------------------------------------
    # AkShare 端点
    # ----------------------------------------------------------

    # ----------------------------------------------------------
    # 研报 camelCase → snake_case 全 51 列映射（类常量，不在循环内定义）
    # ----------------------------------------------------------
    _REPORT_COL_MAP = {
        "infoCode": "info_code", "stockCode": "stock_code", "stockName": "stock_name",
        "publishDate": "publish_date", "title": "title", "column": "column",
        "count": "count", "reportType": "report_type", "encodeUrl": "encode_url",
        "market": "market", "orgCode": "org_code", "orgName": "org_name",
        "orgSName": "org_s_name", "orgType": "org_type",
        "researcher": "researcher", "author": "author", "authorID": "author_id",
        "emRatingCode": "em_rating_code", "emRatingName": "em_rating_name",
        "emRatingValue": "em_rating_value", "ratingChange": "rating_change",
        "lastEmRatingCode": "last_em_rating_code", "lastEmRatingName": "last_em_rating_name",
        "lastEmRatingValue": "last_em_rating_value",
        "sRatingCode": "s_rating_code", "sRatingName": "s_rating_name",
        "industryCode": "industry_code", "industryName": "industry_name",
        "emIndustryCode": "em_industry_code",
        "indvInduCode": "indv_indu_code", "indvInduName": "indv_indu_name",
        "indvIsNew": "indv_is_new",
        "indvAimPriceT": "indv_aim_price_t", "indvAimPriceL": "indv_aim_price_l",
        "predictThisYearEps": "predict_this_year_eps", "predictThisYearPe": "predict_this_year_pe",
        "predictNextYearEps": "predict_next_year_eps", "predictNextYearPe": "predict_next_year_pe",
        "predictNextTwoYearEps": "predict_next_two_year_eps", "predictNextTwoYearPe": "predict_next_two_year_pe",
        "predictLastYearEps": "predict_last_year_eps", "predictLastYearPe": "predict_last_year_pe",
        "actualLastYearEps": "actual_last_year_eps", "actualLastTwoYearEps": "actual_last_two_year_eps",
        "newPurchaseDate": "new_purchase_date", "newListingDate": "new_listing_date",
        "newIssuePrice": "new_issue_price", "newPeIssueA": "new_pe_issue_a",
        "attachType": "attach_type", "attachSize": "attach_size", "attachPages": "attach_pages",
    }
    _REPORT_FLOAT_COLS = (
        "em_rating_value", "last_em_rating_value", "indv_aim_price_t", "indv_aim_price_l",
        "predict_this_year_eps", "predict_this_year_pe", "predict_next_year_eps", "predict_next_year_pe",
        "predict_next_two_year_eps", "predict_next_two_year_pe",
        "predict_last_year_eps", "predict_last_year_pe",
        "actual_last_year_eps", "actual_last_two_year_eps", "new_issue_price", "new_pe_issue_a",
    )
    _REPORT_API_URL = "https://reportapi.eastmoney.com/report/list"

    def download_akshare_research_reports(self, begin_time: str = "2000-01-01", force: bool = True) -> int:
        """
        东方财富研报 API → a_research_report（全 51 列）。

        全量模式（force=True）：多线程按 page 并行下载。
        增量模式（force=False）：串行下载，连续 3 页无变更提前终止。
        """
        end_time = f"{datetime.now().year + 1}-01-01"
        base_params = {
            "industryCode": "*", "pageSize": "5000", "industry": "*",
            "rating": "*", "ratingChange": "*",
            "beginTime": begin_time, "endTime": end_time,
            "pageNo": "1", "fields": "", "qType": "0",
            "orgCode": "", "code": "", "rcode": "",
            "p": "1", "pageNum": "1", "pageNumber": "1",
        }
        try:
            r = requests.get(self._REPORT_API_URL, params=base_params, timeout=30)
            data = r.json()
        except Exception as e:
            logger.error(f"研报 API 失败: {e}")
            return 0

        total_page = data.get("TotalPage", 0)
        if total_page == 0:
            logger.warning("研报 API 空")
            return 0

        logger.info(f"研报: {total_page} 页, {'全量多线程' if force else '增量串行'}, {self._workers} 线程")

        def _fetch_page(page: int) -> int:
            """下载单页研报，返回写入记录数。"""
            p = dict(base_params)
            p.update({"pageNo": str(page), "p": str(page), "pageNum": str(page), "pageNumber": str(page)})
            try:
                resp = requests.get(self._REPORT_API_URL, params=p, timeout=30)
                items = resp.json().get("data", [])
            except Exception as e:
                logger.warning(f"研报第 {page} 页失败: {e}")
                return 0
            if not items:
                return 0

            df = pd.DataFrame(items)
            if "stockCode" not in df.columns:
                return 0

            df = df.rename(columns=self._REPORT_COL_MAP)
            # ts_code
            if "stock_code" in df.columns:
                df["ts_code"] = df["stock_code"].apply(
                    lambda c: self._code_to_ts(str(c)) if pd.notna(c) else None
                )
            # publish_date → timezone-aware
            if "publish_date" in df.columns:
                df["publish_date"] = pd.to_datetime(df["publish_date"], errors="coerce", utc=True)
            # float 安全转换
            for fc in self._REPORT_FLOAT_COLS:
                if fc in df.columns:
                    df[fc] = df[fc].apply(_safe_float)
            # 过滤无 info_code
            if "info_code" in df.columns:
                df = df[df["info_code"].notna() & (df["info_code"] != "")]
            if df.empty:
                return 0

            records = df.to_dict("records")
            self._um.upsert(AResearchReport, records, ["info_code"])
            return len(records)

        total = 0
        if force:
            # 全量：多线程按 page 并行
            with ThreadPoolExecutor(max_workers=self._workers) as pool:
                futures = {pool.submit(_fetch_page, p): p for p in range(1, total_page + 1)}
                for f in tqdm(as_completed(futures), total=len(futures), desc="AkShare Research"):
                    try:
                        total += f.result()
                    except Exception as e:
                        logger.warning(f"研报 page {futures[f]} 失败: {e}")
        else:
            # 增量：串行 + early termination
            no_change_streak = 0
            for page in tqdm(range(1, total_page + 1), desc="AkShare Research (incr)"):
                if no_change_streak >= 3:
                    logger.info(f"连续 {no_change_streak} 页无变更，终止")
                    break
                n = _fetch_page(page)
                if n > 0:
                    total += n
                    no_change_streak = 0
                else:
                    no_change_streak += 1

        self._um.flush()
        logger.info(f"研报完成: {total} 条")
        return total

    # ----------------------------------------------------------
    # 高管持股变动 col_map（AkShare stock_ggcg_em 实际返回列，从源码 inspect 确认）
    # ----------------------------------------------------------
    _INSIDER_COL_MAP = {
        "代码": "stock_code", "名称": "name", "股东名称": "holder_name",
        "持股变动信息-增减": "holder_type", "持股变动信息-变动数量": "change_vol",
        "持股变动信息-占总股本比例": "change_ratio", "持股变动信息-占流通股比例": "hold_ratio",
        "变动后持股情况-持股总数": "total_share", "变动后持股情况-占总股本比例": "total_share_ratio",
        "变动后持股情况-持流通股数": "float_share", "变动后持股情况-占流通股比例": "float_share_ratio",
        "变动开始日": "change_date", "变动截止日": "change_end_date",
        "公告日": "ann_date", "最新价": "latest_price", "涨跌幅": "change_pct",
    }
    _INSIDER_FLOAT_COLS = (
        "change_vol", "change_ratio", "hold_ratio", "total_share",
        "total_share_ratio", "float_share", "float_share_ratio",
        "latest_price", "change_pct",
    )

    def download_akshare_insider(self) -> int:
        """
        AkShare 高管持股变动 → a_insider_transaction（全 16 列）。

        注意：stock_ggcg_em 内部遍历 ~288 个子页面，单次调用耗时 ~5-15 分钟。
        AkShare 自身已并行处理子页面，外层无法再并行。
        """
        try:
            import akshare as ak
        except ImportError:
            raise ImportError("akshare 未安装，pip install akshare")

        logger.info("下载高管持股变动 (AkShare stock_ggcg_em)...")
        try:
            df = ak.stock_ggcg_em(symbol="全部")
        except Exception as e:
            logger.error(f"stock_ggcg_em 失败: {e}")
            return 0
        if df is None or df.empty:
            logger.warning("高管持股变动空")
            return 0

        logger.info(f"stock_ggcg_em 返回 {len(df)} 行, 列: {list(df.columns)}")
        df = df.rename(columns={k: v for k, v in self._INSIDER_COL_MAP.items() if k in df.columns})
        if "stock_code" not in df.columns:
            logger.warning(f"stock_ggcg_em 列名不匹配: {list(df.columns)}")
            return 0

        df["ts_code"] = df["stock_code"].apply(self._code_to_ts)
        for col in ("change_date", "change_end_date", "ann_date"):
            if col in df.columns:
                df[col] = pd.to_datetime(df[col], errors="coerce").dt.date
        for col in self._INSIDER_FLOAT_COLS:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")

        required = ["ts_code", "change_date", "holder_name"]
        missing = [c for c in required if c not in df.columns]
        if missing:
            logger.warning(f"缺少必要列 {missing}，无法 dropna")
            return 0
        df = df.dropna(subset=required)
        if df.empty:
            logger.warning("清洗后空")
            return 0

        self._um.upsert_df(AInsiderTrade, df, ["ts_code", "change_date", "holder_name"])
        self._um.flush()
        logger.info(f"高管持股变动: {len(df)} 条")
        return len(df)

    @staticmethod
    def _code_to_ts(code) -> str:
        """纯数字股票代码 → ts_code（6 开头 .SH，其他 .SZ）。"""
        code = str(code).strip().zfill(6)
        return f"{code}.SH" if code.startswith("6") else f"{code}.SZ"

    # ----------------------------------------------------------
    # 全量入口
    # ----------------------------------------------------------

    def download_tushare_all(self, start_date: str | None = None) -> dict:
        """Tushare 全量下载（按 phase 顺序）。"""
        sd = start_date or self._sd
        results = {}
        t0 = time.time()

        logger.info("=== Phase 1: 基础元数据 ===")
        results["stock_list"] = self.download_tushare_stock_list()
        results["trade_cal"] = self.download_tushare_trade_cal(sd)

        logger.info("=== Phase 2: 日线行情 + 指数 ===")
        results["prices"] = self.download_tushare_daily_prices(sd)
        results["index"] = self.download_tushare_index(sd)

        logger.info("=== Phase 3: 财报四表（per-ticker 多线程 + 断点续跑）===")
        results["income"] = self.download_tushare_income()
        results["balancesheet"] = self.download_tushare_balancesheet()
        results["cashflow"] = self.download_tushare_cashflow()
        results["fina_indicator"] = self.download_tushare_fina_indicator()

        logger.info("=== Phase 4: 行业 / 商品 / 宏观 ===")
        results["industry"] = self.download_tushare_industry()
        results["commodity"] = self.download_tushare_commodity(sd)
        results["macro"] = self.download_tushare_macro(sd)

        elapsed = time.time() - t0
        logger.info(f"Tushare 全量完成: {elapsed:.0f}s — {results}")
        return results

    def download_akshare_all(self) -> dict:
        """AkShare 全量。"""
        results = {}
        t0 = time.time()
        logger.info("=== AkShare 全量 ===")
        results["research_report"] = self.download_akshare_research_reports()
        results["insider"] = self.download_akshare_insider()
        elapsed = time.time() - t0
        logger.info(f"AkShare 全量完成: {elapsed:.0f}s — {results}")
        return results
