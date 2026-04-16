"""Shareholder Yield 因子 (Priestley 2012; Sharpe 2013)

定义：
    Shareholder Yield = (Dividends Paid + Buybacks - Issuance) / Market Cap

    从 FMP CashFlow:
    - Dividends = -common_dividends_paid  (FMP 报负数，取反)
    - Buybacks  = -common_stock_repurchased  (FMP 报负数，取反)
    - Issuance  = common_stock_issuance  (正数=发行)

    SY = (|common_dividends_paid| + |common_stock_repurchased| - common_stock_issuance) / market_cap

经济直觉：
    - 高 SY = 公司通过分红+回购大量回报股东 → 管理层认为股票便宜
    - 优于单看 Dividend Yield：覆盖回购和增发信息
    - 正向因子：SY 越高 → 预期收益越高

因子方向：+1
"""

import logging

import numpy as np
import pandas as pd

from services.config import LOG_LEVEL
from stocks.services.factors.us_registry import AlphaSignal, register

logger = logging.getLogger(__name__)
logger.setLevel(LOG_LEVEL)


@register
class ShareholderYield(AlphaSignal):
    """Shareholder Yield — 股东综合收益率（分红+回购-增发）/ 市值。"""

    name = "SHAREHOLDER_YIELD"
    version = "v1"
    category = "value"
    horizon = "quarter"
    expected_icir = 0.18
    status = "staging"
    inherent_direction = +1
    data_deps = ["us_financial_data", "us_enterprise_value"]
    ic_window_months = 30

    _FIN_COLS = [
        "common_dividends_paid",
        "common_stock_repurchased",
        "common_stock_issuance",
    ]

    def compute(self, date: str, universe: pd.DataFrame) -> pd.DataFrame:
        tickers = universe["ticker"].tolist()
        if not tickers:
            logger.debug("ShareholderYield: 空 universe")
            return pd.DataFrame(columns=["ticker", "factor_value"])

        fin = self.fetch_financial_latest(date, tickers, self._FIN_COLS)
        if fin.empty:
            logger.warning(f"ShareholderYield({date}): 财务数据为空")
            return pd.DataFrame(columns=["ticker", "factor_value"])

        df = fin.copy()

        # FMP 现金流表约定：支出为负（dividends_paid, repurchased 都是负数）
        dividends = df["common_dividends_paid"].fillna(0).abs()
        buybacks = df["common_stock_repurchased"].fillna(0).abs()
        issuance = df["common_stock_issuance"].fillna(0)

        # 净回报 = 分红 + 回购 - 增发
        net_payout = dividends + buybacks - issuance

        # 取市值
        mktcap = self._get_market_cap_on(date, df["ticker"].tolist())
        if mktcap.empty:
            logger.warning(f"ShareholderYield({date}): 无市值数据")
            return pd.DataFrame(columns=["ticker", "factor_value"])

        merged = df[["ticker"]].copy()
        merged["net_payout"] = net_payout.values
        merged = merged.merge(mktcap, on="ticker", how="inner")
        mc = merged["market_cap"].replace(0, np.nan)
        merged["factor_value"] = merged["net_payout"] / mc

        out = merged[["ticker", "factor_value"]].copy()
        n_out = int(out["factor_value"].notna().sum())
        logger.info(f"ShareholderYield({date}): {n_out} / {len(out)} 有值")
        return out

    @staticmethod
    def _get_market_cap_on(date: str, tickers: list[str]) -> pd.DataFrame:
        """从 us_enterprise_value 取 `date` 可见的最新市值。"""
        from stocks.models import USEnterpriseValue

        date_ts = pd.Timestamp(date)
        start = (date_ts - pd.DateOffset(days=200)).date()

        qs = USEnterpriseValue.objects.filter(
            ticker__in=tickers,
            date__gte=start,
            date__lte=date_ts.date(),
            market_capitalization__isnull=False,
            market_capitalization__gt=0,
        ).values_list("ticker", "date", "market_capitalization")

        df = pd.DataFrame(list(qs), columns=["ticker", "date", "market_cap"])
        if df.empty:
            return pd.DataFrame(columns=["ticker", "market_cap"])
        df["date"] = pd.to_datetime(df["date"])
        df = df.sort_values(["ticker", "date"], ascending=[True, False])
        df = df.drop_duplicates(subset=["ticker"], keep="first")
        df["market_cap"] = pd.to_numeric(df["market_cap"], errors="coerce")
        return df[["ticker", "market_cap"]].reset_index(drop=True)
