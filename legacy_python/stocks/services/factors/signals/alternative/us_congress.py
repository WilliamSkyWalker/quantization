"""Congress Net Buy 因子

定义：
    CONGRESS_NET_BUY = (买入笔数 - 卖出笔数) / 总笔数
    过去 90 天窗口。

    USCongressTrade 字段：
    - type: 'Purchase' = 买入, 'Sale'/'Sale (Full)'/'Sale (Partial)' = 卖出
    - amount: 范围字符串（'$1,001 - $15,000' 等），无精确数值

    因为金额不精确，用笔数比率。

经济直觉：
    - 国会议员有信息优势（Ziobrowski et al. 2004, 2011）
    - 国会议员的股票交易显著跑赢市场
    - 正向因子：净买入 = 利好

因子方向：+1
"""

import logging

import numpy as np
import pandas as pd

from services.config import LOG_LEVEL
from stocks.services.factors.us_registry import AlphaSignal, register

logger = logging.getLogger(__name__)
logger.setLevel(LOG_LEVEL)

_BUY_TYPES = {"Purchase"}
_SELL_TYPES = {"Sale", "Sale (Full)", "Sale (Partial)"}


@register
class CongressNetBuy(AlphaSignal):
    """Congress Net Buy — 国会议员 90 天净买入比率。"""

    name = "CONGRESS_NET_BUY"
    version = "v1"
    category = "alternative"
    horizon = "month"
    expected_icir = 0.08
    status = "staging"
    inherent_direction = +1
    data_deps = ["us_congress_trade"]
    ic_window_months = 12

    _WINDOW_DAYS = 90

    def compute(self, date: str, universe: pd.DataFrame) -> pd.DataFrame:
        tickers = universe["ticker"].tolist()
        if not tickers:
            logger.debug("CongressNetBuy: 空 universe")
            return pd.DataFrame(columns=["ticker", "factor_value"])

        date_ts = pd.Timestamp(date)
        start = (date_ts - pd.Timedelta(days=self._WINDOW_DAYS)).date()

        df = pd.DataFrame(columns=["ticker", "type"])

        # 优先从预加载缓存获取
        bulk = self._static_cache.get("_bulk_congress")
        if bulk is not None and not bulk.empty:
            mask = (
                bulk["ticker"].isin(tickers)
                & (bulk["transaction_date"] >= pd.Timestamp(start))
                & (bulk["transaction_date"] <= date_ts)
            )
            filtered = bulk[mask]
            if not filtered.empty:
                df = filtered[["ticker", "transaction_type"]].copy()
                df.columns = ["ticker", "type"]
                logger.debug(f"CongressNetBuy({date}): 缓存命中 {len(df)} 条")
            else:
                logger.debug(f"CongressNetBuy({date}): 缓存中无匹配数据")
        else:
            # fallback ORM
            from stocks.models import USCongressTrade

            qs = USCongressTrade.objects.filter(
                ticker__in=tickers,
                transaction_date__gte=start,
                transaction_date__lte=date_ts.date(),
            ).values_list("ticker", "type")
            df = pd.DataFrame(list(qs), columns=["ticker", "type"])
            logger.debug(f"CongressNetBuy({date}): ORM fallback {len(df)} 条")
        if df.empty:
            logger.debug(f"CongressNetBuy({date}): 无国会交易数据")
            return pd.DataFrame(columns=["ticker", "factor_value"])

        df["is_buy"] = df["type"].isin(_BUY_TYPES).astype(int)
        df["is_sell"] = df["type"].isin(_SELL_TYPES).astype(int)

        agg = df.groupby("ticker").agg(
            buys=("is_buy", "sum"),
            sells=("is_sell", "sum"),
            total=("type", "count"),
        ).reset_index()

        agg["total"] = agg["total"].replace(0, np.nan)
        agg["factor_value"] = (agg["buys"] - agg["sells"]) / agg["total"]

        out = agg[["ticker", "factor_value"]].copy()
        n_out = int(out["factor_value"].notna().sum())
        logger.info(f"CongressNetBuy({date}): {n_out} / {len(out)} 有值")
        return out
