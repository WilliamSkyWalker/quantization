"""商品价格轮动因子 (CMDTY_MOM) 测试。"""

from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest

from data.database import DatabaseManager, CommodityPrice
from factors.commodity import CommodityMomentumFactor


# ============================================================
# Fixtures
# ============================================================

@pytest.fixture
def db_commodity(db_with_data):
    """带商品价格数据的数据库。"""
    _insert_commodity_prices(db_with_data)
    _insert_l2_industry(db_with_data)
    return db_with_data


def _insert_commodity_prices(db: DatabaseManager):
    """
    插入 AU/CU/RB 两个月的模拟商品期货日线。
    AU 和 CU 上涨（正动量），RB 下跌（负动量）。
    """
    np.random.seed(555)
    trade_dates = pd.bdate_range("2024-10-01", "2024-11-29")

    rows = []
    # AU: 黄金，上涨趋势
    base_au = 500.0
    for i, td in enumerate(trade_dates):
        price = base_au * (1 + 0.002 * i)  # 持续上涨
        rows.append({
            "commodity_code": "AU",
            "trade_date": td.date(),
            "ts_code": "AU2412.SHF",
            "open": round(price * 0.999, 2),
            "high": round(price * 1.005, 2),
            "low": round(price * 0.995, 2),
            "close": round(price, 2),
            "settle": round(price * 1.001, 2),
            "volume": 100000 + i * 1000,
            "amount": round(price * 100000, 0),
            "oi": 300000.0,  # 高持仓量
        })

    # CU: 铜，上涨趋势
    base_cu = 70000.0
    for i, td in enumerate(trade_dates):
        price = base_cu * (1 + 0.001 * i)
        rows.append({
            "commodity_code": "CU",
            "trade_date": td.date(),
            "ts_code": "CU2412.SHF",
            "open": round(price * 0.999, 2),
            "high": round(price * 1.003, 2),
            "low": round(price * 0.997, 2),
            "close": round(price, 2),
            "settle": round(price * 1.0005, 2),
            "volume": 200000 + i * 500,
            "amount": round(price * 200000, 0),
            "oi": 500000.0,  # 更高持仓量
        })

    # RB: 螺纹钢，下跌趋势
    base_rb = 3800.0
    for i, td in enumerate(trade_dates):
        price = base_rb * (1 - 0.003 * i)
        rows.append({
            "commodity_code": "RB",
            "trade_date": td.date(),
            "ts_code": "RB2501.SHF",
            "open": round(price * 1.001, 2),
            "high": round(price * 1.005, 2),
            "low": round(price * 0.995, 2),
            "close": round(price, 2),
            "settle": round(price * 0.999, 2),
            "volume": 500000 + i * 2000,
            "amount": round(price * 500000, 0),
            "oi": 400000.0,
        })

    db.upsert_commodity_price(pd.DataFrame(rows))


