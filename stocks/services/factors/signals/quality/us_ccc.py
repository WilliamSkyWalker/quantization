"""Cash Conversion Cycle (CCC)

    CCC = DSO + DIO − DPO

    DSO = Days of Sales Outstanding       应收账款周转天数
    DIO = Days of Inventory Outstanding   存货周转天数
    DPO = Days of Payables Outstanding    应付账款周转天数

CCC 越小越好（占用流动资金越少）。因子方向：−1（反向）。

实现：直接用 USKeyMetric.cash_conversion_cycle（FMP 已预计算）。
"""

import logging

import pandas as pd

from services.config import LOG_LEVEL
from stocks.services.factors.us_registry import AlphaSignal, register

logger = logging.getLogger(__name__)
logger.setLevel(LOG_LEVEL)


@register
class CashConversionCycle(AlphaSignal):
    """Cash Conversion Cycle（现金转换周期，越短越好，反向因子）。"""

    name = "CASH_CONV_CYCLE"
    version = "v1"
    category = "quality"
    horizon = "quarter"
    expected_icir = 0.05
    status = "staging"
    inherent_direction = -1
    data_deps = ["us_key_metric"]
    ic_window_months = 24

    def compute(self, date: str, universe: pd.DataFrame) -> pd.DataFrame:
        tickers = universe["ticker"].tolist()
        if not tickers:
            logger.debug("CashConversionCycle: 空 universe")
            return pd.DataFrame(columns=["ticker", "factor_value"])

        km = self.fetch_key_metric_latest(date, tickers, ["cash_conversion_cycle"])
        if km.empty:
            logger.warning(f"CashConversionCycle({date}): 无 key_metric 数据")
            return pd.DataFrame(columns=["ticker", "factor_value"])

        out = pd.DataFrame(
            {"ticker": km["ticker"], "factor_value": km["cash_conversion_cycle"]}
        )
        n_out = int(out["factor_value"].notna().sum())
        logger.info(f"CashConversionCycle({date}): {n_out} / {len(out)} 有值")
        return out
