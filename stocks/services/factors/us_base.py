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
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional

import numpy as np
import pandas as pd
import polars as pl

from services.config import LOG_LEVEL
from stocks.models import (
    USFinancialData, USDailyPrice, USIndexDaily,
    USAnalystRecommendation, USEarningsSurprise, USEpsEstimate,
    USCorporateAction, USEnterpriseValue, USInsiderTrade,
    USStockBasic,
)

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
    # 回测预加载
    # ----------------------------------------------------------
    @classmethod
    def _find_cache(cls, table: str, start: str, end: str) -> "Path | None":
        """查找可用的 parquet 缓存：已有缓存范围包含请求范围即可。"""
        from services.config import PROJECT_ROOT
        cache_dir = PROJECT_ROOT / "cache"
        if not cache_dir.exists():
            return None
        for f in cache_dir.glob(f"{table}_*.parquet"):
            parts = f.stem.replace(f"{table}_", "").split("_")
            if len(parts) == 2:
                cached_start, cached_end = parts
                if cached_start <= start and cached_end >= end:
                    return f
        return None

    # 超过此行数阈值的表在 DB 冷查询时按年分片并行
    _SHARD_THRESHOLD_YEARS = 3

    @classmethod
    def _load_or_query(cls, table: str, model, cols: list[str],
                       start: str, end: str, date_field: str,
                       order_by: list[str] | None = None,
                       extra_filters: dict | None = None) -> pl.DataFrame:
        """
        先找可用 parquet 缓存（范围包含即命中），没有则查 Django ORM 后存缓存。
        DB 冷查询时，若日期跨度 >= _SHARD_THRESHOLD_YEARS 年，按年分片并行查询。
        返回 polars DataFrame。
        """
        import time
        from services.config import PROJECT_ROOT

        hit = cls._find_cache(table, start, end)
        if hit:
            t0 = time.time()
            df = pl.read_parquet(hit)
            if date_field in df.columns:
                # polars 日期过滤
                dt_col = df[date_field]
                if dt_col.dtype == pl.Utf8:
                    df = df.with_columns(pl.col(date_field).str.to_date().alias(date_field))
                elif dt_col.dtype == pl.Datetime:
                    df = df.with_columns(pl.col(date_field).cast(pl.Date))
                df = df.filter(
                    (pl.col(date_field) >= pl.lit(start).str.to_date())
                    & (pl.col(date_field) <= pl.lit(end).str.to_date())
                )
            logger.info(f"US 预加载 {table}: {df.height} 行 (parquet 缓存 {hit.name}, {time.time()-t0:.1f}s)")
            return df

        t0 = time.time()
        start_ts = pd.to_datetime(start)
        end_ts = pd.to_datetime(end)
        span_years = (end_ts - start_ts).days / 365.25

        if span_years >= cls._SHARD_THRESHOLD_YEARS:
            df = cls._query_sharded(model, cols, start, end, date_field, order_by, extra_filters)
        else:
            df = cls._query_single(model, cols, start, end, date_field, order_by, extra_filters)

        logger.info(f"US 预加载 {table}: {df.height} 行 (DB 查询, {time.time()-t0:.1f}s)")

        if df.height > 0:
            cache_dir = PROJECT_ROOT / "cache"
            cache_dir.mkdir(exist_ok=True)
            path = cache_dir / f"{table}_{start}_{end}.parquet"
            df.write_parquet(path)
            logger.info(f"  → 已缓存到 {path.name}")
        return df

    @classmethod
    def _query_single(cls, model, cols, start, end, date_field, order_by,
                      extra_filters=None) -> pl.DataFrame:
        """单次 ORM 查询，返回 polars DataFrame。"""
        filters = {
            f"{date_field}__gte": start,
            f"{date_field}__lte": end,
        }
        if extra_filters:
            filters.update(extra_filters)
        qs = model.objects.filter(**filters)
        if order_by:
            qs = qs.order_by(*order_by)
        rows = list(qs.values_list(*cols))
        if not rows:
            return pl.DataFrame(schema={c: pl.Utf8 for c in cols})
        return pl.DataFrame(rows, schema=cols, orient="row")

    @classmethod
    def _query_sharded(cls, model, cols, start, end, date_field, order_by,
                       extra_filters=None) -> pl.DataFrame:
        """按年分片，ThreadPoolExecutor 并发查询，pl.concat 合并。"""
        from django.db import connections

        start_ts = pd.to_datetime(start)
        end_ts = pd.to_datetime(end)

        shards = []
        cursor = start_ts
        while cursor <= end_ts:
            shard_end = min(cursor.replace(month=12, day=31), end_ts)
            shards.append((cursor.strftime("%Y-%m-%d"), shard_end.strftime("%Y-%m-%d")))
            cursor = shard_end + pd.Timedelta(days=1)

        def _query_shard(shard_start, shard_end):
            filters = {
                f"{date_field}__gte": shard_start,
                f"{date_field}__lte": shard_end,
            }
            if extra_filters:
                filters.update(extra_filters)
            qs = model.objects.filter(**filters)
            if order_by:
                qs = qs.order_by(*order_by)
            rows = list(qs.values_list(*cols))
            if not rows:
                return pl.DataFrame(schema={c: pl.Utf8 for c in cols})
            return pl.DataFrame(rows, schema=cols, orient="row")

        connections.close_all()

        parts = []
        with ThreadPoolExecutor(max_workers=min(len(shards), 8)) as pool:
            futures = {
                pool.submit(_query_shard, s, e): (s, e) for s, e in shards
            }
            for future in as_completed(futures):
                shard_range = futures[future]
                try:
                    part = future.result()
                    if part.height > 0:
                        parts.append(part)
                except Exception as exc:
                    logger.warning(f"分片查询 {shard_range} 失败: {exc}")

        connections.close_all()

        if not parts:
            return pl.DataFrame(schema={c: pl.Utf8 for c in cols})

        df = pl.concat(parts)
        if order_by:
            sort_cols = [c.lstrip("-") for c in order_by]
            descending = [c.startswith("-") for c in order_by]
            valid = [(c, d) for c, d in zip(sort_cols, descending) if c in df.columns]
            if valid:
                df = df.sort([v[0] for v in valid], descending=[v[1] for v in valid])
        return df

    @classmethod
    def preload_for_backtest(cls, start_date: str, end_date: str, **kwargs):
        """
        一次性并行预加载回测区间所需数据到内存（Django ORM + parquet 缓存）。

        所有独立表通过 ThreadPoolExecutor 并发加载（IO-bound），
        首次从 DB 加载后缓存到本地 parquet，后续直接读文件。
        """
        import time
        from django.db import connections

        t_total = time.time()
        logger.info(f"US 并行预加载开始: {start_date} ~ {end_date}")

        price_start = (pd.to_datetime(start_date) - pd.Timedelta(days=400)).strftime("%Y-%m-%d")
        fin_start = (pd.to_datetime(start_date) - pd.Timedelta(days=5*365)).strftime("%Y-%m-%d")
        analyst_start = (pd.to_datetime(start_date) - pd.Timedelta(days=180)).strftime("%Y-%m-%d")
        es_start = analyst_start
        ee_start = (pd.to_datetime(start_date) - pd.Timedelta(days=400)).strftime("%Y-%m-%d")
        div_start = ee_start
        mktcap_start = (pd.to_datetime(start_date) - pd.Timedelta(days=120)).strftime("%Y-%m-%d")
        insider_start = mktcap_start

        # ---- 每张表的加载函数（独立，可并发） ----

        def _load_financial():
            t0 = time.time()
            fin_cols = [
                "ticker", "period", "date", "filing_date",
                "revenue", "net_income", "eps", "gross_profit",
                "operating_income", "total_stockholders_equity",
                "total_equity", "total_assets",
                "total_debt", "free_cash_flow",
                "weighted_average_shs_out",
            ]
            df = cls._load_or_query(
                "us_financial_data", USFinancialData, fin_cols,
                fin_start, end_date, "filing_date",
            )
            if df.height > 0:
                # 日期转换
                df = df.with_columns([
                    pl.col("filing_date").cast(pl.Date, strict=False),
                    pl.col("date").cast(pl.Date, strict=False),
                ])
                # filing_date 修正
                bad = df.filter(pl.col("filing_date") <= pl.col("date"))
                n_fixed = bad.height
                if n_fixed > 0:
                    df = df.with_columns(
                        pl.when(pl.col("filing_date") <= pl.col("date"))
                        .then(pl.col("date").dt.offset_by("45d"))
                        .otherwise(pl.col("filing_date"))
                        .alias("filing_date")
                    )
                    logger.info(f"Filing date 修正: {n_fixed} 条")
                # 数值转换
                num_cols = ["revenue", "gross_profit", "operating_income", "net_income",
                            "total_stockholders_equity", "weighted_average_shs_out"]
                for col in num_cols:
                    if col in df.columns:
                        df = df.with_columns(pl.col(col).cast(pl.Float64, strict=False))
                # 衍生列
                df = df.with_columns([
                    (pl.col("gross_profit") / pl.when(pl.col("revenue") == 0).then(None).otherwise(pl.col("revenue"))).alias("gross_margin"),
                    (pl.col("operating_income") / pl.when(pl.col("revenue") == 0).then(None).otherwise(pl.col("revenue"))).alias("operating_margin"),
                    (pl.col("net_income") / pl.when(pl.col("total_stockholders_equity") == 0).then(None).otherwise(pl.col("total_stockholders_equity"))).alias("roe"),
                    pl.col("weighted_average_shs_out").alias("weighted_avg_shares"),
                ])
            logger.info(f"  _bulk_financial: {df.height} 行, {time.time()-t0:.1f}s")
            return "_bulk_financial", df

        def _load_daily_price():
            t0 = time.time()
            price_cols = [
                "ticker", "trade_date", "open", "high", "low",
                "close", "adj_close", "volume", "change_percent",
            ]
            df = cls._load_or_query(
                "us_daily_price", USDailyPrice, price_cols,
                price_start, end_date, "trade_date",
                order_by=["ticker", "trade_date"],
            )
            if df.height > 0:
                df = df.with_columns([
                    pl.col("trade_date").cast(pl.Date, strict=False),
                    pl.col("adj_close").cast(pl.Float64, strict=False),
                    pl.col("close").cast(pl.Float64, strict=False),
                ])
                df = df.with_columns(
                    pl.when(pl.col("adj_close").is_null())
                    .then(pl.col("close"))
                    .otherwise(pl.col("adj_close"))
                    .alias("adj_close")
                )
            logger.info(f"  _bulk_daily: {df.height} 行, {time.time()-t0:.1f}s")
            return "_bulk_daily", df

        def _load_index():
            t0 = time.time()
            idx_cols = ["index_code", "trade_date", "close"]
            df = cls._load_or_query(
                "us_index_daily_gspc", USIndexDaily, idx_cols,
                price_start, end_date, "trade_date",
                order_by=["trade_date"],
                extra_filters={"index_code": "^GSPC"},
            )
            if df.height > 0:
                df = df.with_columns([
                    pl.col("trade_date").cast(pl.Date, strict=False),
                    pl.col("close").cast(pl.Float64, strict=False),
                ])
            logger.info(f"  _bulk_index: {df.height} 行, {time.time()-t0:.1f}s")
            return "_bulk_index", df

        def _load_analyst():
            t0 = time.time()
            ar_cols = ["ticker", "date", "grading_company", "new_grade", "action"]
            df = cls._load_or_query(
                "us_analyst_recommendation", USAnalystRecommendation, ar_cols,
                analyst_start, end_date, "date",
                order_by=["ticker", "date"],
            )
            if df.height > 0:
                df = df.with_columns(pl.col("date").cast(pl.Date, strict=False))
            logger.info(f"  _bulk_analyst: {df.height} 行, {time.time()-t0:.1f}s")
            return "_bulk_analyst", df

        def _load_earnings_surprise():
            t0 = time.time()
            es_cols = ["ticker", "date", "eps_actual", "eps_estimated", "surprise", "surprise_pct"]
            df = cls._load_or_query(
                "us_earnings_surprise", USEarningsSurprise, es_cols,
                es_start, end_date, "date",
                order_by=["ticker", "date"],
            )
            if df.height > 0:
                df = df.with_columns(pl.col("date").cast(pl.Date, strict=False))
            logger.info(f"  _bulk_earnings_surprise: {df.height} 行, {time.time()-t0:.1f}s")
            return "_bulk_earnings_surprise", df

        def _load_eps_estimate():
            t0 = time.time()
            ee_cols = ["ticker", "date", "estimated_eps_avg", "estimated_eps_low",
                       "estimated_eps_high", "number_analysts_estimated_eps"]
            df = cls._load_or_query(
                "us_eps_estimate", USEpsEstimate, ee_cols,
                ee_start, end_date, "date",
                order_by=["ticker", "date"],
            )
            if df.height > 0:
                df = df.with_columns(pl.col("date").cast(pl.Date, strict=False))
            logger.info(f"  _bulk_eps_estimate: {df.height} 行, {time.time()-t0:.1f}s")
            return "_bulk_eps_estimate", df

        def _load_dividends():
            t0 = time.time()
            ca_cols = ["ticker", "date", "action_type", "dividend"]
            df = cls._load_or_query(
                "us_corporate_action_div", USCorporateAction, ca_cols,
                div_start, end_date, "date",
                order_by=["ticker", "date"],
                extra_filters={"action_type": "dividend"},
            )
            if df.height > 0:
                df = df.with_columns(pl.col("date").cast(pl.Date, strict=False))
            logger.info(f"  _bulk_dividends: {df.height} 行, {time.time()-t0:.1f}s")
            return "_bulk_dividends", df

        def _load_mktcap():
            t0 = time.time()
            ev_cols = ["ticker", "date", "market_capitalization"]
            df = cls._load_or_query(
                "us_enterprise_value_mktcap", USEnterpriseValue, ev_cols,
                mktcap_start, end_date, "date",
                order_by=["ticker", "date"],
                extra_filters={
                    "market_capitalization__isnull": False,
                    "market_capitalization__gt": 0,
                },
            )
            if df.height > 0:
                df = df.with_columns([
                    pl.col("date").cast(pl.Date, strict=False),
                    pl.col("market_capitalization").cast(pl.Float64, strict=False).alias("market_cap"),
                ])
                if "market_capitalization" in df.columns:
                    df = df.drop("market_capitalization")
            logger.info(f"  _bulk_mktcap: {df.height} 行, {time.time()-t0:.1f}s")
            return "_bulk_mktcap", df

        def _load_insider():
            t0 = time.time()
            insider_types = ["P-Purchase", "S-Sale", "A-Award", "P-Purchase+", "S-Sale+"]
            ins_cols = ["ticker", "filing_date", "acquisition_or_disposition",
                        "securities_transacted", "price"]
            df = cls._load_or_query(
                "us_insider_trade", USInsiderTrade, ins_cols,
                insider_start, end_date, "filing_date",
                extra_filters={
                    "price__gt": 0,
                    "securities_transacted__gt": 0,
                    "transaction_type__in": insider_types,
                },
            )
            if df.height > 0:
                df = df.with_columns([
                    pl.col("filing_date").cast(pl.Date, strict=False),
                    pl.col("securities_transacted").cast(pl.Float64, strict=False),
                    pl.col("price").cast(pl.Float64, strict=False),
                ])
                df = df.with_columns(
                    pl.when(pl.col("acquisition_or_disposition") == "A")
                    .then(pl.col("securities_transacted") * pl.col("price"))
                    .otherwise(-pl.col("securities_transacted") * pl.col("price"))
                    .alias("net_value")
                )
                df = df.select(["ticker", "filing_date", "net_value"])
            logger.info(f"  _bulk_insider: {len(df)} 行, {time.time()-t0:.1f}s")
            return "_bulk_insider", df

        # ---- 并行提交所有基础表加载任务 ----
        loaders = [
            _load_financial,
            _load_daily_price,
            _load_index,
            _load_analyst,
            _load_earnings_surprise,
            _load_eps_estimate,
            _load_dividends,
            _load_mktcap,
            _load_insider,
        ]

        # Django ORM 在多线程中需要关闭陈旧连接
        connections.close_all()

        with ThreadPoolExecutor(max_workers=len(loaders)) as pool:
            futures = {pool.submit(fn): fn.__name__ for fn in loaders}
            for future in as_completed(futures):
                name = futures[future]
                try:
                    cache_key, df = future.result()
                    if cache_key is not None:
                        cls._static_cache[cache_key] = df
                except Exception as e:
                    logger.error(f"并行预加载 {name} 失败: {e}")

        connections.close_all()

        # AlphaSignal 预加载依赖 _bulk_daily（复用检查），必须在基础表之后
        from stocks.services.factors.us_registry import AlphaSignal
        AlphaSignal.preload_alpha_cache(start_date, end_date)

        logger.info(f"US 并行预加载完成: {time.time()-t_total:.1f}s")

    @classmethod
    def precompute_rolling_stats(cls):
        """
        一次性预计算动量/技术因子所需的 rolling 统计量（polars 多线程）。
        必须在 preload_for_backtest() 之后调用。
        """
        import time
        t0 = time.time()

        bulk_daily = cls._static_cache.get("_bulk_daily")
        if bulk_daily is None or (isinstance(bulk_daily, pl.DataFrame) and bulk_daily.is_empty()):
            logger.debug("precompute_rolling_stats: 预加载日线数据为空，跳过rolling预计算")
            return

        df = bulk_daily.select(["ticker", "trade_date", "adj_close", "close",
                                "change_percent", "volume"])

        # 类型转换
        df = df.with_columns([
            pl.col("adj_close").cast(pl.Float64, strict=False),
            pl.col("close").cast(pl.Float64, strict=False),
            pl.col("change_percent").cast(pl.Float64, strict=False),
            pl.col("volume").cast(pl.Float64, strict=False),
        ])

        # adj_close 填充
        df = df.with_columns(
            pl.when(pl.col("adj_close").is_null())
            .then(pl.col("close"))
            .otherwise(pl.col("adj_close"))
            .alias("adj_close")
        )

        df = df.sort(["ticker", "trade_date"])

        # 收益率：优先用 change_percent，否则从 adj_close 算
        df = df.with_columns(
            pl.col("adj_close").pct_change().over("ticker").alias("ret_from_price"),
        )
        df = df.with_columns(
            pl.when(pl.col("change_percent").is_not_null())
            .then(pl.col("change_percent") / 100.0)
            .otherwise(pl.col("ret_from_price"))
            .alias("ret")
        )
        df = df.with_columns(
            (pl.col("ret").clip(-0.99, None) + 1.0).log().alias("log_ret"),
            (pl.col("adj_close") * pl.col("volume")).alias("dollar_volume"),
        )

        # rolling 统计量（polars 自动多线程）
        df = df.with_columns([
            # 5-day cumulative return (REV_5D)
            (pl.col("log_ret").rolling_sum(5, min_periods=3).over("ticker").exp() - 1.0)
            .alias("cum_ret_5d"),
            # 20-day cumulative return (RESIDUAL_MOM)
            (pl.col("log_ret").rolling_sum(20, min_periods=10).over("ticker").exp() - 1.0)
            .alias("cum_ret_20d"),
            # 20-day rolling mean dollar volume (TURN_20D proxy)
            pl.col("dollar_volume").rolling_mean(20, min_periods=10).over("ticker")
            .alias("dvol_20d"),
            # 20-day rolling std of returns (VOL_20D)
            pl.col("ret").rolling_std(20, min_periods=10).over("ticker")
            .alias("vol_20d"),
            # 60-day rolling mean adj_close (PRICE_DEV_60D)
            pl.col("adj_close").rolling_mean(60, min_periods=30).over("ticker")
            .alias("ma60_adj"),
        ])

        # 存储（polars DataFrame，用 filter 代替 xs）
        keep_cols = ["ticker", "trade_date", "adj_close", "cum_ret_5d", "cum_ret_20d",
                     "dvol_20d", "vol_20d", "ma60_adj", "volume", "dollar_volume"]
        cls._static_cache["_rolling_indexed"] = df.select(keep_cols)

        # 预计算月末复权收盘价（MOM_1M/3M/12M 使用）
        df_me = df.select(["ticker", "trade_date", "adj_close"])
        # 提取年月
        df_me = df_me.with_columns(
            pl.col("trade_date").cast(pl.Date).dt.strftime("%Y-%m").alias("year_month")
        )
        # 每个 (ticker, year_month) 取 trade_date 最大的那条
        month_ends = (
            df_me.sort("trade_date")
            .group_by(["ticker", "year_month"])
            .last()
            .select(["ticker", "year_month", "adj_close"])
        )
        cls._static_cache["_month_end_prices"] = month_ends

        logger.info(
            f"US 预计算 rolling stats + 月末价格: {df.height} 行, {time.time()-t0:.1f}s"
        )

    # ----------------------------------------------------------
    # Rolling stats helpers
    # ----------------------------------------------------------
    def _get_rolling_for_date(
        self, date: str, tickers: Optional[set[str]] = None,
    ) -> Optional[pl.DataFrame]:
        """从预计算的 rolling stats 中提取指定日期的截面数据。"""
        ri = self._static_cache.get("_rolling_indexed")
        if ri is None:
            logger.debug("_get_rolling_for_date: rolling预计算数据不存在")
            return None
        date_val = pd.to_datetime(date).date()
        try:
            day = ri.filter(pl.col("trade_date").cast(pl.Date) == date_val)
        except Exception:
            logger.debug(f"_get_rolling_for_date: 日期 {date} 过滤失败")
            return None
        if day.is_empty():
            logger.debug(f"_get_rolling_for_date: 日期 {date} 不在rolling数据中")
            return None
        if tickers:
            day = day.filter(pl.col("ticker").is_in(list(tickers)))
        return day

    def _get_month_end_adj_close(
        self, date: str, months_ago: int, tickers: Optional[set[str]] = None,
    ) -> Optional[pl.DataFrame]:
        """从预计算的月末价格中提取 N 月前月末的复权收盘价。"""
        me = self._static_cache.get("_month_end_prices")
        if me is None:
            logger.debug("_get_month_end_adj_close: 月末价格预计算数据不存在")
            return None
        target = pd.to_datetime(date) - pd.DateOffset(months=months_ago)
        target_ym = target.strftime("%Y-%m")
        result = me.filter(pl.col("year_month") == target_ym)
        if tickers:
            result = result.filter(pl.col("ticker").is_in(list(tickers)))
        if result.is_empty():
            logger.debug(f"_get_month_end_adj_close: {months_ago}月前月末无价格数据")
            return None
        return result.select(["ticker", "adj_close"])

    # ----------------------------------------------------------
    # 初始化
    # ----------------------------------------------------------
    def __init__(self, db=None, **kwargs):
        pass

    @abstractmethod
    def compute(self, date: str, universe: pl.DataFrame) -> pl.DataFrame:
        """
        计算因子值（截面计算）。

        Args:
            date: 计算日期，格式 YYYY-MM-DD。
            universe: polars DataFrame，至少包含 ticker 列。

        Returns:
            polars DataFrame，包含 ticker 和 factor_value 两列。
        """
        raise NotImplementedError

    # ----------------------------------------------------------
    # 行业映射
    # ----------------------------------------------------------
    def get_industry_map_cached(self) -> pl.DataFrame:
        """获取 GICS 行业映射（缓存）。返回 pl.DataFrame[ticker, sector, industry]。"""
        cached = self._static_cache.get("industry_map")
        if cached is not None:
            return cached
        from stocks.models import USIndustryClass
        rows = list(USIndustryClass.objects.values_list("ticker", "sector", "industry"))
        result = pl.DataFrame(rows, schema=["ticker", "sector", "industry"], orient="row") if rows else pl.DataFrame(schema={"ticker": pl.Utf8, "sector": pl.Utf8, "industry": pl.Utf8})
        self._static_cache["industry_map"] = result
        return result

    # ----------------------------------------------------------
    # 通用数据获取工具（全部从缓存读取，preload_for_backtest 必须先调用）
    # ----------------------------------------------------------

    def _pl_empty(self, cols: list[str]) -> pl.DataFrame:
        """创建空 polars DataFrame。"""
        return pl.DataFrame(schema={c: pl.Utf8 if c == "ticker" else pl.Float64 for c in cols})

    def _filter_tickers(self, df: pl.DataFrame, tickers: Optional[list[str]]) -> pl.DataFrame:
        if tickers:
            return df.filter(pl.col("ticker").is_in(tickers))
        return df

    def get_latest_financial(
        self, date: str, columns: list[str],
        universe_tickers: Optional[list[str]] = None,
    ) -> pl.DataFrame:
        """获取截止到指定日期的最新财务数据（按 filing_date）。"""
        cache_key = ("financial", date)
        cached = self._date_cache.get(cache_key)
        if cached is not None:
            df = self._filter_tickers(cached, universe_tickers)
            keep = ["ticker", "filing_date", "date", "period"] + [c for c in columns if c in df.columns]
            return df.select(keep)

        bulk_fin = self._static_cache.get("_bulk_financial")
        if bulk_fin is None or (isinstance(bulk_fin, pl.DataFrame) and bulk_fin.is_empty()):
            logger.warning("get_latest_financial: 缓存为空")
            return self._pl_empty(["ticker"] + columns)

        date_val = pd.to_datetime(date).date()
        df = bulk_fin.filter(pl.col("filing_date").cast(pl.Date) <= date_val)
        df = df.sort("date", descending=True).unique(subset=["ticker"], keep="first")
        self._date_cache[cache_key] = df
        df = self._filter_tickers(df, universe_tickers)
        keep = ["ticker", "filing_date", "date", "period"] + [c for c in columns if c in df.columns]
        return df.select(keep)

    def get_ttm_value(
        self, date: str, field: str,
        universe_tickers: Optional[list[str]] = None,
    ) -> pl.DataFrame:
        """计算 TTM 指标：最近 4 个季度的 field 求和。"""
        cache_key = ("ttm", date, field)
        cached = self._date_cache.get(cache_key)
        if cached is not None:
            return self._filter_tickers(cached, universe_tickers)

        bulk_fin = self._static_cache.get("_bulk_financial")
        if bulk_fin is None or (isinstance(bulk_fin, pl.DataFrame) and bulk_fin.is_empty()):
            logger.warning("get_ttm_value: 缓存为空")
            return self._pl_empty(["ticker", "ttm_value"])

        date_val = pd.to_datetime(date).date()
        df = bulk_fin.filter(pl.col("filing_date").cast(pl.Date) <= date_val)
        if universe_tickers:
            df = df.filter(pl.col("ticker").is_in(universe_tickers))
        # 每个 ticker 取最近 4 季
        df = df.sort("date", descending=True).group_by("ticker").head(4)
        result = df.group_by("ticker").agg([
            pl.col(field).sum().alias("ttm_value"),
            pl.col(field).count().alias("n_quarters"),
        ])
        # 至少 3 个季度
        result = result.with_columns(
            pl.when(pl.col("n_quarters") < 3).then(None).otherwise(pl.col("ttm_value")).alias("ttm_value")
        ).select(["ticker", "ttm_value"])
        self._date_cache[cache_key] = result
        return result

    def get_close_on_date(
        self, date: str, universe_tickers: Optional[list[str]] = None,
    ) -> pl.DataFrame:
        """获取指定日期的复权收盘价。"""
        cache_key = ("close", date)
        cached = self._date_cache.get(cache_key)
        if cached is not None:
            return self._filter_tickers(cached, universe_tickers)

        bulk_daily = self._static_cache.get("_bulk_daily")
        if bulk_daily is None or (isinstance(bulk_daily, pl.DataFrame) and bulk_daily.is_empty()):
            logger.warning("get_close_on_date: 缓存为空")
            return self._pl_empty(["ticker", "adj_close"])

        date_val = pd.to_datetime(date).date()
        day_df = bulk_daily.filter(pl.col("trade_date").cast(pl.Date) == date_val)
        # adj_close 填充
        day_df = day_df.with_columns(
            pl.when(pl.col("adj_close").is_null())
            .then(pl.col("close"))
            .otherwise(pl.col("adj_close"))
            .alias("adj_close")
        )
        df = day_df.select(["ticker", "adj_close"]).with_columns(
            pl.col("adj_close").cast(pl.Float64, strict=False)
        )
        self._date_cache[cache_key] = df
        return self._filter_tickers(df, universe_tickers)

    def get_price_history(
        self, end_date: str, lookback_days: int,
        universe_tickers: Optional[list[str]] = None,
        columns: Optional[list[str]] = None,
    ) -> pl.DataFrame:
        """获取截止到指定日期的历史行情。"""
        cache_key = ("price_hist", end_date, lookback_days)
        cached = self._date_cache.get(cache_key)
        if cached is not None:
            df = self._filter_tickers(cached, universe_tickers)
            if columns:
                keep = ["ticker", "trade_date"] + [c for c in columns if c in df.columns]
                df = df.select(keep)
            return df

        end_val = pd.to_datetime(end_date).date()
        start_val = (pd.to_datetime(end_date) - pd.Timedelta(days=lookback_days)).date()

        bulk_daily = self._static_cache.get("_bulk_daily")
        if bulk_daily is None or (isinstance(bulk_daily, pl.DataFrame) and bulk_daily.is_empty()):
            logger.warning("get_price_history: 缓存为空")
            return pl.DataFrame()

        result = bulk_daily.filter(
            (pl.col("trade_date").cast(pl.Date) >= start_val)
            & (pl.col("trade_date").cast(pl.Date) <= end_val)
        )
        if universe_tickers:
            result = result.filter(pl.col("ticker").is_in(universe_tickers))
        self._date_cache[cache_key] = result
        if columns:
            keep = ["ticker", "trade_date"] + [c for c in columns if c in result.columns]
            result = result.select(keep)
        return result

    def get_month_end_price(
        self, date: str, months_ago: int,
        universe_tickers: Optional[list[str]] = None,
    ) -> pl.DataFrame:
        """获取 N 个月前月末的复权收盘价。"""
        precomputed = self._get_month_end_adj_close(
            date, months_ago, set(universe_tickers) if universe_tickers else None
        )
        if precomputed is not None:
            return precomputed

        cache_key = ("month_end", date, months_ago)
        cached = self._date_cache.get(cache_key)
        if cached is not None:
            return self._filter_tickers(cached, universe_tickers)

        target_date = pd.to_datetime(date) - pd.DateOffset(months=months_ago)
        month_start = target_date.replace(day=1).date()
        month_end_val = (target_date.replace(day=1) + pd.DateOffset(months=1) - pd.Timedelta(days=1)).date()

        bulk_daily = self._static_cache.get("_bulk_daily")
        if bulk_daily is None or (isinstance(bulk_daily, pl.DataFrame) and bulk_daily.is_empty()):
            logger.warning("get_month_end_price: 缓存为空")
            return self._pl_empty(["ticker", "adj_close"])

        df = bulk_daily.filter(
            (pl.col("trade_date").cast(pl.Date) >= month_start)
            & (pl.col("trade_date").cast(pl.Date) <= month_end_val)
        )
        if universe_tickers:
            df = df.filter(pl.col("ticker").is_in(universe_tickers))
        if df.is_empty():
            self._date_cache[cache_key] = df
            return df

        df = df.with_columns(pl.col("adj_close").cast(pl.Float64, strict=False))
        result = df.sort("trade_date", descending=True).unique(subset=["ticker"], keep="first")
        result = result.select(["ticker", "adj_close"])
        self._date_cache[cache_key] = result
        return self._filter_tickers(result, universe_tickers)

    def get_market_cap(
        self, date: str, universe_tickers: Optional[list[str]] = None,
    ) -> pl.DataFrame:
        """获取历史市值（消除前瞻偏差）。"""
        cache_key = ("mktcap_hist", date)
        cached = self._date_cache.get(cache_key)
        if cached is not None:
            return self._filter_tickers(cached, universe_tickers)

        date_val = pd.to_datetime(date).date()
        bulk_mktcap = self._static_cache.get("_bulk_mktcap")
        if bulk_mktcap is not None and not (isinstance(bulk_mktcap, pl.DataFrame) and bulk_mktcap.is_empty()):
            valid = bulk_mktcap.filter(pl.col("date").cast(pl.Date) <= date_val)
            if not valid.is_empty():
                df = valid.sort("date").unique(subset=["ticker"], keep="last").select(["ticker", "market_cap"])
                self._date_cache[cache_key] = df
                return self._filter_tickers(df, universe_tickers)

        logger.warning(f"get_market_cap: 缓存为空")
        return self._pl_empty(["ticker", "market_cap"])

    def get_dividends(
        self, date: str, lookback_days: int = 365,
        universe_tickers: Optional[list[str]] = None,
    ) -> pl.DataFrame:
        """获取过去 N 天的股息数据。"""
        cache_key = ("dividends", date, lookback_days)
        cached = self._date_cache.get(cache_key)
        if cached is not None:
            return self._filter_tickers(cached, universe_tickers)

        date_val = pd.to_datetime(date).date()
        start_val = (pd.to_datetime(date) - pd.Timedelta(days=lookback_days)).date()

        bulk_div = self._static_cache.get("_bulk_dividends")
        if bulk_div is None or (isinstance(bulk_div, pl.DataFrame) and bulk_div.is_empty()):
            logger.warning("get_dividends: 缓存为空")
            return self._pl_empty(["ticker", "total_dividend"])

        df = bulk_div.filter(
            (pl.col("date").cast(pl.Date) >= start_val)
            & (pl.col("date").cast(pl.Date) <= date_val)
        )
        if universe_tickers:
            df = df.filter(pl.col("ticker").is_in(universe_tickers))
        result = df.group_by("ticker").agg(
            pl.col("dividend").sum().alias("total_dividend")
        )
        self._date_cache[cache_key] = result
        return self._filter_tickers(result, universe_tickers)
