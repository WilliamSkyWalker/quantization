"""ESG Risk Rating 因子

定义：
    ESG_RISK = esg_risk_rating 映射为数值

    FMP USESGRating 的 esg_risk_rating 是字母评级。
    映射：A+ → 10, A → 9, A- → 8, B+ → 7, B → 6, B- → 5,
          C+ → 4, C → 3, C- → 2, D → 1

经济直觉：
    - 高 ESG = 低环境/社会/治理风险
    - 学术争议：ESG alpha 可能来自 quality 因子的包装
    - Hong-Kacperczyk (2009): "sin stocks" 有溢价（低 ESG 反而高回报）
    - 设 direction=0 让 IC 决定

因子方向：0
"""

import logging

import numpy as np
import pandas as pd

from services.config import LOG_LEVEL
from stocks.services.factors.us_registry import AlphaSignal, register

logger = logging.getLogger(__name__)
logger.setLevel(LOG_LEVEL)

_ESG_MAP = {
    "A+": 10, "A": 9, "A-": 8,
    "B+": 7, "B": 6, "B-": 5,
    "C+": 4, "C": 3, "C-": 2,
    "D+": 1.5, "D": 1, "D-": 0.5,
}


@register
class EsgRisk(AlphaSignal):
    """ESG Risk Rating — ESG 风险评级数值化。"""

    name = "ESG_RISK"
    version = "v1"
    category = "alternative"
    horizon = "quarter"
    expected_icir = 0.04
    status = "staging"
    inherent_direction = 0
    data_deps = ["us_esg_rating"]
    ic_window_months = 30

    def compute(self, date: str, universe: pd.DataFrame) -> pd.DataFrame:
        tickers = universe["ticker"].tolist()
        if not tickers:
            logger.debug("EsgRisk: 空 universe")
            return pd.DataFrame(columns=["ticker", "factor_value"])

        from stocks.models import USESGRating

        date_ts = pd.Timestamp(date)
        # 取截面年份及之前的最新评级
        max_fy = date_ts.year

        qs = USESGRating.objects.filter(
            ticker__in=tickers,
            fiscal_year__lte=max_fy,
            esg_risk_rating__isnull=False,
        ).values_list("ticker", "fiscal_year", "esg_risk_rating")

        df = pd.DataFrame(list(qs), columns=["ticker", "fy", "rating"])
        if df.empty:
            logger.warning(f"EsgRisk({date}): 无 ESG 数据")
            return pd.DataFrame(columns=["ticker", "factor_value"])

        # 每只股票取最新年份
        df = df.sort_values(["ticker", "fy"], ascending=[True, False])
        df = df.drop_duplicates(subset=["ticker"], keep="first")

        df["factor_value"] = df["rating"].str.strip().map(_ESG_MAP)

        out = df[["ticker", "factor_value"]].copy()
        n_out = int(out["factor_value"].notna().sum())
        logger.info(f"EsgRisk({date}): {n_out} / {len(out)} 有值")
        return out[["ticker", "factor_value"]]
