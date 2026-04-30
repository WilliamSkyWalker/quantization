"""Beneish M-Score (Beneish 1999, FAJ)

盈余操纵（财务造假）概率模型。公式：

    M = −4.84 + 0.92·DSRI + 0.528·GMI + 0.404·AQI + 0.892·SGI
        + 0.115·DEPI − 0.172·SGAI − 0.327·LVGI + 4.679·TATA

    DSRI = (AR_t/Sales_t) / (AR_{t-1}/Sales_{t-1})        应收账款天数变化
    GMI  = GrossMargin_{t-1} / GrossMargin_t              毛利率恶化（反向）
    AQI  = (1 − (CA+PPE)/TA)_t / (1 − (CA+PPE)/TA)_{t-1}  资产质量（无形资产占比变化）
    SGI  = Sales_t / Sales_{t-1}                          销售增长（过快可疑）
    DEPI = (Dep_{t-1}/(Dep+PPE)_{t-1}) / (Dep_t/(Dep+PPE)_t)  折旧率变化（降折旧=粉饰利润）
    SGAI = (SGA/Sales)_t / (SGA/Sales)_{t-1}              SG&A 占比变化
    LVGI = (TL/TA)_t / (TL/TA)_{t-1}                      杠杆变化
    TATA = (ΔWC − Dep) / TA ≈ (NI − CFO) / TA             应计占资产比（简化版）

判读：M > −1.78 视为"可能造假"候选。
因子方向：−1（反向；M 越高嫌疑越大 → 做空候选）。
"""

import logging

import numpy as np
import pandas as pd

from services.config import LOG_LEVEL
from stocks.services.factors.us_registry import AlphaSignal, register

logger = logging.getLogger(__name__)
logger.setLevel(LOG_LEVEL)


@register
class BeneishM(AlphaSignal):
    """Beneish M-Score — 财务造假概率（越高嫌疑越大，反向因子）。"""

    name = "BENEISH_M"
    version = "v1"
    category = "quality"
    horizon = "quarter"
    expected_icir = 0.10
    status = "staging"
    inherent_direction = -1
    data_deps = ["us_financial_data"]
    ic_window_months = 24

    _FIN_COLS = [
        "accounts_receivables",
        "revenue",
        "gross_profit",
        "total_current_assets",
        "property_plant_equipment_net",
        "total_assets",
        "depreciation_and_amortization",
        "selling_general_and_administrative_expenses",
        "total_liabilities",
        "net_income",
        "operating_cash_flow",
    ]

    def compute(self, date: str, universe: pd.DataFrame) -> pd.DataFrame:
        tickers = universe["ticker"].tolist()
        if not tickers:
            logger.debug("BeneishM: 空 universe")
            return pd.DataFrame(columns=["ticker", "factor_value"])

        hist = self.fetch_financial_history(date, tickers, self._FIN_COLS, n_quarters=6)
        if hist.empty:
            logger.warning(f"BeneishM({date}): 无财务数据")
            return pd.DataFrame(columns=["ticker", "factor_value"])

        paired = self.pick_year_ago(hist, self._FIN_COLS)
        if paired.empty:
            logger.warning(f"BeneishM({date}): 无 ticker 有 ≥5 季报")
            return pd.DataFrame(columns=["ticker", "factor_value"])

        df = paired.copy()

        def _safe_div(a, b):
            b_arr = b.replace(0, np.nan) if isinstance(b, pd.Series) else (np.nan if b == 0 else b)
            return a / b_arr

        # —— 派生比率（t 和 t-1 两期） ——
        for s in ["now", "yoy"]:
            rev = df[f"revenue_{s}"]
            ta = df[f"total_assets_{s}"]
            df[f"gm_{s}"] = _safe_div(df[f"gross_profit_{s}"], rev)
            df[f"ar_sales_{s}"] = _safe_div(df[f"accounts_receivables_{s}"], rev)
            ca_ppe = df[f"total_current_assets_{s}"] + df[f"property_plant_equipment_net_{s}"]
            df[f"aq_raw_{s}"] = 1.0 - _safe_div(ca_ppe, ta)
            dep = df[f"depreciation_and_amortization_{s}"]
            dep_ppe = dep + df[f"property_plant_equipment_net_{s}"]
            df[f"dep_rate_{s}"] = _safe_div(dep, dep_ppe)
            df[f"sga_sales_{s}"] = _safe_div(df[f"selling_general_and_administrative_expenses_{s}"], rev)
            df[f"lev_{s}"] = _safe_div(df[f"total_liabilities_{s}"], ta)

        # —— 8 ratios ——
        dsri = _safe_div(df["ar_sales_now"], df["ar_sales_yoy"])
        gmi = _safe_div(df["gm_yoy"], df["gm_now"])  # 注意是 yoy / now
        aqi = _safe_div(df["aq_raw_now"], df["aq_raw_yoy"])
        sgi = _safe_div(df["revenue_now"], df["revenue_yoy"])
        depi = _safe_div(df["dep_rate_yoy"], df["dep_rate_now"])  # yoy / now
        sgai = _safe_div(df["sga_sales_now"], df["sga_sales_yoy"])
        lvgi = _safe_div(df["lev_now"], df["lev_yoy"])

        # TATA 简化版：(NI − CFO) / TA
        tata = _safe_div(
            df["net_income_now"] - df["operating_cash_flow_now"], df["total_assets_now"]
        )

        # —— clip 极值（比率分母接近 0 时会爆炸，保守 clip 到 [-5, 5]） ——
        for s in [dsri, gmi, aqi, sgi, depi, sgai, lvgi]:
            s.clip(-5, 5, inplace=True) if hasattr(s, "clip") else None
        dsri = dsri.clip(-5, 5)
        gmi = gmi.clip(-5, 5)
        aqi = aqi.clip(-5, 5)
        sgi = sgi.clip(-5, 5)
        depi = depi.clip(-5, 5)
        sgai = sgai.clip(-5, 5)
        lvgi = lvgi.clip(-5, 5)
        tata = tata.clip(-1, 1)

        m = (
            -4.84
            + 0.92 * dsri
            + 0.528 * gmi
            + 0.404 * aqi
            + 0.892 * sgi
            + 0.115 * depi
            - 0.172 * sgai
            - 0.327 * lvgi
            + 4.679 * tata
        )

        out = pd.DataFrame({"ticker": df["ticker"], "factor_value": m})
        n_out = int(out["factor_value"].notna().sum())
        logger.info(f"BeneishM({date}): {n_out} / {len(out)} 有值")
        return out
