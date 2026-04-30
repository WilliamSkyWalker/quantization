"""Frog-in-the-Pan (Da, Gurun, Warachka 2014, RFS)

    FACTOR = |sum(daily_returns over 12M)| × (%negative_days − %positive_days)

FIP 捕捉"连续小涨累积的动量"与"一次性大涨累积的动量"的差异：
    - 同样 +50% 12M 收益，如果是 250 天里 130 天上涨 120 天下跌（小步慢走）
      → 投资者吸收信息慢 → PEAD 效应强 → 未来动量延续
    - 如果 +50% 来自某 10 天暴涨
      → 信息一次性释放 → 未来反转

简化实现（Da-Gurun-Warachka 2014 原式）：
    FIP = sign(R_12M) × (%neg − %pos)

    其中 %neg = 过去 252D 里负收益天数占比，%pos = 正收益天数占比。

信号方向：
    - R_12M > 0 且 %neg > %pos（很多小亏但总体涨）→ FIP > 0 → 强延续信号
    - R_12M > 0 且 %neg < %pos（少数大涨带动）→ FIP < 0 → 可能反转

学术引用：Da-Gurun-Warachka 2014 发现 FIP 与动量正交，且"低 FIP 值"
股票的动量反转更快。

方向：+1（高 FIP = 连续累积动量 = 利好）。
"""

import logging

import numpy as np
import pandas as pd

from services.config import LOG_LEVEL
from stocks.services.factors.us_registry import AlphaSignal, register

logger = logging.getLogger(__name__)
logger.setLevel(LOG_LEVEL)


@register
class FrogInPan(AlphaSignal):
    """Frog-in-the-Pan：动量累积方式（连续小涨 vs 一次大涨）。"""

    name = "FROG_IN_PAN"
    version = "v1"
    category = "momentum"
    horizon = "month"
    expected_icir = 0.05
    status = "staging"
    inherent_direction = +1
    data_deps = ["us_daily_price"]
    ic_window_months = 12

    _LOOKBACK_DAYS = 380

    def compute(self, date: str, universe: pd.DataFrame) -> pd.DataFrame:
        tickers = universe["ticker"].tolist()
        if not tickers:
            logger.debug("FrogInPan: 空 universe")
            return pd.DataFrame(columns=["ticker", "factor_value"])

        hist = self.fetch_price_history(
            date, tickers, lookback_days=self._LOOKBACK_DAYS, columns=["adj_close"]
        )
        if hist.empty:
            logger.warning(f"FrogInPan({date}): 无价格数据")
            return pd.DataFrame(columns=["ticker", "factor_value"])

        rows = []
        for ticker, grp in hist.groupby("ticker", sort=False):
            prices = grp["adj_close"].dropna().values
            if len(prices) < 200:
                continue
            returns = np.diff(prices) / prices[:-1]
            returns = returns[~np.isnan(returns)]
            if len(returns) < 100:
                continue

            r_12m = prices[-1] / prices[0] - 1.0
            if abs(r_12m) < 1e-6:
                continue  # 无方向
            sign_r = np.sign(r_12m)
            pct_neg = (returns < 0).mean()
            pct_pos = (returns > 0).mean()

            # FIP 按原文：sign(R) × (pct_neg - pct_pos)
            # 如果总涨(sign=+1) 且更多小亏日 → +正值（累积好）
            # 如果总跌(sign=-1) 且更多小涨日 → +正值（累积的回撤，做空好）
            fip = sign_r * (pct_neg - pct_pos)
            # 乘上 |R_12M| 做幅度加权，让信号强度区分大/小动量
            fip *= abs(r_12m)
            rows.append({"ticker": ticker, "factor_value": fip})

        if not rows:
            logger.warning(f"FrogInPan({date}): 无有效 ticker")
            return pd.DataFrame(columns=["ticker", "factor_value"])
        out = pd.DataFrame(rows)
        logger.info(f"FrogInPan({date}): {len(out)} 有值")
        return out
