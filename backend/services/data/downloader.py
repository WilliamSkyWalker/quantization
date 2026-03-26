"""
Tushare Pro 数据下载模块

负责从 Tushare Pro 获取以下数据并存入 MySQL：
    1. 全市场股票列表（沪深A股，含上市日期、退市日期、是否ST）
    2. 日线行情（未复权 OHLCV + 换手率 + 复权因子 + 涨跌停标记）

与 AkShare 方案的关键差异：
    - 股票列表：pro.stock_basic 2 次请求搞定（在退 + 已退市）
    - 日线行情：按交易日获取全市场（daily + daily_basic + adj_factor），
      无需逐只股票请求，10 年约 7500 次 vs 原 5000+ 次
    - 存储未复权价 + 复权因子，后续查询时动态计算前复权，
      避免分红送股导致的全量刷新问题
"""

import collections
import logging
import time
import threading
from datetime import datetime
from typing import Optional

import numpy as np
import pandas as pd
import tushare as ts
from tqdm import tqdm

from backend.services.config import (
    DATA_START_DATE,
    TUSHARE_TOKEN,
    TUSHARE_RATE_LIMIT,
    TUSHARE_RETRY_WAIT,
    TUSHARE_MAX_RETRIES,
    ST_KEYWORDS,
    LOG_LEVEL,
)
from backend.services.data.database import DatabaseManager

# ============================================================
# 日志配置
# ============================================================

logger = logging.getLogger(__name__)
logger.setLevel(LOG_LEVEL)


# ============================================================
# 工具函数
# ============================================================

def _detect_board(ts_code: str) -> str:
    """根据 ts_code 判断板块（用于涨跌停阈值）。"""
    code = ts_code.split(".")[0]
    if code.startswith("00"):
        return "主板"
    elif code.startswith("30"):
        return "创业板"
    elif code.startswith("60"):
        return "主板"
    elif code.startswith("68"):
        return "科创板"
    return "其他"


def _is_limit(pct_chg: float, board: str, direction: str) -> bool:
    """
    判断是否涨跌停。

    A股涨跌停规则：
        - 主板/中小板：±10%
        - 创业板/科创板：±20%
    """
    if pd.isna(pct_chg):
        return False

    limit = 20.0 if board in ("创业板", "科创板") else 10.0
    threshold = limit - 0.05  # 留微小容差

    if direction == "up":
        return pct_chg >= threshold
    else:
        return pct_chg <= -threshold


# ============================================================
# Tushare 限速器
# ============================================================

class TushareRateLimiter:
    """
    滑动窗口限速器（线程安全）。

    在一个 60 秒窗口内最多允许 max_per_min 次请求。
    超出时自动 sleep 到窗口内最早请求过期。
    """

    def __init__(self, max_per_min: int = TUSHARE_RATE_LIMIT):
        self.max_per_min = max_per_min
        self._timestamps: collections.deque = collections.deque()
        self._lock = threading.Lock()

    def acquire(self):
        """在发起请求前调用，自动等待直到不超限。"""
        with self._lock:
            now = time.monotonic()
            # 清除 60 秒之前的记录
            while self._timestamps and now - self._timestamps[0] >= 60.0:
                self._timestamps.popleft()

            if len(self._timestamps) >= self.max_per_min:
                # 等到最早的记录过期
                wait = 60.0 - (now - self._timestamps[0]) + 0.1
                if wait > 0:
                    logger.debug(f"限速等待 {wait:.1f}s")
                    time.sleep(wait)
                # 清理过期
                now = time.monotonic()
                while self._timestamps and now - self._timestamps[0] >= 60.0:
                    self._timestamps.popleft()

            self._timestamps.append(time.monotonic())


