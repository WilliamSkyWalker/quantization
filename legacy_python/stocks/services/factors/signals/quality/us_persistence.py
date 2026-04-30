"""Earnings Persistence

盈利持续性 = 近 8 季 EPS 的一阶自相关 AR(1) 系数。

学术依据：Sloan (1996) 指出高持续性的盈利在未来更可能延续，是"高质量盈利"的特征。
持续性高的公司股价反应更理性，反之则多由应计/一次性损益驱动。

公式：
    ρ1 = Corr(EPS_t, EPS_{t-1})  over last 8 quarters

范围 [-1, +1]，越高越好。因子方向：+1。
"""

import logging

import numpy as np
import pandas as pd

from services.config import LOG_LEVEL
from stocks.services.factors.us_registry import AlphaSignal, register

logger = logging.getLogger(__name__)
logger.setLevel(LOG_LEVEL)


@register
class EarningsPersistence(AlphaSignal):
    """Earnings Persistence（近 8Q EPS 的 AR(1) 系数）。"""

    name = "EARNINGS_PERSISTENCE"
    version = "v1"
    category = "quality"
    horizon = "quarter"
    expected_icir = 0.08
    status = "staging"
    inherent_direction = +1
    data_deps = ["us_financial_data"]
    ic_window_months = 24

    _N_Q = 8
    _MIN_PAIRS = 5  # 至少 5 对 (t, t-1) 才算相关

    def compute(self, date: str, universe: pd.DataFrame) -> pd.DataFrame:
        tickers = universe["ticker"].tolist()
        if not tickers:
            logger.debug("EarningsPersistence: 空 universe")
            return pd.DataFrame(columns=["ticker", "factor_value"])

        hist = self.fetch_financial_history(date, tickers, ["eps"], n_quarters=self._N_Q)
        if hist.empty:
            logger.warning(f"EarningsPersistence({date}): 无数据")
            return pd.DataFrame(columns=["ticker", "factor_value"])

        rows = []
        for ticker, grp in hist.groupby("ticker", sort=False):
            # hist 按 date DESC，翻过来按时间递增做 AR(1)
            series = grp["eps"].values[::-1]
            series = series[~np.isnan(series)]
            if len(series) < self._MIN_PAIRS + 1:
                continue
            s_t = series[1:]
            s_tm1 = series[:-1]
            if np.std(s_t) < 1e-10 or np.std(s_tm1) < 1e-10:
                continue  # 常数序列无相关可言
            rho = np.corrcoef(s_t, s_tm1)[0, 1]
            if np.isnan(rho):
                continue
            rows.append({"ticker": ticker, "factor_value": rho})

        if not rows:
            logger.warning(f"EarningsPersistence({date}): 无有效 AR(1)")
            return pd.DataFrame(columns=["ticker", "factor_value"])

        out = pd.DataFrame(rows)
        logger.info(f"EarningsPersistence({date}): {len(out)} 有值")
        return out
