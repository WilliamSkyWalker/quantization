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

from services.config import LOG_LEVEL
from data.models import (
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

    @classmethod
    def _load_or_query(cls, table: str, model, cols: list[str],
                       start: str, end: str, date_field: str,
                       order_by: list[str] | None = None) -> pd.DataFrame:
        """
        先找可用 parquet 缓存（范围包含即命中），没有则查 Django ORM 后存缓存。
        """
        import time
        from services.config import PROJECT_ROOT

        hit = cls._find_cache(table, start, end)
        if hit:
            t0 = time.time()
            df = pd.read_parquet(hit)
            # 内存过滤到请求范围
            if date_field in df.columns:
                df[date_field] = pd.to_datetime(df[date_field])
                df = df[(df[date_field] >= start) & (df[date_field] <= end)]
            logger.info(f"US 预加载 {table}: {len(df)} 行 (parquet 缓存 {hit.name}, {time.time()-t0:.1f}s)")
            return df

        t0 = time.time()
        qs = model.objects.filter(**{
            f"{date_field}__gte": start,
            f"{date_field}__lte": end,
        })
        if order_by:
            qs = qs.order_by(*order_by)
        df = pd.DataFrame(qs.values_list(*cols), columns=cols)
        logger.info(f"US 预加载 {table}: {len(df)} 行 (DB 查询, {time.time()-t0:.1f}s)")

        if not df.empty:
            cache_dir = PROJECT_ROOT / "cache"
            cache_dir.mkdir(exist_ok=True)
            path = cache_dir / f"{table}_{start}_{end}.parquet"
            df.to_parquet(path, index=False)
            logger.info(f"  → 已缓存到 {path.name}")
        return df

    @classmethod
    def preload_for_backtest(cls, start_date: str, end_date: str, **kwargs):
        """
        一次性预加载回测区间所需数据到内存（Django ORM + parquet 缓存）。

        首次从 DB 加载后缓存到本地 parquet，后续直接读文件。
        """
        import time
        from pathlib import Path

        # 1. us_financial_data
        t0 = time.time()
        fin_cols = [
            "ticker", "period", "date", "filing_date",
            "revenue", "net_income", "eps", "gross_profit",
            "operating_income", "total_stockholders_equity",
            "total_equity", "total_assets",
            "total_debt", "free_cash_flow",
            "weighted_average_shs_out",
        ]
        fin_start = (pd.to_datetime(start_date) - pd.Timedelta(days=5*365)).strftime("%Y-%m-%d")
        df_fin = cls._load_or_query(
            "us_financial_data", USFinancialData, fin_cols,
            fin_start, end_date, "filing_date",
        )
        if not df_fin.empty:
            df_fin["filing_date"] = pd.to_datetime(df_fin["filing_date"])
            df_fin["date"] = pd.to_datetime(df_fin["date"])

            _FILING_LAG_BUFFER = pd.Timedelta(days=45)
            bad_mask = df_fin["filing_date"] <= df_fin["date"]
            n_fixed = bad_mask.sum()
            if n_fixed > 0:
                df_fin.loc[bad_mask, "filing_date"] = df_fin.loc[bad_mask, "date"] + _FILING_LAG_BUFFER
                logger.info(f"Filing date 修正: {n_fixed} 条 (filing_date <= report_date → +45天)")

            for col in ["revenue", "gross_profit", "operating_income", "net_income",
                        "total_stockholders_equity", "weighted_average_shs_out"]:
                if col in df_fin.columns:
                    df_fin[col] = pd.to_numeric(df_fin[col], errors="coerce")
            rev = df_fin["revenue"].replace(0, np.nan)
            df_fin["gross_margin"] = df_fin["gross_profit"] / rev
            df_fin["operating_margin"] = df_fin["operating_income"] / rev
            equity = df_fin["total_stockholders_equity"].replace(0, np.nan)
            df_fin["roe"] = df_fin["net_income"] / equity
            df_fin["weighted_avg_shares"] = df_fin["weighted_average_shs_out"]
        cls._static_cache["_bulk_financial"] = df_fin

        # 2. us_daily_price（回测区间 + 400 天前移量）
        price_start = (pd.to_datetime(start_date) - pd.Timedelta(days=400)).strftime("%Y-%m-%d")
        price_cols = [
            "ticker", "trade_date", "open", "high", "low",
            "close", "adj_close", "volume", "change_percent",
        ]
        df_price = cls._load_or_query(
            "us_daily_price", USDailyPrice, price_cols,
            price_start, end_date, "trade_date",
            order_by=["ticker", "trade_date"],
        )
        if not df_price.empty:
            df_price["trade_date"] = pd.to_datetime(df_price["trade_date"])
            df_price["adj_close"] = pd.to_numeric(df_price["adj_close"], errors="coerce")
            df_price["close"] = pd.to_numeric(df_price["close"], errors="coerce")
            df_price["adj_close"] = df_price["adj_close"].fillna(df_price["close"])
        cls._static_cache["_bulk_daily"] = df_price

        # 3. S&P 500 指数（IVOL 因子用）
        t0 = time.time()
        try:
            idx_cols = ["trade_date", "close"]
            df_idx = pd.DataFrame(
                USIndexDaily.objects.filter(
                    index_code="^GSPC",
                    trade_date__gte=price_start,
                    trade_date__lte=end_date,
                ).order_by("trade_date").values_list(*idx_cols),
                columns=idx_cols,
            )
            if not df_idx.empty:
                df_idx["trade_date"] = pd.to_datetime(df_idx["trade_date"])
                df_idx["close"] = pd.to_numeric(df_idx["close"], errors="coerce")
            cls._static_cache["_bulk_index"] = df_idx
            logger.info(f"US 预加载 us_index_daily (^GSPC): {len(df_idx)} 行, {time.time()-t0:.1f}s")
        except Exception as e:
            logger.warning(f"预加载 S&P 500 指数失败: {e}")
            cls._static_cache["_bulk_index"] = pd.DataFrame()

        # 4. us_analyst_recommendation
        t0 = time.time()
        analyst_start = (pd.to_datetime(start_date) - pd.Timedelta(days=180)).strftime("%Y-%m-%d")
        ar_cols = ["ticker", "date", "grading_company", "new_grade", "action"]
        df_ar = pd.DataFrame(
            USAnalystRecommendation.objects.filter(
                date__gte=analyst_start,
                date__lte=end_date,
            ).order_by("ticker", "date").values_list(*ar_cols),
            columns=ar_cols,
        )
        if not df_ar.empty:
            df_ar["date"] = pd.to_datetime(df_ar["date"])
        cls._static_cache["_bulk_analyst"] = df_ar
        logger.info(f"US 预加载 us_analyst_recommendation: {len(df_ar)} 行, {time.time()-t0:.1f}s")

        # 5. us_earnings_surprise
        t0 = time.time()
        es_start = (pd.to_datetime(start_date) - pd.Timedelta(days=180)).strftime("%Y-%m-%d")
        es_cols = ["ticker", "date", "eps_actual", "eps_estimated", "surprise", "surprise_pct"]
        df_es = pd.DataFrame(
            USEarningsSurprise.objects.filter(
                date__gte=es_start,
                date__lte=end_date,
            ).order_by("ticker", "date").values_list(*es_cols),
            columns=es_cols,
        )
        if not df_es.empty:
            df_es["date"] = pd.to_datetime(df_es["date"])
        cls._static_cache["_bulk_earnings_surprise"] = df_es
        logger.info(f"US 预加载 us_earnings_surprise: {len(df_es)} 行, {time.time()-t0:.1f}s")

        # 6. us_eps_estimate
        t0 = time.time()
        ee_cols = ["ticker", "date", "estimated_eps_avg", "estimated_eps_low",
                   "estimated_eps_high", "number_analysts_estimated_eps"]
        ee_start = (pd.to_datetime(start_date) - pd.Timedelta(days=400)).strftime("%Y-%m-%d")
        df_ee = pd.DataFrame(
            USEpsEstimate.objects.filter(
                date__gte=ee_start,
                date__lte=end_date,
            ).order_by("ticker", "date").values_list(*ee_cols),
            columns=ee_cols,
        )
        if not df_ee.empty:
            df_ee["date"] = pd.to_datetime(df_ee["date"])
        cls._static_cache["_bulk_eps_estimate"] = df_ee
        logger.info(f"US 预加载 us_eps_estimate: {len(df_ee)} 行, {time.time()-t0:.1f}s")

        # 7. us_corporate_action (dividend)
        t0 = time.time()
        div_start = (pd.to_datetime(start_date) - pd.Timedelta(days=400)).strftime("%Y-%m-%d")
        ca_cols = ["ticker", "date", "action_type", "dividend"]
        df_ca = pd.DataFrame(
            USCorporateAction.objects.filter(
                date__gte=div_start,
                date__lte=end_date,
                action_type="dividend",
            ).order_by("ticker", "date").values_list(*ca_cols),
            columns=ca_cols,
        )
        if not df_ca.empty:
            df_ca["date"] = pd.to_datetime(df_ca["date"])
        cls._static_cache["_bulk_dividends"] = df_ca
        logger.info(f"US 预加载 dividends: {len(df_ca)} 行, {time.time()-t0:.1f}s")

        # 8. us_enterprise_value.market_capitalization（季度精度）
        t0 = time.time()
        try:
            mktcap_start = (pd.to_datetime(start_date) - pd.Timedelta(days=120)).strftime("%Y-%m-%d")
            ev_cols = ["ticker", "date", "market_capitalization"]
            df_mktcap = pd.DataFrame(
                USEnterpriseValue.objects.filter(
                    date__gte=mktcap_start,
                    date__lte=end_date,
                    market_capitalization__isnull=False,
                    market_capitalization__gt=0,
                ).order_by("ticker", "date").values_list(*ev_cols),
                columns=ev_cols,
            )
            if not df_mktcap.empty:
                df_mktcap["date"] = pd.to_datetime(df_mktcap["date"])
                df_mktcap.rename(columns={"market_capitalization": "market_cap"}, inplace=True)
                df_mktcap["market_cap"] = pd.to_numeric(df_mktcap["market_cap"], errors="coerce")
            cls._static_cache["_bulk_mktcap"] = df_mktcap
            logger.info(f"US 预加载 us_enterprise_value: {len(df_mktcap)} 行, {time.time()-t0:.1f}s")
        except Exception as e:
            logger.warning(f"预加载 market_cap 失败: {e}")
            cls._static_cache["_bulk_mktcap"] = pd.DataFrame()

        # 9. us_insider_trade（INSIDER_NET_BUY 因子用）
        t0 = time.time()
        insider_start = (pd.to_datetime(start_date) - pd.Timedelta(days=120)).strftime("%Y-%m-%d")
        try:
            insider_types = ["P-Purchase", "S-Sale", "A-Award", "P-Purchase+", "S-Sale+"]
            ins_cols = ["ticker", "filing_date", "acquisition_or_disposition",
                        "securities_transacted", "price"]
            df_insider = pd.DataFrame(
                USInsiderTrade.objects.filter(
                    filing_date__gte=insider_start,
                    filing_date__lte=end_date,
                    price__gt=0,
                    securities_transacted__gt=0,
                    transaction_type__in=insider_types,
                ).values_list(*ins_cols),
                columns=ins_cols,
            )
            if not df_insider.empty:
                df_insider["filing_date"] = pd.to_datetime(df_insider["filing_date"])
                # 计算 net_value（原来在 SQL CASE WHEN 中做的）
                is_acquire = df_insider["acquisition_or_disposition"] == "A"
                df_insider["net_value"] = np.where(
                    is_acquire,
                    df_insider["securities_transacted"] * df_insider["price"],
                    -df_insider["securities_transacted"] * df_insider["price"],
                )
                df_insider = df_insider[["ticker", "filing_date", "net_value"]]
            cls._static_cache["_bulk_insider"] = df_insider
            logger.info(f"US 预加载 us_insider_trade: {len(df_insider)} 行, {time.time()-t0:.1f}s")
        except Exception as e:
            logger.warning(f"预加载 insider trade 失败: {e}")
            cls._static_cache["_bulk_insider"] = pd.DataFrame()

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
        from data.models import USIndustryClass
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
