"""52-Week High Proximity (George & Hwang 2004, JF)

    FACTOR = P_t / max(P_{t-252...t})

判读：
    接近 1 = 股价接近过去 52 周高点 → 动量强 → 未来更可能突破（行为金融：锚定效应
    使投资者对"接近高点"的股票反应不足）

学术原文：George-Hwang 2004 发现这个因子的预测力超过传统 12-1 动量，
且解释了大部分 Jegadeesh-Titman 动量异象。

方向：+1（高值 = 利好）。
"""

import logging

import numpy as np
import pandas as pd

from services.config import LOG_LEVEL
from stocks.services.factors.us_registry import AlphaSignal, register

logger = logging.getLogger(__name__)
logger.setLevel(LOG_LEVEL)


@register
class Price52WHigh(AlphaSignal):
    """52-Week High Proximity：当前价 / 过去 52 周最高收盘价。"""

    name = "PRICE_52W_HIGH"
    version = "v1"
    category = "momentum"
    horizon = "month"
    expected_icir = 0.10
    status = "staging"
    inherent_direction = +1
    data_deps = ["us_daily_price"]
    ic_window_months = 12

    _LOOKBACK_DAYS = 380  # 252 交易日 ≈ 365 日历日，加 buffer

    def compute(self, date: str, universe: pd.DataFrame) -> pd.DataFrame:
        tickers = universe["ticker"].tolist()
        if not tickers:
            logger.debug("Price52WHigh: 空 universe")
            return pd.DataFrame(columns=["ticker", "factor_value"])

        hist = self.fetch_price_history(
            date, tickers, lookback_days=self._LOOKBACK_DAYS, columns=["adj_close"]
        )
        if hist.empty:
            logger.warning(f"Price52WHigh({date}): 无价格数据")
            return pd.DataFrame(columns=["ticker", "factor_value"])

        rows = []
        for ticker, grp in hist.groupby("ticker", sort=False):
            prices = grp["adj_close"].dropna().values
            if len(prices) < 60:  # 至少 3 个月日线才算
                continue
            current = prices[-1]
            high = prices.max()
            if pd.isna(current) or pd.isna(high) or high <= 0:
                continue
            rows.append({"ticker": ticker, "factor_value": current / high})

        if not rows:
            logger.warning(f"Price52WHigh({date}): 无有效 ticker")
            return pd.DataFrame(columns=["ticker", "factor_value"])
        out = pd.DataFrame(rows)
        logger.info(f"Price52WHigh({date}): {len(out)} 有值")
        return out
