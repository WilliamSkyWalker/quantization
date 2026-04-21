"""Revenue Segment 因子集合

基于 USRevenueSegment（产品线 + 地理拆分），可构建 3 个因子：

1. REV_CONCENTRATION  — 产品线 Herfindahl 指数（营收集中度）
2. GEO_CONCENTRATION  — 地理区域 Herfindahl 指数
3. SEGMENT_GROWTH_DISP — 各产品线增速标准差（结构性变化信号）

数据源：USRevenueSegment (ticker, date, segment_type, segment_name, revenue)
- segment_type: 'product' 或 'geographic'
- 4008 tickers, 2009-2026
"""

import logging

import numpy as np
import pandas as pd

from services.config import LOG_LEVEL
from stocks.services.factors.us_registry import AlphaSignal, register

logger = logging.getLogger(__name__)
logger.setLevel(LOG_LEVEL)


def _herfindahl(revenues: np.ndarray) -> float:
    """计算 Herfindahl 指数 = Σ(s_i²)，s_i = rev_i / total_rev。

    HHI ∈ [1/N, 1]。1/N = 完全分散，1 = 完全集中。
    """
    total = revenues.sum()
    if total <= 0:
        return np.nan
    shares = revenues / total
    return float((shares ** 2).sum())


def _fetch_segments(date: str, tickers: list[str], seg_type: str) -> pd.DataFrame:
    """取截面日可见的最新一期 segment 数据。优先走缓存。"""
    from stocks.services.factors.us_base import USFactorBase

    date_ts = pd.Timestamp(date)
    start_ts = date_ts - pd.DateOffset(years=2)

    # ---- 优先从预加载缓存获取 ----
    bulk = USFactorBase._static_cache.get("_bulk_revenue_segment")
    if bulk is not None and not bulk.empty:
        mask = (
            (bulk["ticker"].isin(tickers))
            & (bulk["date"] >= start_ts)
            & (bulk["date"] <= date_ts)
            & (bulk["segment_type"] == seg_type)
            & (bulk["revenue"].notna())
        )
        df = bulk[mask][["ticker", "date", "segment", "revenue"]].copy()
        if df.empty:
            return df
        # 每只股票取最新 date 的所有 segments
        latest_dates = df.groupby("ticker")["date"].max().reset_index()
        latest_dates.columns = ["ticker", "max_date"]
        df = df.merge(latest_dates, on="ticker")
        df = df[df["date"] == df["max_date"]].drop(columns=["max_date"])
        return df

    # ---- ORM fallback ----
    logger.debug("_fetch_segments: 缓存未命中，回退 ORM 查询")
    from stocks.models import USRevenueSegment

    qs = USRevenueSegment.objects.filter(
        ticker__in=tickers,
        date__gte=start_ts.date(),
        date__lte=date_ts.date(),
        segment_type=seg_type,
        revenue__isnull=False,
    ).values_list("ticker", "date", "segment_name", "revenue")

    df = pd.DataFrame(list(qs), columns=["ticker", "date", "segment", "revenue"])
    if df.empty:
        return df

    df["date"] = pd.to_datetime(df["date"])
    df["revenue"] = pd.to_numeric(df["revenue"], errors="coerce")

    # 每只股票取最新 date 的所有 segments
    latest_dates = df.groupby("ticker")["date"].max().reset_index()
    latest_dates.columns = ["ticker", "max_date"]
    df = df.merge(latest_dates, on="ticker")
    df = df[df["date"] == df["max_date"]].drop(columns=["max_date"])
    return df


# ---------------------------------------------------------------------------
# 1. Revenue Concentration (Product HHI)
# ---------------------------------------------------------------------------


