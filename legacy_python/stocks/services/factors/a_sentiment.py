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

from services.config import LOG_LEVEL
from stocks.services.factors.a_base import FactorBase
from sentiment.services.scrapers.analyzer import SentimentAnalyzer

logger = logging.getLogger(__name__)
logger.setLevel(LOG_LEVEL)


def _get_sentiment_data(db, date: str, date_cache: dict) -> tuple:
    """
    获取舆情数据（带缓存）。

    POLICY_SENT 和 POLICY_INTENSITY 共享同一份数据，
    通过 FactorBase._date_cache 缓存避免重复 DB 查询。

    Returns:
        (daily_score_df, stock_score_df)
    """
    cache_key = f"_sentiment_{date}"
    if cache_key in date_cache:
        return date_cache[cache_key]

    analyzer = SentimentAnalyzer(db)
    daily_score = analyzer.get_daily_score(date)
    stock_score = analyzer.get_daily_stock_score(date)
    date_cache[cache_key] = (daily_score, stock_score)
    return daily_score, stock_score


class SentimentPolicyFactor(FactorBase):
    """
    政策情感因子 (POLICY_SENT)

    因子值 = 情感分 × 强度。
    优先使用 LLM 识别的个股级信号，无个股信号时回退到行业映射。
    正值利好，负值利空。
    """

    name = "POLICY_SENT"
    description = "政策舆情情感因子，个股/行业级政策利好/利空信号"

    def compute(self, date: str, universe: pd.DataFrame) -> pd.DataFrame:
        codes = universe["ts_code"].tolist()
        result = pd.DataFrame({"ts_code": codes, "factor_value": np.nan})

        daily_score, stock_score = _get_sentiment_data(self.db, date, self._date_cache)

        # 1. 个股级信号（LLM affected_stocks）
        stock_direct = {}
        if not stock_score.empty:
            for _, row in stock_score.iterrows():
                stock_direct[row["ts_code"]] = row["sentiment"] * row["intensity"]
            logger.debug(f"POLICY_SENT: {len(stock_direct)} 只个股有直接信号")

        # 2. 行业级信号（回退）— 构建 industry→score 和 ts_code→industry 字典
        industry_df = self.get_industry_map_cached()
        ind_score_map = {}  # industry_name → sent * intensity
        if not daily_score.empty:
            for _, row in daily_score.iterrows():
                ind_score_map[row["industry_name"]] = row["sentiment"] * row["intensity"]

        code_to_ind = {}  # ts_code → industry_name
        if not industry_df.empty:
            mask = industry_df["ts_code"].isin(codes)
            for _, row in industry_df[mask][["ts_code", "industry_name"]].iterrows():
                code_to_ind[row["ts_code"]] = row["industry_name"]

        # 3. 合并：个股信号优先，无则用行业映射（纯 dict 查找，O(1)）
        values = []
        for code in codes:
            if code in stock_direct:
                values.append(stock_direct[code])
            else:
                ind = code_to_ind.get(code)
                if ind and ind in ind_score_map:
                    values.append(ind_score_map[ind])
                else:
                    values.append(np.nan)

        result["factor_value"] = values
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

    def compute(self, date: str, universe: pd.DataFrame) -> pd.DataFrame:
        codes = universe["ts_code"].tolist()
        result = pd.DataFrame({"ts_code": codes, "factor_value": np.nan})

        daily_score, stock_score = _get_sentiment_data(self.db, date, self._date_cache)

        # 1. 个股级信号（LLM affected_stocks）
        stock_direct = {}
        if not stock_score.empty:
            for _, row in stock_score.iterrows():
                stock_direct[row["ts_code"]] = row["intensity"]
            logger.debug(f"POLICY_INTENSITY: {len(stock_direct)} 只个股有直接信号")

        # 2. 行业级信号（回退）— 构建字典
        industry_df = self.get_industry_map_cached()
        ind_intensity_map = {}
        if not daily_score.empty:
            for _, row in daily_score.iterrows():
                ind_intensity_map[row["industry_name"]] = row["intensity"]

        code_to_ind = {}
        if not industry_df.empty:
            mask = industry_df["ts_code"].isin(codes)
            for _, row in industry_df[mask][["ts_code", "industry_name"]].iterrows():
                code_to_ind[row["ts_code"]] = row["industry_name"]

        # 3. 合并：个股信号优先，无则用行业映射（纯 dict 查找，O(1)）
        values = []
        for code in codes:
            if code in stock_direct:
                values.append(stock_direct[code])
            else:
                ind = code_to_ind.get(code)
                if ind and ind in ind_intensity_map:
                    values.append(ind_intensity_map[ind])
                else:
                    values.append(np.nan)

        result["factor_value"] = values
        result["factor_value"] = result["factor_value"].astype(float)

        valid = result["factor_value"].notna().sum()
        logger.debug(f"POLICY_INTENSITY: {valid}/{len(result)} 只有效值 (直接={len(stock_direct)})")
        return result[["ts_code", "factor_value"]]
