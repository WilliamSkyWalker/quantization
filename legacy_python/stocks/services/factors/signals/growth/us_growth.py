"""Growth 因子集合

5 个因子，全部基于 USFinancialData YoY 同比：

1. REVENUE_GROWTH    — 营收同比增速 (+1)
2. EARNINGS_GROWTH   — 净利润同比增速 (+1)
3. RD_INTENSITY      — R&D / Revenue，研发投入强度 (+1，长期成长代理)
4. CAPEX_GROWTH      — 资本开支同比增速 (0，方向不定)
5. GROSS_MARGIN_CHG  — 毛利率同比变化 (+1)
"""

import logging

import numpy as np
import pandas as pd

from services.config import LOG_LEVEL
from stocks.services.factors.us_registry import AlphaSignal, register

logger = logging.getLogger(__name__)
logger.setLevel(LOG_LEVEL)


# ---------------------------------------------------------------------------
# 1. Revenue Growth
# ---------------------------------------------------------------------------


@register
class RevenueGrowth(AlphaSignal):
    """Revenue Growth — 营收同比增速。"""

    name = "REVENUE_GROWTH"
    version = "v1"
    category = "growth"
    horizon = "quarter"
    expected_icir = 0.10
    status = "staging"
    inherent_direction = +1
    data_deps = ["us_financial_data"]
    ic_window_months = 24

    _FIN_COLS = ["revenue"]

    def compute(self, date: str, universe: pd.DataFrame) -> pd.DataFrame:
        tickers = universe["ticker"].tolist()
        if not tickers:
            logger.debug("RevenueGrowth: 空 universe")
            return pd.DataFrame(columns=["ticker", "factor_value"])

        hist = self.fetch_financial_history(date, tickers, self._FIN_COLS, n_quarters=6)
        if hist.empty:
            logger.warning(f"RevenueGrowth({date}): 无数据")
            return pd.DataFrame(columns=["ticker", "factor_value"])

        paired = self.pick_year_ago(hist, self._FIN_COLS)
        if paired.empty:
            logger.warning(f"RevenueGrowth({date}): pick_year_ago 为空")
            return pd.DataFrame(columns=["ticker", "factor_value"])

        denom = paired["revenue_yoy"].replace(0, np.nan).abs()
        growth = (paired["revenue_now"] - paired["revenue_yoy"]) / denom

        out = pd.DataFrame({"ticker": paired["ticker"], "factor_value": growth})
        n_out = int(out["factor_value"].notna().sum())
        logger.info(f"RevenueGrowth({date}): {n_out} / {len(out)} 有值")
        return out[["ticker", "factor_value"]]


# ---------------------------------------------------------------------------
# 2. Earnings Growth
# ---------------------------------------------------------------------------


@register
class EarningsGrowth(AlphaSignal):
    """Earnings Growth — 净利润同比增速。"""

    name = "EARNINGS_GROWTH"
    version = "v1"
    category = "growth"
    horizon = "quarter"
    expected_icir = 0.10
    status = "staging"
    inherent_direction = +1
    data_deps = ["us_financial_data"]
    ic_window_months = 24

    _FIN_COLS = ["net_income"]

    def compute(self, date: str, universe: pd.DataFrame) -> pd.DataFrame:
        tickers = universe["ticker"].tolist()
        if not tickers:
            logger.debug("EarningsGrowth: 空 universe")
            return pd.DataFrame(columns=["ticker", "factor_value"])

        hist = self.fetch_financial_history(date, tickers, self._FIN_COLS, n_quarters=6)
        if hist.empty:
            logger.warning(f"EarningsGrowth({date}): 无数据")
            return pd.DataFrame(columns=["ticker", "factor_value"])

        paired = self.pick_year_ago(hist, self._FIN_COLS)
        if paired.empty:
            logger.warning(f"EarningsGrowth({date}): pick_year_ago 为空")
            return pd.DataFrame(columns=["ticker", "factor_value"])

        denom = paired["net_income_yoy"].replace(0, np.nan).abs()
        growth = (paired["net_income_now"] - paired["net_income_yoy"]) / denom

        out = pd.DataFrame({"ticker": paired["ticker"], "factor_value": growth})
        n_out = int(out["factor_value"].notna().sum())
        logger.info(f"EarningsGrowth({date}): {n_out} / {len(out)} 有值")
        return out[["ticker", "factor_value"]]


# ---------------------------------------------------------------------------
# 3. R&D Intensity
# ---------------------------------------------------------------------------