def _insert_l2_industry(db: DatabaseManager):
    """更新行业分类表添加 L2 信息，匹配 COMMODITY_INDUSTRY_MAP。"""
    # 在已有数据上追加 L2 行业名
    # conftest 已插入: 000001=银行, 000002=房地产, 000063=通信设备,
    # 300750=电池, 600519=白酒, 600036=银行, 688981=半导体
    # 我们额外插入一些有色金属和钢铁行业的测试股票
    records = [
        {"ts_code": "601899.SH", "industry_name": "有色金属",
         "l2_industry_name": "贵金属", "industry_code": None, "l2_industry_code": None},
        {"ts_code": "603993.SH", "industry_name": "有色金属",
         "l2_industry_name": "工业金属", "industry_code": None, "l2_industry_code": None},
        {"ts_code": "600019.SH", "industry_name": "钢铁",
         "l2_industry_name": "普钢", "industry_code": None, "l2_industry_code": None},
    ]
    db.upsert_industry_class(pd.DataFrame(records))

    # 给这些股票插入日线数据（选股需要）
    np.random.seed(777)
    trade_dates = pd.bdate_range("2024-01-02", "2024-12-31")
    all_rows = []
    for ts_code in ["601899.SH", "603993.SH", "600019.SH"]:
        base_price = np.random.uniform(10, 50)
        prices = [base_price]
        for _ in range(len(trade_dates) - 1):
            chg = np.random.normal(0.0005, 0.02)
            chg = np.clip(chg, -0.095, 0.095)
            prices.append(prices[-1] * (1 + chg))

        for i, td in enumerate(trade_dates):
            close = round(prices[i], 2)
            all_rows.append({
                "ts_code": ts_code,
                "trade_date": td.date(),
                "open": round(close * 1.001, 2),
                "high": round(close * 1.02, 2),
                "low": round(close * 0.98, 2),
                "close": close,
                "volume": 100000,
                "amount": round(close * 100000 * 100, 0),
                "turnover_rate": 2.0,
                "pct_chg": 0.0,
                "is_limit_up": 0,
                "is_limit_down": 0,
            })

    db.bulk_insert_daily_price(pd.DataFrame(all_rows))

    # 插入基本信息
    stock_basic = pd.DataFrame([
        {"ts_code": "601899.SH", "name": "紫金矿业", "market": "主板",
         "list_date": date(2008, 4, 25), "delist_date": None, "is_st": 0,
         "total_share": 260000.0, "float_share": 200000.0},
        {"ts_code": "603993.SH", "name": "洛阳钼业", "market": "主板",
         "list_date": date(2012, 10, 9), "delist_date": None, "is_st": 0,
         "total_share": 215000.0, "float_share": 180000.0},
        {"ts_code": "600019.SH", "name": "宝钢股份", "market": "主板",
         "list_date": date(2000, 12, 12), "delist_date": None, "is_st": 0,
         "total_share": 220000.0, "float_share": 200000.0},
    ])
    db.upsert_stock_basic(stock_basic)


# ============================================================
# 测试
# ============================================================

