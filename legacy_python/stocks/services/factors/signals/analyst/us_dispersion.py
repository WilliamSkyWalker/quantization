"""Analyst Dispersion 因子 (Diether, Malloy, Scherbina 2002, JF)

定义：
    DISPERSION = (EPS_high - EPS_low) / |EPS_avg|

    用最近可见的 USEpsEstimate（向前看的估计，report_date 近似为 filing date）。

经济直觉：
    - 高分歧 = 分析师意见不一 = 高不确定性
    - Miller (1977): 卖空约束下，乐观派定价 → 高分歧股票被高估
    - 反向因子：高 DISPERSION = 利空

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
class AnalystDispersion(AlphaSignal):
    """Analyst Dispersion — EPS 预测分歧度（反向）。"""

    name = "ANALYST_DISPERSION"
    version = "v1"
    category = "analyst"
    horizon = "quarter"
    expected_icir = 0.10
    status = "staging"
    inherent_direction = -1
    data_deps = ["us_eps_estimate"]
    ic_window_months = 18

    def compute(self, date: str, universe: pd.DataFrame) -> pd.DataFrame:
        tickers = universe["ticker"].tolist()
        if not tickers:
            logger.debug("AnalystDispersion: 空 universe")
            return pd.DataFrame(columns=["ticker", "factor_value"])

        date_ts = pd.Timestamp(date)
        end_ts = date_ts + pd.DateOffset(years=1)

        # 优先走缓存
        cache = self._static_cache.get("_bulk_eps_estimate")
        if cache is not None and not cache.empty:
            mask = (
                cache["ticker"].isin(tickers)
                & (cache["date"] >= date_ts)
                & (cache["date"] <= end_ts)
                & cache["estimated_eps_avg"].notna()
                & cache["estimated_eps_high"].notna()
                & cache["estimated_eps_low"].notna()
                & (cache["number_analysts_estimated_eps"] >= 2)
            )
            df = cache[mask][["ticker", "date", "estimated_eps_avg", "estimated_eps_high", "estimated_eps_low"]].copy()
            df.columns = ["ticker", "date", "eps_avg", "eps_high", "eps_low"]
        else:
            # ORM fallback
            from stocks.models import USEpsEstimate

            qs = USEpsEstimate.objects.filter(
                ticker__in=tickers,
                date__gte=date_ts.date(),
                date__lte=end_ts.date(),
                estimated_eps_avg__isnull=False,
                estimated_eps_high__isnull=False,
                estimated_eps_low__isnull=False,
                number_analysts_estimated_eps__gte=2,
            ).values_list(
                "ticker", "date",
                "estimated_eps_avg", "estimated_eps_high", "estimated_eps_low",
            )
            df = pd.DataFrame(list(qs), columns=[
                "ticker", "date", "eps_avg", "eps_high", "eps_low",
            ])
            if not df.empty:
                df["date"] = pd.to_datetime(df["date"])

        if df.empty:
            logger.warning(f"AnalystDispersion({date}): 无 EPS 估计数据")
            return pd.DataFrame(columns=["ticker", "factor_value"])

        # 每只股票取最近一期的 estimate（最近的 forward period）
        df = df.sort_values(["ticker", "date"])
        df = df.drop_duplicates(subset=["ticker"], keep="first")

        for c in ["eps_avg", "eps_high", "eps_low"]:
            df[c] = pd.to_numeric(df[c], errors="coerce")

        abs_avg = df["eps_avg"].abs().replace(0, np.nan)
        dispersion = (df["eps_high"] - df["eps_low"]) / abs_avg

        out = pd.DataFrame({"ticker": df["ticker"].values, "factor_value": dispersion.values})
        n_out = int(out["factor_value"].notna().sum())
        logger.info(f"AnalystDispersion({date}): {n_out} / {len(out)} 有值")
        return out[["ticker", "factor_value"]]
