"""美股成长因子: NET_PROFIT_YOY, REVENUE_YOY, NET_PROFIT_CAGR_3Y"""

import logging

import numpy as np
import pandas as pd

from backend.services.config import LOG_LEVEL
from backend.services.us_factors.base import USFactorBase

logger = logging.getLogger(__name__)
logger.setLevel(LOG_LEVEL)


class NetProfitYoY(USFactorBase):
    """Net Profit YoY: TTM net_income now / TTM net_income 1Y ago - 1"""
    name = "NET_PROFIT_YOY"
    description = "净利润同比增速"

    def compute(self, date: str, universe: pd.DataFrame) -> pd.DataFrame:
        tickers = universe["ticker"].tolist()
        return self._yoy_growth(date, "net_income", tickers)

    def _yoy_growth(self, date: str, field: str, tickers: list[str]) -> pd.DataFrame:
        date_ts = pd.to_datetime(date)
        bulk_fin = self._static_cache.get("_bulk_financial")
        if bulk_fin is None or bulk_fin.empty:
            return pd.DataFrame(columns=["ticker", "factor_value"])

        df = bulk_fin[bulk_fin["filing_date"] <= date_ts].copy()
        if tickers:
            df = df[df["ticker"].isin(tickers)]
        df = df.sort_values(["ticker", "date"], ascending=[True, False])
        df[field] = pd.to_numeric(df[field], errors="coerce")

        # 每只股票取最近8个季度（4Q current + 4Q prior year）
        df = df.groupby("ticker").head(8)

        results = []
        for ticker, grp in df.groupby("ticker"):
            if len(grp) < 8:
                results.append({"ticker": ticker, "factor_value": np.nan})
                continue
            current_ttm = grp.iloc[:4][field].sum()
            prior_ttm = grp.iloc[4:8][field].sum()
            if abs(prior_ttm) < 1e-10 or np.isnan(prior_ttm) or np.isnan(current_ttm):
                results.append({"ticker": ticker, "factor_value": np.nan})
                continue
            results.append({"ticker": ticker, "factor_value": current_ttm / abs(prior_ttm) - 1})

        return pd.DataFrame(results)


class RevenueYoY(USFactorBase):
    """Revenue YoY: TTM revenue now / TTM revenue 1Y ago - 1"""
    name = "REVENUE_YOY"
    description = "营收同比增速"

    def compute(self, date: str, universe: pd.DataFrame) -> pd.DataFrame:
        tickers = universe["ticker"].tolist()
        date_ts = pd.to_datetime(date)
        bulk_fin = self._static_cache.get("_bulk_financial")
        if bulk_fin is None or bulk_fin.empty:
            return pd.DataFrame(columns=["ticker", "factor_value"])

        df = bulk_fin[bulk_fin["filing_date"] <= date_ts].copy()
        if tickers:
            df = df[df["ticker"].isin(tickers)]
        df = df.sort_values(["ticker", "date"], ascending=[True, False])
        df["revenue"] = pd.to_numeric(df["revenue"], errors="coerce")

        df = df.groupby("ticker").head(8)

        results = []
        for ticker, grp in df.groupby("ticker"):
            if len(grp) < 8:
                results.append({"ticker": ticker, "factor_value": np.nan})
                continue
            current_ttm = grp.iloc[:4]["revenue"].sum()
            prior_ttm = grp.iloc[4:8]["revenue"].sum()
            if abs(prior_ttm) < 1e-10 or np.isnan(prior_ttm) or np.isnan(current_ttm):
                results.append({"ticker": ticker, "factor_value": np.nan})
                continue
            results.append({"ticker": ticker, "factor_value": current_ttm / abs(prior_ttm) - 1})

        return pd.DataFrame(results)


class NetProfitCAGR3Y(USFactorBase):
    """Net Profit CAGR 3Y: (TTM now / TTM 3Y ago)^(1/3) - 1"""
    name = "NET_PROFIT_CAGR_3Y"
    description = "净利润3年复合增长率"

    def compute(self, date: str, universe: pd.DataFrame) -> pd.DataFrame:
        tickers = universe["ticker"].tolist()
        date_ts = pd.to_datetime(date)
        bulk_fin = self._static_cache.get("_bulk_financial")
        if bulk_fin is None or bulk_fin.empty:
            return pd.DataFrame(columns=["ticker", "factor_value"])

        df = bulk_fin[bulk_fin["filing_date"] <= date_ts].copy()
        if tickers:
            df = df[df["ticker"].isin(tickers)]
        df = df.sort_values(["ticker", "date"], ascending=[True, False])
        df["net_income"] = pd.to_numeric(df["net_income"], errors="coerce")

        # 需要最近16个季度（4Q current + 12Q history = 3Y prior TTM at Q12-Q15）
        df = df.groupby("ticker").head(16)

        results = []
        for ticker, grp in df.groupby("ticker"):
            if len(grp) < 16:
                results.append({"ticker": ticker, "factor_value": np.nan})
                continue
            current_ttm = grp.iloc[:4]["net_income"].sum()
            prior_ttm = grp.iloc[12:16]["net_income"].sum()
            if prior_ttm <= 0 or current_ttm <= 0:
                results.append({"ticker": ticker, "factor_value": np.nan})
                continue
            if np.isnan(prior_ttm) or np.isnan(current_ttm):
                results.append({"ticker": ticker, "factor_value": np.nan})
                continue
            cagr = (current_ttm / prior_ttm) ** (1.0 / 3.0) - 1
            results.append({"ticker": ticker, "factor_value": cagr})

        return pd.DataFrame(results)
