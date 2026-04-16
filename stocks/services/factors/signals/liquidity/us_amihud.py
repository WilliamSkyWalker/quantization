"""Amihud Illiquidity 因子 (Amihud 2002, JFM)

定义：
    ILLIQ = (1/D) × Σ |R_d| / Volume_d    (过去 D 个交易日)

    D = 21 天（1 个月）

经济直觉：
    - ILLIQ 衡量"每单位成交量引起的价格变动"→ 高 ILLIQ = 流动性差
    - 非流动性溢价：流动性差的股票应有更高预期收益（Amihud 2002）
    - 但实证中高 ILLIQ 小票容易暴跌 → 反向更常见

    这里 inherent_direction = 0，让滚动 IC 决定方向。

注意：
    Volume 用美元交易量 = close × volume（单位对齐）。
    FMP 的 volume 是股数，需要 × close 转换。

因子方向：0（由滚动 IC 决定）
"""

import logging

import numpy as np
import pandas as pd

from services.config import LOG_LEVEL
from stocks.services.factors.us_registry import AlphaSignal, register

logger = logging.getLogger(__name__)
logger.setLevel(LOG_LEVEL)


@register
class AmihudIlliquidity(AlphaSignal):
    """Amihud Illiquidity — |R| / DollarVolume 月均（非流动性）。"""

    name = "AMIHUD_ILLIQ"
    version = "v1"
    category = "liquidity"
    horizon = "month"
    expected_icir = 0.08
    status = "staging"
    inherent_direction = 0  # 方向由滚动 IC 决定
    data_deps = ["us_daily_price"]
    ic_window_months = 12

    _LOOKBACK_DAYS = 35  # ~21 交易日 + buffer
    _MIN_DAYS = 15

    def compute(self, date: str, universe: pd.DataFrame) -> pd.DataFrame:
        tickers = universe["ticker"].tolist()
        if not tickers:
            logger.debug("AmihudIlliq: 空 universe")
            return pd.DataFrame(columns=["ticker", "factor_value"])

        hist = self.fetch_price_history(
            date, tickers, lookback_days=self._LOOKBACK_DAYS,
            columns=["adj_close", "close", "volume"],
        )
        if hist.empty:
            logger.warning(f"AmihudIlliq({date}): 无价格数据")
            return pd.DataFrame(columns=["ticker", "factor_value"])

        rows = []
        for ticker, grp in hist.groupby("ticker", sort=False):
            grp = grp.dropna(subset=["adj_close", "close", "volume"])
            if len(grp) < self._MIN_DAYS:
                continue

            prices = grp["adj_close"].values
            close = grp["close"].values
            volume = grp["volume"].values

            # 日收益率
            rets = np.diff(prices) / prices[:-1]
            # 美元成交量（用 close × volume，和 |R| 取对应天数）
            dollar_vol = close[1:] * volume[1:]

            # 过滤掉零成交量
            valid = dollar_vol > 0
            if valid.sum() < self._MIN_DAYS:
                continue

            illiq = np.abs(rets[valid]) / dollar_vol[valid]
            # 月均值
            amihud = float(illiq.mean())

            # 取 log 防极值（Amihud 原文也建议 log 化）
            if amihud > 0:
                amihud = np.log(amihud)
            else:
                continue

            rows.append({"ticker": ticker, "factor_value": amihud})

        if not rows:
            logger.warning(f"AmihudIlliq({date}): 无有效 ticker")
            return pd.DataFrame(columns=["ticker", "factor_value"])

        out = pd.DataFrame(rows)
        n_out = int(out["factor_value"].notna().sum())
        logger.info(f"AmihudIlliq({date}): {n_out} 有值")
        return out
