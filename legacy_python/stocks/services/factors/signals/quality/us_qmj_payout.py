"""QMJ Payout 子项（Asness, Frazzini, Pedersen 2019）

Payout 衡量公司"把盈利变成股东现金"的能力。简化版：

    QMJ_NET_PAYOUT = (dividends + buybacks − net_issuance) / market_cap  (TTM)

FMP 字段注意：
    net_dividends_paid:       cash flow 表中已是负数（流出），我们取绝对值
    common_stock_repurchased: 负数（流出），取绝对值 = 回购金额
    net_stock_issuance:       正数=净发行，负数=净回购（此项是 common+preferred 合计）

简化处理：
    gross_dividends = |net_dividends_paid|
    gross_buyback   = |common_stock_repurchased|
    gross_issuance  = max(net_stock_issuance, 0)  # 只算新发行部分

    净对股东：gross_dividends + gross_buyback − gross_issuance
    除以 market_cap → payout yield（%）

方向：+1（股东收益越高越好）。
"""

import logging

import numpy as np
import pandas as pd

from services.config import LOG_LEVEL
from stocks.services.factors.us_registry import AlphaSignal, register

logger = logging.getLogger(__name__)
logger.setLevel(LOG_LEVEL)


@register
class QmjNetPayout(AlphaSignal):
    """QMJ Payout – Net Payout Yield TTM（股东净收益 / 市值）。"""

    name = "QMJ_NET_PAYOUT"
    version = "v1"
    category = "quality"
    horizon = "quarter"
    expected_icir = 0.10
    status = "staging"
    inherent_direction = +1
    data_deps = ["us_financial_data", "us_enterprise_value"]
    ic_window_months = 24

    _FIN_COLS = [
        "net_dividends_paid",
        "common_stock_repurchased",
        "net_stock_issuance",
    ]
    _TTM_Q = 4

    def compute(self, date: str, universe: pd.DataFrame) -> pd.DataFrame:
        tickers = universe["ticker"].tolist()
        if not tickers:
            logger.debug("QmjNetPayout: 空 universe")
            return pd.DataFrame(columns=["ticker", "factor_value"])

        hist = self.fetch_financial_history(date, tickers, self._FIN_COLS, n_quarters=self._TTM_Q)
        if hist.empty:
            logger.warning(f"QmjNetPayout({date}): 无财务数据")
            return pd.DataFrame(columns=["ticker", "factor_value"])

        agg_rows = []
        for ticker, grp in hist.groupby("ticker", sort=False):
            if len(grp) < 3:  # 至少 3Q 才给 TTM 近似
                continue
            gross_div = grp["net_dividends_paid"].abs().sum()
            gross_buy = grp["common_stock_repurchased"].abs().sum()
            issuance = grp["net_stock_issuance"].clip(lower=0).sum()
            net_payout = gross_div + gross_buy - issuance
            agg_rows.append({"ticker": ticker, "net_payout_ttm": net_payout})

        if not agg_rows:
            logger.warning(f"QmjNetPayout({date}): 无 ticker 有 ≥3Q 数据")
            return pd.DataFrame(columns=["ticker", "factor_value"])

        agg = pd.DataFrame(agg_rows)

        mktcap = self._get_market_cap_on(date, agg["ticker"].tolist())
        merged = agg.merge(mktcap, on="ticker", how="left")
        mc = merged["market_cap"].replace(0, np.nan)
        merged["factor_value"] = merged["net_payout_ttm"] / mc

        out = merged[["ticker", "factor_value"]].copy()
        n_out = int(out["factor_value"].notna().sum())
        logger.info(f"QmjNetPayout({date}): {n_out} / {len(out)} 有值")
        return out

    def _get_market_cap_on(self, date: str, tickers: list[str]) -> pd.DataFrame:
        date_ts = pd.Timestamp(date)

        # ---- 优先走缓存 ----
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

        # ---- ORM fallback ----
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
            logger.debug(f"QmjNetPayout._get_market_cap_on({date}): 无数据")
            return pd.DataFrame(columns=["ticker", "market_cap"])
        df["date"] = pd.to_datetime(df["date"])
        df = df.sort_values(["ticker", "date"], ascending=[True, False])
        df = df.drop_duplicates(subset=["ticker"], keep="first")
        df["market_cap"] = pd.to_numeric(df["market_cap"], errors="coerce")
        return df[["ticker", "market_cap"]].reset_index(drop=True)
