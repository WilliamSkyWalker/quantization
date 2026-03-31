"""
美股复合 Regime 检测器 + 动态 Beta 目标

四维复合指标：
    1. 趋势：S&P 500 vs 60 日 MA
    2. 波动率：VIX 历史百分位
    3. 信用：10Y-2Y 国债利差
    4. 因子拥挤度：动量因子截面分散度

输出：
    strength ∈ [0, 1]（1=牛，0=熊）
    target_beta：根据 strength 映射的目标 beta

Beta 映射：
    牛市 (strength ≥ 0.8) → β = 0.8
    震荡 (0.3~0.8)       → β = 0.3~0.8 线性插值
    熊市 (strength ≤ 0.3) → β = 0.2
"""

import logging

import numpy as np
import pandas as pd

from services.config import (
    LOG_LEVEL,
    US_REGIME_INDEX,
    US_REGIME_MA_WINDOW,
)
from services.data.database import DatabaseManager

logger = logging.getLogger(__name__)
logger.setLevel(LOG_LEVEL)

_TRANSITION_BAND = 0.05
_VIX_LOOKBACK_DAYS = 252

# Beta 目标映射（保留接口但 alpha-first 模式不使用）
_BETA_BULL = 0.8
_BETA_BEAR = 0.2
_BETA_MID = 0.5


