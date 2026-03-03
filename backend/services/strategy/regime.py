"""
市场状态检测（Regime Detection）

基于 CSI 300 是否在 N 日均线上方判断牛/熊市。
用于动态调整大类权重（熊市降动量、升质量）。
"""

import logging

import pandas as pd

from backend.services.config import (
    REGIME_MA_WINDOW,
    REGIME_INDEX_CODE,
    LOG_LEVEL,
)
from backend.services.data.database import DatabaseManager

logger = logging.getLogger(__name__)
logger.setLevel(LOG_LEVEL)


class RegimeDetector:
    """
    市场状态检测器。

    通过比较 CSI 300 收盘价与 N 日简单移动平均线判断牛/熊市。

    用法:
        detector = RegimeDetector(db)
        regime = detector.detect("2024-12-31")  # "bull" 或 "bear"
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

    def detect(self, date: str) -> str:
        """
        检测指定日期的市场状态。

        查询 daily_price 中指数收盘价，计算 N 日 MA。
        当日收盘价 >= MA 时为 bull，否则为 bear。
        数据不足时回退到 bull。

        Args:
            date: 日期，格式 YYYY-MM-DD。

        Returns:
            "bull" 或 "bear"。
        """
        # 需要 ma_window 个交易日数据，按自然日 ×2 回看
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
            return "bull"

        df["close"] = pd.to_numeric(df["close"], errors="coerce")
        ma = df["close"].iloc[-self.ma_window:].mean()
        current_close = df["close"].iloc[-1]

        regime = "bull" if current_close >= ma else "bear"
        logger.info(
            f"Regime 检测: {self.index_code} close={current_close:.2f}, "
            f"MA{self.ma_window}={ma:.2f} → {regime}"
        )
        return regime
