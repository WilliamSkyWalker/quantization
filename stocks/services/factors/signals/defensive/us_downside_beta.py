"""Downside Beta 因子 (Ang, Chen, Xing 2006, RFS)

定义：
    β_down = Cov(R_i, R_m | R_m < μ_m) / Var(R_m | R_m < μ_m)

    只在市场下跌日（R_m < 均值）计算 beta。

经济直觉：
    - 下行 beta 高的股票在市场下跌时跌更多 → 承担更多下行风险
    - 投资者应该要求更高溢价？ → 学术结论：实际上高 β_down 后续收益更低
    - 可能是因为散户低估了下行风险（Ang et al. 2006）
    - 反向因子：高 β_down = 利空

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
        mkt = ff5[["Mkt-RF", "RF"]].copy()
        mkt = mkt[mkt.index <= date_ts].tail(300)
        # 市场总收益 = Mkt-RF + RF
        mkt["r_mkt"] = mkt["Mkt-RF"] + mkt["RF"]
        mkt_mean = mkt["r_mkt"].mean()

        rows = []
        for ticker, grp in hist.groupby("ticker", sort=False):
            prices = grp.set_index("trade_date")["adj_close"].dropna().sort_index()
            if len(prices) < 120:
                continue

            rets = prices.pct_change().dropna()
            rets.name = "r"

            aligned = pd.merge(
                rets, mkt[["r_mkt", "RF"]], left_index=True, right_index=True, how="inner"
            )

            # 只保留市场下跌日
            down_mask = aligned["r_mkt"] < mkt_mean
            down = aligned[down_mask]
            if len(down) < self._MIN_DOWN_DAYS:
                continue

            y = (down["r"] - down["RF"]).values
            x = (down["r_mkt"] - down["RF"]).values

            # OLS: β_down = Cov(y, x) / Var(x)
            x_dm = x - x.mean()
            var_x = np.dot(x_dm, x_dm)
            if var_x < 1e-15:
                continue
            beta_down = np.dot(x_dm, y - y.mean()) / var_x

            rows.append({"ticker": ticker, "factor_value": float(beta_down)})

        if not rows:
            logger.warning(f"DownsideBeta({date}): 无有效 ticker")
            return pd.DataFrame(columns=["ticker", "factor_value"])

        out = pd.DataFrame(rows)
        n_out = int(out["factor_value"].notna().sum())
        logger.info(f"DownsideBeta({date}): {n_out} 有值")
        return out
