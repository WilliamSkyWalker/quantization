"""
A 股因子基类 — Django ORM 版（迁自 services/factors/base.py）

关键差异：
    1. 数据源切到 Django ORM（AStockBasic / ADailyPrice / AFinancialIncome /
       AFinancialIndicator / AIndustryClass 等，managed=False）
    2. 新财报 schema 分成 4 张表，本基类通过 preload 时 join 提供"兼容视图"：
           financial_data 视图列 = 原 services.factors 依赖的宽列
           字段别名：
               - n_income_attr_p   → net_profit
               - roe_yearly        → roe_ttm
               - grossprofit_margin→ gross_margin
               - bps               → bps        (同名)
               - revenue           → revenue    (同名)
    3. `_fast_mysql_read` 移除（PG 下无此捷径），ORM 读取已足够快

子类用法不变：继承 FactorBase 并实现 compute(date, universe)。
"""

import logging
from abc import ABC, abstractmethod
from typing import Optional

import numpy as np
import pandas as pd

from services.config import LOG_LEVEL
from stocks.models import (
    ADailyPrice,
    AFinancialIncome,
    AFinancialIndicator,
    AIndustryClass,
    AStockBasic,
)

logger = logging.getLogger(__name__)
logger.setLevel(LOG_LEVEL)


