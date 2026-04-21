"""Recommendation Change 因子

定义：
    过去 90 天内所有分析师评级变化的净得分。

    grade → 数值映射：
        Strong Buy / Buy / Outperform / Overweight / Market Outperform → 5/4/4/4/4
        Neutral / Equal Weight / Hold / Market Perform / Sector Perform / In Line / Perform → 3
        Underweight / Underperform → 2
        Sell / Strong Sell → 1

    ΔSCORE = new_grade_score - previous_grade_score（每条记录）
    REC_CHANGE = Σ ΔSCORE（90 天内所有变更）/ count

经济直觉：
    - 评级变化 > 评级水平（Womack 1996）
    - 批量升级 → 基本面改善信号 → 正向
    - 批量降级 → 利空

因子方向：+1（净升级 = 利好）
"""

import logging

import numpy as np
import pandas as pd

from services.config import LOG_LEVEL
from stocks.services.factors.us_registry import AlphaSignal, register

logger = logging.getLogger(__name__)
logger.setLevel(LOG_LEVEL)

# 评级 → 数值映射（不区分大小写）
_GRADE_MAP = {
    "strong buy": 5,
    "buy": 4,
    "outperform": 4,
    "overweight": 4,
    "market outperform": 4,
    "long-term buy": 4,
    "positive": 4,
    "sector outperform": 4,
    "top pick": 5,
    "neutral": 3,
    "equal weight": 3,
    "equal-weight": 3,
    "hold": 3,
    "market perform": 3,
    "sector perform": 3,
    "in line": 3,
    "perform": 3,
    "peer perform": 3,
    "sector weight": 3,
    "mixed": 3,
    "underweight": 2,
    "underperform": 2,
    "reduce": 2,
    "negative": 2,
    "sector underperform": 2,
    "market underperform": 2,
    "sell": 1,
    "strong sell": 1,
}


def _grade_to_score(grade: str | None) -> float:
    """评级文本 → 数值。无法识别返回 NaN。"""
    if not grade:
        return np.nan
    return _GRADE_MAP.get(grade.strip().lower(), np.nan)


@register
class RecommendationChange(AlphaSignal):
    """Recommendation Change — 近 90 天分析师评级净变化。"""

    name = "REC_CHANGE"
    version = "v1"
    category = "analyst"
    horizon = "month"
    expected_icir = 0.12
    status = "staging"
    inherent_direction = +1
    data_deps = ["us_analyst_recommendation"]
    ic_window_months = 12

    _WINDOW_DAYS = 90

    def compute(self, date: str, universe: pd.DataFrame) -> pd.DataFrame:
        tickers = universe["ticker"].tolist()
        if not tickers:
            logger.debug("RecChange: 空 universe")
            return pd.DataFrame(columns=["ticker", "factor_value"])

        date_ts = pd.Timestamp(date)
        start_ts = date_ts - pd.Timedelta(days=self._WINDOW_DAYS)

        # 优先走缓存
        cache = self._static_cache.get("_bulk_analyst")
        if cache is not None and not cache.empty:
            mask = (
                cache["ticker"].isin(tickers)
                & (cache["date"] >= start_ts)
                & (cache["date"] <= date_ts)
            )
            df = cache[mask][["ticker", "date", "grading_company", "new_grade"]].copy()
            # 缓存无 previous_grade，从同一分析师的上一条记录推导
            df = df.sort_values(["ticker", "grading_company", "date"])
            df["previous_grade"] = df.groupby(
                ["ticker", "grading_company"]
            )["new_grade"].shift(1)
            df = df[["ticker", "date", "previous_grade", "new_grade"]]
        else:
            # ORM fallback
            from stocks.models import USAnalystRecommendation

            qs = USAnalystRecommendation.objects.filter(
                ticker__in=tickers,
                date__gte=start_ts.date(),
                date__lte=date_ts.date(),
            ).values_list("ticker", "date", "previous_grade", "new_grade")
            df = pd.DataFrame(list(qs), columns=["ticker", "date", "previous_grade", "new_grade"])
        if df.empty:
            logger.warning(f"RecChange({date}): 无推荐数据")
            return pd.DataFrame(columns=["ticker", "factor_value"])

        df["prev_score"] = df["previous_grade"].apply(_grade_to_score)
        df["new_score"] = df["new_grade"].apply(_grade_to_score)
        df["delta"] = df["new_score"] - df["prev_score"]

        # 只保留 prev 和 new 都可解析的记录
        valid = df.dropna(subset=["delta"])
        if valid.empty:
            logger.warning(f"RecChange({date}): 无有效评级变化")
            return pd.DataFrame(columns=["ticker", "factor_value"])

        # 每只股票：平均 delta
        agg = valid.groupby("ticker")["delta"].mean().reset_index()
        agg.columns = ["ticker", "factor_value"]

        # 补齐 universe 中有推荐记录但无变化的 ticker（delta=0）
        # 无任何记录的 ticker 不出值
        out = agg[["ticker", "factor_value"]].copy()
        n_out = int(out["factor_value"].notna().sum())
        logger.info(f"RecChange({date}): {n_out} / {len(out)} 有值")
        return out
