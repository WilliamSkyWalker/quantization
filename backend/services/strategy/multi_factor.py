"""
多因子打分选股模型

实现等权合成多因子得分的选股策略：
    1. 在每个调仓日，对股票池内所有股票计算各因子值
    2. 对每个因子做处理（去极值、中性化、标准化）
    3. 等权合成为综合得分
    4. 选取得分高于阈值的前 N 只股票，等权分配

调仓规则：
    - 频率：月频，每月最后一个交易日
    - 选股范围：可配置（默认全市场可交易股票）
    - 持仓数量：0~30 只（允许空仓和不满仓）
    - 权重分配：等权
    - 最低选股分：得分低于阈值的股票不入选（默认 0，即低于均值不入选）
"""

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional

import pandas as pd
import numpy as np

from backend.services.config import (
    MAX_HOLDINGS,
    MIN_SELECT_SCORE,
    TURNOVER_PENALTY_LAMBDA,
    NEUTRALIZE_MODE,
    NONLINEAR_SIZE,
    MIN_VALID_CATEGORIES,
    CATEGORY_NEUTRALIZE_OVERRIDES,
    STANDARDIZE_MODE,
    WEIGHT_TEMPERATURE,
    REGIME_ENABLED,
    REGIME_BEAR_OVERRIDES,
    REGIME_BULL_OVERRIDES,
    BEAR_HOLDINGS_RATIO,
    SENTIMENT_SURGE_MULTIPLIER,
    SENTIMENT_SURGE_ZSCORE,
    REBALANCE_INTERVAL,
    REBALANCE_DEVIATION_THRESHOLD,
    REBALANCE_CHECK_INTERVAL,
    REBALANCE_MIN_INTERVAL,
    MISSING_FACTOR_THRESHOLD,
    MISSING_FACTOR_MAX_PENALTY,
    LOG_LEVEL,
)
from backend.services.data.database import DatabaseManager
from backend.services.data.cleaner import get_clean_universe, preload_clean_universes
from backend.services.factors.value import EPFactor, BPFactor
from backend.services.factors.dividend import DividendYieldFactor
from backend.services.factors.momentum import MOM1MFactor, MOM3MFactor, MOM12MFactor, ShortReversalFactor, ResidualMomentumFactor
from backend.services.factors.quality import ROEFactor, GrossMarginFactor, ProfitStabilityFactor, MarginTrendFactor
from backend.services.factors.growth import NetProfitYOYFactor, RevenueYOYFactor, NetProfitCAGR3YFactor
from backend.services.factors.technical import (
    Turnover20DFactor,
    VolatilityFactor,
    PriceDeviationFactor,
    SizeFactor,
    IndustryMomentumFactor,
    VolPriceDivFactor,
)
from backend.services.factors.commodity import CommodityMomentumFactor
from backend.services.factors.macro import (
    MacroCycleFactor,
    MacroLiquidityFactor,
    MacroInflationFactor,
    MacroExternalFactor,
)
from backend.services.factors.sentiment import (
    SentimentPolicyFactor,
    SentimentIntensityFactor,
)
from backend.services.factors.research import (
    AnalystRatingFactor,
    AnalystCoverageFactor,
)
from backend.services.factors.base import FactorBase
from backend.services.factors.processor import process_factor, clear_neutralize_cache
from backend.services.strategy.regime import RegimeDetector

logger = logging.getLogger(__name__)
logger.setLevel(LOG_LEVEL)


