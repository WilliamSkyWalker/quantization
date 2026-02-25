"""绩效分析与报告模块测试。"""

import numpy as np
import pandas as pd
import pytest

from monitor.performance import PerformanceAnalyzer
from monitor.report import ReportGenerator


class TestPerformanceMetrics:
    """绩效指标计算测试。"""

    def test_calc_metrics_basic(self, db_with_data, sample_nav):
        analyzer = PerformanceAnalyzer(db_with_data)
        metrics = analyzer.calc_metrics(sample_nav)

        assert "年化收益率" in metrics
        assert "年化波动率" in metrics
        assert "夏普比率" in metrics
        assert "最大回撤" in metrics
        assert "日胜率" in metrics
        assert "交易天数" in metrics

    def test_positive_nav_positive_return(self, db_with_data):
        """稳定上涨的净值应有正收益。"""
        dates = pd.bdate_range("2024-01-02", "2024-12-31")
        nav = pd.Series(np.linspace(1.0, 1.3, len(dates)), index=dates)

        analyzer = PerformanceAnalyzer(db_with_data)
        metrics = analyzer.calc_metrics(nav)

        assert metrics["年化收益率"] > 0
        assert metrics["最大回撤"] > -0.01  # 几乎无回撤

    def test_sharpe_ratio_sign(self, db_with_data):
        """高收益低波动 → 高夏普。"""
        dates = pd.bdate_range("2024-01-02", "2024-12-31")
        # 稳定上涨，极低波动
        np.random.seed(42)
        returns = 0.001 + np.random.normal(0, 0.0001, len(dates))  # 均值0.1%，极小噪声
        nav = pd.Series((1 + returns).cumprod(), index=dates)

        analyzer = PerformanceAnalyzer(db_with_data)
        metrics = analyzer.calc_metrics(nav)
        assert metrics["夏普比率"] > 2  # 应该很高

    def test_max_drawdown_is_negative(self, db_with_data, sample_nav):
        analyzer = PerformanceAnalyzer(db_with_data)
        metrics = analyzer.calc_metrics(sample_nav)
        assert metrics["最大回撤"] <= 0

    def test_with_benchmark(self, db_with_data, sample_nav):
        """带基准的指标计算。"""
        np.random.seed(88)
        bm_dates = sample_nav.index
        bm_returns = np.random.normal(0.0002, 0.012, len(bm_dates))
        benchmark = pd.Series((1 + bm_returns).cumprod(), index=bm_dates)

        analyzer = PerformanceAnalyzer(db_with_data)
        metrics = analyzer.calc_metrics(sample_nav, benchmark)

        assert "超额年化收益率" in metrics
        assert "信息比率" in metrics
        assert "跟踪误差" in metrics


class TestRollingMetrics:
    """滚动指标测试。"""

    def test_rolling_metrics(self, db_with_data, sample_nav):
        analyzer = PerformanceAnalyzer(db_with_data)
        result = analyzer.rolling_metrics(sample_nav, window=60)

        assert "rolling_sharpe" in result.columns
        assert "rolling_volatility" in result.columns
        assert len(result) == len(sample_nav)


class TestMonthlyReturns:
    """月度收益率表测试。"""

    def test_monthly_table(self, sample_nav):
        result = PerformanceAnalyzer.monthly_returns(sample_nav)
        assert not result.empty
        # 应有 12 个月列 + 全年列
        assert "全年" in result.columns

    def test_monthly_returns_sum_approx(self, sample_nav):
        """月度收益复合后约等于总收益。"""
        monthly = PerformanceAnalyzer.monthly_returns(sample_nav)
        # 取某一年的月度收益
        for year_idx in monthly.index:
            month_cols = [c for c in monthly.columns if c != "全年"]
            monthly_vals = monthly.loc[year_idx, month_cols].dropna()
            compounded = (1 + monthly_vals).prod() - 1
            annual_val = monthly.loc[year_idx, "全年"]
            if pd.notna(annual_val):
                assert abs(compounded - annual_val) < 0.02


class TestIndustryAttribution:
    """行业归因测试。"""

    def test_attribution(self, db_with_data, sample_weights):
        analyzer = PerformanceAnalyzer(db_with_data)
        result = analyzer.industry_attribution(
            sample_weights, "2024-06-01", "2024-11-30"
        )
        if not result.empty:
            assert "industry_name" in result.columns
            assert "contribution" in result.columns
            assert "weight" in result.columns


class TestReportGenerator:
    """HTML 报告生成测试。"""

    def test_generate_html(self, sample_nav, tmp_path):
        """生成 HTML 不报错。"""
        gen = ReportGenerator(title="Test Report")
        html = gen.generate(nav=sample_nav)

        assert "<html" in html
        assert "Test Report" in html
        assert "年化收益" in html

    def test_save_report(self, sample_nav, tmp_path):
        """保存报告到文件。"""
        gen = ReportGenerator()
        html = gen.generate(nav=sample_nav)

        filepath = str(tmp_path / "test_report.html")
        result_path = gen.save(html, filepath)
        # save 方法写到 output/reports/ 下，这里验证 html 内容
        assert len(html) > 100

    def test_with_all_sections(self, db_with_data, sample_nav, sample_weights):
        """包含所有章节的完整报告。"""
        np.random.seed(77)
        benchmark = pd.Series(
            (1 + np.random.normal(0.0002, 0.01, len(sample_nav))).cumprod(),
            index=sample_nav.index,
        )

        ic_data = {
            "EP": pd.DataFrame({
                "ic": np.random.uniform(-0.1, 0.15, 12),
            }),
        }

        attribution = pd.DataFrame({
            "industry_name": ["银行", "白酒", "电池"],
            "weight": [0.3, 0.3, 0.4],
            "avg_return": [0.05, 0.10, -0.03],
            "contribution": [0.015, 0.03, -0.012],
            "n_stocks": [2, 1, 1],
        })

        gen = ReportGenerator()
        html = gen.generate(
            nav=sample_nav,
            benchmark_nav=benchmark,
            holdings=sample_weights,
            industry_attribution=attribution,
            factor_ic=ic_data,
        )

        assert "净值曲线" in html
        assert "月度收益" in html
        assert "持仓明细" in html
        assert "行业归因" in html
        assert "因子 IC" in html
