"""
测试公共 fixtures。

使用 SQLite 内存数据库，无需外部 MySQL。
每个测试函数获得一个全新的数据库实例和预填充的示例数据。
"""

import sys
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

# 确保项目根目录在 sys.path 中
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from data.database import DatabaseManager


# ============================================================
# 数据库 fixture
# ============================================================

@pytest.fixture
def db():
    """创建一个 SQLite 内存数据库，自动建表。"""
    manager = DatabaseManager(db_url="sqlite:///:memory:")
    manager.init_tables()
    return manager


@pytest.fixture
def db_with_data(db):
    """带有示例数据的数据库。"""
    _insert_stock_basic(db)
    _insert_daily_price(db)
    _insert_financial_data(db)
    _insert_industry_class(db)
    return db


# ============================================================
# 示例数据生成
# ============================================================

SAMPLE_STOCKS = [
    ("000001.SZ", "平安银行", "主板", date(2000, 1, 1), 0),
    ("000002.SZ", "万科A", "主板", date(2000, 1, 1), 0),
    ("000063.SZ", "中兴通讯", "主板", date(2004, 1, 1), 0),
    ("300750.SZ", "宁德时代", "创业板", date(2018, 6, 11), 0),
    ("600519.SH", "贵州茅台", "主板", date(2001, 8, 27), 0),
    ("600036.SH", "招商银行", "主板", date(2002, 4, 9), 0),
    ("688981.SH", "中芯国际", "科创板", date(2020, 7, 16), 0),
    ("000003.SZ", "ST测试", "主板", date(2000, 1, 1), 1),
    ("000004.SZ", "新股测试", "主板", date(2024, 10, 1), 0),  # 上市不足180天
]

SAMPLE_INDUSTRIES = {
    "000001.SZ": "银行",
    "000002.SZ": "房地产",
    "000063.SZ": "通信设备",
    "300750.SZ": "电池",
    "600519.SH": "白酒",
    "600036.SH": "银行",
    "688981.SH": "半导体",
}


def _insert_stock_basic(db: DatabaseManager):
    """插入股票基本信息。"""
    np.random.seed(77)
    records = []
    for ts_code, name, market, list_date, is_st in SAMPLE_STOCKS:
        records.append({
            "ts_code": ts_code,
            "name": name,
            "market": market,
            "list_date": list_date,
            "delist_date": None,
            "is_st": is_st,
            "total_share": round(np.random.uniform(5000, 500000), 2),
            "float_share": round(np.random.uniform(3000, 400000), 2),
        })
    db.upsert_stock_basic(pd.DataFrame(records))


def _insert_daily_price(db: DatabaseManager):
    """
    插入 2024-01-02 ~ 2024-12-31 的模拟日线数据。
    生成带有趋势的随机价格，确保涨跌幅合理。
    """
    np.random.seed(42)
    trade_dates = pd.bdate_range("2024-01-02", "2024-12-31")

    all_rows = []
    for ts_code, name, market, list_date, is_st in SAMPLE_STOCKS:
        if is_st:
            continue  # ST 股也插入数据便于测试
        base_price = np.random.uniform(10, 100)
        prices = [base_price]
        for _ in range(len(trade_dates) - 1):
            chg = np.random.normal(0.0005, 0.02)
            chg = np.clip(chg, -0.095, 0.095)
            prices.append(prices[-1] * (1 + chg))

        for i, td in enumerate(trade_dates):
            close = round(prices[i], 2)
            open_ = round(close * (1 + np.random.uniform(-0.01, 0.01)), 2)
            high = round(max(open_, close) * (1 + np.random.uniform(0, 0.02)), 2)
            low = round(min(open_, close) * (1 - np.random.uniform(0, 0.02)), 2)
            volume = round(np.random.uniform(50000, 500000))
            amount = round(volume * close * 100)
            turnover = round(np.random.uniform(0.5, 5.0), 2)

            pct_chg = 0.0
            if i > 0:
                pct_chg = round((prices[i] / prices[i - 1] - 1) * 100, 2)

            is_limit_up = 1 if pct_chg >= 9.9 else 0
            is_limit_down = 1 if pct_chg <= -9.9 else 0
            if ts_code.startswith(("30", "68")):
                is_limit_up = 1 if pct_chg >= 19.9 else 0
                is_limit_down = 1 if pct_chg <= -19.9 else 0

            all_rows.append({
                "ts_code": ts_code,
                "trade_date": td.date(),
                "open": open_,
                "high": high,
                "low": low,
                "close": close,
                "volume": volume,
                "amount": amount,
                "turnover_rate": turnover,
                "pct_chg": pct_chg,
                "is_limit_up": is_limit_up,
                "is_limit_down": is_limit_down,
            })

    # ST 股也插入少量数据
    for i, td in enumerate(trade_dates[:10]):
        all_rows.append({
            "ts_code": "000003.SZ",
            "trade_date": td.date(),
            "open": 5.0, "high": 5.1, "low": 4.9, "close": 5.0,
            "volume": 10000, "amount": 500000,
            "turnover_rate": 0.5, "pct_chg": 0.0,
            "is_limit_up": 0, "is_limit_down": 0,
        })

    db.bulk_insert_daily_price(pd.DataFrame(all_rows))