@register
class RevConcentration(AlphaSignal):
    """Revenue Concentration — 产品线 Herfindahl 指数。

    经济直觉：
    - 高 HHI = 营收集中在少数产品 → 业务风险集中
    - Hann et al. (2020): 高集中度公司估值折价
    - 反向因子：高集中度 = 利空（但也有 focus premium 的论文）
    - 设 direction=0 让 IC 决定
    """

    name = "REV_CONCENTRATION"
    version = "v1"
    category = "alternative"
    horizon = "quarter"
    expected_icir = 0.06
    status = "staging"
    inherent_direction = 0
    data_deps = ["us_revenue_segment"]
    ic_window_months = 24

    def compute(self, date: str, universe: pd.DataFrame) -> pd.DataFrame:
        tickers = universe["ticker"].tolist()
        if not tickers:
            logger.debug("RevConcentration: 空 universe")
            return pd.DataFrame(columns=["ticker", "factor_value"])

        df = _fetch_segments(date, tickers, "product")
        if df.empty:
            logger.warning(f"RevConcentration({date}): 无 product segment 数据")
            return pd.DataFrame(columns=["ticker", "factor_value"])

        rows = []
        for ticker, grp in df.groupby("ticker", sort=False):
            revs = grp["revenue"].dropna().values
            revs = revs[revs > 0]
            if len(revs) < 2:
                continue  # 只有 1 个 segment 无意义
            hhi = _herfindahl(revs)
            rows.append({"ticker": ticker, "factor_value": hhi})

        if not rows:
            logger.warning(f"RevConcentration({date}): 无有效 ticker")
            return pd.DataFrame(columns=["ticker", "factor_value"])

        out = pd.DataFrame(rows)
        n_out = int(out["factor_value"].notna().sum())
        logger.info(f"RevConcentration({date}): {n_out} / {len(out)} 有值")
        return out


# ---------------------------------------------------------------------------
# 2. Geographic Concentration (Geo HHI)
# ---------------------------------------------------------------------------


@register
class GeoConcentration(AlphaSignal):
    """Geographic Concentration — 地理区域 Herfindahl 指数。

    经济直觉：
    - 高 geo HHI = 营收集中在单一地区 → 地缘/汇率风险集中
    - Denis et al. (2002): 地理多元化有折价（但也降低尾部风险）
    - 设 direction=0
    """

    name = "GEO_CONCENTRATION"
    version = "v1"
    category = "alternative"
    horizon = "quarter"
    expected_icir = 0.04
    status = "staging"
    inherent_direction = 0
    data_deps = ["us_revenue_segment"]
    ic_window_months = 24

    def compute(self, date: str, universe: pd.DataFrame) -> pd.DataFrame:
        tickers = universe["ticker"].tolist()
        if not tickers:
            logger.debug("GeoConcentration: 空 universe")
            return pd.DataFrame(columns=["ticker", "factor_value"])

        df = _fetch_segments(date, tickers, "geographic")
        if df.empty:
            logger.warning(f"GeoConcentration({date}): 无 geographic segment 数据")
            return pd.DataFrame(columns=["ticker", "factor_value"])

        rows = []
        for ticker, grp in df.groupby("ticker", sort=False):
            revs = grp["revenue"].dropna().values
            revs = revs[revs > 0]
            if len(revs) < 2:
                continue
            hhi = _herfindahl(revs)
            rows.append({"ticker": ticker, "factor_value": hhi})

        if not rows:
            logger.warning(f"GeoConcentration({date}): 无有效 ticker")
            return pd.DataFrame(columns=["ticker", "factor_value"])

        out = pd.DataFrame(rows)
        n_out = int(out["factor_value"].notna().sum())
        logger.info(f"GeoConcentration({date}): {n_out} / {len(out)} 有值")
        return out


# ---------------------------------------------------------------------------
# 3. Segment Growth Dispersion
# ---------------------------------------------------------------------------


