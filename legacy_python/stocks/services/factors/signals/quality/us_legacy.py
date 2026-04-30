"""Quality 类 5 个旧因子按 AlphaSignal 架构迁移（行为不变，只补元数据）。

原文件：
    stocks/services/factors/quality.py      — RoeTTM / GrossMargin / ProfitStability / MarginTrend
    stocks/services/factors/accruals.py     — Accruals（保留 BuybackYield 在 value 批处理）

本次仅重新包装为 AlphaSignal 子类，compute() 逻辑字节对齐原实现。
元数据的 status / inherent_direction / ic_window_months 对齐 strategy.py 原行为：
    - 全部 _NEVER_REVERSE_SET（quality 永不反转）→ inherent_direction = +1
    - _ROLLING_IC_WINDOW 里没列 → 默认 18 月
"""

import logging

import numpy as np
import pandas as pd

from services.config import LOG_LEVEL
from stocks.services.factors.us_registry import AlphaSignal, register

logger = logging.getLogger(__name__)
logger.setLevel(LOG_LEVEL)


# ============================================================================
# RoeTTM — ROE (Return on Equity) 来自 preload 的 roe 列（已在 base.py 派生）
# ============================================================================

@register
class RoeTTM(AlphaSignal):
    """Return on Equity (latest filing)，从 preload 的 us_financial_data 派生列读。"""

    name = "ROE_TTM"
    version = "v1"
    category = "quality"
    horizon = "quarter"
    expected_icir = 0.08
    status = "live"
    inherent_direction = +1
    data_deps = ["us_financial_data"]
    ic_window_months = 18

    def compute(self, date: str, universe: pd.DataFrame) -> pd.DataFrame:
        tickers = universe["ticker"].tolist()
        fin = self.get_latest_financial(date, ["roe"], tickers)
        if fin.empty:
            logger.debug("RoeTTM.compute: 无财务数据")
            return pd.DataFrame(columns=["ticker", "factor_value"])
        fin["factor_value"] = pd.to_numeric(fin["roe"], errors="coerce")
        return fin[["ticker", "factor_value"]]


# ============================================================================
# GrossMargin — 毛利率，同样来自 preload 派生列
# ============================================================================

@register
class GrossMargin(AlphaSignal):
    """Gross Margin (latest filing)，gross_profit / revenue。"""

    name = "GROSS_MARGIN"
    version = "v1"
    category = "quality"
    horizon = "quarter"
    expected_icir = 0.05
    status = "live"
    inherent_direction = +1
    data_deps = ["us_financial_data"]
    ic_window_months = 18

    def compute(self, date: str, universe: pd.DataFrame) -> pd.DataFrame:
        tickers = universe["ticker"].tolist()
        fin = self.get_latest_financial(date, ["gross_margin"], tickers)
        if fin.empty:
            logger.debug("GrossMargin.compute: 无财务数据")
            return pd.DataFrame(columns=["ticker", "factor_value"])
        fin["factor_value"] = pd.to_numeric(fin["gross_margin"], errors="coerce")
        return fin[["ticker", "factor_value"]]


# ============================================================================
# ProfitStability — 近 8Q 净利润 YoY 增速的 CV（取反），越稳定越好
# ============================================================================

@register
class ProfitStability(AlphaSignal):
    """Profit Stability：近 8Q net_income YoY 增速的变异系数（取反）。"""

    name = "PROFIT_STB"
    version = "v1"
    category = "quality"
    horizon = "quarter"
    expected_icir = 0.05
    status = "live"
    inherent_direction = +1
    data_deps = ["us_financial_data"]
    ic_window_months = 18

    def compute(self, date: str, universe: pd.DataFrame) -> pd.DataFrame:
        tickers = universe["ticker"].tolist()
        date_ts = pd.to_datetime(date)

        bulk_fin = self._static_cache.get("_bulk_financial")
        if bulk_fin is None or bulk_fin.empty:
            logger.debug("ProfitStability.compute: 无预加载财务数据")
            return pd.DataFrame(columns=["ticker", "factor_value"])

        df = bulk_fin[bulk_fin["filing_date"] <= date_ts].copy()
        if tickers:
            df = df[df["ticker"].isin(tickers)]
        df = df.sort_values(["ticker", "date"], ascending=[True, False])
        df["net_income"] = pd.to_numeric(df["net_income"], errors="coerce")

        df = df.groupby("ticker").head(8)

        results = []
        for ticker, grp in df.groupby("ticker"):
            if len(grp) < 5:
                results.append({"ticker": ticker, "factor_value": np.nan})
                logger.debug(f"ProfitStability.compute: {ticker} 季度不足 5 条")
                continue
            vals = grp["net_income"].values
            yoy_growths = []
            for i in range(len(vals) - 4):
                base = vals[i + 4]
                if base != 0 and not np.isnan(base):
                    yoy_growths.append((vals[i] - base) / abs(base))
            if len(yoy_growths) < 2:
                results.append({"ticker": ticker, "factor_value": np.nan})
                logger.debug(f"ProfitStability.compute: {ticker} YoY 对数 < 2")
                continue
            arr = np.array(yoy_growths)
            mean = np.mean(arr)
            std = np.std(arr)
            cv = std / abs(mean) if abs(mean) > 1e-10 else np.nan
            results.append({"ticker": ticker, "factor_value": -cv if not np.isnan(cv) else np.nan})

        return pd.DataFrame(results)


