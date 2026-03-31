"""
市场状态检测（Regime Detection）

基于 CSI 300 与 N 日均线的偏离度判断牛/熊市，支持渐进式切换。
用于动态调整大类权重（熊市降动量、升质量/价值）。
"""

import logging

import numpy as np
import pandas as pd

from services.config import (
    REGIME_MA_WINDOW,
    REGIME_INDEX_CODE,
    LOG_LEVEL,
)
from services.data.database import DatabaseManager

logger = logging.getLogger(__name__)
logger.setLevel(LOG_LEVEL)

# 渐进式切换：MA 偏离度在 [-TRANSITION_BAND, +TRANSITION_BAND] 区间内线性插值
REGIME_TRANSITION_BAND = 0.05  # ±5%


class RegimeDetector:
    """
    市场状态检测器（渐进式切换）。

    通过比较 CSI 300 收盘价与 N 日简单移动平均线判断牛/熊市。
    在 MA ±5% 区间内做线性插值，避免频繁二元切换（whipsaw）。

    用法:
        detector = RegimeDetector(db)
        regime = detector.detect("2024-12-31")          # "bull" 或 "bear"
        strength = detector.detect_strength("2024-12-31")  # 0.0~1.0 (0=纯熊, 1=纯牛)
    """

    def __init__(
        self,
        db: DatabaseManager,
        ma_window: int = REGIME_MA_WINDOW,
        index_code: str = REGIME_INDEX_CODE,
    ):
        self.db = db
        self.ma_window = ma_window
        self.index_code = index_code

    def _get_ma_deviation(self, date: str) -> float | None:
        """
        计算收盘价与 MA 的偏离度。

        Returns:
            (close - MA) / MA，数据不足返回 None。
        """
        lookback_days = self.ma_window * 2
        start_date = (
            pd.to_datetime(date) - pd.Timedelta(days=lookback_days)
        ).strftime("%Y-%m-%d")

        df = self.db.query(
            "SELECT trade_date, close FROM daily_price "
            "WHERE ts_code = :index_code "
            "AND trade_date >= :start_date "
            "AND trade_date <= :date "
            "ORDER BY trade_date",
            params={
                "index_code": self.index_code,
                "start_date": start_date,
                "date": date,
            },
        )

        if df.empty or len(df) < self.ma_window:
            logger.debug(
                f"Regime 检测: 数据不足({len(df)}/{self.ma_window})，回退到 bull"
            )
            return None

        df["close"] = pd.to_numeric(df["close"], errors="coerce")
        ma = df["close"].iloc[-self.ma_window:].mean()
        current_close = df["close"].iloc[-1]

        if ma <= 0:
            return None

        deviation = (current_close - ma) / ma
        logger.info(
            f"Regime 检测: {self.index_code} close={current_close:.2f}, "
            f"MA{self.ma_window}={ma:.2f}, 偏离={deviation:+.2%}"
        )
        return deviation

    def detect(self, date: str) -> str:
        """
        检测指定日期的市场状态（兼容旧接口）。

        Returns:
            "bull" 或 "bear"。
        """
        deviation = self._get_ma_deviation(date)
        if deviation is None:
            return "bull"
        return "bull" if deviation >= 0 else "bear"

    def detect_strength(self, date: str) -> float:
        """
        检测市场牛熊强度（渐进式）。

        在 MA ±5% 过渡带内线性插值：
            deviation >= +5%  → 1.0（纯牛）
            deviation <= -5%  → 0.0（纯熊）
            中间              → 线性插值

        Returns:
            0.0~1.0 的牛市强度（0=纯熊, 1=纯牛）。
        """
        deviation = self._get_ma_deviation(date)
        if deviation is None:
            return 1.0  # 数据不足回退到牛市

        band = REGIME_TRANSITION_BAND
        if deviation >= band:
            strength = 1.0
        elif deviation <= -band:
            strength = 0.0
        else:
            # [-band, +band] 线性插值到 [0, 1]
            strength = (deviation + band) / (2 * band)

        strength = float(np.clip(strength, 0.0, 1.0))
        regime_label = "bull" if strength > 0.5 else "bear"
        logger.info(f"Regime 强度: {strength:.2f} ({regime_label})")
        return strength
