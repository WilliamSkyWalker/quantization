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

from backend.services.config import (
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
from backend.services.data.database import DatabaseManager
from backend.services.data.us_cleaner import get_us_clean_universe
from backend.services.us_factors.value import EP, BP, DivYield
from backend.services.us_factors.quality import RoeTTM, GrossMargin, ProfitStability, MarginTrend
from backend.services.us_factors.growth import NetProfitYoY, RevenueYoY, NetProfitCAGR3Y
from backend.services.us_factors.momentum import Mom1M, Mom3M, Mom12M, Rev5D, ResidualMom
from backend.services.us_factors.technical import Turn20D, Vol20D, PriceDev60D, Ivol, Size, VolPriceDiv
from backend.services.us_factors.macro import USMacroCycle, USMacroLiqd, USMacroInfl, USMacroExtr
from backend.services.us_factors.analyst import USAnalystRating, USAnalystCoverage
from backend.services.us_factors.accruals import Accruals, BuybackYield
from backend.services.us_factors.polymarket import PolymarketSent
from backend.services.us_factors.insider import InsiderNetBuy
from backend.services.us_factors.base import USFactorBase
from backend.services.us_factors.processor import process_factor, clear_neutralize_cache
from backend.services.strategy.us_regime import USRegimeDetector

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
        "momentum":  ["MOM_1M", "MOM_3M", "MOM_12M", "REV_5D", "RESIDUAL_MOM"],
        "technical": ["TURN_20D", "VOL_20D", "IVOL", "SIZE", "VOL_PRICE_DIV"],
        "macro":     ["US_MACRO_CYCLE", "US_MACRO_LIQD", "US_MACRO_INFL", "US_MACRO_EXTR"],
        "analyst":   ["US_ANALYST_RATING", "US_ANALYST_COVERAGE"],
        "sentiment": ["POLYMARKET_SENT"],
    }

    # Default category weights (overridden by US_CATEGORY_WEIGHTS from config)
    CATEGORY_WEIGHTS = US_CATEGORY_WEIGHTS

    # Core financial factors — stocks missing ALL of these are excluded
    CORE_FINANCIAL_FACTORS = ["EP", "BP", "ROE_TTM", "GROSS_MARGIN"]

    FINANCIAL_DEPENDENT_FACTORS = [
        "EP", "BP", "ROE_TTM", "GROSS_MARGIN", "PROFIT_STB", "MARGIN_TREND",
        "NET_PROFIT_YOY", "REVENUE_YOY", "NET_PROFIT_CAGR_3Y",
        "ACCRUALS", "BUYBACK_YIELD",
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

        # Initialize factor instances
        self.factors = [
            # Value
            EP(db), BP(db), DivYield(db), BuybackYield(db),
            # Quality
            RoeTTM(db), GrossMargin(db), ProfitStability(db), MarginTrend(db), Accruals(db),
            # Growth
            NetProfitYoY(db), RevenueYoY(db), NetProfitCAGR3Y(db),
            # Momentum
            Mom1M(db), Mom3M(db), Mom12M(db), Rev5D(db), ResidualMom(db),
            # Technical
            Turn20D(db), Vol20D(db), Ivol(db), Size(db), VolPriceDiv(db),
            # Macro
            USMacroCycle(db), USMacroLiqd(db), USMacroInfl(db), USMacroExtr(db),
            # Analyst
            USAnalystRating(db), USAnalystCoverage(db),
            # Sentiment
            PolymarketSent(db),
        ]

        # 等权（不做 IC 引导权重优化——样本外验证已证明 IC 权重是数据窥探）
        if factor_weights is None:
            self.factor_weights = {f.name: 1.0 for f in self.factors}
        else:
            self.factor_weights = factor_weights

        # 反向因子
        self._reverse_factors = ["TURN_20D", "VOL_20D", "IVOL", "PROFIT_STB"]
        for fname in self._reverse_factors:
            if fname in self.factor_weights:
                self.factor_weights[fname] = -abs(self.factor_weights[fname])

        # Regime detector
        self._regime_detector = USRegimeDetector(db) if US_REGIME_ENABLED else None
        self._last_regime_strength: float = 1.0

        # ML scorer (LightGBM)
        from backend.services.config import US_ML_SCORING_ENABLED, US_ML_BLEND_RATIO
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
        except Exception:
            pass
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
        except Exception:
            pass
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

        # 3.5. Financial staleness decay (post-standardize, pre-compose)
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
                ml_scores = self._ml_scorer.predict(composite.set_index("ticker").reindex(
                    composite["ticker"]
                )[self._ml_scorer.feature_cols])
                ml_scores = ml_scores.values
                linear_scores = composite["score"].values
                # Blend: (1-α) × linear + α × ML
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
            return np.array([])
        if tau > 0:
            shifted = scores - scores.max()
            exp_s = np.exp(shifted / tau)
            w = exp_s / exp_s.sum()
        else:
            w = np.ones(len(scores)) / len(scores)
        w = np.maximum(w, min_w)
        return w / w.sum()

    def _select_from_scores(
        self, composite: pd.DataFrame, prev_holdings: set[str],
    ) -> pd.DataFrame:
        """
        Alpha-first: 多空个股对冲，因子选股为主。

        Long: Top-N 高分股 (Softmax 权重)
        Short: Bottom-M 低分股 (score ≤ threshold, Softmax 取反)
        净敞口由 Regime 动态调整。

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

        # === Short leg ===
        short_qualified = composite[composite["score"] <= US_SHORT_SCORE_THRESHOLD]
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
        logger.info(
            f"US L/S selection: {len(long_selected)} long ({long_total:.0%}), "
            f"{len(short_selected)} short ({short_total:.0%}), "
            f"net={net_exp:.0%}, regime={strength:.2f}"
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
                from backend.services.strategy.us_ml_scorer import USMLScorer
                from backend.services.config import US_ML_FORWARD_DAYS, US_ML_LOOKBACK_MONTHS
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

        if max_workers > 1 and n_dates > 1:
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
        """Serial signal generation."""
        signals = {}
        for dt in rebalance_dates:
            if cancel_check and cancel_check():
                raise RuntimeError("Backtest cancelled")
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
