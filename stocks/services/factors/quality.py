"""美股质量因子: ROE_TTM, GROSS_MARGIN, PROFIT_STB, MARGIN_TREND"""

import logging

import numpy as np
import pandas as pd

from services.config import LOG_LEVEL
from stocks.services.factors.base import USFactorBase

logger = logging.getLogger(__name__)
logger.setLevel(LOG_LEVEL)


class RoeTTM(USFactorBase):
    """Return on Equity (latest filing)"""
    name = "ROE_TTM"
    description = "净资产收益率"

    def compute(self, date: str, universe: pd.DataFrame) -> pd.DataFrame:
        tickers = universe["ticker"].tolist()
        fin = self.get_latest_financial(date, ["roe"], tickers)
        if fin.empty:
            logger.debug("RoeTTM.compute: 无财务数据")
            return pd.DataFrame(columns=["ticker", "factor_value"])
        fin["factor_value"] = pd.to_numeric(fin["roe"], errors="coerce")
        return fin[["ticker", "factor_value"]]


class GrossMargin(USFactorBase):
    """Gross Margin (latest filing)"""
    name = "GROSS_MARGIN"
    description = "毛利率"

    def compute(self, date: str, universe: pd.DataFrame) -> pd.DataFrame:
        tickers = universe["ticker"].tolist()
        fin = self.get_latest_financial(date, ["gross_margin"], tickers)
        if fin.empty:
            logger.debug("GrossMargin.compute: 无财务数据")
            return pd.DataFrame(columns=["ticker", "factor_value"])
        fin["factor_value"] = pd.to_numeric(fin["gross_margin"], errors="coerce")
        return fin[["ticker", "factor_value"]]


class ProfitStability(USFactorBase):
    """Profit Stability: CV of quarterly net_income YoY growth (inverse)"""
    name = "PROFIT_STB"
    description = "盈利稳定性 (净利润YoY增速的变异系数，取反)"

    def compute(self, date: str, universe: pd.DataFrame) -> pd.DataFrame:
        tickers = universe["ticker"].tolist()
        date_ts = pd.to_datetime(date)

        bulk_fin = self._static_cache.get("_bulk_financial")
        if bulk_fin is None or bulk_fin.empty:
            logger.debug("ProfitStability.compute: 无预加载财务数据")
            return pd.DataFrame(columns=["ticker", "factor_value"])

        df = bulk_fin[bulk_fin["filing_date"] <= date_ts].copy()
        if tickers:
            df = df[df["ticker"].isin(tickers)]
        df = df.sort_values(["ticker", "date"], ascending=[True, False])
        df["net_income"] = pd.to_numeric(df["net_income"], errors="coerce")

        # 每只股票取最近8个季度
        df = df.groupby("ticker").head(8)

        results = []
        for ticker, grp in df.groupby("ticker"):
            if len(grp) < 5:
                results.append({"ticker": ticker, "factor_value": np.nan})
                logger.debug(f"ProfitStability.compute: {ticker} 季度数据不足5条，跳过")
                continue
            vals = grp["net_income"].values
            # YoY growth: q(i) vs q(i+4)
            if len(vals) < 5:
                results.append({"ticker": ticker, "factor_value": np.nan})
                logger.debug(f"ProfitStability.compute: {ticker} 净利润数据不足5条，跳过")
                continue
            yoy_growths = []
            for i in range(len(vals) - 4):
                base = vals[i + 4]
                if base != 0 and not np.isnan(base):
                    yoy_growths.append((vals[i] - base) / abs(base))
            if len(yoy_growths) < 2:
                results.append({"ticker": ticker, "factor_value": np.nan})
                logger.debug(f"ProfitStability.compute: {ticker} YoY增速不足2条，跳过")
                continue
            arr = np.array(yoy_growths)
            mean = np.mean(arr)
            std = np.std(arr)
            cv = std / abs(mean) if abs(mean) > 1e-10 else np.nan
            # 取反：CV越小 → 越稳定 → 因子值越大
            results.append({"ticker": ticker, "factor_value": -cv if not np.isnan(cv) else np.nan})

        return pd.DataFrame(results)


class MarginTrend(USFactorBase):
    """Margin Trend: QoQ change in gross_margin"""
    name = "MARGIN_TREND"
    description = "毛利率趋势 (最新 vs 上一季度)"

    def compute(self, date: str, universe: pd.DataFrame) -> pd.DataFrame:
        tickers = universe["ticker"].tolist()
        date_ts = pd.to_datetime(date)

        bulk_fin = self._static_cache.get("_bulk_financial")
        if bulk_fin is None or bulk_fin.empty:
            logger.debug("MarginTrend.compute: 无预加载财务数据")
            return pd.DataFrame(columns=["ticker", "factor_value"])

        df = bulk_fin[bulk_fin["filing_date"] <= date_ts].copy()
        if tickers:
            df = df[df["ticker"].isin(tickers)]
        df = df.sort_values(["ticker", "date"], ascending=[True, False])
        df["gross_margin"] = pd.to_numeric(df["gross_margin"], errors="coerce")

        # 每只股票取最近2个季度
        df = df.groupby("ticker").head(2)

        results = []
        for ticker, grp in df.groupby("ticker"):
            if len(grp) < 2:
                results.append({"ticker": ticker, "factor_value": np.nan})
                logger.debug(f"MarginTrend.compute: {ticker} 季度数据不足2条，跳过")
                continue
            latest = grp.iloc[0]["gross_margin"]
            prev = grp.iloc[1]["gross_margin"]
            if pd.isna(latest) or pd.isna(prev):
                results.append({"ticker": ticker, "factor_value": np.nan})
                logger.debug(f"MarginTrend.compute: {ticker} 毛利率数据缺失，跳过")
                continue
            results.append({"ticker": ticker, "factor_value": latest - prev})

        return pd.DataFrame(results)
