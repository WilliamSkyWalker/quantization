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

    # ------------------------------------------------------------------
    # ORM 直查 helpers（Quality 批次用；不经过 preload/parquet）
    # ------------------------------------------------------------------
    # 设计原则：
    # - 新 Quality 因子直接查 USFinancialData / USKeyMetric，走 filing_date <= date 防未来偏差
    # - 每次调用查一次 DB，不做缓存；后续批次再引入缓存层
    # - 只接 Django ORM（不写 raw SQL），遵守 CLAUDE.md
    # ------------------------------------------------------------------

    _FILING_LAG_DAYS = 45  # filing_date <= report_date 时的兜底 lag

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
        """获取每只股票在 `date` 可见的最新一条 us_financial_data（ORM 直查，无缓存）。"""
        from stocks.models import USFinancialData

        tickers = list(tickers)
        if not tickers:
            logger.debug("fetch_financial_latest: 空 ticker 列表")
            return pd.DataFrame()

        date_ts = pd.Timestamp(date)
        start = (date_ts - pd.DateOffset(years=lookback_years)).date()

        base_cols = ["ticker", "date", "filing_date", "period"]
        all_cols = base_cols + [c for c in columns if c not in base_cols]

        qs = USFinancialData.objects.filter(
            ticker__in=tickers,
            filing_date__gte=start,
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
        """获取每只股票 `date` 可见的最近 n_quarters 条季报（长格式，ORM 直查，无缓存）。

        用于：同比差分（ΔROA）、历史波动率（QMJ_EARNINGS_VOL）、自相关（EARNINGS_PERSISTENCE）。
        """
        from stocks.models import USFinancialData

        tickers = list(tickers)
        if not tickers:
            logger.debug("fetch_financial_history: 空 ticker 列表")
            return pd.DataFrame()

        date_ts = pd.Timestamp(date)
        # 每季 ~90 天，加 buffer 保证能拿到 n_quarters 条
        start = (date_ts - pd.DateOffset(days=int(n_quarters * 100) + 180)).date()

        base_cols = ["ticker", "date", "filing_date", "period"]
        all_cols = base_cols + [c for c in columns if c not in base_cols]

        qs = USFinancialData.objects.filter(
            ticker__in=tickers,
            filing_date__gte=start,
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
        """获取每只股票在 `date` 可见的最新一条 us_key_metric。

        注意：USKeyMetric 没有 filing_date 字段，只能用 `date` 列（report date）并加 45 天
        保守 lag，近似模拟 filing_date。
        """
        from stocks.models import USKeyMetric

        tickers = list(tickers)
        if not tickers:
            logger.debug("fetch_key_metric_latest: 空 ticker 列表")
            return pd.DataFrame()

        date_ts = pd.Timestamp(date)
        effective_cutoff = (date_ts - pd.Timedelta(days=cls._FILING_LAG_DAYS)).date()
        start = (date_ts - pd.DateOffset(years=lookback_years)).date()

        base_cols = ["ticker", "date", "period"]
        all_cols = base_cols + [c for c in columns if c not in base_cols]

        qs = USKeyMetric.objects.filter(
            ticker__in=tickers,
            date__gte=start,
            date__lte=effective_cutoff,
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
        """获取 `date` 之前 `lookback_days` 个日历日的日线（ORM 直查，无缓存）。

        Returns:
            DataFrame[ticker, trade_date, *columns]，按 (ticker, trade_date ASC) 排序。
            columns 默认 ['adj_close', 'close', 'volume', 'change_percent']。
        """
        from stocks.models import USDailyPrice

        tickers = list(tickers)
        if not tickers:
            logger.debug("fetch_price_history: 空 ticker")
            return pd.DataFrame()

        if columns is None:
            columns = ["adj_close", "close", "volume", "change_percent"]
        requested = list(columns)
        # 内部强制带 close 做 adj_close fallback（FMP /stable/ 端点不返 adj_close）
        internal_cols = list(requested)
        if "adj_close" in requested and "close" not in internal_cols:
            internal_cols.append("close")
        base = ["ticker", "trade_date"]
        all_cols = base + [c for c in internal_cols if c not in base]

        end_ts = pd.Timestamp(date)
        start = (end_ts - pd.Timedelta(days=lookback_days)).date()

        qs = USDailyPrice.objects.filter(
            ticker__in=tickers,
            trade_date__gte=start,
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
