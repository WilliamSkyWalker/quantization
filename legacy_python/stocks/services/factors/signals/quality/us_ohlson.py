"""Ohlson O-Score (Ohlson 1980, JAR)

破产概率预测（logit 模型）。公式：

    O = -1.32 − 0.407·log(TA/GNP)
        + 6.03·(TL/TA) − 1.43·(WC/TA) + 0.076·(CL/CA)
        − 1.72·OENEG − 2.37·(NI/TA) − 1.83·(FFO/TL)
        + 0.285·INTWO − 0.521·CHIN

    OENEG = 1 if TL > TA else 0             (资不抵债哑变量)
    INTWO = 1 if NI < 0 in both periods else 0  (连续两期亏损)
    CHIN  = (NI_t − NI_{t-1}) / (|NI_t| + |NI_{t-1}|)   (盈利变化率)
    FFO   = operating_cash_flow (近似)
    GNP   实际无法获取——用 log(TA/1e9) 近似（Ohlson 原始用 1968 美元 GNP 做归一化，
          我们用 10 亿美元刻度，等价于在常数项上加/减一个固定偏移，对截面排序无影响）

判读：O 越高，破产概率越高。因子方向：−1（反向）。
"""

import logging

import numpy as np
import pandas as pd

from services.config import LOG_LEVEL
from stocks.services.factors.us_registry import AlphaSignal, register

logger = logging.getLogger(__name__)
logger.setLevel(LOG_LEVEL)


@register
class OhlsonO(AlphaSignal):
    """Ohlson O-Score — 破产概率（越高越危险，反向因子）。"""

    name = "OHLSON_O"
    version = "v1"
    category = "quality"
    horizon = "quarter"
    expected_icir = 0.08
    status = "staging"
    inherent_direction = -1
    data_deps = ["us_financial_data"]
    ic_window_months = 24

    _FIN_COLS = [
        "total_assets",
        "total_liabilities",
        "total_current_assets",
        "total_current_liabilities",
        "net_income",
        "operating_cash_flow",
    ]

    def compute(self, date: str, universe: pd.DataFrame) -> pd.DataFrame:
        tickers = universe["ticker"].tolist()
        if not tickers:
            logger.debug("OhlsonO: 空 universe")
            return pd.DataFrame(columns=["ticker", "factor_value"])

        # 需要两条（当前 + 前季）做 CHIN 和 INTWO
        hist = self.fetch_financial_history(date, tickers, self._FIN_COLS, n_quarters=2)
        if hist.empty:
            logger.warning(f"OhlsonO({date}): 无财务数据")
            return pd.DataFrame(columns=["ticker", "factor_value"])

        rows = []
        for ticker, grp in hist.groupby("ticker", sort=False):
            if len(grp) < 1:
                continue
            now = grp.iloc[0]
            prev = grp.iloc[1] if len(grp) >= 2 else None

            ta = now.get("total_assets")
            tl = now.get("total_liabilities")
            ca = now.get("total_current_assets")
            cl = now.get("total_current_liabilities")
            ni = now.get("net_income")
            cfo = now.get("operating_cash_flow")

            if pd.isna(ta) or ta == 0:
                continue
            if pd.isna(tl) or pd.isna(ca) or pd.isna(cl) or pd.isna(ni):
                continue

            wc = ca - cl
            wc_ta = wc / ta
            tl_ta = tl / ta
            cl_ca = cl / ca if ca else np.nan
            ni_ta = ni / ta
            ffo_tl = (cfo / tl) if (not pd.isna(cfo) and tl) else np.nan

            oeneg = 1.0 if tl > ta else 0.0

            if prev is not None and not pd.isna(prev.get("net_income")):
                ni_prev = prev["net_income"]
                intwo = 1.0 if (ni < 0 and ni_prev < 0) else 0.0
                denom = abs(ni) + abs(ni_prev)
                chin = (ni - ni_prev) / denom if denom > 0 else 0.0
            else:
                intwo = 0.0
                chin = 0.0

            # log(TA / 1e9)：Ohlson 用 log(TA/GNP)，我们用 10 亿美元刻度近似
            ta_scaled = ta / 1e9
            log_ta = np.log(ta_scaled) if ta_scaled > 0 else np.nan

            if pd.isna(cl_ca) or pd.isna(log_ta):
                continue

            # FFO/TL 缺失时，论文做法是设为 0（等价于 "没有数据" 的中性处理）
            if pd.isna(ffo_tl):
                ffo_tl = 0.0

            o = (
                -1.32
                - 0.407 * log_ta
                + 6.03 * tl_ta
                - 1.43 * wc_ta
                + 0.076 * cl_ca
                - 1.72 * oeneg
                - 2.37 * ni_ta
                - 1.83 * ffo_tl
                + 0.285 * intwo
                - 0.521 * chin
            )
            rows.append({"ticker": ticker, "factor_value": o})

        if not rows:
            logger.warning(f"OhlsonO({date}): 无任何 ticker 计算成功")
            return pd.DataFrame(columns=["ticker", "factor_value"])

        out = pd.DataFrame(rows)
        logger.info(f"OhlsonO({date}): {len(out)} 有值")
        return out