class USRegimeDetector:
    """美股复合 Regime 检测器 + 动态 Beta 目标。"""

    def __init__(self, db: DatabaseManager):
        self._db = db
        self._cache: dict[str, dict] = {}

    def detect(self, date: str) -> dict:
        """
        检测 regime 并返回目标 beta。

        Returns:
            {"strength": float, "target_beta": float,
             "trend": float, "vol": float, "credit": float, "crowding": float}
        """
        if date in self._cache:
            return self._cache[date]

        trend = self._trend_score(date)
        vol = self._vol_score(date)
        credit = self._credit_score(date)
        crowding = self._crowding_score(date)

        # 加权合成
        base_strength = 0.35 * trend + 0.30 * vol + 0.25 * credit + 0.10 * crowding
        strength = max(0.0, min(1.0, base_strength))

        # Credit veto：利差倒挂时封顶，防止熊市反弹陷阱
        if credit < 0.2 and strength > 0.5:
            logger.debug(f"Credit veto: credit={credit:.2f}, strength {strength:.2f} → 0.50")
            strength = 0.5

        # 因子拥挤时额外惩罚
        if crowding < 0.3 and strength > 0.6:
            strength = strength * 0.8
            logger.debug(f"Factor crowding penalty: crowding={crowding:.2f}, strength compressed")

        # Beta 目标映射（alpha-first 模式仅用 strength，beta target 保留接口）
        target_beta = _BETA_BEAR + (_BETA_BULL - _BETA_BEAR) * strength

        result = {
            "strength": strength,
            "target_beta": target_beta,
            "trend": trend,
            "vol": vol,
            "credit": credit,
            "crowding": crowding,
        }
        self._cache[date] = result

        logger.debug(
            f"US Regime: date={date}, trend={trend:.2f}, vol={vol:.2f}, "
            f"credit={credit:.2f}, crowding={crowding:.2f}, "
            f"strength={strength:.2f}, β_target={target_beta:.2f}"
        )
        return result

    def detect_strength(self, date: str) -> float:
        """兼容旧接口。"""
        return self.detect(date)["strength"]

    def get_target_beta(self, date: str) -> float:
        return self.detect(date)["target_beta"]

    # ----------------------------------------------------------
    # 四个维度
    # ----------------------------------------------------------

    def _trend_score(self, date: str) -> float:
        """S&P 500 vs 60 日 MA → [0, 1]"""
        lookback = US_REGIME_MA_WINDOW + 10
        df = self._db.query(
            "SELECT trade_date, close FROM us_index_daily "
            "WHERE index_code = :index AND trade_date <= :date "
            "ORDER BY trade_date DESC LIMIT :limit",
            params={"index": US_REGIME_INDEX, "date": date, "limit": lookback},
        )
        if df.empty or len(df) < US_REGIME_MA_WINDOW:
            return 0.5
        prices = pd.to_numeric(df["close"], errors="coerce").dropna().values
        if len(prices) < US_REGIME_MA_WINDOW:
            return 0.5
        current, ma = prices[0], np.mean(prices[:US_REGIME_MA_WINDOW])
        if ma <= 0:
            return 0.5
        dev = (current - ma) / ma
        if dev >= _TRANSITION_BAND:
            return 1.0
        elif dev <= -_TRANSITION_BAND:
            return 0.0
        return (dev + _TRANSITION_BAND) / (2 * _TRANSITION_BAND)

    def _vol_score(self, date: str) -> float:
        """VIX 历史百分位 → [0, 1]"""
        df = self._db.query(
            "SELECT value FROM us_macro_indicator "
            "WHERE indicator_code = 'US_VIX' AND report_date <= :date "
            "ORDER BY report_date DESC LIMIT :limit",
            params={"date": date, "limit": _VIX_LOOKBACK_DAYS + 10},
        )
        if df.empty or len(df) < 20:
            return 0.5
        vals = pd.to_numeric(df["value"], errors="coerce").dropna().values
        if len(vals) < 20:
            return 0.5
        pct = np.mean(vals <= vals[0])
        if pct <= 0.2:
            return 1.0
        elif pct >= 0.8:
            return 0.0
        return 1.0 - (pct - 0.2) / 0.6

    def _credit_score(self, date: str) -> float:
        """10Y-2Y 利差 → [0, 1]"""
        df = self._db.query(
            "SELECT value FROM us_macro_indicator "
            "WHERE indicator_code = 'US_2Y10Y' AND report_date <= :date "
            "ORDER BY report_date DESC LIMIT 5",
            params={"date": date},
        )
        if df.empty:
            return 0.5
        spread = pd.to_numeric(df["value"], errors="coerce").dropna()
        if spread.empty:
            return 0.5
        v = float(spread.iloc[0])
        if v >= 0.5:
            return 1.0
        elif v <= -0.5:
            return 0.0
        return (v + 0.5) / 1.0

    def _crowding_score(self, date: str) -> float:
        """
        因子拥挤度：动量因子截面分散度的历史百分位。

        高分散 = 因子信号分化大 = 因子有效 → 1.0（牛）
        低分散 = 因子信号收敛/拥挤 = 因子失效 → 0.0（降权）
        """
        from services.us_factors.base import USFactorBase

        # 从预加载的 rolling stats 获取动量截面分散度
        ri = USFactorBase._static_cache.get("_rolling_indexed")
        if ri is None:
            return 0.5  # 无数据时中性

        date_ts = pd.to_datetime(date)

        # 获取最近 252 天的动量（20d cumulative return）截面标准差
        try:
            # 取最近的日期
            available_dates = ri.index.get_level_values("trade_date").unique()
            recent = available_dates[available_dates <= date_ts]
            if len(recent) < 60:
                return 0.5

            dispersions = []
            sample_dates = recent[-252::5]  # 每 5 天取一次，共约 50 个样本
            for d in sample_dates:
                try:
                    day = ri.xs(d, level="trade_date")
                    ret = day["cum_ret_20d"].dropna()
                    if len(ret) > 20:
                        dispersions.append(ret.std())
                except KeyError:
                    continue

            if len(dispersions) < 10:
                return 0.5

            # 当前分散度
            try:
                today = ri.xs(recent[-1], level="trade_date")
                current_disp = today["cum_ret_20d"].dropna().std()
            except KeyError:
                return 0.5

            # 百分位
            pct = np.mean(np.array(dispersions) <= current_disp)

            # 高分散（高百分位）= 因子有效 → 高分
            # 低分散（低百分位）= 拥挤 → 低分
            if pct >= 0.8:
                return 1.0
            elif pct <= 0.2:
                return 0.0
            return (pct - 0.2) / 0.6

        except Exception as e:
            logger.debug(f"Crowding score failed: {e}")
            return 0.5