def _tushare_call(pro, method: str, limiter: TushareRateLimiter, **kwargs) -> pd.DataFrame:
    """
    统一 Tushare API 调用入口，含限速 + 重试。

    Args:
        pro: tushare pro_api 实例。
        method: API 方法名，如 "daily"、"stock_basic"。
        limiter: TushareRateLimiter 实例。
        **kwargs: 传递给 API 的参数。

    Returns:
        API 返回的 DataFrame。

    Raises:
        RuntimeError: 超过最大重试次数。
    """
    api_func = getattr(pro, method)
    for attempt in range(1, TUSHARE_MAX_RETRIES + 1):
        limiter.acquire()
        try:
            df = api_func(**kwargs)
            return df if df is not None else pd.DataFrame()
        except Exception as e:
            err_msg = str(e).lower()
            if "最多访问" in err_msg or "exceed" in err_msg or "freq" in err_msg:
                logger.warning(f"{method} 触发限流 (第{attempt}次)，等待 {TUSHARE_RETRY_WAIT}s")
                time.sleep(TUSHARE_RETRY_WAIT)
            else:
                if attempt < TUSHARE_MAX_RETRIES:
                    logger.warning(f"{method} 调用失败 (第{attempt}次): {e}")
                    time.sleep(2)
                else:
                    raise RuntimeError(f"{method}({kwargs}) 重试 {TUSHARE_MAX_RETRIES} 次后仍失败: {e}")
    return pd.DataFrame()


def _group_consecutive_dates(dates: list[str]) -> list[tuple[str, str]]:
    """
    将有序的 YYYYMMDD 日期列表分组为连续交易日区间。

    连续的定义：相邻日期差 <= 5 天（跨周末/节假日仍视为连续）。

    Args:
        dates: 已排序的 YYYYMMDD 字符串列表。

    Returns:
        [(start, end), ...] 元组列表。
    """
    if not dates:
        return []
    groups = []
    start = dates[0]
    prev = pd.to_datetime(dates[0])
    for d in dates[1:]:
        cur = pd.to_datetime(d)
        if (cur - prev).days > 5:
            groups.append((start, prev.strftime('%Y%m%d')))
            start = d
        prev = cur
    groups.append((start, prev.strftime('%Y%m%d')))
    return groups


# ============================================================
# 数据下载器
# ============================================================

