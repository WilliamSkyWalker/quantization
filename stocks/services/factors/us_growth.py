"""美股成长因子: NET_PROFIT_YOY, REVENUE_YOY, NET_PROFIT_CAGR_3Y"""

import logging
from datetime import datetime

import numpy as np
import polars as pl

from services.config import LOG_LEVEL
from stocks.services.factors.us_base import USFactorBase

logger = logging.getLogger(__name__)
logger.setLevel(LOG_LEVEL)

_EMPTY = pl.DataFrame(schema={"ticker": pl.Utf8, "factor_value": pl.Float64})


class NetProfitYoY(USFactorBase):
    """Net Profit YoY: TTM net_income now / TTM net_income 1Y ago - 1"""
    name = "NET_PROFIT_YOY"
    description = "净利润同比增速"

    def compute(self, date: str, universe: pl.DataFrame) -> pl.DataFrame:
        tickers = universe["ticker"].to_list()
        return self._yoy_growth(date, "net_income", tickers)

    def _yoy_growth(self, date: str, field: str, tickers: list[str]) -> pl.DataFrame:
        date_ts = datetime.strptime(date, "%Y-%m-%d") if isinstance(date, str) else date
        bulk_fin = self._static_cache.get("_bulk_financial")
        if bulk_fin is None or bulk_fin.is_empty():
            logger.debug("NetProfitYoY._yoy_growth: 无预加载财务数据")
            return _EMPTY.clone()

        df = bulk_fin.filter(pl.col("filing_date") <= date_ts)
        if tickers:
            df = df.filter(pl.col("ticker").is_in(tickers))
        df = df.sort(["ticker", "date"], descending=[False, True])
        df = df.with_columns(pl.col(field).cast(pl.Float64, strict=False))

        # 每只股票取最近8个季度（4Q current + 4Q prior year）
        df = df.group_by("ticker").head(8)

        results = []
        for ticker, grp in df.group_by("ticker"):
            ticker = ticker[0] if isinstance(ticker, tuple) else ticker
            if grp.height < 8:
                results.append({"ticker": ticker, "factor_value": np.nan})
                logger.debug(f"NetProfitYoY._yoy_growth: {ticker} 季度数据不足8条，跳过")
                continue
            vals = grp[field].to_numpy()
            current_ttm = float(np.nansum(vals[:4]))
            prior_ttm = float(np.nansum(vals[4:8]))
            if abs(prior_ttm) < 1e-10 or np.isnan(prior_ttm) or np.isnan(current_ttm):
                results.append({"ticker": ticker, "factor_value": np.nan})
                logger.debug(f"NetProfitYoY._yoy_growth: {ticker} TTM值无效，跳过")
                continue
            results.append({"ticker": ticker, "factor_value": current_ttm / abs(prior_ttm) - 1})

        if not results:
            return _EMPTY.clone()
        return pl.DataFrame(results)


class RevenueYoY(USFactorBase):
    """Revenue YoY: TTM revenue now / TTM revenue 1Y ago - 1"""
    name = "REVENUE_YOY"
    description = "营收同比增速"

    def compute(self, date: str, universe: pl.DataFrame) -> pl.DataFrame:
        tickers = universe["ticker"].to_list()
        date_ts = datetime.strptime(date, "%Y-%m-%d") if isinstance(date, str) else date
        bulk_fin = self._static_cache.get("_bulk_financial")
        if bulk_fin is None or bulk_fin.is_empty():
            logger.debug("RevenueYoY.compute: 无预加载财务数据")
            return _EMPTY.clone()

        df = bulk_fin.filter(pl.col("filing_date") <= date_ts)
        if tickers:
            df = df.filter(pl.col("ticker").is_in(tickers))
        df = df.sort(["ticker", "date"], descending=[False, True])
        df = df.with_columns(pl.col("revenue").cast(pl.Float64, strict=False))

        df = df.group_by("ticker").head(8)

        results = []
        for ticker, grp in df.group_by("ticker"):
            ticker = ticker[0] if isinstance(ticker, tuple) else ticker
            if grp.height < 8:
                results.append({"ticker": ticker, "factor_value": np.nan})
                logger.debug(f"RevenueYoY.compute: {ticker} 季度数据不足8条，跳过")
                continue
            vals = grp["revenue"].to_numpy()
            current_ttm = float(np.nansum(vals[:4]))
            prior_ttm = float(np.nansum(vals[4:8]))
            if abs(prior_ttm) < 1e-10 or np.isnan(prior_ttm) or np.isnan(current_ttm):
                results.append({"ticker": ticker, "factor_value": np.nan})
                logger.debug(f"RevenueYoY.compute: {ticker} TTM营收值无效，跳过")
                continue
            results.append({"ticker": ticker, "factor_value": current_ttm / abs(prior_ttm) - 1})

        if not results:
            return _EMPTY.clone()
        return pl.DataFrame(results)


class NetProfitCAGR3Y(USFactorBase):
    """Net Profit CAGR 3Y: (TTM now / TTM 3Y ago)^(1/3) - 1"""
    name = "NET_PROFIT_CAGR_3Y"
    description = "净利润3年复合增长率"

    def compute(self, date: str, universe: pl.DataFrame) -> pl.DataFrame:
        tickers = universe["ticker"].to_list()
        date_ts = datetime.strptime(date, "%Y-%m-%d") if isinstance(date, str) else date
        bulk_fin = self._static_cache.get("_bulk_financial")
        if bulk_fin is None or bulk_fin.is_empty():
            logger.debug("NetProfitCAGR3Y.compute: 无预加载财务数据")
            return _EMPTY.clone()

        df = bulk_fin.filter(pl.col("filing_date") <= date_ts)
        if tickers:
            df = df.filter(pl.col("ticker").is_in(tickers))
        df = df.sort(["ticker", "date"], descending=[False, True])
        df = df.with_columns(pl.col("net_income").cast(pl.Float64, strict=False))

        # 需要最近16个季度（4Q current + 12Q history = 3Y prior TTM at Q12-Q15）
        df = df.group_by("ticker").head(16)

        results = []
        for ticker, grp in df.group_by("ticker"):
            ticker = ticker[0] if isinstance(ticker, tuple) else ticker
            if grp.height < 16:
                results.append({"ticker": ticker, "factor_value": np.nan})
                logger.debug(f"NetProfitCAGR3Y.compute: {ticker} 季度数据不足16条，跳过")
                continue
            vals = grp["net_income"].to_numpy()
            current_ttm = float(np.nansum(vals[:4]))
            prior_ttm = float(np.nansum(vals[12:16]))
            if prior_ttm <= 0 or current_ttm <= 0:
                results.append({"ticker": ticker, "factor_value": np.nan})
                logger.debug(f"NetProfitCAGR3Y.compute: {ticker} TTM净利润<=0，跳过")
                continue
            if np.isnan(prior_ttm) or np.isnan(current_ttm):
                results.append({"ticker": ticker, "factor_value": np.nan})
                logger.debug(f"NetProfitCAGR3Y.compute: {ticker} TTM净利润为NaN，跳过")
                continue
            cagr = (current_ttm / prior_ttm) ** (1.0 / 3.0) - 1
            results.append({"ticker": ticker, "factor_value": cagr})

        if not results:
            return _EMPTY.clone()
        return pl.DataFrame(results)
