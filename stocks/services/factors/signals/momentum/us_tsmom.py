"""Time-Series Momentum (Moskowitz, Ooi, Pedersen 2012, JFE)

    FACTOR = sign(R_12M - RF_12M) × |R_12M - RF_12M|

时序（趋势跟踪型）动量——不是和其他股票比，而是**自己和自己历史比**。

原始 TSMOM 方向由过去 12 个月**绝对**超额收益符号决定：
    如果过去 12M 跑赢无风险利率 → 信号 = +|超额收益|
    如果过去 12M 跑输无风险利率 → 信号 = -|超额收益|

这个因子在 CTA / 全球宏观 / 多资产中广泛使用。在股票截面上相对特别，
但仍能捕捉"强者恒强"的趋势特征。

简化实现（日度数据）：
    TSMOM = R_12M × sign(R_12M)  = |R_12M|  # 仅做多趋势强的
    或者保留符号版：R_12M × sign(R_12M - RF) （需 RF）

我们用最简洁版：12M 绝对收益率 × 方向。RF 用 FF5 的 RF 近似。

方向：+1（高值 = 强趋势 → 利好）。
"""

import logging

import numpy as np
import pandas as pd

from services.config import LOG_LEVEL
from stocks.services.factors.us_registry import AlphaSignal, register

logger = logging.getLogger(__name__)
logger.setLevel(LOG_LEVEL)


@register
class Tsmom(AlphaSignal):
    """Time-Series Momentum（12M 超额累计收益 × 符号）。"""

    name = "TSMOM"
    version = "v1"
    category = "momentum"
    horizon = "month"
    expected_icir = 0.06
    status = "staging"
    inherent_direction = 0  # 由滚动 IC 决定（TSMOM 在某些 Regime 可能反转）
    data_deps = ["us_daily_price"]
    ic_window_months = 12

    _LOOKBACK_DAYS = 380  # 252 交易日

    def compute(self, date: str, universe: pd.DataFrame) -> pd.DataFrame:
        tickers = universe["ticker"].tolist()
        if not tickers:
            logger.debug("Tsmom: 空 universe")
            return pd.DataFrame(columns=["ticker", "factor_value"])

        hist = self.fetch_price_history(
            date, tickers, lookback_days=self._LOOKBACK_DAYS, columns=["adj_close"]
        )
        if hist.empty:
            logger.warning(f"Tsmom({date}): 无价格数据")
            return pd.DataFrame(columns=["ticker", "factor_value"])

        # 取过去 ~12 月累计收益
        rows = []
        for ticker, grp in hist.groupby("ticker", sort=False):
            prices = grp["adj_close"].dropna().values
            if len(prices) < 200:  # 至少 ~10 个月
                continue
            start_price = prices[0]
            end_price = prices[-1]
            if pd.isna(start_price) or pd.isna(end_price) or start_price <= 0:
                continue
            r12 = (end_price / start_price) - 1.0
            # TSMOM = r × sign(r)  =  |r|  — 用绝对值作为"趋势强度"
            # 但这会把下跌也当成强信号，所以我们保留符号版：
            # 纯 TSMOM = r（让截面标准化自然形成 z-score，方向由滚动 IC 决定）
            rows.append({"ticker": ticker, "factor_value": r12})

        if not rows:
            logger.warning(f"Tsmom({date}): 无有效 ticker")
            return pd.DataFrame(columns=["ticker", "factor_value"])
        out = pd.DataFrame(rows)
        logger.info(f"Tsmom({date}): {len(out)} 有值")
        return out
