"""Dark Pool Short Indicator 因子

定义：
    DPI_20D = 20 日均 DPI (Dark Pool Indicator = otc_short / otc_total)

    数据源：Quiver Quant off-exchange 数据（USDarkPoolVolume）

经济直觉：
    - 高 DPI = 暗池中做空比例高 → 机构偷偷做空
    - 暗池做空信息不透明，散户无法及时反应 → 信息不对称
    - 反向因子：高 DPI = 利空

    注意：DPI 天然在 [0, 1] 范围内。20 日均线平滑日间噪音。

因子方向：-1
"""

import logging

import numpy as np
import pandas as pd

from services.config import LOG_LEVEL
from stocks.services.factors.us_registry import AlphaSignal, register

logger = logging.getLogger(__name__)
logger.setLevel(LOG_LEVEL)


@register
class DarkPoolShort(AlphaSignal):
    """DPI_20D — 暗池做空指标 20 日均值（反向）。"""

    name = "DARK_POOL_SHORT"
    version = "v1"
    category = "ownership"
    horizon = "month"
    expected_icir = 0.08
    status = "staging"
    inherent_direction = -1
    data_deps = ["us_dark_pool_volume"]
    ic_window_months = 12

    _LOOKBACK_DAYS = 35  # 取 ~20 交易日
    _MIN_DAYS = 10

    def compute(self, date: str, universe: pd.DataFrame) -> pd.DataFrame:
        tickers = universe["ticker"].tolist()
        if not tickers:
            logger.debug("DarkPoolShort: 空 universe")
            return pd.DataFrame(columns=["ticker", "factor_value"])

        date_ts = pd.Timestamp(date)
        start_ts = date_ts - pd.Timedelta(days=self._LOOKBACK_DAYS)

        # ---- 优先从预加载缓存获取 ----
        bulk = self._static_cache.get("_bulk_dark_pool")
        if bulk is not None and not bulk.empty:
            mask = (
                bulk["ticker"].isin(tickers)
                & (bulk["date"] >= start_ts)
                & (bulk["date"] <= date_ts)
            )
            df = bulk.loc[mask, ["ticker", "dpi"]].copy()
            if df.empty:
                logger.warning(f"DarkPoolShort({date}): 缓存中无暗池数据")
                return pd.DataFrame(columns=["ticker", "factor_value"])
            df["dpi"] = pd.to_numeric(df["dpi"], errors="coerce")
            return self._agg_dpi(df, date)

        # ---- fallback ORM ----
        logger.debug(f"DarkPoolShort({date}): 缓存为空，fallback ORM")
        from stocks.models import USDarkPoolVolume

        start = start_ts.date()
        qs = USDarkPoolVolume.objects.filter(
            ticker__in=tickers,
            date__gte=start,
            date__lte=date_ts.date(),
            dpi__isnull=False,
        ).values_list("ticker", "date", "dpi")

        df = pd.DataFrame(list(qs), columns=["ticker", "date", "dpi"])
        if df.empty:
            logger.warning(f"DarkPoolShort({date}): ORM 无暗池数据")
            return pd.DataFrame(columns=["ticker", "factor_value"])
        df["dpi"] = pd.to_numeric(df["dpi"], errors="coerce")
        return self._agg_dpi(df, date)

    def _agg_dpi(self, df: pd.DataFrame, date: str) -> pd.DataFrame:
        """汇总 DPI：每只股票取 20 日均 DPI。"""
        agg = df.groupby("ticker").agg(
            cnt=("dpi", "count"),
            mean_dpi=("dpi", "mean"),
        ).reset_index()

        agg = agg[agg["cnt"] >= self._MIN_DAYS]
        if agg.empty:
            logger.warning(f"DarkPoolShort({date}): 无足够天数的 ticker")
            return pd.DataFrame(columns=["ticker", "factor_value"])

        out = pd.DataFrame({
            "ticker": agg["ticker"].values,
            "factor_value": agg["mean_dpi"].values,
        })
        n_out = int(out["factor_value"].notna().sum())
        logger.info(f"DarkPoolShort({date}): {n_out} / {len(out)} 有值")
        return out[["ticker", "factor_value"]]
