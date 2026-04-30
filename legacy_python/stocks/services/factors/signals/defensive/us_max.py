"""MAX 因子 (Bali, Cakici, Whitelaw 2011, JFE)

定义：
    MAX = max(R_i,d)  for d in [t-21, t]  （过去 1 个月最大单日收益率）

经济直觉：
    - 彩票型偏好：散户偏爱"可能暴涨"的股票 → 高 MAX 股被高估
    - 高 MAX 股票后续收益显著低于低 MAX 股票
    - 反向因子

扩展（可选）：
    - MAX5 = 过去 1 月 Top 5 日均值（更稳健）
    - 这里用 MAX5 版本

因子方向：-1（高 MAX = 利空）
"""

import logging

import numpy as np
import pandas as pd

from services.config import LOG_LEVEL
from stocks.services.factors.us_registry import AlphaSignal, register

logger = logging.getLogger(__name__)
logger.setLevel(LOG_LEVEL)

_TOP_N = 5  # MAX5: 取最大 5 天均值


@register
class MaxReturn(AlphaSignal):
    """MAX5 — 过去 1 月 Top-5 单日收益率均值（反向）。"""

    name = "MAX_RET"
    version = "v1"
    category = "defensive"
    horizon = "month"
    expected_icir = 0.12
    status = "staging"
    inherent_direction = -1
    data_deps = ["us_daily_price"]
    ic_window_months = 12

    _LOOKBACK_DAYS = 35  # ~21 交易日 + buffer
    _MIN_DAYS = 15  # 至少 15 天观测

    def compute(self, date: str, universe: pd.DataFrame) -> pd.DataFrame:
        tickers = universe["ticker"].tolist()
        if not tickers:
            logger.debug("MaxReturn: 空 universe")
            return pd.DataFrame(columns=["ticker", "factor_value"])

        hist = self.fetch_price_history(
            date, tickers, lookback_days=self._LOOKBACK_DAYS, columns=["adj_close"]
        )
        if hist.empty:
            logger.warning(f"MaxReturn({date}): 无价格数据")
            return pd.DataFrame(columns=["ticker", "factor_value"])

        rows = []
        for ticker, grp in hist.groupby("ticker", sort=False):
            prices = grp["adj_close"].dropna().values
            if len(prices) < self._MIN_DAYS:
                continue

            rets = np.diff(prices) / prices[:-1]  # 日收益率
            if len(rets) < self._MIN_DAYS:
                continue

            # Top-5 最大日收益率均值
            top_n = min(_TOP_N, len(rets))
            max5 = np.sort(rets)[-top_n:].mean()
            rows.append({"ticker": ticker, "factor_value": float(max5)})

        if not rows:
            logger.warning(f"MaxReturn({date}): 无有效 ticker")
            return pd.DataFrame(columns=["ticker", "factor_value"])

        out = pd.DataFrame(rows)
        n_out = int(out["factor_value"].notna().sum())
        logger.info(f"MaxReturn({date}): {n_out} 有值")
        return out
