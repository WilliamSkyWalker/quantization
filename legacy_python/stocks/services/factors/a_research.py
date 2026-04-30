"""
券商研报因子（ANALYST_RATING, ANALYST_COVERAGE）

将券商研报评级数据转化为量化因子：
    - ANALYST_RATING: 分析师共识评级（平均评级分数）
    - ANALYST_COVERAGE: 分析师覆盖度（覆盖机构数对数）

直接按 ts_code 匹配，无需行业映射。
"""

import logging

import numpy as np
import pandas as pd

from services.config import LOG_LEVEL, RESEARCH_LOOKBACK_DAYS
from stocks.services.factors.a_base import FactorBase

logger = logging.getLogger(__name__)
logger.setLevel(LOG_LEVEL)


class AnalystRatingFactor(FactorBase):
    """
    分析师共识评级因子 (ANALYST_RATING)

    因子值 = 近 N 天内该股票所有研报的平均 rating_score。
    无研报覆盖 → NaN。
    """

    name = "ANALYST_RATING"
    description = "分析师共识评级，近期研报平均评分"

    def compute(self, date: str, universe: pd.DataFrame) -> pd.DataFrame:
        codes = universe["ts_code"].tolist()
        result = pd.DataFrame({"ts_code": codes, "factor_value": np.nan})

        try:
            stats = self.db.get_research_report_stats(
                end_date=date,
                lookback_days=RESEARCH_LOOKBACK_DAYS,
            )
        except Exception as e:
            logger.debug(f"ANALYST_RATING: 获取研报统计失败: {e}")
            return result

        if stats.empty:
            logger.debug("ANALYST_RATING: 无研报数据")
            return result

        # 只保留在 universe 内的股票
        stats = stats[stats["ts_code"].isin(codes)]

        if stats.empty:
            logger.debug("ANALYST_RATING: 研报数据过滤后无匹配股票，返回空")
            return result

        rating_map = dict(zip(stats["ts_code"], stats["avg_rating"]))
        result["factor_value"] = result["ts_code"].map(rating_map)

        valid = result["factor_value"].notna().sum()
        logger.debug(f"ANALYST_RATING: {valid}/{len(result)} 只有效值")
        return result[["ts_code", "factor_value"]]


class AnalystCoverageFactor(FactorBase):
    """
    分析师覆盖度因子 (ANALYST_COVERAGE)

    因子值 = log(1 + 覆盖机构数)。
    对数变换减轻大票偏向。
    无研报覆盖 → NaN。
    """

    name = "ANALYST_COVERAGE"
    description = "分析师覆盖度，研报覆盖机构数对数"

    def compute(self, date: str, universe: pd.DataFrame) -> pd.DataFrame:
        codes = universe["ts_code"].tolist()
        result = pd.DataFrame({"ts_code": codes, "factor_value": np.nan})

        try:
            stats = self.db.get_research_report_stats(
                end_date=date,
                lookback_days=RESEARCH_LOOKBACK_DAYS,
            )
        except Exception as e:
            logger.debug(f"ANALYST_COVERAGE: 获取研报统计失败: {e}")
            return result

        if stats.empty:
            logger.debug("ANALYST_COVERAGE: 无研报数据")
            return result

        # 只保留在 universe 内的股票
        stats = stats[stats["ts_code"].isin(codes)]

        if stats.empty:
            logger.debug("ANALYST_COVERAGE: 研报数据过滤后无匹配股票，返回空")
            return result

        coverage_map = {
            row["ts_code"]: np.log(1 + row["institution_count"])
            for _, row in stats.iterrows()
        }
        result["factor_value"] = result["ts_code"].map(coverage_map)

        valid = result["factor_value"].notna().sum()
        logger.debug(f"ANALYST_COVERAGE: {valid}/{len(result)} 只有效值")
        return result[["ts_code", "factor_value"]]
