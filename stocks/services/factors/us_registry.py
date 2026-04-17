"""
AlphaSignal 基类 + 全局因子注册表。

设计目标：
- 每个因子一个模块，用 @register 装饰器注册，自动进入全局表
- 元数据（version/category/horizon/expected_icir/status/inherent_direction/ic_window_months）
  由因子类自己声明，策略层和 CLI 层直接读取，不再硬编码
- 继承 USFactorBase，自动拥有预加载数据访问能力（_static_cache / get_latest_financial / ...）

用法：
    from stocks.services.factors.us_registry import AlphaSignal, register

    @register
    class MyFactor(AlphaSignal):
        name = "MY_FACTOR"
        version = "v1"
        category = "quality"
        horizon = "quarter"
        expected_icir = 0.10
        status = "staging"
        inherent_direction = +1
        data_deps = ["us_financial_data"]
        ic_window_months = 24

        def compute(self, date, universe):
            ...
            return df[["ticker", "factor_value"]]
"""

import logging
from abc import ABC, abstractmethod
from datetime import timedelta
from typing import ClassVar, Iterable

import numpy as np
import pandas as pd

from services.config import LOG_LEVEL
from stocks.services.factors.us_base import USFactorBase

logger = logging.getLogger(__name__)
logger.setLevel(LOG_LEVEL)


# ===========================================================================
# 全局 registry
# ===========================================================================

_REGISTRY: dict[str, type["AlphaSignal"]] = {}

# 合法 status 集合 — 超出会报错
_VALID_STATUS = {"dev", "staging", "live", "deprecated"}
# 合法 horizon 集合
_VALID_HORIZON = {"day", "week", "month", "quarter"}


def register(cls: type["AlphaSignal"]) -> type["AlphaSignal"]:
    """装饰器：把 AlphaSignal 子类注册到全局表。

    校验元数据完整性：
        - name 必填、全局唯一
        - category / horizon / status 在合法集合中
        - inherent_direction 只能是 -1 / 0 / +1
    """
    if not issubclass(cls, AlphaSignal):
        raise TypeError(f"@register can only decorate AlphaSignal subclasses, got {cls}")

    if not cls.name or cls.name == AlphaSignal.name:
        raise ValueError(f"{cls.__name__} must override class attribute 'name'")

    if cls.name in _REGISTRY:
        existing = _REGISTRY[cls.name]
        raise ValueError(
            f"Signal name {cls.name!r} already registered by {existing.__module__}.{existing.__name__}, "
            f"cannot register {cls.__module__}.{cls.__name__}"
        )

    if not cls.category:
        raise ValueError(f"{cls.__name__}: category must be non-empty")

    if cls.horizon not in _VALID_HORIZON:
        raise ValueError(f"{cls.__name__}: horizon={cls.horizon!r} not in {_VALID_HORIZON}")

    if cls.status not in _VALID_STATUS:
        raise ValueError(f"{cls.__name__}: status={cls.status!r} not in {_VALID_STATUS}")

    if cls.inherent_direction not in (-1, 0, 1):
        raise ValueError(
            f"{cls.__name__}: inherent_direction={cls.inherent_direction} must be -1, 0, or +1"
        )

    _REGISTRY[cls.name] = cls
    logger.debug(
        f"Registered AlphaSignal {cls.name} "
        f"(category={cls.category}, status={cls.status}, direction={cls.inherent_direction})"
    )
    return cls


def get_registered() -> dict[str, type["AlphaSignal"]]:
    """返回注册表的只读副本。"""
    return dict(_REGISTRY)


def get_active() -> dict[str, type["AlphaSignal"]]:
    """只返回 status in (live, staging) 的因子——真正参与策略的那些。"""
    return {n: c for n, c in _REGISTRY.items() if c.status in ("live", "staging")}


def get_by_category(category: str, only_active: bool = True) -> list[type["AlphaSignal"]]:
    """按 category 过滤。默认只返回 active 的。"""
    pool = get_active() if only_active else get_registered()
    return [c for c in pool.values() if c.category == category]


def clear_registry() -> None:
    """仅测试用——清空注册表。"""
    _REGISTRY.clear()


