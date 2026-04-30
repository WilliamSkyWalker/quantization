"""Days Since Earnings 因子 (PEAD 窗口标记)

定义：
    DSE = 距上次财报发布的日历天数

经济直觉：
    - PEAD (Post-Earnings Announcement Drift): 财报发布后 ~60 天内股价持续漂移
    - 低 DSE = 刚发完财报 → PEAD 效应最强（如果 surprise > 0 则涨势延续）
    - 本因子不区分 surprise 方向，仅标记"距财报远近"
    - 配合 SUE_PEAD 使用效果更好

    inherent_direction = -1（天数越少 = PEAD 窗口内 = 利好）
    但实际方向取决于和 SUE 的交互，设 0 让 IC 决定。

因子方向：0（由滚动 IC 决定）
"""

import logging

import pandas as pd

from services.config import LOG_LEVEL
from stocks.services.factors.us_registry import AlphaSignal, register

logger = logging.getLogger(__name__)
logger.setLevel(LOG_LEVEL)


@register
class DaysSinceEarnings(AlphaSignal):
    """Days Since Earnings — 距上次财报发布天数。"""

    name = "DAYS_SINCE_EARNINGS"
    version = "v1"
    category = "analyst"
    horizon = "month"
    expected_icir = 0.05
    status = "staging"
    inherent_direction = 0
    data_deps = ["us_earnings_surprise"]
    ic_window_months = 12

    _MAX_LOOKBACK_DAYS = 200  # 最多回看 200 天

    def compute(self, date: str, universe: pd.DataFrame) -> pd.DataFrame:
        tickers = universe["ticker"].tolist()
        if not tickers:
            logger.debug("DaysSinceEarnings: 空 universe")
            return pd.DataFrame(columns=["ticker", "factor_value"])

        date_ts = pd.Timestamp(date)
        start_ts = date_ts - pd.Timedelta(days=self._MAX_LOOKBACK_DAYS)

        # 优先走缓存
        cache = self._static_cache.get("_bulk_earnings_surprise")
        if cache is not None and not cache.empty:
            mask = (
                cache["ticker"].isin(tickers)
                & (cache["date"] >= start_ts)
                & (cache["date"] <= date_ts)
            )
            df = cache[mask][["ticker", "date"]].copy()
        else:
            # ORM fallback
            from stocks.models import USEarningsSurprise

            qs = USEarningsSurprise.objects.filter(
                ticker__in=tickers,
                date__gte=start_ts.date(),
                date__lte=date_ts.date(),
            ).values_list("ticker", "date")
            df = pd.DataFrame(list(qs), columns=["ticker", "date"])
            if not df.empty:
                df["date"] = pd.to_datetime(df["date"])

        if df.empty:
            logger.warning(f"DaysSinceEarnings({date}): 无财报数据")
            return pd.DataFrame(columns=["ticker", "factor_value"])
        # 每只股票取最近一次财报
        df = df.sort_values(["ticker", "date"], ascending=[True, False])
        df = df.drop_duplicates(subset=["ticker"], keep="first")

        df["days"] = (date_ts - df["date"]).dt.days

        out = pd.DataFrame({
            "ticker": df["ticker"].values,
            "factor_value": df["days"].values.astype(float),
        })
        n_out = int(out["factor_value"].notna().sum())
        logger.info(f"DaysSinceEarnings({date}): {n_out} / {len(out)} 有值")
        return out[["ticker", "factor_value"]]
