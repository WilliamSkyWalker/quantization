"""
美股回购收益率因子

BUYBACK_YIELD: (Share Repurchases) / Market Cap
  回购金额 / 市值，补充 DIV_YIELD 对科技股股东回报的低估
  数据来源：SimFin cashflow 表的 repurchase of equity 字段

注：ACCRUALS 已迁移到 AlphaSignal registry（stocks/services/factors/signals/quality/legacy.py）。
"""

import logging
from datetime import datetime

import numpy as np
import polars as pl

from services.config import LOG_LEVEL
from stocks.services.factors.us_base import USFactorBase

logger = logging.getLogger(__name__)
logger.setLevel(LOG_LEVEL)

_EMPTY = pl.DataFrame(schema={"ticker": pl.Utf8, "factor_value": pl.Float64})


class BuybackYield(USFactorBase):
    """Buyback Yield: trailing 12M share repurchases / market cap"""
    name = "BUYBACK_YIELD"
    description = "回购收益率 (近12月回购金额 / 市值)"

    def compute(self, date: str, universe: pl.DataFrame) -> pl.DataFrame:
        tickers = universe["ticker"].to_list()
        date_ts = datetime.strptime(date, "%Y-%m-%d") if isinstance(date, str) else date

        bulk_fin = self._static_cache.get("_bulk_financial")
        if bulk_fin is None or bulk_fin.is_empty():
            logger.debug("BuybackYield.compute: 无预加载财务数据")
            return _EMPTY.clone()

        df = bulk_fin.filter(pl.col("filing_date") <= date_ts)
        if tickers:
            df = df.filter(pl.col("ticker").is_in(tickers))
        df = df.sort(["ticker", "date"], descending=[False, True])

        df = df.group_by("ticker").head(4)
        df = df.with_columns([
            pl.col("total_equity").cast(pl.Float64, strict=False),
            pl.col("net_income").cast(pl.Float64, strict=False),
        ])

        mktcap = self.get_market_cap(date, tickers)
        mktcap_dict = dict(zip(
            mktcap["ticker"].to_list(),
            mktcap["market_cap"].to_list(),
        )) if not mktcap.is_empty() else {}

        # 获取股息
        divs = self.get_dividends(date, lookback_days=365, universe_tickers=tickers)
        div_dict = dict(zip(
            divs["ticker"].to_list(),
            divs["total_dividend"].to_list(),
        )) if not divs.is_empty() else {}

        results = []
        for ticker, grp in df.group_by("ticker"):
            ticker = ticker[0] if isinstance(ticker, tuple) else ticker
            if grp.height < 2:
                results.append({"ticker": ticker, "factor_value": np.nan})
                logger.debug(f"BuybackYield.compute: {ticker} 季度数据不足2条，跳过")
                continue

            mc = mktcap_dict.get(ticker)
            if not mc or mc <= 0:
                results.append({"ticker": ticker, "factor_value": np.nan})
                logger.debug(f"BuybackYield.compute: {ticker} 市值无效，跳过")
                continue

            # TTM net income
            ni_vals = grp["net_income"].to_numpy()
            ni_ttm = float(np.nansum(ni_vals))
            # Equity change (latest - oldest in window)
            eq_vals = grp["total_equity"].to_numpy()
            eq_latest = float(eq_vals[0])
            eq_oldest = float(eq_vals[-1])

            if np.isnan(eq_latest) or np.isnan(eq_oldest) or np.isnan(ni_ttm):
                results.append({"ticker": ticker, "factor_value": np.nan})
                logger.debug(f"BuybackYield.compute: {ticker} 权益或净利润数据缺失，跳过")
                continue

            equity_change = eq_latest - eq_oldest
            # Estimated buyback = NI - equity_change - dividends
            total_div = div_dict.get(ticker, 0) or 0
            estimated_buyback = ni_ttm - equity_change - total_div
            buyback_yield = max(0, estimated_buyback) / mc  # only count net buybacks

            results.append({"ticker": ticker, "factor_value": buyback_yield})

        if not results:
            return _EMPTY.clone()
        return pl.DataFrame(results)