@register
class SegmentGrowthDisp(AlphaSignal):
    """Segment Growth Dispersion — 各产品线 YoY 增速的标准差。

    经济直觉：
    - 高分散 = 部分业务线强、部分弱 → 结构性转型中
    - 可能是好信号（新增长点）也可能是坏信号（核心业务衰退）
    - 设 direction=0
    """

    name = "SEGMENT_GROWTH_DISP"
    version = "v1"
    category = "alternative"
    horizon = "quarter"
    expected_icir = 0.05
    status = "staging"
    inherent_direction = 0
    data_deps = ["us_revenue_segment"]
    ic_window_months = 24

    def compute(self, date: str, universe: pd.DataFrame) -> pd.DataFrame:
        tickers = universe["ticker"].tolist()
        if not tickers:
            logger.debug("SegmentGrowthDisp: 空 universe")
            return pd.DataFrame(columns=["ticker", "factor_value"])

        date_ts = pd.Timestamp(date)
        start_ts = date_ts - pd.DateOffset(years=3)

        # ---- 优先从预加载缓存获取 ----
        from stocks.services.factors.us_base import USFactorBase
        bulk = USFactorBase._static_cache.get("_bulk_revenue_segment")
        if bulk is not None and not bulk.empty:
            mask = (
                (bulk["ticker"].isin(tickers))
                & (bulk["date"] >= start_ts)
                & (bulk["date"] <= date_ts)
                & (bulk["segment_type"] == "product")
                & (bulk["revenue"].notna())
            )
            df = bulk[mask][["ticker", "date", "segment", "revenue"]].copy()
        else:
            # ---- ORM fallback ----
            logger.debug("SegmentGrowthDisp: 缓存未命中，回退 ORM 查询")
            from stocks.models import USRevenueSegment

            qs = USRevenueSegment.objects.filter(
                ticker__in=tickers,
                date__gte=start_ts.date(),
                date__lte=date_ts.date(),
                segment_type="product",
                revenue__isnull=False,
            ).values_list("ticker", "date", "segment_name", "revenue")

            df = pd.DataFrame(list(qs), columns=["ticker", "date", "segment", "revenue"])
            if not df.empty:
                df["date"] = pd.to_datetime(df["date"])
                df["revenue"] = pd.to_numeric(df["revenue"], errors="coerce")

        if df.empty:
            logger.warning(f"SegmentGrowthDisp({date}): 无数据")
            return pd.DataFrame(columns=["ticker", "factor_value"])

        rows = []
        for ticker, grp in df.groupby("ticker", sort=False):
            # 找最新两个 date（YoY 对比）
            dates_sorted = sorted(grp["date"].unique(), reverse=True)
            if len(dates_sorted) < 2:
                continue

            now_date = dates_sorted[0]
            # 找 ~1 年前的 date（距离 now 300-400 天）
            yoy_date = None
            for d in dates_sorted[1:]:
                gap = (now_date - d).days
                if 270 < gap < 450:
                    yoy_date = d
                    break
            if yoy_date is None:
                continue

            now_segs = grp[grp["date"] == now_date].set_index("segment")["revenue"]
            yoy_segs = grp[grp["date"] == yoy_date].set_index("segment")["revenue"]

            # 只取两期都有的 segment
            common = now_segs.index.intersection(yoy_segs.index)
            if len(common) < 2:
                continue

            # 各 segment 增速
            growths = []
            for seg in common:
                prev = yoy_segs[seg]
                if pd.isna(prev) or abs(prev) < 1e-6:
                    continue
                g = (now_segs[seg] - prev) / abs(prev)
                growths.append(g)

            if len(growths) < 2:
                continue

            disp = float(np.std(growths, ddof=1))
            rows.append({"ticker": ticker, "factor_value": disp})

        if not rows:
            logger.warning(f"SegmentGrowthDisp({date}): 无有效 ticker")
            return pd.DataFrame(columns=["ticker", "factor_value"])

        out = pd.DataFrame(rows)
        n_out = int(out["factor_value"].notna().sum())
        logger.info(f"SegmentGrowthDisp({date}): {n_out} / {len(out)} 有值")
        return out
