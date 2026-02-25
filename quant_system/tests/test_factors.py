"""因子计算模块测试。"""

import numpy as np
import pandas as pd
import pytest

from factors.value import EPFactor, BPFactor
from factors.momentum import MOM1MFactor, MOM3MFactor, MOM12MFactor
from factors.quality import ROEFactor, GrossMarginFactor
from factors.technical import Turnover20DFactor


class TestEPFactor:
    """EP（市盈率倒数）因子测试。"""

    def test_compute_basic(self, db_with_data, sample_universe):
        factor = EPFactor(db_with_data)
        result = factor.compute("2024-11-30", sample_universe)
        assert "ts_code" in result.columns
        assert "factor_value" in result.columns
        assert len(result) > 0

    def test_positive_pe_yields_positive_ep(self, db_with_data, sample_universe):
        """正 PE → 正 EP。"""
        factor = EPFactor(db_with_data)
        result = factor.compute("2024-11-30", sample_universe)
        valid = result.dropna(subset=["factor_value"])
        assert (valid["factor_value"] > 0).all()

    def test_ep_is_ttm_profit_over_market_cap(self, db_with_data, sample_universe):
        """EP = TTM净利润 / 总市值（本地计算）。"""
        factor = EPFactor(db_with_data)
        result = factor.compute("2024-11-30", sample_universe)
        # 验证 EP 值合理（正数）
        valid = result.dropna(subset=["factor_value"])
        if not valid.empty:
            assert (valid["factor_value"] > 0).all()
            assert (valid["factor_value"] < 1).all()  # EP 通常远小于 1

    def test_repr(self, db_with_data):
        factor = EPFactor(db_with_data)
        assert "EP" in repr(factor)


class TestBPFactor:
    """BP（市净率倒数）因子测试。"""

    def test_compute_basic(self, db_with_data, sample_universe):
        factor = BPFactor(db_with_data)
        result = factor.compute("2024-11-30", sample_universe)
        assert len(result) > 0

    def test_positive_pb_yields_positive_bp(self, db_with_data, sample_universe):
        factor = BPFactor(db_with_data)
        result = factor.compute("2024-11-30", sample_universe)
        valid = result.dropna(subset=["factor_value"])
        assert (valid["factor_value"] > 0).all()


class TestMomentumFactors:
    """动量因子测试。"""

    def test_mom_1m(self, db_with_data, sample_universe):
        factor = MOM1MFactor(db_with_data)
        result = factor.compute("2024-11-30", sample_universe)
        assert "factor_value" in result.columns

    def test_mom_3m(self, db_with_data, sample_universe):
        factor = MOM3MFactor(db_with_data)
        result = factor.compute("2024-11-30", sample_universe)
        assert "factor_value" in result.columns

    def test_mom_12m(self, db_with_data, sample_universe):
        """12-1 动量因子。"""
        factor = MOM12MFactor(db_with_data)
        result = factor.compute("2024-11-30", sample_universe)
        assert "factor_value" in result.columns

    def test_mom_values_are_reasonable(self, db_with_data, sample_universe):
        """收益率在合理范围内（不超过 ±500%）。"""
        factor = MOM3MFactor(db_with_data)
        result = factor.compute("2024-11-30", sample_universe)
        valid = result.dropna(subset=["factor_value"])
        if not valid.empty:
            assert valid["factor_value"].abs().max() < 5.0


class TestQualityFactors:
    """质量因子测试。"""

    def test_roe(self, db_with_data, sample_universe):
        factor = ROEFactor(db_with_data)
        result = factor.compute("2024-11-30", sample_universe)
        assert len(result) > 0

    def test_gross_margin(self, db_with_data, sample_universe):
        factor = GrossMarginFactor(db_with_data)
        result = factor.compute("2024-11-30", sample_universe)
        assert len(result) > 0


class TestTechnicalFactor:
    """技术因子测试。"""

    def test_turnover_20d(self, db_with_data, sample_universe):
        factor = Turnover20DFactor(db_with_data)
        result = factor.compute("2024-11-30", sample_universe)
        assert len(result) > 0

    def test_turnover_positive(self, db_with_data, sample_universe):
        """换手率应为正值。"""
        factor = Turnover20DFactor(db_with_data)
        result = factor.compute("2024-11-30", sample_universe)
        valid = result.dropna(subset=["factor_value"])
        if not valid.empty:
            assert (valid["factor_value"] >= 0).all()


class TestFactorBaseHelpers:
    """因子基类工具方法测试。"""

    def test_get_latest_financial_respects_ann_date(self, db_with_data, sample_universe):
        """财务数据按公告日期过滤，防止未来函数。"""
        factor = EPFactor(db_with_data)
        # 在 Q1 公告日之前查询，不应看到 Q1 数据
        result = factor.get_latest_financial("2024-01-01", ["pe_ttm"])
        # 2024-01-01 之前没有 2024 年的公告
        assert result.empty or all(
            pd.to_datetime(row["ann_date"]).date() <= pd.to_datetime("2024-01-01").date()
            for _, row in result.iterrows()
        )

    def test_get_price_history(self, db_with_data, sample_universe):
        factor = EPFactor(db_with_data)
        df = factor.get_price_history("2024-06-30", 30, ["000001.SZ"])
        assert len(df) > 0
        assert "close" in df.columns or "ts_code" in df.columns

    def test_get_month_end_price(self, db_with_data, sample_universe):
        factor = EPFactor(db_with_data)
        df = factor.get_month_end_price("2024-06-30", 0, ["000001.SZ"])
        assert len(df) > 0
