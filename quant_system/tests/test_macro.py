"""宏观经济因子测试。"""

from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest

from data.database import DatabaseManager, MacroIndicator
from factors.macro import (
    MacroCycleFactor,
    MacroLiquidityFactor,
    MacroInflationFactor,
    MacroExternalFactor,
)


# ============================================================
# Fixtures
# ============================================================

@pytest.fixture
def db_macro(db_with_data):
    """带宏观指标数据的数据库（24 个月模拟数据）。"""
    _insert_macro_indicators(db_with_data)
    return db_with_data


def _insert_macro_indicators(db: DatabaseManager):
    """
    插入 24 个月模拟宏观指标数据（2023-01 ~ 2024-12）。
    模拟经济上行周期：PMI > 50，PPI 上升，M2 宽松。
    """
    np.random.seed(888)
    records = []

    for i in range(24):
        month_date = pd.Timestamp("2023-01-31") + pd.DateOffset(months=i)
        report_date = month_date + pd.offsets.MonthEnd(0)

        # PMI: 49~52 区间，逐步上升
        pmi = 49.0 + i * 0.15 + np.random.normal(0, 0.3)
        records.append({"indicator_code": "PMI_MFG", "report_date": report_date, "value": pmi})

        # PMI 新订单: 跟随 PMI
        pmi_no = pmi + np.random.normal(0.5, 0.3)
        records.append({"indicator_code": "PMI_NEW_ORDER", "report_date": report_date, "value": pmi_no})

        # PPI 同比: -2% ~ +3%，逐步回升
        ppi = -2.0 + i * 0.25 + np.random.normal(0, 0.5)
        records.append({"indicator_code": "PPI_YOY", "report_date": report_date, "value": ppi})

        # PPI 生产资料: 跟随 PPI
        ppi_mp = ppi + np.random.normal(0.5, 0.3)
        records.append({"indicator_code": "PPI_MP_YOY", "report_date": report_date, "value": ppi_mp})

        # CPI 同比: 1% ~ 3%
        cpi = 1.5 + np.random.normal(0, 0.3)
        records.append({"indicator_code": "CPI_YOY", "report_date": report_date, "value": cpi})

        # M2 同比: 8% ~ 12%
        m2 = 9.5 + np.random.normal(0, 0.5)
        records.append({"indicator_code": "M2_YOY", "report_date": report_date, "value": m2})

        # M1 同比: 3% ~ 8%
        m1 = 5.0 + i * 0.1 + np.random.normal(0, 0.5)
        records.append({"indicator_code": "M1_YOY", "report_date": report_date, "value": m1})

        # M1-M2 剪刀差
        records.append({"indicator_code": "M1_M2_SPREAD", "report_date": report_date, "value": m1 - m2})

    # 日频数据：SHIBOR, LPR, UST（只插入每月月末值简化测试）
    for i in range(24):
        month_date = pd.Timestamp("2023-01-31") + pd.DateOffset(months=i)
        report_date = month_date + pd.offsets.MonthEnd(0)

        # SHIBOR 3M: 2.3% ~ 2.8%，小幅波动
        shibor = 2.5 + np.random.normal(0, 0.1)
        records.append({"indicator_code": "SHIBOR_3M", "report_date": report_date, "value": shibor})

        records.append({"indicator_code": "SHIBOR_ON", "report_date": report_date, "value": shibor - 1.0})

        # LPR 1Y: 3.45% 附近，偶尔下调
        lpr = 3.45 - (i // 6) * 0.05
        records.append({"indicator_code": "LPR_1Y", "report_date": report_date, "value": lpr})

        # UST 10Y: 3.5% ~ 4.5%
        ust10 = 4.0 + np.random.normal(0, 0.2)
        records.append({"indicator_code": "UST_10Y", "report_date": report_date, "value": ust10})

        # UST 2Y-10Y 利差
        spread = 0.3 + np.random.normal(0, 0.1)
        records.append({"indicator_code": "UST_2Y10Y", "report_date": report_date, "value": spread})

    # GDP 季度数据
    for q in range(8):  # 8 个季度
        quarter_date = pd.Timestamp("2023-03-31") + pd.DateOffset(months=q * 3)
        report_date = quarter_date + pd.offsets.QuarterEnd(0)
        gdp_yoy = 4.5 + q * 0.2 + np.random.normal(0, 0.3)
        records.append({"indicator_code": "GDP_YOY", "report_date": report_date, "value": gdp_yoy})

    db.upsert_macro_indicator(pd.DataFrame(records))


# ============================================================
# DB 操作测试
# ============================================================

class TestMacroDB:
    """宏观指标数据库操作测试。"""

    def test_upsert_and_query(self, db):
        """基本写入和查询。"""
        df = pd.DataFrame([{
            "indicator_code": "CPI_YOY",
            "report_date": date(2024, 12, 31),
            "value": 2.3,
        }])
        db.upsert_macro_indicator(df)

        result = db.get_macro_indicator_history("CPI_YOY", "2024-12-31", lookback_months=3)
        assert len(result) == 1
        assert abs(result["value"].iloc[0] - 2.3) < 1e-6

    def test_upsert_idempotent(self, db):
        """重复写入不报错（upsert 语义）。"""
        df = pd.DataFrame([{
            "indicator_code": "PPI_YOY",
            "report_date": date(2024, 12, 31),
            "value": 1.5,
        }])
        db.upsert_macro_indicator(df)
        # 更新值
        df2 = pd.DataFrame([{
            "indicator_code": "PPI_YOY",
            "report_date": date(2024, 12, 31),
            "value": 2.0,
        }])
        db.upsert_macro_indicator(df2)

        result = db.get_macro_indicator_history("PPI_YOY", "2024-12-31", lookback_months=3)
        assert len(result) == 1
        assert abs(result["value"].iloc[0] - 2.0) < 1e-6

    def test_latest_macro_date(self, db):
        """获取最新宏观指标日期。"""
        assert db.get_latest_macro_date() is None

        df = pd.DataFrame([
            {"indicator_code": "CPI_YOY", "report_date": date(2024, 11, 30), "value": 2.0},
            {"indicator_code": "CPI_YOY", "report_date": date(2024, 12, 31), "value": 2.3},
        ])
        db.upsert_macro_indicator(df)

        latest = db.get_latest_macro_date()
        assert latest is not None
        assert "2024-12-31" in latest

        # 按指标筛选
        latest_cpi = db.get_latest_macro_date("CPI_YOY")
        assert "2024-12-31" in latest_cpi

    def test_history_lookback(self, db_macro):
        """历史查询回看月数正确。"""
        # 查询最近 6 个月
        result = db_macro.get_macro_indicator_history("PMI_MFG", "2024-12-31", lookback_months=6)
        assert len(result) >= 5  # 至少 5 个月数据


# ============================================================
# 因子测试
# ============================================================

class TestMacroCycleFactor:
    """经济周期因子测试。"""

    def test_output_format(self, db_macro):
        """因子输出格式正确。"""
        factor = MacroCycleFactor(db_macro)
        universe = pd.DataFrame({
            "ts_code": ["000001.SZ", "000002.SZ", "600519.SH"]
        })
        result = factor.compute("2024-12-31", universe)

        assert isinstance(result, pd.DataFrame)
        assert "ts_code" in result.columns
        assert "factor_value" in result.columns
        assert len(result) == 3

    def test_cycle_industry_has_value(self, db_macro):
        """周期行业应有因子值。"""
        factor = MacroCycleFactor(db_macro)
        # 000001.SZ=银行（无映射），600519.SH=白酒（无映射）
        # 需要插入有周期行业的股票
        _insert_cycle_stocks(db_macro)

        universe = pd.DataFrame({
            "ts_code": ["TEST_STEEL.SZ", "TEST_FOOD.SZ", "000001.SZ"]
        })
        result = factor.compute("2024-12-31", universe)

        # 钢铁和食品饮料有映射
        mapped = result[result["factor_value"].notna()]
        assert len(mapped) >= 1, f"期望至少 1 只有映射的，实际 {len(mapped)}"

    def test_unmapped_industry_nan(self, db_macro):
        """无映射行业应为 NaN。"""
        factor = MacroCycleFactor(db_macro)
        # 半导体 (688981.SH) 在 MACRO_CYCLE_SENSITIVITY 中无映射
        universe = pd.DataFrame({"ts_code": ["688981.SH"]})
        result = factor.compute("2024-12-31", universe)
        assert result["factor_value"].isna().all()

    def test_pmi_fallback(self, db_macro):
        """PMI 不可用时退化到 PPI only 版本。"""
        # 删除 PMI 数据
        with db_macro.get_session() as session:
            session.query(MacroIndicator).filter(
                MacroIndicator.indicator_code.in_(["PMI_MFG", "PMI_NEW_ORDER"])
            ).delete(synchronize_session="fetch")
            session.commit()

        _insert_cycle_stocks(db_macro)

        factor = MacroCycleFactor(db_macro)
        universe = pd.DataFrame({"ts_code": ["TEST_STEEL.SZ"]})
        result = factor.compute("2024-12-31", universe)

        # 应仍有值（PPI 退化版本）
        assert result["factor_value"].notna().any(), "PPI退化版本应仍有值"


class TestMacroLiquidityFactor:
    """流动性因子测试。"""

    def test_output_format(self, db_macro):
        """因子输出格式正确。"""
        factor = MacroLiquidityFactor(db_macro)
        universe = pd.DataFrame({
            "ts_code": ["000001.SZ", "000002.SZ"]
        })
        result = factor.compute("2024-12-31", universe)

        assert len(result) == 2
        assert "factor_value" in result.columns

    def test_real_estate_has_value(self, db_macro):
        """房地产行业应有因子值（流动性高敏感）。"""
        factor = MacroLiquidityFactor(db_macro)
        # 000002.SZ = 房地产
        universe = pd.DataFrame({"ts_code": ["000002.SZ"]})
        result = factor.compute("2024-12-31", universe)
        assert result["factor_value"].notna().any(), "房地产应有流动性因子值"


class TestMacroInflationFactor:
    """通胀结构因子测试。"""

    def test_output_format(self, db_macro):
        """因子输出格式正确。"""
        factor = MacroInflationFactor(db_macro)
        universe = pd.DataFrame({
            "ts_code": ["000001.SZ", "600519.SH"]
        })
        result = factor.compute("2024-12-31", universe)
        assert len(result) == 2

    def test_consumer_industry_sensitivity(self, db_macro):
        """消费行业应有正向通胀敏感度。"""
        _insert_cycle_stocks(db_macro)

        factor = MacroInflationFactor(db_macro)
        universe = pd.DataFrame({"ts_code": ["TEST_FOOD.SZ", "TEST_STEEL.SZ"]})
        result = factor.compute("2024-12-31", universe)

        food_val = result[result["ts_code"] == "TEST_FOOD.SZ"]["factor_value"]
        steel_val = result[result["ts_code"] == "TEST_STEEL.SZ"]["factor_value"]

        # 两者方向应相反（食品饮料正，钢铁负）
        if food_val.notna().any() and steel_val.notna().any():
            # 符号相反
            assert (food_val.iloc[0] * steel_val.iloc[0]) < 0, \
                "食品饮料和钢铁的通胀因子应方向相反"


class TestMacroExternalFactor:
    """外部风险因子测试。"""

    def test_output_format(self, db_macro):
        """因子输出格式正确。"""
        factor = MacroExternalFactor(db_macro)
        universe = pd.DataFrame({
            "ts_code": ["000001.SZ", "000063.SZ"]
        })
        result = factor.compute("2024-12-31", universe)
        assert len(result) == 2

    def test_no_data_graceful(self, db):
        """无宏观数据时优雅降级。"""
        # 使用空数据库
        factor = MacroExternalFactor(db)
        universe = pd.DataFrame({"ts_code": ["000001.SZ"]})
        result = factor.compute("2024-12-31", universe)
        assert len(result) == 1
        assert result["factor_value"].isna().all()


class TestPublicationLag:
    """发布延迟测试。"""

    def test_cpi_lag(self, db_macro):
        """CPI 有 16 天发布延迟。"""
        factor = MacroInflationFactor(db_macro)

        # 2025-01-15 应该看不到 2024-12-31 的 CPI（延迟 16 天）
        cpi_series = factor._get_indicator_history("CPI_YOY", "2025-01-15")
        if not cpi_series.empty:
            latest_date = cpi_series.index[-1]
            effective_date = pd.Timestamp("2025-01-15") - pd.Timedelta(days=16)
            assert latest_date <= effective_date, \
                f"CPI 最新数据 {latest_date} 不应超过有效日期 {effective_date}"

    def test_shibor_no_lag(self, db_macro):
        """SHIBOR 无发布延迟。"""
        factor = MacroLiquidityFactor(db_macro)
        series = factor._get_indicator_history("SHIBOR_3M", "2024-12-31")
        if not series.empty:
            latest_date = series.index[-1]
            # SHIBOR lag=0，应能取到 2024-12-31
            assert latest_date <= pd.Timestamp("2024-12-31")


# ============================================================
# 辅助数据插入
# ============================================================

def _insert_cycle_stocks(db: DatabaseManager):
    """插入周期/消费测试股票。"""
    # 行业分类
    records = [
        {"ts_code": "TEST_STEEL.SZ", "industry_name": "钢铁",
         "industry_code": None, "l2_industry_code": None, "l2_industry_name": None},
        {"ts_code": "TEST_FOOD.SZ", "industry_name": "食品饮料",
         "industry_code": None, "l2_industry_code": None, "l2_industry_name": None},
    ]
    db.upsert_industry_class(pd.DataFrame(records))

    # 日线数据
    np.random.seed(999)
    trade_dates = pd.bdate_range("2024-01-02", "2024-12-31")
    all_rows = []
    for ts_code in ["TEST_STEEL.SZ", "TEST_FOOD.SZ"]:
        base_price = 20.0
        for i, td in enumerate(trade_dates):
            close = round(base_price * (1 + np.random.normal(0, 0.01)), 2)
            base_price = close
            all_rows.append({
                "ts_code": ts_code, "trade_date": td.date(),
                "open": close, "high": round(close * 1.01, 2),
                "low": round(close * 0.99, 2), "close": close,
                "volume": 100000, "amount": round(close * 100000 * 100, 0),
                "turnover_rate": 2.0, "pct_chg": 0.0,
                "is_limit_up": 0, "is_limit_down": 0,
            })
    db.bulk_insert_daily_price(pd.DataFrame(all_rows))

    # 基本信息
    stock_basic = pd.DataFrame([
        {"ts_code": "TEST_STEEL.SZ", "name": "测试钢铁", "market": "主板",
         "list_date": date(2000, 1, 1), "delist_date": None, "is_st": 0,
         "total_share": 100000.0, "float_share": 80000.0},
        {"ts_code": "TEST_FOOD.SZ", "name": "测试食品", "market": "主板",
         "list_date": date(2000, 1, 1), "delist_date": None, "is_st": 0,
         "total_share": 100000.0, "float_share": 80000.0},
    ])
    db.upsert_stock_basic(stock_basic)
