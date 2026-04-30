"""Piotroski F-Score (Piotroski 2000, JAR)

9 个 binary signals，每项满足 +1，合计 0-9：

    Profitability (4):
        1. ROA > 0          (net_income / total_assets)
        2. CFO > 0          (operating_cash_flow)
        3. ΔROA > 0         (ROA_t vs ROA_{t-4Q})
        4. CFO > NI         (应计质量；OCF 高于净利润 → 盈利现金化好)

    Leverage / Liquidity / Source of Funds (3):
        5. ΔLTD/Assets ≤ 0  (长期债务占比下降)
        6. ΔCurrent Ratio > 0
        7. No equity issuance (net_stock_issuance ≤ 0)

    Operating Efficiency (2):
        8. ΔGross Margin > 0
        9. ΔAsset Turnover > 0

F-Score ∈ [0, 9]，越高越好。经典阈值：≥7 做多、≤3 做空。
"""

import logging

import numpy as np
import pandas as pd

from services.config import LOG_LEVEL
from stocks.services.factors.us_registry import AlphaSignal, register

logger = logging.getLogger(__name__)
logger.setLevel(LOG_LEVEL)


@register
class PiotroskiF(AlphaSignal):
    """Piotroski F-Score (0-9) — 9 分财务体检。"""

    name = "PIOTROSKI_F"
    version = "v1"
    category = "quality"
    horizon = "quarter"
    expected_icir = 0.15
    status = "staging"
    inherent_direction = +1
    data_deps = ["us_financial_data"]
    ic_window_months = 24

    _FIN_COLS = [
        "total_assets",
        "net_income",
        "operating_cash_flow",
        "long_term_debt",
        "total_current_assets",
        "total_current_liabilities",
        "net_stock_issuance",
        "gross_profit",
        "revenue",
    ]

    def compute(self, date: str, universe: pd.DataFrame) -> pd.DataFrame:
        tickers = universe["ticker"].tolist()
        if not tickers:
            logger.debug("PiotroskiF: 空 universe")
            return pd.DataFrame(columns=["ticker", "factor_value"])

        # 每只股票取最近 5 条季报（index 0 = 最新 Q, index 4 = 1 年前同 Q）
        hist = self.fetch_financial_history(date, tickers, self._FIN_COLS, n_quarters=6)
        if hist.empty:
            logger.warning(f"PiotroskiF({date}): fetch_financial_history 为空")
            return pd.DataFrame(columns=["ticker", "factor_value"])

        paired = self.pick_year_ago(hist, self._FIN_COLS)
        if paired.empty:
            logger.warning(f"PiotroskiF({date}): pick_year_ago 为空（无 ticker 有 ≥5 季报）")
            return pd.DataFrame(columns=["ticker", "factor_value"])

        df = paired.copy()

        # —— 派生指标（当期 + 同比） ——
        for suffix in ["now", "yoy"]:
            ta = df[f"total_assets_{suffix}"].replace(0, np.nan)
            df[f"roa_{suffix}"] = df[f"net_income_{suffix}"] / ta
            df[f"asset_turnover_{suffix}"] = df[f"revenue_{suffix}"] / ta
            rev = df[f"revenue_{suffix}"].replace(0, np.nan)
            df[f"gross_margin_{suffix}"] = df[f"gross_profit_{suffix}"] / rev
            df[f"ltd_ratio_{suffix}"] = df[f"long_term_debt_{suffix}"] / ta
            ccl = df[f"total_current_liabilities_{suffix}"].replace(0, np.nan)
            df[f"current_ratio_{suffix}"] = df[f"total_current_assets_{suffix}"] / ccl

        # —— 9 项 signal（NaN 当 0 处理但总分只在有足够非 NaN 时返回） ——
        sig = pd.DataFrame({"ticker": df["ticker"]})

        # 1. ROA > 0
        sig["s1"] = (df["roa_now"] > 0).astype(int)
        # 2. CFO > 0
        sig["s2"] = (df["operating_cash_flow_now"] > 0).astype(int)
        # 3. ΔROA > 0
        sig["s3"] = (df["roa_now"] > df["roa_yoy"]).astype(int)
        # 4. CFO > NI
        sig["s4"] = (df["operating_cash_flow_now"] > df["net_income_now"]).astype(int)
        # 5. ΔLTD Ratio ≤ 0（降杠杆利好，等于也算好）
        sig["s5"] = (df["ltd_ratio_now"] <= df["ltd_ratio_yoy"]).astype(int)
        # 6. ΔCurrent Ratio > 0
        sig["s6"] = (df["current_ratio_now"] > df["current_ratio_yoy"]).astype(int)
        # 7. No equity issuance（FMP: net_stock_issuance 正数=发行，负数=回购。≤0 视为没发行）
        sig["s7"] = (df["net_stock_issuance_now"].fillna(0) <= 0).astype(int)
        # 8. ΔGross Margin > 0
        sig["s8"] = (df["gross_margin_now"] > df["gross_margin_yoy"]).astype(int)
        # 9. ΔAsset Turnover > 0
        sig["s9"] = (df["asset_turnover_now"] > df["asset_turnover_yoy"]).astype(int)

        # —— 有效性校验：如果关键输入是 NaN，那一条 signal 不应得分 ——
        # NaN 比较永远是 False，已经变成 0，无需额外处理。但如果"两边都 NaN"导致的 False
        # 也会变 0，符合保守评分。只在有足够非 NaN 时才输出总分：
        # 要求至少 5 个 signal 有"可判断性"（两个期值都非 NaN 或单期非 NaN）
        validity = pd.DataFrame({"ticker": df["ticker"]})
        validity["v1"] = df["roa_now"].notna()
        validity["v2"] = df["operating_cash_flow_now"].notna()
        validity["v3"] = df["roa_now"].notna() & df["roa_yoy"].notna()
        validity["v4"] = df["operating_cash_flow_now"].notna() & df["net_income_now"].notna()
        validity["v5"] = df["ltd_ratio_now"].notna() & df["ltd_ratio_yoy"].notna()
        validity["v6"] = df["current_ratio_now"].notna() & df["current_ratio_yoy"].notna()
        validity["v7"] = df["net_stock_issuance_now"].notna()
        validity["v8"] = df["gross_margin_now"].notna() & df["gross_margin_yoy"].notna()
        validity["v9"] = df["asset_turnover_now"].notna() & df["asset_turnover_yoy"].notna()

        n_valid = validity[[f"v{i}" for i in range(1, 10)]].sum(axis=1)
        score = sig[[f"s{i}" for i in range(1, 10)]].sum(axis=1).astype(float)
        score.loc[n_valid < 5] = np.nan  # 有效 signal < 5 不出分

        out = pd.DataFrame({"ticker": sig["ticker"].values, "factor_value": score.values})
        n_out = int(out["factor_value"].notna().sum())
        logger.info(f"PiotroskiF({date}): {n_out} / {len(out)} 有值")
        return out
