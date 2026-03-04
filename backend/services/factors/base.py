"""
因子基类

定义因子计算的统一接口和通用工具方法。
所有具体因子类继承 FactorBase，实现 compute() 方法。

设计原则：
    - 截面计算：同一时间截面对所有股票计算因子值
    - 防止未来数据：财务数据按公告日期取值，价格数据只用截止到计算日的历史
    - 统一输出格式：DataFrame[ts_code, factor_value]
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


class FactorBase(ABC):
    """
    因子计算基类。

    所有因子类必须继承此基类并实现 compute() 方法。

    用法:
        class MyFactor(FactorBase):
            name = "my_factor"
            def compute(self, date, universe):
                ...
                return df[["ts_code", "factor_value"]]

        factor = MyFactor(db)
        result = factor.compute("2024-12-31", universe_df)
    """

    # 子类必须覆盖
    name: str = "base"
    description: str = ""

    # ----------------------------------------------------------
    # 查询缓存（类级别，所有因子实例共享）
    # ----------------------------------------------------------
    # _static_cache: 跨日期持久化（industry_map, total_share 等不随日期变化的数据）
    # _date_cache: 每个调仓日期清空（close, financial, price_history 等日期相关数据）
    _static_cache: dict = {}
    _date_cache: dict = {}
    _IN_CLAUSE_THRESHOLD = 2000

    @classmethod
    def clear_date_cache(cls):
        """清空日期相关缓存（每个调仓日期调用）。"""
        cls._date_cache.clear()

    @classmethod
    def clear_all_cache(cls):
        """清空所有缓存（generate_signals 结束时调用）。"""
        cls._static_cache.clear()
        cls._date_cache.clear()

    @classmethod
    def preload_for_backtest(cls, db: "DatabaseManager", start_date: str, end_date: str):
        """
        一次性预加载回测区间所需的 financial_data 和 daily_price 到内存。

        预加载后，get_latest_financial / _compute_ttm_vectorized / get_price_history /
        get_month_end_price / get_close_on_date 自动从内存过滤，跳过 SQL 查询。

        Args:
            db: DatabaseManager 实例。
            start_date: 回测起始日期。
            end_date: 回测结束日期。
        """
        import time

        # 1. 预加载 financial_data 全量（~200K 行）
        t0 = time.time()
        fin_sql = (
            "SELECT ts_code, ann_date, end_date, roe_ttm, gross_margin, bps, "
            "net_profit, revenue FROM financial_data"
        )
        df_fin = db.query(fin_sql, params={})
        if not df_fin.empty:
            df_fin["ann_date"] = pd.to_datetime(df_fin["ann_date"])
            df_fin["end_date"] = pd.to_datetime(df_fin["end_date"])
            for col in ["roe_ttm", "gross_margin", "bps", "net_profit", "revenue"]:
                if col in df_fin.columns:
                    df_fin[col] = pd.to_numeric(df_fin[col], errors="coerce")
        cls._static_cache["_bulk_financial"] = df_fin
        logger.info(f"预加载 financial_data: {len(df_fin)} 行, {time.time()-t0:.1f}s")

        # 2. 预加载 daily_price（回测区间 + 400 天前移量，覆盖动量/技术因子回看需求）
        t0 = time.time()
        price_start = (
            pd.to_datetime(start_date) - pd.Timedelta(days=400)
        ).strftime("%Y-%m-%d")
        price_sql = (
            "SELECT ts_code, trade_date, pct_chg, turnover_rate, volume, amount, "
            "close, adj_factor FROM daily_price "
            "WHERE trade_date >= :start_date AND trade_date <= :end_date "
            "ORDER BY ts_code, trade_date"
        )
        df_price = db.query(price_sql, params={"start_date": price_start, "end_date": end_date})
        if not df_price.empty:
            df_price["trade_date"] = pd.to_datetime(df_price["trade_date"])
        cls._static_cache["_bulk_daily"] = df_price
        logger.info(f"预加载 daily_price: {len(df_price)} 行, {time.time()-t0:.1f}s")

    def __init__(self, db: DatabaseManager):
        """
        Args:
            db: DatabaseManager 实例。
        """
        self.db = db

    def get_industry_map_cached(self) -> pd.DataFrame:
        """获取行业映射（使用静态缓存，跨日期复用）。"""
        cached = self._static_cache.get("industry_map")
        if cached is not None:
            return cached.copy()
        result = self.db.get_industry_map()
        self._static_cache["industry_map"] = result
        return result.copy()

    @abstractmethod
    def compute(self, date: str, universe: pd.DataFrame) -> pd.DataFrame:
        """
        计算因子值（截面计算）。

        Args:
            date: 计算日期，格式 YYYY-MM-DD。
            universe: 股票池 DataFrame，至少包含 ts_code 列。

        Returns:
            DataFrame，包含 ts_code 和 factor_value 两列。
            factor_value 为 NaN 表示该股票该因子值缺失。
        """
        raise NotImplementedError

    # ----------------------------------------------------------
    # 通用数据获取工具
    # ----------------------------------------------------------

    @staticmethod
    def _build_in_clause(codes: list[str], prefix: str = "code") -> tuple[str, dict]:
        """
        构建 IN 子句的参数化占位符。

        SQLAlchemy text() 不支持直接传递列表参数，
        因此需要为每个元素生成独立的命名占位符。

        Args:
            codes: 值列表。
            prefix: 占位符前缀。

        Returns:
            (sql_fragment, params_dict)
            例如 codes=['000001.SZ','000002.SZ'] 返回
            ("(:code_0, :code_1)", {'code_0': '000001.SZ', 'code_1': '000002.SZ'})
        """
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
        universe_codes: Optional[list[str]] = None,
    ) -> pd.DataFrame:
        """
        获取截止到指定日期的最新财务数据（按公告日期，防止未来函数）。

        对每只股票，取 ann_date <= date 的最近一条记录。

        Args:
            date: 截止日期，格式 YYYY-MM-DD。
            columns: 需要的财务字段列表（如 ["pe_ttm", "pb"]）。
            universe_codes: 股票代码列表（可选，限定范围）。

        Returns:
            DataFrame，包含 ts_code 和请求的列。
        """
        # 缓存键仅用 date（始终获取宽列集以复用）
        cache_key = ("financial", date)
        cached = self._date_cache.get(cache_key)
        if cached is not None:
            df = cached
            if universe_codes:
                df = df[df["ts_code"].isin(universe_codes)]
            return df[["ts_code", "ann_date", "end_date"] + [c for c in columns if c in df.columns]].copy()

        # 预加载数据可用时，从内存过滤
        bulk_fin = self._static_cache.get("_bulk_financial")
        if bulk_fin is not None and not bulk_fin.empty:
            date_ts = pd.to_datetime(date)
            df = bulk_fin[bulk_fin["ann_date"] <= date_ts].copy()
            df = df.sort_values("end_date", ascending=False).drop_duplicates(subset=["ts_code"], keep="first")
            self._date_cache[cache_key] = df
            if universe_codes:
                df = df[df["ts_code"].isin(universe_codes)]
            return df[["ts_code", "ann_date", "end_date"] + [c for c in columns if c in df.columns]].copy()

        # 获取宽列数据（合并多个因子常用列，减少重复查询）
        all_columns = list(set(columns) | {"roe_ttm", "gross_margin", "bps", "net_profit", "revenue"})
        cols_str = ", ".join(["ts_code", "ann_date", "end_date"] + all_columns)
        params: dict = {"date": date}

        inner_sql = (
            f"SELECT {cols_str}, ROW_NUMBER() OVER "
            f"(PARTITION BY ts_code ORDER BY end_date DESC) as rn "
            f"FROM financial_data WHERE ann_date <= :date"
        )

        if universe_codes and len(universe_codes) <= self._IN_CLAUSE_THRESHOLD:
            in_clause, in_params = self._build_in_clause(universe_codes)
            inner_sql += f" AND ts_code IN {in_clause}"
            params.update(in_params)

        sql = f"SELECT * FROM ({inner_sql}) t WHERE rn = 1"

        df = self.db.query(sql, params=params)

        if df.empty:
            self._date_cache[cache_key] = df
            return df

        # 移除辅助列
        df = df.drop(columns=["rn"], errors="ignore")
        self._date_cache[cache_key] = df

        if universe_codes:
            df = df[df["ts_code"].isin(universe_codes)]
        return df[["ts_code", "ann_date", "end_date"] + [c for c in columns if c in df.columns]].copy()

    def get_price_history(
        self,
        end_date: str,
        lookback_days: int,
        universe_codes: Optional[list[str]] = None,
        columns: Optional[list[str]] = None,
    ) -> pd.DataFrame:
        """
        获取截止到指定日期的历史行情数据。

        Args:
            end_date: 截止日期，格式 YYYY-MM-DD。
            lookback_days: 向前回看的自然日天数。
            universe_codes: 股票代码列表（可选）。
            columns: 需要的行情字段（默认全部）。

        Returns:
            日线行情 DataFrame。
        """
        # 缓存：按 (end_date, lookback_days) 缓存宽表，请求列从缓存过滤
        cache_key = ("price_hist", end_date, lookback_days)
        cached = self._date_cache.get(cache_key)
        if cached is not None:
            df = cached
            if universe_codes:
                df = df[df["ts_code"].isin(universe_codes)]
            if columns:
                keep = ["ts_code", "trade_date"] + [c for c in columns if c in df.columns]
                df = df[keep]
            return df.copy()

        start_dt = pd.to_datetime(end_date) - pd.Timedelta(days=lookback_days)
        start_date = start_dt.strftime("%Y-%m-%d")

        # 预加载数据可用时，从内存过滤
        bulk_daily = self._static_cache.get("_bulk_daily")
        if bulk_daily is not None and not bulk_daily.empty:
            end_ts = pd.to_datetime(end_date)
            mask = (bulk_daily["trade_date"] >= start_dt) & (bulk_daily["trade_date"] <= end_ts)
            result = bulk_daily[mask].copy()
            if universe_codes:
                result = result[result["ts_code"].isin(universe_codes)]
            self._date_cache[cache_key] = result
            if columns:
                keep = ["ts_code", "trade_date"] + [c for c in columns if c in result.columns]
                result = result[keep]
            return result.copy()

        # 始终获取常用列宽表以便复用（不含 open 保留字，因子不需要）
        base_cols = {"ts_code", "trade_date", "pct_chg", "turnover_rate",
                     "volume", "amount", "close", "adj_factor"}
        if columns:
            base_cols.update(columns)
        wide_cols = sorted(base_cols)
        cols_str = ", ".join(wide_cols)

        params: dict = {"start_date": start_date, "end_date": end_date}

        sql = (
            f"SELECT {cols_str} FROM daily_price "
            f"WHERE trade_date >= :start_date "
            f"AND trade_date <= :end_date"
        )

        if universe_codes and len(universe_codes) <= self._IN_CLAUSE_THRESHOLD:
            in_clause, in_params = self._build_in_clause(universe_codes)
            sql += f" AND ts_code IN {in_clause}"
            params.update(in_params)

        sql += " ORDER BY ts_code, trade_date"

        result = self.db.query(sql, params=params)
        self._date_cache[cache_key] = result

        if columns:
            keep = ["ts_code", "trade_date"] + [c for c in columns if c in result.columns]
            result = result[keep]
        return result.copy()

    def get_month_end_price(
        self,
        date: str,
        months_ago: int,
        universe_codes: Optional[list[str]] = None,
    ) -> pd.DataFrame:
        """
        获取 N 个月前月末的前复权收盘价。

        使用 adj_factor 计算前复权价格：adj_close = close * adj_factor。
        adj_factor 为 NULL 时 fillna(1.0) 保持向后兼容。

        Args:
            date: 基准日期。
            months_ago: 向前推 N 个月。
            universe_codes: 股票代码列表（可选）。

        Returns:
            DataFrame，包含 ts_code 和 close 列（前复权价格）。
        """
        cache_key = ("month_end", date, months_ago)
        cached = self._date_cache.get(cache_key)
        if cached is not None:
            if universe_codes:
                return cached[cached["ts_code"].isin(universe_codes)].copy()
            return cached.copy()

        target_date = pd.to_datetime(date) - pd.DateOffset(months=months_ago)
        # 取目标月份的最后一个交易日
        month_start_dt = target_date.replace(day=1)
        month_end_dt = month_start_dt + pd.DateOffset(months=1) - pd.Timedelta(days=1)

        # 预加载数据可用时，从内存过滤
        bulk_daily = self._static_cache.get("_bulk_daily")
        if bulk_daily is not None and not bulk_daily.empty:
            mask = (bulk_daily["trade_date"] >= month_start_dt) & (bulk_daily["trade_date"] <= month_end_dt)
            df = bulk_daily[mask].copy()
            if universe_codes:
                df = df[df["ts_code"].isin(universe_codes)]
            if df.empty:
                self._date_cache[cache_key] = df
                return df
            # 每只股票取月内最后一个交易日
            df = df.sort_values("trade_date", ascending=False).drop_duplicates(subset=["ts_code"], keep="first")
            df["adj_factor"] = pd.to_numeric(df["adj_factor"], errors="coerce").fillna(1.0)
            df["close"] = pd.to_numeric(df["close"], errors="coerce") * df["adj_factor"]
            result = df[["ts_code", "close"]]
            self._date_cache[cache_key] = result
            return result.copy()

        month_start = month_start_dt.strftime("%Y-%m-%d")
        month_end = month_end_dt.strftime("%Y-%m-%d")

        params: dict = {"month_start": month_start, "month_end": month_end}

        inner_sql = (
            "SELECT ts_code, trade_date, close, adj_factor, "
            "ROW_NUMBER() OVER (PARTITION BY ts_code ORDER BY trade_date DESC) as rn "
            "FROM daily_price "
            "WHERE trade_date >= :month_start "
            "AND trade_date <= :month_end"
        )

        if universe_codes and len(universe_codes) <= self._IN_CLAUSE_THRESHOLD:
            in_clause, in_params = self._build_in_clause(universe_codes)
            inner_sql += f" AND ts_code IN {in_clause}"
            params.update(in_params)

        sql = f"SELECT * FROM ({inner_sql}) t WHERE rn = 1"

        df = self.db.query(sql, params=params)
        if df.empty:
            self._date_cache[cache_key] = df
            return df

        # 计算前复权价格
        df["adj_factor"] = pd.to_numeric(df["adj_factor"], errors="coerce").fillna(1.0)
        df["close"] = pd.to_numeric(df["close"], errors="coerce") * df["adj_factor"]

        result = df[["ts_code", "close"]]
        self._date_cache[cache_key] = result
        return result.copy()

    def _compute_ttm_vectorized(
        self,
        date: str,
        value_col: str,
        result_col: str,
        universe_codes: Optional[list[str]] = None,
    ) -> pd.DataFrame:
        """
        向量化 TTM 计算（供 get_ttm_net_profit / get_ttm_revenue 内部使用）。

        用 groupby + merge 替代逐股票循环，将 O(N) 次 DataFrame 筛选
        压缩为 2 次 merge 操作。

        Args:
            date: 截止日期。
            value_col: 财务数据列名（如 "net_profit"）。
            result_col: 输出列名（如 "ttm_net_profit"）。
            universe_codes: 股票代码列表（可选）。

        Returns:
            DataFrame[ts_code, result_col]。
        """
        # 预加载数据可用时，从内存过滤
        bulk_fin = self._static_cache.get("_bulk_financial")
        if bulk_fin is not None and not bulk_fin.empty:
            date_ts = pd.to_datetime(date)
            df = bulk_fin[["ts_code", "ann_date", "end_date", value_col]].copy()
            df = df[(df["ann_date"] <= date_ts) & df[value_col].notna()]
            if universe_codes:
                df = df[df["ts_code"].isin(universe_codes)]
            df = df.sort_values(["ts_code", "end_date"], ascending=[True, False])
        else:
            params: dict = {"date": date}
            sql = (
                f"SELECT ts_code, ann_date, end_date, {value_col} FROM financial_data "
                f"WHERE ann_date <= :date AND {value_col} IS NOT NULL"
            )
            if universe_codes and len(universe_codes) <= self._IN_CLAUSE_THRESHOLD:
                in_clause, in_params = self._build_in_clause(universe_codes)
                sql += f" AND ts_code IN {in_clause}"
                params.update(in_params)
            sql += " ORDER BY ts_code, end_date DESC"

            df = self.db.query(sql, params=params)
        if df.empty:
            return pd.DataFrame(columns=["ts_code", result_col])

        df["end_date"] = pd.to_datetime(df["end_date"])
        df[value_col] = pd.to_numeric(df[value_col], errors="coerce")

        # groupby 保序，first() 取每只股票最新一期（SQL 已按 end_date DESC 排序）
        latest = df.groupby("ts_code").first().reset_index()[["ts_code", "end_date", value_col]]
        latest.columns = ["ts_code", "latest_end", "current_val"]
        latest["month"] = latest["latest_end"].dt.month

        # 年报（month==12）：TTM = 当期值
        mask_annual = latest["month"] == 12
        annual = latest.loc[mask_annual, ["ts_code", "current_val"]].copy()
        annual.rename(columns={"current_val": result_col}, inplace=True)

        non_annual = latest[~mask_annual].copy()
        if non_annual.empty:
            return annual[["ts_code", result_col]].reset_index(drop=True)

        # 构建 lookup：(ts_code, end_date) → value（去重后取第一条）
        lookup = df.drop_duplicates(subset=["ts_code", "end_date"])[
            ["ts_code", "end_date", value_col]
        ]

        # 向量化构建上年年报 end_date 和上年同期 end_date
        non_annual["prev_annual_end"] = pd.to_datetime(dict(
            year=non_annual["latest_end"].dt.year - 1, month=12, day=31,
        ))
        non_annual["prev_same_end"] = pd.to_datetime(dict(
            year=non_annual["latest_end"].dt.year - 1,
            month=non_annual["latest_end"].dt.month,
            day=non_annual["latest_end"].dt.day,
        ))

        # merge 上年年报
        non_annual = non_annual.merge(
            lookup.rename(columns={"end_date": "prev_annual_end", value_col: "annual_val"}),
            on=["ts_code", "prev_annual_end"],
            how="left",
        )
        # merge 上年同期
        non_annual = non_annual.merge(
            lookup.rename(columns={"end_date": "prev_same_end", value_col: "same_val"}),
            on=["ts_code", "prev_same_end"],
            how="left",
        )

        # TTM = 当期累计 + 上年年报 - 上年同期累计
        non_annual[result_col] = np.where(
            non_annual["annual_val"].notna() & non_annual["same_val"].notna(),
            non_annual["current_val"] + non_annual["annual_val"] - non_annual["same_val"],
            np.nan,
        )

        return pd.concat(
            [annual[["ts_code", result_col]], non_annual[["ts_code", result_col]]],
            ignore_index=True,
        )

    def get_ttm_net_profit(
        self,
        date: str,
        universe_codes: Optional[list[str]] = None,
    ) -> pd.DataFrame:
        """
        计算截止到指定日期的 TTM 净利润（滚动四季度）。

        计算逻辑（net_profit 是季度累计值）：
            - 年报期（12月）: TTM = 当期净利润
            - 其他期: TTM = 当期累计 + 上年年报 - 上年同期累计
        严格遵守 ann_date <= date 防止未来函数。

        Args:
            date: 截止日期，格式 YYYY-MM-DD。
            universe_codes: 股票代码列表（可选）。

        Returns:
            DataFrame，包含 ts_code 和 ttm_net_profit 列。
        """
        cache_key = ("ttm_np", date)
        cached = self._date_cache.get(cache_key)
        if cached is not None:
            if universe_codes:
                return cached[cached["ts_code"].isin(universe_codes)].copy()
            return cached.copy()

        result = self._compute_ttm_vectorized(date, "net_profit", "ttm_net_profit", universe_codes)
        self._date_cache[cache_key] = result
        if universe_codes:
            return result[result["ts_code"].isin(universe_codes)].copy()
        return result.copy()

    def get_ttm_revenue(
        self,
        date: str,
        universe_codes: Optional[list[str]] = None,
    ) -> pd.DataFrame:
        """
        计算截止到指定日期的 TTM 营收（滚动四季度）。

        计算逻辑（revenue 是季度累计值）：
            - 年报期（12月）: TTM = 当期营收
            - 其他期: TTM = 当期累计 + 上年年报 - 上年同期累计
        严格遵守 ann_date <= date 防止未来函数。

        Args:
            date: 截止日期，格式 YYYY-MM-DD。
            universe_codes: 股票代码列表（可选）。

        Returns:
            DataFrame，包含 ts_code 和 ttm_revenue 列。
        """
        cache_key = ("ttm_rev", date)
        cached = self._date_cache.get(cache_key)
        if cached is not None:
            if universe_codes:
                return cached[cached["ts_code"].isin(universe_codes)].copy()
            return cached.copy()

        result = self._compute_ttm_vectorized(date, "revenue", "ttm_revenue", universe_codes)
        self._date_cache[cache_key] = result
        if universe_codes:
            return result[result["ts_code"].isin(universe_codes)].copy()
        return result.copy()

    def get_close_on_date(
        self,
        date: str,
        universe_codes: Optional[list[str]] = None,
    ) -> pd.DataFrame:
        """
        获取指定日期的收盘价。如果当日无数据，取之前最近一个交易日。

        Args:
            date: 日期，格式 YYYY-MM-DD。
            universe_codes: 股票代码列表（可选）。

        Returns:
            DataFrame，包含 ts_code 和 close 列。
        """
        cache_key = ("close_on_date", date)
        cached = self._date_cache.get(cache_key)
        if cached is not None:
            if universe_codes:
                return cached[cached["ts_code"].isin(universe_codes)].copy()
            return cached.copy()

        date_ts = pd.to_datetime(date)
        lookback_ts = date_ts - pd.Timedelta(days=10)

        # 预加载数据可用时，从内存过滤
        bulk_daily = self._static_cache.get("_bulk_daily")
        if bulk_daily is not None and not bulk_daily.empty:
            mask = (bulk_daily["trade_date"] >= lookback_ts) & (bulk_daily["trade_date"] <= date_ts)
            df = bulk_daily[mask].copy()
            if universe_codes:
                df = df[df["ts_code"].isin(universe_codes)]
            if df.empty:
                result = pd.DataFrame(columns=["ts_code", "close"])
                self._date_cache[cache_key] = result
                return result
            df = df.sort_values("trade_date", ascending=False).drop_duplicates(subset=["ts_code"], keep="first")
            result = df[["ts_code", "close"]]
            self._date_cache[cache_key] = result
            return result.copy()

        lookback = lookback_ts.strftime("%Y-%m-%d")
        params: dict = {"lookback": lookback, "date": date}

        inner_sql = (
            "SELECT ts_code, trade_date, close, "
            "ROW_NUMBER() OVER (PARTITION BY ts_code ORDER BY trade_date DESC) as rn "
            "FROM daily_price "
            "WHERE trade_date >= :lookback AND trade_date <= :date"
        )
        if universe_codes and len(universe_codes) <= self._IN_CLAUSE_THRESHOLD:
            in_clause, in_params = self._build_in_clause(universe_codes)
            inner_sql += f" AND ts_code IN {in_clause}"
            params.update(in_params)

        sql = f"SELECT * FROM ({inner_sql}) t WHERE rn = 1"

        df = self.db.query(sql, params=params)
        if df.empty:
            result = pd.DataFrame(columns=["ts_code", "close"])
            self._date_cache[cache_key] = result
            return result

        result = df[["ts_code", "close"]]
        self._date_cache[cache_key] = result
        return result.copy()

    def get_total_share(
        self,
        universe_codes: Optional[list[str]] = None,
    ) -> pd.DataFrame:
        """
        从 stock_basic 获取总股本（万股）。

        Args:
            universe_codes: 股票代码列表（可选）。

        Returns:
            DataFrame，包含 ts_code 和 total_share 列。
        """
        cache_key = "total_share"
        cached = self._static_cache.get(cache_key)
        if cached is not None:
            if universe_codes:
                return cached[cached["ts_code"].isin(universe_codes)].copy()
            return cached.copy()

        # 获取全量数据
        sql = "SELECT ts_code, total_share FROM stock_basic WHERE total_share IS NOT NULL"
        result = self.db.query(sql, params={})
        self._static_cache[cache_key] = result

        if universe_codes:
            return result[result["ts_code"].isin(universe_codes)].copy()
        return result.copy()

    def __repr__(self) -> str:
        return f"<Factor: {self.name}>"
