"""
股息率因子 (DIV_YIELD)

利用 Tushare daily_basic 提供的 dv_ttm（近12个月股息率）构建因子。
高股息策略在 2024 年表现突出（银行 +31%、煤炭 +15%），
加入此因子有助于捕捉高股息行情。

数据来源：daily_price.dv_ttm（已通过 daily_basic 接口下载）。
回退逻辑：dv_ttm 缺失时，使用 EP（1/PE_TTM）近似（股息 ≈ 盈利 × 派息率）。
"""

import logging

import numpy as np
import pandas as pd

from backend.services.config import LOG_LEVEL
from backend.services.factors.base import FactorBase

logger = logging.getLogger(__name__)
logger.setLevel(LOG_LEVEL)


class DividendYieldFactor(FactorBase):
    """
    股息率因子，dv_ttm 越高表示股息回报越高。

    优先使用 daily_price 中的 dv_ttm（Tushare daily_basic 提供），
    缺失时回退到 1/pe_ttm 近似。
    """

    name = "DIV_YIELD"
    description = "近12个月股息率，高股息策略核心因子"

    def compute(self, date: str, universe: pd.DataFrame) -> pd.DataFrame:
        codes = universe["ts_code"].tolist()
        result = pd.DataFrame({"ts_code": codes, "factor_value": np.nan})

        # 从 daily_price 获取 dv_ttm（预加载路径自动从内存过滤）
        df_price = self.get_price_history(
            end_date=date,
            lookback_days=10,
            universe_codes=codes,
            columns=["dv_ttm", "pe_ttm"],
        )

        if df_price.empty:
            logger.warning("DIV_YIELD: 无价格数据")
            return result

        # 取每只股票最近一个交易日的数据
        df_price["trade_date"] = pd.to_datetime(df_price["trade_date"])
        latest = (
            df_price.sort_values("trade_date", ascending=False)
            .drop_duplicates(subset=["ts_code"], keep="first")
        )

        # 优先使用 dv_ttm
        if "dv_ttm" in latest.columns:
            latest["factor_value"] = pd.to_numeric(
                latest["dv_ttm"], errors="coerce"
            )

        # 回退：dv_ttm 缺失时用 1/pe_ttm 近似
        if "pe_ttm" in latest.columns:
            mask = latest["factor_value"].isna() | (latest["factor_value"] <= 0)
            pe = pd.to_numeric(latest["pe_ttm"], errors="coerce")
            # 只对 PE > 0 的计算（亏损股无股息意义）
            fallback = np.where((pe > 0) & pe.notna(), 1.0 / pe * 100, np.nan)
            latest.loc[mask, "factor_value"] = fallback[mask.values]

        # 股息率为负或零视为无效
        latest.loc[latest["factor_value"] <= 0, "factor_value"] = np.nan

        # 合并回结果
        result = result[["ts_code"]].merge(
            latest[["ts_code", "factor_value"]],
            on="ts_code",
            how="left",
        )
        result["factor_value"] = result["factor_value"].astype(float)

        valid = result["factor_value"].notna().sum()
        logger.debug(f"DIV_YIELD: {valid}/{len(result)} 只有效值")
        return result[["ts_code", "factor_value"]]
