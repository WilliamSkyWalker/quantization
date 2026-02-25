"""因子评估模块测试。"""

import numpy as np
import pandas as pd
import pytest

from factors.evaluation import (
    calc_ic_series,
    calc_ic_summary,
    calc_quantile_returns,
    calc_cumulative_quantile_returns,
    evaluate_factor,
)


@pytest.fixture
def factor_and_return_data():
    """生成有预测力的因子数据和收益率数据。"""
    np.random.seed(42)
    dates = pd.date_range("2024-01-31", periods=12, freq="ME")
    records_factor = []
    records_return = []

    for dt in dates:
        n_stocks = 50
        codes = [f"S{i:04d}" for i in range(n_stocks)]
        factor_vals = np.random.randn(n_stocks)
        # 收益率与因子值正相关（加噪声）
        forward_rets = factor_vals * 0.02 + np.random.randn(n_stocks) * 0.05

        for i, code in enumerate(codes):
            records_factor.append({
                "date": dt, "ts_code": code, "factor_value": factor_vals[i],
            })
            records_return.append({
                "date": dt, "ts_code": code, "forward_return": forward_rets[i],
            })

    return pd.DataFrame(records_factor), pd.DataFrame(records_return)


@pytest.fixture
def random_factor_data():
    """生成无预测力的随机因子数据。"""
    np.random.seed(99)
    dates = pd.date_range("2024-01-31", periods=12, freq="ME")
    records_factor = []
    records_return = []

    for dt in dates:
        n_stocks = 50
        codes = [f"S{i:04d}" for i in range(n_stocks)]
        for i, code in enumerate(codes):
            records_factor.append({
                "date": dt, "ts_code": code,
                "factor_value": np.random.randn(),
            })
            records_return.append({
                "date": dt, "ts_code": code,
                "forward_return": np.random.randn() * 0.05,
            })

    return pd.DataFrame(records_factor), pd.DataFrame(records_return)


class TestICCalculation:
    """IC 计算测试。"""

    def test_ic_series_length(self, factor_and_return_data):
        """IC 序列长度 = 日期数。"""
        factor_df, return_df = factor_and_return_data
        ic = calc_ic_series(factor_df, return_df)
        assert len(ic) == 12

    def test_predictive_factor_positive_ic(self, factor_and_return_data):
        """有预测力的因子 IC 均值应为正。"""
        factor_df, return_df = factor_and_return_data
        ic = calc_ic_series(factor_df, return_df)
        assert ic["ic"].mean() > 0

    def test_random_factor_low_ic(self, random_factor_data):
        """随机因子的 IC 接近 0。"""
        factor_df, return_df = random_factor_data
        ic = calc_ic_series(factor_df, return_df)
        assert abs(ic["ic"].mean()) < 0.15

    def test_empty_input(self):
        """空输入返回空。"""
        empty = pd.DataFrame(columns=["date", "ts_code", "factor_value"])
        empty_ret = pd.DataFrame(columns=["date", "ts_code", "forward_return"])
        ic = calc_ic_series(empty, empty_ret)
        assert ic.empty


class TestICSummary:
    """IC 摘要统计测试。"""

    def test_summary_keys(self, factor_and_return_data):
        factor_df, return_df = factor_and_return_data
        ic = calc_ic_series(factor_df, return_df)
        summary = calc_ic_summary(ic)

        assert "ic_mean" in summary
        assert "ic_std" in summary
        assert "icir" in summary
        assert "ic_positive_rate" in summary
        assert "num_periods" in summary

    def test_icir_formula(self, factor_and_return_data):
        """ICIR = IC_mean / IC_std。"""
        factor_df, return_df = factor_and_return_data
        ic = calc_ic_series(factor_df, return_df)
        summary = calc_ic_summary(ic)

        expected_icir = summary["ic_mean"] / summary["ic_std"]
        assert abs(summary["icir"] - expected_icir) < 0.01

    def test_empty_ic(self):
        summary = calc_ic_summary(pd.DataFrame(columns=["date", "ic"]))
        assert summary["num_periods"] == 0


class TestQuantileReturns:
    """因子分层回测测试。"""

    def test_quantile_groups(self, factor_and_return_data):
        """分 5 组。"""
        factor_df, return_df = factor_and_return_data
        qr = calc_quantile_returns(factor_df, return_df, n_groups=5)
        assert "Q1" in qr.columns
        assert "Q5" in qr.columns
        assert len(qr) > 0

    def test_monotonic_returns(self, factor_and_return_data):
        """有预测力的因子：高分组收益 > 低分组。"""
        factor_df, return_df = factor_and_return_data
        qr = calc_quantile_returns(factor_df, return_df, n_groups=5)
        mean_returns = qr.mean()
        # Q5（高因子值组）的平均收益应高于 Q1（低因子值组）
        assert mean_returns["Q5"] > mean_returns["Q1"]

    def test_cumulative_returns(self, factor_and_return_data):
        factor_df, return_df = factor_and_return_data
        qr = calc_quantile_returns(factor_df, return_df, n_groups=5)
        cum = calc_cumulative_quantile_returns(qr)
        # 累计收益从 1 附近开始
        assert all(abs(cum.iloc[0] - (1 + qr.iloc[0])) < 0.01)


class TestEvaluateFactor:
    """综合评估测试。"""

    def test_evaluate(self, factor_and_return_data, tmp_path):
        """完整评估流程。"""
        factor_df, return_df = factor_and_return_data
        result = evaluate_factor(
            "TestFactor", factor_df, return_df,
            n_groups=5, plot=False,
        )
        assert "factor_name" in result
        assert result["factor_name"] == "TestFactor"
        assert "ic_mean" in result
        assert "icir" in result
        assert "group_annual_returns" in result
