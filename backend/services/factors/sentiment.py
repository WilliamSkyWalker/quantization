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

    因子值 = 情感分 × 强度。
    优先使用 LLM 识别的个股级信号，无个股信号时回退到行业映射。
    正值利好，负值利空。
    """

    name = "POLICY_SENT"
    description = "政策舆情情感因子，个股/行业级政策利好/利空信号"

    def __init__(self, db):
        super().__init__(db)
        self._analyzer = SentimentAnalyzer(db)

    def compute(self, date: str, universe: pd.DataFrame) -> pd.DataFrame:
        codes = universe["ts_code"].tolist()
        result = pd.DataFrame({"ts_code": codes, "factor_value": np.nan})

        # 1. 个股级信号（LLM affected_stocks）
        stock_score = self._analyzer.get_daily_stock_score(date)
        stock_direct = {}
        if not stock_score.empty:
            for _, row in stock_score.iterrows():
                stock_direct[row["ts_code"]] = row["sentiment"] * row["intensity"]
            logger.debug(f"POLICY_SENT: {len(stock_direct)} 只个股有直接信号")

        # 2. 行业级信号（回退）
        industry_df = self.get_industry_map_cached()
        daily_score = self._analyzer.get_daily_score(date)
        score_map = {}
        if not daily_score.empty:
            for _, row in daily_score.iterrows():
                score_map[row["industry_name"]] = {
                    "sentiment": row["sentiment"],
                    "intensity": row["intensity"],
                }

        stock_industry = pd.DataFrame()
        if not industry_df.empty:
            stock_industry = industry_df[
                industry_df["ts_code"].isin(codes)
            ][["ts_code", "industry_name"]].copy()

        # 3. 合并：个股信号优先，无则用行业映射
        def _map(ts_code):
            if ts_code in stock_direct:
                return stock_direct[ts_code]
            if not stock_industry.empty:
                ind_rows = stock_industry[stock_industry["ts_code"] == ts_code]
                if not ind_rows.empty:
                    ind = ind_rows.iloc[0]["industry_name"]
                    if pd.notna(ind) and ind in score_map:
                        s = score_map[ind]
                        return s["sentiment"] * s["intensity"]
            return np.nan

        result["factor_value"] = result["ts_code"].apply(_map)
        result["factor_value"] = result["factor_value"].astype(float)

        valid = result["factor_value"].notna().sum()
        logger.debug(f"POLICY_SENT: {valid}/{len(result)} 只有效值 (直接={len(stock_direct)})")
        return result[["ts_code", "factor_value"]]


class SentimentIntensityFactor(FactorBase):
    """
    政策关注度因子 (POLICY_INTENSITY)

    因子值 = 强度得分（不考虑正负方向，只看关注度）。
    优先使用 LLM 识别的个股级信号，无个股信号时回退到行业映射。
    """

    name = "POLICY_INTENSITY"
    description = "政策关注度因子，个股/行业政策关注热度信号"

    def __init__(self, db):
        super().__init__(db)
        self._analyzer = SentimentAnalyzer(db)

    def compute(self, date: str, universe: pd.DataFrame) -> pd.DataFrame:
        codes = universe["ts_code"].tolist()
        result = pd.DataFrame({"ts_code": codes, "factor_value": np.nan})

        # 1. 个股级信号（LLM affected_stocks）
        stock_score = self._analyzer.get_daily_stock_score(date)
        stock_direct = {}
        if not stock_score.empty:
            for _, row in stock_score.iterrows():
                stock_direct[row["ts_code"]] = row["intensity"]
            logger.debug(f"POLICY_INTENSITY: {len(stock_direct)} 只个股有直接信号")

        # 2. 行业级信号（回退）
        industry_df = self.get_industry_map_cached()
        daily_score = self._analyzer.get_daily_score(date)
        intensity_map = {}
        if not daily_score.empty:
            for _, row in daily_score.iterrows():
                intensity_map[row["industry_name"]] = row["intensity"]

        stock_industry = pd.DataFrame()
        if not industry_df.empty:
            stock_industry = industry_df[
                industry_df["ts_code"].isin(codes)
            ][["ts_code", "industry_name"]].copy()

        # 3. 合并：个股信号优先，无则用行业映射
        def _map(ts_code):
            if ts_code in stock_direct:
                return stock_direct[ts_code]
            if not stock_industry.empty:
                ind_rows = stock_industry[stock_industry["ts_code"] == ts_code]
                if not ind_rows.empty:
                    ind = ind_rows.iloc[0]["industry_name"]
                    if pd.notna(ind) and ind in intensity_map:
                        return intensity_map[ind]
            return np.nan

        result["factor_value"] = result["ts_code"].apply(_map)
        result["factor_value"] = result["factor_value"].astype(float)

        valid = result["factor_value"].notna().sum()
        logger.debug(f"POLICY_INTENSITY: {valid}/{len(result)} 只有效值 (直接={len(stock_direct)})")
        return result[["ts_code", "factor_value"]]
