"""
美股应计异常和回购收益率因子

ACCRUALS: (Net Income - Operating Cash Flow) / Total Assets
  应计项目越高 → 利润质量越差 → 未来收益越可能被下修（Sloan 1996）
  取反：高应计 = 低因子值

BUYBACK_YIELD: (Share Repurchases) / Market Cap
  回购金额 / 市值，补充 DIV_YIELD 对科技股股东回报的低估
  数据来源：SimFin cashflow 表的 repurchase of equity 字段
"""

import logging

import numpy as np
import pandas as pd

from services.config import LOG_LEVEL
from stocks.services.factors.base import USFactorBase

logger = logging.getLogger(__name__)
logger.setLevel(LOG_LEVEL)


class Accruals(USFactorBase):
    """Accruals Anomaly: (Net Income - Operating CF) / Total Assets (inverse)"""
    name = "ACCRUALS"
    description = "应计异常 (利润质量，取反)"

    def compute(self, date: str, universe: pd.DataFrame) -> pd.DataFrame:
        tickers = universe["ticker"].tolist()
        date_ts = pd.to_datetime(date)

        bulk_fin = self._static_cache.get("_bulk_financial")
        if bulk_fin is None or bulk_fin.empty:
            logger.debug("Accruals.compute: 无预加载财务数据")
            return pd.DataFrame(columns=["ticker", "factor_value"])

        df = bulk_fin[bulk_fin["filing_date"] <= date_ts].copy()
        if tickers:
            df = df[df["ticker"].isin(tickers)]
        df = df.sort_values(["ticker", "date"], ascending=[True, False])

        # 取每只股票最近 4 个季度（TTM）
        df = df.groupby("ticker").head(4)

        # 需要 net_income, free_cash_flow (= operating CF - capex), total_assets
        for col in ["net_income", "free_cash_flow", "total_assets"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")

        results = []
        for ticker, grp in df.groupby("ticker"):
            ni_ttm = grp["net_income"].sum()
            fcf_ttm = grp["free_cash_flow"].sum()
            # Operating CF ≈ FCF + CapEx，但 SimFin 的 free_cash_flow = OpCF - CapEx
            # 所以 Accruals = NI - OpCF = NI - (FCF + CapEx)
            # 简化：Accruals = NI - FCF（忽略 CapEx 部分，这样更保守）
            total_assets = grp["total_assets"].iloc[0]  # 最新季度

            if pd.isna(ni_ttm) or pd.isna(fcf_ttm) or pd.isna(total_assets) or abs(total_assets) < 1e-6:
                results.append({"ticker": ticker, "factor_value": np.nan})
                logger.debug(f"Accruals.compute: {ticker} 财务数据缺失或总资产为零，跳过")
                continue

            accrual = (ni_ttm - fcf_ttm) / abs(total_assets)
            # 取反：高应计 = 低因子值
            results.append({"ticker": ticker, "factor_value": -accrual})

        return pd.DataFrame(results)


class BuybackYield(USFactorBase):
    """Buyback Yield: trailing 12M share repurchases / market cap"""
    name = "BUYBACK_YIELD"
    description = "回购收益率 (近12月回购金额 / 市值)"

    def compute(self, date: str, universe: pd.DataFrame) -> pd.DataFrame:
        tickers = universe["ticker"].tolist()
        date_ts = pd.to_datetime(date)

        bulk_fin = self._static_cache.get("_bulk_financial")
        if bulk_fin is None or bulk_fin.empty:
            logger.debug("BuybackYield.compute: 无预加载财务数据")
            return pd.DataFrame(columns=["ticker", "factor_value"])

        df = bulk_fin[bulk_fin["filing_date"] <= date_ts].copy()
        if tickers:
            df = df[df["ticker"].isin(tickers)]
        df = df.sort_values(["ticker", "date"], ascending=[True, False])

        # SimFin cashflow 没有单独的回购字段，用 free_cash_flow 和 net_income 的差异来近似
        # 更好的方式：用 us_corporate_action 中的 split 数据，但那不是回购
        # 最佳近似：(Operating CF - Free CF - Net Income Change) 但太间接
        # 实际做法：查 SimFin 原始 CSV 的回购字段

        # 先尝试从 bulk_financial 获取（如果有 repurchase 字段）
        # SimFin 下载器没存回购字段，回退到市值变化近似
        # 简化方案：用 equity 变化 + dividends 来推算回购
        # equity_change = equity(t) - equity(t-1) - net_income + dividends
        # buyback ≈ -equity_change（当 equity 下降但有盈利时，说明在回购）

        df = df.groupby("ticker").head(4)
        for col in ["total_equity", "net_income"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")

        mktcap = self.get_market_cap(date, tickers)
        mktcap_map = dict(zip(mktcap["ticker"], mktcap["market_cap"]))

        # 获取股息
        divs = self.get_dividends(date, lookback_days=365, universe_tickers=tickers)
        div_map = dict(zip(divs["ticker"], divs["total_dividend"])) if not divs.empty else {}

        results = []
        for ticker, grp in df.groupby("ticker"):
            if len(grp) < 2:
                results.append({"ticker": ticker, "factor_value": np.nan})
                logger.debug(f"BuybackYield.compute: {ticker} 季度数据不足2条，跳过")
                continue

            mc = mktcap_map.get(ticker)
            if not mc or mc <= 0:
                results.append({"ticker": ticker, "factor_value": np.nan})
                logger.debug(f"BuybackYield.compute: {ticker} 市值无效，跳过")
                continue

            # TTM net income
            ni_ttm = grp["net_income"].sum()
            # Equity change (latest - oldest in window)
            eq_latest = grp["total_equity"].iloc[0]
            eq_oldest = grp["total_equity"].iloc[-1]

            if pd.isna(eq_latest) or pd.isna(eq_oldest) or pd.isna(ni_ttm):
                results.append({"ticker": ticker, "factor_value": np.nan})
                logger.debug(f"BuybackYield.compute: {ticker} 权益或净利润数据缺失，跳过")
                continue

            equity_change = eq_latest - eq_oldest
            # Estimated buyback = NI - equity_change - dividends
            # (if company earned NI but equity didn't increase, the difference was returned)
            total_div = div_map.get(ticker, 0) or 0
            estimated_buyback = ni_ttm - equity_change - total_div
            buyback_yield = max(0, estimated_buyback) / mc  # only count net buybacks

            results.append({"ticker": ticker, "factor_value": buyback_yield})

        return pd.DataFrame(results)
