"""
US Risk Model — Ledoit-Wolf Shrinkage Covariance Estimator

Computes an N×N covariance matrix Σ from daily returns, using
Ledoit-Wolf shrinkage to stabilize estimation for high-dimensional data.

Usage:
    from backtest.services.us_risk_model import USRiskModel

    risk = USRiskModel()
    cov, tickers = risk.estimate(date="2024-12-31", universe=["AAPL", "MSFT", ...])
    # cov: np.ndarray (N×N), tickers: list[str] (aligned with cov rows/cols)
"""

import logging
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.covariance import LedoitWolf

from services.config import US_COV_LOOKBACK, US_MIN_HISTORY_DAYS, LOG_LEVEL

logger = logging.getLogger(__name__)
logger.setLevel(LOG_LEVEL)

# Parquet cache directory
_CACHE_DIR = Path(__file__).resolve().parent.parent.parent / "cache" / "cov"


class USRiskModel:
    """
    Ledoit-Wolf shrinkage covariance estimator for US equities.

    Reads daily prices from USFactorBase._static_cache["_bulk_daily"]
    (must call preload_for_backtest first).
    """

    def __init__(self, lookback: int = US_COV_LOOKBACK, min_days: int = US_MIN_HISTORY_DAYS):
        self.lookback = lookback
        self.min_days = min_days
        self._cache: dict[str, tuple[np.ndarray, list[str]]] = {}

    def estimate(
        self,
        date: str,
        universe: list[str],
    ) -> tuple[np.ndarray, list[str]]:
        """
        Estimate covariance matrix for a given date and universe.

        Args:
            date: Rebalance date (YYYY-MM-DD).
            universe: List of tickers to include.

        Returns:
            (cov_matrix, tickers) — cov_matrix is N×N ndarray,
            tickers is the aligned list (may be smaller than input
            if some tickers lack sufficient history).
        """
        if not universe:
            logger.warning("USRiskModel.estimate: 空 universe")
            return np.array([[]]), []

        cache_key = date
        if cache_key in self._cache:
            cov, tickers = self._cache[cache_key]
            # Filter to requested universe (subset)
            mask = [t in set(universe) for t in tickers]
            idx = [i for i, m in enumerate(mask) if m]
            if idx:
                return cov[np.ix_(idx, idx)], [tickers[i] for i in idx]

        # Try parquet cache
        cov, tickers = self._load_parquet(date)
        if cov is not None:
            self._cache[cache_key] = (cov, tickers)
            mask = [t in set(universe) for t in tickers]
            idx = [i for i, m in enumerate(mask) if m]
            if idx:
                logger.debug(f"Cov cache hit: {date}, {len(idx)}/{len(universe)} tickers")
                return cov[np.ix_(idx, idx)], [tickers[i] for i in idx]

        # Compute from scratch
        returns_matrix, valid_tickers = self._build_returns_matrix(date, universe)
        if returns_matrix is None or len(valid_tickers) < 2:
            logger.warning(
                f"USRiskModel: {date} 有效股票不足 ({len(valid_tickers) if valid_tickers else 0})，"
                f"返回对角矩阵"
            )
            n = len(universe)
            return np.eye(n) * 0.04, universe  # 20% vol diagonal fallback

        lw = LedoitWolf()
        lw.fit(returns_matrix)
        cov = lw.covariance_
        shrinkage = lw.shrinkage_

        logger.info(
            f"USRiskModel: {date}, {len(valid_tickers)} stocks, "
            f"{returns_matrix.shape[0]} days, shrinkage={shrinkage:.4f}"
        )

        # Cache
        self._cache[cache_key] = (cov, valid_tickers)
        self._save_parquet(date, cov, valid_tickers)

        # Filter to requested universe
        ticker_set = set(universe)
        mask = [t in ticker_set for t in valid_tickers]
        idx = [i for i, m in enumerate(mask) if m]
        if not idx:
            n = len(universe)
            return np.eye(n) * 0.04, universe
        return cov[np.ix_(idx, idx)], [valid_tickers[i] for i in idx]

    def _build_returns_matrix(
        self, date: str, universe: list[str],
    ) -> tuple[np.ndarray | None, list[str]]:
        """
        Build T×N daily returns matrix from preloaded price cache.

        Returns:
            (returns_matrix, valid_tickers) or (None, []) if insufficient data.
        """
        from stocks.services.factors.us_base import USFactorBase

        bulk_daily = USFactorBase._static_cache.get("_bulk_daily")
        if bulk_daily is None or bulk_daily.empty:
            logger.warning("USRiskModel: _bulk_daily 缓存为空")
            return None, []

        date_ts = pd.to_datetime(date)
        start_ts = date_ts - pd.Timedelta(days=int(self.lookback * 1.5))

        # Filter to date range and universe
        mask = (
            (bulk_daily["trade_date"] >= start_ts)
            & (bulk_daily["trade_date"] <= date_ts)
            & (bulk_daily["ticker"].isin(universe))
        )
        df = bulk_daily.loc[mask, ["ticker", "trade_date", "adj_close"]].copy()
        if df.empty:
            logger.warning(f"USRiskModel: {date} 无价格数据")
            return None, []

        # Pivot to wide format: rows=dates, cols=tickers
        df["adj_close"] = pd.to_numeric(df["adj_close"], errors="coerce")
        pivot = df.pivot_table(
            index="trade_date", columns="ticker", values="adj_close", aggfunc="last",
        )
        pivot = pivot.sort_index()

        # Keep only tickers with enough history
        valid_counts = pivot.notna().sum()
        valid_tickers = valid_counts[valid_counts >= self.min_days].index.tolist()
        if len(valid_tickers) < 2:
            logger.warning(
                f"USRiskModel: {date} 只有 {len(valid_tickers)} 只股票有 "
                f">={self.min_days} 天数据"
            )
            return None, []

        pivot = pivot[valid_tickers]

        # Take last `lookback` trading days
        pivot = pivot.tail(self.lookback + 1)

        # Forward-fill small gaps (max 5 days), then drop remaining NaN rows
        pivot = pivot.ffill(limit=5)

        # Compute daily log returns
        returns = np.log(pivot / pivot.shift(1))
        returns = returns.iloc[1:]  # drop first NaN row
        returns = returns.dropna(axis=1, how="any")

        if returns.shape[1] < 2:
            logger.warning(f"USRiskModel: {date} 收益矩阵列数不足 ({returns.shape[1]})")
            return None, []

        valid_tickers = returns.columns.tolist()
        returns_np = returns.values  # T×N

        # Sanity check: clip extreme returns (>50% daily = data error)
        returns_np = np.clip(returns_np, -0.5, 0.5)

        logger.debug(
            f"USRiskModel returns matrix: {returns_np.shape[0]}×{returns_np.shape[1]}, "
            f"date range {returns.index[0].strftime('%Y-%m-%d')}~{returns.index[-1].strftime('%Y-%m-%d')}"
        )
        return returns_np, valid_tickers

    def _save_parquet(self, date: str, cov: np.ndarray, tickers: list[str]) -> None:
        """Save covariance matrix to parquet cache."""
        try:
            _CACHE_DIR.mkdir(parents=True, exist_ok=True)
            path = _CACHE_DIR / f"cov_{date}.parquet"
            df = pd.DataFrame(cov, index=tickers, columns=tickers)
            df.to_parquet(path)
            logger.debug(f"Cov saved: {path}")
        except Exception as e:
            logger.debug(f"Cov cache save failed: {e}")

    def _load_parquet(self, date: str) -> tuple[np.ndarray | None, list[str]]:
        """Load covariance matrix from parquet cache."""
        path = _CACHE_DIR / f"cov_{date}.parquet"
        if not path.exists():
            return None, []
        try:
            df = pd.read_parquet(path)
            tickers = df.index.tolist()
            return df.values, tickers
        except Exception as e:
            logger.debug(f"Cov cache load failed: {e}")
            return None, []
