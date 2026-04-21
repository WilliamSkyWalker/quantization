"""Lobby Intensity 因子

定义：
    LOBBY_INTENSITY = 过去 12 个月游说支出总额 / 市值

    数据源：USLobbying（ticker, date, amount）

经济直觉：
    - 游说支出 = 政治资本投入，可获取监管红利、政府合同、税收优惠
    - Hill et al. (2013): 游说支出与异常收益正相关，ROI 高达 5600%
    - Chen et al. (2015): 游说公司在税收减免和补贴方面有系统性优势
    - 正向因子：高游说强度 → 政治影响力大 → 预期收益更高

    归一化到市值：防止大公司绝对金额大但实际强度低的偏差。

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
class LobbyIntensity(AlphaSignal):
    """Lobby Intensity — 游说支出 / 市值。"""

    name = "LOBBY_INTENSITY"
    version = "v1"
    category = "alternative"
    horizon = "quarter"
    expected_icir = 0.06
    status = "staging"
    inherent_direction = +1
    data_deps = ["us_lobbying", "us_enterprise_value"]
    ic_window_months = 24

    _WINDOW_DAYS = 365

    def compute(self, date: str, universe: pd.DataFrame) -> pd.DataFrame:
        tickers = universe["ticker"].tolist()
        if not tickers:
            logger.debug("LobbyIntensity: 空 universe")
            return pd.DataFrame(columns=["ticker", "factor_value"])

        date_ts = pd.Timestamp(date)
        start_ts = date_ts - pd.Timedelta(days=self._WINDOW_DAYS)

        # 优先走缓存（列名: ticker, year, amount — year 是日期字段）
        df = pd.DataFrame()
        cache = self._static_cache.get("_bulk_lobbying")
        if cache is not None and not cache.empty:
            mask = (
                cache["ticker"].isin(tickers)
                & (cache["year"] >= start_ts)
                & (cache["year"] <= date_ts)
                & cache["amount"].notna()
                & (cache["amount"] > 0)
            )
            df = cache.loc[mask, ["ticker", "amount"]].copy()

        # fallback ORM
        if df.empty:
            from stocks.models import USLobbying

            start = start_ts.date()
            qs = USLobbying.objects.filter(
                ticker__in=tickers,
                date__gte=start,
                date__lte=date_ts.date(),
                amount__isnull=False,
                amount__gt=0,
            ).values_list("ticker", "amount")
            df = pd.DataFrame(list(qs), columns=["ticker", "amount"])
        if df.empty:
            logger.warning(f"LobbyIntensity({date}): 无游说数据")
            return pd.DataFrame(columns=["ticker", "factor_value"])

        df["amount"] = pd.to_numeric(df["amount"], errors="coerce")
        agg = df.groupby("ticker")["amount"].sum().reset_index()
        agg.columns = ["ticker", "lobby_total"]

        # 取市值归一化
        mktcap = self._get_market_cap(date, agg["ticker"].tolist())
        if mktcap.empty:
            logger.warning(f"LobbyIntensity({date}): 无市值数据")
            return pd.DataFrame(columns=["ticker", "factor_value"])

        merged = agg.merge(mktcap, on="ticker", how="inner")
        mc = merged["market_cap"].replace(0, np.nan)
        merged["factor_value"] = merged["lobby_total"] / mc

        out = merged[["ticker", "factor_value"]].copy()
        n_out = int(out["factor_value"].notna().sum())
        logger.info(f"LobbyIntensity({date}): {n_out} / {len(out)} 有值")
        return out

    def _get_market_cap(self, date: str, tickers: list[str]) -> pd.DataFrame:
        date_ts = pd.Timestamp(date)
        start_ts = date_ts - pd.Timedelta(days=200)

        # 优先走缓存（列名: ticker, date, market_capitalization, enterprise_value）
        cache = self._static_cache.get("_alpha_ev")
        if cache is not None and not cache.empty:
            mask = (
                cache["ticker"].isin(tickers)
                & (cache["date"] >= start_ts)
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

        # fallback ORM
        from stocks.models import USEnterpriseValue

        start = start_ts.date()
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
