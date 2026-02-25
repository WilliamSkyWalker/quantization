"""
风控模块

实现组合风控规则：
    1. 个股持仓上限：单只股票权重不超过 5%
    2. 行业暴露上限：单行业权重不超过 30%
    3. 最大回撤触发降仓：回撤超 15% 时仓位降至 50%
    4. 流动性过滤：剔除日均成交额低于 5000 万的股票

核心接口：
    adjust_weights(weights_df, date) -> 风控后的权重 DataFrame
"""

import logging
from typing import Optional

import numpy as np
import pandas as pd

from config.settings import (
    MAX_SINGLE_WEIGHT,
    MAX_INDUSTRY_WEIGHT,
    MAX_DRAWDOWN_THRESHOLD,
    DRAWDOWN_REDUCE_POSITION,
    MIN_DAILY_TURNOVER,
    LOG_LEVEL,
)
from data.database import DatabaseManager

logger = logging.getLogger(__name__)
logger.setLevel(LOG_LEVEL)


class RiskManager:
    """
    风控管理器。

    用法:
        rm = RiskManager(db)
        adjusted = rm.adjust_weights(weights_df, "2024-12-31")
        position_scale = rm.check_drawdown(nav_series)
    """

    def __init__(
        self,
        db: DatabaseManager,
        max_single_weight: float = MAX_SINGLE_WEIGHT,
        max_industry_weight: float = MAX_INDUSTRY_WEIGHT,
        max_drawdown: float = MAX_DRAWDOWN_THRESHOLD,
        drawdown_position: float = DRAWDOWN_REDUCE_POSITION,
        min_turnover: float = MIN_DAILY_TURNOVER,
    ):
        """
        Args:
            db: DatabaseManager 实例。
            max_single_weight: 个股持仓上限。
            max_industry_weight: 单行业暴露上限。
            max_drawdown: 最大回撤降仓阈值。
            drawdown_position: 触发降仓后的目标仓位。
            min_turnover: 日均成交额下限（元）。
        """
        self.db = db
        self.max_single_weight = max_single_weight
        self.max_industry_weight = max_industry_weight
        self.max_drawdown = max_drawdown
        self.drawdown_position = drawdown_position
        self.min_turnover = min_turnover

    def adjust_weights(
        self,
        weights_df: pd.DataFrame,
        date: str,
        nav_series: Optional[pd.Series] = None,
    ) -> pd.DataFrame:
        """
        对组合权重进行风控调整。

        处理流程：
            1. 流动性过滤
            2. 个股权重上限截断
            3. 行业暴露上限截断
            4. 回撤降仓（如有净值序列）
            5. 权重再归一化

        Args:
            weights_df: 原始权重 DataFrame，包含 ts_code, weight 列。
            date: 调仓日期。
            nav_series: 策略净值时间序列（用于回撤判断，可选）。

        Returns:
            风控调整后的 DataFrame[ts_code, weight]。
        """
        if weights_df.empty:
            return weights_df

        df = weights_df[["ts_code", "weight"]].copy()
        initial_count = len(df)

        # 1. 流动性过滤
        df = self._filter_liquidity(df, date)

        # 2. 个股权重上限
        df = self._cap_single_weight(df)

        # 3. 行业暴露上限
        df = self._cap_industry_weight(df, date)

        # 4. 权重归一化（在降仓之前归一化）
        df = self._normalize_weights(df)

        # 5. 最大回撤降仓（降仓后不再归一化，剩余部分视为现金）
        if nav_series is not None:
            position_scale = self.check_drawdown(nav_series)
            if position_scale < 1.0:
                df["weight"] = df["weight"] * position_scale
                logger.warning(
                    f"触发回撤降仓: 仓位缩减至 {position_scale:.0%}"
                )

        final_count = len(df)
        if final_count < initial_count:
            logger.info(f"风控过滤: {initial_count} -> {final_count} 只")

        return df

    # ----------------------------------------------------------
    # 流动性过滤
    # ----------------------------------------------------------

    def _filter_liquidity(self, df: pd.DataFrame, date: str) -> pd.DataFrame:
        """
        剔除日均成交额低于阈值的股票。

        Args:
            df: 权重 DataFrame。
            date: 当前日期。

        Returns:
            过滤后的 DataFrame。
        """
        if self.min_turnover <= 0:
            return df

        codes = df["ts_code"].tolist()
        codes_str = "','".join(codes)

        # 查最近20个交易日平均成交额
        lookback_start = (
            pd.to_datetime(date) - pd.Timedelta(days=40)
        ).strftime("%Y-%m-%d")

        df_amount = self.db.query(
            f"SELECT ts_code, AVG(amount) as avg_amount FROM daily_price "
            f"WHERE trade_date >= '{lookback_start}' "
            f"AND trade_date <= '{date}' "
            f"AND ts_code IN ('{codes_str}') "
            f"GROUP BY ts_code"
        )

        if df_amount.empty:
            return df

        liquid_codes = df_amount[
            df_amount["avg_amount"] * 1000 >= self.min_turnover
        ]["ts_code"].tolist()

        removed = set(codes) - set(liquid_codes)
        if removed:
            logger.info(f"流动性过滤剔除: {len(removed)} 只")

        return df[df["ts_code"].isin(liquid_codes)]

    # ----------------------------------------------------------
    # 个股权重上限
    # ----------------------------------------------------------

    def _cap_single_weight(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        截断个股权重至上限，超出部分等比分配给其他股票。

        Args:
            df: 权重 DataFrame。

        Returns:
            调整后的 DataFrame。
        """
        df = df.copy()

        # 迭代截断直到收敛
        for _ in range(10):
            over_mask = df["weight"] > self.max_single_weight
            if not over_mask.any():
                break

            excess = (df.loc[over_mask, "weight"] - self.max_single_weight).sum()
            df.loc[over_mask, "weight"] = self.max_single_weight

            # 将超出部分等比分配给未超限的股票
            under_mask = df["weight"] < self.max_single_weight
            if under_mask.any():
                under_total = df.loc[under_mask, "weight"].sum()
                if under_total > 0:
                    df.loc[under_mask, "weight"] += (
                        excess * df.loc[under_mask, "weight"] / under_total
                    )

        capped = (df["weight"] >= self.max_single_weight - 1e-6).sum()
        if capped > 0:
            logger.debug(f"个股权重上限: {capped} 只触及上限 {self.max_single_weight:.0%}")

        return df

    # ----------------------------------------------------------
    # 行业暴露上限
    # ----------------------------------------------------------

    def _cap_industry_weight(self, df: pd.DataFrame, date: str) -> pd.DataFrame:
        """
        截断单行业暴露至上限。

        Args:
            df: 权重 DataFrame。
            date: 当前日期。

        Returns:
            调整后的 DataFrame。
        """
        try:
            industry_map = self.db.get_industry_map()
        except Exception:
            return df

        if industry_map.empty:
            return df

        df = df.merge(industry_map, on="ts_code", how="left")
        df["industry_name"] = df["industry_name"].fillna("未知")

        # 计算行业权重
        ind_weights = df.groupby("industry_name")["weight"].sum()
        over_industries = ind_weights[ind_weights > self.max_industry_weight]

        for ind_name, ind_weight in over_industries.items():
            scale = self.max_industry_weight / ind_weight
            mask = df["industry_name"] == ind_name
            df.loc[mask, "weight"] *= scale
            logger.info(
                f"行业暴露截断: {ind_name} {ind_weight:.1%} -> {self.max_industry_weight:.0%}"
            )

        df = df.drop(columns=["industry_name"], errors="ignore")
        return df

    # ----------------------------------------------------------
    # 最大回撤检查
    # ----------------------------------------------------------

    def check_drawdown(self, nav_series: pd.Series) -> float:
        """
        检查当前回撤是否触发降仓。

        Args:
            nav_series: 策略净值时间序列。

        Returns:
            仓位缩放系数：1.0 = 满仓, <1.0 = 降仓。
        """
        if nav_series.empty:
            return 1.0

        peak = nav_series.cummax()
        current_drawdown = (nav_series.iloc[-1] - peak.iloc[-1]) / peak.iloc[-1]

        if current_drawdown < -self.max_drawdown:
            logger.warning(
                f"当前回撤 {current_drawdown:.2%} 超过阈值 {-self.max_drawdown:.2%}"
            )
            return self.drawdown_position

        return 1.0

    # ----------------------------------------------------------
    # 权重归一化
    # ----------------------------------------------------------

    @staticmethod
    def _normalize_weights(df: pd.DataFrame) -> pd.DataFrame:
        """权重归一化至总和为 1。"""
        total = df["weight"].sum()
        if total > 0:
            df["weight"] = df["weight"] / total
        return df

    # ----------------------------------------------------------
    # 风控报告
    # ----------------------------------------------------------

    def risk_report(self, weights_df: pd.DataFrame, date: str) -> dict:
        """
        生成风控报告。

        Args:
            weights_df: 当前持仓权重。
            date: 日期。

        Returns:
            风控指标字典。
        """
        df = weights_df.copy()

        report = {
            "持仓数量": len(df),
            "最大个股权重": f"{df['weight'].max():.2%}" if not df.empty else "0",
            "最小个股权重": f"{df['weight'].min():.2%}" if not df.empty else "0",
        }

        # 行业集中度
        try:
            industry_map = self.db.get_industry_map()
            if not industry_map.empty:
                df = df.merge(industry_map, on="ts_code", how="left")
                ind_weights = df.groupby("industry_name")["weight"].sum()
                report["最大行业暴露"] = f"{ind_weights.max():.2%}"
                report["行业数量"] = len(ind_weights)
                report["前3大行业"] = ", ".join(
                    f"{name}({w:.1%})"
                    for name, w in ind_weights.nlargest(3).items()
                )
        except Exception:
            pass

        return report