class TushareDownloader:
    """
    Tushare Pro 数据下载器

    用法:
        db = DatabaseManager()
        db.init_tables()
        downloader = TushareDownloader(db)
        downloader.download_stock_list()
        downloader.download_daily_prices()
    """

    def __init__(self, db: DatabaseManager):
        self.db = db
        self.pro = ts.pro_api(TUSHARE_TOKEN)
        self.limiter = TushareRateLimiter()

    # ----------------------------------------------------------
    # 股票列表下载
    # ----------------------------------------------------------

    def download_stock_list(self) -> pd.DataFrame:
        """
        下载沪深A股全市场股票列表并存入数据库。

        使用 Tushare pro.stock_basic：
            - list_status='L' 获取上市中股票
            - list_status='D' 获取已退市股票
        合计 2 次请求即可获取全部股票 + 上市日期 + 退市日期。

        Returns:
            处理后的股票列表 DataFrame。
        """
        logger.info("开始下载股票列表...")

        # 获取上市中 + 已退市股票（不传 exchange，返回全部交易所）
        fields = "ts_code,name,market,list_date,delist_date"
        df_listed = _tushare_call(self.pro, "stock_basic", self.limiter,
                                  list_status="L", fields=fields)
        df_delisted = _tushare_call(self.pro, "stock_basic", self.limiter,
                                    list_status="D", fields=fields)

        df = pd.concat([df_listed, df_delisted], ignore_index=True)

        if df.empty:
            logger.error("Tushare stock_basic 返回为空，请检查 TOKEN")
            raise ValueError("stock_basic 返回为空")

        # 筛选沪深两市（排除北交所 .BJ）
        df = df[df["ts_code"].str.match(r"^(00|30|60|68)")].copy()

        logger.info(f"获取到 {len(df)} 只沪深A股")

        # 板块映射
        df["market"] = df["ts_code"].apply(_detect_board)

        # 日期转换（NaT → None，MySQL 不接受 NaT）
        df["list_date"] = pd.to_datetime(df["list_date"], errors="coerce")
        df["delist_date"] = pd.to_datetime(df["delist_date"], errors="coerce")
        df["list_date"] = df["list_date"].apply(lambda x: x.date() if pd.notna(x) else None)
        df["delist_date"] = df["delist_date"].apply(lambda x: x.date() if pd.notna(x) else None)

        # ST 标记
        df["is_st"] = df["name"].apply(
            lambda n: 1 if any(kw in str(n) for kw in ST_KEYWORDS) else 0
        )

        # 写入数据库
        write_cols = ["ts_code", "name", "market", "list_date", "delist_date", "is_st"]
        self.db.upsert_stock_basic(df[write_cols])

        logger.info(
            f"股票列表下载完成: {len(df)} 只"
            f"（ST: {df['is_st'].sum()} 只，退市: {df['delist_date'].notna().sum()} 只）"
        )
        return df

    # ----------------------------------------------------------
    # 日线行情下载（按交易日）
    # ----------------------------------------------------------

    def download_daily_prices(
        self,
        start_date: str = DATA_START_DATE,
        end_date: Optional[str] = None,
    ) -> int:
        """
        下载全市场日线行情（按交易日遍历）。

        每个交易日 3 次请求：
            - pro.daily(trade_date=d)       → 全市场 OHLCV + 涨跌幅
            - pro.daily_basic(trade_date=d)  → 换手率
            - pro.adj_factor(trade_date=d)   → 复权因子

        Args:
            start_date: 起始日期 YYYYMMDD。
            end_date: 结束日期 YYYYMMDD，默认今天。

        Returns:
            成功下载的交易日数量。
        """
        if end_date is None:
            end_date = datetime.now().strftime("%Y%m%d")

        # 获取交易日历
        trade_dates = self._get_trade_dates(start_date, end_date)
        if not trade_dates:
            logger.warning(f"区间 {start_date}~{end_date} 无交易日")
            return 0

        total = len(trade_dates)
        logger.info(
            f"开始下载日线行情: {total} 个交易日, "
            f"区间 {start_date} ~ {end_date}"
        )

        success_count = 0
        total_records = 0

        for trade_date in tqdm(trade_dates, desc="下载日线行情"):
            try:
                df = self._fetch_daily_by_date(trade_date)
                if df is not None and not df.empty:
                    self.db.bulk_upsert_daily_price(df)
                    success_count += 1
                    total_records += len(df)
            except Exception as e:
                logger.warning(f"交易日 {trade_date} 下载失败: {e}")

        logger.info(
            f"日线行情下载完成: 成功 {success_count}/{total} 个交易日, "
            f"共 {total_records} 条记录"
        )
        return success_count

    def _get_trade_dates(self, start_date: str, end_date: str) -> list[str]:
        """获取区间内的交易日列表（YYYYMMDD 字符串）。"""
        df = _tushare_call(self.pro, "trade_cal", self.limiter,
                           exchange="SSE", start_date=start_date, end_date=end_date,
                           fields="cal_date,is_open")
        if df.empty:
            return []
        df = df[df["is_open"] == 1].sort_values("cal_date")
        return df["cal_date"].tolist()

    def _fetch_daily_by_date(self, trade_date: str) -> Optional[pd.DataFrame]:
        """
        获取单个交易日的全市场日线数据。

        合并 daily + daily_basic + adj_factor 三个接口的数据，
        计算涨跌停标记后返回。

        Args:
            trade_date: 交易日期 YYYYMMDD。

        Returns:
            处理后的 DataFrame，失败返回 None。
        """
        # 1. 日线行情（OHLCV + 涨跌幅）
        df_daily = _tushare_call(self.pro, "daily", self.limiter, trade_date=trade_date)
        if df_daily.empty:
            return None

        # 2. 每日指标（换手率、估值、市值等）
        df_basic = _tushare_call(self.pro, "daily_basic", self.limiter,
                                 trade_date=trade_date,
                                 fields="ts_code,trade_date,turnover_rate,dv_ttm,"
                                        "pe_ttm,pb,ps_ttm,total_mv,circ_mv,"
                                        "turnover_rate_f,volume_ratio")

        # 3. 复权因子
        df_adj = _tushare_call(self.pro, "adj_factor", self.limiter,
                               trade_date=trade_date, fields="ts_code,trade_date,adj_factor")

        # 筛选沪深两市
        df_daily = df_daily[df_daily["ts_code"].str.match(r"^(00|30|60|68)")].copy()
        if df_daily.empty:
            return None

        # 合并每日指标（换手率、估值、市值等）
        if not df_basic.empty:
            basic_cols = ["ts_code"]
            for col in ["turnover_rate", "dv_ttm", "pe_ttm", "pb", "ps_ttm",
                         "total_mv", "circ_mv", "turnover_rate_f", "volume_ratio"]:
                if col in df_basic.columns:
                    basic_cols.append(col)
            df_daily = df_daily.merge(
                df_basic[basic_cols],
                on="ts_code", how="left",
            )

        # 合并复权因子
        if not df_adj.empty:
            df_daily = df_daily.merge(
                df_adj[["ts_code", "adj_factor"]],
                on="ts_code", how="left",
            )

        # 涨跌停标记
        df_daily["board"] = df_daily["ts_code"].apply(_detect_board)
        df_daily["is_limit_up"] = df_daily.apply(
            lambda r: 1 if _is_limit(r["pct_chg"], r["board"], "up") else 0, axis=1
        )
        df_daily["is_limit_down"] = df_daily.apply(
            lambda r: 1 if _is_limit(r["pct_chg"], r["board"], "down") else 0, axis=1
        )

        # 保留需要的列
        keep_cols = [
            "ts_code", "trade_date", "open", "high", "low", "close",
            "vol", "amount", "pct_chg", "turnover_rate", "adj_factor",
            "dv_ttm", "pe_ttm", "pb", "ps_ttm",
            "total_mv", "circ_mv", "turnover_rate_f", "volume_ratio",
            "is_limit_up", "is_limit_down",
        ]
        df_out = df_daily[[c for c in keep_cols if c in df_daily.columns]].copy()

        # Tushare daily 的成交量列名是 vol，统一为 volume
        if "vol" in df_out.columns:
            df_out = df_out.rename(columns={"vol": "volume"})

        return df_out

    # ----------------------------------------------------------
    # 日线行情补录
    # ----------------------------------------------------------

    def backfill_daily_prices(self) -> dict:
        """
        检测并补录缺失的日线行情交易日。

        用交易日历获取 DATA_START_DATE 至今全部交易日，
        与 DB 中 daily_price 已有的 distinct trade_date（仅股票）做差集，
        对缺失交易日逐日调用 _fetch_daily_by_date() 补录。

        Returns:
            {'missing_dates': int, 'filled': int}
        """
        end_date = datetime.now().strftime("%Y%m%d")
        all_trade_dates = set(self._get_trade_dates(DATA_START_DATE, end_date))
        if not all_trade_dates:
            logger.warning("交易日历为空")
            return {'missing_dates': 0, 'filled': 0}

        # 查 DB 中已有的交易日（排除指数，只看股票代码格式）
        df_existing = self.db.query(
            "SELECT DISTINCT trade_date FROM daily_price "
            "WHERE ts_code LIKE '00%' OR ts_code LIKE '30%' "
            "OR ts_code LIKE '60%' OR ts_code LIKE '68%'"
        )
        existing_dates = set()
        if not df_existing.empty:
            existing_dates = set(
                pd.to_datetime(df_existing['trade_date']).dt.strftime('%Y%m%d').tolist()
            )

        missing = sorted(all_trade_dates - existing_dates)
        if not missing:
            logger.info("日线行情无缺失交易日")
            return {'missing_dates': 0, 'filled': 0}

        logger.info(f"检测到 {len(missing)} 个缺失交易日，开始补录")
        filled = 0
        for trade_date in tqdm(missing, desc="补录日线行情"):
            try:
                df = self._fetch_daily_by_date(trade_date)
                if df is not None and not df.empty:
                    self.db.bulk_upsert_daily_price(df)
                    filled += 1
            except Exception as e:
                logger.warning(f"补录交易日 {trade_date} 失败: {e}")

        logger.info(f"日线行情补录完成: 缺失 {len(missing)} 日, 成功补录 {filled} 日")
        return {'missing_dates': len(missing), 'filled': filled}

    # ----------------------------------------------------------
    # daily_basic 字段补录
    # ----------------------------------------------------------

    def backfill_daily_basic(
        self,
        start_date: str = DATA_START_DATE,
        end_date: Optional[str] = None,
    ) -> dict:
        """
        补录 daily_price 中缺失的 daily_basic 字段（pe_ttm/pb/dv_ttm 等）。

        仅对 pe_ttm IS NULL 的交易日重新拉取 daily_basic 并 UPDATE，
        不重新下载 daily/adj_factor，节省 2/3 的 API 调用。

        Returns:
            {'total_dates': int, 'filled': int}
        """
        if end_date is None:
            end_date = datetime.now().strftime("%Y%m%d")

        # 找出需要补录的交易日（pe_ttm 为 NULL 的日期）
        df_missing = self.db.query(
            "SELECT DISTINCT trade_date as td "
            "FROM daily_price "
            "WHERE pe_ttm IS NULL "
            "AND ts_code LIKE '00%' "
            "AND trade_date >= :start_date AND trade_date <= :end_date "
            "ORDER BY td",
            params={"start_date": start_date, "end_date": end_date},
        )

        if df_missing.empty:
            logger.info("daily_basic 字段无需补录")
            return {'total_dates': 0, 'filled': 0}

        # 转为 YYYYMMDD 格式
        dates = pd.to_datetime(df_missing["td"]).dt.strftime("%Y%m%d").tolist()
        logger.info(f"需补录 daily_basic 的交易日: {len(dates)} 个")

        filled = 0
        basic_fields = (
            "ts_code,trade_date,turnover_rate,dv_ttm,pe_ttm,pb,ps_ttm,"
            "total_mv,circ_mv,turnover_rate_f,volume_ratio"
        )

        from sqlalchemy import text as sa_text

        for trade_date in tqdm(dates, desc="补录 daily_basic"):
            try:
                df = _tushare_call(
                    self.pro, "daily_basic", self.limiter,
                    trade_date=trade_date, fields=basic_fields,
                )
                if df.empty:
                    continue

                # 筛选沪深两市
                df = df[df["ts_code"].str.match(r"^(00|30|60|68)")].copy()
                if df.empty:
                    continue

                # 构建批量 UPDATE（通过临时 INSERT ON DUPLICATE KEY UPDATE）
                update_cols = [
                    "dv_ttm", "pe_ttm", "pb", "ps_ttm",
                    "total_mv", "circ_mv", "turnover_rate_f", "volume_ratio",
                ]
                existing_update_cols = [c for c in update_cols if c in df.columns]

                if not existing_update_cols:
                    continue

                # 转日期格式
                df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.date

                # 使用 ON DUPLICATE KEY UPDATE 只更新新字段
                col_names = ", ".join([f"`{c}`" for c in ["ts_code", "trade_date"] + existing_update_cols])
                placeholders = ", ".join([f":{c}" for c in ["ts_code", "trade_date"] + existing_update_cols])
                update_clause = ", ".join([f"`{c}` = VALUES(`{c}`)" for c in existing_update_cols])

                sql = sa_text(
                    f"INSERT INTO daily_price ({col_names}) VALUES ({placeholders}) "
                    f"ON DUPLICATE KEY UPDATE {update_clause}"
                )

                records = df[["ts_code", "trade_date"] + existing_update_cols].to_dict("records")
                # NaN → None
                for rec in records:
                    for k, v in rec.items():
                        if isinstance(v, float) and pd.isna(v):
                            rec[k] = None

                batch_size = 1000
                with self.db.engine.begin() as conn:
                    for i in range(0, len(records), batch_size):
                        batch = records[i:i + batch_size]
                        conn.execute(sql, batch)

                filled += 1

            except Exception as e:
                logger.warning(f"补录交易日 {trade_date} daily_basic 失败: {e}")

        logger.info(f"daily_basic 补录完成: {filled}/{len(dates)} 个交易日")
        return {'total_dates': len(dates), 'filled': filled}

    # ----------------------------------------------------------
    # 指数数据补录
    # ----------------------------------------------------------

    def backfill_index_daily(self) -> dict:
        """
        检测并补录缺失的指数日线数据。

        检查 000300.SH + INDUSTRY_INDEX_MAP 所有指数，
        对每个指数比较交易日历 vs DB 已有日期，
        将缺失日期分组为连续区间后调用 download_index_daily() 补录。

        Returns:
            {'indices_checked': int, 'total_filled': int}
        """
        from backend.services.config import INDUSTRY_INDEX_MAP

        end_date = datetime.now().strftime("%Y%m%d")
        all_trade_dates = set(self._get_trade_dates(DATA_START_DATE, end_date))
        if not all_trade_dates:
            logger.warning("交易日历为空")
            return {'indices_checked': 0, 'total_filled': 0}

        # 收集所有指数代码
        index_codes = ['000300.SH']
        if INDUSTRY_INDEX_MAP:
            index_codes.extend(INDUSTRY_INDEX_MAP.values())
        index_codes = list(dict.fromkeys(index_codes))  # 去重保序

        total_filled = 0
        for code in index_codes:
            # 查 DB 中该指数已有日期
            df_existing = self.db.query(
                "SELECT DISTINCT trade_date FROM daily_price WHERE ts_code = :code",
                params={'code': code},
            )
            existing_dates = set()
            if not df_existing.empty:
                existing_dates = set(
                    pd.to_datetime(df_existing['trade_date']).dt.strftime('%Y%m%d').tolist()
                )

            missing = sorted(all_trade_dates - existing_dates)
            if not missing:
                continue

            logger.info(f"指数 {code}: 缺失 {len(missing)} 个交易日")
            # 分组为连续区间
            for seg_start, seg_end in _group_consecutive_dates(missing):
                try:
                    self.download_index_daily(code, start_date=seg_start, end_date=seg_end)
                    total_filled += 1
                except Exception as e:
                    logger.warning(f"指数 {code} 补录 {seg_start}~{seg_end} 失败: {e}")

        logger.info(f"指数补录完成: 检查 {len(index_codes)} 个指数, 补录 {total_filled} 个区间")
        return {'indices_checked': len(index_codes), 'total_filled': total_filled}

    # ----------------------------------------------------------
    # 指数日线下载
    # ----------------------------------------------------------

    def download_index_daily(
        self,
        index_code: str = "000300.SH",
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
    ) -> int:
        """
        下载指数日线行情并存入 daily_price 表。

        自动识别申万指数（.SI 后缀）并使用 sw_daily 接口，
        其他指数使用 index_daily 接口。

        Args:
            index_code: 指数代码，默认沪深300（000300.SH）。
            start_date: 起始日期，格式 YYYYMMDD，默认 DATA_START_DATE。
            end_date: 结束日期，格式 YYYYMMDD，默认今天。

        Returns:
            下载的记录数。
        """
        # 申万指数使用专用接口
        if index_code.endswith(".SI"):
            return self._download_sw_index_daily(index_code, start_date, end_date)

        if start_date is None:
            start_date = DATA_START_DATE
        if end_date is None:
            end_date = datetime.now().strftime("%Y%m%d")

        logger.info(f"下载指数日线: {index_code}, {start_date} ~ {end_date}")

        df = _tushare_call(
            self.pro, "index_daily", self.limiter,
            ts_code=index_code,
            start_date=start_date,
            end_date=end_date,
        )

        if df.empty:
            logger.warning(f"指数 {index_code} 无数据")
            return 0

        # 对齐到 daily_price 表结构
        df = df.rename(columns={"vol": "volume"})
        df["turnover_rate"] = None
        df["adj_factor"] = 1.0
        df["is_limit_up"] = 0
        df["is_limit_down"] = 0

        keep_cols = [
            "ts_code", "trade_date", "open", "high", "low", "close",
            "volume", "amount", "turnover_rate", "pct_chg",
            "adj_factor", "is_limit_up", "is_limit_down",
        ]
        df = df[[c for c in keep_cols if c in df.columns]]

        self.db.bulk_upsert_daily_price(df)
        logger.info(f"指数 {index_code}: 写入 {len(df)} 条日线")
        return len(df)

    def _download_sw_index_daily(
        self,
        index_code: str,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
    ) -> int:
        """
        下载申万行业指数日线（使用 sw_daily 接口）。

        Args:
            index_code: 申万指数代码（如 801050.SI）。
            start_date: 起始日期。
            end_date: 结束日期。

        Returns:
            下载的记录数。
        """
        if start_date is None:
            start_date = DATA_START_DATE
        if end_date is None:
            end_date = datetime.now().strftime("%Y%m%d")

        logger.info(f"下载申万指数日线: {index_code}, {start_date} ~ {end_date}")

        df = _tushare_call(
            self.pro, "sw_daily", self.limiter,
            ts_code=index_code,
            start_date=start_date,
            end_date=end_date,
        )

        if df.empty:
            logger.warning(f"申万指数 {index_code} 无数据")
            return 0

        # 对齐到 daily_price 表结构
        # sw_daily 返回字段: ts_code, trade_date, name, open, low, high, close,
        #                     change, pct_change, vol, amount, pe, pb, float_mv, total_mv
        df = df.rename(columns={"vol": "volume", "pct_change": "pct_chg"})
        df["turnover_rate"] = None
        df["adj_factor"] = 1.0
        df["is_limit_up"] = 0
        df["is_limit_down"] = 0

        keep_cols = [
            "ts_code", "trade_date", "open", "high", "low", "close",
            "volume", "amount", "turnover_rate", "pct_chg",
            "adj_factor", "is_limit_up", "is_limit_down",
        ]
        df = df[[c for c in keep_cols if c in df.columns]]

        self.db.bulk_upsert_daily_price(df)
        logger.info(f"申万指数 {index_code}: 写入 {len(df)} 条日线")
        return len(df)

    def update_index_daily(self, index_code: str = "000300.SH") -> int:
        """
        增量更新指数日线。

        从该指数在 daily_price 中的最新日期开始更新。

        Args:
            index_code: 指数代码。

        Returns:
            更新的记录数。
        """
        latest = self.db.get_latest_trade_date(ts_code=index_code)
        if latest is None:
            return self.download_index_daily(index_code)

        start = (pd.to_datetime(latest) + pd.Timedelta(days=1)).strftime("%Y%m%d")
        today = pd.Timestamp.now(tz="Asia/Shanghai").normalize().tz_localize(None).strftime("%Y%m%d")

        if start > today:
            logger.info(f"指数 {index_code} 数据已是最新")
            return 0

        return self.download_index_daily(index_code, start_date=start, end_date=today)

    def update_fund_daily(self, ts_code: str = "511010.SH") -> int:
        """
        增量更新基金/ETF 日线（通过 tushare fund_daily）。

        Args:
            ts_code: 基金代码（如 511010.SH 国债ETF）。

        Returns:
            更新的记录数。
        """
        latest = self.db.get_latest_trade_date(ts_code=ts_code)
        today = pd.Timestamp.now(tz="Asia/Shanghai").normalize().tz_localize(None).strftime("%Y%m%d")

        if latest:
            start = (pd.to_datetime(latest) + pd.Timedelta(days=1)).strftime("%Y%m%d")
            if start > today:
                logger.info(f"基金 {ts_code} 数据已是最新")
                return 0
        else:
            start = "20150101"

        self.limiter.acquire()
        df = self.pro.fund_daily(
            ts_code=ts_code,
            start_date=start, end_date=today,
        )
        if df is None or df.empty:
            return 0

        df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.strftime("%Y-%m-%d")
        df = df.rename(columns={"vol": "volume"})
        keep = ["ts_code", "trade_date", "open", "high", "low", "close", "volume"]
        df = df[[c for c in keep if c in df.columns]]

        self.db.upsert_daily_price(df)
        logger.info(f"基金 {ts_code}: 更新 {len(df)} 条日线")
        return len(df)

    # ----------------------------------------------------------
    # 增量更新
    # ----------------------------------------------------------

    def update_daily_prices(self) -> int:
        """
        增量更新日线行情。

        从数据库最新日期的下一天开始，到今天为止。
        若数据库为空则自动执行全量下载。

        Returns:
            成功更新的交易日数量。
        """
        # 只看个股数据的最新日期（排除指数/行业指数，避免被提前更新的指数数据误导）
        result = self.db.query(
            "SELECT MAX(trade_date) as max_date FROM daily_price "
            "WHERE ts_code NOT LIKE '%.SH' AND ts_code NOT LIKE '%.SI'"
        )
        latest_date = result["max_date"].iloc[0]
        if pd.isna(latest_date):
            latest_date = None
        else:
            latest_date = str(latest_date)

        if latest_date is None:
            logger.info("数据库无历史数据，执行全量下载")
            return self.download_daily_prices()

        start = pd.to_datetime(latest_date) + pd.Timedelta(days=1)
        today = pd.Timestamp.now(tz="Asia/Shanghai").normalize().tz_localize(None)

        if start > today:
            logger.info("数据已是最新，无需更新")
            return 0

        start_str = start.strftime("%Y%m%d")
        end_str = today.strftime("%Y%m%d")

        logger.info(f"增量更新日线行情: {start_str} ~ {end_str}")
        return self.download_daily_prices(start_date=start_str, end_date=end_str)


