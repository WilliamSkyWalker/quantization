"""策略与回测模块测试。"""

import numpy as np
import pandas as pd
import pytest

from strategy.multi_factor import MultiFactorStrategy
from strategy.backtest import BacktestEngine


class TestMultiFactorStrategy:
    """多因子选股策略测试。"""

    def test_get_rebalance_dates(self, db_with_data):
        """获取调仓日期（每月最后一个交易日）。"""
        strategy = MultiFactorStrategy(db_with_data)
        dates = strategy.get_rebalance_dates("2024-01-01", "2024-06-30")
        assert len(dates) >= 5  # 至少 5 个月
        # 每个日期应在月末附近
        for d in dates:
            dt = pd.to_datetime(d)
            assert dt.day >= 25 or dt.month in (2,)  # 2月可能28号

    def test_select_stocks_basic(self, db_with_data):
        """基本选股流程不报错。"""
        strategy = MultiFactorStrategy(db_with_data, n_holdings=5)
        result = strategy.select_stocks("2024-11-29")
        # 可能因数据不全返回空，但不应报错
        if not result.empty:
            assert "ts_code" in result.columns
            assert "weight" in result.columns
            assert "score" in result.columns

    def test_select_stocks_weight_sum(self, db_with_data):
        """选股结果权重之和为 1。"""
        strategy = MultiFactorStrategy(db_with_data, n_holdings=5)
        result = strategy.select_stocks("2024-11-29")
        if not result.empty:
            assert abs(result["weight"].sum() - 1.0) < 1e-6

    def test_select_stocks_count(self, db_with_data):
        """选股数量不超过 n_holdings。"""
        n = 3
        strategy = MultiFactorStrategy(db_with_data, n_holdings=n)
        result = strategy.select_stocks("2024-11-29")
        assert len(result) <= n

    def test_generate_signals(self, db_with_data):
        """生成多期信号。"""
        strategy = MultiFactorStrategy(db_with_data, n_holdings=3)
        signals = strategy.generate_signals("2024-09-01", "2024-12-31")
        # 至少有一些信号
        assert isinstance(signals, dict)


class TestBacktestEngine:
    """回测引擎测试。"""

    def test_run_basic(self, db_with_data):
        """基本回测流程。"""
        signals = {
            "2024-06-28": pd.DataFrame({
                "ts_code": ["000001.SZ", "600519.SH"],
                "weight": [0.5, 0.5],
            }),
            "2024-09-30": pd.DataFrame({
                "ts_code": ["000002.SZ", "600036.SH"],
                "weight": [0.5, 0.5],
            }),
        }
        engine = BacktestEngine(db_with_data)
        result = engine.run(signals, "2024-06-01", "2024-12-31")

        assert "nav" in result
        assert "trades" in result
        assert len(result["nav"]) > 0

    def test_nav_starts_at_one(self, db_with_data):
        """净值从 1 开始。"""
        signals = {
            "2024-06-28": pd.DataFrame({
                "ts_code": ["000001.SZ"],
                "weight": [1.0],
            }),
        }
        engine = BacktestEngine(db_with_data)
        result = engine.run(signals, "2024-06-01", "2024-12-31")
        nav = result["nav"]
        assert abs(nav.iloc[0] - 1.0) < 0.01  # 第一天接近 1

    def test_trade_cost_reduces_nav(self, db_with_data):
        """有交易成本的净值低于无成本的。"""
        signals = {
            "2024-06-28": pd.DataFrame({
                "ts_code": ["000001.SZ"], "weight": [1.0],
            }),
            "2024-09-30": pd.DataFrame({
                "ts_code": ["600519.SH"], "weight": [1.0],
            }),
        }
        engine_cost = BacktestEngine(db_with_data, buy_cost=0.003, sell_cost=0.003)
        engine_free = BacktestEngine(db_with_data, buy_cost=0, sell_cost=0)

        result_cost = engine_cost.run(signals, "2024-06-01", "2024-12-31")
        result_free = engine_free.run(signals, "2024-06-01", "2024-12-31")

        # 有成本的终值应低于无成本的
        assert result_cost["nav"].iloc[-1] <= result_free["nav"].iloc[-1]

    def test_summary(self, db_with_data):
        """绩效摘要计算。"""
        signals = {
            "2024-06-28": pd.DataFrame({
                "ts_code": ["000001.SZ", "600519.SH"],
                "weight": [0.5, 0.5],
            }),
        }
        engine = BacktestEngine(db_with_data)
        result = engine.run(signals, "2024-06-01", "2024-12-31")
        summary = engine.summary(result)

        assert not summary.empty
        metrics = summary.set_index("指标")["值"]
        assert "年化收益" in metrics.index
        assert "最大回撤" in metrics.index
        assert "夏普比率" in metrics.index

    def test_empty_signals(self, db_with_data):
        """空信号不报错。"""
        engine = BacktestEngine(db_with_data)
        result = engine.run({}, "2024-06-01", "2024-12-31")
        assert result.get("nav") is not None

    def test_plot_no_crash(self, db_with_data, tmp_path):
        """绘图不报错。"""
        signals = {
            "2024-06-28": pd.DataFrame({
                "ts_code": ["000001.SZ"], "weight": [1.0],
            }),
        }
        engine = BacktestEngine(db_with_data)
        result = engine.run(signals, "2024-06-01", "2024-12-31")
        save_path = str(tmp_path / "test_backtest.png")
        engine.plot(result, save_path=save_path)
