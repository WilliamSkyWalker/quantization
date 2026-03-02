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

from backend.services.config import LOG_LEVEL
from backend.services.data.database import DatabaseManager

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

    @staticmethod
    def _build_in_clause(codes: list[str], prefix: str = "code") -> tuple[str, dict]:
        """
        构建 IN 子句的参数化占位符。

        SQLAlchemy text() 不支持直接传递列表参数，
        因此需要为每个元素生成独立的命名占位符。

        Args:
            codes: 值列表。
            prefix: 占位符前缀。

        Returns:
            (sql_fragment, params_dict)
            例如 codes=['000001.SZ','000002.SZ'] 返回
            ("(:code_0, :code_1)", {'code_0': '000001.SZ', 'code_1': '000002.SZ'})
        """
        placeholders = []
        params = {}
        for i, code in enumerate(codes):
            key = f"{prefix}_{i}"
            placeholders.append(f":{key}")
            params[key] = code
        sql_fragment = f"({', '.join(placeholders)})"
        return sql_fragment, params

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
        params: dict = {"date": date}

        inner_sql = (
            f"SELECT {cols_str}, ROW_NUMBER() OVER "
            f"(PARTITION BY ts_code ORDER BY end_date DESC) as rn "
            f"FROM financial_data WHERE ann_date <= :date"
        )

        if universe_codes:
            in_clause, in_params = self._build_in_clause(universe_codes)
            inner_sql += f" AND ts_code IN {in_clause}"
            params.update(in_params)

        sql = f"SELECT * FROM ({inner_sql}) t WHERE rn = 1"

        df = self.db.query(sql, params=params)

        if df.empty:
            return df

        # 移除辅助列
        df = df.drop(columns=["rn"], errors="ignore")

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

        params: dict = {"start_date": start_date, "end_date": end_date}

        sql = (
            f"SELECT {cols_str} FROM daily_price "
            f"WHERE trade_date >= :start_date "
            f"AND trade_date <= :end_date"
        )

        if universe_codes:
            in_clause, in_params = self._build_in_clause(universe_codes)
            sql += f" AND ts_code IN {in_clause}"
            params.update(in_params)

        sql += " ORDER BY ts_code, trade_date"

        return self.db.query(sql, params=params)

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

        params: dict = {"month_start": month_start, "month_end": month_end}

        inner_sql = (
            "SELECT ts_code, trade_date, close, "
            "ROW_NUMBER() OVER (PARTITION BY ts_code ORDER BY trade_date DESC) as rn "
            "FROM daily_price "
            "WHERE trade_date >= :month_start "
            "AND trade_date <= :month_end"
        )

        if universe_codes:
            in_clause, in_params = self._build_in_clause(universe_codes)
            inner_sql += f" AND ts_code IN {in_clause}"
            params.update(in_params)

        sql = f"SELECT * FROM ({inner_sql}) t WHERE rn = 1"

        df = self.db.query(sql, params=params)
        if df.empty:
            return df

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
        params: dict = {"date": date}
        sql = (
            "SELECT ts_code, ann_date, end_date, net_profit FROM financial_data "
            "WHERE ann_date <= :date AND net_profit IS NOT NULL"
        )
        if universe_codes:
            in_clause, in_params = self._build_in_clause(universe_codes)
            sql += f" AND ts_code IN {in_clause}"
            params.update(in_params)
        sql += " ORDER BY ts_code, end_date DESC"

        df = self.db.query(sql, params=params)
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

    def get_ttm_revenue(
        self,
        date: str,
        universe_codes: Optional[list[str]] = None,
    ) -> pd.DataFrame:
        """
        计算截止到指定日期的 TTM 营收（滚动四季度）。

        计算逻辑（revenue 是季度累计值）：
            - 年报期（12月）: TTM = 当期营收
            - 其他期: TTM = 当期累计 + 上年年报 - 上年同期累计
        严格遵守 ann_date <= date 防止未来函数。

        Args:
            date: 截止日期，格式 YYYY-MM-DD。
            universe_codes: 股票代码列表（可选）。

        Returns:
            DataFrame，包含 ts_code 和 ttm_revenue 列。
        """
        params: dict = {"date": date}
        sql = (
            "SELECT ts_code, ann_date, end_date, revenue FROM financial_data "
            "WHERE ann_date <= :date AND revenue IS NOT NULL"
        )
        if universe_codes:
            in_clause, in_params = self._build_in_clause(universe_codes)
            sql += f" AND ts_code IN {in_clause}"
            params.update(in_params)
        sql += " ORDER BY ts_code, end_date DESC"

        df = self.db.query(sql, params=params)
        if df.empty:
            return pd.DataFrame(columns=["ts_code", "ttm_revenue"])

        df["end_date"] = pd.to_datetime(df["end_date"])
        results = []

        for ts_code, grp in df.groupby("ts_code"):
            grp = grp.sort_values("end_date", ascending=False)
            latest = grp.iloc[0]
            end_dt = latest["end_date"]
            month = end_dt.month

            if month == 12:
                results.append({"ts_code": ts_code, "ttm_revenue": latest["revenue"]})
            else:
                prev_year = end_dt.year - 1
                prev_annual_end = pd.Timestamp(year=prev_year, month=12, day=31)
                prev_same_end = pd.Timestamp(year=prev_year, month=month, day=end_dt.day)

                prev_annual = grp[grp["end_date"] == prev_annual_end]
                prev_same = grp[grp["end_date"] == prev_same_end]

                if not prev_annual.empty and not prev_same.empty:
                    ttm = (latest["revenue"]
                           + prev_annual.iloc[0]["revenue"]
                           - prev_same.iloc[0]["revenue"])
                    results.append({"ts_code": ts_code, "ttm_revenue": ttm})
                else:
                    results.append({"ts_code": ts_code, "ttm_revenue": float("nan")})

        return pd.DataFrame(results) if results else pd.DataFrame(columns=["ts_code", "ttm_revenue"])

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

        params: dict = {"lookback": lookback, "date": date}

        inner_sql = (
            "SELECT ts_code, trade_date, close, "
            "ROW_NUMBER() OVER (PARTITION BY ts_code ORDER BY trade_date DESC) as rn "
            "FROM daily_price "
            "WHERE trade_date >= :lookback AND trade_date <= :date"
        )
        if universe_codes:
            in_clause, in_params = self._build_in_clause(universe_codes)
            inner_sql += f" AND ts_code IN {in_clause}"
            params.update(in_params)

        sql = f"SELECT * FROM ({inner_sql}) t WHERE rn = 1"

        df = self.db.query(sql, params=params)
        if df.empty:
            return pd.DataFrame(columns=["ts_code", "close"])

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
        params: dict = {}
        sql = "SELECT ts_code, total_share FROM stock_basic WHERE total_share IS NOT NULL"
        if universe_codes:
            in_clause, in_params = self._build_in_clause(universe_codes)
            sql += f" AND ts_code IN {in_clause}"
            params.update(in_params)

        return self.db.query(sql, params=params)

    def __repr__(self) -> str:
        return f"<Factor: {self.name}>"