class TestCommodityMomentumFactor:
    """商品价格轮动因子测试。"""

    def test_output_format(self, db_commodity):
        """因子输出格式正确：DataFrame[ts_code, factor_value]。"""
        factor = CommodityMomentumFactor(db_commodity)
        universe = pd.DataFrame({
            "ts_code": ["601899.SH", "603993.SH", "600019.SH", "000001.SZ"]
        })
        result = factor.compute("2024-11-29", universe)

        assert isinstance(result, pd.DataFrame)
        assert "ts_code" in result.columns
        assert "factor_value" in result.columns
        assert len(result) == len(universe)

    def test_mapped_industry_has_value(self, db_commodity):
        """有映射行业的股票应有因子值。"""
        factor = CommodityMomentumFactor(db_commodity)
        universe = pd.DataFrame({
            "ts_code": ["601899.SH", "603993.SH", "600019.SH"]
        })
        result = factor.compute("2024-11-29", universe)

        # 有色金属和钢铁行业都有商品映射
        mapped = result[result["factor_value"].notna()]
        assert len(mapped) >= 2, f"期望至少2只有映射行业有值，实际 {len(mapped)}"

    def test_unmapped_industry_is_nan(self, db_commodity):
        """无映射行业的股票应为 NaN。"""
        factor = CommodityMomentumFactor(db_commodity)
        universe = pd.DataFrame({
            "ts_code": ["000001.SZ", "600519.SH"]  # 银行、白酒
        })
        result = factor.compute("2024-11-29", universe)

        # 银行和白酒无商品映射
        assert result["factor_value"].isna().all(), \
            f"无映射行业应为NaN: {result}"

    def test_l2_priority_over_l1(self, db_commodity):
        """L2 匹配优先于 L1。"""
        factor = CommodityMomentumFactor(db_commodity)

        # 601899=贵金属(L2), 603993=工业金属(L2)
        # 两者 L1 都是有色金属，但 L2 不同
        # AU 映射到 L2=贵金属, CU 映射到 L2=工业金属
        # AU 涨幅 > CU 涨幅（AU 日涨 0.2% vs CU 日涨 0.1%）
        universe = pd.DataFrame({
            "ts_code": ["601899.SH", "603993.SH"]
        })
        result = factor.compute("2024-11-29", universe)

        val_601899 = result[result["ts_code"] == "601899.SH"]["factor_value"].iloc[0]
        val_603993 = result[result["ts_code"] == "603993.SH"]["factor_value"].iloc[0]

        assert pd.notna(val_601899), "贵金属应有值"
        assert pd.notna(val_603993), "工业金属应有值"
        # AU(贵金属) 涨幅 > CU(工业金属) 涨幅
        assert val_601899 > val_603993, \
            f"贵金属动量({val_601899:.4f})应大于工业金属({val_603993:.4f})"

    def test_negative_momentum(self, db_commodity):
        """下跌商品对应的行业应有负动量。"""
        factor = CommodityMomentumFactor(db_commodity)
        # 600019=钢铁/普钢, RB 下跌趋势
        universe = pd.DataFrame({"ts_code": ["600019.SH"]})
        result = factor.compute("2024-11-29", universe)

        val = result["factor_value"].iloc[0]
        assert pd.notna(val), "钢铁行业应有值"
        assert val < 0, f"RB下跌，钢铁动量应为负: {val:.4f}"

    def test_no_commodity_data_graceful(self, db_with_data):
        """无商品数据时优雅降级（全部 NaN）。"""
        factor = CommodityMomentumFactor(db_with_data)
        universe = pd.DataFrame({
            "ts_code": ["000001.SZ", "600519.SH"]
        })
        result = factor.compute("2024-11-29", universe)

        assert len(result) == 2
        assert result["factor_value"].isna().all()

    def test_oi_weighted_aggregation(self, db_commodity):
        """同行业多商品应按 OI 加权平均。"""
        factor = CommodityMomentumFactor(db_commodity)

        # 获取商品动量（内部方法测试）
        commodity_df = db_commodity.get_commodity_price_history(
            ["AU", "CU"], "2024-11-29", 60
        )
        commodity_mom = factor._calc_commodity_momentum(commodity_df)

        # AU 和 CU 都映射到 L1=有色金属
        l1_mom = factor._aggregate_by_industry(commodity_mom, level="l1")

        assert "有色金属" in l1_mom
        au_mom = commodity_mom["AU"]["mom"]
        cu_mom = commodity_mom["CU"]["mom"]
        au_oi = commodity_mom["AU"]["oi"]
        cu_oi = commodity_mom["CU"]["oi"]

        expected = (au_mom * au_oi + cu_mom * cu_oi) / (au_oi + cu_oi)
        assert abs(l1_mom["有色金属"] - expected) < 1e-6, \
            f"OI加权: 期望 {expected:.6f}, 实际 {l1_mom['有色金属']:.6f}"


class TestCommodityPriceDB:
    """商品价格数据库操作测试。"""

    def test_upsert_and_query(self, db):
        """基本写入和查询。"""
        df = pd.DataFrame([{
            "commodity_code": "AU",
            "trade_date": date(2024, 11, 29),
            "ts_code": "AU2412.SHF",
            "close": 500.0,
            "settle": 501.0,
            "oi": 300000.0,
        }])
        db.upsert_commodity_price(df)

        result = db.get_commodity_price_history(["AU"], "2024-11-29", 30)
        assert len(result) == 1
        assert result["commodity_code"].iloc[0] == "AU"

    def test_upsert_idempotent(self, db):
        """重复写入不报错（upsert 语义）。"""
        df = pd.DataFrame([{
            "commodity_code": "CU",
            "trade_date": date(2024, 11, 29),
            "close": 70000.0,
            "settle": 70050.0,
            "oi": 500000.0,
        }])
        db.upsert_commodity_price(df)
        db.upsert_commodity_price(df)  # 重复

        result = db.get_commodity_price_history(["CU"], "2024-11-29", 30)
        assert len(result) == 1

    def test_latest_commodity_date(self, db):
        """获取最新商品日期。"""
        assert db.get_latest_commodity_date() is None

        df = pd.DataFrame([{
            "commodity_code": "AU",
            "trade_date": date(2024, 11, 29),
            "close": 500.0,
        }])
        db.upsert_commodity_price(df)

        latest = db.get_latest_commodity_date()
        assert latest is not None
        assert "2024-11-29" in latest
