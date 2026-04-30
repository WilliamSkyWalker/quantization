"""Net Operating Assets 因子 (Hirshleifer, Hou, Teoh, Zhang 2004, JAE)

定义：
    NOA = (Operating Assets - Operating Liabilities) / Total Assets
        Operating Assets  = Total Assets - Cash & Short-term Investments
        Operating Liabilities = Total Assets - Short-term Debt - Long-term Debt
                                - Minority Interest - Preferred Stock
                                - Total Stockholders' Equity

简化公式（等价）：
    NOA = (Total Assets - Cash - Total Debt - Total Stockholders' Equity
           - Minority Interest - Preferred Stock) / Total Assets
       ≈ 1 - (Cash + Debt + Equity) / Total Assets

经济直觉：
    - 高 NOA = 应计利润占比高，盈利质量低
    - 反向因子：NOA 越高 → 预期收益越低

因子方向：-1（高 NOA = 利空）
"""

import logging

import numpy as np
import pandas as pd

from services.config import LOG_LEVEL
from stocks.services.factors.us_registry import AlphaSignal, register

logger = logging.getLogger(__name__)
logger.setLevel(LOG_LEVEL)


@register
class NetOperatingAssets(AlphaSignal):
    """Net Operating Assets — 净经营资产占比（反向，高值=低质量）。"""

    name = "NET_OPERATING_ASSETS"
    version = "v1"
    category = "value"
    horizon = "quarter"
    expected_icir = 0.15
    status = "staging"
    inherent_direction = -1
    data_deps = ["us_financial_data"]
    ic_window_months = 30

    _FIN_COLS = [
        "total_assets",
        "cash_and_short_term_investments",
        "total_debt",
        "total_stockholders_equity",
        "minority_interest",
        "preferred_stock",
    ]

    def compute(self, date: str, universe: pd.DataFrame) -> pd.DataFrame:
        tickers = universe["ticker"].tolist()
        if not tickers:
            logger.debug("NetOperatingAssets: 空 universe")
            return pd.DataFrame(columns=["ticker", "factor_value"])

        fin = self.fetch_financial_latest(date, tickers, self._FIN_COLS)
        if fin.empty:
            logger.warning(f"NetOperatingAssets({date}): 财务数据为空")
            return pd.DataFrame(columns=["ticker", "factor_value"])

        df = fin.copy()
        ta = df["total_assets"].replace(0, np.nan)

        # Operating Assets = Total Assets - Cash
        oa = df["total_assets"] - df["cash_and_short_term_investments"].fillna(0)

        # Operating Liabilities = Total Assets - Debt - Equity - Minority - Preferred
        ol = (
            df["total_assets"]
            - df["total_debt"].fillna(0)
            - df["total_stockholders_equity"].fillna(0)
            - df["minority_interest"].fillna(0)
            - df["preferred_stock"].fillna(0)
        )

        noa = (oa - ol) / ta

        out = pd.DataFrame({"ticker": df["ticker"], "factor_value": noa})
        n_out = int(out["factor_value"].notna().sum())
        logger.info(f"NetOperatingAssets({date}): {n_out} / {len(out)} 有值")
        return out[["ticker", "factor_value"]]
