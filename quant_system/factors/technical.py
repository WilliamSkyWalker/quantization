"""
技术因子

实现基于交易数据的技术因子：
    - TURN_20D: 过去 20 个交易日平均换手率（流动性代理变量）

换手率越高通常意味着流动性越好，但过高可能暗示投机行为。
在多因子模型中，换手率因子常被用作流动性控制变量。
"""

import numpy as np
import pandas as pd

from factors.base import FactorBase


class Turnover20DFactor(FactorBase):
    """
    过去 20 个交易日平均换手率。

    使用日线行情中的 turnover_rate 字段，取最近 20 个交易日的均值。
    """

    name = "TURN_20D"
    description = "过去20日平均换手率，流动性代理"

    def compute(self, date: str, universe: pd.DataFrame) -> pd.DataFrame:
        codes = universe["ts_code"].tolist()

        # 取约30个自然日确保覆盖20个交易日
        df_price = self.get_price_history(
            date, lookback_days=45,
            universe_codes=codes,
            columns=["turnover_rate"],
        )

        if df_price.empty:
            return pd.DataFrame(columns=["ts_code", "factor_value"])

        # 每只股票取最近20个交易日
        df_price["trade_date"] = pd.to_datetime(df_price["trade_date"])
        df_price = df_price.sort_values(["ts_code", "trade_date"])

        df_recent = (
            df_price.groupby("ts_code")
            .tail(20)
        )

        df_avg = (
            df_recent.groupby("ts_code")["turnover_rate"]
            .mean()
            .reset_index()
        )
        df_avg.columns = ["ts_code", "factor_value"]

        return df_avg
