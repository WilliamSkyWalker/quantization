"""Asset Growth 反向因子 (Cooper, Gulen, Schill 2008, JF)

定义：
    AG = (Total Assets_t - Total Assets_{t-4Q}) / Total Assets_{t-4Q}

经济直觉：
    - 资产高增长的公司通常在过度投资（empire building），后续回报低
    - 反向因子：AG 越高 → 预期收益越低（做空）

因子方向：-1（高 AG = 利空）
"""

import logging

import numpy as np
import pandas as pd

from services.config import LOG_LEVEL
from stocks.services.factors.us_registry import AlphaSignal, register

logger = logging.getLogger(__name__)
logger.setLevel(LOG_LEVEL)


@register
class AssetGrowth(AlphaSignal):
    """Asset Growth — 总资产同比增长率（反向）。"""

    name = "ASSET_GROWTH"
    version = "v1"
    category = "value"
    horizon = "quarter"
    expected_icir = 0.20
    status = "staging"
    inherent_direction = -1
    data_deps = ["us_financial_data"]
    ic_window_months = 30

    _FIN_COLS = ["total_assets"]

    def compute(self, date: str, universe: pd.DataFrame) -> pd.DataFrame:
        tickers = universe["ticker"].tolist()
        if not tickers:
            logger.debug("AssetGrowth: 空 universe")
            return pd.DataFrame(columns=["ticker", "factor_value"])

        # 取 6 季报（保证至少能配对 now + yoy）
        hist = self.fetch_financial_history(date, tickers, self._FIN_COLS, n_quarters=6)
        if hist.empty:
            logger.warning(f"AssetGrowth({date}): fetch_financial_history 为空")
            return pd.DataFrame(columns=["ticker", "factor_value"])

        paired = self.pick_year_ago(hist, self._FIN_COLS)
        if paired.empty:
            logger.warning(f"AssetGrowth({date}): pick_year_ago 为空")
            return pd.DataFrame(columns=["ticker", "factor_value"])

        df = paired.copy()
        ta_yoy = df["total_assets_yoy"].replace(0, np.nan)
        ag = (df["total_assets_now"] - df["total_assets_yoy"]) / ta_yoy.abs()

        out = pd.DataFrame({"ticker": df["ticker"], "factor_value": ag})
        n_out = int(out["factor_value"].notna().sum())
        logger.info(f"AssetGrowth({date}): {n_out} / {len(out)} 有值")
        return out[["ticker", "factor_value"]]
