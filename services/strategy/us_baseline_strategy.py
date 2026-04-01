"""
US Alpha v2 Strategy — Full Factor Set + Regime L/S

Reuses USMultiFactorStrategy's complete 27-factor scoring pipeline
(8 categories, two-layer scoring, penalties, staleness decay, ML blend),
but replaces the portfolio construction with:
    - Decile-based L/S (top 10% long, bottom 10% short)
    - Regime-driven asymmetric net exposure
    - Softmax weight allocation

This allows direct A/B comparison vs Alpha v1 to isolate the effect
of portfolio construction changes while keeping identical factor signals.
"""

import logging
import time
from typing import Optional

import numpy as np
import pandas as pd

from services.config import LOG_LEVEL
from services.data.database import DatabaseManager
from services.us_factors.base import USFactorBase
from services.strategy.us_multi_factor import USMultiFactorStrategy
from services.strategy.us_regime import USRegimeDetector

logger = logging.getLogger(__name__)
logger.setLevel(LOG_LEVEL)


class USBaselineStrategy:
    """
    Alpha v2: Full 27-factor scoring + Regime-driven decile L/S.

    Delegates factor computation and scoring to USMultiFactorStrategy,
    then applies its own portfolio construction:
        - Top 10% long, bottom 10% short (Softmax weights)
        - Regime-driven net exposure (bull: 85% net, bear: 25% net)
        - Monthly rebalance

    Usage:
        strategy = USBaselineStrategy(db)
        signals = strategy.generate_signals("2015-01-01", "2023-12-31")
    """

    DECILE_PCT = 0.10

    # Net exposure: strength → (long_total, short_total)
    LONG_BULL = 1.00
    LONG_BEAR = 0.60
    SHORT_BULL = 0.15
    SHORT_BEAR = 0.35

    WEIGHT_TAU = 1.5

    def __init__(self, db: DatabaseManager):
        self.db = db
        # Reuse full factor pipeline from Alpha v1
        self._scorer = USMultiFactorStrategy(db)
        self._regime = USRegimeDetector(db)

    def generate_signals(
        self,
        start_date: str,
        end_date: str,
        cancel_check: Optional[callable] = None,
        max_workers: int = 0,
    ) -> dict[str, pd.DataFrame]:
        """Generate monthly L/S signals using full factor scoring."""
        t0 = time.time()

        # Preload data (delegates to USMultiFactorStrategy's machinery)
        existing_bulk = USFactorBase._static_cache.get("_bulk_daily")
        if existing_bulk is not None and not existing_bulk.empty:
            price_start = pd.to_datetime(start_date) - pd.Timedelta(days=400)
            cached_min = existing_bulk["trade_date"].min()
            cached_max = existing_bulk["trade_date"].max()
            if cached_min <= price_start and cached_max >= pd.to_datetime(end_date):
                logger.info("Using cached data for Alpha v2")
                USFactorBase._date_cache.clear()
                if USFactorBase._static_cache.get("_rolling_indexed") is None:
                    USFactorBase.precompute_rolling_stats()
            else:
                USFactorBase.clear_all_cache()
                USFactorBase.preload_for_backtest(self.db, start_date, end_date)
                USFactorBase.precompute_rolling_stats()
        else:
            USFactorBase.clear_all_cache()
            USFactorBase.preload_for_backtest(self.db, start_date, end_date)
            USFactorBase.precompute_rolling_stats()

        # Monthly rebalance dates
        rebalance_dates = self._get_month_end_dates(start_date, end_date)

        # Look back for prior month-end
        prior_start = (
            pd.to_datetime(start_date) - pd.DateOffset(months=2)
        ).strftime("%Y-%m-%d")
        prior_end = (
            pd.to_datetime(start_date) - pd.Timedelta(days=1)
        ).strftime("%Y-%m-%d")
        prior_dates = self._get_month_end_dates(prior_start, prior_end)
        if prior_dates:
            rebalance_dates = [prior_dates[-1]] + rebalance_dates

        n_dates = len(rebalance_dates)
        logger.info(f"Alpha v2: {start_date} ~ {end_date}, {n_dates} rebalance dates")

        # Initialize ML scorer if enabled (same as v1)
        from services.config import US_ML_SCORING_ENABLED
        if US_ML_SCORING_ENABLED:
            try:
                from services.strategy.us_ml_scorer import USMLScorer
                from services.config import US_ML_FORWARD_DAYS, US_ML_LOOKBACK_MONTHS
                self._scorer._ml_scorer = USMLScorer(
                    self.db,
                    forward_days=US_ML_FORWARD_DAYS,
                    lookback_months=US_ML_LOOKBACK_MONTHS,
                )
                logger.info("ML scorer initialized for Alpha v2")
            except Exception as e:
                logger.warning(f"ML scorer init failed: {e}")
                self._scorer._ml_scorer = None

        signals = {}
        for i, dt in enumerate(rebalance_dates):
            if cancel_check and cancel_check():
                raise RuntimeError("Backtest cancelled")
            try:
                result = self._select_for_date(dt)
                signals[dt] = result
                n_long = (result["weight"] > 0).sum() if not result.empty else 0
                n_short = (result["weight"] < 0).sum() if not result.empty else 0
                logger.info(f"[{i+1}/{n_dates}] {dt}: {n_long}L / {n_short}S")
            except Exception as e:
                logger.warning(f"{dt} Alpha v2 selection failed: {e}")
                signals[dt] = pd.DataFrame(columns=["ticker", "weight"])

        USFactorBase.clear_date_cache()
        elapsed = time.time() - t0
        logger.info(f"Alpha v2 signals done: {len(signals)} periods ({elapsed:.1f}s)")
        return signals

    def _select_for_date(self, date: str) -> pd.DataFrame:
        """
        Use USMultiFactorStrategy's full scoring + selection (top-N + small short).
        Delegates entirely to v1's select_stocks which already handles
        Regime-aware L/S with Softmax weights.
        """
        return self._scorer.select_stocks(date)

    @staticmethod
    def _softmax(scores: np.ndarray) -> np.ndarray:
        tau = USBaselineStrategy.WEIGHT_TAU
        if len(scores) == 0:
            logger.debug("_softmax: 输入得分为空，返回空数组")
            return np.array([])
        shifted = scores - scores.max()
        exp_s = np.exp(shifted / tau) if tau > 0 else np.ones(len(scores))
        w = exp_s / exp_s.sum()
        min_w = 1.0 / (len(scores) * 3)
        w = np.maximum(w, min_w)
        return w / w.sum()

    def _get_month_end_dates(self, start_date: str, end_date: str) -> list[str]:
        df = self.db.query(
            "SELECT DISTINCT trade_date FROM us_index_daily "
            "WHERE index_code = '^GSPC' "
            "AND trade_date >= :start AND trade_date <= :end "
            "ORDER BY trade_date",
            params={"start": start_date, "end": end_date},
        )
        if df.empty:
            logger.debug(f"_get_month_end_dates: {start_date}~{end_date} 无交易日数据，返回空列表")
            return []
        df["trade_date"] = pd.to_datetime(df["trade_date"])
        df["ym"] = df["trade_date"].dt.to_period("M")
        month_ends = df.groupby("ym")["trade_date"].max()
        return sorted(month_ends.dt.strftime("%Y-%m-%d").tolist())
