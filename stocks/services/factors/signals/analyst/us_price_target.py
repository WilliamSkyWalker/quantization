"""Price Target Ratio 因子

定义：
    PTR = target_consensus / current_price

    用 USPriceTarget（snapshot 表，无历史日期）的 consensus 目标价
    除以截面日最近收盘价。

经济直觉：
    - PTR > 1 → 分析师认为当前价格被低估
    - PTR < 1 → 分析师认为当前价格被高估
    - 正向因子：PTR 越高 → 预期收益越高

注意：
    - USPriceTarget 是 snapshot 表（无 date），只有当前最新值
    - 回测中存在前瞻偏差（无法知道历史某天的目标价）
    - 仅适用于实盘/近期回测，不适用于长历史回测

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
class PriceTargetRatio(AlphaSignal):
    """Price Target Ratio — 目标价 / 当前价。"""

    name = "PRICE_TARGET_RATIO"
    version = "v1"
    category = "analyst"
    horizon = "month"
    expected_icir = 0.10
    status = "staging"
    inherent_direction = +1
    data_deps = ["us_price_target", "us_daily_price"]
    ic_window_months = 12

    def compute(self, date: str, universe: pd.DataFrame) -> pd.DataFrame:
        tickers = universe["ticker"].tolist()
        if not tickers:
            logger.debug("PriceTargetRatio: 空 universe")
            return pd.DataFrame(columns=["ticker", "factor_value"])

        from stocks.models import USPriceTarget

        qs = USPriceTarget.objects.filter(
            ticker__in=tickers,
            target_consensus__isnull=False,
            target_consensus__gt=0,
        ).values_list("ticker", "target_consensus")

        pt = pd.DataFrame(list(qs), columns=["ticker", "target"])
        if pt.empty:
            logger.warning(f"PriceTargetRatio({date}): 无目标价数据")
            return pd.DataFrame(columns=["ticker", "factor_value"])

        pt["target"] = pd.to_numeric(pt["target"], errors="coerce")

        # 取截面日最近收盘价
        price = self._get_latest_price(date, pt["ticker"].tolist())
        if price.empty:
            logger.warning(f"PriceTargetRatio({date}): 无价格数据")
            return pd.DataFrame(columns=["ticker", "factor_value"])

        merged = pt.merge(price, on="ticker", how="inner")
        p = merged["close"].replace(0, np.nan)
        merged["factor_value"] = merged["target"] / p

        out = merged[["ticker", "factor_value"]].copy()
        n_out = int(out["factor_value"].notna().sum())
        logger.info(f"PriceTargetRatio({date}): {n_out} / {len(out)} 有值")
        return out

    @staticmethod
    def _get_latest_price(date: str, tickers: list[str]) -> pd.DataFrame:
        """取截面日最近收盘价。"""
        from stocks.models import USDailyPrice

        date_ts = pd.Timestamp(date)
        start = (date_ts - pd.Timedelta(days=10)).date()

        qs = USDailyPrice.objects.filter(
            ticker__in=tickers,
            trade_date__gte=start,
            trade_date__lte=date_ts.date(),
        ).values_list("ticker", "trade_date", "close")

        df = pd.DataFrame(list(qs), columns=["ticker", "trade_date", "close"])
        if df.empty:
            return pd.DataFrame(columns=["ticker", "close"])
        df["trade_date"] = pd.to_datetime(df["trade_date"])
        df = df.sort_values(["ticker", "trade_date"], ascending=[True, False])
        df = df.drop_duplicates(subset=["ticker"], keep="first")
        df["close"] = pd.to_numeric(df["close"], errors="coerce")
        return df[["ticker", "close"]].reset_index(drop=True)
