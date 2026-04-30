"""
A 股风控模块（Django ORM 版）

迁自 services/risk/risk_manager.py。

实现：
    1. 流动性过滤（日均成交额 < 阈值剔除）
    2. 个股权重上限
    3. 单行业暴露上限（申万 L1）
    4. 关联行业组合上限（如地产链）
    5. 最大回撤降仓 / 波动率目标管理
"""

import logging
from typing import Optional

import numpy as np
import pandas as pd

from services.config import (
    DD_MAX_THRESHOLD,
    DD_MIN_POSITION,
    DD_START_THRESHOLD,
    DRAWDOWN_REDUCE_POSITION,
    INDUSTRY_GROUPS,
    LOG_LEVEL,
    MAX_DRAWDOWN_THRESHOLD,
    MAX_INDUSTRY_GROUP_WEIGHT,
    MAX_INDUSTRY_WEIGHT,
    MAX_SINGLE_WEIGHT,
    MIN_DAILY_TURNOVER,
    TARGET_VOL,
    USE_VOL_TARGETING,
    VOL_LOOKBACK_DAYS,
    VOL_SCALE_MAX,
    VOL_SCALE_MIN,
)
from stocks.models import ADailyPrice, AIndustryClass

logger = logging.getLogger(__name__)
logger.setLevel(LOG_LEVEL)


