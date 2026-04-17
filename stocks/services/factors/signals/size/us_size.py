"""Size & Float 因子

1. LOG_MARKET_CAP — log(市值)，经典 Fama-French SMB 控制变量
2. FREE_FLOAT_PCT — 自由流通股 / 总股本，流通性代理

数据源：USEnterpriseValue (market_cap) + USSharesFloat (free_float)
"""

import logging

import numpy as np
import pandas as pd

from services.config import LOG_LEVEL
from stocks.services.factors.us_registry import AlphaSignal, register

logger = logging.getLogger(__name__)
logger.setLevel(LOG_LEVEL)


# ---------------------------------------------------------------------------
# 1. Log Market Cap
# ---------------------------------------------------------------------------


@register
class LogMarketCap(AlphaSignal):
    """Log Market Cap — log(市值)，小盘溢价控制变量。

    Fama-French (1993): 小盘股长期跑赢大盘。
    方向设 0，让 IC 决定（近年大盘股跑赢，小盘溢价消失）。
    """

    name = "LOG_MARKET_CAP"
    version = "v1"
    category = "size"
    horizon = "month"
    expected_icir = 0.05
    status = "staging"
    inherent_direction = 0
    data_deps = ["us_enterprise_value"]
    ic_window_months = 18

    def compute(self, date: str, universe: pd.DataFrame) -> pd.DataFrame:
        tickers = universe["ticker"].tolist()
        if not tickers:
            logger.debug("LogMarketCap: 空 universe")
            return pd.DataFrame(columns=["ticker", "factor_value"])

        mktcap = self._get_market_cap(date, tickers)
        if mktcap.empty:
            logger.warning(f"LogMarketCap({date}): 无市值数据")
            return pd.DataFrame(columns=["ticker", "factor_value"])

        mktcap["factor_value"] = np.log(mktcap["market_cap"])

        out = mktcap[["ticker", "factor_value"]].copy()
        n_out = int(out["factor_value"].notna().sum())
        logger.info(f"LogMarketCap({date}): {n_out} / {len(out)} 有值")
        return out

    @staticmethod
    def _get_market_cap(date: str, tickers: list[str]) -> pd.DataFrame:
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


# ---------------------------------------------------------------------------
# 2. Free Float Percentage
# ---------------------------------------------------------------------------


@register
class FreeFloatPct(AlphaSignal):
    """Free Float % — 自由流通股占比。

    低 free float = 流动性差 + 容易被挤仓。
    USSharesFloat 是 snapshot 表（无历史 date），仅适用于近期/实盘。
    """

    name = "FREE_FLOAT_PCT"
    version = "v1"
    category = "size"
    horizon = "quarter"
    expected_icir = 0.04
    status = "staging"
    inherent_direction = 0
    data_deps = ["us_shares_float"]
    ic_window_months = 18

    def compute(self, date: str, universe: pd.DataFrame) -> pd.DataFrame:
        tickers = universe["ticker"].tolist()
        if not tickers:
            logger.debug("FreeFloatPct: 空 universe")
            return pd.DataFrame(columns=["ticker", "factor_value"])

        from stocks.models import USSharesFloat

        qs = USSharesFloat.objects.filter(
            ticker__in=tickers,
            free_float__isnull=False,
        ).values_list("ticker", "free_float")

        df = pd.DataFrame(list(qs), columns=["ticker", "free_float"])
        if df.empty:
            logger.warning(f"FreeFloatPct({date}): 无 float 数据")
            return pd.DataFrame(columns=["ticker", "factor_value"])

        df["free_float"] = pd.to_numeric(df["free_float"], errors="coerce")
        # free_float 已是百分比 (0-100)
        out = pd.DataFrame({
            "ticker": df["ticker"],
            "factor_value": df["free_float"],
        })
        n_out = int(out["factor_value"].notna().sum())
        logger.info(f"FreeFloatPct({date}): {n_out} / {len(out)} 有值")
        return out[["ticker", "factor_value"]]
