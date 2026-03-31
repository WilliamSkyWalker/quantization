"""
US stock risk manager

Risk controls for US equity portfolio:
    1. Liquidity filter: remove stocks with avg daily dollar volume < $1M
    2. Single stock cap: clip individual weight to 10%
    3. Sector cap: clip GICS sector total weight to 25%
    4. Renormalize weights after all adjustments

Uses GICS sector from us_stock_basic instead of Shanwan industry classification.
No industry groups concept (unlike A-share real-estate/finance/TMT groups).

Usage:
    rm = USRiskManager(db)
    adjusted = rm.adjust_weights(weights_df, "2024-12-31")
"""

import logging

import pandas as pd

from services.config import (
    US_MAX_SINGLE_WEIGHT,
    US_MAX_SECTOR_WEIGHT,
    US_MIN_DAILY_VOLUME,
    LOG_LEVEL,
)
from services.data.database import DatabaseManager

logger = logging.getLogger(__name__)
logger.setLevel(LOG_LEVEL)


class USRiskManager:
    """
    US stock risk manager.

    Usage:
        rm = USRiskManager(db)
        adjusted = rm.adjust_weights(weights_df, "2024-12-31")
    """

    def __init__(
        self,
        db: DatabaseManager,
        max_single_weight: float = US_MAX_SINGLE_WEIGHT,
        max_sector_weight: float = US_MAX_SECTOR_WEIGHT,
        min_daily_volume: float = US_MIN_DAILY_VOLUME,
    ):
        """
        Args:
            db: DatabaseManager instance.
            max_single_weight: Single stock weight cap (default 10%).
            max_sector_weight: GICS sector weight cap (default 25%).
            min_daily_volume: Minimum avg daily dollar volume (default $1M).
        """
        self.db = db
        self.max_single_weight = max_single_weight
        self.max_sector_weight = max_sector_weight
        self.min_daily_volume = min_daily_volume

    def adjust_weights(
        self,
        weights: pd.DataFrame,
        date: str,
    ) -> pd.DataFrame:
        """
        Apply all risk controls and return adjusted weights.

        Pipeline:
            1. Liquidity filter
            2. Single stock cap
            3. Sector cap
            4. Renormalize

        Args:
            weights: DataFrame[ticker, weight].
            date: Reference date string (YYYY-MM-DD).

        Returns:
            Risk-adjusted DataFrame[ticker, weight].
        """
        if weights.empty:
            return weights

        df = weights[["ticker", "weight"]].copy()
        initial_count = len(df)

        # 1. Liquidity filter
        df = self._filter_liquidity(df, date)

        # 2. Single stock cap
        df = self._cap_single_weight(df)

        # 3. Sector cap
        df = self._cap_sector_weight(df, date)

        # 4. Renormalize
        df = self._normalize_weights(df)

        final_count = len(df)
        if final_count < initial_count:
            logger.info(f"US risk filter: {initial_count} -> {final_count} stocks")

        return df

    # ----------------------------------------------------------
    # Liquidity Filter
    # ----------------------------------------------------------

    def _filter_liquidity(self, df: pd.DataFrame, date: str) -> pd.DataFrame:
        """
        Remove stocks with average daily dollar volume below threshold.

        Uses 20-day lookback from us_daily_price (volume * adj_close).
        """
        if self.min_daily_volume <= 0:
            return df

        tickers = df["ticker"].tolist()
        tickers_str = "','".join(tickers)

        lookback_start = (
            pd.to_datetime(date) - pd.Timedelta(days=40)
        ).strftime("%Y-%m-%d")

        df_vol = self.db.query(
            f"SELECT ticker, AVG(volume * adj_close) as avg_dollar_volume "
            f"FROM us_daily_price "
            f"WHERE trade_date >= '{lookback_start}' "
            f"AND trade_date <= '{date}' "
            f"AND ticker IN ('{tickers_str}') "
            f"GROUP BY ticker"
        )

        if df_vol.empty:
            return df

        liquid_tickers = df_vol[
            df_vol["avg_dollar_volume"] >= self.min_daily_volume
        ]["ticker"].tolist()

        removed = set(tickers) - set(liquid_tickers)
        if removed:
            logger.info(
                f"Liquidity filter removed {len(removed)} stocks "
                f"(< ${self.min_daily_volume:,.0f} avg daily volume)"
            )

        return df[df["ticker"].isin(liquid_tickers)]

    # ----------------------------------------------------------
    # Single Stock Cap
    # ----------------------------------------------------------

    def _cap_single_weight(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Clip individual stock weight to ±max_single_weight.
        Supports long-short: positive weights capped at +max, negative at -max.
        """
        df = df.copy()
        cap = self.max_single_weight

        # Cap long positions
        long_over = df["weight"] > cap
        if long_over.any():
            df.loc[long_over, "weight"] = cap

        # Cap short positions (symmetric)
        short_over = df["weight"] < -cap
        if short_over.any():
            df.loc[short_over, "weight"] = -cap

        n_capped = long_over.sum() + short_over.sum()
        if n_capped > 0:
            logger.debug(f"Single stock cap: {n_capped} stocks hit ±{cap:.0%} limit")

        return df

    # ----------------------------------------------------------
    # Sector Cap
    # ----------------------------------------------------------

    def _cap_sector_weight(self, df: pd.DataFrame, date: str) -> pd.DataFrame:
        """
        Clip GICS sector total weight to max_sector_weight.

        Sector info is fetched from us_stock_basic.sector column.
        """
        tickers = df["ticker"].tolist()
        tickers_str = "','".join(tickers)

        sector_df = self.db.query(
            f"SELECT ticker, sector FROM us_stock_basic "
            f"WHERE ticker IN ('{tickers_str}')"
        )

        if sector_df.empty:
            return df

        df = df.merge(sector_df, on="ticker", how="left")
        df["sector"] = df["sector"].fillna("Unknown")

        # Cap long sector exposure
        long_mask = df["weight"] > 0
        if long_mask.any():
            long_sector = df[long_mask].groupby("sector")["weight"].sum()
            for sec, sw in long_sector.items():
                if sw > self.max_sector_weight:
                    mask = (df["sector"] == sec) & long_mask
                    df.loc[mask, "weight"] *= self.max_sector_weight / sw
                    logger.info(f"Sector cap (long): {sec} {sw:.1%} -> {self.max_sector_weight:.0%}")

        # Cap short sector exposure (symmetric)
        short_mask = df["weight"] < 0
        if short_mask.any():
            short_sector = df[short_mask].groupby("sector")["weight"].sum()
            for sec, sw in short_sector.items():
                if sw < -self.max_sector_weight:
                    mask = (df["sector"] == sec) & short_mask
                    df.loc[mask, "weight"] *= self.max_sector_weight / abs(sw)
                    logger.info(f"Sector cap (short): {sec} {sw:.1%} -> -{self.max_sector_weight:.0%}")

        df = df.drop(columns=["sector"], errors="ignore")
        return df

    # ----------------------------------------------------------
    # Normalize
    # ----------------------------------------------------------

    @staticmethod
    def _normalize_weights(df: pd.DataFrame) -> pd.DataFrame:
        """
        Normalize weights.

        Long-short mode: preserve long/short ratio, normalize each leg separately.
        Long-only mode: normalize to sum 1.
        """
        has_short = (df["weight"] < 0).any()
        if not has_short:
            # Pure long: normalize to 1
            total = df["weight"].sum()
            if total > 0:
                df["weight"] = df["weight"] / total
        else:
            # Long-short: normalize each leg to preserve their totals
            # (caps may have reduced totals; re-scale back to original)
            long_mask = df["weight"] > 0
            short_mask = df["weight"] < 0
            long_total = df.loc[long_mask, "weight"].sum()
            short_total = df.loc[short_mask, "weight"].sum()  # negative
            # Keep as-is (already allocated by strategy)
            # Only normalize if gross exposure exceeds limit
            from services.config import US_GROSS_EXPOSURE_CAP
            gross = df["weight"].abs().sum()
            if gross > US_GROSS_EXPOSURE_CAP:
                scale = US_GROSS_EXPOSURE_CAP / gross
                df["weight"] *= scale
                logger.info(f"Gross exposure cap: {gross:.2f} -> {US_GROSS_EXPOSURE_CAP:.2f}")
        return df

    # ----------------------------------------------------------
    # Risk Report
    # ----------------------------------------------------------

    def risk_report(self, weights_df: pd.DataFrame, date: str) -> dict:
        """
        Generate a risk report for the portfolio.

        Args:
            weights_df: Current portfolio weights.
            date: Date string.

        Returns:
            Risk metrics dictionary.
        """
        df = weights_df.copy()

        report = {
            "n_holdings": len(df),
            "max_single_weight": f"{df['weight'].max():.2%}" if not df.empty else "0",
            "min_single_weight": f"{df['weight'].min():.2%}" if not df.empty else "0",
        }

        # Sector concentration
        try:
            tickers = df["ticker"].tolist()
            tickers_str = "','".join(tickers)
            sector_df = self.db.query(
                f"SELECT ticker, sector FROM us_stock_basic "
                f"WHERE ticker IN ('{tickers_str}')"
            )
            if not sector_df.empty:
                df = df.merge(sector_df, on="ticker", how="left")
                sec_weights = df.groupby("sector")["weight"].sum()
                report["max_sector_weight"] = f"{sec_weights.max():.2%}"
                report["n_sectors"] = len(sec_weights)
                report["top3_sectors"] = ", ".join(
                    f"{name}({w:.1%})"
                    for name, w in sec_weights.nlargest(3).items()
                )
        except Exception:
            pass

        return report
