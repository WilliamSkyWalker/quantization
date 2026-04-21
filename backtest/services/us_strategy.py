"""
US Multi-Factor Stock Selection Strategy

Two-layer scoring model for US equities:
    1. Compute factor values for all stocks in the clean universe
    2. Process factors (winsorize, neutralize by GICS sector, standardize)
    3. Intra-category weighted average (dynamic denominator)
    4. Inter-category weighted sum (missing category weight redistribution)
    5. Apply penalties (value trap, trend filter, missing factor)
    6. Top-N selection with Softmax weight allocation

Rebalancing:
    - Base interval: every US_REBALANCE_INTERVAL trading days
    - Adaptive: deviation trigger between base dates
    - Regime-linked holdings count (bear → fewer holdings)
"""

import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional

import numpy as np
import pandas as pd

from services.config import (
    US_MAX_HOLDINGS,
    US_MIN_SELECT_SCORE,
    US_WEIGHT_TEMPERATURE,
    US_NEUTRALIZE_MODE,
    US_STANDARDIZE_MODE,
    US_CATEGORY_NEUTRALIZE_OVERRIDES,
    US_CATEGORY_WEIGHTS,
    US_REGIME_ENABLED,
    US_REGIME_BEAR_OVERRIDES,
    US_BEAR_HOLDINGS_RATIO,
    US_REBALANCE_INTERVAL,
    US_REBALANCE_MIN_INTERVAL,
    MISSING_FACTOR_THRESHOLD,
    MISSING_FACTOR_MAX_PENALTY,
    MIN_VALID_CATEGORIES,
    US_SHORT_ENABLED,
    US_LONG_N,
    US_SHORT_N,
    US_NET_EXPOSURE,
    US_SHORT_REGIME_GATE,
    US_SHORT_MIN_MCAP,
    US_SHORT_MIN_VOLUME,
    US_SHORT_EPS_REV_PCT,
    US_SHORT_SCORE_PCT,
    US_SHORT_FACTOR_WEIGHTS,
    US_SHORT_BORROW_FEE_TIERS,
    US_USE_OPTIMIZER,
    LOG_LEVEL,
)
from stocks.services.us_cleaner import get_us_clean_universe

# Legacy factor imports (still needed for Quality 批次之外的类别 — 后续批次迁移后移除)
from stocks.services.factors.us_value import EP, BP, DivYield
from stocks.services.factors.us_growth import NetProfitYoY, RevenueYoY, NetProfitCAGR3Y
from stocks.services.factors.us_momentum import Mom1M, Mom3M, Mom12M, Rev5D
from stocks.services.factors.us_technical import Turn20D, Vol20D, Ivol, Size
from stocks.services.factors.us_analyst import USAnalystRating, USAnalystCoverage
from stocks.services.factors.us_accruals import BuybackYield  # Accruals 已迁移到 signals/quality/legacy.py
from stocks.services.factors.us_polymarket import PolymarketSent
from stocks.services.factors.us_quiver import LobbyIntensity, GovContract, WsbSentiment
from stocks.services.factors.us_alphavantage import NewsSentiment, IvSkew, PutCallRatio
from stocks.services.factors.us_insider import InsiderNetBuy
from stocks.services.factors.us_earnings import EarningsSurprise, EpsRevision
from stocks.services.factors.us_base import USFactorBase
from stocks.services.factors.us_processor import process_factor, clear_neutralize_cache

# AlphaSignal registry（Quality 批次已迁移进来；后续批次陆续加入）
import stocks.services.factors.signals  # noqa: F401 — 触发 @register 自动注册
from stocks.services.factors.us_registry import get_active as get_active_signals

from backtest.services.us_regime import USRegimeDetector

logger = logging.getLogger(__name__)
logger.setLevel(LOG_LEVEL)