# ===========================================================================
# AlphaSignal 基类
# ===========================================================================


class AlphaSignal(USFactorBase, ABC):
    """工业级 Alpha 信号基类。

    所有因子必须：
    1. 继承 AlphaSignal
    2. 覆盖 name / category / horizon / inherent_direction / status（其他有默认值）
    3. 实现 compute(date, universe) -> DataFrame[ticker, factor_value]
    4. 用 @register 装饰器注册

    设计约定：
    - compute() 只从 _static_cache / _date_cache 读数据，不直接查 DB
    - 每个 return 前必须有 logger.debug/warning 说明原因（禁止静默失败）
    - 返回 DataFrame 必须有且仅有 ["ticker", "factor_value"] 两列
    """

    # ——— 元数据（子类覆盖） ———
    name: ClassVar[str] = ""
    """因子名，大写，例 'PIOTROSKI_F'。全局唯一。"""

    version: ClassVar[str] = "v1"
    """版本号，算法或数据源变更时 +1。"""

    category: ClassVar[str] = ""
    """类别：value/quality/growth/momentum/technical/analyst/sentiment/defensive/liquidity/..."""

    horizon: ClassVar[str] = "month"
    """预期信号衰减期：day/week/month/quarter。决定滚动 IC 窗口和信号混合权重。"""

    expected_icir: ClassVar[float] = 0.0
    """预期 ICIR（论文 / 历史观察值），仅做注释参考，不参与运算。"""

    status: ClassVar[str] = "dev"
    """状态机：
        dev       — 开发中，不参与策略
        staging   — 灰度上线，参与策略但权重可监控
        live      — 正式上线
        deprecated — 已下线，保留代码但不参与策略
    """

    inherent_direction: ClassVar[int] = 0
    """因子固有方向：
        +1 — 高值=利好（ROE / FCF-yield），锁定正向
        -1 — 高值=利空（Beneish M / Ohlson O / Volatility），锁定负向
         0 — 方向不定，由滚动 IC 每月决定
    """

    data_deps: ClassVar[list[str]] = []
    """数据依赖的 DB 表名列表，用于依赖检查和文档。例 ['us_financial_data', 'us_key_metric']。"""

    ic_window_months: ClassVar[int] = 18
    """滚动 IC 计算窗口（月）。慢因子用大值（Value 30M），快因子用小值（Momentum 6-12M）。"""

    # ——— 行为 ———

    @abstractmethod
    def compute(self, date: str, universe: pd.DataFrame) -> pd.DataFrame:
        """计算截面因子值。

        Args:
            date: YYYY-MM-DD，计算日（截面日）。
            universe: 至少包含 ticker 列的 DataFrame。

        Returns:
            DataFrame 严格包含 ["ticker", "factor_value"] 两列。
            不可返回空 DataFrame 的情况，请返回 `pd.DataFrame(columns=["ticker", "factor_value"])`
            并在 return 前打 logger.debug/warning。
        """
        raise NotImplementedError

    @classmethod
    def metadata(cls) -> dict:
        """返回因子元数据（用于展示 / 文档生成）。"""
        return {
            "name": cls.name,
            "version": cls.version,
            "category": cls.category,
            "horizon": cls.horizon,
            "expected_icir": cls.expected_icir,
            "status": cls.status,
            "inherent_direction": cls.inherent_direction,
            "data_deps": list(cls.data_deps),
            "ic_window_months": cls.ic_window_months,
            "description": (cls.__doc__ or "").strip().split("\n")[0],
        }

    # ==================================================================
    # 三层缓存：DB → 本地 parquet → 内存 _static_cache
    # ==================================================================
    #
    # 调用 preload_alpha_cache(start, end) 后：
    #   1. 检查 cache/ 目录是否有覆盖范围的 parquet 文件
    #   2. 没有 → 从 DB 查询 → 存 parquet
    #   3. 从 parquet 加载到 _static_cache（内存）
    #   4. fetch_* helpers 先查内存，miss 则 fallback ORM
    # ==================================================================

    _FILING_LAG_DAYS = 45  # filing_date <= report_date 时的兜底 lag

    @classmethod
    def preload_alpha_cache(cls, start_date: str, end_date: str) -> None:
        """一次性预加载 AlphaSignal 所需全部数据到内存。

        数据流：DB → parquet 文件 → _static_cache（内存）。
        已有 parquet 缓存且范围覆盖时直接读文件，不查 DB。

        Args:
            start_date: 回测起始日 (YYYY-MM-DD)
            end_date: 回测结束日 (YYYY-MM-DD)
        """
        import time

        t_total = time.time()
        logger.info(f"AlphaSignal preload_alpha_cache: {start_date} → {end_date}")

        # ---- 1. us_financial_data（全字段，5 年回看） ----
        cls._preload_financial(start_date, end_date)

        # ---- 2. us_daily_price（400 天回看） ----
        cls._preload_daily_price(start_date, end_date)

        # ---- 3. us_key_metric（2 年回看） ----
        cls._preload_key_metric(start_date, end_date)

        # ---- 4. us_enterprise_value（全字段，200 天回看） ----
        cls._preload_enterprise_value(start_date, end_date)

        # ---- 5. FF5（已有本地 CSV，直接加载） ----
        cls.fetch_ff5_factors()

        logger.info(f"AlphaSignal preload 完成: {time.time() - t_total:.1f}s")

    @classmethod
    def _preload_financial(cls, start_date: str, end_date: str) -> None:
        """预加载 us_financial_data 全字段。"""
        import time
        from stocks.models import USFinancialData

        t0 = time.time()
        fin_start = (pd.Timestamp(start_date) - pd.DateOffset(years=5)).strftime("%Y-%m-%d")

        # 取全部非系统字段
        all_fields = [
            f.name for f in USFinancialData._meta.get_fields()
            if f.name not in ("id", "updated_at")
        ]

        df = cls._load_or_query(
            "alpha_financial", USFinancialData, all_fields,
            fin_start, end_date, "filing_date",
        )
        if not df.empty:
            df["filing_date"] = pd.to_datetime(df["filing_date"])
            df["date"] = pd.to_datetime(df["date"])
            # filing_date 修正
            bad = df["filing_date"] <= df["date"]
            if bad.any():
                df.loc[bad, "filing_date"] = df.loc[bad, "date"] + pd.Timedelta(days=cls._FILING_LAG_DAYS)

        cls._static_cache["_alpha_financial"] = df
        logger.info(f"  alpha_financial: {len(df)} rows, {time.time() - t0:.1f}s")

    @classmethod
    def _preload_daily_price(cls, start_date: str, end_date: str) -> None:
        """预加载 us_daily_price。复用 legacy _bulk_daily 如果已存在。"""
        import time

        # 如果 legacy preload 已加载，直接复用
        if "_bulk_daily" in cls._static_cache and not cls._static_cache["_bulk_daily"].empty:
            cls._static_cache["_alpha_daily"] = cls._static_cache["_bulk_daily"]
            logger.info("  alpha_daily: 复用 _bulk_daily")
            return

        from stocks.models import USDailyPrice

        t0 = time.time()
        price_start = (pd.Timestamp(start_date) - pd.Timedelta(days=450)).strftime("%Y-%m-%d")
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
            for c in ["open", "high", "low", "volume", "change_percent"]:
                if c in df.columns:
                    df[c] = pd.to_numeric(df[c], errors="coerce")

        cls._static_cache["_alpha_daily"] = df
        logger.info(f"  alpha_daily: {len(df)} rows, {time.time() - t0:.1f}s")

    @classmethod
    def _preload_key_metric(cls, start_date: str, end_date: str) -> None:
        """预加载 us_key_metric 全字段。"""
        import time
        from stocks.models import USKeyMetric

        t0 = time.time()
        km_start = (pd.Timestamp(start_date) - pd.DateOffset(years=2)).strftime("%Y-%m-%d")

        all_fields = [
            f.name for f in USKeyMetric._meta.get_fields()
            if f.name not in ("id", "updated_at")
        ]

        df = cls._load_or_query(
            "alpha_key_metric", USKeyMetric, all_fields,
            km_start, end_date, "date",
        )
        if not df.empty:
            df["date"] = pd.to_datetime(df["date"])

        cls._static_cache["_alpha_key_metric"] = df
        logger.info(f"  alpha_key_metric: {len(df)} rows, {time.time() - t0:.1f}s")

    @classmethod
    def _preload_enterprise_value(cls, start_date: str, end_date: str) -> None:
        """预加载 us_enterprise_value（market_cap + EV）。"""
        import time
        from stocks.models import USEnterpriseValue

        t0 = time.time()
        ev_start = (pd.Timestamp(start_date) - pd.Timedelta(days=250)).strftime("%Y-%m-%d")
        ev_cols = ["ticker", "date", "market_capitalization", "enterprise_value"]

        df = cls._load_or_query(
            "alpha_enterprise_value", USEnterpriseValue, ev_cols,
            ev_start, end_date, "date",
        )
        if not df.empty:
            df["date"] = pd.to_datetime(df["date"])
            df["market_capitalization"] = pd.to_numeric(df["market_capitalization"], errors="coerce")
            df["enterprise_value"] = pd.to_numeric(df["enterprise_value"], errors="coerce")

        cls._static_cache["_alpha_ev"] = df
        logger.info(f"  alpha_ev: {len(df)} rows, {time.time() - t0:.1f}s")

    # ------------------------------------------------------------------
    # 统一 market_cap / EV 查询（走缓存或 ORM）
    # ------------------------------------------------------------------

    @classmethod
    def fetch_market_cap(cls, date: str, tickers: list[str]) -> pd.DataFrame:
        """取 date 可见的最新市值。优先走缓存。

        Returns:
            DataFrame[ticker, market_cap]
        """
        date_ts = pd.Timestamp(date)

        # 尝试从缓存切片
        cache = cls._static_cache.get("_alpha_ev")
        if cache is not None and not cache.empty:
            start = date_ts - pd.Timedelta(days=200)
            mask = (
                cache["ticker"].isin(tickers)
                & (cache["date"] >= start)
                & (cache["date"] <= date_ts)
                & cache["market_capitalization"].notna()
                & (cache["market_capitalization"] > 0)
            )
            df = cache.loc[mask, ["ticker", "date", "market_capitalization"]].copy()
            if not df.empty:
                df = df.sort_values(["ticker", "date"], ascending=[True, False])
                df = df.drop_duplicates(subset=["ticker"], keep="first")
                df = df.rename(columns={"market_capitalization": "market_cap"})
                return df[["ticker", "market_cap"]].reset_index(drop=True)

        # fallback ORM
        from stocks.models import USEnterpriseValue

        start = (date_ts - pd.DateOffset(days=200)).date()
        qs = USEnterpriseValue.objects.filter(
            ticker__in=tickers,
            date__gte=start,
            date__lte=date_ts.date(),
            market_capitalization__isnull=False,
            market_capitalization__gt=0,
        ).values_list("ticker", "date", "market_capitalization")

        df = pd.DataFrame(list(qs), columns=["ticker", "date", "market_cap"])
        if df.empty:
            return pd.DataFrame(columns=["ticker", "market_cap"])
        df["date"] = pd.to_datetime(df["date"])
        df = df.sort_values(["ticker", "date"], ascending=[True, False])
        df = df.drop_duplicates(subset=["ticker"], keep="first")
        df["market_cap"] = pd.to_numeric(df["market_cap"], errors="coerce")
        return df[["ticker", "market_cap"]].reset_index(drop=True)

    @classmethod
    def fetch_enterprise_value(cls, date: str, tickers: list[str]) -> pd.DataFrame:
        """取 date 可见的最新 EV。优先走缓存。

        Returns:
            DataFrame[ticker, ev]
        """
        date_ts = pd.Timestamp(date)

        cache = cls._static_cache.get("_alpha_ev")
        if cache is not None and not cache.empty:
            start = date_ts - pd.Timedelta(days=200)
            mask = (
                cache["ticker"].isin(tickers)
                & (cache["date"] >= start)
                & (cache["date"] <= date_ts)
                & cache["enterprise_value"].notna()
            )
            df = cache.loc[mask, ["ticker", "date", "enterprise_value"]].copy()
            if not df.empty:
                df = df.sort_values(["ticker", "date"], ascending=[True, False])
                df = df.drop_duplicates(subset=["ticker"], keep="first")
                df = df.rename(columns={"enterprise_value": "ev"})
                return df[["ticker", "ev"]].reset_index(drop=True)

        # fallback ORM
        from stocks.models import USEnterpriseValue

        start = (date_ts - pd.DateOffset(days=200)).date()
        qs = USEnterpriseValue.objects.filter(
            ticker__in=tickers,
            date__gte=start,
            date__lte=date_ts.date(),
            enterprise_value__isnull=False,
        ).values_list("ticker", "date", "enterprise_value")

        df = pd.DataFrame(list(qs), columns=["ticker", "date", "ev"])
        if df.empty:
            return pd.DataFrame(columns=["ticker", "ev"])
        df["date"] = pd.to_datetime(df["date"])
        df = df.sort_values(["ticker", "date"], ascending=[True, False])
        df = df.drop_duplicates(subset=["ticker"], keep="first")
        df["ev"] = pd.to_numeric(df["ev"], errors="coerce")
        return df[["ticker", "ev"]].reset_index(drop=True)

    @staticmethod
    def _apply_filing_lag(df: pd.DataFrame) -> pd.DataFrame:
        """修正 filing_date <= report_date 的异常（常见于历史数据），加 45 天兜底。"""
        if df.empty or "filing_date" not in df.columns or "date" not in df.columns:
            return df
        df = df.copy()
        df["filing_date"] = pd.to_datetime(df["filing_date"])
        df["date"] = pd.to_datetime(df["date"])
        bad = df["filing_date"] <= df["date"]
        n_bad = int(bad.sum())
        if n_bad:
            df.loc[bad, "filing_date"] = df.loc[bad, "date"] + pd.Timedelta(days=AlphaSignal._FILING_LAG_DAYS)
            logger.debug(f"_apply_filing_lag: 修正 {n_bad} 条 filing_date <= report_date")
        return df

    @classmethod
    def fetch_financial_latest(
        cls,
        date: str,
        tickers: Iterable[str],
        columns: list[str],
        lookback_years: int = 2,
    ) -> pd.DataFrame:
        """获取每只股票在 `date` 可见的最新一条 us_financial_data。优先走缓存。"""
        tickers = list(tickers)
        if not tickers:
            logger.debug("fetch_financial_latest: 空 ticker 列表")
            return pd.DataFrame()

        date_ts = pd.Timestamp(date)
        start_ts = date_ts - pd.DateOffset(years=lookback_years)
        base_cols = ["ticker", "date", "filing_date", "period"]
        all_cols = base_cols + [c for c in columns if c not in base_cols]

        # ---- 尝试缓存 ----
        cache = cls._static_cache.get("_alpha_financial")
        if cache is not None and not cache.empty:
            # 检查请求列是否都在缓存中
            missing = [c for c in all_cols if c not in cache.columns]
            if not missing:
                mask = (
                    cache["ticker"].isin(tickers)
                    & (cache["filing_date"] >= start_ts)
                    & (cache["filing_date"] <= date_ts)
                )
                df = cache.loc[mask, all_cols].copy()
                if not df.empty:
                    df = df.sort_values(["ticker", "date"], ascending=[True, False])
                    df = df.drop_duplicates(subset=["ticker"], keep="first").reset_index(drop=True)
                    for c in columns:
                        if c in df.columns:
                            df[c] = pd.to_numeric(df[c], errors="coerce")
                    return df

        # ---- fallback ORM ----
        from stocks.models import USFinancialData

        qs = USFinancialData.objects.filter(
            ticker__in=tickers,
            filing_date__gte=start_ts.date(),
            filing_date__lte=date_ts.date(),
        ).values_list(*all_cols)

        df = pd.DataFrame(list(qs), columns=all_cols)
        if df.empty:
            logger.debug(f"fetch_financial_latest({date}): 无数据 (tickers={len(tickers)})")
            return df

        df = cls._apply_filing_lag(df)
        df = df[df["filing_date"] <= date_ts]
        if df.empty:
            logger.debug(f"fetch_financial_latest({date}): filing_date 修正后无剩余")
            return df

        df = df.sort_values(["ticker", "date"], ascending=[True, False])
        df = df.drop_duplicates(subset=["ticker"], keep="first").reset_index(drop=True)
        for c in columns:
            if c in df.columns:
                df[c] = pd.to_numeric(df[c], errors="coerce")
        return df

    @classmethod
    def fetch_financial_history(
        cls,
        date: str,
        tickers: Iterable[str],
        columns: list[str],
        n_quarters: int,
    ) -> pd.DataFrame:
        """获取每只股票 `date` 可见的最近 n_quarters 条季报（长格式）。优先走缓存。

        用于：同比差分（ΔROA）、历史波动率（QMJ_EARNINGS_VOL）、自相关（EARNINGS_PERSISTENCE）。
        """
        tickers = list(tickers)
        if not tickers:
            logger.debug("fetch_financial_history: 空 ticker 列表")
            return pd.DataFrame()

        date_ts = pd.Timestamp(date)
        start_ts = date_ts - pd.DateOffset(days=int(n_quarters * 100) + 180)
        base_cols = ["ticker", "date", "filing_date", "period"]
        all_cols = base_cols + [c for c in columns if c not in base_cols]

        # ---- 尝试缓存 ----
        cache = cls._static_cache.get("_alpha_financial")
        if cache is not None and not cache.empty:
            missing = [c for c in all_cols if c not in cache.columns]
            if not missing:
                mask = (
                    cache["ticker"].isin(tickers)
                    & (cache["filing_date"] >= start_ts)
                    & (cache["filing_date"] <= date_ts)
                )
                df = cache.loc[mask, all_cols].copy()
                if not df.empty:
                    df = df.sort_values(["ticker", "date"], ascending=[True, False])
                    df = df.groupby("ticker", group_keys=False).head(n_quarters).reset_index(drop=True)
                    for c in columns:
                        if c in df.columns:
                            df[c] = pd.to_numeric(df[c], errors="coerce")
                    return df

        # ---- fallback ORM ----
        from stocks.models import USFinancialData

        qs = USFinancialData.objects.filter(
            ticker__in=tickers,
            filing_date__gte=start_ts.date(),
            filing_date__lte=date_ts.date(),
        ).values_list(*all_cols)

        df = pd.DataFrame(list(qs), columns=all_cols)
        if df.empty:
            logger.debug(f"fetch_financial_history({date}, n={n_quarters}): 无数据")
            return df

        df = cls._apply_filing_lag(df)
        df = df[df["filing_date"] <= date_ts]
        df = df.sort_values(["ticker", "date"], ascending=[True, False])
        df = df.groupby("ticker", group_keys=False).head(n_quarters).reset_index(drop=True)
        for c in columns:
            if c in df.columns:
                df[c] = pd.to_numeric(df[c], errors="coerce")
        return df

    @classmethod
    def fetch_key_metric_latest(
        cls,
        date: str,
        tickers: Iterable[str],
        columns: list[str],
        lookback_years: int = 2,
    ) -> pd.DataFrame:
        """获取每只股票在 `date` 可见的最新一条 us_key_metric。优先走缓存。

        注意：USKeyMetric 没有 filing_date 字段，只能用 `date` 列（report date）并加 45 天
        保守 lag，近似模拟 filing_date。
        """
        tickers = list(tickers)
        if not tickers:
            logger.debug("fetch_key_metric_latest: 空 ticker 列表")
            return pd.DataFrame()

        date_ts = pd.Timestamp(date)
        effective_cutoff = date_ts - pd.Timedelta(days=cls._FILING_LAG_DAYS)
        start_ts = date_ts - pd.DateOffset(years=lookback_years)
        base_cols = ["ticker", "date", "period"]
        all_cols = base_cols + [c for c in columns if c not in base_cols]

        # ---- 尝试缓存 ----
        cache = cls._static_cache.get("_alpha_key_metric")
        if cache is not None and not cache.empty:
            missing = [c for c in all_cols if c not in cache.columns]
            if not missing:
                mask = (
                    cache["ticker"].isin(tickers)
                    & (cache["date"] >= start_ts)
                    & (cache["date"] <= effective_cutoff)
                )
                df = cache.loc[mask, all_cols].copy()
                if not df.empty:
                    df = df.sort_values(["ticker", "date"], ascending=[True, False])
                    df = df.drop_duplicates(subset=["ticker"], keep="first").reset_index(drop=True)
                    for c in columns:
                        if c in df.columns:
                            df[c] = pd.to_numeric(df[c], errors="coerce")
                    return df

        # ---- fallback ORM ----
        from stocks.models import USKeyMetric

        qs = USKeyMetric.objects.filter(
            ticker__in=tickers,
            date__gte=start_ts.date(),
            date__lte=effective_cutoff.date(),
        ).values_list(*all_cols)

        df = pd.DataFrame(list(qs), columns=all_cols)
        if df.empty:
            logger.debug(f"fetch_key_metric_latest({date}): 无数据")
            return df

        df["date"] = pd.to_datetime(df["date"])
        df = df.sort_values(["ticker", "date"], ascending=[True, False])
        df = df.drop_duplicates(subset=["ticker"], keep="first").reset_index(drop=True)
        for c in columns:
            if c in df.columns:
                df[c] = pd.to_numeric(df[c], errors="coerce")
        return df

    # ------------------------------------------------------------------
    # 价格 / 指数 / 行业 helper（Momentum 批次新增）
    # ------------------------------------------------------------------

    @classmethod
    def fetch_price_history(
        cls,
        date: str,
        tickers: Iterable[str],
        lookback_days: int,
        columns: list[str] | None = None,
    ) -> pd.DataFrame:
        """获取 `date` 之前 `lookback_days` 个日历日的日线。优先走缓存。

        Returns:
            DataFrame[ticker, trade_date, *columns]，按 (ticker, trade_date ASC) 排序。
            columns 默认 ['adj_close', 'close', 'volume', 'change_percent']。
        """
        tickers = list(tickers)
        if not tickers:
            logger.debug("fetch_price_history: 空 ticker")
            return pd.DataFrame()

        if columns is None:
            columns = ["adj_close", "close", "volume", "change_percent"]
        requested = list(columns)
        internal_cols = list(requested)
        if "adj_close" in requested and "close" not in internal_cols:
            internal_cols.append("close")
        base = ["ticker", "trade_date"]
        all_cols = base + [c for c in internal_cols if c not in base]

        end_ts = pd.Timestamp(date)
        start_ts = end_ts - pd.Timedelta(days=lookback_days)

        # ---- 尝试缓存 ----
        cache = cls._static_cache.get("_alpha_daily")
        if cache is not None and not cache.empty:
            missing = [c for c in all_cols if c not in cache.columns]
            if not missing:
                tickers_set = set(tickers)
                mask = (
                    cache["ticker"].isin(tickers_set)
                    & (cache["trade_date"] >= start_ts)
                    & (cache["trade_date"] <= end_ts)
                )
                df = cache.loc[mask, all_cols].copy()
                if not df.empty:
                    df = df.sort_values(["ticker", "trade_date"])
                    if "adj_close" in df.columns and "close" in df.columns:
                        df["adj_close"] = df["adj_close"].fillna(df["close"])
                    keep = base + [c for c in requested if c in df.columns]
                    return df[keep]

        # ---- fallback ORM ----
        from stocks.models import USDailyPrice

        qs = USDailyPrice.objects.filter(
            ticker__in=tickers,
            trade_date__gte=start_ts.date(),
            trade_date__lte=end_ts.date(),
        ).order_by("ticker", "trade_date").values_list(*all_cols)

        df = pd.DataFrame(list(qs), columns=all_cols)
        if df.empty:
            logger.debug(f"fetch_price_history({date}, {lookback_days}d): 无数据")
            return df
        df["trade_date"] = pd.to_datetime(df["trade_date"])
        for c in internal_cols:
            if c in df.columns:
                df[c] = pd.to_numeric(df[c], errors="coerce")
        if "adj_close" in df.columns and "close" in df.columns:
            df["adj_close"] = df["adj_close"].fillna(df["close"])
        keep = base + [c for c in requested if c in df.columns]
        return df[keep]

    @classmethod
    def fetch_index_history(
        cls,
        date: str,
        index_code: str,
        lookback_days: int,
    ) -> pd.DataFrame:
        """获取指数（如 ^GSPC）的日线。"""
        from stocks.models import USIndexDaily

        end_ts = pd.Timestamp(date)
        start = (end_ts - pd.Timedelta(days=lookback_days)).date()
        qs = USIndexDaily.objects.filter(
            index_code=index_code,
            trade_date__gte=start,
            trade_date__lte=end_ts.date(),
        ).order_by("trade_date").values_list("trade_date", "close")
        df = pd.DataFrame(list(qs), columns=["trade_date", "close"])
        if df.empty:
            logger.debug(f"fetch_index_history({index_code}, {date}): 无数据")
            return df
        df["trade_date"] = pd.to_datetime(df["trade_date"])
        df["close"] = pd.to_numeric(df["close"], errors="coerce")
        return df

    _ff5_cache: ClassVar[pd.DataFrame | None] = None

    @classmethod
    def fetch_ff5_factors(cls) -> pd.DataFrame:
        """加载 FF5 日度因子数据（本地 CSV 缓存）。

        Returns:
            DataFrame index=Date, cols=['Mkt-RF', 'SMB', 'HML', 'RMW', 'CMA', 'RF']
            数值单位：日收益率（小数，非百分比）。
        """
        if cls._ff5_cache is not None:
            return cls._ff5_cache
        from services.config import PROJECT_ROOT

        cache_path = PROJECT_ROOT / "output" / "ff5_data" / "ff5_daily.csv"
        if not cache_path.exists():
            logger.warning(f"FF5 cache not found at {cache_path}")
            return pd.DataFrame()
        df = pd.read_csv(cache_path, index_col=0, parse_dates=True)
        for c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
        cls._ff5_cache = df
        logger.debug(f"FF5 loaded: {len(df)} days, {df.columns.tolist()}")
        return df

    @classmethod
    def fetch_industry_map(
        cls, tickers: Iterable[str] | None = None
    ) -> pd.DataFrame:
        """获取 GICS 行业映射。

        Returns:
            DataFrame[ticker, sector, industry]。
        """
        from stocks.models import USIndustryClass

        qs = USIndustryClass.objects.values_list("ticker", "sector", "industry")
        df = pd.DataFrame(list(qs), columns=["ticker", "sector", "industry"])
        if tickers is not None:
            tickers = set(tickers)
            df = df[df["ticker"].isin(tickers)]
        return df.reset_index(drop=True)

    @classmethod
    def pick_year_ago(
        cls,
        hist: pd.DataFrame,
        columns: list[str],
    ) -> pd.DataFrame:
        """从 fetch_financial_history 的长格式里，每只股票取最新 + ~4 季度前两条。

        Returns:
            DataFrame[ticker, *{col}_now, *{col}_yoy]，每只股票 1 行（有 ≥ 5 条季报才给结果）。
        """
        if hist.empty:
            return pd.DataFrame()

        out_rows = []
        for ticker, grp in hist.groupby("ticker", sort=False):
            # 已按 date DESC 排序
            if len(grp) < 5:
                continue
            latest = grp.iloc[0]
            yoy = grp.iloc[4]  # 4 季度前（index 0 是最新 Q，index 4 是 1 年前同期 Q）
            row = {"ticker": ticker}
            for c in columns:
                row[f"{c}_now"] = latest.get(c, np.nan)
                row[f"{c}_yoy"] = yoy.get(c, np.nan)
            out_rows.append(row)

        if not out_rows:
            logger.debug("pick_year_ago: 所有 ticker 季报数都 < 5，返回空")
            return pd.DataFrame()
        return pd.DataFrame(out_rows)
