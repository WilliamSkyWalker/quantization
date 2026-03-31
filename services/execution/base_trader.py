"""
交易执行器抽象基类

定义统一的交易接口，所有交易执行器（PaperTrader / QMTTrader / PtradeTrader）
都必须实现这些方法，方便后续在不同执行环境间切换。

切换方式：
    1. 在 .env 中设置 TRADER_TYPE=paper / qmt / ptrade
    2. main.py 通过工厂函数自动选择对应实现
    3. 策略层和风控层代码无需改动
"""

from abc import ABC, abstractmethod

import pandas as pd


class BaseTrader(ABC):
    """
    交易执行器统一接口。

    所有实现必须遵循以下约定：
        - connect() 成功后才能调用其他方法
        - get_current_positions() 返回固定列: [ts_code, volume, market_value, cost]
        - sync_position() 接收 DataFrame[ts_code, weight]，返回执行结果 dict
        - get_account_info() 返回固定键: {total_assets, available_cash, market_value, pnl}
    """

    @abstractmethod
    def connect(self, **kwargs):
        """
        连接/初始化交易账户。

        不同实现的参数不同：
            - PaperTrader: initial_capital（初始资金）
            - QMTTrader: account_id, password 等
        """

    @abstractmethod
    def get_account_info(self) -> dict:
        """
        获取账户信息。

        Returns:
            包含以下键的字典:
                - total_assets: 总资产
                - available_cash: 可用现金
                - market_value: 持仓市值
                - pnl: 累计盈亏
        """

    @abstractmethod
    def get_current_positions(self) -> pd.DataFrame:
        """
        获取当前持仓。

        Returns:
            DataFrame，包含以下列:
                - ts_code: 股票代码（如 "000001.SZ"）
                - volume: 持仓股数
                - market_value: 持仓市值
                - cost: 持仓成本价
        """

    @abstractmethod
    def sync_position(self, target_weights: pd.DataFrame, **kwargs) -> dict:
        """
        将持仓同步到目标权重。

        Args:
            target_weights: DataFrame，包含列 [ts_code, weight]。
                weight 为目标权重（0~1），所有 weight 之和应 <= 1。

        Returns:
            执行结果字典:
                - success: 成功下单数
                - failed: 失败下单数
                - skipped: 跳过数（权重变化过小）
        """

    @abstractmethod
    def order_target_percent(self, ts_code: str, target_percent: float) -> bool:
        """
        按目标比例下单。

        Args:
            ts_code: 股票代码。
            target_percent: 目标持仓占总资产的比例（0~1）。

        Returns:
            是否下单成功。
        """

    @abstractmethod
    def reconcile(self, target_weights: pd.DataFrame) -> pd.DataFrame:
        """
        对账：比较目标权重和实际持仓的差异。

        Args:
            target_weights: 目标权重 DataFrame[ts_code, weight]。

        Returns:
            差异 DataFrame[ts_code, target_weight, actual_weight, diff]。
        """

    @abstractmethod
    def get_position_report(self) -> str:
        """
        生成持仓报告文本。

        Returns:
            格式化的持仓报告字符串。
        """
