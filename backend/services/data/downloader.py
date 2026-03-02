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

        # 2. 换手率
        df_basic = _tushare_call(self.pro, "daily_basic", self.limiter,
                                 trade_date=trade_date, fields="ts_code,trade_date,turnover_rate")

        # 3. 复权因子
        df_adj = _tushare_call(self.pro, "adj_factor", self.limiter,
                               trade_date=trade_date, fields="ts_code,trade_date,adj_factor")

        # 筛选沪深两市
        df_daily = df_daily[df_daily["ts_code"].str.match(r"^(00|30|60|68)")].copy()
        if df_daily.empty:
            return None

        # 合并换手率
        if not df_basic.empty:
            df_daily = df_daily.merge(
                df_basic[["ts_code", "turnover_rate"]],
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
            "is_limit_up", "is_limit_down",
        ]
        df_out = df_daily[[c for c in keep_cols if c in df_daily.columns]].copy()

        # Tushare daily 的成交量列名是 vol，统一为 volume
        if "vol" in df_out.columns:
            df_out = df_out.rename(columns={"vol": "volume"})

        return df_out

    # ----------------------------------------------------------
    # 增量更新
    # ----------------------------------------------------------

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
        latest_date = self.db.get_latest_trade_date()
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