def _insert_financial_data(db: DatabaseManager):
    """插入季度财务数据。"""
    records = []
    quarters = [
        ("20231231", "20240430"),  # 2023 年报（用于 TTM 计算）
        ("20240331", "20240430"),
        ("20240630", "20240831"),
        ("20240930", "20241031"),
    ]

    np.random.seed(123)
    for ts_code, name, market, list_date, is_st in SAMPLE_STOCKS:
        if is_st:
            continue
        for end_str, ann_str in quarters:
            records.append({
                "ts_code": ts_code,
                "ann_date": date(int(ann_str[:4]), int(ann_str[4:6]), int(ann_str[6:])),
                "end_date": date(int(end_str[:4]), int(end_str[4:6]), int(end_str[6:])),
                "pe_ttm": round(np.random.uniform(5, 80), 2),
                "pb": round(np.random.uniform(0.5, 10), 2),
                "roe_ttm": round(np.random.uniform(-5, 30), 2),
                "gross_margin": round(np.random.uniform(10, 70), 2),
                "revenue": round(np.random.uniform(1e8, 1e11), 0),
                "net_profit": round(np.random.uniform(1e7, 1e10), 0),
                "bps": round(np.random.uniform(2, 30), 2),
                "total_mv": round(np.random.uniform(1e5, 1e7), 0),
                "circ_mv": round(np.random.uniform(5e4, 5e6), 0),
            })

    db.upsert_financial_data(pd.DataFrame(records))


def _insert_industry_class(db: DatabaseManager):
    """插入行业分类。"""
    records = [
        {"ts_code": code, "industry_name": name, "industry_code": None}
        for code, name in SAMPLE_INDUSTRIES.items()
    ]
    db.upsert_industry_class(pd.DataFrame(records))


# ============================================================
# 通用辅助 fixtures
# ============================================================

@pytest.fixture
def sample_universe():
    """示例股票池 DataFrame。"""
    data = [
        {"ts_code": code, "name": name}
        for code, name, *_ in SAMPLE_STOCKS
        if not code.startswith("000003") and not code.startswith("000004")
    ]
    return pd.DataFrame(data)


@pytest.fixture
def sample_weights():
    """示例持仓权重 DataFrame。"""
    codes = ["000001.SZ", "000002.SZ", "000063.SZ", "300750.SZ",
             "600519.SH", "600036.SH", "688981.SH"]
    return pd.DataFrame({
        "ts_code": codes,
        "weight": [1 / len(codes)] * len(codes),
    })


@pytest.fixture
def sample_nav():
    """示例净值序列（模拟一年）。"""
    np.random.seed(99)
    dates = pd.bdate_range("2024-01-02", "2024-12-31")
    returns = np.random.normal(0.0003, 0.015, len(dates))
    nav = pd.Series((1 + returns).cumprod(), index=dates)
    return nav
