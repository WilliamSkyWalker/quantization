"""Betting Against Beta 因子 (Frazzini & Pedersen 2014, JFE)

BAB 核心洞察：
    - 高杠杆约束投资者只能买高 beta 股票 → 高 beta 被高估
    - 低 beta 股票长期跑赢 CAPM 预测 → 低 beta 异象

因子定义：
    BAB_SIGNAL = -β_market

    即直接用市场 beta 的负数作为因子值。
    低 beta 股票信号高（做多），高 beta 股票信号低（做空）。

实现（向量化）：
    1. 把日线 pivot 成 wide-format 收益率矩阵 (date × ticker)
    2. 与 Mkt-RF 对齐
    3. β = Cov(r_ex, MktRF) / Var(MktRF)，一次算所有 ticker
    4. Vasicek shrinkage: β_shrunk = 0.6·β_OLS + 0.4·1.0

因子方向：+1（高信号 = 低 beta = 利好）
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

        # --- 向量化：pivot 成 wide-format ---
        # 排除和 FF 列名冲突的 ticker（极少数如 'RF'）
        _FF_COLS = {"Mkt-RF", "SMB", "HML", "RMW", "CMA", "RF"}
        hist = hist[~hist["ticker"].isin(_FF_COLS)]

        wide = hist.pivot(index="trade_date", columns="ticker", values="adj_close")
        rets = wide.pct_change().iloc[1:]  # (T, N) 日收益率矩阵

        # 对齐 FF 因子
        mkt = ff5[["Mkt-RF", "RF"]].copy()
        mkt = mkt[mkt.index <= date_ts]
        aligned = rets.join(mkt, how="inner")
        if len(aligned) < self._MIN_OBS:
            logger.warning(f"BAB({date}): 对齐后天数不足 {len(aligned)}")
            return pd.DataFrame(columns=["ticker", "factor_value"])

        # 提取矩阵
        ticker_cols = [c for c in aligned.columns if c not in ("Mkt-RF", "RF")]
        R = aligned[ticker_cols].values  # (T, N)
        rf = aligned["RF"].values[:, None]  # (T, 1)
        mkt_rf = aligned["Mkt-RF"].values  # (T,)

        R_ex = R - rf  # 超额收益矩阵 (T, N)

        # 每列有效观测数 mask
        valid = np.isfinite(R_ex)  # (T, N)
        n_valid = valid.sum(axis=0)  # (N,)

        # β = Cov(R_ex, MktRF) / Var(MktRF)，按列向量化
        # 用 nanmean 处理 NaN
        mkt_dm = mkt_rf - np.nanmean(mkt_rf)  # (T,)
        mkt_var = np.nansum(mkt_dm ** 2)

        if mkt_var < 1e-15:
            logger.warning(f"BAB({date}): MktRF 方差为零")
            return pd.DataFrame(columns=["ticker", "factor_value"])

        # 对 NaN 位置置 0 参与运算（不影响 cov，因为 x_dm 对应位置也参与）
        R_ex_clean = np.where(valid, R_ex, 0.0)
        # 每列均值（只对有效观测）
        col_means = np.nanmean(R_ex, axis=0)  # (N,)
        R_ex_dm = R_ex_clean - np.where(valid, col_means[None, :], 0.0)  # (T, N)

        cov_vec = (R_ex_dm * mkt_dm[:, None]).sum(axis=0)  # (N,)
        beta_ols = cov_vec / mkt_var  # (N,)

        # Vasicek shrinkage
        beta_shrunk = _SHRINKAGE_WEIGHT * beta_ols + (1 - _SHRINKAGE_WEIGHT) * 1.0

        # 过滤观测不足的 ticker
        bab_signal = -beta_shrunk
        bab_signal[n_valid < self._MIN_OBS] = np.nan

        out = pd.DataFrame({
            "ticker": ticker_cols,
            "factor_value": bab_signal,
        })
        out = out.dropna(subset=["factor_value"])
        n_out = len(out)
        logger.info(f"BAB({date}): {n_out} 有值")
        return out[["ticker", "factor_value"]].reset_index(drop=True)
