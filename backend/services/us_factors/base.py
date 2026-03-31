"""
美股因子基类

定义美股因子计算的统一接口和通用工具方法。
所有具体因子类继承 USFactorBase，实现 compute() 方法。

设计原则：
    - 截面计算：同一时间截面对所有股票计算因子值
    - 防止未来数据：财务数据按 filing_date 取值，价格只用截止到计算日的历史
    - 统一输出格式：DataFrame[ticker, factor_value]
"""

import logging
from abc import ABC, abstractmethod
from typing import Optional

import numpy as np
import pandas as pd

from backend.services.config import LOG_LEVEL
from backend.services.data.database import DatabaseManager

logger = logging.getLogger(__name__)
logger.setLevel(LOG_LEVEL)


class USFactorBase(ABC):
    """
    美股因子计算基类。

    所有因子类必须继承此基类并实现 compute() 方法。

    用法:
        class MyFactor(USFactorBase):
            name = "my_factor"
            def compute(self, date, universe):
                ...
                return df[["ticker", "factor_value"]]

        factor = MyFactor(db)
        result = factor.compute("2024-12-31", universe_df)
    """

    # 子类必须覆盖
    name: str = "base"
    description: str = ""

    # ----------------------------------------------------------
    # 查询缓存（类级别，所有因子实例共享）
    # ----------------------------------------------------------
    _static_cache: dict = {}
    _date_cache: dict = {}
    _IN_CLAUSE_THRESHOLD = 2000

    @classmethod
    def clear_date_cache(cls):
        """清空日期相关缓存（每个调仓日期调用）。"""
        cls._date_cache.clear()

    @classmethod
    def clear_all_cache(cls):
        """清空所有缓存。"""
        cls._static_cache.clear()
        cls._date_cache.clear()

    # ----------------------------------------------------------
    # 快速 MySQL 读取
    # ----------------------------------------------------------
    @classmethod
    def _fast_mysql_read(
        cls,
        db: "DatabaseManager",
        columns: list[str],
        table: str,
        where: str = "",
        order_by: str = "",
    ) -> pd.DataFrame:
        """mysql CLI 导出 TSV → pd.read_csv，比 pymysql 快 3-4 倍。"""
        import subprocess
        import tempfile
        import os
        from backend.services.config import (
            MYSQL_HOST, MYSQL_PORT, MYSQL_USER, MYSQL_PASSWORD, MYSQL_DATABASE,
        )

        sql = f"SELECT {', '.join(columns)} FROM {table}"
        if where:
            sql += f" WHERE {where}"
        if order_by:
            sql += f" ORDER BY {order_by}"

        try:
            tmpf = tempfile.NamedTemporaryFile(suffix=".tsv", delete=False)
            tmpf.close()
            with open(tmpf.name, "w") as fout:
                proc = subprocess.run(
                    [
                        "mysql",
                        "-h", str(MYSQL_HOST),
                        "-P", str(MYSQL_PORT),
                        "-u", MYSQL_USER,
                        f"-p{MYSQL_PASSWORD}",
                        "-N", "-B", "--quick",
                        MYSQL_DATABASE, "-e", sql,
                    ],
                    stdout=fout,
                    stderr=subprocess.PIPE,
                    timeout=300,
                )
            if proc.returncode != 0:
                raise RuntimeError(proc.stderr.decode(errors="replace"))
            df = pd.read_csv(
                tmpf.name, sep="\t", header=None, names=columns,
                na_values="NULL", low_memory=False,
            )
            os.unlink(tmpf.name)
            return df
        except Exception as e:
            logger.warning(f"mysql CLI 快速读取失败 ({e})，回退到 pymysql")
            from sqlalchemy import text as sa_text
            fallback_sql = f"SELECT {', '.join(columns)} FROM {table}"
            if where:
                fallback_sql += f" WHERE {where}"
            if order_by:
                fallback_sql += f" ORDER BY {order_by}"
            return db.query(fallback_sql, params={})

    # ----------------------------------------------------------
    # 回测预加载
    # ----------------------------------------------------------
    @classmethod
    def preload_for_backtest(cls, db: "DatabaseManager", start_date: str, end_date: str):
        """
        一次性预加载回测区间所需的 us_financial_data 和 us_daily_price 到内存。

        预加载后 get_latest_financial / get_price_history / get_close_on_date
        自动从内存过滤，跳过 SQL 查询。
        """
        import time

        # 1. 预加载 us_financial_data 全量
        t0 = time.time()
        fin_cols = [
            "ticker", "period", "date", "filing_date",
            "revenue", "net_income", "eps", "gross_margin",
            "operating_margin", "roe", "total_equity", "total_assets",
            "total_debt", "free_cash_flow", "pe_ratio", "pb_ratio",
        ]
        df_fin = cls._fast_mysql_read(db, fin_cols, "us_financial_data")
        if not df_fin.empty:
            df_fin["filing_date"] = pd.to_datetime(df_fin["filing_date"])
            df_fin["date"] = pd.to_datetime(df_fin["date"])

            # 防前视偏差修正：filing_date 不可靠时强制加安全缓冲
            # 1. filing_date == report_date（yfinance 回退数据）→ 加 45 天
            # 2. filing_date < report_date（错误数据）→ 加 45 天
            _FILING_LAG_BUFFER = pd.Timedelta(days=45)
            bad_mask = df_fin["filing_date"] <= df_fin["date"]
            n_fixed = bad_mask.sum()
            if n_fixed > 0:
                df_fin.loc[bad_mask, "filing_date"] = df_fin.loc[bad_mask, "date"] + _FILING_LAG_BUFFER
                logger.info(f"Filing date 修正: {n_fixed} 条 (filing_date <= report_date → +45天)")

        cls._static_cache["_bulk_financial"] = df_fin
        logger.info(f"US 预加载 us_financial_data: {len(df_fin)} 行, {time.time()-t0:.1f}s")

        # 2. 预加载 us_daily_price（回测区间 + 400 天前移量）
        t0 = time.time()
        price_start = (
            pd.to_datetime(start_date) - pd.Timedelta(days=400)
        ).strftime("%Y-%m-%d")
        price_cols = [
            "ticker", "trade_date", "open", "high", "low",
            "close", "adj_close", "volume", "change_pct",
        ]
        df_price = cls._fast_mysql_read(
            db, price_cols, "us_daily_price",
            where=f"trade_date >= '{price_start}' AND trade_date <= '{end_date}'",
            order_by="ticker, trade_date",
        )
        if not df_price.empty:
            df_price["trade_date"] = pd.to_datetime(df_price["trade_date"])
        cls._static_cache["_bulk_daily"] = df_price
        logger.info(f"US 预加载 us_daily_price: {len(df_price)} 行, {time.time()-t0:.1f}s")

        # 3. 预加载 us_analyst_recommendation
        t0 = time.time()
        ar_cols = ["ticker", "date", "analyst_company", "rating", "price_target"]
        analyst_start = (
            pd.to_datetime(start_date) - pd.Timedelta(days=180)
        ).strftime("%Y-%m-%d")
        df_ar = cls._fast_mysql_read(
            db, ar_cols, "us_analyst_recommendation",
            where=f"date >= '{analyst_start}' AND date <= '{end_date}'",
            order_by="ticker, date",
        )
        if not df_ar.empty:
            df_ar["date"] = pd.to_datetime(df_ar["date"])
        cls._static_cache["_bulk_analyst"] = df_ar
        logger.info(f"US 预加载 us_analyst_recommendation: {len(df_ar)} 行, {time.time()-t0:.1f}s")

        # 4. 预加载 us_earnings_surprise
        t0 = time.time()
        es_cols = ["ticker", "date", "actual_eps", "estimated_eps", "surprise", "surprise_pct"]
        es_start = (
            pd.to_datetime(start_date) - pd.Timedelta(days=180)
        ).strftime("%Y-%m-%d")
        df_es = cls._fast_mysql_read(
            db, es_cols, "us_earnings_surprise",
            where=f"date >= '{es_start}' AND date <= '{end_date}'",
            order_by="ticker, date",
        )
        if not df_es.empty:
            df_es["date"] = pd.to_datetime(df_es["date"])
        cls._static_cache["_bulk_earnings_surprise"] = df_es
        logger.info(f"US 预加载 us_earnings_surprise: {len(df_es)} 行, {time.time()-t0:.1f}s")

        # 5. 预加载 us_eps_estimate
        t0 = time.time()
        ee_cols = ["ticker", "date", "eps_avg", "eps_low", "eps_high", "num_analysts"]
        df_ee = cls._fast_mysql_read(
            db, ee_cols, "us_eps_estimate",
            order_by="ticker, date",
        )
        if not df_ee.empty:
            df_ee["date"] = pd.to_datetime(df_ee["date"])
        cls._static_cache["_bulk_eps_estimate"] = df_ee
        logger.info(f"US 预加载 us_eps_estimate: {len(df_ee)} 行, {time.time()-t0:.1f}s")

        # 6. 预加载 us_corporate_action (dividend)
        t0 = time.time()
        ca_cols = ["ticker", "date", "action_type", "value"]
        div_start = (
            pd.to_datetime(start_date) - pd.Timedelta(days=400)
        ).strftime("%Y-%m-%d")
        df_ca = cls._fast_mysql_read(
            db, ca_cols, "us_corporate_action",
            where=f"date >= '{div_start}' AND date <= '{end_date}' AND action_type = 'dividend'",
            order_by="ticker, date",
        )
        if not df_ca.empty:
            df_ca["date"] = pd.to_datetime(df_ca["date"])
        cls._static_cache["_bulk_dividends"] = df_ca
        logger.info(f"US 预加载 dividends: {len(df_ca)} 行, {time.time()-t0:.1f}s")

    @classmethod
    def precompute_rolling_stats(cls):
        """
        一次性预计算动量/技术因子所需的 rolling 统计量。
        必须在 preload_for_backtest() 之后调用。
        """
        import time
        t0 = time.time()

        bulk_daily = cls._static_cache.get("_bulk_daily")
        if bulk_daily is None or bulk_daily.empty:
            return

        df = bulk_daily[["ticker", "trade_date", "adj_close", "close",
                         "change_pct", "volume"]].copy()
        df = df.sort_values(["ticker", "trade_date"])

        for col in ["adj_close", "close", "change_pct", "volume"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")

        g = df.groupby("ticker", sort=False)

        # 从 adj_close 计算收益率（change_pct 可能为 NULL，yfinance 不一定提供）
        df["ret"] = g["adj_close"].transform(lambda x: x.pct_change())
        # 如果 change_pct 有值则优先使用（更精确）
        has_pct = df["change_pct"].notna()
        if has_pct.any():
            df.loc[has_pct, "ret"] = df.loc[has_pct, "change_pct"] / 100.0
        df["log_ret"] = np.log1p(df["ret"].clip(-0.99, None))

        # 用 adj_close 计算美元交易额（代替 A股的 turnover_rate）
        df["dollar_volume"] = df["adj_close"] * df["volume"]

        # 5-day cumulative return (REV_5D)
        df["cum_ret_5d"] = np.expm1(
            g["log_ret"].transform(lambda x: x.rolling(5, min_periods=3).sum())
        )
        # 20-day cumulative return (RESIDUAL_MOM)
        df["cum_ret_20d"] = np.expm1(
            g["log_ret"].transform(lambda x: x.rolling(20, min_periods=10).sum())
        )
        # 20-day rolling mean dollar volume (TURN_20D proxy)
        df["dvol_20d"] = g["dollar_volume"].transform(
            lambda x: x.rolling(20, min_periods=10).mean()
        )
        # 20-day rolling std of returns (VOL_20D)
        df["vol_20d"] = g["ret"].transform(
            lambda x: x.rolling(20, min_periods=10).std()
        )
        # 60-day rolling mean adj_close (PRICE_DEV_60D)
        df["ma60_adj"] = g["adj_close"].transform(
            lambda x: x.rolling(60, min_periods=30).mean()
        )

        # 存储为 MultiIndex (trade_date, ticker) 以便 xs() 快速查找
        keep_cols = ["adj_close", "cum_ret_5d", "cum_ret_20d",
                     "dvol_20d", "vol_20d", "ma60_adj", "volume", "dollar_volume"]
        df_indexed = df[["ticker", "trade_date"] + keep_cols].copy()
        df_indexed = df_indexed.set_index(["trade_date", "ticker"]).sort_index()
        cls._static_cache["_rolling_indexed"] = df_indexed

        # 预计算月末复权收盘价（MOM_1M/3M/12M 使用）
        df_me = df[["ticker", "trade_date", "adj_close"]].copy()
        df_me["year_month"] = df_me["trade_date"].dt.to_period("M")
        idx = df_me.groupby(["ticker", "year_month"])["trade_date"].idxmax()
        month_ends = df_me.loc[idx, ["ticker", "year_month", "adj_close"]].reset_index(drop=True)
        cls._static_cache["_month_end_prices"] = month_ends

        logger.info(
            f"US 预计算 rolling stats + 月末价格: {len(df)} 行, {time.time()-t0:.1f}s"
        )

    # ----------------------------------------------------------
    # Rolling stats helpers
    # ----------------------------------------------------------
    def _get_rolling_for_date(
        self, date: str, tickers: Optional[set[str]] = None,
    ) -> Optional[pd.DataFrame]:
        """从预计算的 rolling stats 中提取指定日期的截面数据。"""
        ri = self._static_cache.get("_rolling_indexed")
        if ri is None:
            return None
        date_ts = pd.to_datetime(date)
        try:
            day = ri.xs(date_ts, level="trade_date")
            if tickers:
                day = day[day.index.isin(tickers)]
            return day
        except KeyError:
            return None

    def _get_month_end_adj_close(
        self, date: str, months_ago: int, tickers: Optional[set[str]] = None,
    ) -> Optional[pd.DataFrame]:
        """从预计算的月末价格中提取 N 月前月末的复权收盘价。"""
        me = self._static_cache.get("_month_end_prices")
        if me is None:
            return None
        target = pd.to_datetime(date) - pd.DateOffset(months=months_ago)
        target_period = target.to_period("M")
        result = me[me["year_month"] == target_period]
        if tickers:
            result = result[result["ticker"].isin(tickers)]
        if result.empty:
            return None
        return result[["ticker", "adj_close"]].copy()

    # ----------------------------------------------------------
    # 初始化
    # ----------------------------------------------------------
    def __init__(self, db: DatabaseManager):
        self.db = db

    @abstractmethod
    def compute(self, date: str, universe: pd.DataFrame) -> pd.DataFrame:
        """
        计算因子值（截面计算）。

        Args:
            date: 计算日期，格式 YYYY-MM-DD。
            universe: 股票池 DataFrame，至少包含 ticker 列。

        Returns:
            DataFrame，包含 ticker 和 factor_value 两列。
        """
        raise NotImplementedError

    # ----------------------------------------------------------
    # 行业映射
    # ----------------------------------------------------------
    def get_industry_map_cached(self) -> pd.DataFrame:
        """获取 GICS 行业映射（缓存）。返回 DataFrame[ticker, sector, industry]。"""
        cached = self._static_cache.get("industry_map")
        if cached is not None:
            return cached.copy()
        result = self.db.query(
            "SELECT ticker, sector, industry FROM us_industry_class"
        )
        self._static_cache["industry_map"] = result
        return result.copy()

    # ----------------------------------------------------------
    # 通用数据获取工具
    # ----------------------------------------------------------
    @staticmethod
    def _build_in_clause(codes: list[str], prefix: str = "code") -> tuple[str, dict]:
        """构建 IN 子句的参数化占位符。"""
        placeholders = []
        params = {}
        for i, code in enumerate(codes):
            key = f"{prefix}_{i}"
            placeholders.append(f":{key}")
            params[key] = code
        sql_fragment = f"({', '.join(placeholders)})"
        return sql_fragment, params

    def get_latest_financial(
        self,
        date: str,
        columns: list[str],
        universe_tickers: Optional[list[str]] = None,
    ) -> pd.DataFrame:
        """
        获取截止到指定日期的最新财务数据（按 filing_date，防止未来函数）。

        对每只股票，取 filing_date <= date 的最近一条记录（按 date/period 排序取最新）。

        Returns:
            DataFrame，包含 ticker, filing_date, period 和请求的列。
        """
        cache_key = ("financial", date)
        cached = self._date_cache.get(cache_key)
        if cached is not None:
            df = cached
            if universe_tickers:
                df = df[df["ticker"].isin(universe_tickers)]
            return df[["ticker", "filing_date", "date", "period"] + [c for c in columns if c in df.columns]].copy()

        # 预加载数据可用时，从内存过滤
        bulk_fin = self._static_cache.get("_bulk_financial")
        if bulk_fin is not None and not bulk_fin.empty:
            date_ts = pd.to_datetime(date)
            df = bulk_fin[bulk_fin["filing_date"] <= date_ts].copy()
            # 每只股票取 period 最新（filing_date 已满足时效性）
            df = df.sort_values("date", ascending=False).drop_duplicates(subset=["ticker"], keep="first")
            self._date_cache[cache_key] = df
            if universe_tickers:
                df = df[df["ticker"].isin(universe_tickers)]
            return df[["ticker", "filing_date", "date", "period"] + [c for c in columns if c in df.columns]].copy()

        # SQL 回退
        all_columns = list(set(columns) | {
            "revenue", "net_income", "eps", "gross_margin", "roe", "total_equity",
        })
        cols_str = ", ".join(["ticker", "filing_date", "date", "period"] + all_columns)
        params: dict = {"date": date}

        inner_sql = (
            f"SELECT {cols_str}, ROW_NUMBER() OVER "
            f"(PARTITION BY ticker ORDER BY date DESC) as rn "
            f"FROM us_financial_data WHERE filing_date <= :date"
        )

        if universe_tickers and len(universe_tickers) <= self._IN_CLAUSE_THRESHOLD:
            in_clause, in_params = self._build_in_clause(universe_tickers)
            inner_sql += f" AND ticker IN {in_clause}"
            params.update(in_params)

        sql = f"SELECT * FROM ({inner_sql}) t WHERE rn = 1"
        df = self.db.query(sql, params=params)

        if not df.empty:
            df = df.drop(columns=["rn"], errors="ignore")
        self._date_cache[cache_key] = df

        if universe_tickers:
            df = df[df["ticker"].isin(universe_tickers)]
        return df[["ticker", "filing_date", "date", "period"] + [c for c in columns if c in df.columns]].copy()

    def get_ttm_value(
        self,
        date: str,
        field: str,
        universe_tickers: Optional[list[str]] = None,
    ) -> pd.DataFrame:
        """
        计算 TTM（Trailing Twelve Months）指标：最近 4 个季度的 field 求和。

        Returns:
            DataFrame[ticker, ttm_value]
        """
        cache_key = ("ttm", date, field)
        cached = self._date_cache.get(cache_key)
        if cached is not None:
            if universe_tickers:
                return cached[cached["ticker"].isin(universe_tickers)].copy()
            return cached.copy()

        bulk_fin = self._static_cache.get("_bulk_financial")
        if bulk_fin is not None and not bulk_fin.empty:
            date_ts = pd.to_datetime(date)
            df = bulk_fin[bulk_fin["filing_date"] <= date_ts].copy()
            if universe_tickers:
                df = df[df["ticker"].isin(universe_tickers)]
            # 取每只股票最近4个季度
            df = df.sort_values("date", ascending=False)
            df = df.groupby("ticker").head(4)
            # 统计有效季度数和求和
            result = df.groupby("ticker").agg(
                ttm_value=(field, "sum"),
                n_quarters=(field, "count"),
            ).reset_index()
            # 至少需要3个季度才计算TTM（4Q最佳，3Q可接受）
            result.loc[result["n_quarters"] < 3, "ttm_value"] = np.nan
            result = result[["ticker", "ttm_value"]]
            self._date_cache[cache_key] = result
            return result.copy()

        # SQL 回退（较慢）
        params: dict = {"date": date}
        sql = (
            f"SELECT ticker, SUM({field}) as ttm_value, COUNT(*) as n "
            f"FROM ("
            f"  SELECT ticker, {field}, ROW_NUMBER() OVER "
            f"  (PARTITION BY ticker ORDER BY date DESC) as rn "
            f"  FROM us_financial_data WHERE filing_date <= :date"
            f") t WHERE rn <= 4 GROUP BY ticker HAVING n >= 3"
        )
        result = self.db.query(sql, params=params)
        if result.empty:
            result = pd.DataFrame(columns=["ticker", "ttm_value"])
        else:
            result = result[["ticker", "ttm_value"]]
        self._date_cache[cache_key] = result
        if universe_tickers:
            result = result[result["ticker"].isin(universe_tickers)]
        return result.copy()

    def get_close_on_date(
        self,
        date: str,
        universe_tickers: Optional[list[str]] = None,
    ) -> pd.DataFrame:
        """
        获取指定日期的复权收盘价。

        Returns:
            DataFrame[ticker, adj_close]
        """
        cache_key = ("close", date)
        cached = self._date_cache.get(cache_key)
        if cached is not None:
            if universe_tickers:
                return cached[cached["ticker"].isin(universe_tickers)].copy()
            return cached.copy()

        bulk_daily = self._static_cache.get("_bulk_daily")
        if bulk_daily is not None and not bulk_daily.empty:
            date_ts = pd.to_datetime(date)
            df = bulk_daily[bulk_daily["trade_date"] == date_ts][["ticker", "adj_close"]].copy()
            df["adj_close"] = pd.to_numeric(df["adj_close"], errors="coerce")
            self._date_cache[cache_key] = df
            if universe_tickers:
                df = df[df["ticker"].isin(universe_tickers)]
            return df.copy()

        params: dict = {"date": date}
        sql = "SELECT ticker, adj_close FROM us_daily_price WHERE trade_date = :date"
        if universe_tickers and len(universe_tickers) <= self._IN_CLAUSE_THRESHOLD:
            in_clause, in_params = self._build_in_clause(universe_tickers)
            sql += f" AND ticker IN {in_clause}"
            params.update(in_params)
        df = self.db.query(sql, params=params)
        self._date_cache[cache_key] = df
        if universe_tickers:
            df = df[df["ticker"].isin(universe_tickers)]
        return df.copy()

    def get_price_history(
        self,
        end_date: str,
        lookback_days: int,
        universe_tickers: Optional[list[str]] = None,
        columns: Optional[list[str]] = None,
    ) -> pd.DataFrame:
        """获取截止到指定日期的历史行情。"""
        cache_key = ("price_hist", end_date, lookback_days)
        cached = self._date_cache.get(cache_key)
        if cached is not None:
            df = cached
            if universe_tickers:
                df = df[df["ticker"].isin(universe_tickers)]
            if columns:
                keep = ["ticker", "trade_date"] + [c for c in columns if c in df.columns]
                df = df[keep]
            return df.copy()

        start_dt = pd.to_datetime(end_date) - pd.Timedelta(days=lookback_days)
        start_date = start_dt.strftime("%Y-%m-%d")

        bulk_daily = self._static_cache.get("_bulk_daily")
        if bulk_daily is not None and not bulk_daily.empty:
            end_ts = pd.to_datetime(end_date)
            mask = (bulk_daily["trade_date"] >= start_dt) & (bulk_daily["trade_date"] <= end_ts)
            result = bulk_daily[mask].copy()
            if universe_tickers:
                result = result[result["ticker"].isin(universe_tickers)]
            self._date_cache[cache_key] = result
            if columns:
                keep = ["ticker", "trade_date"] + [c for c in columns if c in result.columns]
                result = result[keep]
            return result.copy()

        base_cols = {"ticker", "trade_date", "adj_close", "close", "change_pct", "volume", "open", "high", "low"}
        if columns:
            base_cols.update(columns)
        cols_str = ", ".join(sorted(base_cols))

        params: dict = {"start_date": start_date, "end_date": end_date}
        sql = (
            f"SELECT {cols_str} FROM us_daily_price "
            f"WHERE trade_date >= :start_date AND trade_date <= :end_date"
        )
        if universe_tickers and len(universe_tickers) <= self._IN_CLAUSE_THRESHOLD:
            in_clause, in_params = self._build_in_clause(universe_tickers)
            sql += f" AND ticker IN {in_clause}"
            params.update(in_params)
        sql += " ORDER BY ticker, trade_date"

        result = self.db.query(sql, params=params)
        self._date_cache[cache_key] = result
        if columns:
            keep = ["ticker", "trade_date"] + [c for c in columns if c in result.columns]
            result = result[keep]
        return result.copy()

    def get_month_end_price(
        self,
        date: str,
        months_ago: int,
        universe_tickers: Optional[list[str]] = None,
    ) -> pd.DataFrame:
        """
        获取 N 个月前月末的复权收盘价。

        Returns:
            DataFrame[ticker, adj_close]
        """
        # 优先使用预计算
        precomputed = self._get_month_end_adj_close(date, months_ago,
                                                     set(universe_tickers) if universe_tickers else None)
        if precomputed is not None:
            return precomputed

        cache_key = ("month_end", date, months_ago)
        cached = self._date_cache.get(cache_key)
        if cached is not None:
            if universe_tickers:
                return cached[cached["ticker"].isin(universe_tickers)].copy()
            return cached.copy()

        target_date = pd.to_datetime(date) - pd.DateOffset(months=months_ago)
        month_start = target_date.replace(day=1)
        month_end = month_start + pd.DateOffset(months=1) - pd.Timedelta(days=1)

        bulk_daily = self._static_cache.get("_bulk_daily")
        if bulk_daily is not None and not bulk_daily.empty:
            mask = (bulk_daily["trade_date"] >= month_start) & (bulk_daily["trade_date"] <= month_end)
            df = bulk_daily[mask].copy()
            if universe_tickers:
                df = df[df["ticker"].isin(universe_tickers)]
            if df.empty:
                self._date_cache[cache_key] = df
                return df
            df["adj_close"] = pd.to_numeric(df["adj_close"], errors="coerce")
            df = df.sort_values("trade_date", ascending=False).drop_duplicates(subset=["ticker"], keep="first")
            result = df[["ticker", "adj_close"]].copy()
            self._date_cache[cache_key] = result
            return result.copy()

        params: dict = {
            "start": month_start.strftime("%Y-%m-%d"),
            "end": month_end.strftime("%Y-%m-%d"),
        }
        sql = (
            "SELECT ticker, adj_close, trade_date FROM us_daily_price "
            "WHERE trade_date >= :start AND trade_date <= :end "
            "ORDER BY ticker, trade_date DESC"
        )
        df = self.db.query(sql, params=params)
        if df.empty:
            self._date_cache[cache_key] = df
            return df
        df = df.drop_duplicates(subset=["ticker"], keep="first")
        result = df[["ticker", "adj_close"]].copy()
        self._date_cache[cache_key] = result
        if universe_tickers:
            result = result[result["ticker"].isin(universe_tickers)]
        return result.copy()

    def get_market_cap(
        self,
        date: str,
        universe_tickers: Optional[list[str]] = None,
    ) -> pd.DataFrame:
        """
        获取市值。使用 us_stock_basic.market_cap（静态快照，更新频率较低）。

        Returns:
            DataFrame[ticker, market_cap]
        """
        cached = self._static_cache.get("market_cap")
        if cached is not None:
            df = cached
            if universe_tickers:
                df = df[df["ticker"].isin(universe_tickers)]
            return df.copy()

        df = self.db.query("SELECT ticker, market_cap FROM us_stock_basic WHERE is_active = 1")
        df["market_cap"] = pd.to_numeric(df["market_cap"], errors="coerce")
        self._static_cache["market_cap"] = df
        if universe_tickers:
            df = df[df["ticker"].isin(universe_tickers)]
        return df.copy()

    def get_dividends(
        self,
        date: str,
        lookback_days: int = 365,
        universe_tickers: Optional[list[str]] = None,
    ) -> pd.DataFrame:
        """
        获取过去 N 天的股息数据。

        Returns:
            DataFrame[ticker, total_dividend] — trailing 期间内累计每股股息
        """
        cache_key = ("dividends", date, lookback_days)
        cached = self._date_cache.get(cache_key)
        if cached is not None:
            if universe_tickers:
                return cached[cached["ticker"].isin(universe_tickers)].copy()
            return cached.copy()

        date_ts = pd.to_datetime(date)
        start_ts = date_ts - pd.Timedelta(days=lookback_days)

        bulk_div = self._static_cache.get("_bulk_dividends")
        if bulk_div is not None and not bulk_div.empty:
            mask = (bulk_div["date"] >= start_ts) & (bulk_div["date"] <= date_ts)
            df = bulk_div[mask].copy()
            if universe_tickers:
                df = df[df["ticker"].isin(universe_tickers)]
            result = df.groupby("ticker")["value"].sum().reset_index()
            result.columns = ["ticker", "total_dividend"]
            self._date_cache[cache_key] = result
            return result.copy()

        params: dict = {
            "start": start_ts.strftime("%Y-%m-%d"),
            "end": date,
        }
        sql = (
            "SELECT ticker, SUM(value) as total_dividend "
            "FROM us_corporate_action "
            "WHERE action_type = 'dividend' AND date >= :start AND date <= :end "
            "GROUP BY ticker"
        )
        result = self.db.query(sql, params=params)
        if result.empty:
            result = pd.DataFrame(columns=["ticker", "total_dividend"])
        self._date_cache[cache_key] = result
        if universe_tickers:
            result = result[result["ticker"].isin(universe_tickers)]
        return result.copy()
