"""
因子基类

定义因子计算的统一接口和通用工具方法。
所有具体因子类继承 FactorBase，实现 compute() 方法。

设计原则：
    - 截面计算：同一时间截面对所有股票计算因子值
    - 防止未来数据：财务数据按公告日期取值，价格数据只用截止到计算日的历史
    - 统一输出格式：DataFrame[ts_code, factor_value]
"""

import logging
from abc import ABC, abstractmethod
from typing import Optional

import pandas as pd

from config.settings import LOG_LEVEL
from data.database import DatabaseManager

logger = logging.getLogger(__name__)
logger.setLevel(LOG_LEVEL)


class FactorBase(ABC):
    """
    因子计算基类。

    所有因子类必须继承此基类并实现 compute() 方法。

    用法:
        class MyFactor(FactorBase):
            name = "my_factor"
            def compute(self, date, universe):
                ...
                return df[["ts_code", "factor_value"]]

        factor = MyFactor(db)
        result = factor.compute("2024-12-31", universe_df)
    """

    # 子类必须覆盖
    name: str = "base"
    description: str = ""

    def __init__(self, db: DatabaseManager):
        """
        Args:
            db: DatabaseManager 实例。
        """
        self.db = db

    @abstractmethod
    def compute(self, date: str, universe: pd.DataFrame) -> pd.DataFrame:
        """
        计算因子值（截面计算）。

        Args:
            date: 计算日期，格式 YYYY-MM-DD。
            universe: 股票池 DataFrame，至少包含 ts_code 列。

        Returns:
            DataFrame，包含 ts_code 和 factor_value 两列。
            factor_value 为 NaN 表示该股票该因子值缺失。
        """
        raise NotImplementedError

    # ----------------------------------------------------------
    # 通用数据获取工具
    # ----------------------------------------------------------

    def get_latest_financial(
        self,
        date: str,
        columns: list[str],
        universe_codes: Optional[list[str]] = None,
    ) -> pd.DataFrame:
        """
        获取截止到指定日期的最新财务数据（按公告日期，防止未来函数）。

        对每只股票，取 ann_date <= date 的最近一条记录。

        Args:
            date: 截止日期，格式 YYYY-MM-DD。
            columns: 需要的财务字段列表（如 ["pe_ttm", "pb"]）。
            universe_codes: 股票代码列表（可选，限定范围）。

        Returns:
            DataFrame，包含 ts_code 和请求的列。
        """
        cols_str = ", ".join(["ts_code", "ann_date", "end_date"] + columns)

        sql = (
            f"SELECT {cols_str} FROM financial_data "
            f"WHERE ann_date <= '{date}'"
        )

        if universe_codes:
            codes_str = "','".join(universe_codes)
            sql += f" AND ts_code IN ('{codes_str}')"

        sql += " ORDER BY ts_code, end_date DESC"

        df = self.db.query(sql)

        if df.empty:
            return df

        # 每只股票只保留最新一条
        df = df.drop_duplicates(subset=["ts_code"], keep="first")

        return df

    def get_price_history(
        self,
        end_date: str,
        lookback_days: int,
        universe_codes: Optional[list[str]] = None,
        columns: Optional[list[str]] = None,
    ) -> pd.DataFrame:
        """
        获取截止到指定日期的历史行情数据。

        Args:
            end_date: 截止日期，格式 YYYY-MM-DD。
            lookback_days: 向前回看的自然日天数。
            universe_codes: 股票代码列表（可选）。
            columns: 需要的行情字段（默认全部）。

        Returns:
            日线行情 DataFrame。
        """
        start_date = (
            pd.to_datetime(end_date) - pd.Timedelta(days=lookback_days)
        ).strftime("%Y-%m-%d")

        if columns:
            cols_str = ", ".join(["ts_code", "trade_date"] + columns)
        else:
            cols_str = "*"

        sql = (
            f"SELECT {cols_str} FROM daily_price "
            f"WHERE trade_date >= '{start_date}' "
            f"AND trade_date <= '{end_date}'"
        )

        if universe_codes:
            codes_str = "','".join(universe_codes)
            sql += f" AND ts_code IN ('{codes_str}')"

        sql += " ORDER BY ts_code, trade_date"

        return self.db.query(sql)

    def get_month_end_price(
        self,
        date: str,
        months_ago: int,
        universe_codes: Optional[list[str]] = None,
    ) -> pd.DataFrame:
        """
        获取 N 个月前月末的收盘价。

        Args:
            date: 基准日期。
            months_ago: 向前推 N 个月。
            universe_codes: 股票代码列表（可选）。

        Returns:
            DataFrame，包含 ts_code 和 close 列。
        """
        target_date = pd.to_datetime(date) - pd.DateOffset(months=months_ago)
        # 取目标月份的最后一个交易日
        month_start = target_date.replace(day=1).strftime("%Y-%m-%d")
        month_end = (
            target_date.replace(day=1) + pd.DateOffset(months=1) - pd.Timedelta(days=1)
        ).strftime("%Y-%m-%d")

        sql = (
            f"SELECT ts_code, trade_date, close FROM daily_price "
            f"WHERE trade_date >= '{month_start}' "
            f"AND trade_date <= '{month_end}'"
        )

        if universe_codes:
            codes_str = "','".join(universe_codes)
            sql += f" AND ts_code IN ('{codes_str}')"

        sql += " ORDER BY ts_code, trade_date DESC"

        df = self.db.query(sql)
        if df.empty:
            return df

        # 每只股票取月内最后一个交易日
        df = df.drop_duplicates(subset=["ts_code"], keep="first")
        return df[["ts_code", "close"]]

    def get_ttm_net_profit(
        self,
        date: str,
        universe_codes: Optional[list[str]] = None,
    ) -> pd.DataFrame:
        """
        计算截止到指定日期的 TTM 净利润（滚动四季度）。

        计算逻辑（net_profit 是季度累计值）：
            - 年报期（12月）: TTM = 当期净利润
            - 其他期: TTM = 当期累计 + 上年年报 - 上年同期累计
        严格遵守 ann_date <= date 防止未来函数。

        Args:
            date: 截止日期，格式 YYYY-MM-DD。
            universe_codes: 股票代码列表（可选）。

        Returns:
            DataFrame，包含 ts_code 和 ttm_net_profit 列。
        """
        sql = (
            "SELECT ts_code, ann_date, end_date, net_profit FROM financial_data "
            f"WHERE ann_date <= '{date}' AND net_profit IS NOT NULL"
        )
        if universe_codes:
            codes_str = "','".join(universe_codes)
            sql += f" AND ts_code IN ('{codes_str}')"
        sql += " ORDER BY ts_code, end_date DESC"

        df = self.db.query(sql)
        if df.empty:
            return pd.DataFrame(columns=["ts_code", "ttm_net_profit"])

        df["end_date"] = pd.to_datetime(df["end_date"])
        results = []

        for ts_code, grp in df.groupby("ts_code"):
            grp = grp.sort_values("end_date", ascending=False)
            # 取最新一期
            latest = grp.iloc[0]
            end_dt = latest["end_date"]
            month = end_dt.month

            if month == 12:
                # 年报：TTM = 当期净利润
                results.append({"ts_code": ts_code, "ttm_net_profit": latest["net_profit"]})
            else:
                # 需要上年年报和上年同期累计
                prev_year = end_dt.year - 1
                prev_annual_end = pd.Timestamp(year=prev_year, month=12, day=31)
                prev_same_end = pd.Timestamp(year=prev_year, month=month, day=end_dt.day)

                prev_annual = grp[grp["end_date"] == prev_annual_end]
                prev_same = grp[grp["end_date"] == prev_same_end]

                if not prev_annual.empty and not prev_same.empty:
                    ttm = (latest["net_profit"]
                           + prev_annual.iloc[0]["net_profit"]
                           - prev_same.iloc[0]["net_profit"])
                    results.append({"ts_code": ts_code, "ttm_net_profit": ttm})
                else:
                    results.append({"ts_code": ts_code, "ttm_net_profit": float("nan")})

        return pd.DataFrame(results) if results else pd.DataFrame(columns=["ts_code", "ttm_net_profit"])

    def get_close_on_date(
        self,
        date: str,
        universe_codes: Optional[list[str]] = None,
    ) -> pd.DataFrame:
        """
        获取指定日期的收盘价。如果当日无数据，取之前最近一个交易日。

        Args:
            date: 日期，格式 YYYY-MM-DD。
            universe_codes: 股票代码列表（可选）。

        Returns:
            DataFrame，包含 ts_code 和 close 列。
        """
        # 向前查找最多 10 个自然日
        lookback = (pd.to_datetime(date) - pd.Timedelta(days=10)).strftime("%Y-%m-%d")

        sql = (
            "SELECT ts_code, trade_date, close FROM daily_price "
            f"WHERE trade_date >= '{lookback}' AND trade_date <= '{date}'"
        )
        if universe_codes:
            codes_str = "','".join(universe_codes)
            sql += f" AND ts_code IN ('{codes_str}')"
        sql += " ORDER BY ts_code, trade_date DESC"

        df = self.db.query(sql)
        if df.empty:
            return pd.DataFrame(columns=["ts_code", "close"])

        # 每只股票取最近一个交易日
        df = df.drop_duplicates(subset=["ts_code"], keep="first")
        return df[["ts_code", "close"]]

    def get_total_share(
        self,
        universe_codes: Optional[list[str]] = None,
    ) -> pd.DataFrame:
        """
        从 stock_basic 获取总股本（万股）。

        Args:
            universe_codes: 股票代码列表（可选）。

        Returns:
            DataFrame，包含 ts_code 和 total_share 列。
        """
        sql = "SELECT ts_code, total_share FROM stock_basic WHERE total_share IS NOT NULL"
        if universe_codes:
            codes_str = "','".join(universe_codes)
            sql += f" AND ts_code IN ('{codes_str}')"

        return self.db.query(sql)

    def __repr__(self) -> str:
        return f"<Factor: {self.name}>"
