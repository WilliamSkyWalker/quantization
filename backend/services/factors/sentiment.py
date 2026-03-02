"""
舆情政策因子（POLICY_SENT, POLICY_INTENSITY）

将政策文章分析结果转化为量化因子：
    - 分析结果按行业聚合（时间衰减加权）
    - 通过行业分类映射到个股
    - 无行业匹配的股票 → NaN

设计模式复用宏观因子：外部信号 → 行业映射 → 个股因子值。
"""

import logging

import numpy as np
import pandas as pd

from backend.services.config import LOG_LEVEL
from backend.services.factors.base import FactorBase
from backend.services.sentiment.analyzer import SentimentAnalyzer

logger = logging.getLogger(__name__)
logger.setLevel(LOG_LEVEL)


class SentimentPolicyFactor(FactorBase):
    """
    政策情感因子 (POLICY_SENT)

    因子值 = 该行业的加权情感分 × 强度。
    正值利好，负值利空。
    """

    name = "POLICY_SENT"
    description = "政策舆情情感因子，行业级政策利好/利空信号"

    def __init__(self, db):
        super().__init__(db)
        self._analyzer = SentimentAnalyzer(db)

    def compute(self, date: str, universe: pd.DataFrame) -> pd.DataFrame:
        codes = universe["ts_code"].tolist()
        result = pd.DataFrame({"ts_code": codes, "factor_value": np.nan})

        # 1. 获取行业映射
        industry_df = self.db.get_industry_map()
        if industry_df.empty:
            logger.warning("POLICY_SENT: 无行业分类数据")
            return result

        # 2. 获取行业级情感得分
        daily_score = self._analyzer.get_daily_score(date)
        if daily_score.empty:
            logger.debug("POLICY_SENT: 无舆情分析数据")
            return result

        # 构建行业→得分映射
        score_map = {}
        for _, row in daily_score.iterrows():
            score_map[row["industry_name"]] = {
                "sentiment": row["sentiment"],
                "intensity": row["intensity"],
            }

        # 3. 映射到个股
        stock_industry = industry_df[
            industry_df["ts_code"].isin(codes)
        ][["ts_code", "industry_name"]].copy()

        if stock_industry.empty:
            return result

        def _map(row):
            ind = row.get("industry_name")
            if pd.notna(ind) and ind in score_map:
                s = score_map[ind]
                return s["sentiment"] * s["intensity"]
            return np.nan

        stock_industry["factor_value"] = stock_industry.apply(_map, axis=1)

        result = result[["ts_code"]].merge(
            stock_industry[["ts_code", "factor_value"]],
            on="ts_code",
            how="left",
        )
        result["factor_value"] = result["factor_value"].astype(float)

        valid = result["factor_value"].notna().sum()
        logger.debug(f"POLICY_SENT: {valid}/{len(result)} 只有效值")
        return result[["ts_code", "factor_value"]]


class SentimentIntensityFactor(FactorBase):
    """
    政策关注度因子 (POLICY_INTENSITY)

    因子值 = 行业强度得分（不考虑正负方向，只看关注度）。
    高关注度行业可能有更大波动，无论利好利空。
    """

    name = "POLICY_INTENSITY"
    description = "政策关注度因子，行业政策关注热度信号"

    def __init__(self, db):
        super().__init__(db)
        self._analyzer = SentimentAnalyzer(db)

    def compute(self, date: str, universe: pd.DataFrame) -> pd.DataFrame:
        codes = universe["ts_code"].tolist()
        result = pd.DataFrame({"ts_code": codes, "factor_value": np.nan})

        # 1. 获取行业映射
        industry_df = self.db.get_industry_map()
        if industry_df.empty:
            logger.warning("POLICY_INTENSITY: 无行业分类数据")
            return result

        # 2. 获取行业级情感得分
        daily_score = self._analyzer.get_daily_score(date)
        if daily_score.empty:
            logger.debug("POLICY_INTENSITY: 无舆情分析数据")
            return result

        # 构建行业→强度映射
        intensity_map = {}
        for _, row in daily_score.iterrows():
            intensity_map[row["industry_name"]] = row["intensity"]

        # 3. 映射到个股
        stock_industry = industry_df[
            industry_df["ts_code"].isin(codes)
        ][["ts_code", "industry_name"]].copy()

        if stock_industry.empty:
            return result

        def _map(row):
            ind = row.get("industry_name")
            if pd.notna(ind) and ind in intensity_map:
                return intensity_map[ind]
            return np.nan

        stock_industry["factor_value"] = stock_industry.apply(_map, axis=1)

        result = result[["ts_code"]].merge(
            stock_industry[["ts_code", "factor_value"]],
            on="ts_code",
            how="left",
        )
        result["factor_value"] = result["factor_value"].astype(float)

        valid = result["factor_value"].notna().sum()
        logger.debug(f"POLICY_INTENSITY: {valid}/{len(result)} 只有效值")
        return result[["ts_code", "factor_value"]]
