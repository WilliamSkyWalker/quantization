"""因子处理流水线测试。"""

import numpy as np
import pandas as pd
import pytest

from factors.processor import (
    winsorize_mad,
    neutralize,
    zscore,
    process_factor,
    process_all_factors,
)


class TestWinsorizeMAD:
    """MAD 去极值测试。"""

    def test_clips_outliers(self):
        """极端值被截断。"""
        s = pd.Series([1, 2, 3, 4, 5, 100])  # 100 是极端值
        result = winsorize_mad(s, n=3)
        assert result.max() < 100

    def test_preserves_normal_values(self):
        """正常值不受影响。"""
        s = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0])
        result = winsorize_mad(s, n=5)
        pd.testing.assert_series_equal(s, result)

    def test_handles_constant_series(self):
        """全部相同值（MAD=0）不报错。"""
        s = pd.Series([5.0, 5.0, 5.0, 5.0])
        result = winsorize_mad(s)
        pd.testing.assert_series_equal(s, result)

    def test_symmetric_clipping(self):
        """上下界对称截断。"""
        s = pd.Series([-100, 1, 2, 3, 4, 5, 100])
        result = winsorize_mad(s, n=3)
        # 上下界应对称于中位数
        median = s.median()
        assert abs((result.max() - median) - (median - result.min())) < 1e-6


class TestZScore:
    """Z-Score 标准化测试。"""

    def test_mean_zero(self):
        """标准化后均值接近 0。"""
        s = pd.Series([10.0, 20.0, 30.0, 40.0, 50.0])
        result = zscore(s)
        assert abs(result.mean()) < 1e-10

    def test_std_one(self):
        """标准化后标准差接近 1。"""
        s = pd.Series([10.0, 20.0, 30.0, 40.0, 50.0])
        result = zscore(s)
        assert abs(result.std() - 1.0) < 1e-10

    def test_constant_series_returns_zero(self):
        """常数序列标准化后全为 0。"""
        s = pd.Series([5.0, 5.0, 5.0])
        result = zscore(s)
        assert (result == 0).all()


class TestNeutralize:
    """行业市值中性化测试。"""

    def test_basic_neutralize(self):
        """中性化后残差与行业不相关。"""
        np.random.seed(42)
        n = 100
        df = pd.DataFrame({
            "factor_value": np.random.randn(n) + np.repeat([0, 1, 2, 3, 4], 20),
            "industry_name": np.repeat(["A", "B", "C", "D", "E"], 20),
            "ln_mktcap": np.random.uniform(10, 15, n),
        })
        result = neutralize(df)
        assert len(result) == n

        # 残差与行业的相关性应显著降低
        df["residual"] = result
        group_means = df.groupby("industry_name")["residual"].mean()
        assert group_means.abs().max() < 0.5  # 行业均值接近0

    def test_insufficient_samples_skip(self):
        """样本不足跳过中性化。"""
        df = pd.DataFrame({
            "factor_value": [1.0, 2.0],
            "industry_name": ["A", "B"],
            "ln_mktcap": [12.0, 13.0],
        })
        result = neutralize(df)
        assert len(result) == 2


class TestProcessFactor:
    """完整因子处理流水线测试。"""

    def test_full_pipeline(self):
        """去极值 → 标准化（不含中性化）。"""
        np.random.seed(42)
        df = pd.DataFrame({
            "ts_code": [f"code_{i}" for i in range(50)],
            "factor_value": np.random.randn(50),
        })
        # 加入极端值
        df.loc[0, "factor_value"] = 100.0

        result = process_factor(df, do_neutralize=False)

        assert len(result) == 50
        # 标准化后均值接近 0
        valid = result["factor_value"].dropna()
        assert abs(valid.mean()) < 0.1

    def test_pipeline_with_nan(self):
        """含 NaN 值不报错。"""
        df = pd.DataFrame({
            "ts_code": ["A", "B", "C", "D"],
            "factor_value": [1.0, np.nan, 3.0, 4.0],
        })
        result = process_factor(df, do_neutralize=False)
        assert result["factor_value"].notna().sum() == 3

    def test_pipeline_with_neutralize(self):
        """含中性化的完整流水线。"""
        np.random.seed(42)
        n = 50
        df = pd.DataFrame({
            "ts_code": [f"code_{i}" for i in range(n)],
            "factor_value": np.random.randn(n),
        })
        industry_df = pd.DataFrame({
            "ts_code": [f"code_{i}" for i in range(n)],
            "industry_name": np.repeat(["A", "B", "C", "D", "E"], 10),
        })
        mktcap_df = pd.DataFrame({
            "ts_code": [f"code_{i}" for i in range(n)],
            "total_mv": np.random.uniform(1e5, 1e7, n),
        })

        result = process_factor(df, industry_df, mktcap_df)
        assert len(result) == n


class TestProcessAllFactors:
    """批量因子处理测试。"""

    def test_processes_multiple(self):
        np.random.seed(42)
        codes = [f"code_{i}" for i in range(20)]
        factor_dict = {
            "F1": pd.DataFrame({"ts_code": codes, "factor_value": np.random.randn(20)}),
            "F2": pd.DataFrame({"ts_code": codes, "factor_value": np.random.randn(20)}),
        }
        result = process_all_factors(factor_dict)
        assert "F1" in result
        assert "F2" in result
        assert len(result["F1"]) == 20
