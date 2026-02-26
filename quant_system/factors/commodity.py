"""
商品价格轮动因子 (CMDTY_MOM)

利用商品期货价格动量驱动行业轮动：
    - 金银铜价格上涨 → 有色金属行业股票加分
    - 螺纹钢铁矿石上涨 → 钢铁行业股票加分
    - 原油上涨 → 石油石化行业股票加分

匹配逻辑（两层）：
    1. L2 优先：股票的申万二级行业 → 匹配 L2 商品映射
    2. L1 回退：L2 无匹配时 → 回退到申万一级行业匹配
    3. 无关行业：银行、计算机等无映射行业 → factor_value = NaN

同行业多商品：按 OI（持仓量）加权平均。
"""

import logging

import numpy as np
import pandas as pd

from config.settings import (
    COMMODITY_INDUSTRY_MAP,
    COMMODITY_MOM_LOOKBACK,
    COMMODITY_SYMBOLS,
    LOG_LEVEL,
)
from factors.base import FactorBase

logger = logging.getLogger(__name__)
logger.setLevel(LOG_LEVEL)


class CommodityMomentumFactor(FactorBase):
    """商品价格轮动因子，利用商品-行业联动效应。"""

    name = "CMDTY_MOM"
    description = "商品价格轮动因子，利用商品期货动量驱动行业轮动"

    def compute(self, date: str, universe: pd.DataFrame) -> pd.DataFrame:
        """
        计算商品动量因子。

        Args:
            date: 计算日期 YYYY-MM-DD。
            universe: 股票池 DataFrame（至少含 ts_code）。

        Returns:
            DataFrame[ts_code, factor_value]。
            无映射行业的股票 factor_value = NaN。
        """
        codes = universe["ts_code"].tolist()
        result = pd.DataFrame({"ts_code": codes, "factor_value": np.nan})

        # 1. 获取行业映射
        industry_df = self.db.get_industry_map()
        if industry_df.empty:
            logger.warning("CMDTY_MOM: 无行业分类数据")
            return result

        # 2. 获取商品价格历史
        lookback_cal_days = int(COMMODITY_MOM_LOOKBACK * 1.8)  # 交易日→自然日
        commodity_df = self.db.get_commodity_price_history(
            COMMODITY_SYMBOLS, date, lookback_cal_days
        )
        if commodity_df.empty:
            logger.warning("CMDTY_MOM: 无商品价格数据")
            return result

        # 3. 计算每种商品的动量和 OI
        commodity_mom = self._calc_commodity_momentum(commodity_df)
        if not commodity_mom:
            logger.warning("CMDTY_MOM: 商品动量计算结果为空")
            return result

        # 4. 构建行业→商品动量映射（L2 和 L1 两层）
        l2_momentum = self._aggregate_by_industry(commodity_mom, level="l2")
        l1_momentum = self._aggregate_by_industry(commodity_mom, level="l1")

        # 5. 为每只股票赋值
        stock_industry = industry_df[
            industry_df["ts_code"].isin(codes)
        ][["ts_code", "industry_name", "l2_industry_name"]].copy()

        if stock_industry.empty:
            return result

        def _get_momentum(row):
            l2 = row.get("l2_industry_name")
            l1 = row.get("industry_name")
            # L2 优先
            if pd.notna(l2) and l2 in l2_momentum:
                return l2_momentum[l2]
            # L1 回退
            if pd.notna(l1) and l1 in l1_momentum:
                return l1_momentum[l1]
            return np.nan

        stock_industry["factor_value"] = stock_industry.apply(_get_momentum, axis=1)

        # 合并回结果
        result = result[["ts_code"]].merge(
            stock_industry[["ts_code", "factor_value"]],
            on="ts_code",
            how="left",
        )
        result["factor_value"] = result["factor_value"].astype(float)

        valid = result["factor_value"].notna().sum()
        logger.debug(f"CMDTY_MOM: {valid}/{len(result)} 只有效值")
        return result[["ts_code", "factor_value"]]

    def _calc_commodity_momentum(
        self, df: pd.DataFrame
    ) -> dict[str, dict]:
        """
        计算每种商品的 N 日收益率和最新 OI。

        Returns:
            {commodity_code: {"mom": float, "oi": float}}
        """
        result = {}
        for code, grp in df.groupby("commodity_code"):
            grp = grp.sort_values("trade_date")
            if len(grp) < 2:
                continue

            # 使用 settle 优先，close 回退
            price_col = "settle" if grp["settle"].notna().any() else "close"
            prices = grp[price_col].dropna()
            if len(prices) < 2:
                continue

            # N 日收益率（取最后 N+1 个数据点）
            n = min(COMMODITY_MOM_LOOKBACK, len(prices) - 1)
            mom = prices.iloc[-1] / prices.iloc[-(n + 1)] - 1

            # 最新 OI
            oi = grp["oi"].dropna().iloc[-1] if grp["oi"].notna().any() else 1.0

            result[code] = {"mom": mom, "oi": max(oi, 1.0)}

        return result

    def _aggregate_by_industry(
        self,
        commodity_mom: dict[str, dict],
        level: str = "l2",
    ) -> dict[str, float]:
        """
        按行业聚合商品动量（OI 加权平均）。

        Args:
            commodity_mom: {commodity_code: {"mom": float, "oi": float}}
            level: "l1" 或 "l2"

        Returns:
            {industry_name: weighted_momentum}
        """
        # 构建 industry → [(mom, oi), ...] 映射
        industry_data: dict[str, list] = {}
        for code, data in commodity_mom.items():
            mapping = COMMODITY_INDUSTRY_MAP.get(code)
            if not mapping:
                continue
            ind_name = mapping.get(level)
            if not ind_name:
                continue
            if ind_name not in industry_data:
                industry_data[ind_name] = []
            industry_data[ind_name].append((data["mom"], data["oi"]))

        # OI 加权平均
        result = {}
        for ind_name, items in industry_data.items():
            total_oi = sum(oi for _, oi in items)
            if total_oi > 0:
                weighted_mom = sum(mom * oi for mom, oi in items) / total_oi
                result[ind_name] = weighted_mom

        return result
