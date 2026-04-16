"""Residual Momentum (Blitz, Huij, Martens 2011, JEF)

    R_it = α_i + β₁·(Mkt-RF)_t + β₂·SMB_t + β₃·HML_t + ε_it

    FACTOR_it = cumsum(ε over past 12-1 months)

剔除 FF3 因子暴露后的残差动量。比纯 12-1 动量：
    - 更稳定（剔除风格暴露 → 熊市/牛市风格切换时不被打爆）
    - IC 更持续（不依赖风格红利）
    - 实施上更稳（少受行业集中度影响）

实现：
    1. 拉过去 ~14 月日线 + FF3 日频因子
    2. 用过去 ~12 月（skip 最近 1 月）的日度数据跑 OLS 回归
    3. 取回归残差的累加 = residual momentum
    4. 返回

学术证据：Blitz-Huij-Martens 2011 发现 residual momentum 在全球股票中
ICIR 约为纯 momentum 的 2 倍，Sharpe 更高。

方向：+1（高残差累加 = 风格无法解释的"真正 alpha 动量"→ 利好）。
"""

import logging

import numpy as np
import pandas as pd

from services.config import LOG_LEVEL
from stocks.services.factors.us_registry import AlphaSignal, register

logger = logging.getLogger(__name__)
logger.setLevel(LOG_LEVEL)


@register
class ResidualMomFF3(AlphaSignal):
    """Residual Momentum：剔除 FF3（Mkt-RF/SMB/HML）后的残差累加动量。"""

    name = "RESIDUAL_MOM_FF3"
    version = "v1"
    category = "momentum"
    horizon = "month"
    expected_icir = 0.18
    status = "staging"
    inherent_direction = +1
    data_deps = ["us_daily_price", "ff5_daily"]
    ic_window_months = 12

    _LOOKBACK_DAYS = 430  # ~14 月日线（12M + 1M skip + buffer）
    _SKIP_DAYS = 21       # 最近 1 月跳过（经典 12-1 动量）
    _REG_DAYS = 252       # 用过去 252 日估计 β（约 12 月）

    def compute(self, date: str, universe: pd.DataFrame) -> pd.DataFrame:
        tickers = universe["ticker"].tolist()
        if not tickers:
            logger.debug("ResidualMomFF3: 空 universe")
            return pd.DataFrame(columns=["ticker", "factor_value"])

        # 1. FF3 日度数据
        ff5 = self.fetch_ff5_factors()
        if ff5.empty:
            logger.warning(f"ResidualMomFF3({date}): 无 FF5 数据")
            return pd.DataFrame(columns=["ticker", "factor_value"])
        ff3 = ff5[["Mkt-RF", "SMB", "HML", "RF"]].copy()

        # 2. 价格
        hist = self.fetch_price_history(
            date, tickers, lookback_days=self._LOOKBACK_DAYS, columns=["adj_close"]
        )
        if hist.empty:
            logger.warning(f"ResidualMomFF3({date}): 无价格")
            return pd.DataFrame(columns=["ticker", "factor_value"])

        date_ts = pd.Timestamp(date)
        skip_cutoff = date_ts - pd.Timedelta(days=self._SKIP_DAYS)

        rows = []
        for ticker, grp in hist.groupby("ticker", sort=False):
            grp = grp.sort_values("trade_date")
            # 只保留 skip_cutoff 之前的数据（跳过最近 1 月）
            grp = grp[grp["trade_date"] <= skip_cutoff]
            if len(grp) < 150:  # 至少 ~7 月日线
                continue

            prices = grp["adj_close"].dropna()
            if len(prices) < 150:
                continue

            # 日收益
            rets = prices.pct_change().dropna()
            rets.index = grp["trade_date"].iloc[1:len(rets) + 1].values
            rets = rets[~np.isnan(rets.values)]

            # 对齐 FF3（按日期 inner join）
            aligned = pd.merge(
                rets.rename("r").to_frame(),
                ff3,
                left_index=True,
                right_index=True,
                how="inner",
            )
            if len(aligned) < 150:
                continue

            # 构造超额收益
            aligned["r_ex"] = aligned["r"] - aligned["RF"]

            # OLS: r_ex ~ α + β₁·MktRF + β₂·SMB + β₃·HML
            X = aligned[["Mkt-RF", "SMB", "HML"]].values
            X = np.column_stack([np.ones(len(X)), X])  # intercept
            y = aligned["r_ex"].values
            try:
                beta, *_ = np.linalg.lstsq(X, y, rcond=None)
                residuals = y - X @ beta
            except np.linalg.LinAlgError:
                continue

            # residual momentum = cumulative residual 过去 252 日（约 12M）
            resid_12m = residuals[-self._REG_DAYS:]
            if len(resid_12m) < 100:
                continue
            # 用累加而非累乘（Blitz 2011 原文做法：log-ret 的 simple sum）
            cum_resid = resid_12m.sum()
            rows.append({"ticker": ticker, "factor_value": float(cum_resid)})

        if not rows:
            logger.warning(f"ResidualMomFF3({date}): 无有效 ticker")
            return pd.DataFrame(columns=["ticker", "factor_value"])
        out = pd.DataFrame(rows)
        logger.info(f"ResidualMomFF3({date}): {len(out)} 有值")
        return out
