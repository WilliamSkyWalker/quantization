"""Altman Z-Score (Altman 1968, JF)

破产概率预测。公式（公开交易公司版）：

    Z = 1.2·X1 + 1.4·X2 + 3.3·X3 + 0.6·X4 + 1.0·X5

    X1 = Working Capital / Total Assets         流动性
    X2 = Retained Earnings / Total Assets       累计盈利能力
    X3 = EBIT / Total Assets                    当期盈利能力
    X4 = Market Value of Equity / Total Liab    市场对资产的估值
    X5 = Sales / Total Assets                   资产效率

判读：
    Z > 2.99 — 安全区
    1.81 < Z < 2.99 — 灰色区
    Z < 1.81 — 破产危险

因子方向：+1（Z 越高越安全，利好）
"""

import logging

import numpy as np
import pandas as pd

from services.config import LOG_LEVEL
from stocks.services.factors.us_registry import AlphaSignal, register

logger = logging.getLogger(__name__)
logger.setLevel(LOG_LEVEL)


@register
class AltmanZ(AlphaSignal):
    """Altman Z-Score — 破产概率（越高越安全）。"""

    name = "ALTMAN_Z"
    version = "v1"
    category = "quality"
    horizon = "quarter"
    expected_icir = 0.10
    status = "staging"
    inherent_direction = +1
    data_deps = ["us_financial_data", "us_enterprise_value"]
    ic_window_months = 24

    _FIN_COLS = [
        "total_assets",
        "total_current_assets",
        "total_current_liabilities",
        "retained_earnings",
        "ebit",
        "revenue",
        "total_liabilities",
    ]

    def compute(self, date: str, universe: pd.DataFrame) -> pd.DataFrame:
        tickers = universe["ticker"].tolist()
        if not tickers:
            logger.debug("AltmanZ: 空 universe")
            return pd.DataFrame(columns=["ticker", "factor_value"])

        fin = self.fetch_financial_latest(date, tickers, self._FIN_COLS)
        if fin.empty:
            logger.warning(f"AltmanZ({date}): 财务数据为空")
            return pd.DataFrame(columns=["ticker", "factor_value"])

        ta = fin["total_assets"].replace(0, np.nan)
        wc = fin["total_current_assets"] - fin["total_current_liabilities"]
        x1 = wc / ta
        x2 = fin["retained_earnings"] / ta
        x3 = fin["ebit"] / ta
        x5 = fin["revenue"] / ta

        # X4 需要 market cap / total liabilities。market cap 用 enterprise_value.market_capitalization
        mktcap = self._get_market_cap_on(date, fin["ticker"].tolist())
        merged = fin.merge(mktcap, on="ticker", how="left")
        tl = merged["total_liabilities"].replace(0, np.nan)
        x4 = merged["market_cap"] / tl

        z = 1.2 * x1 + 1.4 * x2 + 3.3 * x3 + 0.6 * x4 + 1.0 * x5

        out = pd.DataFrame({"ticker": merged["ticker"], "factor_value": z})
        n_out = int(out["factor_value"].notna().sum())
        logger.info(f"AltmanZ({date}): {n_out} / {len(out)} 有值")
        return out[["ticker", "factor_value"]]

    def _get_market_cap_on(self, date: str, tickers: list[str]) -> pd.DataFrame:
        """从 us_enterprise_value 取 `date` 可见的最新市值。优先走缓存。"""
        date_ts = pd.Timestamp(date)

        # 优先从缓存切片
        cache = self._static_cache.get("_alpha_ev")
        if cache is not None and not cache.empty:
            start = date_ts - pd.Timedelta(days=200)
            mask = (
                cache["ticker"].isin(tickers)
                & (cache["date"] >= start)
                & (cache["date"] <= date_ts)
                & cache["market_capitalization"].notna()
                & (cache["market_capitalization"] > 0)
            )
            df = cache.loc[mask, ["ticker", "date", "market_capitalization"]].copy()
            if not df.empty:
                df = df.sort_values(["ticker", "date"], ascending=[True, False])
                df = df.drop_duplicates(subset=["ticker"], keep="first")
                df = df.rename(columns={"market_capitalization": "market_cap"})
                return df[["ticker", "market_cap"]].reset_index(drop=True)

        # ORM fallback
        from stocks.models import USEnterpriseValue

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
            logger.debug(f"AltmanZ._get_market_cap_on({date}): 无数据")
            return pd.DataFrame(columns=["ticker", "market_cap"])
        df["date"] = pd.to_datetime(df["date"])
        df = df.sort_values(["ticker", "date"], ascending=[True, False])
        df = df.drop_duplicates(subset=["ticker"], keep="first")
        df["market_cap"] = pd.to_numeric(df["market_cap"], errors="coerce")
        return df[["ticker", "market_cap"]].reset_index(drop=True)
