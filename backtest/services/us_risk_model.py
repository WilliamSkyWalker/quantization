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
import polars as pl
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
        import datetime as _dt
        from stocks.services.factors.us_base import USFactorBase

        bulk_daily_pd = USFactorBase._static_cache.get("_bulk_daily")
        if bulk_daily_pd is None or bulk_daily_pd.empty:
            logger.warning("USRiskModel: _bulk_daily 缓存为空")
            return None, []

        # Convert from pandas cache to polars for processing
        date_ts = _dt.datetime.strptime(date, "%Y-%m-%d")
        start_ts = date_ts - _dt.timedelta(days=int(self.lookback * 1.5))

        # Filter in pandas first (avoids full copy to polars)
        mask = (
            (bulk_daily_pd["trade_date"] >= start_ts)
            & (bulk_daily_pd["trade_date"] <= date_ts)
            & (bulk_daily_pd["ticker"].isin(universe))
        )
        subset_pd = bulk_daily_pd.loc[mask, ["ticker", "trade_date", "adj_close"]]
        if subset_pd.empty:
            logger.warning(f"USRiskModel: {date} 无价格数据")
            return None, []

        # Convert to polars for pivot
        df = pl.from_pandas(subset_pd).with_columns(
            pl.col("adj_close").cast(pl.Float64, strict=False)
        )

        # Pivot to wide format: rows=dates, cols=tickers
        pivot = (
            df.sort("trade_date")
            .group_by(["trade_date", "ticker"])
            .agg(pl.col("adj_close").last())
            .pivot(on="ticker", index="trade_date", values="adj_close")
            .sort("trade_date")
        )

        # Get ticker columns (everything except trade_date)
        ticker_cols = [c for c in pivot.columns if c != "trade_date"]

        # Keep only tickers with enough non-null history
        valid_tickers = []
        for col in ticker_cols:
            non_null = pivot.get_column(col).is_not_null().sum()
            if non_null >= self.min_days:
                valid_tickers.append(col)

        if len(valid_tickers) < 2:
            logger.warning(
                f"USRiskModel: {date} 只有 {len(valid_tickers)} 只股票有 "
                f">={self.min_days} 天数据"
            )
            return None, []

        pivot = pivot.select(["trade_date"] + valid_tickers)

        # Take last `lookback` trading days
        pivot = pivot.tail(self.lookback + 1)

        # Forward-fill small gaps (max 5 days)
        for col in valid_tickers:
            pivot = pivot.with_columns(
                pl.col(col).forward_fill(limit=5)
            )

        # Convert to numpy for log returns calculation
        price_np = pivot.select(valid_tickers).to_numpy()  # T×N

        # Compute daily log returns
        returns_np = np.log(price_np[1:] / price_np[:-1])

        # Drop columns (tickers) that have any NaN
        col_mask = ~np.isnan(returns_np).any(axis=0)
        if col_mask.sum() < 2:
            logger.warning(f"USRiskModel: {date} 收益矩阵列数不足 ({col_mask.sum()})")
            return None, []

        returns_np = returns_np[:, col_mask]
        valid_tickers = [valid_tickers[i] for i in range(len(valid_tickers)) if col_mask[i]]

        # Sanity check: clip extreme returns (>50% daily = data error)
        returns_np = np.clip(returns_np, -0.5, 0.5)

        # Get date range for logging
        dates_col = pivot.get_column("trade_date")
        logger.debug(
            f"USRiskModel returns matrix: {returns_np.shape[0]}×{returns_np.shape[1]}, "
            f"date range {dates_col[1]}~{dates_col[-1]}"
        )
        return returns_np, valid_tickers

    def _save_parquet(self, date: str, cov: np.ndarray, tickers: list[str]) -> None:
        """Save covariance matrix to parquet cache."""
        try:
            _CACHE_DIR.mkdir(parents=True, exist_ok=True)
            path = _CACHE_DIR / f"cov_{date}.parquet"
            # Store tickers as first column, then cov columns named by ticker
            data = {"_ticker": tickers}
            for i, t in enumerate(tickers):
                data[t] = cov[:, i].tolist()
            df = pl.DataFrame(data)
            df.write_parquet(path)
            logger.debug(f"Cov saved: {path}")
        except Exception as e:
            logger.debug(f"Cov cache save failed: {e}")

    def _load_parquet(self, date: str) -> tuple[np.ndarray | None, list[str]]:
        """Load covariance matrix from parquet cache."""
        path = _CACHE_DIR / f"cov_{date}.parquet"
        if not path.exists():
            return None, []
        try:
            df = pl.read_parquet(path)
            if "_ticker" in df.columns:
                # New polars format
                tickers = df.get_column("_ticker").to_list()
                cov = df.select([c for c in df.columns if c != "_ticker"]).to_numpy()
            else:
                # Legacy pandas format (has row index as first column or no _ticker)
                import pandas as _pd
                pdf = _pd.read_parquet(path)
                tickers = pdf.index.tolist()
                cov = pdf.values
            return cov, tickers
        except Exception as e:
            logger.debug(f"Cov cache load failed: {e}")
            return None, []
