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

from services.config import LOG_LEVEL
from stocks.models import (
    USFinancialData, USDailyPrice, USIndexDaily,
    USAnalystRecommendation, USEarningsSurprise, USEpsEstimate,
    USCorporateAction, USEnterpriseValue, USInsiderTrade,
    USStockBasic,
    USRevenueSegment, USDarkPoolVolume, USESGRating,
    USLobbying, USGovContract, USCongressTrade,
    USEmployeeCount, USSharesFloat, USInstitutionalHolder,
    USPriceTargetDetail,
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
                       extra_filters: dict | None = None) -> pd.DataFrame:
        """
        先找可用 parquet 缓存（范围包含即命中），没有则查 Django ORM 后存缓存。
        DB 冷查询时，若日期跨度 >= _SHARD_THRESHOLD_YEARS 年，按年分片并行查询。
        所有表都会缓存到 parquet，后续直接读文件。
        """
        import time
        from services.config import PROJECT_ROOT

        hit = cls._find_cache(table, start, end)
        if hit:
            t0 = time.time()
            df = pd.read_parquet(hit)
            if date_field in df.columns:
                df[date_field] = pd.to_datetime(df[date_field])
                df = df[(df[date_field] >= start) & (df[date_field] <= end)]
            logger.info(f"US 预加载 {table}: {len(df)} 行 (parquet 缓存 {hit.name}, {time.time()-t0:.1f}s)")
            return df

        t0 = time.time()
        start_ts = pd.to_datetime(start)
        end_ts = pd.to_datetime(end)
        span_years = (end_ts - start_ts).days / 365.25

        if span_years >= cls._SHARD_THRESHOLD_YEARS:
            df = cls._query_sharded(model, cols, start, end, date_field, order_by, extra_filters)
        else:
            df = cls._query_single(model, cols, start, end, date_field, order_by, extra_filters)

        logger.info(f"US 预加载 {table}: {len(df)} 行 (DB 查询, {time.time()-t0:.1f}s)")

        if not df.empty:
            cache_dir = PROJECT_ROOT / "cache"
            cache_dir.mkdir(exist_ok=True)
            path = cache_dir / f"{table}_{start}_{end}.parquet"
            df.to_parquet(path, index=False)
            logger.info(f"  → 已缓存到 {path.name}")
        return df

    @classmethod
    def _query_single(cls, model, cols, start, end, date_field, order_by,
                      extra_filters=None):
        """单次 ORM 查询。"""
        filters = {
            f"{date_field}__gte": start,
            f"{date_field}__lte": end,
        }
        if extra_filters:
            filters.update(extra_filters)
        qs = model.objects.filter(**filters)
        if order_by:
            qs = qs.order_by(*order_by)
        return pd.DataFrame(qs.values_list(*cols), columns=cols)

    @classmethod
    def _query_sharded(cls, model, cols, start, end, date_field, order_by,
                       extra_filters=None):
        """按年分片，ThreadPoolExecutor 并发查询，pd.concat 合并。"""
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
            return pd.DataFrame(qs.values_list(*cols), columns=cols)

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
                    if not part.empty:
                        parts.append(part)
                except Exception as exc:
                    logger.warning(f"分片查询 {shard_range} 失败: {exc}")

        connections.close_all()

        if not parts:
            return pd.DataFrame(columns=cols)

        df = pd.concat(parts, ignore_index=True)
        if order_by:
            sort_cols = [c.lstrip("-") for c in order_by]
            ascending = [not c.startswith("-") for c in order_by]
            valid_sort = [c for c in sort_cols if c in df.columns]
            if valid_sort:
                valid_asc = ascending[:len(valid_sort)]
                df = df.sort_values(valid_sort, ascending=valid_asc).reset_index(drop=True)
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
            if not df.empty:
                df["filing_date"] = pd.to_datetime(df["filing_date"])
                df["date"] = pd.to_datetime(df["date"])
                _FILING_LAG_BUFFER = pd.Timedelta(days=45)
                bad_mask = df["filing_date"] <= df["date"]
                n_fixed = bad_mask.sum()
                if n_fixed > 0:
                    df.loc[bad_mask, "filing_date"] = df.loc[bad_mask, "date"] + _FILING_LAG_BUFFER
                    logger.info(f"Filing date 修正: {n_fixed} 条")
                for col in ["revenue", "gross_profit", "operating_income", "net_income",
                            "total_stockholders_equity", "weighted_average_shs_out"]:
                    if col in df.columns:
                        df[col] = pd.to_numeric(df[col], errors="coerce")
                rev = df["revenue"].replace(0, np.nan)
                df["gross_margin"] = df["gross_profit"] / rev
                df["operating_margin"] = df["operating_income"] / rev
                equity = df["total_stockholders_equity"].replace(0, np.nan)
                df["roe"] = df["net_income"] / equity
                df["weighted_avg_shares"] = df["weighted_average_shs_out"]
            logger.info(f"  _bulk_financial: {len(df)} 行, {time.time()-t0:.1f}s")
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
            if not df.empty:
                df["trade_date"] = pd.to_datetime(df["trade_date"])
                df["adj_close"] = pd.to_numeric(df["adj_close"], errors="coerce")
                df["close"] = pd.to_numeric(df["close"], errors="coerce")
                df["adj_close"] = df["adj_close"].fillna(df["close"])
            logger.info(f"  _bulk_daily: {len(df)} 行, {time.time()-t0:.1f}s")
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
            if not df.empty:
                df["trade_date"] = pd.to_datetime(df["trade_date"])
                df["close"] = pd.to_numeric(df["close"], errors="coerce")
            logger.info(f"  _bulk_index: {len(df)} 行, {time.time()-t0:.1f}s")
            return "_bulk_index", df

        def _load_analyst():
            t0 = time.time()
            ar_cols = ["ticker", "date", "grading_company", "new_grade", "action"]
            df = cls._load_or_query(
                "us_analyst_recommendation", USAnalystRecommendation, ar_cols,
                analyst_start, end_date, "date",
                order_by=["ticker", "date"],
            )
            if not df.empty:
                df["date"] = pd.to_datetime(df["date"])
            logger.info(f"  _bulk_analyst: {len(df)} 行, {time.time()-t0:.1f}s")
            return "_bulk_analyst", df

        def _load_earnings_surprise():
            t0 = time.time()
            es_cols = ["ticker", "date", "eps_actual", "eps_estimated", "surprise", "surprise_pct"]
            df = cls._load_or_query(
                "us_earnings_surprise", USEarningsSurprise, es_cols,
                es_start, end_date, "date",
                order_by=["ticker", "date"],
            )
            if not df.empty:
                df["date"] = pd.to_datetime(df["date"])
            logger.info(f"  _bulk_earnings_surprise: {len(df)} 行, {time.time()-t0:.1f}s")
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
            if not df.empty:
                df["date"] = pd.to_datetime(df["date"])
            logger.info(f"  _bulk_eps_estimate: {len(df)} 行, {time.time()-t0:.1f}s")
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
            if not df.empty:
                df["date"] = pd.to_datetime(df["date"])
            logger.info(f"  _bulk_dividends: {len(df)} 行, {time.time()-t0:.1f}s")
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
            if not df.empty:
                df["date"] = pd.to_datetime(df["date"])
                df.rename(columns={"market_capitalization": "market_cap"}, inplace=True)
                df["market_cap"] = pd.to_numeric(df["market_cap"], errors="coerce")
            logger.info(f"  _bulk_mktcap: {len(df)} 行, {time.time()-t0:.1f}s")
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
            if not df.empty:
                df["filing_date"] = pd.to_datetime(df["filing_date"])
                is_acquire = df["acquisition_or_disposition"] == "A"
                df["net_value"] = np.where(
                    is_acquire,
                    df["securities_transacted"] * df["price"],
                    -df["securities_transacted"] * df["price"],
                )
                df = df[["ticker", "filing_date", "net_value"]]
            logger.info(f"  _bulk_insider: {len(df)} 行, {time.time()-t0:.1f}s")
            return "_bulk_insider", df

        # ---- 因子用到但之前没预加载的表（消除 ORM fallback） ----

        def _load_generic(table_name, model, cols, start, end, date_field,
                          cache_key=None, order_by=None, extra_filters=None):
            """通用加载：任意表 → _static_cache[cache_key]。"""
            t0 = time.time()
            df = cls._load_or_query(table_name, model, cols, start, end, date_field,
                                    order_by=order_by, extra_filters=extra_filters)
            if not df.empty and date_field in df.columns:
                df[date_field] = pd.to_datetime(df[date_field])
            key = cache_key or f"_bulk_{table_name}"
            logger.info(f"  {key}: {len(df)} 行, {time.time()-t0:.1f}s")
            return key, df

        def _load_revenue_segment():
            cols = ["ticker", "date", "segment", "revenue", "segment_type"]
            seg_start = (pd.to_datetime(start_date) - pd.Timedelta(days=3*365)).strftime("%Y-%m-%d")
            return _load_generic("us_revenue_segment", USRevenueSegment, cols,
                                 seg_start, end_date, "date",
                                 cache_key="_bulk_revenue_segment", order_by=["ticker", "date"])

        def _load_dark_pool():
            cols = ["ticker", "date", "short_volume", "total_volume"]
            return _load_generic("us_dark_pool_volume", USDarkPoolVolume, cols,
                                 analyst_start, end_date, "date",
                                 cache_key="_bulk_dark_pool", order_by=["ticker", "date"])

        def _load_esg():
            cols = ["ticker", "fiscal_year", "esg_risk_rating"]
            return _load_generic("us_esg_rating", USESGRating, cols,
                                 "2010-01-01", end_date, "fiscal_year",
                                 cache_key="_bulk_esg")

        def _load_lobbying():
            cols = ["ticker", "year", "amount"]
            return _load_generic("us_lobbying", USLobbying, cols,
                                 "2010-01-01", end_date, "year",
                                 cache_key="_bulk_lobbying")

        def _load_gov_contract():
            cols = ["ticker", "date", "amount"]
            return _load_generic("us_gov_contract", USGovContract, cols,
                                 analyst_start, end_date, "date",
                                 cache_key="_bulk_gov_contract")

        def _load_congress():
            cols = ["ticker", "transaction_date", "transaction_type"]
            cong_start = (pd.to_datetime(start_date) - pd.Timedelta(days=180)).strftime("%Y-%m-%d")
            return _load_generic("us_congress_trade", USCongressTrade, cols,
                                 cong_start, end_date, "transaction_date",
                                 cache_key="_bulk_congress")

        def _load_employee():
            cols = ["ticker", "date", "employee_count"]
            return _load_generic("us_employee_count", USEmployeeCount, cols,
                                 fin_start, end_date, "date",
                                 cache_key="_bulk_employee", order_by=["ticker", "date"])

        def _load_shares_float():
            cols = ["ticker", "free_float", "float_shares", "outstanding_shares"]
            t0 = time.time()
            rows = list(USSharesFloat.objects.values_list(*cols))
            df = pd.DataFrame(rows, columns=cols) if rows else pd.DataFrame(columns=cols)
            logger.info(f"  _bulk_shares_float: {len(df)} 行, {time.time()-t0:.1f}s")
            return "_bulk_shares_float", df

        def _load_institutional_holder():
            cols = ["ticker", "date", "investors_holding", "number_of_13f_shares"]
            ih_start = (pd.to_datetime(start_date) - pd.Timedelta(days=400)).strftime("%Y-%m-%d")
            return _load_generic("us_institutional_holder", USInstitutionalHolder, cols,
                                 ih_start, end_date, "date",
                                 cache_key="_bulk_institutional", order_by=["ticker", "date"])

        def _load_price_target_detail():
            cols = ["ticker", "published_date", "analyst_company", "price_target", "price_when_posted"]
            pt_start = "2020-01-01"
            return _load_generic("us_price_target_detail", USPriceTargetDetail, cols,
                                 pt_start, end_date, "published_date",
                                 cache_key="_bulk_pt_detail", order_by=["ticker", "published_date"])

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
            # 因子专用表（消除 ORM fallback）
            _load_revenue_segment,
            _load_dark_pool,
            _load_esg,
            _load_lobbying,
            _load_gov_contract,
            _load_congress,
            _load_employee,
            _load_shares_float,
            _load_institutional_holder,
            _load_price_target_detail,
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
        一次性预计算动量/技术因子所需的 rolling 统计量。
        必须在 preload_for_backtest() 之后调用。
        """
        import time
        t0 = time.time()

        bulk_daily = cls._static_cache.get("_bulk_daily")
        if bulk_daily is None or bulk_daily.empty:
            logger.debug("precompute_rolling_stats: 预加载日线数据为空，跳过rolling预计算")
            return

        df = bulk_daily[["ticker", "trade_date", "adj_close", "close",
                         "change_percent", "volume"]].copy()
        df = df.sort_values(["ticker", "trade_date"])

        for col in ["adj_close", "close", "change_percent", "volume"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")

        # FMP /stable/ 端点不返回 adjClose，用 close 填充（FMP close 已是 split-adjusted）
        df["adj_close"] = df["adj_close"].fillna(df["close"])

        g = df.groupby("ticker", sort=False)

        # 从 adj_close 计算收益率（change_percent 可能为 NULL，yfinance 不一定提供）
        df["ret"] = g["adj_close"].transform(lambda x: x.pct_change())
        # 如果 change_percent 有值则优先使用（更精确）
        has_pct = df["change_percent"].notna()
        if has_pct.any():
            df.loc[has_pct, "ret"] = df.loc[has_pct, "change_percent"] / 100.0
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

        # 存 parquet 缓存（供 spawn worker 读取，跳过重算）
        from services.config import PROJECT_ROOT
        cache_dir = PROJECT_ROOT / "cache"
        cache_dir.mkdir(exist_ok=True)
        df_indexed.reset_index().to_parquet(cache_dir / "_rolling_indexed.parquet", index=False)
        month_ends.to_parquet(cache_dir / "_month_end_prices.parquet", index=False)

        logger.info(
            f"US 预计算 rolling stats + 月末价格: {len(df)} 行, {time.time()-t0:.1f}s"
        )

    @classmethod
    def load_precomputed_cache(cls):
        """供 spawn worker 调用：从 parquet 读取预算好的缓存，跳过 precompute_rolling_stats。"""
        import time
        from services.config import PROJECT_ROOT
        t0 = time.time()
        cache_dir = PROJECT_ROOT / "cache"

        # 1. 加载所有基础表（已有 parquet 缓存，秒级）
        # preload_for_backtest 会自动命中 parquet
        # 这里只需读 rolling stats 和 month_end
        ri_path = cache_dir / "_rolling_indexed.parquet"
        me_path = cache_dir / "_month_end_prices.parquet"

        if ri_path.exists() and me_path.exists():
            df_ri = pd.read_parquet(ri_path)
            df_ri["trade_date"] = pd.to_datetime(df_ri["trade_date"])
            df_ri = df_ri.set_index(["trade_date", "ticker"]).sort_index()
            cls._static_cache["_rolling_indexed"] = df_ri

            df_me = pd.read_parquet(me_path)
            # parquet 存的是 ticker/year_month/adj_close，year_month 是字符串
            df_me["year_month"] = df_me["year_month"].apply(lambda x: pd.Period(str(x), freq="M"))
            cls._static_cache["_month_end_prices"] = df_me

            logger.info(f"Worker 加载预算缓存: rolling={len(df_ri)}, month_end={len(df_me)}, {time.time()-t0:.1f}s")
            return True
        else:
            logger.warning("预算缓存不存在，需要跑 precompute_rolling_stats()")
            return False

    # ----------------------------------------------------------
    # Rolling stats helpers
    # ----------------------------------------------------------
    def _get_rolling_for_date(
        self, date: str, tickers: Optional[set[str]] = None,
    ) -> Optional[pd.DataFrame]:
        """从预计算的 rolling stats 中提取指定日期的截面数据。"""
        ri = self._static_cache.get("_rolling_indexed")
        if ri is None:
            logger.debug("_get_rolling_for_date: rolling预计算数据不存在")
            return None
        date_ts = pd.to_datetime(date)
        try:
            day = ri.xs(date_ts, level="trade_date")
            if tickers:
                day = day[day.index.isin(tickers)]
            return day
        except KeyError:
            logger.debug(f"_get_rolling_for_date: 日期 {date} 不在rolling数据中")
            return None

    def _get_month_end_adj_close(
        self, date: str, months_ago: int, tickers: Optional[set[str]] = None,
    ) -> Optional[pd.DataFrame]:
        """从预计算的月末价格中提取 N 月前月末的复权收盘价。"""
        me = self._static_cache.get("_month_end_prices")
        if me is None:
            logger.debug("_get_month_end_adj_close: 月末价格预计算数据不存在")
            return None
        target = pd.to_datetime(date) - pd.DateOffset(months=months_ago)
        target_period = target.to_period("M")
        result = me[me["year_month"] == target_period]
        if tickers:
            result = result[result["ticker"].isin(tickers)]
        if result.empty:
            logger.debug(f"_get_month_end_adj_close: {months_ago}月前月末无价格数据")
            return None
        return result[["ticker", "adj_close"]].copy()

    # ----------------------------------------------------------
    # 初始化
    # ----------------------------------------------------------
    def __init__(self, db=None, **kwargs):
        pass

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
        from stocks.models import USIndustryClass
        result = pd.DataFrame(
            USIndustryClass.objects.values("ticker", "sector", "industry")
        )
        self._static_cache["industry_map"] = result
        return result.copy()

    # ----------------------------------------------------------
    # 通用数据获取工具（全部从缓存读取，preload_for_backtest 必须先调用）
    # ----------------------------------------------------------

    def get_latest_financial(
        self,
        date: str,
        columns: list[str],
        universe_tickers: Optional[list[str]] = None,
    ) -> pd.DataFrame:
        """
        获取截止到指定日期的最新财务数据（按 filing_date，防止未来函数）。

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

        bulk_fin = self._static_cache.get("_bulk_financial")
        if bulk_fin is None or bulk_fin.empty:
            logger.warning("get_latest_financial: 缓存为空，请先调用 preload_for_backtest()")
            return pd.DataFrame()

        date_ts = pd.to_datetime(date)
        df = bulk_fin[bulk_fin["filing_date"] <= date_ts].copy()
        df = df.sort_values("date", ascending=False).drop_duplicates(subset=["ticker"], keep="first")
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

        logger.warning("get_ttm_value: 缓存为空，请先调用 preload_for_backtest()")
        return pd.DataFrame(columns=["ticker", "ttm_value"])

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
            day_df = bulk_daily[bulk_daily["trade_date"] == date_ts].copy()
            # adj_close 可能全为 NaN（FMP bulk 数据），回退到 close
            if day_df["adj_close"].notna().sum() == 0 and "close" in day_df.columns:
                logger.debug("get_close_on_date: adj_close 全为空，回退到 close")
                day_df["adj_close"] = day_df["close"]
            df = day_df[["ticker", "adj_close"]].copy()
            df["adj_close"] = pd.to_numeric(df["adj_close"], errors="coerce")
            self._date_cache[cache_key] = df
            if universe_tickers:
                df = df[df["ticker"].isin(universe_tickers)]
            return df.copy()

        logger.warning("get_close_on_date: 缓存为空，请先调用 preload_for_backtest()")
        return pd.DataFrame(columns=["ticker", "adj_close"])

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

        logger.warning("get_price_history: 缓存为空，请先调用 preload_for_backtest()")
        return pd.DataFrame()

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
                logger.debug(f"get_month_end_price: 预加载数据中 {months_ago}月前月末无数据")
                self._date_cache[cache_key] = df
                return df
            df["adj_close"] = pd.to_numeric(df["adj_close"], errors="coerce")
            df = df.sort_values("trade_date", ascending=False).drop_duplicates(subset=["ticker"], keep="first")
            result = df[["ticker", "adj_close"]].copy()
            self._date_cache[cache_key] = result
            return result.copy()

        logger.warning("get_month_end_price: 缓存为空，请先调用 preload_for_backtest()")
        return pd.DataFrame(columns=["ticker", "adj_close"])

    def get_market_cap(
        self,
        date: str,
        universe_tickers: Optional[list[str]] = None,
    ) -> pd.DataFrame:
        """
        获取历史市值（消除前瞻偏差）。

        方法：us_enterprise_value 季度 market_cap，取 <= date 最近一条。
        回退链：预加载 _bulk_mktcap → SQL 查 us_enterprise_value → us_stock_basic 静态快照。

        Returns:
            DataFrame[ticker, market_cap]
        """
        cache_key = ("mktcap_hist", date)
        cached = self._date_cache.get(cache_key)
        if cached is not None:
            df = cached
            if universe_tickers:
                df = df[df["ticker"].isin(universe_tickers)]
            return df.copy()

        date_ts = pd.to_datetime(date)

        # 1. Try historical market cap from preloaded us_enterprise_value (季度, take last <= date)
        bulk_mktcap = self._static_cache.get("_bulk_mktcap")
        if bulk_mktcap is not None and not bulk_mktcap.empty:
            valid = bulk_mktcap[bulk_mktcap["date"] <= date_ts]
            if not valid.empty:
                df = (
                    valid.sort_values("date")
                    .drop_duplicates(subset=["ticker"], keep="last")
                    [["ticker", "market_cap"]]
                )
                self._date_cache[cache_key] = df
                if universe_tickers:
                    df = df[df["ticker"].isin(universe_tickers)]
                return df.copy()
            else:
                logger.debug(f"get_market_cap: no historical mktcap data before {date}")

        # 2. 缓存无数据
        logger.warning(f"get_market_cap: 缓存为空，请先调用 preload_for_backtest()")
        return pd.DataFrame(columns=["ticker", "market_cap"])

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
            result = df.groupby("ticker")["dividend"].sum().reset_index()
            result.columns = ["ticker", "total_dividend"]
            self._date_cache[cache_key] = result
            return result.copy()

        logger.warning("get_dividends: 缓存为空，请先调用 preload_for_backtest()")
        return pd.DataFrame(columns=["ticker", "total_dividend"])