class MultiFactorStrategy:
    """
    多因子选股策略。

    用法:
        db = DatabaseManager()
        strategy = MultiFactorStrategy(db)
        result = strategy.select_stocks("2024-12-31")
        signals = strategy.generate_signals("2023-01-01", "2024-12-31")
    """

    # ----------------------------------------------------------
    # 因子大类定义（组内加权平均 → 组间固定分母合成）
    # ----------------------------------------------------------
    FACTOR_CATEGORIES = {
        "value":    ["EP", "BP", "DIV_YIELD"],
        "quality":  ["ROE_TTM", "GROSS_MARGIN", "PROFIT_STB", "MARGIN_TREND"],
        "growth":   ["NET_PROFIT_YOY", "REVENUE_YOY", "NET_PROFIT_CAGR_3Y"],
        "momentum": ["MOM_1M", "MOM_3M", "MOM_12M", "REV_5D", "IND_MOM", "RESIDUAL_MOM", "CMDTY_MOM"],
        "technical":["TURN_20D", "VOL_20D", "PRICE_DEV_60D", "SIZE", "VOL_PRICE_DIV"],
        "macro":    ["MACRO_CYCLE", "MACRO_LIQD", "MACRO_INFL", "MACRO_EXTR"],
        "sentiment": ["POLICY_SENT", "POLICY_INTENSITY", "ANALYST_RATING", "ANALYST_COVERAGE"],
    }

    # 大类权重（质量主导 1.3、动量提升 0.9、价值降权 0.7 避免价值陷阱）
    CATEGORY_WEIGHTS = {
        "value": 0.7,
        "quality": 1.3,
        "growth": 1.0,
        "momentum": 0.9,
        "technical": 0.7,
        "macro": 0.6,
        "sentiment": 0.6,
    }

    # 核心财务因子 — 全部缺失则剔除出池
    CORE_FINANCIAL_FACTORS = ["EP", "BP", "ROE_TTM", "GROSS_MARGIN"]

    # 依赖 financial_data 表的因子（受财报时效衰减影响）
    FINANCIAL_DEPENDENT_FACTORS = [
        "EP", "BP", "ROE_TTM", "GROSS_MARGIN", "PROFIT_STB", "MARGIN_TREND",
        "NET_PROFIT_YOY", "REVENUE_YOY", "NET_PROFIT_CAGR_3Y",
    ]

    # 因子→大类反向映射（用于按大类覆盖中性化模式）
    FACTOR_TO_CATEGORY = {f: cat for cat, fs in FACTOR_CATEGORIES.items() for f in fs}

    def __init__(
        self,
        db: DatabaseManager,
        n_holdings: int = MAX_HOLDINGS,
        factor_weights: Optional[dict[str, float]] = None,
        min_select_score: float = MIN_SELECT_SCORE,
        turnover_penalty_lambda: float = TURNOVER_PENALTY_LAMBDA,
    ):
        """
        Args:
            db: DatabaseManager 实例。
            n_holdings: 最大持仓数量。
            factor_weights: 因子权重字典 {因子名: 权重}。
                           为 None 则等权。权重越大越重要。
            min_select_score: 选股最低综合得分。
                             得分低于此阈值的股票不入选。
                             默认 0（Z-Score 标准化后 0 为均值，仅选高于均值的股票）。
                             设为 -999 则不过滤（总是满仓）。
            turnover_penalty_lambda: 换手惩罚系数，0.0 = 关闭。
        """
        self.db = db
        self.n_holdings = n_holdings
        self.min_select_score = min_select_score
        self.turnover_penalty_lambda = turnover_penalty_lambda
        self._prev_holdings: set[str] = set()

        # 初始化因子实例
        self.factors = [
            # 价值因子
            EPFactor(db),
            BPFactor(db),
            DividendYieldFactor(db),
            # 动量因子
            MOM1MFactor(db),
            MOM3MFactor(db),
            MOM12MFactor(db),
            # 质量因子
            ROEFactor(db),
            GrossMarginFactor(db),
            # 技术因子
            Turnover20DFactor(db),
            # --- Phase 7 新增因子 ---
            # 防守型
            VolatilityFactor(db),        # VOL_20D
            PriceDeviationFactor(db),    # PRICE_DEV_60D
            ShortReversalFactor(db),     # REV_5D
            # 质量增强
            ProfitStabilityFactor(db),   # PROFIT_STB
            MarginTrendFactor(db),       # MARGIN_TREND
            # 效率型
            SizeFactor(db),              # SIZE
            IndustryMomentumFactor(db),  # IND_MOM
            # --- Phase 8 新增因子 ---
            # 成长因子
            NetProfitYOYFactor(db),      # NET_PROFIT_YOY
            RevenueYOYFactor(db),        # REVENUE_YOY
            NetProfitCAGR3YFactor(db),   # NET_PROFIT_CAGR_3Y
            # 残差动量
            ResidualMomentumFactor(db),  # RESIDUAL_MOM
            # 量价背离
            VolPriceDivFactor(db),       # VOL_PRICE_DIV
            # --- 商品轮动因子 ---
            CommodityMomentumFactor(db), # CMDTY_MOM
            # --- 宏观因子 ---
            MacroCycleFactor(db),        # MACRO_CYCLE
            MacroLiquidityFactor(db),    # MACRO_LIQD
            MacroInflationFactor(db),    # MACRO_INFL
            MacroExternalFactor(db),     # MACRO_EXTR
            # --- 舆情因子 ---
            SentimentPolicyFactor(db),   # POLICY_SENT
            SentimentIntensityFactor(db),# POLICY_INTENSITY
            # --- 券商研报因子 ---
            AnalystRatingFactor(db),     # ANALYST_RATING
            AnalystCoverageFactor(db),   # ANALYST_COVERAGE
        ]

        # 因子权重（默认等权，新因子使用差异化权重）
        if factor_weights is None:
            self.factor_weights = {f.name: 1.0 for f in self.factors}
            # 股息率因子
            self.factor_weights["DIV_YIELD"] = 0.8
            # 动量因子（降低纯趋势因子权重，提升反转因子）
            self.factor_weights["MOM_1M"] = 0.6            # 1月动量噪音大（1.0→0.6）
            self.factor_weights["MOM_3M"] = 0.8            # 3月动量适度降权（1.0→0.8）
            self.factor_weights["REV_5D"] = 0.7            # 加强短期反转捕捉（0.4→0.7）
            self.factor_weights["IND_MOM"] = 0.8           # 行业轮动
            self.factor_weights["RESIDUAL_MOM"] = 0.7
            # 技术因子（加强防守信号）
            self.factor_weights["TURN_20D"] = 0.5          # 降低换手惩罚（1.0→0.5）
            self.factor_weights["VOL_20D"] = 0.6           # 加强低波偏好（0.3→0.6）
            self.factor_weights["PRICE_DEV_60D"] = 0.4     # 加强超跌保护（0.15→0.4）
            self.factor_weights["SIZE"] = 0.3
            self.factor_weights["VOL_PRICE_DIV"] = 0.4
            # 质量因子
            self.factor_weights["PROFIT_STB"] = 0.5
            self.factor_weights["MARGIN_TREND"] = 0.4
            # 成长因子
            self.factor_weights["NET_PROFIT_YOY"] = 1.0
            self.factor_weights["REVENUE_YOY"] = 0.8
            self.factor_weights["NET_PROFIT_CAGR_3Y"] = 0.8
            # 商品轮动因子（低于 IND_MOM=0.8，因信号更间接）
            self.factor_weights["CMDTY_MOM"] = 0.6
            # 宏观因子（类内权重）
            self.factor_weights["MACRO_CYCLE"] = 0.8
            self.factor_weights["MACRO_LIQD"] = 0.7
            self.factor_weights["MACRO_INFL"] = 0.5
            self.factor_weights["MACRO_EXTR"] = 0.4
            # 舆情因子（类内权重）
            self.factor_weights["POLICY_SENT"] = 0.6
            self.factor_weights["POLICY_INTENSITY"] = 0.4
            # 券商研报因子（类内权重，归入 sentiment 大类）
            self.factor_weights["ANALYST_RATING"] = 0.6
            self.factor_weights["ANALYST_COVERAGE"] = 0.3
        else:
            self.factor_weights = factor_weights

        # 反向因子列表（值越低越好的因子）
        self._reverse_factors = ["TURN_20D", "VOL_20D", "PRICE_DEV_60D", "PROFIT_STB"]

        # 对硬编码权重中的反向因子取反
        for fname in self._reverse_factors:
            if fname in self.factor_weights:
                self.factor_weights[fname] = -abs(self.factor_weights[fname])

        # Regime 检测器
        self._regime_detector = RegimeDetector(db) if REGIME_ENABLED else None
        self._last_regime_strength: float = 1.0  # 上次 regime 强度，用于动态持仓数

        # 从 DB 加载行业因子权重配置
        self._industry_weights = self._load_industry_weights()

    # ----------------------------------------------------------
    # 缓存辅助方法
    # ----------------------------------------------------------

    def _get_cached_industry_df(self) -> pd.DataFrame | None:
        """获取行业映射（复用 FactorBase 静态缓存）。"""
        try:
            return self.factors[0].get_industry_map_cached()
        except Exception:
            return None

    def _get_cached_mktcap_df(self, date: str) -> pd.DataFrame | None:
        """计算市值数据（每日期缓存）。"""
        cache_key = ("mktcap", date)
        cached = FactorBase._date_cache.get(cache_key)
        if cached is not None:
            return cached
        try:
            # 复用 FactorBase 的缓存方法获取收盘价和总股本
            # 使用第一个因子实例来调用（所有因子共享同一 db）
            factor = self.factors[0]
            df_close = factor.get_close_on_date(date)
            df_share = factor.get_total_share()
            if not df_close.empty and not df_share.empty:
                mktcap_df = df_close[["ts_code", "close"]].merge(
                    df_share, on="ts_code", how="inner"
                )
                mktcap_df["total_mv"] = mktcap_df["close"] * mktcap_df["total_share"]
                mktcap_df = mktcap_df[["ts_code", "total_mv"]]
                FactorBase._date_cache[cache_key] = mktcap_df
                return mktcap_df
        except Exception:
            pass
        return None

    def _load_industry_weights(self) -> dict[str, dict[str, float]]:
        """
        从 DB 加载全部行业因子权重配置。

        Returns:
            {industry_name: {factor_name: weight}}，weight 为带符号值。
        """
        try:
            df = self.db.get_industry_factor_weights()
            if df.empty:
                return {}
            result = {}
            for _, row in df.iterrows():
                ind = row["industry_name"]
                if ind not in result:
                    result[ind] = {}
                result[ind][row["factor_name"]] = row["weight"]
            return result
        except Exception as e:
            logger.debug(f"加载行业因子权重配置失败（表可能不存在）: {e}")
            return {}

    def _get_weights_for_industry(self, industry_name: str) -> dict[str, float]:
        """
        获取指定行业的因子权重，回退链：具体行业 → __DEFAULT__ → self.factor_weights。

        Args:
            industry_name: 行业名称。

        Returns:
            {factor_name: weight} 字典。
        """
        if industry_name and industry_name in self._industry_weights:
            return self._industry_weights[industry_name]
        if "__DEFAULT__" in self._industry_weights:
            return self._industry_weights["__DEFAULT__"]
        return self.factor_weights

    def _get_regime_cat_weights(self, date: str) -> dict[str, float]:
        """
        获取 regime 感知的大类权重（渐进式插值）。

        使用 detect_strength() 返回 0~1 的牛市强度，
        在牛市权重和熊市权重之间做线性插值，避免二元跳变。

        Args:
            date: 选股日期。

        Returns:
            大类权重字典。
        """
        if self._regime_detector is None:
            return self.CATEGORY_WEIGHTS

        if not REGIME_BEAR_OVERRIDES:
            return self.CATEGORY_WEIGHTS

        strength = self._regime_detector.detect_strength(date)
        self._last_regime_strength = strength

        # strength=1.0 → 纯牛市权重；strength=0.0 → 纯熊市权重
        if strength >= 1.0:
            return self.CATEGORY_WEIGHTS

        weights = {}
        for cat, bull_w in self.CATEGORY_WEIGHTS.items():
            bear_w = REGIME_BEAR_OVERRIDES.get(cat, bull_w)
            weights[cat] = bull_w * strength + bear_w * (1.0 - strength)

        logger.info(
            f"Regime 渐进权重 (strength={strength:.2f}): "
            + ", ".join(f"{c}={w:.2f}" for c, w in weights.items())
        )
        return weights

    def _adjust_sentiment_weight(
        self,
        weights: dict[str, float],
        date: str,
    ) -> dict[str, float]:
        """
        动态调整舆情大类权重：当某些行业文章数量异常集中时提升权重。

        检测逻辑：
            1. 获取当日行业舆情得分（含文章计数 n_articles）
            2. 计算文章数的 z-score，找出异常集中的行业
            3. 存在 z > 阈值的热点行业 → 信号质量高 → 提升权重

        效果：AI 热潮（计算机 20+ 篇 vs 平均 10 篇）时舆情权重自动提升，
        日常均匀分布的信号保持基础权重。
        """
        if SENTIMENT_SURGE_MULTIPLIER <= 1.0:
            return weights

        try:
            # 复用已有的 SentimentAnalyzer（通过 sentiment 因子实例）
            for factor in self.factors:
                if hasattr(factor, '_analyzer'):
                    daily_score = factor._analyzer.get_daily_score(date)
                    break
            else:
                return weights

            if daily_score.empty or len(daily_score) < 5:
                return weights

            if "n_articles" not in daily_score.columns:
                return weights

            # 用文章数计算 z-score
            counts = daily_score["n_articles"].astype(float)
            mean_c = counts.mean()
            std_c = counts.std()
            if std_c < 1.0:
                return weights

            max_z = (counts.max() - mean_c) / std_c

            if max_z >= SENTIMENT_SURGE_ZSCORE:
                # z-score 越高，权重提升越大（线性插值）
                cap_z = min(max_z, 3.0)
                boost = 1.0 + (cap_z - SENTIMENT_SURGE_ZSCORE) / max(3.0 - SENTIMENT_SURGE_ZSCORE, 0.1) * (SENTIMENT_SURGE_MULTIPLIER - 1.0)
                boost = min(boost, SENTIMENT_SURGE_MULTIPLIER)

                old_w = weights.get("sentiment", 0.6)
                new_w = old_w * boost
                weights = dict(weights)
                weights["sentiment"] = new_w

                # 找出热点行业
                daily_score["_z"] = (counts - mean_c) / std_c
                hot = daily_score[daily_score["_z"] >= SENTIMENT_SURGE_ZSCORE]
                hot_inds = hot.sort_values("_z", ascending=False)["industry_name"].tolist()
                logger.info(
                    f"舆情热点集中 (max_z={max_z:.1f}): "
                    f"权重 {old_w:.2f}→{new_w:.2f} "
                    f"热点: {','.join(hot_inds[:3])}"
                )

        except Exception as e:
            logger.debug(f"舆情权重动态调整异常: {e}")

        return weights

    def _compute_scores(
        self,
        composite: pd.DataFrame,
        factor_cols: list[str],
        industry_df,
        category_weights: dict[str, float] = None,
    ) -> pd.DataFrame:
        """
        大类合成评分（缺失大类权重再分配）— 向量化实现。

        评分公式：
            1. 剔除缺失全部核心财务因子的股票（准入过滤）
            2. 每个大类内：加权平均（动态分母，同类因子可互替）
            3. 大类间：分母 = 有值大类的权重之和（缺失大类权重自动再分配给有值大类）

        效果：
            - 动量 6 个因子的贡献限制在 1 个大类权重内（与价值 2 因子等权）
            - MIN_VALID_CATEGORIES 限制最大膨胀幅度
        """
        composite = composite.copy()
        effective_cat_weights = category_weights or self.CATEGORY_WEIGHTS

        # 1. 核心财务准入过滤
        core_cols = [c for c in self.CORE_FINANCIAL_FACTORS if c in factor_cols]
        if core_cols:
            has_financial = composite[core_cols].notna().any(axis=1)
            n_before = len(composite)
            composite = composite[has_financial].copy()
            n_dropped = n_before - len(composite)
            if n_dropped > 0:
                logger.info(f"核心财务准入过滤: 剔除 {n_dropped} 只（缺失全部核心财务指标）")

        if composite.empty:
            composite["score"] = np.nan
            return composite

        # 2. 合并行业信息（用于行业权重查找）
        if industry_df is not None:
            composite = composite.merge(
                industry_df[["ts_code", "industry_name"]], on="ts_code", how="left"
            )

        use_industry = bool(self._industry_weights) and industry_df is not None

        # 3. 向量化评分
        if use_industry:
            # 行业差异化权重路径：按行业分组向量化
            composite["score"] = self._compute_scores_by_industry(
                composite, factor_cols, effective_cat_weights
            )
        else:
            # 通用路径：纯向量化（无行业差异权重）
            composite["score"] = self._compute_scores_vectorized(
                composite, factor_cols, effective_cat_weights
            )

        return composite

    @staticmethod
    def _apply_value_trap_penalty(
        cat_scores: np.ndarray,
        cat_has_value: np.ndarray,
        cat_names: list[str],
    ) -> np.ndarray:
        """
        价值陷阱惩罚：价值得分高但质量得分为负时，压缩价值得分。

        逻辑：quality < 0 时，value_score *= max(0.2, 1 + quality_score)
        质量越差，价值得分被压缩越多（最多压到 20%）。
        """
        try:
            val_idx = cat_names.index("value")
            qual_idx = cat_names.index("quality")
        except ValueError:
            return cat_scores

        cat_scores = cat_scores.copy()
        both_valid = cat_has_value[:, val_idx] & cat_has_value[:, qual_idx]
        # 仅在质量严重恶化（< -0.5）时触发，避免误伤正常估值波动
        trap_mask = both_valid & (cat_scores[:, val_idx] > 0) & (cat_scores[:, qual_idx] < -0.5)

        if trap_mask.any():
            # quality_score 在 [-3, -0.5) 范围，1.5 + quality 在 (-1.5, 1.0) → clip 到 [0.3, 1.0]
            penalty = np.clip(1.5 + cat_scores[trap_mask, qual_idx], 0.3, 1.0)
            cat_scores[trap_mask, val_idx] *= penalty
            n_penalized = trap_mask.sum()
            logger.debug(f"价值陷阱惩罚: {n_penalized} 只股票")

        return cat_scores

    @staticmethod
    def _apply_missing_factor_penalty(
        final_score: np.ndarray,
        composite: pd.DataFrame,
        factor_cols: list[str],
    ) -> np.ndarray:
        """
        缺失因子惩罚：缺失比例超过阈值时线性衰减得分。

        避免因子覆盖率低的股票（如小盘次新股）因动态分母获得虚高分数。
        """
        if MISSING_FACTOR_THRESHOLD >= 1.0 or MISSING_FACTOR_MAX_PENALTY <= 0:
            return final_score

        n_total = len(factor_cols)
        if n_total == 0:
            return final_score

        n_valid = composite[factor_cols].notna().sum(axis=1).values
        missing_ratio = 1.0 - n_valid / n_total

        # 超过阈值部分线性惩罚
        excess = np.clip(missing_ratio - MISSING_FACTOR_THRESHOLD, 0, None)
        max_excess = 1.0 - MISSING_FACTOR_THRESHOLD
        penalty = 1.0 - (excess / max_excess) * MISSING_FACTOR_MAX_PENALTY

        result = final_score.copy() if isinstance(final_score, np.ndarray) else np.array(final_score)
        valid_mask = ~np.isnan(result)
        result[valid_mask] *= penalty[valid_mask] if isinstance(penalty, np.ndarray) else penalty

        n_penalized = (valid_mask & (penalty < 1.0)).sum()
        if n_penalized > 0:
            logger.info(f"缺失因子惩罚: {n_penalized} 只股票被降分")

        return result

    def _apply_financial_staleness_decay(
        self,
        date: str,
        composite: pd.DataFrame,
        factor_cols: list[str],
    ) -> pd.DataFrame:
        """
        财务数据时效衰减：报告期越远，财务因子得分越低。

        衰减规则（按报告期距今月数）：
            ≤3 个月: 100%
            3~6 个月: 50%
            6~9 个月: 25%
            >9 个月: 负面惩罚（因子值设为 -1.0，延迟/不披露视为负面信号）
        """
        fin_cols = [f for f in self.FINANCIAL_DEPENDENT_FACTORS if f in factor_cols]
        if not fin_cols:
            return composite

        # 查每只股票最新财报的 end_date（报告期）— 优先使用预加载数据
        bulk_fin = FactorBase._static_cache.get("_bulk_financial")
        if bulk_fin is not None and not bulk_fin.empty:
            date_ts = pd.to_datetime(date)
            codes_set = set(composite["ts_code"].tolist())
            df_fin = bulk_fin[
                (bulk_fin["ann_date"] <= date_ts) & bulk_fin["ts_code"].isin(codes_set)
            ]
            if df_fin.empty:
                return composite
            df_latest = df_fin.groupby("ts_code")["end_date"].max().reset_index()
            df_latest.columns = ["ts_code", "latest_end_date"]
        else:
            codes = composite["ts_code"].tolist()
            codes_str = "','".join(codes)
            df_latest = self.db.query(
                f"SELECT ts_code, MAX(end_date) as latest_end_date "
                f"FROM financial_data "
                f"WHERE ts_code IN ('{codes_str}') AND ann_date <= '{date}' "
                f"GROUP BY ts_code"
            )

        if df_latest.empty:
            return composite

        composite = composite.copy()
        ref_date = pd.to_datetime(date)
        df_latest["latest_end_date"] = pd.to_datetime(df_latest["latest_end_date"])
        df_latest["months_stale"] = (
            (ref_date - df_latest["latest_end_date"]).dt.days / 30.44
        )

        # 合并
        composite = composite.merge(
            df_latest[["ts_code", "months_stale"]], on="ts_code", how="left"
        )
        # 无财务数据的股票视为极度过时
        composite["months_stale"] = composite["months_stale"].fillna(99)

        months = composite["months_stale"].values
        decay = np.where(
            months <= 3, 1.0,
            np.where(months <= 6, 0.5,
                np.where(months <= 9, 0.25, 0.0))
        )

        # 对财务因子乘以衰减系数；>9个月设为负面惩罚值
        stale_penalty = -1.0  # 超期未披露 → 负面信号
        for col in fin_cols:
            if col in composite.columns:
                vals = composite[col].values.astype(float)
                composite[col] = np.where(
                    decay > 0, vals * decay, stale_penalty
                )

        n_decayed = ((decay < 1.0) & (decay > 0)).sum()
        n_penalized = (decay == 0).sum()
        if n_decayed > 0 or n_penalized > 0:
            logger.info(
                f"财报时效衰减: {n_decayed} 只降权, {n_penalized} 只财务因子负面惩罚"
            )

        composite = composite.drop(columns=["months_stale"])
        return composite

    def _compute_scores_vectorized(
        self,
        composite: pd.DataFrame,
        factor_cols: list[str],
        effective_cat_weights: dict[str, float],
    ) -> np.ndarray:
        """纯向量化评分（通用权重，不区分行业）。"""
        n_stocks = len(composite)
        n_cats = len(self.FACTOR_CATEGORIES)
        cat_names = list(self.FACTOR_CATEGORIES.keys())

        # 预计算每个大类的得分
        cat_scores = np.full((n_stocks, n_cats), np.nan)
        cat_has_value = np.zeros((n_stocks, n_cats), dtype=bool)

        for cat_idx, cat in enumerate(cat_names):
            factors = self.FACTOR_CATEGORIES[cat]
            cat_factor_cols = [f for f in factors if f in factor_cols]
            if not cat_factor_cols:
                continue

            # 因子权重数组
            fw = np.array([self.factor_weights.get(f, 1.0) for f in cat_factor_cols])
            # 因子值矩阵 (n_stocks, n_factors_in_cat)
            values = composite[cat_factor_cols].values.astype(float)

            valid = ~np.isnan(values)
            # 加权求和（NaN 视为 0）
            weighted_sum = np.nansum(values * fw, axis=1)
            # 权重分母（仅计入有效因子）
            weight_denom = (valid * np.abs(fw)).sum(axis=1)

            has_value = weight_denom > 0
            cat_has_value[:, cat_idx] = has_value
            cat_scores[has_value, cat_idx] = weighted_sum[has_value] / weight_denom[has_value]

        # 价值陷阱惩罚
        cat_scores = self._apply_value_trap_penalty(cat_scores, cat_has_value, cat_names)

        # 大类权重
        cat_weight_arr = np.array([effective_cat_weights.get(c, 1.0) for c in cat_names])

        # 加权大类得分
        weighted_cat = np.where(cat_has_value, cat_scores * cat_weight_arr, 0.0)
        total_score = weighted_cat.sum(axis=1)

        # 有效大类计数
        n_valid_cats = cat_has_value.sum(axis=1)
        # 有值大类权重之和
        weight_denom_total = (cat_has_value * np.abs(cat_weight_arr)).sum(axis=1)

        # 最终得分
        final_score = np.where(
            (n_valid_cats >= MIN_VALID_CATEGORIES) & (weight_denom_total > 0),
            total_score / weight_denom_total,
            np.nan,
        )

        # 缺失因子惩罚
        final_score = self._apply_missing_factor_penalty(
            final_score, composite, factor_cols
        )

        return final_score

    def _compute_scores_by_industry(
        self,
        composite: pd.DataFrame,
        factor_cols: list[str],
        effective_cat_weights: dict[str, float],
    ) -> pd.Series:
        """按行业分组向量化评分（行业差异化权重）。"""
        result = pd.Series(np.nan, index=composite.index)
        cat_names = list(self.FACTOR_CATEGORIES.keys())

        for ind_name, group in composite.groupby("industry_name", dropna=False):
            weights = self._get_weights_for_industry(ind_name if pd.notna(ind_name) else "")
            idx = group.index
            n = len(group)
            n_cats = len(cat_names)

            cat_scores = np.full((n, n_cats), np.nan)
            cat_has_value = np.zeros((n, n_cats), dtype=bool)

            for cat_idx, cat in enumerate(cat_names):
                factors = self.FACTOR_CATEGORIES[cat]
                cat_factor_cols = [f for f in factors if f in factor_cols]
                if not cat_factor_cols:
                    continue

                fw = np.array([weights.get(f, self.factor_weights.get(f, 1.0)) for f in cat_factor_cols])
                values = group[cat_factor_cols].values.astype(float)
                valid = ~np.isnan(values)

                weighted_sum = np.nansum(values * fw, axis=1)
                weight_denom = (valid * np.abs(fw)).sum(axis=1)

                has_value = weight_denom > 0
                cat_has_value[:, cat_idx] = has_value
                cat_scores[has_value, cat_idx] = weighted_sum[has_value] / weight_denom[has_value]

            # 价值陷阱惩罚
            cat_scores = self._apply_value_trap_penalty(cat_scores, cat_has_value, cat_names)

            cat_weight_arr = np.array([effective_cat_weights.get(c, 1.0) for c in cat_names])
            weighted_cat = np.where(cat_has_value, cat_scores * cat_weight_arr, 0.0)
            total_score = weighted_cat.sum(axis=1)
            n_valid_cats = cat_has_value.sum(axis=1)
            weight_denom_total = (cat_has_value * np.abs(cat_weight_arr)).sum(axis=1)

            scores = np.where(
                (n_valid_cats >= MIN_VALID_CATEGORIES) & (weight_denom_total > 0),
                total_score / weight_denom_total,
                np.nan,
            )
            result.iloc[result.index.get_indexer(idx)] = scores

        # 缺失因子惩罚（全局计算）
        result_arr = self._apply_missing_factor_penalty(
            result.values, composite, factor_cols
        )
        return pd.Series(result_arr, index=result.index)

    def _compute_scores_for_date(self, date: str) -> pd.DataFrame:
        """
        计算指定日期的全量因子得分（线程安全，不含换手惩罚和 Top-N 选取）。

        这是 select_stocks 的重活部分，可并行执行。

        Returns:
            全量评分 DataFrame[ts_code, score]，或空 DataFrame。
        """
        logger.info(f"计算因子: {date}")

        # 清除上一日期的中性化缓存
        clear_neutralize_cache()

        # 1. 构建股票池（缓存避免同一日期重复查询）
        cache_key = f"_universe_{date}"
        cached_univ = FactorBase._date_cache.get(cache_key)
        if cached_univ is not None:
            universe = cached_univ
        else:
            universe = get_clean_universe(self.db, date, min_turnover=0)
            FactorBase._date_cache[cache_key] = universe
        if universe.empty:
            logger.warning(f"{date} 股票池为空")
            return pd.DataFrame(columns=["ts_code", "score"])

        # 2. 计算各因子值
        factor_scores = {}
        for factor in self.factors:
            try:
                df_factor = factor.compute(date, universe)
                if not df_factor.empty:
                    factor_scores[factor.name] = df_factor
            except Exception as e:
                logger.warning(f"因子 {factor.name} 计算失败: {e}")

        if not factor_scores:
            logger.warning(f"{date} 所有因子计算失败")
            return pd.DataFrame(columns=["ts_code", "score"])

        # 3. 因子处理 + 合成
        industry_df = self._get_cached_industry_df()
        mktcap_df = self._get_cached_mktcap_df(date)

        all_codes = universe["ts_code"].tolist()
        composite = pd.DataFrame({"ts_code": all_codes})

        for fname, df_raw in factor_scores.items():
            cat = self.FACTOR_TO_CATEGORY.get(fname)
            effective_neutralize = CATEGORY_NEUTRALIZE_OVERRIDES.get(cat, NEUTRALIZE_MODE)
            processed = process_factor(
                df_raw,
                industry_df=industry_df,
                mktcap_df=mktcap_df,
                do_neutralize=(mktcap_df is not None),
                neutralize_mode=effective_neutralize,
                nonlinear_size=NONLINEAR_SIZE,
                standardize_mode=STANDARDIZE_MODE,
            )
            processed = processed.rename(columns={"factor_value": fname})
            composite = composite.merge(processed, on="ts_code", how="left")

        factor_cols = [c for c in composite.columns if c != "ts_code"]

        # 3.5 财报时效衰减（标准化后、合成前）
        composite = self._apply_financial_staleness_decay(date, composite, factor_cols)

        # 4. 大类合成评分（regime + 舆情动态权重）
        effective_cat_weights = self._get_regime_cat_weights(date)
        effective_cat_weights = self._adjust_sentiment_weight(effective_cat_weights, date)
        composite = self._compute_scores(composite, factor_cols, industry_df, category_weights=effective_cat_weights)
        composite = composite.drop(columns=["industry_name"], errors="ignore")

        composite = composite.dropna(subset=["score"])

        # 5. 趋势门槛过滤：MOM_12M 强烈为负的股票惩罚得分（防止买入持续下跌股）
        if "MOM_12M" in composite.columns:
            mom12 = composite["MOM_12M"]
            # MOM_12M < -1.0（标准化后约底部 16%）→ 得分乘以衰减系数
            # MOM_12M = -1.0 → penalty = 0.7, MOM_12M = -3.0 → penalty = 0.3
            trend_penalty_mask = mom12 < -1.0
            if trend_penalty_mask.any():
                penalty = np.clip(1.0 + 0.3 * mom12[trend_penalty_mask], 0.3, 0.7)
                composite.loc[trend_penalty_mask, "score"] *= penalty
                n_penalized = trend_penalty_mask.sum()
                logger.info(f"趋势门槛过滤: {n_penalized} 只股票得分被惩罚")

        # 6. 行业级趋势过滤：行业整体 MOM_12M 为负时惩罚该行业所有股票
        if "MOM_12M" in composite.columns and industry_df is not None:
            ind_merged = composite[["ts_code", "MOM_12M", "score"]].merge(
                industry_df[["ts_code", "industry_name"]], on="ts_code", how="left"
            )
            ind_merged["industry_name"] = ind_merged["industry_name"].fillna("未知")
            # 行业级 MOM_12M 中位数（比均值更稳健，不受极端值影响）
            ind_mom = ind_merged.groupby("industry_name")["MOM_12M"].median()
            # 行业 MOM_12M 中位数 < -0.5 → 该行业处于下行趋势
            bad_industries = ind_mom[ind_mom < -0.5].index.tolist()
            if bad_industries:
                bad_mask = ind_merged["industry_name"].isin(bad_industries)
                bad_codes = ind_merged.loc[bad_mask, "ts_code"].tolist()
                code_mask = composite["ts_code"].isin(bad_codes)
                if code_mask.any():
                    # 行业 MOM_12M 越负，惩罚越大：median=-0.5 → 0.8, median=-2.0 → 0.4
                    code_to_ind = ind_merged.set_index("ts_code")["industry_name"]
                    ind_penalty_map = ind_mom[bad_industries].clip(-3.0, -0.5)
                    # 线性映射: -0.5 → 0.8, -2.0 → 0.4
                    ind_penalty_val = 0.8 + (ind_penalty_map - (-0.5)) / (-2.0 - (-0.5)) * (0.4 - 0.8)
                    ind_penalty_val = ind_penalty_val.clip(0.4, 0.8)
                    # 将行业惩罚映射到个股
                    stock_inds = code_to_ind.reindex(composite.loc[code_mask, "ts_code"])
                    stock_penalty = stock_inds.map(ind_penalty_val).values
                    composite.loc[code_mask, "score"] *= stock_penalty
                    logger.info(
                        f"行业趋势过滤: {len(bad_industries)} 个行业 "
                        f"({', '.join(bad_industries[:5])}), "
                        f"{code_mask.sum()} 只股票被惩罚"
                    )

        # 排除涨停股
        limit_up_codes = universe[universe["is_limit_up"] == 1]["ts_code"].tolist()
        composite = composite[~composite["ts_code"].isin(limit_up_codes)]

        logger.info(f"{date} 评分完成: {len(composite)} 只有效")
        return composite[["ts_code", "score"]]

    def _select_from_scores(
        self, composite: pd.DataFrame, prev_holdings: set[str],
    ) -> pd.DataFrame:
        """
        从全量评分中执行 Top-N 选取 + 换手惩罚 + Softmax 权重分配。

        Args:
            composite: _compute_scores_for_date 返回的 DataFrame[ts_code, score]。
            prev_holdings: 前一期持仓股票代码集合。

        Returns:
            选中的股票 DataFrame[ts_code, score, weight]。
        """
        if composite.empty:
            return pd.DataFrame(columns=["ts_code", "score", "weight"])

        composite = composite.copy()

        # 换手惩罚
        if self.turnover_penalty_lambda > 0 and prev_holdings:
            composite["score"] = composite["score"] + self.turnover_penalty_lambda * composite["ts_code"].isin(prev_holdings).astype(float)

        composite = composite.sort_values("score", ascending=False)
        qualified = composite[composite["score"] >= self.min_select_score]

        # Regime 联动持仓数：牛市满仓，熊市缩减（多留现金）
        strength = self._last_regime_strength
        bear_n = max(3, int(self.n_holdings * BEAR_HOLDINGS_RATIO))
        effective_n = int(bear_n + (self.n_holdings - bear_n) * strength)
        effective_n = max(bear_n, min(effective_n, self.n_holdings))
        if effective_n < self.n_holdings:
            logger.info(f"Regime 联动持仓: {self.n_holdings} -> {effective_n} (strength={strength:.2f})")

        selected = qualified.head(effective_n).copy()

        # Softmax 权重分配
        if len(selected) > 0:
            scores = selected["score"].values
            tau = WEIGHT_TEMPERATURE
            if tau > 0:
                shifted = scores - scores.max()
                exp_scores = np.exp(shifted / tau)
                raw_w = exp_scores / exp_scores.sum()
            else:
                raw_w = np.ones(len(scores)) / len(scores)
            min_w = 1.0 / (self.n_holdings * 3)
            raw_w = np.maximum(raw_w, min_w)
            selected["weight"] = raw_w / raw_w.sum()

        return selected[["ts_code", "score", "weight"]] if len(selected) > 0 else pd.DataFrame(columns=["ts_code", "score", "weight"])

    def select_stocks(self, date: str) -> pd.DataFrame:
        """
        在指定日期进行选股（单日完整流程）。

        Args:
            date: 选股日期，格式 YYYY-MM-DD。

        Returns:
            选中的股票 DataFrame，包含 ts_code, score, weight 列。
        """
        composite = self._compute_scores_for_date(date)
        selected = self._select_from_scores(composite, self._prev_holdings)
        self._prev_holdings = set(selected["ts_code"].tolist()) if len(selected) > 0 else set()

        if len(selected) > 0:
            logger.info(
                f"选股完成: {len(selected)} 只, "
                f"得分范围 [{selected['score'].min():.3f}, {selected['score'].max():.3f}]"
            )
        else:
            logger.info(f"选股完成: 0 只入选（空仓）")

        return selected

    def score_all_stocks(self, date: str, include_factors: bool = False) -> pd.DataFrame:
        """
        对全部可交易股票评分（不做 top-N 筛选，不限行业）。

        返回全量得分 DataFrame[ts_code, score, industry_name]，按 score 降序。
        用于分行业展示等场景。include_factors=True 时额外保留各因子列（供因子明细查看）。
        """
        logger.info(f"全量评分: {date}")

        universe = get_clean_universe(self.db, date, min_turnover=0, skip_industry_filter=True)
        if universe.empty:
            return pd.DataFrame(columns=["ts_code", "score"])

        # 计算因子
        factor_scores = {}
        for factor in self.factors:
            try:
                df_factor = factor.compute(date, universe)
                if not df_factor.empty:
                    factor_scores[factor.name] = df_factor
            except Exception as e:
                logger.warning(f"因子 {factor.name} 计算失败: {e}")

        if not factor_scores:
            return pd.DataFrame(columns=["ts_code", "score"])

        # 行业/市值数据（使用缓存）
        industry_df = self._get_cached_industry_df()
        mktcap_df = self._get_cached_mktcap_df(date)

        # 因子处理 + 合并
        all_codes = universe["ts_code"].tolist()
        composite = pd.DataFrame({"ts_code": all_codes})

        for fname, df_raw in factor_scores.items():
            cat = self.FACTOR_TO_CATEGORY.get(fname)
            effective_neutralize = CATEGORY_NEUTRALIZE_OVERRIDES.get(cat, NEUTRALIZE_MODE)
            processed = process_factor(
                df_raw,
                industry_df=industry_df,
                mktcap_df=mktcap_df,
                do_neutralize=(mktcap_df is not None),
                neutralize_mode=effective_neutralize,
                nonlinear_size=NONLINEAR_SIZE,
                standardize_mode=STANDARDIZE_MODE,
            )
            processed = processed.rename(columns={"factor_value": fname})
            composite = composite.merge(processed, on="ts_code", how="left")

        factor_cols = [c for c in composite.columns if c != "ts_code"]

        # 财报时效衰减
        composite = self._apply_financial_staleness_decay(date, composite, factor_cols)

        # 核心财务准入过滤 + 大类合成评分（regime + 舆情动态权重）
        effective_cat_weights = self._get_regime_cat_weights(date)
        effective_cat_weights = self._adjust_sentiment_weight(effective_cat_weights, date)
        composite = self._compute_scores(composite, factor_cols, industry_df, category_weights=effective_cat_weights)

        composite = composite.dropna(subset=["score"])

        # 排除涨停股
        limit_up_codes = universe[universe["is_limit_up"] == 1]["ts_code"].tolist()
        composite = composite[~composite["ts_code"].isin(limit_up_codes)]

        composite = composite.sort_values("score", ascending=False)

        if include_factors:
            return composite.reset_index(drop=True)

        # 只保留需要的列
        keep_cols = ["ts_code", "score"]
        if "industry_name" in composite.columns:
            keep_cols.append("industry_name")
        return composite[keep_cols].reset_index(drop=True)

    def get_rebalance_dates(self, start_date: str, end_date: str) -> list[str]:
        """
        获取调仓日期列表（每 10 个交易日调仓一次）。

        Args:
            start_date: 起始日期。
            end_date: 结束日期。

        Returns:
            调仓日期列表。
        """
        df = self.db.query(
            "SELECT DISTINCT trade_date FROM daily_price "
            "WHERE trade_date >= :start_date "
            "AND trade_date <= :end_date "
            "ORDER BY trade_date",
            params={"start_date": start_date, "end_date": end_date},
        )

        if df.empty:
            return []

        trading_days = sorted(pd.to_datetime(df["trade_date"]).dt.strftime("%Y-%m-%d").tolist())
        return trading_days[::REBALANCE_INTERVAL]

    def generate_signals(
        self, start_date: str, end_date: str,
        cancel_check: Optional[callable] = None,
        max_workers: int = 0,
    ) -> dict[str, pd.DataFrame]:
        """
        生成回测区间内所有调仓日的选股信号。

        支持自适应调仓：在半月频基准之间，每隔 REBALANCE_CHECK_INTERVAL 个交易日
        检查偏离度，若 Top-N 持仓变化超过 REBALANCE_DEVIATION_THRESHOLD 则触发额外调仓。

        Args:
            start_date: 回测起始日期。
            end_date: 回测结束日期。
            cancel_check: 可选的取消检查回调，返回 True 时终止。
            max_workers: 并行线程数。0=自动（调仓日>=6 时启用，线程数=min(8, cpu_count)），
                         1=串行，>1=指定线程数。

        Returns:
            字典 {调仓日期: 选股结果 DataFrame}。
        """
        rebalance_dates = self.get_rebalance_dates(start_date, end_date)

        # 回溯查找 start_date 之前最近一个调仓日
        prior_start = (pd.to_datetime(start_date) - pd.DateOffset(months=2)).strftime("%Y-%m-%d")
        prior_end = (pd.to_datetime(start_date) - pd.Timedelta(days=1)).strftime("%Y-%m-%d")
        prior_dates = self.get_rebalance_dates(prior_start, prior_end)
        if prior_dates:
            last_prior = prior_dates[-1]
            rebalance_dates = [last_prior] + rebalance_dates
            logger.info(f"回溯前月调仓日: {last_prior}")

        # 自适应调仓：插入偏离度检查日期
        if REBALANCE_CHECK_INTERVAL > 0 and REBALANCE_DEVIATION_THRESHOLD > 0:
            check_dates = self._get_adaptive_check_dates(
                start_date, end_date, set(rebalance_dates)
            )
            if check_dates:
                logger.info(
                    f"自适应调仓: {len(check_dates)} 个检查日, "
                    f"偏离度阈值={REBALANCE_DEVIATION_THRESHOLD:.0%}, "
                    f"检查间隔={REBALANCE_CHECK_INTERVAL}日"
                )
        else:
            check_dates = []

        n_dates = len(rebalance_dates)
        logger.info(f"回测区间: {start_date} ~ {end_date}, {n_dates} 个基准调仓日")

        # 检查是否已有预加载的回测数据（数据管理页可提前加载）
        existing_bulk = FactorBase._static_cache.get("_bulk_daily")
        if existing_bulk is not None and not existing_bulk.empty:
            # 验证预加载数据覆盖回测区间
            price_start_needed = (
                pd.to_datetime(start_date) - pd.Timedelta(days=400)
            )
            cached_min = existing_bulk["trade_date"].min()
            cached_max = existing_bulk["trade_date"].max()
            if cached_min <= price_start_needed and cached_max >= pd.to_datetime(end_date):
                logger.info(
                    f"使用已缓存的回测数据 ({len(existing_bulk)} 行, "
                    f"{cached_min.strftime('%Y-%m-%d')}~{cached_max.strftime('%Y-%m-%d')})"
                )
                # 仅清除日期相关缓存，保留静态缓存
                FactorBase._date_cache.clear()
                # 确保 rolling stats 已预计算
                if FactorBase._static_cache.get("_rolling_indexed") is None:
                    FactorBase.precompute_rolling_stats()
            else:
                logger.info(
                    f"缓存数据范围不足 ({cached_min.strftime('%Y-%m-%d')}~{cached_max.strftime('%Y-%m-%d')}), 重新加载"
                )
                FactorBase.clear_all_cache()
                FactorBase.preload_for_backtest(self.db, start_date, end_date)
                FactorBase.precompute_rolling_stats()
        else:
            # 初始化缓存（静态数据跨日期复用）
            FactorBase.clear_all_cache()
            FactorBase.preload_for_backtest(self.db, start_date, end_date)
            FactorBase.precompute_rolling_stats()

        # 批量预计算所有调仓日的 clean universe（避免逐日 SQL 查询）
        all_dates = sorted(set(rebalance_dates + check_dates))
        bulk_daily = FactorBase._static_cache.get("_bulk_daily")
        if bulk_daily is not None and not bulk_daily.empty:
            preloaded_universes = preload_clean_universes(self.db, all_dates, bulk_daily)
            for dt, univ_df in preloaded_universes.items():
                FactorBase._date_cache[f"_universe_{dt}"] = univ_df

        # 自适应模式：Phase1 并行因子计算 + Phase2 串行偏离度判断
        if check_dates:
            signals = self._generate_signals_adaptive(
                rebalance_dates, check_dates, cancel_check
            )
        else:
            # 决定并行度
            if max_workers == 0:
                import os
                max_workers = min(8, os.cpu_count() or 4) if n_dates >= 6 else 1

            if max_workers > 1 and n_dates > 1:
                signals = self._generate_signals_parallel(rebalance_dates, max_workers, cancel_check)
            else:
                signals = self._generate_signals_sequential(rebalance_dates, cancel_check)

        # 仅清除日期相关缓存，保留静态数据（供后续回测复用）
        FactorBase.clear_date_cache()
        logger.info(f"信号生成完成: {len(signals)} 期信号（含空仓）")
        return signals

    def _get_adaptive_check_dates(
        self,
        start_date: str,
        end_date: str,
        base_rebalance_set: set[str],
    ) -> list[str]:
        """
        获取自适应调仓的偏离度检查日期（排除已有的基准调仓日）。

        在基准调仓日之间，每隔 REBALANCE_CHECK_INTERVAL 个交易日插入一个检查点。
        """
        df = self.db.query(
            "SELECT DISTINCT trade_date FROM daily_price "
            "WHERE trade_date >= :start_date "
            "AND trade_date <= :end_date "
            "ORDER BY trade_date",
            params={"start_date": start_date, "end_date": end_date},
        )
        if df.empty:
            return []

        all_trade_dates = pd.to_datetime(df["trade_date"]).dt.strftime("%Y-%m-%d").tolist()

        check_dates = []
        for i, dt in enumerate(all_trade_dates):
            if dt in base_rebalance_set:
                continue
            if i % REBALANCE_CHECK_INTERVAL == 0:
                check_dates.append(dt)

        return check_dates

    def _generate_signals_adaptive(
        self,
        rebalance_dates: list[str],
        check_dates: list[str],
        cancel_check: Optional[callable] = None,
    ) -> dict[str, pd.DataFrame]:
        """
        自适应调仓信号生成（两阶段：并行因子计算 + 串行偏离度判断）。

        Phase 1: 并行计算所有日期（基准+检查日）的因子评分
        Phase 2: 按时间顺序串行遍历，基准日无条件选股，检查日做偏离度判断
        """
        import os
        import time
        t0 = time.time()

        # 合并所有日期并标记类型
        all_dates = {}
        for dt in rebalance_dates:
            all_dates[dt] = "base"
        for dt in check_dates:
            if dt not in all_dates:
                all_dates[dt] = "check"

        sorted_dates = sorted(all_dates.keys())

        # 为了计算最短间隔，获取全部交易日列表
        all_trade_df = self.db.query(
            "SELECT DISTINCT trade_date FROM daily_price "
            "WHERE trade_date >= :start AND trade_date <= :end ORDER BY trade_date",
            params={"start": sorted_dates[0] if sorted_dates else "", "end": sorted_dates[-1] if sorted_dates else ""},
        )
        trade_date_list = pd.to_datetime(all_trade_df["trade_date"]).dt.strftime("%Y-%m-%d").tolist() if not all_trade_df.empty else []
        trade_date_to_idx = {d: i for i, d in enumerate(trade_date_list)}

        # ---- Phase 1: 并行计算所有日期的因子评分 ----
        composites: dict[str, pd.DataFrame] = {}
        n_workers = min(8, os.cpu_count() or 4) if len(sorted_dates) >= 6 else 1

        if n_workers > 1:
            logger.info(f"自适应调仓 Phase1: 并行计算 {len(sorted_dates)} 个日期因子, {n_workers} 线程")
            with ThreadPoolExecutor(max_workers=n_workers) as pool:
                future_map = {}
                for dt in sorted_dates:
                    if cancel_check and cancel_check():
                        raise RuntimeError('回测已取消')
                    future = pool.submit(self._compute_scores_for_date, dt)
                    future_map[future] = dt

                for future in as_completed(future_map):
                    dt = future_map[future]
                    try:
                        composites[dt] = future.result()
                    except Exception as e:
                        logger.warning(f"{dt} 因子计算失败: {e}")
                        composites[dt] = pd.DataFrame(columns=["ts_code", "score"])
        else:
            logger.info(f"自适应调仓 Phase1: 串行计算 {len(sorted_dates)} 个日期因子")
            for dt in sorted_dates:
                if cancel_check and cancel_check():
                    raise RuntimeError('回测已取消')
                try:
                    composites[dt] = self._compute_scores_for_date(dt)
                except Exception as e:
                    logger.warning(f"{dt} 因子计算失败: {e}")
                    composites[dt] = pd.DataFrame(columns=["ts_code", "score"])

        t1 = time.time()
        logger.info(f"自适应调仓 Phase1 完成: {t1 - t0:.1f}s")

        # ---- Phase 2: 串行偏离度判断 + 选股 ----
        signals = {}
        prev_holdings: set[str] = set()
        last_rebalance_idx = -999

        n_base = 0
        n_adaptive = 0
        n_skipped = 0

        for dt in sorted_dates:
            composite = composites.get(dt, pd.DataFrame())
            dtype = all_dates[dt]

            if dtype == "base":
                selected = self._select_from_scores(composite, prev_holdings)
                signals[dt] = selected
                prev_holdings = set(selected["ts_code"].tolist()) if len(selected) > 0 else set()
                last_rebalance_idx = trade_date_to_idx.get(dt, last_rebalance_idx)
                n_base += 1
            else:
                # 检查日：计算偏离度，决定是否触发
                current_idx = trade_date_to_idx.get(dt, 0)
                if current_idx - last_rebalance_idx < REBALANCE_MIN_INTERVAL:
                    continue

                if composite.empty:
                    continue

                new_top = self._select_from_scores(composite, prev_holdings)
                if new_top.empty:
                    continue

                new_codes = set(new_top["ts_code"].tolist())

                if prev_holdings and new_codes:
                    overlap = len(new_codes & prev_holdings)
                    deviation = 1.0 - overlap / len(new_codes)
                else:
                    deviation = 1.0

                if deviation >= REBALANCE_DEVIATION_THRESHOLD:
                    signals[dt] = new_top
                    prev_holdings = new_codes
                    last_rebalance_idx = current_idx
                    n_adaptive += 1
                    logger.info(
                        f"自适应调仓触发: {dt}, 偏离度={deviation:.0%}, "
                        f"新股票={len(new_codes - (prev_holdings - new_codes))}/{len(new_codes)}"
                    )
                else:
                    n_skipped += 1

        self._prev_holdings = prev_holdings

        t2 = time.time()
        logger.info(
            f"自适应调仓完成: {t2 - t0:.1f}s (因子={t1 - t0:.1f}s, 选股={t2 - t1:.1f}s), "
            f"基准={n_base}, 触发={n_adaptive}, 跳过={n_skipped}"
        )
        return signals

    def _generate_signals_sequential(
        self,
        rebalance_dates: list[str],
        cancel_check: Optional[callable] = None,
    ) -> dict[str, pd.DataFrame]:
        """串行生成信号（原始逻辑）。"""
        signals = {}
        for dt in rebalance_dates:
            if cancel_check and cancel_check():
                raise RuntimeError('回测已取消')
            try:
                result = self.select_stocks(dt)
                signals[dt] = result
                if result.empty:
                    logger.info(f"{dt} 空仓信号")
            except Exception as e:
                logger.warning(f"{dt} 选股失败: {e}")
        return signals

    def _generate_signals_parallel(
        self,
        rebalance_dates: list[str],
        max_workers: int,
        cancel_check: Optional[callable] = None,
    ) -> dict[str, pd.DataFrame]:
        """
        并行生成信号：多线程计算因子 → 顺序应用换手惩罚。

        Phase 1: 并行执行 _compute_scores_for_date（重活：因子计算+打分）
        Phase 2: 顺序执行 _select_from_scores（轻活：换手惩罚+Top-N+Softmax）
        """
        import time
        t0 = time.time()

        # Phase 1: 并行因子计算
        composites: dict[str, pd.DataFrame] = {}
        effective_workers = min(max_workers, len(rebalance_dates))
        logger.info(f"并行计算因子: {len(rebalance_dates)} 个日期, {effective_workers} 线程")

        with ThreadPoolExecutor(max_workers=effective_workers) as pool:
            future_map = {}
            for dt in rebalance_dates:
                if cancel_check and cancel_check():
                    raise RuntimeError('回测已取消')
                future = pool.submit(self._compute_scores_for_date, dt)
                future_map[future] = dt

            for future in as_completed(future_map):
                dt = future_map[future]
                try:
                    composites[dt] = future.result()
                except Exception as e:
                    logger.warning(f"{dt} 因子计算失败: {e}")
                    composites[dt] = pd.DataFrame(columns=["ts_code", "score"])

        t1 = time.time()
        logger.info(f"并行因子计算完成: {t1 - t0:.1f}s")

        # Phase 2: 顺序应用换手惩罚 + Top-N 选取
        signals = {}
        prev_holdings: set[str] = set()

        for dt in sorted(composites.keys()):
            composite = composites[dt]
            selected = self._select_from_scores(composite, prev_holdings)
            signals[dt] = selected
            prev_holdings = set(selected["ts_code"].tolist()) if len(selected) > 0 else set()

            if selected.empty:
                logger.info(f"{dt} 空仓信号")

        self._prev_holdings = prev_holdings

        t2 = time.time()
        logger.info(f"顺序选股完成: {t2 - t1:.1f}s, 总计: {t2 - t0:.1f}s")

        return signals
