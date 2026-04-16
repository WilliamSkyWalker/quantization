"""
交易执行器抽象基类

定义统一的交易接口，所有交易执行器（PaperTrader / QMTTrader / GMTrader / AlpacaTrader）
都必须实现这些方法。
"""

from abc import ABC, abstractmethod

import pandas as pd


class BaseTrader(ABC):
    """交易执行器统一接口。"""

    @abstractmethod
    def connect(self, **kwargs):
        """连接/初始化交易账户。"""

    @abstractmethod
    def get_account_info(self) -> dict:
        """
        获取账户信息。

        Returns:
            {total_assets, available_cash, market_value, pnl}
        """

    @abstractmethod
    def get_current_positions(self) -> pd.DataFrame:
        """DataFrame[ts_code, volume, market_value, cost]。"""

    @abstractmethod
    def sync_position(self, target_weights: pd.DataFrame, **kwargs) -> dict:
        """
        把持仓同步到目标权重。

        Args:
            target_weights: DataFrame[ts_code, weight]。

        Returns:
            {success, failed, skipped}
        """

    @abstractmethod
    def order_target_percent(self, ts_code: str, target_percent: float) -> bool:
        """按目标比例下单。"""

    @abstractmethod
    def reconcile(self, target_weights: pd.DataFrame) -> pd.DataFrame:
        """对账：目标权重 vs 实际持仓。"""

    @abstractmethod
    def get_position_report(self) -> str:
        """格式化持仓报告。"""