@register
class RdIntensity(AlphaSignal):
    """R&D Intensity — 研发支出 / 营收（长期成长代理）。

    经济直觉：高研发投入 → 长期竞争力。Chan-Lakonishok-Sougiannis (2001):
    R&D 密集股票长期跑赢市场，因为 R&D 被费用化导致盈利低估。
    """

    name = "RD_INTENSITY"
    version = "v1"
    category = "growth"
    horizon = "quarter"
    expected_icir = 0.08
    status = "staging"
    inherent_direction = +1
    data_deps = ["us_financial_data"]
    ic_window_months = 30

    _FIN_COLS = ["research_and_development_expenses", "revenue"]

    def compute(self, date: str, universe: pd.DataFrame) -> pd.DataFrame:
        tickers = universe["ticker"].tolist()
        if not tickers:
            logger.debug("RdIntensity: 空 universe")
            return pd.DataFrame(columns=["ticker", "factor_value"])

        fin = self.fetch_financial_latest(date, tickers, self._FIN_COLS)
        if fin.empty:
            logger.warning(f"RdIntensity({date}): 无数据")
            return pd.DataFrame(columns=["ticker", "factor_value"])

        rev = fin["revenue"].replace(0, np.nan)
        rd = fin["research_and_development_expenses"].fillna(0)
        intensity = rd / rev

        out = pd.DataFrame({"ticker": fin["ticker"], "factor_value": intensity})
        n_out = int(out["factor_value"].notna().sum())
        logger.info(f"RdIntensity({date}): {n_out} / {len(out)} 有值")
        return out[["ticker", "factor_value"]]


# ---------------------------------------------------------------------------
# 4. Capex Growth
# ---------------------------------------------------------------------------


@register
class CapexGrowth(AlphaSignal):
    """Capex Growth — 资本开支同比增速。

    方向不定（0）：高 capex growth 可能是扩张信号（利好），也可能是过度投资（利空）。
    由滚动 IC 决定。
    """

    name = "CAPEX_GROWTH"
    version = "v1"
    category = "growth"
    horizon = "quarter"
    expected_icir = 0.06
    status = "staging"
    inherent_direction = 0
    data_deps = ["us_financial_data"]
    ic_window_months = 24

    _FIN_COLS = ["capital_expenditure"]

    def compute(self, date: str, universe: pd.DataFrame) -> pd.DataFrame:
        tickers = universe["ticker"].tolist()
        if not tickers:
            logger.debug("CapexGrowth: 空 universe")
            return pd.DataFrame(columns=["ticker", "factor_value"])

        hist = self.fetch_financial_history(date, tickers, self._FIN_COLS, n_quarters=6)
        if hist.empty:
            logger.warning(f"CapexGrowth({date}): 无数据")
            return pd.DataFrame(columns=["ticker", "factor_value"])

        paired = self.pick_year_ago(hist, self._FIN_COLS)
        if paired.empty:
            logger.warning(f"CapexGrowth({date}): pick_year_ago 为空")
            return pd.DataFrame(columns=["ticker", "factor_value"])

        # capex 在 FMP 里通常是负数，取绝对值再算增速
        now = paired["capital_expenditure_now"].abs()
        yoy = paired["capital_expenditure_yoy"].abs().replace(0, np.nan)
        growth = (now - yoy.abs()) / yoy

        out = pd.DataFrame({"ticker": paired["ticker"], "factor_value": growth})
        n_out = int(out["factor_value"].notna().sum())
        logger.info(f"CapexGrowth({date}): {n_out} / {len(out)} 有值")
        return out[["ticker", "factor_value"]]


# ---------------------------------------------------------------------------
# 5. Gross Margin Change
# ---------------------------------------------------------------------------


@register
class GrossMarginChange(AlphaSignal):
    """Gross Margin Change — 毛利率同比变化（百分点）。

    经济直觉：毛利率扩张 → 定价权/成本控制改善 → 盈利质量提升。
    Novy-Marx (2013) 发现 gross profitability 有独立 alpha。
    """

    name = "GROSS_MARGIN_CHG"
    version = "v1"
    category = "growth"
    horizon = "quarter"
    expected_icir = 0.10
    status = "staging"
    inherent_direction = +1
    data_deps = ["us_financial_data"]
    ic_window_months = 24

    _FIN_COLS = ["gross_profit", "revenue"]

    def compute(self, date: str, universe: pd.DataFrame) -> pd.DataFrame:
        tickers = universe["ticker"].tolist()
        if not tickers:
            logger.debug("GrossMarginChange: 空 universe")
            return pd.DataFrame(columns=["ticker", "factor_value"])

        hist = self.fetch_financial_history(date, tickers, self._FIN_COLS, n_quarters=6)
        if hist.empty:
            logger.warning(f"GrossMarginChange({date}): 无数据")
            return pd.DataFrame(columns=["ticker", "factor_value"])

        paired = self.pick_year_ago(hist, self._FIN_COLS)
        if paired.empty:
            logger.warning(f"GrossMarginChange({date}): pick_year_ago 为空")
            return pd.DataFrame(columns=["ticker", "factor_value"])

        rev_now = paired["revenue_now"].replace(0, np.nan)
        rev_yoy = paired["revenue_yoy"].replace(0, np.nan)
        gm_now = paired["gross_profit_now"] / rev_now
        gm_yoy = paired["gross_profit_yoy"] / rev_yoy

        delta = gm_now - gm_yoy  # 百分点变化

        out = pd.DataFrame({"ticker": paired["ticker"], "factor_value": delta})
        n_out = int(out["factor_value"].notna().sum())
        logger.info(f"GrossMarginChange({date}): {n_out} / {len(out)} 有值")
        return out[["ticker", "factor_value"]]