class RiskManager:
    """A 股风控管理器。"""

    def __init__(
        self,
        db=None,
        max_single_weight: float = MAX_SINGLE_WEIGHT,
        max_industry_weight: float = MAX_INDUSTRY_WEIGHT,
        max_drawdown: float = MAX_DRAWDOWN_THRESHOLD,
        drawdown_position: float = DRAWDOWN_REDUCE_POSITION,
        dd_start_threshold: float = DD_START_THRESHOLD,
        dd_max_threshold: float = DD_MAX_THRESHOLD,
        dd_min_position: float = DD_MIN_POSITION,
        min_turnover: float = MIN_DAILY_TURNOVER,
        use_vol_targeting: bool = USE_VOL_TARGETING,
        target_vol: float = TARGET_VOL,
        vol_lookback: int = VOL_LOOKBACK_DAYS,
        vol_scale_min: float = VOL_SCALE_MIN,
        vol_scale_max: float = VOL_SCALE_MAX,
    ):
        # db 参数保留兼容性，内部用 Django ORM
        self.db = db
        self.max_single_weight = max_single_weight
        self.max_industry_weight = max_industry_weight
        self.max_drawdown = max_drawdown
        self.drawdown_position = drawdown_position
        self.dd_start = dd_start_threshold
        self.dd_max = dd_max_threshold
        self.dd_min_position = dd_min_position
        self.min_turnover = min_turnover
        self.use_vol_targeting = use_vol_targeting
        self.target_vol = target_vol
        self.vol_lookback = vol_lookback
        self.vol_scale_min = vol_scale_min
        self.vol_scale_max = vol_scale_max

    def adjust_weights(
        self,
        weights_df: pd.DataFrame,
        date: str,
        nav_series: Optional[pd.Series] = None,
    ) -> pd.DataFrame:
        """对组合权重做风控调整。"""
        if weights_df.empty:
            logger.debug("adjust_weights: 输入空，直接返回")
            return weights_df

        df = weights_df[["ts_code", "weight"]].copy()
        initial_count = len(df)

        df = self._filter_liquidity(df, date)
        df = self._cap_single_weight(df)
        df = self._cap_industry_weight(df, date)
        df = self._cap_industry_group_weight(df, date)
        df = self._normalize_weights(df)

        if nav_series is not None:
            if self.use_vol_targeting:
                scale = self.calc_vol_scale(nav_series)
                if scale < 1.0:
                    df["weight"] = df["weight"] * scale
                    logger.info(f"波动率目标: 仓位缩放 {scale:.0%}")
            else:
                scale = self.check_drawdown(nav_series)
                if scale < 1.0:
                    df["weight"] = df["weight"] * scale
                    logger.warning(f"触发回撤降仓: {scale:.0%}")

        final_count = len(df)
        if final_count < initial_count:
            logger.info(f"风控过滤: {initial_count} -> {final_count}")
        return df

    # ----------------------------------------------------------
    # 流动性
    # ----------------------------------------------------------

    def _filter_liquidity(self, df: pd.DataFrame, date: str) -> pd.DataFrame:
        """剔除日均成交额 < 阈值的股票。"""
        if self.min_turnover <= 0:
            return df

        codes = df["ts_code"].tolist()
        if not codes:
            return df

        lookback_start = (pd.to_datetime(date) - pd.Timedelta(days=40)).date()
        end_d = pd.to_datetime(date).date()

        # 用 Django aggregate + 分组
        from django.db.models import Avg
        rows = list(
            ADailyPrice.objects.filter(
                ts_code__in=codes,
                trade_date__gte=lookback_start,
                trade_date__lte=end_d,
            ).values("ts_code").annotate(avg_amount=Avg("amount"))
        )
        if not rows:
            logger.debug("_filter_liquidity: 无成交额数据，跳过")
            return df

        df_amount = pd.DataFrame(rows)
        # amount 单位是"千元"，乘 1000 换成元
        liquid_codes = df_amount[df_amount["avg_amount"] * 1000 >= self.min_turnover]["ts_code"].tolist()
        removed = set(codes) - set(liquid_codes)
        if removed:
            logger.info(f"流动性过滤剔除: {len(removed)} 只")
        return df[df["ts_code"].isin(liquid_codes)]

    # ----------------------------------------------------------
    # 个股权重
    # ----------------------------------------------------------

    def _cap_single_weight(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()
        for _ in range(10):
            over_mask = df["weight"] > self.max_single_weight
            if not over_mask.any():
                break
            excess = (df.loc[over_mask, "weight"] - self.max_single_weight).sum()
            df.loc[over_mask, "weight"] = self.max_single_weight
            under_mask = df["weight"] < self.max_single_weight
            if under_mask.any():
                under_total = df.loc[under_mask, "weight"].sum()
                if under_total > 0:
                    df.loc[under_mask, "weight"] += (
                        excess * df.loc[under_mask, "weight"] / under_total
                    )
        capped = (df["weight"] >= self.max_single_weight - 1e-6).sum()
        if capped > 0:
            logger.debug(f"个股权重上限: {capped} 只触及")
        return df

    # ----------------------------------------------------------
    # 行业
    # ----------------------------------------------------------

    def _get_industry_map(self) -> pd.DataFrame:
        """DataFrame[ts_code, industry_name]，缓存到实例属性。"""
        if hasattr(self, "_industry_map_cache"):
            return self._industry_map_cache
        try:
            rows = list(
                AIndustryClass.objects.filter(
                    src="SW2021", level="L1", out_date__isnull=True,
                ).values("ts_code", "index_name")
            )
            df = pd.DataFrame(rows)
            if not df.empty:
                df = df.rename(columns={"index_name": "industry_name"})
            else:
                df = pd.DataFrame(columns=["ts_code", "industry_name"])
        except Exception as e:
            logger.warning(f"_get_industry_map 失败: {e}")
            df = pd.DataFrame(columns=["ts_code", "industry_name"])
        self._industry_map_cache = df
        return df

    def _cap_industry_weight(self, df: pd.DataFrame, date: str) -> pd.DataFrame:
        industry_map = self._get_industry_map()
        if industry_map.empty:
            return df

        df = df.merge(industry_map, on="ts_code", how="left")
        df["industry_name"] = df["industry_name"].fillna("未知")

        ind_weights = df.groupby("industry_name")["weight"].sum()
        over = ind_weights[ind_weights > self.max_industry_weight]
        for ind_name, w in over.items():
            scale = self.max_industry_weight / w
            mask = df["industry_name"] == ind_name
            df.loc[mask, "weight"] *= scale
            logger.info(
                f"行业暴露截断: {ind_name} {w:.1%} -> {self.max_industry_weight:.0%}"
            )
        df = df.drop(columns=["industry_name"], errors="ignore")
        return df

    def _cap_industry_group_weight(self, df: pd.DataFrame, date: str) -> pd.DataFrame:
        if not INDUSTRY_GROUPS or MAX_INDUSTRY_GROUP_WEIGHT >= 1.0:
            return df

        industry_map = self._get_industry_map()
        if industry_map.empty:
            return df

        df = df.merge(industry_map, on="ts_code", how="left")
        df["industry_name"] = df["industry_name"].fillna("未知")

        for group_name, industries in INDUSTRY_GROUPS.items():
            mask = df["industry_name"].isin(industries)
            group_weight = df.loc[mask, "weight"].sum()
            if group_weight > MAX_INDUSTRY_GROUP_WEIGHT:
                scale = MAX_INDUSTRY_GROUP_WEIGHT / group_weight
                df.loc[mask, "weight"] *= scale
                logger.info(
                    f"关联行业组截断: {group_name}({','.join(industries)}) "
                    f"{group_weight:.1%} -> {MAX_INDUSTRY_GROUP_WEIGHT:.0%}"
                )
        df = df.drop(columns=["industry_name"], errors="ignore")
        return df

    # ----------------------------------------------------------
    # 回撤 / 波动率
    # ----------------------------------------------------------

    def check_drawdown(self, nav_series: pd.Series) -> float:
        if nav_series.empty:
            return 1.0
        peak = nav_series.cummax()
        dd = abs((nav_series.iloc[-1] - peak.iloc[-1]) / peak.iloc[-1])
        if dd <= self.dd_start:
            return 1.0
        if dd >= self.dd_max:
            logger.warning(f"回撤 {dd:.2%} 达最大阈值 {self.dd_max:.2%}，仓位降至 {self.dd_min_position:.0%}")
            return self.dd_min_position
        scale = 1.0 - (dd - self.dd_start) / (self.dd_max - self.dd_start) * (1.0 - self.dd_min_position)
        logger.info(f"线性回撤: dd={dd:.2%}, 仓位={scale:.0%}")
        return scale

    def calc_vol_scale(self, nav_series: pd.Series) -> float:
        if len(nav_series) < self.vol_lookback + 1:
            return self.vol_scale_max
        daily_ret = nav_series.pct_change().dropna()
        recent = daily_ret.iloc[-self.vol_lookback:]
        realized_vol = recent.std() * np.sqrt(252)
        if realized_vol <= 0 or np.isnan(realized_vol):
            return self.vol_scale_max
        scale = self.target_vol / realized_vol
        scale = np.clip(scale, self.vol_scale_min, self.vol_scale_max)
        logger.debug(
            f"vol scale: realized={realized_vol:.2%}, target={self.target_vol:.2%}, scale={scale:.2f}"
        )
        return float(scale)

    @staticmethod
    def _normalize_weights(df: pd.DataFrame) -> pd.DataFrame:
        total = df["weight"].sum()
        if total > 0:
            df["weight"] = df["weight"] / total
        return df

    # ----------------------------------------------------------
    # 报告
    # ----------------------------------------------------------

    def risk_report(self, weights_df: pd.DataFrame, date: str) -> dict:
        df = weights_df.copy()
        report = {
            "持仓数量": len(df),
            "最大个股权重": f"{df['weight'].max():.2%}" if not df.empty else "0",
            "最小个股权重": f"{df['weight'].min():.2%}" if not df.empty else "0",
        }
        try:
            industry_map = self._get_industry_map()
            if not industry_map.empty:
                df = df.merge(industry_map, on="ts_code", how="left")
                ind_weights = df.groupby("industry_name")["weight"].sum()
                report["最大行业暴露"] = f"{ind_weights.max():.2%}"
                report["行业数量"] = len(ind_weights)
                report["前3大行业"] = ", ".join(
                    f"{name}({w:.1%})" for name, w in ind_weights.nlargest(3).items()
                )
        except Exception as e:
            logger.warning(f"risk_report 行业集中度失败: {e}")
        return report
