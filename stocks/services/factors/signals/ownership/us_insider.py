"""Insider Net Buy 因子 (Lakonishok & Lee 2001, RFS)

定义：
    INSIDER_NET_BUY = (买入金额 - 卖出金额) / (买入金额 + 卖出金额)
    范围 [-1, +1]，过去 90 天窗口。

    FMP 数据字段：
    - acquisition_or_disposition: 'A' = 买入, 'D' = 卖出
    - securities_transacted × price = 交易金额

经济直觉：
    - 内部人净买入 → 管理层认为股票被低估（信息优势）
    - Lakonishok & Lee (2001): insider buy 有预测力，sell 噪音较大
    - 正向因子：净买入比例越高 → 预期收益越高

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
class InsiderNetBuy(AlphaSignal):
    """Insider Net Buy — 内部人 90 天净买入比率。"""

    name = "INSIDER_NET_BUY"
    version = "v1"
    category = "ownership"
    horizon = "month"
    expected_icir = 0.10
    status = "staging"
    inherent_direction = +1
    data_deps = ["us_insider_trade"]
    ic_window_months = 12

    _WINDOW_DAYS = 90

    def compute(self, date: str, universe: pd.DataFrame) -> pd.DataFrame:
        tickers = universe["ticker"].tolist()
        if not tickers:
            logger.debug("InsiderNetBuy: 空 universe")
            return pd.DataFrame(columns=["ticker", "factor_value"])

        date_ts = pd.Timestamp(date)
        start_ts = date_ts - pd.Timedelta(days=self._WINDOW_DAYS)

        # ---- 优先从预加载缓存获取 ----
        # 缓存列: ticker, filing_date, net_value（已预计算 buy=+, sell=-）
        bulk = self._static_cache.get("_bulk_insider")
        if bulk is not None and not bulk.empty:
            mask = (
                bulk["ticker"].isin(tickers)
                & (bulk["filing_date"] >= start_ts)
                & (bulk["filing_date"] <= date_ts)
            )
            df = bulk.loc[mask, ["ticker", "net_value"]].copy()
            if df.empty:
                logger.warning(f"InsiderNetBuy({date}): 缓存中无内部人交易数据")
                return pd.DataFrame(columns=["ticker", "factor_value"])

            # net_value > 0 = 买入金额, < 0 = 卖出金额
            agg = df.groupby("ticker")["net_value"].agg(["sum", lambda x: x.abs().sum()])
            agg.columns = ["net", "total"]
            agg["total"] = agg["total"].replace(0, np.nan)
            agg["factor_value"] = agg["net"] / agg["total"]
            out = agg[["factor_value"]].reset_index()
            n_out = int(out["factor_value"].notna().sum())
            logger.info(f"InsiderNetBuy({date}): {n_out} / {len(out)} 有值 (cache)")
            return out[["ticker", "factor_value"]]

        # ---- fallback ORM ----
        logger.debug(f"InsiderNetBuy({date}): 缓存为空，fallback ORM")
        from stocks.models import USInsiderTrade

        start = start_ts.date()
        # FMP: acquisition_or_disposition 全 None，用 transaction_type 判断买卖
        # P-Purchase = 买入, S-Sale = 卖出
        qs = USInsiderTrade.objects.filter(
            ticker__in=tickers,
            transaction_date__gte=start,
            transaction_date__lte=date_ts.date(),
            securities_transacted__isnull=False,
            price__isnull=False,
            transaction_type__in=["P-Purchase", "S-Sale"],
        ).values_list("ticker", "transaction_type", "securities_transacted", "price")

        df = pd.DataFrame(list(qs), columns=["ticker", "txn_type", "shares", "price"])
        if df.empty:
            logger.warning(f"InsiderNetBuy({date}): ORM 无内部人交易数据")
            return pd.DataFrame(columns=["ticker", "factor_value"])

        df["shares"] = pd.to_numeric(df["shares"], errors="coerce")
        df["price"] = pd.to_numeric(df["price"], errors="coerce")
        df["amount"] = (df["shares"] * df["price"]).abs()

        # 分组汇总
        buy = df[df["txn_type"] == "P-Purchase"].groupby("ticker")["amount"].sum()
        sell = df[df["txn_type"] == "S-Sale"].groupby("ticker")["amount"].sum()

        # 合并
        agg = pd.DataFrame({"buy": buy, "sell": sell}).fillna(0)
        total = agg["buy"] + agg["sell"]
        total = total.replace(0, np.nan)
        ratio = (agg["buy"] - agg["sell"]) / total

        out = ratio.reset_index()
        out.columns = ["ticker", "factor_value"]
        n_out = int(out["factor_value"].notna().sum())
        logger.info(f"InsiderNetBuy({date}): {n_out} / {len(out)} 有值")
        return out[["ticker", "factor_value"]]
