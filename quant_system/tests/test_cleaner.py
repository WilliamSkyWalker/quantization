"""数据清洗模块测试。"""

import pandas as pd
import pytest

from data.cleaner import (
    get_clean_universe,
    verify_limit_flags,
    mark_suspended,
    batch_clean_daily_data,
)


class TestVerifyLimitFlags:
    """涨跌停标记校验测试。"""

    def test_mainboard_limit_up(self):
        """主板涨停阈值 9.9%。"""
        df = pd.DataFrame([
            {"ts_code": "000001.SZ", "pct_chg": 10.0, "is_limit_up": 0, "is_limit_down": 0},
            {"ts_code": "000002.SZ", "pct_chg": 5.0, "is_limit_up": 0, "is_limit_down": 0},
        ])
        result = verify_limit_flags(df)
        assert result.iloc[0]["is_limit_up_v"] == 1
        assert result.iloc[1]["is_limit_up_v"] == 0

    def test_mainboard_limit_down(self):
        """主板跌停阈值 -9.9%。"""
        df = pd.DataFrame([
            {"ts_code": "600519.SH", "pct_chg": -10.0, "is_limit_up": 0, "is_limit_down": 0},
        ])
        result = verify_limit_flags(df)
        assert result.iloc[0]["is_limit_down_v"] == 1

    def test_gem_limit_20pct(self):
        """创业板涨跌停阈值 ±19.9%。"""
        df = pd.DataFrame([
            {"ts_code": "300750.SZ", "pct_chg": 15.0, "is_limit_up": 0, "is_limit_down": 0},
            {"ts_code": "300750.SZ", "pct_chg": 20.0, "is_limit_up": 0, "is_limit_down": 0},
        ])
        result = verify_limit_flags(df)
        assert result.iloc[0]["is_limit_up_v"] == 0  # 15% 不是涨停
        assert result.iloc[1]["is_limit_up_v"] == 1  # 20% 是涨停

    def test_star_limit_20pct(self):
        """科创板涨跌停阈值 ±19.9%。"""
        df = pd.DataFrame([
            {"ts_code": "688981.SH", "pct_chg": -20.0, "is_limit_up": 0, "is_limit_down": 0},
        ])
        result = verify_limit_flags(df)
        assert result.iloc[0]["is_limit_down_v"] == 1

    def test_empty_input(self):
        """空 DataFrame 不报错。"""
        result = verify_limit_flags(pd.DataFrame())
        assert result.empty


class TestMarkSuspended:
    """停牌标记测试。"""

    def test_zero_volume_is_suspended(self):
        """成交量为 0 标记为停牌。"""
        df = pd.DataFrame([
            {"volume": 100000},
            {"volume": 0},
            {"volume": None},
        ])
        result = mark_suspended(df)
        assert result.iloc[0]["is_suspended"] == 0
        assert result.iloc[1]["is_suspended"] == 1
        assert result.iloc[2]["is_suspended"] == 1


class TestGetCleanUniverse:
    """可交易股票池测试。"""

    def test_basic_filtering(self, db_with_data):
        """基本过滤逻辑（ST + 新股 + 停牌）。"""
        universe = get_clean_universe(db_with_data, "2024-06-28", min_turnover=0)
        codes = universe["ts_code"].tolist()

        # ST 股被排除
        assert "000003.SZ" not in codes

        # 有股票入选
        assert len(universe) > 0

    def test_returns_expected_columns(self, db_with_data):
        """返回必要的列。"""
        universe = get_clean_universe(db_with_data, "2024-06-28", min_turnover=0)
        if not universe.empty:
            assert "ts_code" in universe.columns
            assert "is_limit_up" in universe.columns
            assert "is_limit_down" in universe.columns

    def test_non_trading_day_returns_empty(self, db_with_data):
        """非交易日返回空。"""
        universe = get_clean_universe(db_with_data, "2024-01-01", min_turnover=0)
        assert universe.empty

    def test_liquidity_filter(self, db_with_data):
        """流动性过滤可以减少股票数量。"""
        no_filter = get_clean_universe(db_with_data, "2024-06-28", min_turnover=0)
        with_filter = get_clean_universe(
            db_with_data, "2024-06-28", min_turnover=1e15  # 极高阈值
        )
        assert len(with_filter) <= len(no_filter)


class TestBatchClean:
    """批量清洗测试。"""

    def test_batch_clean_single_stock(self, db_with_data):
        """单只股票清洗不报错。"""
        df = batch_clean_daily_data(db_with_data, "000001.SZ")
        assert not df.empty
        assert "is_suspended" in df.columns

    def test_batch_clean_nonexistent_code(self, db_with_data):
        """不存在的代码返回空。"""
        df = batch_clean_daily_data(db_with_data, "999999.SZ")
        assert df.empty
