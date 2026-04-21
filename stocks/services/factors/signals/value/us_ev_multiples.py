"""EV 系列估值因子 — EV/EBIT, EV/FCF, EV/Sales

三个经典的企业价值倍数。相比 P/E：
- 用 Enterprise Value 代替 Market Cap → 资本结构中性
- 用 EBIT/FCF/Sales 代替 Earnings → 更难被会计操纵

数据源：USKeyMetric 已有预计算的 ev_to_ebitda / ev_to_free_cash_flow / ev_to_sales。
但 EV/EBIT 没有直接字段，需从 USFinancialData(EBIT) + USEnterpriseValue(EV) 计算。

简化方案：直接用 USKeyMetric 的 3 个字段（ev_to_sales, ev_to_free_cash_flow）
+ USKeyMetric 没有 ev_to_ebit，改用 enterprise_value_multiple (= EV/EBITDA) 或自算。

最终方案：
- EV_TO_EBIT: 从 USFinancialData(ebit) + USEnterpriseValue(enterprise_value) 自算
- EV_TO_FCF: USKeyMetric.ev_to_free_cash_flow
- EV_TO_SALES: USKeyMetric.ev_to_sales

因子方向：-1（高倍数 = 贵 = 利空）
"""

import logging

import numpy as np
import pandas as pd

from services.config import LOG_LEVEL
from stocks.services.factors.us_registry import AlphaSignal, register

logger = logging.getLogger(__name__)
logger.setLevel(LOG_LEVEL)


# ---------------------------------------------------------------------------
# EV/EBIT — 自算
# ---------------------------------------------------------------------------


@register
class EvToEbit(AlphaSignal):
    """EV/EBIT — 企业价值 / 息税前利润。"""

    name = "EV_TO_EBIT"
    version = "v1"
    category = "value"
    horizon = "quarter"
    expected_icir = 0.15
    status = "staging"
    inherent_direction = -1
    data_deps = ["us_financial_data", "us_enterprise_value"]
    ic_window_months = 30

    _FIN_COLS = ["ebit"]

    def compute(self, date: str, universe: pd.DataFrame) -> pd.DataFrame:
        tickers = universe["ticker"].tolist()
        if not tickers:
            logger.debug("EvToEbit: 空 universe")
            return pd.DataFrame(columns=["ticker", "factor_value"])

        fin = self.fetch_financial_latest(date, tickers, self._FIN_COLS)
        if fin.empty:
            logger.warning(f"EvToEbit({date}): 财务数据为空")
            return pd.DataFrame(columns=["ticker", "factor_value"])

        ev_df = self.fetch_enterprise_value(date, fin["ticker"].tolist())
        if ev_df.empty:
            logger.warning(f"EvToEbit({date}): 无 EV 数据")
            return pd.DataFrame(columns=["ticker", "factor_value"])

        merged = fin[["ticker", "ebit"]].merge(ev_df, on="ticker", how="inner")
        ebit = merged["ebit"].replace(0, np.nan)
        merged["factor_value"] = merged["ev"] / ebit

        out = merged[["ticker", "factor_value"]].copy()
        n_out = int(out["factor_value"].notna().sum())
        logger.info(f"EvToEbit({date}): {n_out} / {len(out)} 有值")
        return out


# ---------------------------------------------------------------------------
# EV/FCF — 直接从 USKeyMetric 读
# ---------------------------------------------------------------------------


@register
class EvToFcf(AlphaSignal):
    """EV/FCF — 企业价值 / 自由现金流。"""

    name = "EV_TO_FCF"
    version = "v1"
    category = "value"
    horizon = "quarter"
    expected_icir = 0.15
    status = "staging"
    inherent_direction = -1
    data_deps = ["us_key_metric"]
    ic_window_months = 30

    def compute(self, date: str, universe: pd.DataFrame) -> pd.DataFrame:
        tickers = universe["ticker"].tolist()
        if not tickers:
            logger.debug("EvToFcf: 空 universe")
            return pd.DataFrame(columns=["ticker", "factor_value"])

        km = self.fetch_key_metric_latest(date, tickers, ["ev_to_free_cash_flow"])
        if km.empty:
            logger.warning(f"EvToFcf({date}): key_metric 数据为空")
            return pd.DataFrame(columns=["ticker", "factor_value"])

        out = pd.DataFrame({
            "ticker": km["ticker"],
            "factor_value": km["ev_to_free_cash_flow"],
        })
        n_out = int(out["factor_value"].notna().sum())
        logger.info(f"EvToFcf({date}): {n_out} / {len(out)} 有值")
        return out[["ticker", "factor_value"]]


# ---------------------------------------------------------------------------
# EV/Sales — 直接从 USKeyMetric 读
# ---------------------------------------------------------------------------


@register
class EvToSales(AlphaSignal):
    """EV/Sales — 企业价值 / 营收。"""

    name = "EV_TO_SALES"
    version = "v1"
    category = "value"
    horizon = "quarter"
    expected_icir = 0.12
    status = "staging"
    inherent_direction = -1
    data_deps = ["us_key_metric"]
    ic_window_months = 30

    def compute(self, date: str, universe: pd.DataFrame) -> pd.DataFrame:
        tickers = universe["ticker"].tolist()
        if not tickers:
            logger.debug("EvToSales: 空 universe")
            return pd.DataFrame(columns=["ticker", "factor_value"])

        km = self.fetch_key_metric_latest(date, tickers, ["ev_to_sales"])
        if km.empty:
            logger.warning(f"EvToSales({date}): key_metric 数据为空")
            return pd.DataFrame(columns=["ticker", "factor_value"])

        out = pd.DataFrame({
            "ticker": km["ticker"],
            "factor_value": km["ev_to_sales"],
        })
        n_out = int(out["factor_value"].notna().sum())
        logger.info(f"EvToSales({date}): {n_out} / {len(out)} 有值")
        return out[["ticker", "factor_value"]]
