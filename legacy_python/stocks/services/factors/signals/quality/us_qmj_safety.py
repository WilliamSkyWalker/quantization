"""QMJ Safety 子项（Asness, Frazzini, Pedersen 2019, RFS）

Safety 衡量公司对市场冲击的韧性，由三个子项合成：
    - Low Leverage        债务负担
    - Low Earnings Vol    盈利波动性
    - Low ROE Vol         ROE 波动性

本模块实现 3 个独立因子（用户侧再合成 / 或给策略层加权）：

    QMJ_LEVERAGE       total_debt / total_stockholders_equity（负向：高杠杆=危险）
    QMJ_EARNINGS_VOL   trailing 20Q net_income std / |mean|（CV）
    QMJ_ROE_VOL        trailing 20Q ROE std（直接用 std，不做 CV）
"""

import logging

import numpy as np
import pandas as pd

from services.config import LOG_LEVEL
from stocks.services.factors.us_registry import AlphaSignal, register

logger = logging.getLogger(__name__)
logger.setLevel(LOG_LEVEL)


@register
class QmjLeverage(AlphaSignal):
    """QMJ Safety – Leverage（total_debt / equity，反向因子）。"""

    name = "QMJ_LEVERAGE"
    version = "v1"
    category = "quality"
    horizon = "quarter"
    expected_icir = 0.05
    status = "staging"
    inherent_direction = -1  # 高杠杆=危险
    data_deps = ["us_financial_data"]
    ic_window_months = 24

    def compute(self, date: str, universe: pd.DataFrame) -> pd.DataFrame:
        tickers = universe["ticker"].tolist()
        if not tickers:
            logger.debug("QmjLeverage: 空 universe")
            return pd.DataFrame(columns=["ticker", "factor_value"])

        fin = self.fetch_financial_latest(
            date, tickers, ["total_debt", "total_stockholders_equity"]
        )
        if fin.empty:
            logger.warning(f"QmjLeverage({date}): 无数据")
            return pd.DataFrame(columns=["ticker", "factor_value"])

        equity = fin["total_stockholders_equity"].replace(0, np.nan)
        # 负股本的公司直接置 NaN（杠杆无意义）
        equity = equity.where(equity > 0, np.nan)
        leverage = fin["total_debt"] / equity

        out = pd.DataFrame({"ticker": fin["ticker"], "factor_value": leverage})
        n_out = int(out["factor_value"].notna().sum())
        logger.info(f"QmjLeverage({date}): {n_out} / {len(out)} 有值")
        return out


@register
class QmjEarningsVol(AlphaSignal):
    """QMJ Safety – Earnings Volatility（20Q net_income CV，反向）。"""

    name = "QMJ_EARNINGS_VOL"
    version = "v1"
    category = "quality"
    horizon = "quarter"
    expected_icir = 0.05
    status = "staging"
    inherent_direction = -1  # 高波动=危险
    data_deps = ["us_financial_data"]
    ic_window_months = 24

    _MIN_Q = 12  # 至少 12 季才给结果

    def compute(self, date: str, universe: pd.DataFrame) -> pd.DataFrame:
        tickers = universe["ticker"].tolist()
        if not tickers:
            logger.debug("QmjEarningsVol: 空 universe")
            return pd.DataFrame(columns=["ticker", "factor_value"])

        hist = self.fetch_financial_history(date, tickers, ["net_income"], n_quarters=20)
        if hist.empty:
            logger.warning(f"QmjEarningsVol({date}): 无数据")
            return pd.DataFrame(columns=["ticker", "factor_value"])

        rows = []
        for ticker, grp in hist.groupby("ticker", sort=False):
            vals = grp["net_income"].dropna().values
            if len(vals) < self._MIN_Q:
                continue
            mean = np.mean(vals)
            std = np.std(vals, ddof=1)
            if abs(mean) < 1e-6:
                cv = np.nan  # 均值接近 0 时 CV 无意义
            else:
                cv = std / abs(mean)
            rows.append({"ticker": ticker, "factor_value": cv})

        if not rows:
            logger.warning(f"QmjEarningsVol({date}): 所有 ticker 季报 < {self._MIN_Q}")
            return pd.DataFrame(columns=["ticker", "factor_value"])
        out = pd.DataFrame(rows)
        logger.info(f"QmjEarningsVol({date}): {len(out)} 有值")
        return out


@register
class QmjRoeVol(AlphaSignal):
    """QMJ Safety – ROE Volatility（20Q ROE std，反向）。"""

    name = "QMJ_ROE_VOL"
    version = "v1"
    category = "quality"
    horizon = "quarter"
    expected_icir = 0.05
    status = "staging"
    inherent_direction = -1
    data_deps = ["us_financial_data"]
    ic_window_months = 24

    _MIN_Q = 12

    def compute(self, date: str, universe: pd.DataFrame) -> pd.DataFrame:
        tickers = universe["ticker"].tolist()
        if not tickers:
            logger.debug("QmjRoeVol: 空 universe")
            return pd.DataFrame(columns=["ticker", "factor_value"])

        hist = self.fetch_financial_history(
            date, tickers, ["net_income", "total_stockholders_equity"], n_quarters=20
        )
        if hist.empty:
            logger.warning(f"QmjRoeVol({date}): 无数据")
            return pd.DataFrame(columns=["ticker", "factor_value"])

        rows = []
        for ticker, grp in hist.groupby("ticker", sort=False):
            ni = grp["net_income"].values
            eq = grp["total_stockholders_equity"].replace(0, np.nan).values
            roe = ni / eq
            roe = roe[~np.isnan(roe)]
            if len(roe) < self._MIN_Q:
                continue
            std = np.std(roe, ddof=1)
            rows.append({"ticker": ticker, "factor_value": std})

        if not rows:
            logger.warning(f"QmjRoeVol({date}): 所有 ticker 季报 < {self._MIN_Q}")
            return pd.DataFrame(columns=["ticker", "factor_value"])
        out = pd.DataFrame(rows)
        logger.info(f"QmjRoeVol({date}): {len(out)} 有值")
        return out