class FactorBase(ABC):
    """A 股因子基类（Django ORM 版）。"""

    name: str = "base"
    description: str = ""

    _static_cache: dict = {}
    _date_cache: dict = {}
    _IN_CLAUSE_THRESHOLD = 2000

    # ----------------------------------------------------------
    # 缓存控制
    # ----------------------------------------------------------

    @classmethod
    def clear_date_cache(cls):
        cls._date_cache.clear()

    @classmethod
    def clear_all_cache(cls):
        cls._static_cache.clear()
        cls._date_cache.clear()

    # ----------------------------------------------------------
    # 预加载（回测热路径）
    # ----------------------------------------------------------

    @classmethod
    def preload_for_backtest(cls, db, start_date: str, end_date: str):
        """
        批量预加载回测区间内的 financial / daily / sentiment 数据到内存。

        db 参数保留仅为向下兼容（调用方仍传入，内部不使用）。
        """
        import time

        # 1. financial_data 兼容视图：AFinancialIncome + AFinancialIndicator
        t0 = time.time()
        income_qs = AFinancialIncome.objects.values(
            "ts_code", "ann_date", "end_date",
            "revenue", "n_income_attr_p", "ebit", "ebitda", "total_profit",
        )
        df_income = pd.DataFrame(list(income_qs))

        indicator_qs = AFinancialIndicator.objects.values(
            "ts_code", "end_date",
            "roe_yearly", "roe_dt", "grossprofit_margin", "bps",
            "op_yoy", "netprofit_yoy", "roic", "debt_to_assets",
        )
        df_ind = pd.DataFrame(list(indicator_qs))

        if not df_income.empty and not df_ind.empty:
            df_fin = df_income.merge(df_ind, on=["ts_code", "end_date"], how="outer")
        elif not df_income.empty:
            df_fin = df_income
            for col in ("roe_yearly", "roe_dt", "grossprofit_margin", "bps",
                        "op_yoy", "netprofit_yoy", "roic", "debt_to_assets"):
                df_fin[col] = None
        elif not df_ind.empty:
            df_fin = df_ind
            for col in ("ann_date", "revenue", "n_income_attr_p", "ebit", "ebitda", "total_profit"):
                df_fin[col] = None
        else:
            df_fin = pd.DataFrame()

        if not df_fin.empty:
            # 旧字段别名（保持 services.factors 子类兼容）
            df_fin = df_fin.rename(columns={
                "n_income_attr_p": "net_profit",
                "roe_yearly": "roe_ttm",
                "grossprofit_margin": "gross_margin",
            })
            for col in ("ann_date", "end_date"):
                if col in df_fin.columns:
                    df_fin[col] = pd.to_datetime(df_fin[col], errors="coerce")
            df_fin = df_fin.dropna(subset=["ann_date"])

        cls._static_cache["_bulk_financial"] = df_fin
        logger.info(f"a_factors preload financial: {len(df_fin)} 行, {time.time()-t0:.1f}s")

        # 2. daily_price 预加载（回测区间 + 400 天前移，供动量/技术因子）
        t0 = time.time()
        price_start = (pd.to_datetime(start_date) - pd.Timedelta(days=400)).date()
        price_end = pd.to_datetime(end_date).date()

        price_qs = ADailyPrice.objects.filter(
            trade_date__gte=price_start, trade_date__lte=price_end,
        ).values(
            "ts_code", "trade_date", "pct_chg", "turnover_rate",
            "vol", "amount", "close", "adj_factor", "dv_ttm",
        )
        df_price = pd.DataFrame(list(price_qs))
        if not df_price.empty:
            df_price["trade_date"] = pd.to_datetime(df_price["trade_date"])
            # vol → volume 兼容（ADailyPrice 字段是 vol，旧代码用 volume）
            df_price = df_price.rename(columns={"vol": "volume"})
        cls._static_cache["_bulk_daily"] = df_price
        logger.info(f"a_factors preload daily: {len(df_price)} 行, {time.time()-t0:.1f}s")

        # 3. policy_analysis / policy_article — sentiment 因子未启用，保留空 DataFrame
        cls._static_cache["_bulk_policy_analysis"] = pd.DataFrame()

    @classmethod
    def precompute_rolling_stats(cls):
        """预计算 rolling 统计量供动量/技术因子使用。"""
        import time
        t0 = time.time()

        bulk_daily = cls._static_cache.get("_bulk_daily")
        if bulk_daily is None or bulk_daily.empty:
            logger.debug("precompute_rolling_stats: 无预加载日线，跳过")
            return

        df = bulk_daily[["ts_code", "trade_date", "close", "adj_factor",
                         "pct_chg", "turnover_rate", "volume", "amount"]].copy()
        df = df.sort_values(["ts_code", "trade_date"])

        for col in ("close", "adj_factor", "pct_chg", "turnover_rate", "volume", "amount"):
            df[col] = pd.to_numeric(df[col], errors="coerce")
        df["adj_factor"] = df["adj_factor"].fillna(1.0)
        df["adj_close"] = df["close"] * df["adj_factor"]
        df["ret"] = df["pct_chg"] / 100.0
        df["log_ret"] = np.log1p(df["ret"].clip(-0.99, None))

        g = df.groupby("ts_code", sort=False)
        df["cum_ret_5d"] = np.expm1(g["log_ret"].transform(lambda x: x.rolling(5, min_periods=3).sum()))
        df["cum_ret_20d"] = np.expm1(g["log_ret"].transform(lambda x: x.rolling(20, min_periods=10).sum()))
        df["turn_20d"] = g["turnover_rate"].transform(lambda x: x.rolling(20, min_periods=10).mean())
        df["vol_20d"] = g["ret"].transform(lambda x: x.rolling(20, min_periods=10).std())
        df["ma60_adj"] = g["adj_close"].transform(lambda x: x.rolling(60, min_periods=30).mean())

        keep_cols = ["adj_close", "cum_ret_5d", "cum_ret_20d",
                     "turn_20d", "vol_20d", "ma60_adj", "volume"]
        df_indexed = df[["ts_code", "trade_date"] + keep_cols].copy()
        df_indexed = df_indexed.set_index(["trade_date", "ts_code"]).sort_index()
        cls._static_cache["_rolling_indexed"] = df_indexed

        df_me = df[["ts_code", "trade_date", "adj_close"]].copy()
        df_me["year_month"] = df_me["trade_date"].dt.to_period("M")
        idx = df_me.groupby(["ts_code", "year_month"])["trade_date"].idxmax()
        month_ends = df_me.loc[idx, ["ts_code", "year_month", "adj_close"]].reset_index(drop=True)
        cls._static_cache["_month_end_prices"] = month_ends

        logger.info(f"a_factors precompute rolling: {len(df)} 行, {time.time()-t0:.1f}s")

    def _get_rolling_for_date(
        self, date: str, codes: Optional[set[str]] = None,
    ) -> Optional[pd.DataFrame]:
        ri = self._static_cache.get("_rolling_indexed")
        if ri is None:
            logger.debug("_get_rolling_for_date: 无预计算数据")
            return None
        try:
            day = ri.xs(pd.to_datetime(date), level="trade_date")
            if codes:
                day = day[day.index.isin(codes)]
            return day
        except KeyError:
            logger.debug(f"_get_rolling_for_date: 日期 {date} 不在预计算中")
            return None

    def _get_month_end_adj_close(
        self, date: str, months_ago: int, codes: Optional[set[str]] = None,
    ) -> Optional[pd.DataFrame]:
        me = self._static_cache.get("_month_end_prices")
        if me is None:
            logger.debug("_get_month_end_adj_close: 无预计算月末价格")
            return None
        target = pd.to_datetime(date) - pd.DateOffset(months=months_ago)
        target_period = target.to_period("M")
        result = me[me["year_month"] == target_period]
        if codes:
            result = result[result["ts_code"].isin(codes)]
        if result.empty:
            logger.debug(f"_get_month_end_adj_close: {target_period} 无匹配")
            return None
        return result[["ts_code", "adj_close"]].copy()

    # ----------------------------------------------------------
    # 构造 & 抽象接口
    # ----------------------------------------------------------

    def __init__(self, db=None):
        """db 参数保留兼容性，内部用 ORM 不依赖。"""
        self.db = db

    def get_industry_map_cached(self) -> pd.DataFrame:
        """
        获取行业映射（申万 2021 L1）。

        Returns:
            DataFrame[ts_code, industry_name]
        """
        cached = self._static_cache.get("industry_map")
        if cached is not None:
            return cached.copy()

        qs = AIndustryClass.objects.filter(
            src="SW2021", level="L1", out_date__isnull=True,
        ).values("ts_code", "index_name")
        df = pd.DataFrame(list(qs))
        if not df.empty:
            df = df.rename(columns={"index_name": "industry_name"})
        else:
            df = pd.DataFrame(columns=["ts_code", "industry_name"])
        self._static_cache["industry_map"] = df
        return df.copy()

    @abstractmethod
    def compute(self, date: str, universe: pd.DataFrame) -> pd.DataFrame:
        raise NotImplementedError

    # ----------------------------------------------------------
    # 通用查询工具（从预加载缓存读，无则走 ORM）
    # ----------------------------------------------------------

    def get_latest_financial(
        self,
        date: str,
        columns: list[str],
        universe_codes: Optional[list[str]] = None,
    ) -> pd.DataFrame:
        """
        取截止 date 的最新财报（按 ann_date <= date）。

        columns 可以是旧名（net_profit / revenue / roe_ttm / gross_margin / bps ...）
        或直接 Tushare 原名。
        """
        cache_key = ("financial", date)
        cached = self._date_cache.get(cache_key)
        if cached is not None:
            df = cached
            if universe_codes:
                df = df[df["ts_code"].isin(universe_codes)]
            keep = ["ts_code", "ann_date", "end_date"] + [c for c in columns if c in df.columns]
            return df[keep].copy()

        bulk_fin = self._static_cache.get("_bulk_financial")
        if bulk_fin is not None and not bulk_fin.empty:
            date_ts = pd.to_datetime(date)
            df = bulk_fin[bulk_fin["ann_date"] <= date_ts].copy()
            df = df.sort_values("end_date", ascending=False).drop_duplicates(subset=["ts_code"], keep="first")
            self._date_cache[cache_key] = df
            if universe_codes:
                df = df[df["ts_code"].isin(universe_codes)]
            keep = ["ts_code", "ann_date", "end_date"] + [c for c in columns if c in df.columns]
            return df[keep].copy()

        return self._query_financial_from_orm(date, columns, universe_codes)

    def _query_financial_from_orm(
        self, date: str, columns: list[str], universe_codes: Optional[list[str]],
    ) -> pd.DataFrame:
        """ORM 查询财报（用于缓存未命中时）。"""
        date_d = pd.to_datetime(date).date()
        income_q = AFinancialIncome.objects.filter(ann_date__lte=date_d)
        ind_q = AFinancialIndicator.objects.filter(end_date__lte=date_d)
        if universe_codes:
            income_q = income_q.filter(ts_code__in=universe_codes)
            ind_q = ind_q.filter(ts_code__in=universe_codes)

        df_inc = pd.DataFrame(list(income_q.values(
            "ts_code", "ann_date", "end_date",
            "revenue", "n_income_attr_p", "ebit", "ebitda", "total_profit",
        )))
        df_ind = pd.DataFrame(list(ind_q.values(
            "ts_code", "end_date",
            "roe_yearly", "roe_dt", "grossprofit_margin", "bps",
            "op_yoy", "netprofit_yoy", "roic", "debt_to_assets",
        )))

        if df_inc.empty and df_ind.empty:
            return pd.DataFrame(columns=["ts_code", "ann_date", "end_date"])

        if not df_inc.empty and not df_ind.empty:
            df = df_inc.merge(df_ind, on=["ts_code", "end_date"], how="outer")
        elif not df_inc.empty:
            df = df_inc
        else:
            df = df_ind
            df["ann_date"] = df["end_date"]  # fallback

        df = df.rename(columns={
            "n_income_attr_p": "net_profit",
            "roe_yearly": "roe_ttm",
            "grossprofit_margin": "gross_margin",
        })
        df["ann_date"] = pd.to_datetime(df["ann_date"])
        df["end_date"] = pd.to_datetime(df["end_date"])
        df = df.dropna(subset=["ann_date"]).sort_values(
            "end_date", ascending=False
        ).drop_duplicates(subset=["ts_code"], keep="first")

        keep = ["ts_code", "ann_date", "end_date"] + [c for c in columns if c in df.columns]
        return df[keep].copy()

    def get_price_history(
        self,
        end_date: str,
        lookback_days: int,
        universe_codes: Optional[list[str]] = None,
        columns: Optional[list[str]] = None,
    ) -> pd.DataFrame:
        """取截止 end_date 的历史行情（含 lookback_days 自然日回看）。"""
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

        q = ADailyPrice.objects.filter(
            trade_date__gte=start_dt.date(),
            trade_date__lte=pd.to_datetime(end_date).date(),
        )
        if universe_codes:
            q = q.filter(ts_code__in=universe_codes)
        fields = ["ts_code", "trade_date", "pct_chg", "turnover_rate",
                  "vol", "amount", "close", "adj_factor"]
        df = pd.DataFrame(list(q.values(*fields)))
        if not df.empty:
            df = df.rename(columns={"vol": "volume"})
            df["trade_date"] = pd.to_datetime(df["trade_date"])
        self._date_cache[cache_key] = df
        if columns:
            keep = ["ts_code", "trade_date"] + [c for c in columns if c in df.columns]
            df = df[keep]
        return df.copy()

    def get_month_end_price(
        self, date: str, months_ago: int,
        universe_codes: Optional[list[str]] = None,
    ) -> pd.DataFrame:
        """N 月前月末的前复权收盘价。"""
        cache_key = ("month_end", date, months_ago)
        cached = self._date_cache.get(cache_key)
        if cached is not None:
            if universe_codes:
                return cached[cached["ts_code"].isin(universe_codes)].copy()
            return cached.copy()

        target_date = pd.to_datetime(date) - pd.DateOffset(months=months_ago)
        month_start_dt = target_date.replace(day=1)
        month_end_dt = month_start_dt + pd.DateOffset(months=1) - pd.Timedelta(days=1)

        bulk_daily = self._static_cache.get("_bulk_daily")
        if bulk_daily is not None and not bulk_daily.empty:
            mask = (bulk_daily["trade_date"] >= month_start_dt) & (bulk_daily["trade_date"] <= month_end_dt)
            df = bulk_daily[mask].copy()
            if universe_codes:
                df = df[df["ts_code"].isin(universe_codes)]
            if df.empty:
                self._date_cache[cache_key] = df
                return df
            df = df.sort_values("trade_date", ascending=False).drop_duplicates(subset=["ts_code"], keep="first")
            df["adj_factor"] = pd.to_numeric(df["adj_factor"], errors="coerce").fillna(1.0)
            df["close"] = pd.to_numeric(df["close"], errors="coerce") * df["adj_factor"]
            result = df[["ts_code", "close"]]
            self._date_cache[cache_key] = result
            return result.copy()

        q = ADailyPrice.objects.filter(
            trade_date__gte=month_start_dt.date(),
            trade_date__lte=month_end_dt.date(),
        )
        if universe_codes:
            q = q.filter(ts_code__in=universe_codes)
        df = pd.DataFrame(list(q.values("ts_code", "trade_date", "close", "adj_factor")))
        if df.empty:
            self._date_cache[cache_key] = df
            return df
        df["trade_date"] = pd.to_datetime(df["trade_date"])
        df = df.sort_values("trade_date", ascending=False).drop_duplicates(subset=["ts_code"], keep="first")
        df["adj_factor"] = pd.to_numeric(df["adj_factor"], errors="coerce").fillna(1.0)
        df["close"] = pd.to_numeric(df["close"], errors="coerce") * df["adj_factor"]
        result = df[["ts_code", "close"]]
        self._date_cache[cache_key] = result
        return result.copy()

    # ----------------------------------------------------------
    # TTM 计算
    # ----------------------------------------------------------

    def _compute_ttm_vectorized(
        self,
        date: str,
        value_col: str,
        result_col: str,
        universe_codes: Optional[list[str]] = None,
    ) -> pd.DataFrame:
        """向量化 TTM 计算。value_col 可以是旧名或 Tushare 原名。"""
        _new_name = {
            "net_profit": "n_income_attr_p",
            "revenue": "revenue",
        }.get(value_col, value_col)

        df = None
        bulk_fin = self._static_cache.get("_bulk_financial")
        if bulk_fin is not None and not bulk_fin.empty:
            # bulk_fin 已 rename 为旧名
            read_col = value_col if value_col in bulk_fin.columns else _new_name
            if read_col in bulk_fin.columns:
                date_ts = pd.to_datetime(date)
                df = bulk_fin[["ts_code", "ann_date", "end_date", read_col]].copy()
                df = df.rename(columns={read_col: value_col})
                df = df[(df["ann_date"] <= date_ts) & df[value_col].notna()]
                if universe_codes:
                    df = df[df["ts_code"].isin(universe_codes)]
                df = df.sort_values(["ts_code", "end_date"], ascending=[True, False])

        if df is None or df.empty:
            q = AFinancialIncome.objects.filter(
                ann_date__lte=pd.to_datetime(date).date(),
                **{f"{_new_name}__isnull": False},
            )
            if universe_codes:
                q = q.filter(ts_code__in=universe_codes)
            df = pd.DataFrame(list(q.values("ts_code", "ann_date", "end_date", _new_name)))
            if df.empty:
                logger.debug(f"_compute_ttm_vectorized: {value_col} 财务数据空")
                return pd.DataFrame(columns=["ts_code", result_col])
            df = df.rename(columns={_new_name: value_col})
            df["ann_date"] = pd.to_datetime(df["ann_date"])
            df["end_date"] = pd.to_datetime(df["end_date"])
            df = df.sort_values(["ts_code", "end_date"], ascending=[True, False])

        df[value_col] = pd.to_numeric(df[value_col], errors="coerce")

        latest = df.groupby("ts_code").first().reset_index()[["ts_code", "end_date", value_col]]
        latest.columns = ["ts_code", "latest_end", "current_val"]
        latest["month"] = latest["latest_end"].dt.month

        mask_annual = latest["month"] == 12
        annual = latest.loc[mask_annual, ["ts_code", "current_val"]].copy()
        annual.rename(columns={"current_val": result_col}, inplace=True)

        non_annual = latest[~mask_annual].copy()
        if non_annual.empty:
            return annual[["ts_code", result_col]].reset_index(drop=True)

        lookup = df.drop_duplicates(subset=["ts_code", "end_date"])[
            ["ts_code", "end_date", value_col]
        ]

        non_annual["prev_annual_end"] = pd.to_datetime(dict(
            year=non_annual["latest_end"].dt.year - 1, month=12, day=31,
        ))
        non_annual["prev_same_end"] = pd.to_datetime(dict(
            year=non_annual["latest_end"].dt.year - 1,
            month=non_annual["latest_end"].dt.month,
            day=non_annual["latest_end"].dt.day,
        ))

        non_annual = non_annual.merge(
            lookup.rename(columns={"end_date": "prev_annual_end", value_col: "annual_val"}),
            on=["ts_code", "prev_annual_end"], how="left",
        )
        non_annual = non_annual.merge(
            lookup.rename(columns={"end_date": "prev_same_end", value_col: "same_val"}),
            on=["ts_code", "prev_same_end"], how="left",
        )

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
        self, date: str, universe_codes: Optional[list[str]] = None,
    ) -> pd.DataFrame:
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
        self, date: str, universe_codes: Optional[list[str]] = None,
    ) -> pd.DataFrame:
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
        self, date: str, universe_codes: Optional[list[str]] = None,
    ) -> pd.DataFrame:
        """指定日期（或之前最近交易日）的收盘价。"""
        cache_key = ("close_on_date", date)
        cached = self._date_cache.get(cache_key)
        if cached is not None:
            if universe_codes:
                return cached[cached["ts_code"].isin(universe_codes)].copy()
            return cached.copy()

        date_ts = pd.to_datetime(date)
        lookback_ts = date_ts - pd.Timedelta(days=10)

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

        q = ADailyPrice.objects.filter(
            trade_date__gte=lookback_ts.date(),
            trade_date__lte=date_ts.date(),
        )
        if universe_codes:
            q = q.filter(ts_code__in=universe_codes)
        df = pd.DataFrame(list(q.values("ts_code", "trade_date", "close")))
        if df.empty:
            result = pd.DataFrame(columns=["ts_code", "close"])
            self._date_cache[cache_key] = result
            return result
        df["trade_date"] = pd.to_datetime(df["trade_date"])
        df = df.sort_values("trade_date", ascending=False).drop_duplicates(subset=["ts_code"], keep="first")
        result = df[["ts_code", "close"]]
        self._date_cache[cache_key] = result
        return result.copy()

    def get_total_share(
        self, universe_codes: Optional[list[str]] = None,
    ) -> pd.DataFrame:
        """从 stock_basic 取总股本（万股）。"""
        cache_key = "total_share"
        cached = self._static_cache.get(cache_key)
        if cached is not None:
            if universe_codes:
                return cached[cached["ts_code"].isin(universe_codes)].copy()
            return cached.copy()

        q = AStockBasic.objects.filter(total_share__isnull=False).values("ts_code", "total_share")
        df = pd.DataFrame(list(q))
        if df.empty:
            df = pd.DataFrame(columns=["ts_code", "total_share"])
        self._static_cache[cache_key] = df
        if universe_codes:
            return df[df["ts_code"].isin(universe_codes)].copy()
        return df.copy()

    def get_float_share(
        self, universe_codes: Optional[list[str]] = None,
    ) -> pd.DataFrame:
        """从 stock_basic 取流通股本（万股）。"""
        cache_key = "float_share"
        cached = self._static_cache.get(cache_key)
        if cached is not None:
            if universe_codes:
                return cached[cached["ts_code"].isin(universe_codes)].copy()
            return cached.copy()

        q = AStockBasic.objects.filter(float_share__isnull=False).values("ts_code", "float_share")
        df = pd.DataFrame(list(q))
        if df.empty:
            df = pd.DataFrame(columns=["ts_code", "float_share"])
        self._static_cache[cache_key] = df
        if universe_codes:
            return df[df["ts_code"].isin(universe_codes)].copy()
        return df.copy()

    def fetch_financial_column(
        self,
        date: str,
        column: str,
        universe_codes: Optional[list[str]] = None,
    ) -> pd.DataFrame:
        """
        取单列财务历史（ann_date <= date）。工厂方法：给老代码兼容。

        自动从 income 或 indicator 表选择对应列。column 支持旧名。
        返回 DataFrame[ts_code, ann_date, end_date, <column>]，按 (ts_code, end_date) 倒序。
        """
        new_name = {
            "net_profit": "n_income_attr_p",
            "gross_margin": "grossprofit_margin",
            "roe_ttm": "roe_yearly",
        }.get(column, column)

        income_fields = {f.column for f in AFinancialIncome._meta.get_fields() if hasattr(f, "column")}
        ind_fields = {f.column for f in AFinancialIndicator._meta.get_fields() if hasattr(f, "column")}

        if new_name in income_fields:
            q = AFinancialIncome.objects.filter(
                ann_date__lte=pd.to_datetime(date).date(),
                **{f"{new_name}__isnull": False},
            )
            if universe_codes:
                q = q.filter(ts_code__in=universe_codes)
            df = pd.DataFrame(list(q.values("ts_code", "ann_date", "end_date", new_name)))
        elif new_name in ind_fields:
            q = AFinancialIndicator.objects.filter(
                end_date__lte=pd.to_datetime(date).date(),
                **{f"{new_name}__isnull": False},
            )
            if universe_codes:
                q = q.filter(ts_code__in=universe_codes)
            df = pd.DataFrame(list(q.values("ts_code", "ann_date", "end_date", new_name)))
            if not df.empty and df["ann_date"].isna().all():
                df["ann_date"] = df["end_date"]
        else:
            logger.warning(f"fetch_financial_column: 未知列 {column}")
            return pd.DataFrame(columns=["ts_code", "ann_date", "end_date", column])

        if df.empty:
            return pd.DataFrame(columns=["ts_code", "ann_date", "end_date", column])

        df = df.rename(columns={new_name: column})
        df["ann_date"] = pd.to_datetime(df["ann_date"])
        df["end_date"] = pd.to_datetime(df["end_date"])
        return df.sort_values(["ts_code", "end_date"], ascending=[True, False])

    def __repr__(self) -> str:
        return f"<CNFactor: {self.name}>"
