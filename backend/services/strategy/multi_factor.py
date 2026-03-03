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
    LOG_LEVEL,
)
from backend.services.data.database import DatabaseManager
from backend.services.data.cleaner import get_clean_universe
from backend.services.factors.value import EPFactor, BPFactor
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
from backend.services.factors.processor import process_factor
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
        "value":    ["EP", "BP"],
        "quality":  ["ROE_TTM", "GROSS_MARGIN", "PROFIT_STB", "MARGIN_TREND"],
        "growth":   ["NET_PROFIT_YOY", "REVENUE_YOY", "NET_PROFIT_CAGR_3Y"],
        "momentum": ["MOM_1M", "MOM_3M", "MOM_12M", "REV_5D", "IND_MOM", "RESIDUAL_MOM", "CMDTY_MOM"],
        "technical":["TURN_20D", "VOL_20D", "PRICE_DEV_60D", "SIZE", "VOL_PRICE_DIV"],
        "macro":    ["MACRO_CYCLE", "MACRO_LIQD", "MACRO_INFL", "MACRO_EXTR"],
        "sentiment": ["POLICY_SENT", "POLICY_INTENSITY", "ANALYST_RATING", "ANALYST_COVERAGE"],
    }

    # 大类权重（动量增强 1.3、成长增强 1.2，技术半权 0.5）
    CATEGORY_WEIGHTS = {
        "value": 1.0,
        "quality": 1.0,
        "growth": 1.2,
        "momentum": 1.3,
        "technical": 0.5,
        "macro": 0.6,
        "sentiment": 0.4,
    }

    # 核心财务因子 — 全部缺失则剔除出池
    CORE_FINANCIAL_FACTORS = ["EP", "BP", "ROE_TTM", "GROSS_MARGIN"]

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
            # Phase 7 新因子差异化权重
            self.factor_weights["VOL_20D"] = 0.3          # 降低波动率惩罚（0.5→0.3）
            self.factor_weights["PRICE_DEV_60D"] = 0.15   # 降低偏离度惩罚（0.3→0.15）
            self.factor_weights["REV_5D"] = 0.4
            self.factor_weights["PROFIT_STB"] = 0.5
            self.factor_weights["MARGIN_TREND"] = 0.4
            self.factor_weights["SIZE"] = 0.3
            self.factor_weights["IND_MOM"] = 0.8          # 提高行业动量（0.5→0.8）
            # Phase 8 新因子差异化权重
            self.factor_weights["NET_PROFIT_YOY"] = 1.0   # 提高净利润增速（0.8→1.0）
            self.factor_weights["REVENUE_YOY"] = 0.8      # 提高营收增速（0.6→0.8）
            self.factor_weights["NET_PROFIT_CAGR_3Y"] = 0.8  # 3年复合增长率
            self.factor_weights["RESIDUAL_MOM"] = 0.7
            self.factor_weights["VOL_PRICE_DIV"] = 0.4
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
        获取 regime 感知的大类权重。

        熊市时合并 REGIME_BEAR_OVERRIDES 到 CATEGORY_WEIGHTS。

        Args:
            date: 选股日期。

        Returns:
            大类权重字典。
        """
        if self._regime_detector is None:
            return self.CATEGORY_WEIGHTS

        regime = self._regime_detector.detect(date)
        if regime == "bear" and REGIME_BEAR_OVERRIDES:
            weights = dict(self.CATEGORY_WEIGHTS)
            for cat, w in REGIME_BEAR_OVERRIDES.items():
                if cat in weights:
                    weights[cat] = w
            logger.info(f"Regime bear: 大类权重覆盖 {REGIME_BEAR_OVERRIDES}")
            return weights

        return self.CATEGORY_WEIGHTS

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

        return result

    def select_stocks(self, date: str) -> pd.DataFrame:
        """
        在指定日期进行选股。

        流程：
            1. 构建当日可交易股票池
            2. 计算各因子值
            3. 因子处理（去极值 + 标准化）
            4. 等权合成综合得分
            5. 选取得分最高的 N 只股票

        Args:
            date: 选股日期，格式 YYYY-MM-DD。

        Returns:
            选中的股票 DataFrame，包含 ts_code, score, weight 列。
        """
        logger.info(f"选股: {date}")

        # 1. 构建股票池
        universe = get_clean_universe(self.db, date, min_turnover=0)
        if universe.empty:
            logger.warning(f"{date} 股票池为空")
            return pd.DataFrame()

        logger.info(f"股票池: {len(universe)} 只")

        # 2. 计算各因子值
        factor_scores = {}
        for factor in self.factors:
            try:
                df_factor = factor.compute(date, universe)
                if not df_factor.empty:
                    factor_scores[factor.name] = df_factor
                    valid_count = df_factor["factor_value"].notna().sum()
                    logger.debug(f"  {factor.name}: {valid_count} 个有效值")
            except Exception as e:
                logger.warning(f"因子 {factor.name} 计算失败: {e}")

        if not factor_scores:
            logger.warning(f"{date} 所有因子计算失败")
            return pd.DataFrame()

        # 3. 因子处理 + 合成
        # 获取行业和市值数据（用于中性化）— 使用缓存
        industry_df = self._get_cached_industry_df()
        mktcap_df = self._get_cached_mktcap_df(date)

        # 处理并合并所有因子
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
            # 合并该因子 Z-score（不乘权重，权重在按行业合成时使用）
            processed = processed.rename(columns={"factor_value": fname})
            composite = composite.merge(processed, on="ts_code", how="left")

        factor_cols = [c for c in composite.columns if c != "ts_code"]

        # 4. 核心财务准入过滤 + 大类合成评分（regime 感知权重）
        effective_cat_weights = self._get_regime_cat_weights(date)
        composite = self._compute_scores(composite, factor_cols, industry_df, category_weights=effective_cat_weights)
        composite = composite.drop(columns=["industry_name"], errors="ignore")

        # 过滤掉因子值全缺失的股票
        composite = composite.dropna(subset=["score"])

        # 排除涨停股（不可买入）
        limit_up_codes = universe[universe["is_limit_up"] == 1]["ts_code"].tolist()
        composite = composite[~composite["ts_code"].isin(limit_up_codes)]

        # 换手惩罚：对已持仓股票加分（排序前）
        if self.turnover_penalty_lambda > 0 and self._prev_holdings:
            composite["score"] = composite["score"] + self.turnover_penalty_lambda * composite["ts_code"].isin(self._prev_holdings).astype(float)

        # 5. 选取得分最高的 N 只（允许空仓和不满仓）
        composite = composite.sort_values("score", ascending=False)

        # 过滤低于最低分阈值的股票（允许空仓）
        qualified = composite[composite["score"] >= self.min_select_score]
        selected = qualified.head(self.n_holdings).copy()

        # Softmax 权重分配（温度参数 τ 控制集中度）
        if len(selected) > 0:
            scores = selected["score"].values
            tau = WEIGHT_TEMPERATURE
            if tau > 0:
                shifted = scores - scores.max()    # 数值稳定
                exp_scores = np.exp(shifted / tau)
                raw_w = exp_scores / exp_scores.sum()
            else:
                raw_w = np.ones(len(scores)) / len(scores)  # τ=0 等权
            min_w = 1.0 / (self.n_holdings * 3)
            raw_w = np.maximum(raw_w, min_w)
            selected["weight"] = raw_w / raw_w.sum()

            logger.info(
                f"选股完成: {len(selected)} 只 (softmax τ={tau}), "
                f"得分范围 [{selected['score'].min():.3f}, {selected['score'].max():.3f}]"
            )
        else:
            logger.info(f"选股完成: 0 只入选（空仓），"
                        f"最高分 {composite['score'].max():.3f} < 阈值 {self.min_select_score}")

        # 更新持仓记录（用于下期换手惩罚）
        self._prev_holdings = set(selected["ts_code"].tolist()) if len(selected) > 0 else set()

        return selected[["ts_code", "score", "weight"]] if len(selected) > 0 else pd.DataFrame(columns=["ts_code", "score", "weight"])

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

        # 核心财务准入过滤 + 大类合成评分（regime 感知权重）
        effective_cat_weights = self._get_regime_cat_weights(date)
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
        获取调仓日期列表（每月最后一个交易日）。

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

        df["trade_date"] = pd.to_datetime(df["trade_date"])
        df["year_month"] = df["trade_date"].dt.to_period("M")

        # 每月最后一个交易日
        month_end = df.groupby("year_month")["trade_date"].max()
        dates = [d.strftime("%Y-%m-%d") for d in month_end]

        return dates

    def generate_signals(
        self, start_date: str, end_date: str
    ) -> dict[str, pd.DataFrame]:
        """
        生成回测区间内所有调仓日的选股信号。

        会自动回溯查找 start_date 之前最近一个调仓日，
        以确保回测首日即有持仓（T+1 执行）。

        Args:
            start_date: 回测起始日期。
            end_date: 回测结束日期。

        Returns:
            字典 {调仓日期: 选股结果 DataFrame}。
        """
        rebalance_dates = self.get_rebalance_dates(start_date, end_date)

        # 回溯查找 start_date 之前最近一个调仓日
        # 这样回测首日就能 T+1 执行该信号，避免空仓
        prior_start = (pd.to_datetime(start_date) - pd.DateOffset(months=2)).strftime("%Y-%m-%d")
        prior_end = (pd.to_datetime(start_date) - pd.Timedelta(days=1)).strftime("%Y-%m-%d")
        prior_dates = self.get_rebalance_dates(prior_start, prior_end)
        if prior_dates:
            last_prior = prior_dates[-1]
            rebalance_dates = [last_prior] + rebalance_dates
            logger.info(f"回溯前月调仓日: {last_prior}")

        logger.info(f"回测区间: {start_date} ~ {end_date}, {len(rebalance_dates)} 个调仓日")

        # 初始化缓存（静态数据跨日期复用，日期数据每期清空）
        FactorBase.clear_all_cache()

        signals = {}
        for dt in rebalance_dates:
            # 每个调仓日清空日期相关缓存，保留静态缓存
            FactorBase.clear_date_cache()
            try:
                result = self.select_stocks(dt)
                # 允许空仓信号：空 DataFrame 表示清仓持现金
                signals[dt] = result
                if result.empty:
                    logger.info(f"{dt} 空仓信号（清仓持现金）")
            except Exception as e:
                logger.warning(f"{dt} 选股失败: {e}")

        # 清理所有缓存
        FactorBase.clear_all_cache()

        logger.info(f"信号生成完成: {len(signals)} 期信号（含空仓）")
        return signals