class USMultiFactorStrategy:
    """
    US multi-factor stock selection strategy.

    Usage:
        db = DatabaseManager()
        strategy = USMultiFactorStrategy(db)
        result = strategy.select_stocks("2024-12-31")
        signals = strategy.generate_signals("2023-01-01", "2024-12-31")
    """

    # ----------------------------------------------------------
    # Factor category definitions
    # ----------------------------------------------------------
    # 非 Quality 类别因子仍用硬编码列表（后续批次会逐步迁移到 registry）
    # Quality 类别已迁移到 AlphaSignal registry，由 __init__ 合并注入
    #
    # Pruned (leave-one-out alpha analysis, 2015-2023):
    #   RESIDUAL_MOM: Δα=-3.46%; VOL_PRICE_DIV: Δα=-4.30%; 4x MACRO: Δα=-0.25% each
    _LEGACY_FACTOR_CATEGORIES = {
        "value":     ["EP", "BP", "DIV_YIELD", "BUYBACK_YIELD"],
        "growth":    ["NET_PROFIT_YOY", "REVENUE_YOY", "NET_PROFIT_CAGR_3Y"],
        "momentum":  ["MOM_1M", "MOM_3M", "MOM_12M", "REV_5D"],
        "technical": ["TURN_20D", "VOL_20D", "IVOL", "SIZE", "IV_SKEW", "PUT_CALL_RATIO"],
        "analyst":   ["US_ANALYST_RATING", "US_ANALYST_COVERAGE", "EARNINGS_SURPRISE", "EPS_REVISION", "INSIDER_NET_BUY"],
        "sentiment": ["POLYMARKET_SENT", "LOBBY_INTENSITY", "GOV_CONTRACT", "NEWS_SENTIMENT"],
        # WSB_SENTIMENT 移除：只有 3 个 ticker（AAPL/GME/TSLA），无截面区分力
    }

    # Default category weights (overridden by US_CATEGORY_WEIGHTS from config)
    CATEGORY_WEIGHTS = US_CATEGORY_WEIGHTS

    # Core financial factors — stocks missing ALL of these are excluded
    CORE_FINANCIAL_FACTORS = ["GROSS_MARGIN"]  # GrossProfit 依赖财报

    FINANCIAL_DEPENDENT_FACTORS = [
        "GROSS_MARGIN", "DIV_YIELD", "BUYBACK_YIELD",  # 依赖财报/公司行动
    ]

    def __init__(
        self,
        db=None,
        n_holdings: int = US_MAX_HOLDINGS,
        factor_weights: Optional[dict[str, float]] = None,
        min_select_score: float = US_MIN_SELECT_SCORE,
        **kwargs,
    ):
        self.n_holdings = n_holdings
        self.min_select_score = min_select_score
        self._prev_holdings: set[str] = set()
        self._prev_weights_dict: dict[str, float] = {}
        self._last_date: str = ""

        # ----------------------------------------------------------
        # Factor instances：AlphaSignal registry 的 active 因子 + 旧式硬编码因子
        # ----------------------------------------------------------
        # Quality 类已全部迁移到 registry，其他类别（value/growth/momentum/technical/
        # analyst/sentiment）暂保留旧实现，逐批迁移。
        alpha_signals = [cls() for cls in get_active_signals().values()]
        legacy_factors = [
            # Value
            EP(), BP(), DivYield(), BuybackYield(),
            # Growth
            NetProfitYoY(), RevenueYoY(), NetProfitCAGR3Y(),
            # Momentum
            Mom1M(), Mom3M(), Mom12M(), Rev5D(),
            # Technical (IV_SKEW, PUT_CALL_RATIO 数据源无历史，不参与评分)
            Turn20D(), Vol20D(), Ivol(), Size(),
            # Analyst
            USAnalystRating(), USAnalystCoverage(),
            EarningsSurprise(), EpsRevision(), InsiderNetBuy(),
            # Sentiment (POLYMARKET_SENT, NEWS_SENTIMENT 数据积累中)
            LobbyIntensity(), GovContract(),
        ]
        self.factors = alpha_signals + legacy_factors

        # ----------------------------------------------------------
        # FACTOR_CATEGORIES / FACTOR_TO_CATEGORY：legacy 静态 + AlphaSignal 动态合并
        # ----------------------------------------------------------
        self.FACTOR_CATEGORIES = {cat: list(fs) for cat, fs in self._LEGACY_FACTOR_CATEGORIES.items()}
        for sig in alpha_signals:
            self.FACTOR_CATEGORIES.setdefault(sig.category, [])
            if sig.name not in self.FACTOR_CATEGORIES[sig.category]:
                self.FACTOR_CATEGORIES[sig.category].append(sig.name)
        self.FACTOR_TO_CATEGORY = {
            f: cat for cat, fs in self.FACTOR_CATEGORIES.items() for f in fs
        }

        # ----------------------------------------------------------
        # 方向锁定集合：由 AlphaSignal.inherent_direction 推导 + legacy 硬编码
        # ----------------------------------------------------------
        # Legacy: TURN_20D/VOL_20D/IVOL 是"高值=坏"的固有反转
        self._INHERENT_REVERSE_SET = {"TURN_20D", "VOL_20D", "IVOL"}
        # Legacy: 旧的 5 个 quality 因子永不反转（已迁移为 AlphaSignal，但保留兼容）
        self._NEVER_REVERSE_SET: set[str] = set()
        for sig in alpha_signals:
            if sig.inherent_direction == -1:
                self._INHERENT_REVERSE_SET.add(sig.name)
            elif sig.inherent_direction == +1:
                self._NEVER_REVERSE_SET.add(sig.name)

        # ----------------------------------------------------------
        # 滚动 IC 窗口：legacy 硬编码 + AlphaSignal.ic_window_months
        # ----------------------------------------------------------
        self._ROLLING_IC_WINDOW = {
            # 基本面（价值/成长）：风格切换慢，24-36M
            "EP": 30, "BP": 30, "DIV_YIELD": 30, "BUYBACK_YIELD": 30,
            "NET_PROFIT_YOY": 24, "REVENUE_YOY": 24, "NET_PROFIT_CAGR_3Y": 36,
            # 动量/技术：信号衰减快，6-12M
            "MOM_1M": 6, "MOM_3M": 9, "MOM_12M": 12, "REV_5D": 6,
            "PRICE_DEV_60D": 9, "SIZE": 24, "VOL_PRICE_DIV": 12,
            # 分析师/盈利：中等节奏，12-18M
            "US_ANALYST_RATING": 18, "US_ANALYST_COVERAGE": 18,
            "EARNINGS_SURPRISE": 18, "EPS_REVISION": 12, "INSIDER_NET_BUY": 12,
            # 情绪/另类：信号生命周期短，6M
            "POLYMARKET_SENT": 6, "LOBBY_INTENSITY": 12, "GOV_CONTRACT": 12,
            "NEWS_SENTIMENT": 6, "IV_SKEW": 6, "PUT_CALL_RATIO": 6,
        }
        # AlphaSignal 覆盖 legacy（同名时以 registry 为准）
        for sig in alpha_signals:
            self._ROLLING_IC_WINDOW[sig.name] = sig.ic_window_months
        self._ROLLING_IC_DEFAULT = 18  # 未列出的因子默认 18 个月

        # ----------------------------------------------------------
        # factor_weights：等权，反向因子权重 -1.0
        # ----------------------------------------------------------
        if factor_weights is None:
            self.factor_weights = {
                f.name: (-1.0 if f.name in self._INHERENT_REVERSE_SET else 1.0)
                for f in self.factors
            }
        else:
            self.factor_weights = factor_weights

        logger.info(
            f"USMultiFactorStrategy init: {len(self.factors)} factors "
            f"({len(alpha_signals)} AlphaSignal + {len(legacy_factors)} legacy), "
            f"{len(self.FACTOR_CATEGORIES)} categories, "
            f"{len(self._INHERENT_REVERSE_SET)} inherent-reverse, "
            f"{len(self._NEVER_REVERSE_SET)} never-reverse"
        )

        # Regime detector
        self._regime_detector = USRegimeDetector(db) if US_REGIME_ENABLED else None
        self._last_regime_strength: float = 1.0

        # MVO optimizer + risk model
        self._use_optimizer = US_USE_OPTIMIZER
        self._risk_model = None
        self._optimizer = None
        if self._use_optimizer:
            from backtest.services.us_risk_model import USRiskModel
            from backtest.services.us_optimizer import USPortfolioOptimizer
            self._risk_model = USRiskModel()
            self._optimizer = USPortfolioOptimizer()
            logger.info("MVO optimizer enabled (Ledoit-Wolf + cvxpy/OSQP)")

        # ML scorer (LightGBM)
        from services.config import US_ML_SCORING_ENABLED, US_ML_BLEND_RATIO
        self._ml_scorer = None
        self._ml_blend_ratio = US_ML_BLEND_RATIO
        self._ml_enabled = US_ML_SCORING_ENABLED
        self._ml_factor_history: dict[str, pd.DataFrame] = {}
        self._ml_last_train_idx = -1

    # ----------------------------------------------------------
    # Cache helpers
    # ----------------------------------------------------------

    def _get_cached_sector_df(self) -> pd.DataFrame | None:
        """Get sector mapping from DB (cached)."""
        cache_key = "_us_sector_map"
        cached = USFactorBase._static_cache.get(cache_key)
        if cached is not None:
            return cached
        try:
            from stocks.models import USIndustryClass
            df = pd.DataFrame(
                USIndustryClass.objects.filter(sector__isnull=False)
                .values_list("ticker", "sector"),
                columns=["ticker", "sector"],
            )
            if not df.empty:
                USFactorBase._static_cache[cache_key] = df
                return df
            logger.debug("_get_cached_sector_df: 行业映射表为空")
        except Exception as e:
            logger.debug(f"_get_cached_sector_df: 获取行业映射失败: {e}")
        return None

    def _get_cached_mktcap_df(self, date: str) -> pd.DataFrame | None:
        """Get historical market cap for a given date (delegates to USFactorBase)."""
        try:
            # Use a temporary factor instance to access get_market_cap()
            factor_instance = self.factors[0] if self.factors else USFactorBase.__new__(USFactorBase)
            df = factor_instance.get_market_cap(date)
            if not df.empty:
                return df
            logger.debug(f"_get_cached_mktcap_df: {date} 市值数据为空")
        except Exception as e:
            logger.debug(f"_get_cached_mktcap_df: 获取市值数据失败: {e}")
        return None

    # ----------------------------------------------------------
    # Regime-aware category weights
    # ----------------------------------------------------------

    def _get_regime_cat_weights(self, date: str) -> dict[str, float]:
        """
        Get regime-aware category weights with gradual interpolation.

        strength=1.0 → bull weights; strength=0.0 → bear weights.
        Linear interpolation in between to avoid whipsaw.
        """
        if self._regime_detector is None:
            return dict(self.CATEGORY_WEIGHTS)

        strength = self._regime_detector.detect_strength(date)
        self._last_regime_strength = strength

        if not US_REGIME_BEAR_OVERRIDES or strength >= 1.0:
            return dict(self.CATEGORY_WEIGHTS)

        weights = {}
        for cat, bull_w in self.CATEGORY_WEIGHTS.items():
            bear_w = US_REGIME_BEAR_OVERRIDES.get(cat, bull_w)
            weights[cat] = bull_w * strength + bear_w * (1.0 - strength)

        logger.info(
            f"US Regime weights (strength={strength:.2f}): "
            + ", ".join(f"{c}={w:.2f}" for c, w in weights.items())
        )
        return weights

    # ----------------------------------------------------------
    # Scoring
    # ----------------------------------------------------------

    def _compute_scores(
        self,
        composite: pd.DataFrame,
        factor_cols: list[str],
        sector_df: pd.DataFrame | None,
        category_weights: dict[str, float] | None = None,
    ) -> pd.DataFrame:
        """
        Two-layer category scoring with missing category weight redistribution.

        1. Core financial admission filter
        2. Intra-category: weighted average with dynamic denominator
        3. Inter-category: weighted sum / sum of valid category weights
        """
        composite = composite.copy()
        effective_cat_weights = category_weights or self.CATEGORY_WEIGHTS

        # 1. Core financial admission filter
        core_cols = [c for c in self.CORE_FINANCIAL_FACTORS if c in factor_cols]
        if core_cols:
            has_financial = composite[core_cols].notna().any(axis=1)
            n_before = len(composite)
            composite = composite[has_financial].copy()
            n_dropped = n_before - len(composite)
            if n_dropped > 0:
                logger.info(f"Core financial filter: dropped {n_dropped} stocks (missing all core financials)")

        if composite.empty:
            logger.debug("_compute_scores: 核心财务过滤后股票池为空，返回空评分")
            composite["score"] = np.nan
            return composite

        # 2. Vectorized scoring
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
        Value trap penalty: compress value score when quality is negative.

        When quality < -0.5 and value > 0, scale value score down.
        """
        try:
            val_idx = cat_names.index("value")
            qual_idx = cat_names.index("quality")
        except ValueError:
            logger.debug("_apply_value_trap_penalty: 缺少 value 或 quality 大类，跳过价值陷阱惩罚")
            return cat_scores

        cat_scores = cat_scores.copy()
        both_valid = cat_has_value[:, val_idx] & cat_has_value[:, qual_idx]
        trap_mask = both_valid & (cat_scores[:, val_idx] > 0) & (cat_scores[:, qual_idx] < -0.5)

        if trap_mask.any():
            penalty = np.clip(1.5 + cat_scores[trap_mask, qual_idx], 0.3, 1.0)
            cat_scores[trap_mask, val_idx] *= penalty
            n_penalized = trap_mask.sum()
            logger.debug(f"Value trap penalty: {n_penalized} stocks")

        return cat_scores

    @staticmethod
    def _apply_missing_factor_penalty(
        final_score: np.ndarray,
        composite: pd.DataFrame,
        factor_cols: list[str],
    ) -> np.ndarray:
        """
        Missing factor penalty: linear score decay when missing ratio exceeds threshold.
        """
        if MISSING_FACTOR_THRESHOLD >= 1.0 or MISSING_FACTOR_MAX_PENALTY <= 0:
            return final_score

        n_total = len(factor_cols)
        if n_total == 0:
            return final_score

        n_valid = composite[factor_cols].notna().sum(axis=1).values
        missing_ratio = 1.0 - n_valid / n_total

        excess = np.clip(missing_ratio - MISSING_FACTOR_THRESHOLD, 0, None)
        max_excess = 1.0 - MISSING_FACTOR_THRESHOLD
        penalty = 1.0 - (excess / max_excess) * MISSING_FACTOR_MAX_PENALTY

        result = final_score.copy() if isinstance(final_score, np.ndarray) else np.array(final_score)
        valid_mask = ~np.isnan(result)
        result[valid_mask] *= penalty[valid_mask] if isinstance(penalty, np.ndarray) else penalty

        n_penalized = (valid_mask & (penalty < 1.0)).sum()
        if n_penalized > 0:
            logger.info(f"Missing factor penalty: {n_penalized} stocks penalized")

        return result

    # ----------------------------------------------------------
    # 滚动 IC 动态方向
    # ----------------------------------------------------------

    # 因子固有方向（因子定义上高值=负信号，不受 IC 控制）
    # _INHERENT_REVERSE_SET / _NEVER_REVERSE_SET / _ROLLING_IC_WINDOW / _ROLLING_IC_DEFAULT
    # 已改为 __init__ 中实例属性，由 AlphaSignal registry 推导 + legacy 硬编码合并生成。

    # IC → 连续权重的缩放参数
    _IC_SCALE_REF = 0.02       # IC 均值达到此值时权重 = 1.0
    _IC_WEIGHT_MIN = 0.2       # 最低权重（IC ≈ 0 时）
    _IC_WEIGHT_MAX = 2.0       # 最高权重
    _IC_EMA_HALFLIFE = 6       # EMA 半衰期（月），越小对近期越敏感

    def _update_rolling_ic_weights(
        self, date: str, composite: pd.DataFrame, factor_cols: list[str],
    ):
        """
        根据分因子滚动 IC，动态决定每个因子的**连续权重**。

        三层改进（v2）：
        1. 连续权重：weight = sign(ema_ic) × clip(|ema_ic| / IC_SCALE_REF, MIN, MAX)
           - 强 IC → 高权重（最高 2.0）
           - 弱 IC → 低权重（最低 0.2，不完全静默）
           - 方向翻转时平滑过渡，不是突然 +1 → -1
        2. EMA 替代平均：半衰期 6 个月，近期 IC 权重更高，捕捉方向变化更快
        3. 置信度缩放：观测不足时自动降权

        固有方向因子（_INHERENT_REVERSE_SET / _NEVER_REVERSE_SET）不受 IC 影响。
        """
        date_ts = pd.to_datetime(date)

        # 初始化滚动 IC 存储
        if not hasattr(self, "_rolling_ic_window"):
            self._rolling_ic_window = {}  # {factor_name: [ic_values]}
            self._prev_factor_snapshot = None
            self._prev_date = None

        # Step 1: 用上一期快照 + 本期收益计算 IC（回看，不前看）
        bulk_daily = USFactorBase._static_cache.get("_bulk_daily")
        if (self._prev_factor_snapshot is not None
                and self._prev_date is not None
                and bulk_daily is not None
                and not bulk_daily.empty):
            prev_ts = pd.to_datetime(self._prev_date)

            # 上一期价格
            mask1 = (bulk_daily["trade_date"] >= prev_ts - pd.Timedelta(days=5)) & \
                     (bulk_daily["trade_date"] <= prev_ts)
            px_prev = bulk_daily[mask1].sort_values("trade_date").groupby("ticker").tail(1)[["ticker", "adj_close"]]

            # 本期价格
            mask2 = (bulk_daily["trade_date"] >= date_ts - pd.Timedelta(days=5)) & \
                     (bulk_daily["trade_date"] <= date_ts)
            px_now = bulk_daily[mask2].sort_values("trade_date").groupby("ticker").tail(1)[["ticker", "adj_close"]]

            if not px_prev.empty and not px_now.empty:
                px_prev.columns = ["ticker", "px_prev"]
                px_now.columns = ["ticker", "px_now"]
                fwd_ret = px_prev.merge(px_now, on="ticker")
                fwd_ret["ret"] = fwd_ret["px_now"] / fwd_ret["px_prev"] - 1

                prev_snap = self._prev_factor_snapshot
                for fname in prev_snap.columns:
                    if fname == "ticker":
                        continue
                    if fname in self._INHERENT_REVERSE_SET or fname in self._NEVER_REVERSE_SET:
                        continue

                    merged = prev_snap[["ticker", fname]].merge(
                        fwd_ret[["ticker", "ret"]], on="ticker"
                    ).dropna()
                    if len(merged) < 30:
                        continue

                    ic = merged[fname].corr(merged["ret"], method="spearman")
                    if not np.isnan(ic):
                        if fname not in self._rolling_ic_window:
                            self._rolling_ic_window[fname] = []
                        self._rolling_ic_window[fname].append(ic)
                        # 按因子类型保持不同窗口大小
                        max_window = self._ROLLING_IC_WINDOW.get(fname, self._ROLLING_IC_DEFAULT)
                        if len(self._rolling_ic_window[fname]) > max_window:
                            self._rolling_ic_window[fname] = \
                                self._rolling_ic_window[fname][-max_window:]

        # Step 2: 保存本期因子快照（供下一期计算 IC）
        snap_cols = ["ticker"] + [f for f in factor_cols if f in composite.columns]
        self._prev_factor_snapshot = composite[snap_cols].copy()
        self._prev_date = date

        # Step 3: 用 EMA IC 计算连续权重
        alpha = 1.0 - np.exp(-np.log(2) / self._IC_EMA_HALFLIFE)  # EMA 衰减系数

        changes = []
        for fname in factor_cols:
            if fname in self._INHERENT_REVERSE_SET:
                new_w = -1.0
            elif fname in self._NEVER_REVERSE_SET:
                new_w = 1.0
            elif fname in self._rolling_ic_window:
                ic_series = self._rolling_ic_window[fname]
                max_window = self._ROLLING_IC_WINDOW.get(fname, self._ROLLING_IC_DEFAULT)
                min_obs = max(4, max_window // 4)

                if len(ic_series) >= min_obs:
                    # EMA（对近期 IC 更敏感）
                    ema = ic_series[0]
                    for ic_val in ic_series[1:]:
                        ema = alpha * ic_val + (1 - alpha) * ema

                    # 连续权重：|ema| / ref → 缩放到 [MIN, MAX]
                    magnitude = min(abs(ema) / self._IC_SCALE_REF, self._IC_WEIGHT_MAX)
                    magnitude = max(magnitude, self._IC_WEIGHT_MIN)
                    new_w = np.sign(ema) * magnitude if abs(ema) > 1e-6 else self._IC_WEIGHT_MIN

                    # 置信度缩放：观测少时降权
                    confidence = min(len(ic_series) / max(min_obs * 2, 1), 1.0)
                    new_w *= confidence
                else:
                    new_w = self._IC_WEIGHT_MIN  # 冷启动：低权重而非默认 +1
            else:
                new_w = self._IC_WEIGHT_MIN  # 无 IC 数据：低权重

            if fname in self.factor_weights:
                old_w = self.factor_weights[fname]
                if abs(old_w - new_w) > 0.1:
                    changes.append(f"{fname} {old_w:+.2f}→{new_w:+.2f}")
                self.factor_weights[fname] = new_w

        if changes:
            logger.info(f"Rolling IC 权重变更 ({len(changes)}): {', '.join(changes[:10])}"
                        + (f" ...+{len(changes)-10}" if len(changes) > 10 else ""))

    def _apply_financial_staleness_decay(
        self,
        date: str,
        composite: pd.DataFrame,
        factor_cols: list[str],
    ) -> pd.DataFrame:
        """
        Financial data staleness decay based on filing recency.

        Decay rules by months since last report:
            <= 3 months: 100%
            3~6 months: 50%
            6~9 months: 25%
            > 9 months: negative penalty (-1.0)
        """
        fin_cols = [f for f in self.FINANCIAL_DEPENDENT_FACTORS if f in factor_cols]
        if not fin_cols:
            logger.debug("_apply_financial_staleness_decay: 无财务依赖因子，跳过衰减")
            return composite

        # Get latest filing date per ticker from preloaded data
        bulk_fin = USFactorBase._static_cache.get("_bulk_financial")
        if bulk_fin is not None and not bulk_fin.empty:
            date_ts = pd.to_datetime(date)
            codes_set = set(composite["ticker"].tolist())
            df_fin = bulk_fin[
                (bulk_fin["filing_date"] <= date_ts) & bulk_fin["ticker"].isin(codes_set)
            ]
            if df_fin.empty:
                logger.debug(f"_apply_financial_staleness_decay: {date} 无财报数据，跳过衰减")
                return composite
            df_latest = df_fin.groupby("ticker")["date"].max().reset_index()
            df_latest.columns = ["ticker", "latest_end_date"]
        else:
            logger.warning("_apply_financial_staleness_decay: 缓存为空，请先调用 preload_for_backtest()")
            return composite

        if df_latest.empty:
            logger.debug(f"_apply_financial_staleness_decay: {date} 无财报日期数据，跳过衰减")
            return composite

        composite = composite.copy()
        ref_date = pd.to_datetime(date)
        df_latest["latest_end_date"] = pd.to_datetime(df_latest["latest_end_date"])
        df_latest["months_stale"] = (
            (ref_date - df_latest["latest_end_date"]).dt.days / 30.44
        )

        composite = composite.merge(
            df_latest[["ticker", "months_stale"]], on="ticker", how="left"
        )
        composite["months_stale"] = composite["months_stale"].fillna(99)

        months = composite["months_stale"].values
        decay = np.where(
            months <= 3, 1.0,
            np.where(months <= 6, 0.5,
                np.where(months <= 9, 0.25, 0.0))
        )

        stale_penalty = -1.0
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
                f"Financial staleness decay: {n_decayed} decayed, {n_penalized} penalized"
            )

        composite = composite.drop(columns=["months_stale"])
        return composite

    def _compute_scores_vectorized(
        self,
        composite: pd.DataFrame,
        factor_cols: list[str],
        effective_cat_weights: dict[str, float],
    ) -> np.ndarray:
        """Vectorized two-layer scoring (universal weights)."""
        n_stocks = len(composite)
        n_cats = len(self.FACTOR_CATEGORIES)
        cat_names = list(self.FACTOR_CATEGORIES.keys())

        cat_scores = np.full((n_stocks, n_cats), np.nan)
        cat_has_value = np.zeros((n_stocks, n_cats), dtype=bool)

        for cat_idx, cat in enumerate(cat_names):
            factors = self.FACTOR_CATEGORIES[cat]
            cat_factor_cols = [f for f in factors if f in factor_cols]
            if not cat_factor_cols:
                continue

            fw = np.array([self.factor_weights.get(f, 1.0) for f in cat_factor_cols])
            values = composite[cat_factor_cols].values.astype(float)

            valid = ~np.isnan(values)
            weighted_sum = np.nansum(values * fw, axis=1)
            weight_denom = (valid * np.abs(fw)).sum(axis=1)

            has_value = weight_denom > 0
            cat_has_value[:, cat_idx] = has_value
            cat_scores[has_value, cat_idx] = weighted_sum[has_value] / weight_denom[has_value]

        # Value trap penalty
        cat_scores = self._apply_value_trap_penalty(cat_scores, cat_has_value, cat_names)

        # Category weights
        cat_weight_arr = np.array([effective_cat_weights.get(c, 1.0) for c in cat_names])

        # Weighted category scores
        weighted_cat = np.where(cat_has_value, cat_scores * cat_weight_arr, 0.0)
        total_score = weighted_cat.sum(axis=1)

        # Valid category count
        n_valid_cats = cat_has_value.sum(axis=1)
        weight_denom_total = (cat_has_value * np.abs(cat_weight_arr)).sum(axis=1)

        # Final score with minimum valid category requirement
        final_score = np.where(
            (n_valid_cats >= MIN_VALID_CATEGORIES) & (weight_denom_total > 0),
            total_score / weight_denom_total,
            np.nan,
        )

        # Missing factor penalty
        final_score = self._apply_missing_factor_penalty(
            final_score, composite, factor_cols
        )

        return final_score

    # ----------------------------------------------------------
    # Factor computation for a single date
    # ----------------------------------------------------------

    def _compute_scores_for_date(self, date: str) -> pd.DataFrame:
        """
        Compute full factor scores for a given date (thread-safe, no top-N selection).

        Returns:
            DataFrame[ticker, score], or empty DataFrame.
        """
        logger.info(f"Computing US factors: {date}")

        clear_neutralize_cache()

        # 1. Build universe (cached)
        cache_key = f"_us_universe_{date}"
        cached_univ = USFactorBase._date_cache.get(cache_key)
        if cached_univ is not None:
            universe = cached_univ
        else:
            universe = get_us_clean_universe(date)
            USFactorBase._date_cache[cache_key] = universe
        if universe.empty:
            logger.warning(f"{date} US universe is empty")
            return pd.DataFrame(columns=["ticker", "score"])

        # 2. Compute factors
        factor_scores = {}
        for factor in self.factors:
            try:
                df_factor = factor.compute(date, universe)
                if not df_factor.empty:
                    factor_scores[factor.name] = df_factor
            except Exception as e:
                logger.warning(f"Factor {factor.name} failed: {e}")

        if not factor_scores:
            logger.warning(f"{date} all US factors failed")
            return pd.DataFrame(columns=["ticker", "score"])

        # 3. Process factors + compose
        sector_df = self._get_cached_sector_df()
        mktcap_df = self._get_cached_mktcap_df(date)

        all_tickers = universe["ticker"].tolist()
        composite = pd.DataFrame({"ticker": all_tickers})

        for fname, df_raw in factor_scores.items():
            cat = self.FACTOR_TO_CATEGORY.get(fname)
            effective_neutralize = US_CATEGORY_NEUTRALIZE_OVERRIDES.get(cat, US_NEUTRALIZE_MODE)
            processed = process_factor(
                df_raw,
                industry_df=sector_df,
                mktcap_df=mktcap_df,
                do_neutralize=(mktcap_df is not None),
                neutralize_mode=effective_neutralize,
                nonlinear_size=False,
                standardize_mode=US_STANDARDIZE_MODE,
            )
            processed = processed.rename(columns={"factor_value": fname})
            composite = composite.merge(processed, on="ticker", how="left")

        factor_cols = [c for c in composite.columns if c != "ticker"]

        # 3.5a. 滚动 IC 动态方向（trailing 36M，每月调仓时重算）
        self._update_rolling_ic_weights(date, composite, factor_cols)

        # 3.5b. Financial staleness decay (post-standardize, pre-compose)
        composite = self._apply_financial_staleness_decay(date, composite, factor_cols)

        # 4. Category scoring (regime-aware)
        effective_cat_weights = self._get_regime_cat_weights(date)
        composite = self._compute_scores(composite, factor_cols, sector_df, category_weights=effective_cat_weights)

        composite = composite.dropna(subset=["score"])

        # 5. Trend threshold filter: MOM_12M < -1.0 → score penalty
        if "MOM_12M" in composite.columns:
            mom12 = composite["MOM_12M"]
            trend_penalty_mask = mom12 < -1.0
            if trend_penalty_mask.any():
                penalty = np.clip(1.0 + 0.3 * mom12[trend_penalty_mask], 0.3, 0.7)
                composite.loc[trend_penalty_mask, "score"] *= penalty
                n_penalized = trend_penalty_mask.sum()
                logger.info(f"Trend filter: {n_penalized} stocks penalized")

        # 6. Sector-level trend filter: sector median MOM_12M < -0.5
        if "MOM_12M" in composite.columns and sector_df is not None:
            sec_merged = composite[["ticker", "MOM_12M", "score"]].merge(
                sector_df[["ticker", "sector"]], on="ticker", how="left"
            )
            sec_merged["sector"] = sec_merged["sector"].fillna("Unknown")
            sec_mom = sec_merged.groupby("sector")["MOM_12M"].median()
            bad_sectors = sec_mom[sec_mom < -0.5].index.tolist()
            if bad_sectors:
                bad_mask = sec_merged["sector"].isin(bad_sectors)
                bad_tickers = sec_merged.loc[bad_mask, "ticker"].tolist()
                ticker_mask = composite["ticker"].isin(bad_tickers)
                if ticker_mask.any():
                    ticker_to_sec = sec_merged.set_index("ticker")["sector"]
                    sec_penalty_map = sec_mom[bad_sectors].clip(-3.0, -0.5)
                    sec_penalty_val = 0.8 + (sec_penalty_map - (-0.5)) / (-2.0 - (-0.5)) * (0.4 - 0.8)
                    sec_penalty_val = sec_penalty_val.clip(0.4, 0.8)
                    stock_secs = ticker_to_sec.reindex(composite.loc[ticker_mask, "ticker"])
                    stock_penalty = stock_secs.map(sec_penalty_val).values
                    composite.loc[ticker_mask, "score"] *= stock_penalty
                    logger.info(
                        f"Sector trend filter: {len(bad_sectors)} sectors "
                        f"({', '.join(bad_sectors[:5])}), "
                        f"{ticker_mask.sum()} stocks penalized"
                    )

        logger.info(f"{date} US scoring done: {len(composite)} valid stocks")

        # ML scoring blend (if enabled and trained)
        if self._ml_enabled and self._ml_scorer is not None and self._ml_scorer.model is not None:
            try:
                # Build feature matrix aligned with composite rows
                feature_df = composite[["ticker"]].copy()
                for col in self._ml_scorer.feature_cols:
                    feature_df[col] = composite[col] if col in composite.columns else 0.0
                ml_scores = self._ml_scorer.predict(
                    feature_df[self._ml_scorer.feature_cols]
                ).values
                linear_scores = composite["score"].values
                alpha = self._ml_blend_ratio
                composite["score"] = (1 - alpha) * linear_scores + alpha * ml_scores
                logger.info(f"ML blend applied: α={alpha:.1f}")
            except Exception as e:
                logger.warning(f"ML scoring failed, using linear only: {e}")

        # 记录因子截面用于 ML 训练
        if self._ml_enabled:
            self._ml_factor_history[date] = composite.copy()

        # 保留空头独立因子列（供 _compute_short_score 使用）
        keep_cols = ["ticker", "score"]
        for col in US_SHORT_FACTOR_WEIGHTS:
            if col != "BORROW_COST" and col in composite.columns:
                keep_cols.append(col)
        return composite[keep_cols]

    # ----------------------------------------------------------
    # Short-side independent scoring
    # ----------------------------------------------------------

    def _compute_short_score(self, composite: pd.DataFrame) -> pd.Series:
        """
        Compute independent short score from dedicated short factors.

        Unlike the long composite score (predicts "will go up"), this predicts
        "will go down" using a separate factor subset and fixed weights.

        Returns:
            Series indexed like composite, higher = more suitable for shorting.
        """
        weights = US_SHORT_FACTOR_WEIGHTS
        short_score = pd.Series(0.0, index=composite.index)
        n_factors = 0

        # Factor direction mapping for short prediction:
        #   EPS_REVISION: negative revision → short signal → negate
        #   ACCRUALS: high accruals → short signal → keep positive
        #   EARNINGS_SURPRISE: miss → short signal → negate
        #   INSIDER_NET_BUY: net selling → short signal → negate
        direction = {
            "EPS_REVISION": -1,
            "ACCRUALS": 1,
            "EARNINGS_SURPRISE": -1,
            "INSIDER_NET_BUY": -1,
        }

        for factor_name, w in weights.items():
            if factor_name == "BORROW_COST":
                continue  # handled separately below
            if factor_name not in composite.columns:
                logger.debug(f"_compute_short_score: {factor_name} not in composite, skipping")
                continue

            raw = composite[factor_name].copy()
            d = direction.get(factor_name, 1)
            vals = raw * d

            # Z-score within the short candidate pool
            mean = vals.mean()
            std = vals.std()
            if std > 1e-10:
                z = (vals - mean) / std
            else:
                z = pd.Series(0.0, index=composite.index)

            short_score += w * z
            n_factors += 1

        if n_factors == 0:
            logger.warning("_compute_short_score: no valid short factors available")
            return short_score

        # BORROW_COST factor (negative weight — penalize expensive borrows)
        if "BORROW_COST" in composite.columns and "BORROW_COST" in weights:
            bc = composite["BORROW_COST"]
            bc_mean = bc.mean()
            bc_std = bc.std()
            if bc_std > 1e-10:
                bc_z = (bc - bc_mean) / bc_std
            else:
                bc_z = pd.Series(0.0, index=composite.index)
            short_score += weights["BORROW_COST"] * bc_z

        return short_score

    # ----------------------------------------------------------
    # Top-N selection + Softmax weights
    # ----------------------------------------------------------

    @staticmethod
    def _softmax_weights(scores: np.ndarray, tau: float, min_w: float) -> np.ndarray:
        """Apply Softmax to scores and return normalized weights (all positive)."""
        if len(scores) == 0:
            logger.debug("_softmax_weights: 输入得分为空，返回空数组")
            return np.array([])
        if tau > 0:
            shifted = scores - scores.max()
            exp_s = np.exp(shifted / tau)
            w = exp_s / exp_s.sum()
        else:
            w = np.ones(len(scores)) / len(scores)
        w = np.maximum(w, min_w)
        return w / w.sum()

    # Maximum net weight any single GICS sector can have
    MAX_SECTOR_NET_WEIGHT = 0.15

    def _select_from_scores(
        self, composite: pd.DataFrame, prev_holdings: set[str],
    ) -> pd.DataFrame:
        """
        Dispatch to MVO optimizer or TopN fallback based on US_USE_OPTIMIZER.

        Returns:
            DataFrame[ticker, score, weight, side].
        """
        if self._use_optimizer and self._optimizer is not None:
            result = self._select_from_scores_mvo(composite, prev_holdings)
            if result is not None and not result.empty:
                return result
            logger.warning("MVO 失败，降级到 Top-N + Softmax")
        return self._select_from_scores_topn(composite, prev_holdings)

    def _select_from_scores_mvo(
        self, composite: pd.DataFrame, prev_holdings: set[str],
    ) -> pd.DataFrame | None:
        """
        MVO-based portfolio construction using risk model + optimizer.

        Returns:
            DataFrame[ticker, score, weight, side] or None if optimization fails.
        """
        empty = pd.DataFrame(columns=["ticker", "score", "weight", "side"])
        if composite.empty:
            return empty

        date = self._last_date
        universe = composite["ticker"].tolist()

        # 1. Estimate covariance matrix
        cov_matrix, cov_tickers = self._risk_model.estimate(date, universe)
        if len(cov_tickers) < 2:
            logger.warning(f"MVO: 协方差矩阵有效股票不足 ({len(cov_tickers)})")
            return None

        # 2. Build score vector (only tickers in cov)
        cov_set = set(cov_tickers)
        scored = composite[composite["ticker"].isin(cov_set)].copy()
        scores = scored.set_index("ticker")["score"]

        # 3. Build previous weights dict
        prev_weights = {}
        if hasattr(self, '_prev_weights_dict'):
            prev_weights = self._prev_weights_dict

        # 4. Build sector map
        sector_map = {}
        sector_df = self._get_cached_sector_df()
        if sector_df is not None and not sector_df.empty:
            sector_map = dict(zip(sector_df["ticker"], sector_df["sector"]))

        # 5. Run optimizer
        opt_weights = self._optimizer.optimize(
            scores=scores,
            cov_matrix=cov_matrix,
            cov_tickers=cov_tickers,
            prev_weights=prev_weights,
            sector_map=sector_map,
            short_enabled=US_SHORT_ENABLED,
        )

        if not opt_weights:
            return None

        # 6. Build output DataFrame
        rows = []
        score_map = dict(zip(composite["ticker"], composite["score"]))
        for ticker, weight in opt_weights.items():
            rows.append({
                "ticker": ticker,
                "score": score_map.get(ticker, 0.0),
                "weight": weight,
                "side": "LONG" if weight > 0 else "SHORT",
            })

        result = pd.DataFrame(rows)

        # Store weights for next period's turnover penalty
        self._prev_weights_dict = opt_weights

        n_long = (result["weight"] > 0).sum()
        n_short = (result["weight"] < 0).sum()
        net = result["weight"].sum()
        gross = result["weight"].abs().sum()
        logger.info(
            f"MVO selection: {n_long}L/{n_short}S, net={net:.2f}, "
            f"gross={gross:.2f}, regime={self._last_regime_strength:.2f}"
        )

        return result[["ticker", "score", "weight", "side"]]

    def _select_from_scores_topn(
        self, composite: pd.DataFrame, prev_holdings: set[str],
    ) -> pd.DataFrame:
        """
        Top-N + Softmax fallback (original v3 implementation).

        Score-ranked L/S with soft sector cap: top-N long, bottom-M short,
        then clip any sector whose net weight exceeds MAX_SECTOR_NET_WEIGHT.

        Returns:
            DataFrame[ticker, score, weight, side].
        """
        empty = pd.DataFrame(columns=["ticker", "score", "weight", "side"])
        if composite.empty:
            return empty

        composite = composite.copy()
        composite = composite.sort_values("score", ascending=False)
        tau = US_WEIGHT_TEMPERATURE
        strength = self._last_regime_strength

        # === Long leg ===
        # Long count is FIXED (not regime-dynamic) to preserve v3 behavior.
        # Regime strength only controls short gate and short allocation.
        long_qualified = composite[composite["score"] >= self.min_select_score]
        long_n = US_LONG_N if US_SHORT_ENABLED else self.n_holdings
        effective_long_n = long_n

        long_selected = long_qualified.head(effective_long_n).copy()
        min_w = 1.0 / (max(long_n, 1) * 3)

        if not US_SHORT_ENABLED:
            if len(long_selected) > 0:
                long_selected["weight"] = self._softmax_weights(
                    long_selected["score"].values, tau, min_w
                )
                long_selected["side"] = "LONG"
            return long_selected[["ticker", "score", "weight", "side"]] if len(long_selected) > 0 else empty

        # === Short leg v5: independent short factor model (always-on) ===
        short_selected = pd.DataFrame()

        # Build short candidate pool: mcap >= $10B (historical market cap)
        short_date = getattr(self, '_last_date', '')
        mktcap_df = self._get_cached_mktcap_df(short_date)

        short_pool = composite.copy()
        if mktcap_df is not None and not mktcap_df.empty:
            short_pool = short_pool.merge(
                mktcap_df[["ticker", "market_cap"]], on="ticker", how="left"
            )
            before_n = len(short_pool)
            short_pool = short_pool[
                short_pool["market_cap"].fillna(0) >= US_SHORT_MIN_MCAP
            ]
            logger.debug(
                f"Short mcap filter: {before_n} → {len(short_pool)} "
                f"(>= ${US_SHORT_MIN_MCAP/1e9:.0f}B)"
            )

            # Assign tiered borrow cost
            short_pool["BORROW_COST"] = 0.015  # default
            for mcap_threshold, rate in sorted(
                US_SHORT_BORROW_FEE_TIERS.items(), reverse=True
            ):
                short_pool.loc[
                    short_pool["market_cap"] >= mcap_threshold, "BORROW_COST"
                ] = rate
        else:
            logger.debug("Short pool: no mktcap data, using all stocks with default borrow cost")
            short_pool["BORROW_COST"] = 0.015

        if len(short_pool) < 3:
            logger.info(f"Short pool too small ({len(short_pool)}), skipping shorts")
        else:
            # Compute independent short score
            short_pool["short_score"] = self._compute_short_score(short_pool)

            # INTERSECTION filter (3 conditions):
            # 1. short_score > Nth percentile
            score_cutoff = short_pool["short_score"].quantile(
                1.0 - US_SHORT_SCORE_PCT
            )
            # 2. EPS_REVISION gatekeeper: worst N%
            # 3. composite score <= 0 (don't short stocks the long model likes)
            base_mask = (
                (short_pool["short_score"] >= score_cutoff)
                & (short_pool["score"] <= 0)
            )
            if "EPS_REVISION" in short_pool.columns:
                eps_cutoff = short_pool["EPS_REVISION"].quantile(
                    US_SHORT_EPS_REV_PCT
                )
                candidates = short_pool[
                    base_mask & (short_pool["EPS_REVISION"] <= eps_cutoff)
                ]
            else:
                logger.warning("Short selection: EPS_REVISION not available, using short_score only")
                candidates = short_pool[base_mask]

            candidates = candidates.sort_values("short_score", ascending=False)
            short_selected = candidates.head(US_SHORT_N).copy()

            logger.info(
                f"Short selection: {len(short_pool)} pool → "
                    f"{len(candidates)} candidates → {len(short_selected)} selected "
                    f"(regime={strength:.2f})"
                )

        if len(long_selected) == 0 and len(short_selected) == 0:
            return empty

        # === Weight allocation (fixed net exposure, v3 behavior) ===
        has_shorts = len(short_selected) > 0
        if has_shorts:
            net_exp = US_NET_EXPOSURE  # fixed 0.6 = 80% long / 20% short
            long_total = (1.0 + net_exp) / 2.0
            short_total = (1.0 - net_exp) / 2.0
        else:
            # No shorts: all weight to longs
            net_exp = 1.0
            long_total = 1.0
            short_total = 0.0

        if len(long_selected) > 0:
            long_selected["weight"] = self._softmax_weights(
                long_selected["score"].values, tau, min_w
            ) * long_total
            long_selected["side"] = "LONG"

        if has_shorts:
            # Softmax weights (v3 behavior): worse short_score → higher weight
            short_scores = short_selected["short_score"].values if "short_score" in short_selected.columns else -short_selected["score"].values
            short_min_w = 1.0 / (max(US_SHORT_N, 1) * 3)
            short_selected["weight"] = -self._softmax_weights(
                short_scores, tau, short_min_w
            ) * short_total
            short_selected["side"] = "SHORT"

        result = pd.concat(
            [long_selected] + ([short_selected] if has_shorts else []),
            ignore_index=True,
        )

        # === Soft sector cap: clip sectors exceeding MAX_SECTOR_NET_WEIGHT ===
        sector_df = self._get_cached_sector_df()
        if sector_df is not None and not result.empty:
            result = result.merge(sector_df, on="ticker", how="left")
            result["sector"] = result["sector"].fillna("Unknown")

            for _ in range(3):  # iterate to converge
                sector_net = result.groupby("sector")["weight"].sum()
                over = sector_net[sector_net.abs() > self.MAX_SECTOR_NET_WEIGHT]
                if over.empty:
                    break
                for sec, net_w in over.items():
                    mask = result["sector"] == sec
                    if net_w > self.MAX_SECTOR_NET_WEIGHT:
                        # Scale down longs in this sector
                        long_mask = mask & (result["weight"] > 0)
                        if long_mask.any():
                            scale = self.MAX_SECTOR_NET_WEIGHT / net_w
                            result.loc[long_mask, "weight"] *= scale
                    elif net_w < -self.MAX_SECTOR_NET_WEIGHT:
                        short_mask = mask & (result["weight"] < 0)
                        if short_mask.any():
                            scale = self.MAX_SECTOR_NET_WEIGHT / abs(net_w)
                            result.loc[short_mask, "weight"] *= scale

            # Renormalize to target totals
            long_sum = result.loc[result["weight"] > 0, "weight"].sum()
            short_sum = result.loc[result["weight"] < 0, "weight"].sum()
            if long_sum > 0:
                result.loc[result["weight"] > 0, "weight"] *= long_total / long_sum
            if short_sum < 0:
                result.loc[result["weight"] < 0, "weight"] *= short_total / abs(short_sum)

            if "sector" in result.columns:
                result = result.drop(columns=["sector"])

        n_long = (result["weight"] > 0).sum()
        n_short = (result["weight"] < 0).sum()
        logger.info(
            f"L/S selection (sector cap {self.MAX_SECTOR_NET_WEIGHT:.0%}): "
            f"{n_long}L / {n_short}S, net={net_exp:.0%}, regime={strength:.2f}"
        )
        # Drop extra columns, keep only standard output
        out_cols = ["ticker", "score", "weight", "side"]
        return result[[c for c in out_cols if c in result.columns]]

    # ----------------------------------------------------------
    # Public API: select_stocks
    # ----------------------------------------------------------

    def select_stocks(self, date: str) -> pd.DataFrame:
        """
        Run full stock selection for a single date.

        Args:
            date: Selection date, format YYYY-MM-DD.

        Returns:
            DataFrame[ticker, score, weight].
        """
        self._last_date = date
        composite = self._compute_scores_for_date(date)
        selected = self._select_from_scores(composite, self._prev_holdings)
        self._prev_holdings = set(selected["ticker"].tolist()) if len(selected) > 0 else set()
        # Update prev_weights_dict for next period's turnover penalty
        if len(selected) > 0:
            self._prev_weights_dict = dict(zip(selected["ticker"], selected["weight"]))
        else:
            self._prev_weights_dict = {}

        if len(selected) > 0:
            logger.info(
                f"US selection done: {len(selected)} stocks, "
                f"score range [{selected['score'].min():.3f}, {selected['score'].max():.3f}]"
            )
        else:
            logger.info("US selection done: 0 stocks (empty portfolio)")

        return selected

    # ----------------------------------------------------------
    # Rebalance dates
    # ----------------------------------------------------------

    def get_rebalance_dates(self, start_date: str, end_date: str) -> list[str]:
        """
        Get rebalance dates from us_index_daily (^GSPC trading calendar).

        Returns:
            List of rebalance date strings.
        """
        from stocks.models import USIndexDaily
        dates = list(
            USIndexDaily.objects.filter(
                index_code="^GSPC",
                trade_date__gte=start_date,
                trade_date__lte=end_date,
            ).values_list("trade_date", flat=True).distinct().order_by("trade_date")
        )

        if not dates:
            logger.debug("get_rebalance_dates: 无交易日数据，返回空列表")
            return []

        trading_days = sorted(d.strftime("%Y-%m-%d") for d in dates)
        return trading_days[::US_REBALANCE_INTERVAL]

    def _get_all_trade_dates(self, start_date: str, end_date: str) -> list[str]:
        """Get all US trading days in range from us_index_daily."""
        from stocks.models import USIndexDaily
        dates = list(
            USIndexDaily.objects.filter(
                index_code="^GSPC",
                trade_date__gte=start_date,
                trade_date__lte=end_date,
            ).values_list("trade_date", flat=True).distinct().order_by("trade_date")
        )
        if not dates:
            logger.debug("_get_all_trade_dates: 无交易日数据，返回空列表")
            return []
        return pd.to_datetime(df["trade_date"]).dt.strftime("%Y-%m-%d").tolist()

    # ----------------------------------------------------------
    # Adaptive rebalancing helpers
    # ----------------------------------------------------------

    # ----------------------------------------------------------
    # Signal generation (for backtest)
    # ----------------------------------------------------------

    def generate_signals(
        self, start_date: str, end_date: str,
        cancel_check: Optional[callable] = None,
        max_workers: int = 0,
    ) -> dict[str, pd.DataFrame]:
        """
        Generate selection signals for the full backtest period (pure monthly rebalance).

        Args:
            start_date: Backtest start date.
            end_date: Backtest end date.
            cancel_check: Optional cancel callback; returns True to abort.
            max_workers: Thread count. 0=auto, 1=serial, >1=specified.

        Returns:
            Dict {date_str: DataFrame[ticker, weight]}.
        """
        rebalance_dates = self.get_rebalance_dates(start_date, end_date)

        # Look back for prior rebalance date
        prior_start = (pd.to_datetime(start_date) - pd.DateOffset(months=2)).strftime("%Y-%m-%d")
        prior_end = (pd.to_datetime(start_date) - pd.Timedelta(days=1)).strftime("%Y-%m-%d")
        prior_dates = self.get_rebalance_dates(prior_start, prior_end)
        if prior_dates:
            last_prior = prior_dates[-1]
            rebalance_dates = [last_prior] + rebalance_dates
            logger.info(f"US prior rebalance date: {last_prior}")

        n_dates = len(rebalance_dates)
        logger.info(f"US backtest: {start_date} ~ {end_date}, {n_dates} base rebalance dates")

        # Check for existing preloaded data
        existing_bulk = USFactorBase._static_cache.get("_bulk_daily")
        if existing_bulk is not None and not existing_bulk.empty:
            price_start_needed = (
                pd.to_datetime(start_date) - pd.Timedelta(days=400)
            )
            cached_min = existing_bulk["trade_date"].min()
            cached_max = existing_bulk["trade_date"].max()
            if cached_min <= price_start_needed and cached_max >= pd.to_datetime(end_date):
                logger.info(
                    f"Using cached US backtest data ({len(existing_bulk)} rows, "
                    f"{cached_min.strftime('%Y-%m-%d')}~{cached_max.strftime('%Y-%m-%d')})"
                )
                USFactorBase._date_cache.clear()
                if USFactorBase._static_cache.get("_rolling_indexed") is None:
                    USFactorBase.precompute_rolling_stats()
            else:
                logger.info(
                    f"Cached data range insufficient "
                    f"({cached_min.strftime('%Y-%m-%d')}~{cached_max.strftime('%Y-%m-%d')}), reloading"
                )
                USFactorBase.clear_all_cache()
                USFactorBase.preload_for_backtest(start_date, end_date)
                USFactorBase.precompute_rolling_stats()
        else:
            USFactorBase.clear_all_cache()
            USFactorBase.preload_for_backtest(start_date, end_date)
            USFactorBase.precompute_rolling_stats()

        # Initialize ML scorer if enabled
        if self._ml_enabled:
            try:
                from backtest.services.us_ml_scorer import USMLScorer
                from services.config import US_ML_FORWARD_DAYS, US_ML_LOOKBACK_MONTHS
                self._ml_scorer = USMLScorer(
                    forward_days=US_ML_FORWARD_DAYS,
                    lookback_months=US_ML_LOOKBACK_MONTHS,
                )
                logger.info("ML scorer initialized (will train after first rebalance window)")
            except Exception as e:
                logger.warning(f"ML scorer init failed: {e}")
                self._ml_scorer = None

        # Determine parallelism
        if max_workers == 0:
            import os
            max_workers = min(8, os.cpu_count() or 4) if n_dates >= 6 else 1

        # ML training requires sequential execution (model state is shared)
        if self._ml_enabled and self._ml_scorer is not None:
            signals = self._generate_signals_sequential(rebalance_dates, cancel_check)
        elif max_workers > 1 and n_dates > 1:
            signals = self._generate_signals_parallel(rebalance_dates, max_workers, cancel_check)
        else:
            signals = self._generate_signals_sequential(rebalance_dates, cancel_check)

        # Clear date cache, keep static data for reuse
        USFactorBase.clear_date_cache()
        logger.info(f"US signal generation done: {len(signals)} periods")
        return signals

    # ----------------------------------------------------------
    # Signal generation implementations
    # ----------------------------------------------------------

    def _generate_signals_sequential(
        self,
        rebalance_dates: list[str],
        cancel_check: Optional[callable] = None,
    ) -> dict[str, pd.DataFrame]:
        """Serial signal generation with rolling ML training."""
        from services.config import US_ML_RETRAIN_INTERVAL, US_ML_FORWARD_DAYS

        signals = {}
        for i, dt in enumerate(rebalance_dates):
            if cancel_check and cancel_check():
                raise RuntimeError("Backtest cancelled")

            # Rolling ML training: retrain every N rebalance dates
            if (
                self._ml_enabled
                and self._ml_scorer is not None
                and i > 0
                and (i - self._ml_last_train_idx) >= US_ML_RETRAIN_INTERVAL // 20
                and len(self._ml_factor_history) >= 6
            ):
                # train_end = current date - forward_days (prevent look-ahead)
                dt_ts = pd.to_datetime(dt)
                train_end = (
                    dt_ts - pd.Timedelta(days=US_ML_FORWARD_DAYS + 5)
                ).strftime("%Y-%m-%d")
                try:
                    all_factors = [f for fs in self.FACTOR_CATEGORIES.values() for f in fs]
                    result_ml = self._ml_scorer.train(
                        self._ml_factor_history, train_end,
                        factor_cols=all_factors,
                    )
                    if "error" not in result_ml:
                        self._ml_last_train_idx = i
                        logger.info(
                            f"ML retrained at {dt}: "
                            f"{result_ml.get('n_samples',0)} samples, "
                            f"val_corr={result_ml.get('val_corr',0):.3f}"
                        )
                except Exception as e:
                    logger.warning(f"ML training failed at {dt}: {e}")

            try:
                result = self.select_stocks(dt)
                signals[dt] = result
                if result.empty:
                    logger.info(f"{dt} empty portfolio signal")
            except Exception as e:
                logger.warning(f"{dt} selection failed: {e}")
        return signals

    def _generate_signals_parallel(
        self,
        rebalance_dates: list[str],
        max_workers: int,
        cancel_check: Optional[callable] = None,
    ) -> dict[str, pd.DataFrame]:
        """
        Parallel signal generation: multi-thread factor computation → serial top-N selection.
        Each worker thread gets its own DB connection to avoid 'another command in progress'.
        """
        from django.db import connections
        t0 = time.time()

        # Phase 1: Parallel factor computation
        composites: dict[str, pd.DataFrame] = {}
        effective_workers = min(max_workers, len(rebalance_dates))
        logger.info(f"US parallel factors: {len(rebalance_dates)} dates, {effective_workers} threads")

        def _compute_with_own_connection(dt: str) -> pd.DataFrame:
            """每个线程关闭继承的连接，让 Django 自动创建独立连接。"""
            connections.close_all()
            try:
                return self._compute_scores_for_date(dt)
            finally:
                connections.close_all()

        connections.close_all()

        with ThreadPoolExecutor(max_workers=effective_workers) as pool:
            future_map = {}
            for dt in rebalance_dates:
                if cancel_check and cancel_check():
                    raise RuntimeError("Backtest cancelled")
                future = pool.submit(_compute_with_own_connection, dt)
                future_map[future] = dt

            n_done = 0
            n_total = len(future_map)
            for future in as_completed(future_map):
                dt = future_map[future]
                n_done += 1
                try:
                    composites[dt] = future.result()
                    n_stocks = len(composites[dt])
                    logger.info(f"[{n_done}/{n_total}] {dt} 因子计算完成: {n_stocks} stocks, {time.time()-t0:.0f}s elapsed")
                except Exception as e:
                    logger.warning(f"[{n_done}/{n_total}] {dt} factor computation failed: {e}")
                    composites[dt] = pd.DataFrame(columns=["ticker", "score"])

        connections.close_all()

        t1 = time.time()
        logger.info(f"US parallel factors done: {t1 - t0:.1f}s")

        # Phase 2: Serial selection with turnover tracking
        signals = {}
        prev_holdings: set[str] = set()

        for i, dt in enumerate(sorted(composites.keys())):
            self._last_date = dt
            composite = composites[dt]
            selected = self._select_from_scores(composite, prev_holdings)
            signals[dt] = selected
            prev_holdings = set(selected["ticker"].tolist()) if len(selected) > 0 else set()

            n_sel = len(selected)
            if selected.empty:
                logger.info(f"[{i+1}/{len(composites)}] {dt} empty portfolio signal")
            else:
                logger.info(f"[{i+1}/{len(composites)}] {dt} 选股完成: {n_sel} stocks")

        self._prev_holdings = prev_holdings

        t2 = time.time()
        logger.info(f"US serial selection done: {t2 - t1:.1f}s, total: {t2 - t0:.1f}s")

        return signals
