"""Betting Against Beta 因子 (Frazzini & Pedersen 2014, JFE)

BAB 核心洞察：
    - 高杠杆约束投资者只能买高 beta 股票 → 高 beta 被高估
    - 低 beta 股票长期跑赢 CAPM 预测 → 低 beta 异象

因子定义：
    BAB_SIGNAL = -β_market

    即直接用市场 beta 的负数作为因子值。
    低 beta 股票信号高（做多），高 beta 股票信号低（做空）。

实现：
    β 用过去 252 个交易日，个股日收益率 vs 市场日收益率 OLS 回归。
    市场收益率用 FF5 的 Mkt-RF + RF。

    Frazzini-Pedersen 原文用 3 天重叠收益 + Vasicek shrinkage (先验 β=1)：
    β_shrunk = 0.6 × β_TS + 0.4 × 1.0
    这里简化为普通 OLS + shrinkage。

因子方向：+1（高信号 = 低 beta = 利好）
实际上 factor_value = -beta，inherent_direction = +1 等价于"低 beta 做多"。
"""

import logging

import numpy as np
import pandas as pd

from services.config import LOG_LEVEL
from stocks.services.factors.us_registry import AlphaSignal, register

logger = logging.getLogger(__name__)
logger.setLevel(LOG_LEVEL)

_SHRINKAGE_WEIGHT = 0.6  # Vasicek shrinkage: β_shrunk = w·β_OLS + (1-w)·1.0


@register
class BettingAgainstBeta(AlphaSignal):
    """BAB — 市场 Beta 的负数（低 beta 做多）。"""

    name = "BAB_BETA"
    version = "v1"
    category = "defensive"
    horizon = "month"
    expected_icir = 0.15
    status = "staging"
    inherent_direction = +1  # 高信号(= 低 beta) = 利好
    data_deps = ["us_daily_price", "ff5_daily"]
    ic_window_months = 18

    _LOOKBACK_DAYS = 380  # ~252 交易日 + buffer
    _MIN_OBS = 120  # 至少 120 天有效观测

    def compute(self, date: str, universe: pd.DataFrame) -> pd.DataFrame:
        tickers = universe["ticker"].tolist()
        if not tickers:
            logger.debug("BAB: 空 universe")
            return pd.DataFrame(columns=["ticker", "factor_value"])

        # 取 FF5 获取 Mkt-RF 作为市场收益率
        ff5 = self.fetch_ff5_factors()
        if ff5.empty:
            logger.warning(f"BAB({date}): FF5 数据为空")
            return pd.DataFrame(columns=["ticker", "factor_value"])

        hist = self.fetch_price_history(
            date, tickers, lookback_days=self._LOOKBACK_DAYS, columns=["adj_close"]
        )
        if hist.empty:
            logger.warning(f"BAB({date}): 无价格数据")
            return pd.DataFrame(columns=["ticker", "factor_value"])

        date_ts = pd.Timestamp(date)
        mkt = ff5[["Mkt-RF", "RF"]].copy()
        mkt = mkt[mkt.index <= date_ts]
        mkt = mkt.tail(300)  # 稍多取一些，inner join 后保证够

        rows = []
        for ticker, grp in hist.groupby("ticker", sort=False):
            prices = grp.set_index("trade_date")["adj_close"].dropna().sort_index()
            if len(prices) < self._MIN_OBS:
                continue

            rets = prices.pct_change().dropna()
            rets.name = "r"

            # 对齐
            aligned = pd.merge(
                rets, mkt, left_index=True, right_index=True, how="inner"
            )
            if len(aligned) < self._MIN_OBS:
                continue

            # 超额收益
            y = (aligned["r"] - aligned["RF"]).values
            x = aligned["Mkt-RF"].values

            # OLS beta: β = Cov(y, x) / Var(x)
            x_dm = x - x.mean()
            beta_ols = np.dot(x_dm, y - y.mean()) / np.dot(x_dm, x_dm)

            # Vasicek shrinkage toward 1.0
            beta_shrunk = _SHRINKAGE_WEIGHT * beta_ols + (1 - _SHRINKAGE_WEIGHT) * 1.0

            # BAB 信号 = -beta（低 beta 做多）
            rows.append({"ticker": ticker, "factor_value": -beta_shrunk})

        if not rows:
            logger.warning(f"BAB({date}): 无有效 ticker")
            return pd.DataFrame(columns=["ticker", "factor_value"])

        out = pd.DataFrame(rows)
        n_out = int(out["factor_value"].notna().sum())
        logger.info(f"BAB({date}): {n_out} 有值")
        return out
