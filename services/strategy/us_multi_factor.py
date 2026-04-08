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
    US_SHORT_SCORE_THRESHOLD,
    US_NET_EXPOSURE,
    LOG_LEVEL,
)
from services.data.database import DatabaseManager
from services.data.us_cleaner import get_us_clean_universe
from services.us_factors.value import EP, BP, DivYield
from services.us_factors.quality import RoeTTM, GrossMargin, ProfitStability, MarginTrend
from services.us_factors.growth import NetProfitYoY, RevenueYoY, NetProfitCAGR3Y
from services.us_factors.momentum import Mom1M, Mom3M, Mom12M, Rev5D
from services.us_factors.technical import Turn20D, Vol20D, Ivol, Size
from services.us_factors.analyst import USAnalystRating, USAnalystCoverage
from services.us_factors.accruals import Accruals, BuybackYield
from services.us_factors.polymarket import PolymarketSent
from services.us_factors.quiver import LobbyIntensity, GovContract, WsbSentiment
from services.us_factors.alphavantage import NewsSentiment, IvSkew, PutCallRatio
from services.us_factors.insider import InsiderNetBuy
from services.us_factors.earnings import EarningsSurprise, EpsRevision
from services.us_factors.base import USFactorBase
from services.us_factors.processor import process_factor, clear_neutralize_cache
from services.strategy.us_regime import USRegimeDetector

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
    FACTOR_CATEGORIES = {
        "value":     ["EP", "BP", "DIV_YIELD", "BUYBACK_YIELD"],
        "quality":   ["ROE_TTM", "GROSS_MARGIN", "PROFIT_STB", "MARGIN_TREND", "ACCRUALS"],
        "growth":    ["NET_PROFIT_YOY", "REVENUE_YOY", "NET_PROFIT_CAGR_3Y"],
        "momentum":  ["MOM_1M", "MOM_3M", "MOM_12M", "REV_5D"],
        "technical": ["TURN_20D", "VOL_20D", "IVOL", "SIZE", "IV_SKEW", "PUT_CALL_RATIO"],
        "analyst":   ["US_ANALYST_RATING", "US_ANALYST_COVERAGE", "EARNINGS_SURPRISE", "EPS_REVISION", "INSIDER_NET_BUY"],
        "sentiment": ["POLYMARKET_SENT", "LOBBY_INTENSITY", "GOV_CONTRACT", "NEWS_SENTIMENT"],
        # WSB_SENTIMENT 移除：只有 3 个 ticker（AAPL/GME/TSLA），无截面区分力
    }
    # Pruned (leave-one-out alpha analysis, 2015-2023):
    #   RESIDUAL_MOM: Δα=-3.46% (redundant with MOM_1M/3M/12M, noisier)
    #   VOL_PRICE_DIV: Δα=-4.30% (no signal in US large-cap)
    #   4x MACRO: Δα=-0.25% each (same value for all stocks, zero cross-sectional power)
    # Removed macro category entirely (all 4 factors pruned).

    # Default category weights (overridden by US_CATEGORY_WEIGHTS from config)
    CATEGORY_WEIGHTS = US_CATEGORY_WEIGHTS

    # Core financial factors — stocks missing ALL of these are excluded
    CORE_FINANCIAL_FACTORS = ["GROSS_MARGIN"]  # GrossProfit 依赖财报

    FINANCIAL_DEPENDENT_FACTORS = [
        "GROSS_MARGIN", "DIV_YIELD", "BUYBACK_YIELD",  # 依赖财报/公司行动
    ]

    # Reverse lookup: factor → category
    FACTOR_TO_CATEGORY = {f: cat for cat, fs in FACTOR_CATEGORIES.items() for f in fs}

    def __init__(
        self,
        db: DatabaseManager,
        n_holdings: int = US_MAX_HOLDINGS,
        factor_weights: Optional[dict[str, float]] = None,
        min_select_score: float = US_MIN_SELECT_SCORE,
    ):
        self.db = db
        self.n_holdings = n_holdings
        self.min_select_score = min_select_score
        self._prev_holdings: set[str] = set()
        self._last_date: str = ""

        # Initialize factor instances (23 factors, 7 categories)
        self.factors = [
            # Value
            EP(db), BP(db), DivYield(db), BuybackYield(db),
            # Quality
            RoeTTM(db), GrossMargin(db), ProfitStability(db), MarginTrend(db), Accruals(db),
            # Growth
            NetProfitYoY(db), RevenueYoY(db), NetProfitCAGR3Y(db),
            # Momentum (pruned: ResidualMom — redundant, Δα=-3.46%)
            Mom1M(db), Mom3M(db), Mom12M(db), Rev5D(db),
            # Technical (pruned: VolPriceDiv — no signal, Δα=-4.30%)
            Turn20D(db), Vol20D(db), Ivol(db), Size(db),
            IvSkew(db), PutCallRatio(db),
            # Macro: entire category pruned (截面同值, Δα=-0.25% each)
            # Analyst
            USAnalystRating(db), USAnalystCoverage(db),
            EarningsSurprise(db), EpsRevision(db), InsiderNetBuy(db),
            # Sentiment
            PolymarketSent(db),
            LobbyIntensity(db), GovContract(db),
            NewsSentiment(db),
            # WsbSentiment 移除：只有 3 个 ticker，无截面区分力
        ]

        # 等权（不做 IC 引导权重优化——样本外验证已证明 IC 权重是数据窥探）
        # 反向因子：高值 = 负信号，权重设为 -1.0 使其方向翻转
        #   原有: TURN_20D(高换手=差), VOL_20D(高波动=差), IVOL(同), PROFIT_STB(CV取反已在因子内处理但权重仍为负)
        #   新增: BP/SIZE/DIV_YIELD/BUYBACK_YIELD/LOBBY_INTENSITY (IC 评估确认 ICIR < -0.3)
        # 因子固有方向反转（因子定义上高值=负信号，与 IC 方向无关）
        _INHERENT_REVERSE = {"TURN_20D", "VOL_20D", "IVOL"}
        # 注意：PROFIT_STB 因子内部已取反（factor_value = -CV），不需要再反转
        # BP/SIZE/DIV_YIELD/BUYBACK_YIELD/LOBBY_INTENSITY 的反转
        # 已迁移到 _compute_rolling_ic_direction() 动态决定
        _REVERSE_FACTORS = _INHERENT_REVERSE
        if factor_weights is None:
            self.factor_weights = {
                f.name: (-1.0 if f.name in _REVERSE_FACTORS else 1.0)
                for f in self.factors
            }
        else:
            self.factor_weights = factor_weights

        # Regime detector
        self._regime_detector = USRegimeDetector(db) if US_REGIME_ENABLED else None
        self._last_regime_strength: float = 1.0

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
            df = self.db.query(
                "SELECT ticker, sector FROM us_industry_class WHERE sector IS NOT NULL"
            )
            if not df.empty:
                USFactorBase._static_cache[cache_key] = df
                return df
            logger.debug("_get_cached_sector_df: 行业映射表为空")
        except Exception as e:
            logger.debug(f"_get_cached_sector_df: 获取行业映射失败: {e}")
        return None

    def _get_cached_mktcap_df(self, date: str) -> pd.DataFrame | None:
        """Get market cap data for a given date (cached per date)."""
        cache_key = ("us_mktcap", date)
        cached = USFactorBase._date_cache.get(cache_key)
        if cached is not None:
            return cached
        try:
            # Try preloaded bulk daily data first
            bulk_daily = USFactorBase._static_cache.get("_bulk_daily")
            if bulk_daily is not None and not bulk_daily.empty:
                date_ts = pd.to_datetime(date)
                day = bulk_daily[bulk_daily["trade_date"] == date_ts]
                if not day.empty:
                    # Use close * volume as proxy; but we need actual market_cap
                    # from us_stock_basic
                    pass

            # Query market cap from us_stock_basic
            df = self.db.query(
                "SELECT ticker, market_cap FROM us_stock_basic "
                "WHERE is_active = 1 AND market_cap IS NOT NULL"
            )
            if not df.empty:
                df["market_cap"] = pd.to_numeric(df["market_cap"], errors="coerce")
                df = df.dropna(subset=["market_cap"])
                USFactorBase._date_cache[cache_key] = df
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

        if not US_REGIME_BEAR_OVERRIDES:
            return dict(self.CATEGORY_WEIGHTS)

        strength = self._regime_detector.detect_strength(date)
        self._last_regime_strength = strength

        if strength >= 1.0:
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
    _INHERENT_REVERSE_SET = {"TURN_20D", "VOL_20D", "IVOL"}
    # 质量因子永不反转（即使短期 IC 为负，做空优质资产长期自杀）
    _NEVER_REVERSE_SET = {"ROE_TTM", "GROSS_MARGIN", "PROFIT_STB", "MARGIN_TREND", "ACCRUALS"}
    # 分因子滚动 IC 窗口（月数）：基本面慢、动量快、情绪最快
    _ROLLING_IC_WINDOW = {
        # 基本面（价值/质量/成长）：风格切换慢，24-36M
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
    _ROLLING_IC_DEFAULT = 18  # 未列出的因子默认 18 个月

    def _update_rolling_ic_weights(
        self, date: str, composite: pd.DataFrame, factor_cols: list[str],
    ):
        """
        根据分因子滚动 IC 方向，动态决定每个因子的权重符号。

        机制：
        1. 在每个调仓日 T，用上一期的因子快照 + T 的实际收益计算 IC
        2. 将 IC 追加到滚动窗口（按因子类型不同窗口长度）
        3. 用滚动 IC 均值的符号决定本期权重方向

        窗口长度（_ROLLING_IC_WINDOW）：
        - 基本面（EP/BP/DIV_YIELD 等）：24-36M（风格切换慢）
        - 动量/技术（MOM_1M/3M/12M 等）：6-12M（信号衰减快）
        - 分析师/盈利（ANALYST/EPS 等）：12-18M
        - 情绪/另类（NEWS/WSB/IV 等）：6M（信号生命周期短）

        规则：
        - 固有反转因子（TURN_20D/VOL_20D/IVOL）：始终 -1.0
        - 质量因子（ROE_TTM/GROSS_MARGIN/PROFIT_STB/MARGIN_TREND/ACCRUALS）：始终 +1.0
        - 其他因子：滚动 IC 均值 < -0.01 → -1.0，否则 +1.0
        - 冷启动期（观测数 < 窗口的 1/3）：默认 +1.0
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

        # Step 3: 用滚动 IC 均值决定方向
        changes = []
        for fname in factor_cols:
            if fname in self._INHERENT_REVERSE_SET:
                new_dir = -1.0
            elif fname in self._NEVER_REVERSE_SET:
                new_dir = 1.0
            elif fname in self._rolling_ic_window:
                max_window = self._ROLLING_IC_WINDOW.get(fname, self._ROLLING_IC_DEFAULT)
                min_obs = max(6, max_window // 3)  # 冷启动：至少窗口的 1/3
                if len(self._rolling_ic_window[fname]) >= min_obs:
                    avg_ic = np.mean(self._rolling_ic_window[fname])
                    new_dir = -1.0 if avg_ic < -0.01 else 1.0
                else:
                    new_dir = 1.0  # 冷启动期默认正向
            else:
                new_dir = 1.0

            if fname in self.factor_weights:
                old_dir = self.factor_weights[fname]
                if old_dir != new_dir:
                    changes.append(f"{fname} {old_dir:+.0f}→{new_dir:+.0f}")
                self.factor_weights[fname] = new_dir

        if changes:
            logger.info(f"Rolling IC 方向变更: {', '.join(changes)}")

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
            tickers = composite["ticker"].tolist()
            tickers_str = "','".join(tickers)
            df_latest = self.db.query(
                f"SELECT ticker, MAX(date) as latest_end_date "
                f"FROM us_financial_data "
                f"WHERE ticker IN ('{tickers_str}') AND filing_date <= '{date}' "
                f"GROUP BY ticker"
            )

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
            universe = get_us_clean_universe(self.db, date)
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

        return composite[["ticker", "score"]]

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
        Score-ranked L/S with soft sector cap: top-N long, bottom-M short,
        then clip any sector whose net weight exceeds MAX_SECTOR_NET_WEIGHT.

        Preserves factor-driven sector tilts while preventing dangerous concentration.
        Regime controls net equity exposure.

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
        long_qualified = composite[composite["score"] >= self.min_select_score]
        long_n = US_LONG_N if US_SHORT_ENABLED else self.n_holdings
        bear_n = max(3, int(long_n * US_BEAR_HOLDINGS_RATIO))
        effective_long_n = int(bear_n + (long_n - bear_n) * strength)
        effective_long_n = max(bear_n, min(effective_long_n, long_n))

        long_selected = long_qualified.head(effective_long_n).copy()
        min_w = 1.0 / (max(long_n, 1) * 3)

        if not US_SHORT_ENABLED:
            if len(long_selected) > 0:
                long_selected["weight"] = self._softmax_weights(
                    long_selected["score"].values, tau, min_w
                )
                long_selected["side"] = "LONG"
            return long_selected[["ticker", "score", "weight", "side"]] if len(long_selected) > 0 else empty

        # === Short leg (relaxed threshold for sector diversification) ===
        short_threshold = -0.3  # relaxed from -0.8 to get more shorts
        short_qualified = composite[composite["score"] <= short_threshold]
        short_qualified = short_qualified.sort_values("score", ascending=True)
        effective_short_n = int(US_SHORT_N * (1.0 + 0.3 * (1.0 - strength)))
        effective_short_n = min(effective_short_n, len(short_qualified))
        short_selected = short_qualified.head(effective_short_n).copy()

        if len(long_selected) == 0 and len(short_selected) == 0:
            return empty

        # === Weight allocation (Regime-dynamic net exposure) ===
        base_net = US_NET_EXPOSURE
        bear_net = 0.2
        net_exp = bear_net + (base_net - bear_net) * strength
        long_total = (1.0 + net_exp) / 2.0
        short_total = (1.0 - net_exp) / 2.0

        if len(long_selected) > 0:
            long_selected["weight"] = self._softmax_weights(
                long_selected["score"].values, tau, min_w
            ) * long_total
            long_selected["side"] = "LONG"

        if len(short_selected) > 0:
            short_scores = -short_selected["score"].values
            short_min_w = 1.0 / (max(US_SHORT_N, 1) * 3)
            short_selected["weight"] = -self._softmax_weights(
                short_scores, tau, short_min_w
            ) * short_total
            short_selected["side"] = "SHORT"

        result = pd.concat([long_selected, short_selected], ignore_index=True)

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
        return result[["ticker", "score", "weight", "side"]]

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
        df = self.db.query(
            "SELECT DISTINCT trade_date FROM us_index_daily "
            "WHERE index_code = '^GSPC' "
            "AND trade_date >= :start_date "
            "AND trade_date <= :end_date "
            "ORDER BY trade_date",
            params={"start_date": start_date, "end_date": end_date},
        )

        if df.empty:
            logger.debug("get_rebalance_dates: 无交易日数据，返回空列表")
            return []

        trading_days = sorted(pd.to_datetime(df["trade_date"]).dt.strftime("%Y-%m-%d").tolist())
        return trading_days[::US_REBALANCE_INTERVAL]

    def _get_all_trade_dates(self, start_date: str, end_date: str) -> list[str]:
        """Get all US trading days in range from us_index_daily."""
        df = self.db.query(
            "SELECT DISTINCT trade_date FROM us_index_daily "
            "WHERE index_code = '^GSPC' "
            "AND trade_date >= :start_date "
            "AND trade_date <= :end_date "
            "ORDER BY trade_date",
            params={"start_date": start_date, "end_date": end_date},
        )
        if df.empty:
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
                USFactorBase.preload_for_backtest(self.db, start_date, end_date)
                USFactorBase.precompute_rolling_stats()
        else:
            USFactorBase.clear_all_cache()
            USFactorBase.preload_for_backtest(self.db, start_date, end_date)
            USFactorBase.precompute_rolling_stats()

        # Initialize ML scorer if enabled
        if self._ml_enabled:
            try:
                from services.strategy.us_ml_scorer import USMLScorer
                from services.config import US_ML_FORWARD_DAYS, US_ML_LOOKBACK_MONTHS
                self._ml_scorer = USMLScorer(
                    self.db,
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
                    result_ml = self._ml_scorer.train(
                        self._ml_factor_history, train_end
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
        """
        t0 = time.time()

        # Phase 1: Parallel factor computation
        composites: dict[str, pd.DataFrame] = {}
        effective_workers = min(max_workers, len(rebalance_dates))
        logger.info(f"US parallel factors: {len(rebalance_dates)} dates, {effective_workers} threads")

        with ThreadPoolExecutor(max_workers=effective_workers) as pool:
            future_map = {}
            for dt in rebalance_dates:
                if cancel_check and cancel_check():
                    raise RuntimeError("Backtest cancelled")
                future = pool.submit(self._compute_scores_for_date, dt)
                future_map[future] = dt

            for future in as_completed(future_map):
                dt = future_map[future]
                try:
                    composites[dt] = future.result()
                except Exception as e:
                    logger.warning(f"{dt} factor computation failed: {e}")
                    composites[dt] = pd.DataFrame(columns=["ticker", "score"])

        t1 = time.time()
        logger.info(f"US parallel factors done: {t1 - t0:.1f}s")

        # Phase 2: Serial selection with turnover tracking
        signals = {}
        prev_holdings: set[str] = set()

        for dt in sorted(composites.keys()):
            self._last_date = dt
            composite = composites[dt]
            selected = self._select_from_scores(composite, prev_holdings)
            signals[dt] = selected
            prev_holdings = set(selected["ticker"].tolist()) if len(selected) > 0 else set()

            if selected.empty:
                logger.info(f"{dt} empty portfolio signal")

        self._prev_holdings = prev_holdings

        t2 = time.time()
        logger.info(f"US serial selection done: {t2 - t1:.1f}s, total: {t2 - t0:.1f}s")

        return signals
