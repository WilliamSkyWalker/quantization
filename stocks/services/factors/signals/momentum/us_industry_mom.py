"""Industry Momentum (Moskowitz & Grinblatt 1999, JF)

    FACTOR = Ret_stock_12M − Ret_industry_median_12M

"行业内相对动量"——每只股票过去 12 月收益率减去其所属行业中位数收益率。

行业动量解释了大部分个股动量，但 Grinblatt 发现行业内动量（剔除行业影响）
仍有显著 alpha。这里我们用差分版：individual − industry，取相对行业的超额。

学术证据：Moskowitz-Grinblatt 1999 原文发现 industry momentum 是 alpha 来源，
优于 pure 12-1 momentum。

方向：+1（高值 = 行业内跑赢 → 利好）。
"""

import logging

import numpy as np
import pandas as pd

from services.config import LOG_LEVEL
from stocks.services.factors.us_registry import AlphaSignal, register

logger = logging.getLogger(__name__)
logger.setLevel(LOG_LEVEL)


@register
class IndustryMomentum(AlphaSignal):
    """Industry Momentum：个股 12M 收益 − 所属行业中位数 12M 收益。"""

    name = "INDUSTRY_MOM"
    version = "v1"
    category = "momentum"
    horizon = "month"
    expected_icir = 0.08
    status = "staging"
    inherent_direction = +1
    data_deps = ["us_daily_price", "us_industry_class"]
    ic_window_months = 12

    _LOOKBACK_DAYS = 380

    def compute(self, date: str, universe: pd.DataFrame) -> pd.DataFrame:
        tickers = universe["ticker"].tolist()
        if not tickers:
            logger.debug("IndustryMomentum: 空 universe")
            return pd.DataFrame(columns=["ticker", "factor_value"])

        ind_map = self.fetch_industry_map(tickers)
        if ind_map.empty:
            logger.warning(f"IndustryMomentum({date}): 无行业映射")
            return pd.DataFrame(columns=["ticker", "factor_value"])

        hist = self.fetch_price_history(
            date, tickers, lookback_days=self._LOOKBACK_DAYS, columns=["adj_close"]
        )
        if hist.empty:
            logger.warning(f"IndustryMomentum({date}): 无价格数据")
            return pd.DataFrame(columns=["ticker", "factor_value"])

        # 每只股票算 12M return
        ret_rows = []
        for ticker, grp in hist.groupby("ticker", sort=False):
            prices = grp["adj_close"].dropna().values
            if len(prices) < 200:
                continue
            start_p = prices[0]
            end_p = prices[-1]
            if pd.isna(start_p) or pd.isna(end_p) or start_p <= 0:
                continue
            ret_rows.append({"ticker": ticker, "ret_12m": (end_p / start_p) - 1.0})

        if not ret_rows:
            logger.warning(f"IndustryMomentum({date}): 无有效收益")
            return pd.DataFrame(columns=["ticker", "factor_value"])

        ret = pd.DataFrame(ret_rows)
        merged = ret.merge(ind_map[["ticker", "industry"]], on="ticker", how="left")
        merged["industry"] = merged["industry"].fillna("__unknown__")

        # 按 industry 算中位数
        ind_median = merged.groupby("industry")["ret_12m"].median().to_dict()
        merged["industry_median"] = merged["industry"].map(ind_median)
        merged["factor_value"] = merged["ret_12m"] - merged["industry_median"]

        out = merged[["ticker", "factor_value"]].copy()
        logger.info(f"IndustryMomentum({date}): {len(out)} 有值")
        return out
