"""Intangible-Adjusted Book-to-Price 因子 (Eisfeldt & Papanikolaou 2013, RFS)

定义：
    Intangible-Adjusted B/P = (Book Value + Intangible Capital) / Market Cap

    Intangible Capital ≈ R&D Capital + 0.3 × SG&A Capital
    R&D Capital = R&D_t + 0.8·R&D_{t-1} + 0.6·R&D_{t-2} + 0.4·R&D_{t-3} + 0.2·R&D_{t-4}
    SG&A Capital = SG&A_t (当期即可，衰减更快)

    简化版（单期近似）：
    Intangible Capital ≈ R&D_t × 3 + SG&A_t × 0.3
    （R&D 假设 5 年折旧 → 累计 ≈ 3x 单期；SG&A 只取 30% 作为无形资产部分）

经济直觉：
    - 传统 B/P 低估了科技公司——R&D 被费用化但实际是资产
    - 加回无形资本后，科技公司不再被错误归类为"成长股"
    - 正向因子：调整后 B/P 越高 → 越便宜 → 预期收益越高

因子方向：+1
"""

import logging

import numpy as np
import pandas as pd

from services.config import LOG_LEVEL
from stocks.services.factors.us_registry import AlphaSignal, register

logger = logging.getLogger(__name__)
logger.setLevel(LOG_LEVEL)

# R&D 永续盘存权重（Eisfeldt & Papanikolaou 2013, 折旧率 δ=0.2）
_RD_WEIGHTS = [1.0, 0.8, 0.6, 0.4, 0.2]
# SG&A 取 30% 视为无形资本投入
_SGA_FRAC = 0.3


@register
class IntangibleAdjBP(AlphaSignal):
    """Intangible-Adjusted B/P — 加回 R&D + SG&A 无形资本的账面价值比。"""

    name = "INTANGIBLE_ADJ_BP"
    version = "v1"
    category = "value"
    horizon = "quarter"
    expected_icir = 0.15
    status = "staging"
    inherent_direction = +1
    data_deps = ["us_financial_data", "us_enterprise_value"]
    ic_window_months = 30

    _FIN_COLS = [
        "total_stockholders_equity",
        "research_and_development_expenses",
        "selling_general_and_administrative_expenses",
    ]

    def compute(self, date: str, universe: pd.DataFrame) -> pd.DataFrame:
        tickers = universe["ticker"].tolist()
        if not tickers:
            logger.debug("IntangibleAdjBP: 空 universe")
            return pd.DataFrame(columns=["ticker", "factor_value"])

        # 取 6 季报用于 R&D 永续盘存
        hist = self.fetch_financial_history(date, tickers, self._FIN_COLS, n_quarters=6)
        if hist.empty:
            logger.warning(f"IntangibleAdjBP({date}): fetch_financial_history 为空")
            return pd.DataFrame(columns=["ticker", "factor_value"])

        rows = []
        for ticker, grp in hist.groupby("ticker", sort=False):
            if len(grp) < 1:
                continue
            # 最新季报的 book value
            latest = grp.iloc[0]
            bv = latest.get("total_stockholders_equity", np.nan)
            if pd.isna(bv):
                continue

            # R&D capital: 永续盘存法（有多少季用多少季，最多 5 期）
            rd_vals = grp["research_and_development_expenses"].fillna(0).values
            n_rd = min(len(rd_vals), len(_RD_WEIGHTS))
            rd_capital = sum(rd_vals[i] * _RD_WEIGHTS[i] for i in range(n_rd))

            # SG&A capital: 当期 × 0.3
            sga = latest.get("selling_general_and_administrative_expenses", 0) or 0
            sga_capital = sga * _SGA_FRAC

            intangible_capital = rd_capital + sga_capital
            adj_bv = bv + intangible_capital
            rows.append({"ticker": ticker, "adj_bv": adj_bv})

        if not rows:
            logger.warning(f"IntangibleAdjBP({date}): 无有效数据")
            return pd.DataFrame(columns=["ticker", "factor_value"])

        bv_df = pd.DataFrame(rows)

        # 取市值
        mktcap = self._get_market_cap_on(date, bv_df["ticker"].tolist())
        if mktcap.empty:
            logger.warning(f"IntangibleAdjBP({date}): 无市值数据")
            return pd.DataFrame(columns=["ticker", "factor_value"])

        merged = bv_df.merge(mktcap, on="ticker", how="inner")
        mc = merged["market_cap"].replace(0, np.nan)
        merged["factor_value"] = merged["adj_bv"] / mc

        out = merged[["ticker", "factor_value"]].copy()
        n_out = int(out["factor_value"].notna().sum())
        logger.info(f"IntangibleAdjBP({date}): {n_out} / {len(out)} 有值")
        return out

    @staticmethod
    def _get_market_cap_on(date: str, tickers: list[str]) -> pd.DataFrame:
        """从 us_enterprise_value 取 `date` 可见的最新市值。"""
        from stocks.models import USEnterpriseValue

        date_ts = pd.Timestamp(date)
        start = (date_ts - pd.DateOffset(days=200)).date()

        qs = USEnterpriseValue.objects.filter(
            ticker__in=tickers,
            date__gte=start,
            date__lte=date_ts.date(),
            market_capitalization__isnull=False,
            market_capitalization__gt=0,
        ).values_list("ticker", "date", "market_capitalization")

        df = pd.DataFrame(list(qs), columns=["ticker", "date", "market_cap"])
        if df.empty:
            return pd.DataFrame(columns=["ticker", "market_cap"])
        df["date"] = pd.to_datetime(df["date"])
        df = df.sort_values(["ticker", "date"], ascending=[True, False])
        df = df.drop_duplicates(subset=["ticker"], keep="first")
        df["market_cap"] = pd.to_numeric(df["market_cap"], errors="coerce")
        return df[["ticker", "market_cap"]].reset_index(drop=True)
