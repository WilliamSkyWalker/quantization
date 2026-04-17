"""Downside Beta 因子 (Ang, Chen, Xing 2006, RFS)

定义：
    β_down = Cov(R_i, R_m | R_m < μ_m) / Var(R_m | R_m < μ_m)

    只在市场下跌日（R_m < 均值）计算 beta。

经济直觉：
    - 下行 beta 高的股票在市场下跌时跌更多 → 承担更多下行风险
    - 反向因子：高 β_down = 利空

实现（向量化）：
    1. Pivot 成 wide-format 收益率矩阵
    2. 用 down-day mask 过滤
    3. 矩阵 beta 一次算所有 ticker

因子方向：-1
"""

import logging

import numpy as np
import pandas as pd

from services.config import LOG_LEVEL
from stocks.services.factors.us_registry import AlphaSignal, register

logger = logging.getLogger(__name__)
logger.setLevel(LOG_LEVEL)


@register
class DownsideBeta(AlphaSignal):
    """Downside Beta — 仅市场下跌日的 Beta（反向）。"""

    name = "DOWNSIDE_BETA"
    version = "v1"
    category = "defensive"
    horizon = "month"
    expected_icir = 0.10
    status = "staging"
    inherent_direction = -1
    data_deps = ["us_daily_price", "ff5_daily"]
    ic_window_months = 18

    _LOOKBACK_DAYS = 380  # ~252 交易日
    _MIN_DOWN_DAYS = 50  # 至少 50 个下跌日

    def compute(self, date: str, universe: pd.DataFrame) -> pd.DataFrame:
        tickers = universe["ticker"].tolist()
        if not tickers:
            logger.debug("DownsideBeta: 空 universe")
            return pd.DataFrame(columns=["ticker", "factor_value"])

        ff5 = self.fetch_ff5_factors()
        if ff5.empty:
            logger.warning(f"DownsideBeta({date}): FF5 数据为空")
            return pd.DataFrame(columns=["ticker", "factor_value"])

        hist = self.fetch_price_history(
            date, tickers, lookback_days=self._LOOKBACK_DAYS, columns=["adj_close"]
        )
        if hist.empty:
            logger.warning(f"DownsideBeta({date}): 无价格数据")
            return pd.DataFrame(columns=["ticker", "factor_value"])

        date_ts = pd.Timestamp(date)

        # --- 向量化：pivot wide ---
        _FF_COLS = {"Mkt-RF", "SMB", "HML", "RMW", "CMA", "RF", "r_mkt"}
        hist = hist[~hist["ticker"].isin(_FF_COLS)]

        wide = hist.pivot(index="trade_date", columns="ticker", values="adj_close")
        rets = wide.pct_change().iloc[1:]

        mkt = ff5[["Mkt-RF", "RF"]].copy()
        mkt = mkt[mkt.index <= date_ts]
        mkt["r_mkt"] = mkt["Mkt-RF"] + mkt["RF"]

        aligned = rets.join(mkt, how="inner")
        if len(aligned) < 60:
            logger.warning(f"DownsideBeta({date}): 对齐后天数不足")
            return pd.DataFrame(columns=["ticker", "factor_value"])

        ticker_cols = [c for c in aligned.columns if c not in ("Mkt-RF", "RF", "r_mkt")]

        # 只保留市场下跌日
        r_mkt = aligned["r_mkt"].values
        mkt_mean = np.nanmean(r_mkt)
        down_mask = r_mkt < mkt_mean  # (T,) bool

        n_down = down_mask.sum()
        if n_down < self._MIN_DOWN_DAYS:
            logger.warning(f"DownsideBeta({date}): 市场下跌日仅 {n_down}")
            return pd.DataFrame(columns=["ticker", "factor_value"])

        # 下跌日的数据
        R = aligned[ticker_cols].values[down_mask]  # (T_down, N)
        rf = aligned["RF"].values[down_mask, None]  # (T_down, 1)
        mkt_rf_down = aligned["Mkt-RF"].values[down_mask]  # (T_down,)

        R_ex = R - rf  # (T_down, N)

        # 有效观测计数
        valid = np.isfinite(R_ex)
        n_valid = valid.sum(axis=0)

        # β_down = Cov(R_ex, MktRF_down) / Var(MktRF_down)
        mkt_dm = mkt_rf_down - np.nanmean(mkt_rf_down)
        mkt_var = np.nansum(mkt_dm ** 2)

        if mkt_var < 1e-15:
            logger.warning(f"DownsideBeta({date}): MktRF 下行方差为零")
            return pd.DataFrame(columns=["ticker", "factor_value"])

        R_ex_clean = np.where(valid, R_ex, 0.0)
        col_means = np.nanmean(R_ex, axis=0)
        R_ex_dm = R_ex_clean - np.where(valid, col_means[None, :], 0.0)

        cov_vec = (R_ex_dm * mkt_dm[:, None]).sum(axis=0)
        beta_down = cov_vec / mkt_var

        beta_down[n_valid < self._MIN_DOWN_DAYS] = np.nan

        out = pd.DataFrame({
            "ticker": ticker_cols,
            "factor_value": beta_down,
        })
        out = out.dropna(subset=["factor_value"])
        n_out = len(out)
        logger.info(f"DownsideBeta({date}): {n_out} 有值")
        return out[["ticker", "factor_value"]].reset_index(drop=True)
