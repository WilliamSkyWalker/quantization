"""Composite Equity Issuance 因子 (Daniel & Titman 2006, JF)

定义：
    CEI = log(ME_t / ME_{t-5y}) - Σ log(1 + r_i)   (i = t-5y to t)

    其中：
    - ME_t = 当前市值
    - ME_{t-5y} = 5 年前市值
    - Σ log(1+r_i) = 5 年累计对数收益率（拆股复权后）

经济直觉：
    - CEI 衡量"市值变化中不能被股价收益解释的部分" = 净发行（增发/拆股/期权行权）
    - 高 CEI = 大量增发 → 管理层认为自家股票被高估，反向做空
    - 低 CEI = 大量回购 → 管理层认为自家股票被低估，正向做多

简化实现：
    因 5 年市值数据可能不全，退化为 1 年版本：
    CEI = log(ME_t / ME_{t-1y}) - log(P_t / P_{t-1y})
        = log(shares_t / shares_{t-1y})

因子方向：-1（高 CEI = 净增发 = 利空）
"""

import logging

import numpy as np
import pandas as pd

from services.config import LOG_LEVEL
from stocks.services.factors.us_registry import AlphaSignal, register

logger = logging.getLogger(__name__)
logger.setLevel(LOG_LEVEL)


@register
class CompositeEquityIssuance(AlphaSignal):
    """Composite Equity Issuance — 净股权发行（反向，高值=大量增发）。"""

    name = "COMPOSITE_EQUITY_ISSUANCE"
    version = "v1"
    category = "value"
    horizon = "quarter"
    expected_icir = 0.12
    status = "staging"
    inherent_direction = -1
    data_deps = ["us_financial_data"]
    ic_window_months = 30

    _FIN_COLS = ["weighted_average_shs_out_dil"]

    def compute(self, date: str, universe: pd.DataFrame) -> pd.DataFrame:
        tickers = universe["ticker"].tolist()
        if not tickers:
            logger.debug("CompositeEquityIssuance: 空 universe")
            return pd.DataFrame(columns=["ticker", "factor_value"])

        # 取 6 季报配对 now vs yoy
        hist = self.fetch_financial_history(date, tickers, self._FIN_COLS, n_quarters=6)
        if hist.empty:
            logger.warning(f"CompositeEquityIssuance({date}): fetch_financial_history 为空")
            return pd.DataFrame(columns=["ticker", "factor_value"])

        paired = self.pick_year_ago(hist, self._FIN_COLS)
        if paired.empty:
            logger.warning(f"CompositeEquityIssuance({date}): pick_year_ago 为空")
            return pd.DataFrame(columns=["ticker", "factor_value"])

        df = paired.copy()

        shares_now = df["weighted_average_shs_out_dil_now"].replace(0, np.nan)
        shares_yoy = df["weighted_average_shs_out_dil_yoy"].replace(0, np.nan)

        # CEI ≈ log(shares_now / shares_yoy)
        # 正值 = 股数增加 = 净增发; 负值 = 股数减少 = 净回购
        cei = np.log(shares_now / shares_yoy)

        out = pd.DataFrame({"ticker": df["ticker"], "factor_value": cei})
        n_out = int(out["factor_value"].notna().sum())
        logger.info(f"CompositeEquityIssuance({date}): {n_out} / {len(out)} 有值")
        return out[["ticker", "factor_value"]]
