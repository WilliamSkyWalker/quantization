"""数据库模块测试。"""

from datetime import date

import pandas as pd
import pytest


class TestDatabaseInit:
    """数据库初始化测试。"""

    def test_create_tables(self, db):
        """验证 4 张表是否成功创建。"""
        from sqlalchemy import text
        with db.engine.connect() as conn:
            tables = conn.execute(
                text("SELECT name FROM sqlite_master WHERE type='table'")
            ).fetchall()
        table_names = {t[0] for t in tables}
        assert "stock_basic" in table_names
        assert "daily_price" in table_names
        assert "financial_data" in table_names
        assert "industry_class" in table_names

    def test_table_count_empty(self, db):
        """空表返回 0。"""
        assert db.table_count("stock_basic") == 0


class TestStockBasic:
    """股票基本信息表读写测试。"""

    def test_upsert_insert(self, db):
        """插入新记录。"""
        df = pd.DataFrame([{
            "ts_code": "000001.SZ",
            "name": "平安银行",
            "market": "主板",
            "list_date": date(2000, 1, 1),
            "is_st": 0,
        }])
        db.upsert_stock_basic(df)
        assert db.table_count("stock_basic") == 1

    def test_upsert_update(self, db):
        """更新已有记录（同一 ts_code 不重复插入）。"""
        df1 = pd.DataFrame([{
            "ts_code": "000001.SZ", "name": "平安银行",
            "market": "主板", "list_date": date(2000, 1, 1), "is_st": 0,
        }])
        db.upsert_stock_basic(df1)

        df2 = pd.DataFrame([{
            "ts_code": "000001.SZ", "name": "平安银行",
            "market": "主板", "list_date": date(2000, 1, 1), "is_st": 1,
        }])
        db.upsert_stock_basic(df2)

        assert db.table_count("stock_basic") == 1
        result = db.get_stock_list(exclude_st=False)
        assert result.iloc[0]["is_st"] == 1

    def test_upsert_empty(self, db):
        """空 DataFrame 不报错。"""
        db.upsert_stock_basic(pd.DataFrame())
        assert db.table_count("stock_basic") == 0

    def test_get_stock_list_exclude_st(self, db_with_data):
        """剔除 ST 后数量减少。"""
        all_stocks = db_with_data.get_stock_list(exclude_st=False)
        no_st = db_with_data.get_stock_list(exclude_st=True)
        assert len(no_st) < len(all_stocks)
        assert all(row["is_st"] == 0 for _, row in no_st.iterrows())


class TestDailyPrice:
    """日线行情表读写测试。"""

    def test_bulk_insert(self, db):
        """批量插入日线数据。"""
        df = pd.DataFrame([{
            "ts_code": "000001.SZ",
            "trade_date": date(2024, 1, 2),
            "open": 10.0, "high": 10.5, "low": 9.8, "close": 10.2,
            "volume": 100000, "amount": 1020000,
            "turnover_rate": 1.5, "pct_chg": 2.0,
            "is_limit_up": 0, "is_limit_down": 0,
        }])
        db.bulk_insert_daily_price(df)
        assert db.table_count("daily_price") == 1

    def test_get_daily_price_by_code(self, db_with_data):
        """按代码查询日线。"""
        df = db_with_data.get_daily_price(ts_code="000001.SZ")
        assert len(df) > 0
        assert df["ts_code"].unique()[0] == "000001.SZ"

    def test_get_daily_price_by_date_range(self, db_with_data):
        """按日期范围查询。"""
        df = db_with_data.get_daily_price(
            start_date="2024-06-01", end_date="2024-06-30"
        )
        assert len(df) > 0

    def test_get_latest_trade_date(self, db_with_data):
        """获取最新交易日。"""
        latest = db_with_data.get_latest_trade_date()
        assert latest is not None
        assert "2024" in latest

    def test_get_latest_trade_date_empty(self, db):
        """空表返回 None。"""
        assert db.get_latest_trade_date() is None


class TestFinancialData:
    """财务数据表测试。"""

    def test_upsert_financial(self, db):
        """插入财务数据。"""
        df = pd.DataFrame([{
            "ts_code": "000001.SZ",
            "ann_date": date(2024, 4, 30),
            "end_date": date(2024, 3, 31),
            "pe_ttm": 8.5, "pb": 0.7, "roe_ttm": 12.5,
            "gross_margin": 45.0, "revenue": 1e10, "net_profit": 1e9,
            "total_mv": 3e6, "circ_mv": 2e6,
        }])
        db.upsert_financial_data(df)
        assert db.table_count("financial_data") == 1

    def test_upsert_financial_update(self, db):
        """同一 (ts_code, end_date) 更新而非重复插入。"""
        base = {
            "ts_code": "000001.SZ",
            "ann_date": date(2024, 4, 30),
            "end_date": date(2024, 3, 31),
            "pe_ttm": 8.5, "pb": 0.7,
        }
        db.upsert_financial_data(pd.DataFrame([base]))
        base["pe_ttm"] = 9.0
        db.upsert_financial_data(pd.DataFrame([base]))

        assert db.table_count("financial_data") == 1
        result = db.query("SELECT pe_ttm FROM financial_data")
        assert result.iloc[0]["pe_ttm"] == 9.0


class TestIndustryClass:
    """行业分类表测试。"""

    def test_upsert_industry(self, db):
        df = pd.DataFrame([{
            "ts_code": "000001.SZ",
            "industry_name": "银行",
            "industry_code": None,
        }])
        db.upsert_industry_class(df)
        assert db.table_count("industry_class") == 1

    def test_get_industry_map(self, db_with_data):
        df = db_with_data.get_industry_map()
        assert len(df) > 0
        assert "ts_code" in df.columns
        assert "industry_name" in df.columns


class TestQuery:
    """通用查询测试。"""

    def test_raw_query(self, db_with_data):
        result = db_with_data.query("SELECT COUNT(*) as cnt FROM stock_basic")
        assert result.iloc[0]["cnt"] > 0