# ============================================================================
# MarginTrend — 毛利率 QoQ 差（最新 − 上一季）
# ============================================================================

@register
class MarginTrend(AlphaSignal):
    """Margin Trend：近两季 gross_margin 环比变化。"""

    name = "MARGIN_TREND"
    version = "v1"
    category = "quality"
    horizon = "quarter"
    expected_icir = 0.04
    status = "live"
    inherent_direction = +1
    data_deps = ["us_financial_data"]
    ic_window_months = 18

    def compute(self, date: str, universe: pd.DataFrame) -> pd.DataFrame:
        tickers = universe["ticker"].tolist()
        date_ts = pd.to_datetime(date)

        bulk_fin = self._static_cache.get("_bulk_financial")
        if bulk_fin is None or bulk_fin.empty:
            logger.debug("MarginTrend.compute: 无预加载财务数据")
            return pd.DataFrame(columns=["ticker", "factor_value"])

        df = bulk_fin[bulk_fin["filing_date"] <= date_ts].copy()
        if tickers:
            df = df[df["ticker"].isin(tickers)]
        df = df.sort_values(["ticker", "date"], ascending=[True, False])
        df["gross_margin"] = pd.to_numeric(df["gross_margin"], errors="coerce")

        df = df.groupby("ticker").head(2)

        results = []
        for ticker, grp in df.groupby("ticker"):
            if len(grp) < 2:
                results.append({"ticker": ticker, "factor_value": np.nan})
                logger.debug(f"MarginTrend.compute: {ticker} 季度不足 2 条")
                continue
            latest = grp.iloc[0]["gross_margin"]
            prev = grp.iloc[1]["gross_margin"]
            if pd.isna(latest) or pd.isna(prev):
                results.append({"ticker": ticker, "factor_value": np.nan})
                logger.debug(f"MarginTrend.compute: {ticker} 毛利率缺失")
                continue
            results.append({"ticker": ticker, "factor_value": latest - prev})

        return pd.DataFrame(results)


# ============================================================================
# Accruals — (Net Income - OpCF) / Total Assets（取反，高应计 = 差）
# ============================================================================

@register
class Accruals(AlphaSignal):
    """Accruals Anomaly (Sloan 1996)：(Net Income − OpCF) / Total Assets（取反）。"""

    name = "ACCRUALS"
    version = "v1"
    category = "quality"
    horizon = "quarter"
    expected_icir = 0.10
    status = "live"
    inherent_direction = +1  # 已在 compute 里取反，类层面是 "正向"
    data_deps = ["us_financial_data"]
    ic_window_months = 18

    def compute(self, date: str, universe: pd.DataFrame) -> pd.DataFrame:
        tickers = universe["ticker"].tolist()
        date_ts = pd.to_datetime(date)

        bulk_fin = self._static_cache.get("_bulk_financial")
        if bulk_fin is None or bulk_fin.empty:
            logger.debug("Accruals.compute: 无预加载财务数据")
            return pd.DataFrame(columns=["ticker", "factor_value"])

        df = bulk_fin[bulk_fin["filing_date"] <= date_ts].copy()
        if tickers:
            df = df[df["ticker"].isin(tickers)]
        df = df.sort_values(["ticker", "date"], ascending=[True, False])
        df = df.groupby("ticker").head(4)

        for col in ["net_income", "free_cash_flow", "total_assets"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")

        results = []
        for ticker, grp in df.groupby("ticker"):
            ni_ttm = grp["net_income"].sum()
            fcf_ttm = grp["free_cash_flow"].sum()
            total_assets = grp["total_assets"].iloc[0]

            if pd.isna(ni_ttm) or pd.isna(fcf_ttm) or pd.isna(total_assets) or abs(total_assets) < 1e-6:
                results.append({"ticker": ticker, "factor_value": np.nan})
                logger.debug(f"Accruals.compute: {ticker} 财务或资产缺失")
                continue

            accrual = (ni_ttm - fcf_ttm) / abs(total_assets)
            results.append({"ticker": ticker, "factor_value": -accrual})

        return pd.DataFrame(results)