# ============================================================
# 命令行入口
# ============================================================

if __name__ == "__main__":
    import sys

    logging.basicConfig(
        level=LOG_LEVEL,
        format="%(asctime)s | %(name)-20s | %(levelname)-7s | %(message)s",
    )

    db = DatabaseManager()
    db.init_tables()
    downloader = TushareDownloader(db)

    mode = sys.argv[1] if len(sys.argv) > 1 else "full"

    if mode == "list":
        print("=== 下载股票列表 ===")
        df = downloader.download_stock_list()
        print(f"完成，共 {len(df)} 只股票")

    elif mode == "daily":
        print("=== 下载日线行情 ===")
        count = downloader.download_daily_prices()
        print(f"完成，成功 {count} 个交易日")

    elif mode == "update":
        print("=== 增量更新日线行情 ===")
        count = downloader.update_daily_prices()
        print(f"完成，成功更新 {count} 个交易日")

    elif mode == "full":
        print("=== 全量下载 ===")
        print("步骤 1/2: 下载股票列表...")
        df = downloader.download_stock_list()
        print(f"股票列表完成，共 {len(df)} 只")

        print("步骤 2/2: 下载日线行情...")
        count = downloader.download_daily_prices()
        print(f"日线行情完成，成功 {count} 个交易日")

    else:
        print("用法: python -m data.downloader [list|daily|update|full]")
        print("  list   - 只下载股票列表")
        print("  daily  - 只下载日线行情")
        print("  update - 增量更新日线行情")
        print("  full   - 全量下载（默认）")
        sys.exit(1)
