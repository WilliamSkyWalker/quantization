"""风控模块测试。"""

import numpy as np
import pandas as pd
import pytest

from risk.risk_manager import RiskManager


class TestCapSingleWeight:
    """个股权重上限测试。"""

    def test_caps_overweight(self, db_with_data):
        """超过 5% 的个股权重被截断。"""
        rm = RiskManager(db_with_data, max_single_weight=0.05)
        df = pd.DataFrame({
            "ts_code": ["A", "B", "C"],
            "weight": [0.8, 0.1, 0.1],
        })
        result = rm._cap_single_weight(df)
        assert result["weight"].max() <= 0.05 + 1e-6

    def test_preserves_normal_weights(self, db_with_data):
        """正常权重不受影响。"""
        rm = RiskManager(db_with_data, max_single_weight=0.5)
        df = pd.DataFrame({
            "ts_code": ["A", "B", "C"],
            "weight": [0.3, 0.3, 0.4],
        })
        result = rm._cap_single_weight(df)
        # 总权重不变
        assert abs(result["weight"].sum() - 1.0) < 0.01

    def test_equal_weight_under_cap(self, db_with_data):
        """等权且在上限以内不变。"""
        rm = RiskManager(db_with_data, max_single_weight=0.2)
        df = pd.DataFrame({
            "ts_code": [f"S{i}" for i in range(10)],
            "weight": [0.1] * 10,
        })
        result = rm._cap_single_weight(df)
        assert abs(result["weight"].sum() - 1.0) < 1e-6


class TestCapIndustryWeight:
    """行业暴露上限测试。"""

    def test_caps_industry_overweight(self, db_with_data):
        """银行行业超限被截断。"""
        rm = RiskManager(db_with_data, max_industry_weight=0.30)
        # 000001 和 600036 都是银行，共占 80%
        df = pd.DataFrame({
            "ts_code": ["000001.SZ", "600036.SH", "600519.SH"],
            "weight": [0.4, 0.4, 0.2],
        })
        result = rm._cap_industry_weight(df, "2024-06-28")
        # 银行权重应被截断
        bank_codes = ["000001.SZ", "600036.SH"]
        bank_weight = result[result["ts_code"].isin(bank_codes)]["weight"].sum()
        assert bank_weight <= 0.30 + 0.01


class TestCheckDrawdown:
    """最大回撤降仓测试。"""

    def test_no_drawdown(self, db_with_data):
        """无回撤时仓位系数为 1。"""
        rm = RiskManager(db_with_data, max_drawdown=0.15)
        nav = pd.Series([1.0, 1.01, 1.02, 1.03])
        assert rm.check_drawdown(nav) == 1.0

    def test_triggers_drawdown(self, db_with_data):
        """回撤超阈值时降仓。"""
        rm = RiskManager(db_with_data, max_drawdown=0.15, drawdown_position=0.5)
        nav = pd.Series([1.0, 1.1, 1.2, 0.9])  # 从1.2回撤到0.9，回撤25%
        scale = rm.check_drawdown(nav)
        assert scale == 0.5

    def test_mild_drawdown_no_trigger(self, db_with_data):
        """轻度回撤不触发。"""
        rm = RiskManager(db_with_data, max_drawdown=0.15)
        nav = pd.Series([1.0, 1.1, 1.05])  # 回撤约4.5%
        assert rm.check_drawdown(nav) == 1.0

    def test_empty_nav(self, db_with_data):
        rm = RiskManager(db_with_data)
        assert rm.check_drawdown(pd.Series(dtype=float)) == 1.0


class TestAdjustWeights:
    """完整风控调整测试。"""

    def test_full_adjustment(self, db_with_data, sample_weights):
        """完整风控流程不报错。"""
        rm = RiskManager(db_with_data, min_turnover=0)
        result = rm.adjust_weights(sample_weights, "2024-06-28")
        assert not result.empty
        assert abs(result["weight"].sum() - 1.0) < 0.01

    def test_empty_input(self, db_with_data):
        rm = RiskManager(db_with_data)
        result = rm.adjust_weights(pd.DataFrame(columns=["ts_code", "weight"]), "2024-06-28")
        assert result.empty

    def test_with_drawdown_nav(self, db_with_data, sample_weights):
        """带回撤降仓的风控。"""
        rm = RiskManager(db_with_data, max_drawdown=0.15, drawdown_position=0.5,
                         min_turnover=0)
        # 制造大回撤净值
        nav = pd.Series([1.0, 1.1, 1.2, 0.8])
        result = rm.adjust_weights(sample_weights, "2024-06-28", nav_series=nav)
        # 权重总和应低于 1（降仓后）
        assert result["weight"].sum() < 0.6


class TestRiskReport:
    """风控报告测试。"""

    def test_risk_report(self, db_with_data, sample_weights):
        rm = RiskManager(db_with_data)
        report = rm.risk_report(sample_weights, "2024-06-28")
        assert "持仓数量" in report
        assert report["持仓数量"] == len(sample_weights)
