"""
PaperTrader 单元测试。

覆盖：
    - 账户创建与加载
    - 买卖执行
    - 涨跌停限制
    - 资金不足处理
    - 100股整手约束
    - 费用计算
    - 回放模式
    - 净值记录
    - 账户重置
"""

from datetime import date

import numpy as np
import pandas as pd
import pytest

from data.database import (
    DatabaseManager,
    PaperAccount,
    PaperPosition,
    PaperTransaction,
    PaperNav,
)
from execution.paper_trader import PaperTrader, LOT_SIZE, MIN_COMMISSION


# ============================================================
# Fixtures
# ============================================================

@pytest.fixture
def db():
    """SQLite 内存数据库。"""
    manager = DatabaseManager(db_url="sqlite:///:memory:")
    manager.init_tables()
    return manager


@pytest.fixture
def db_with_prices(db):
    """带有行情数据的数据库。"""
    _insert_sample_prices(db)
    return db


@pytest.fixture
def trader(db_with_prices):
    """已连接的 PaperTrader。"""
    t = PaperTrader(db_with_prices, account_name="test")
    t.connect(initial_capital=1_000_000)
    return t


def _insert_sample_prices(db: DatabaseManager):
    """插入测试用行情数据。"""
    rows = []
    # 5只股票，2024-01-02 ~ 2024-01-10（7个交易日）
    stocks = {
        "000001.SZ": 10.0,   # 平安银行
        "000002.SZ": 20.0,   # 万科A
        "600519.SH": 100.0,  # 贵州茅台
        "300750.SZ": 50.0,   # 宁德时代
        "600036.SH": 30.0,   # 招商银行
    }

    trade_dates = pd.bdate_range("2024-01-02", "2024-01-10")
    np.random.seed(42)

    for ts_code, base_price in stocks.items():
        price = base_price
        prev_price = base_price
        for i, td in enumerate(trade_dates):
            if i > 0:
                chg = np.random.normal(0, 0.02)
                chg = np.clip(chg, -0.09, 0.09)
                price = round(prev_price * (1 + chg), 2)

            pct_chg = round((price / prev_price - 1) * 100, 2) if i > 0 else 0
            is_limit_up = 1 if pct_chg >= 9.9 else 0
            is_limit_down = 1 if pct_chg <= -9.9 else 0

            rows.append({
                "ts_code": ts_code,
                "trade_date": td.date(),
                "open": price,
                "high": round(price * 1.01, 2),
                "low": round(price * 0.99, 2),
                "close": price,
                "volume": 100000,
                "amount": 100000 * price * 100,
                "turnover_rate": 2.0,
                "pct_chg": pct_chg,
                "adj_factor": 1.0,
                "is_limit_up": is_limit_up,
                "is_limit_down": is_limit_down,
            })
            prev_price = price

    db.bulk_insert_daily_price(pd.DataFrame(rows))


# ============================================================
# 账户管理
# ============================================================

class TestAccountManagement:

    def test_connect_creates_account(self, db_with_prices):
        """新账户创建。"""
        t = PaperTrader(db_with_prices, account_name="new_test")
        t.connect(initial_capital=500_000)

        info = t.get_account_info()
        assert info["total_assets"] == 500_000
        assert info["available_cash"] == 500_000
        assert info["market_value"] == 0
        assert info["pnl"] == 0

    def test_connect_loads_existing(self, db_with_prices):
        """已有账户加载。"""
        t1 = PaperTrader(db_with_prices, account_name="persist_test")
        t1.connect(initial_capital=800_000)

        t2 = PaperTrader(db_with_prices, account_name="persist_test")
        t2.connect(initial_capital=999_999)  # 不同金额，应被忽略

        info = t2.get_account_info()
        assert info["total_assets"] == 800_000  # 保持原始金额

    def test_reset_account(self, trader):
        """账户重置。"""
        # 先做一笔交易
        weights = pd.DataFrame({
            "ts_code": ["000001.SZ"],
            "weight": [0.5],
        })
        trader.sync_position(weights, trade_date="2024-01-02")

        # 确认有持仓
        pos = trader.get_current_positions()
        assert not pos.empty

        # 重置
        trader.reset_account()

        pos = trader.get_current_positions()
        assert pos.empty

        info = trader.get_account_info()
        assert info["total_assets"] == 1_000_000
        assert info["available_cash"] == 1_000_000

    def test_get_position_report(self, trader):
        """持仓报告格式。"""
        report = trader.get_position_report()
        assert "模拟盘持仓报告" in report
        assert "总资产" in report


# ============================================================
# 交易执行
# ============================================================

class TestTradeExecution:

    def test_buy_single_stock(self, trader):
        """买入单只股票。"""
        weights = pd.DataFrame({
            "ts_code": ["000001.SZ"],
            "weight": [0.5],
        })
        result = trader.sync_position(weights, trade_date="2024-01-02")
        assert result["success"] >= 1

        pos = trader.get_current_positions()
        assert len(pos) == 1
        assert pos.iloc[0]["ts_code"] == "000001.SZ"
        assert pos.iloc[0]["volume"] > 0
        assert pos.iloc[0]["volume"] % LOT_SIZE == 0

        info = trader.get_account_info()
        assert info["available_cash"] < 1_000_000
        assert info["market_value"] > 0

    def test_sell_position(self, trader):
        """卖出已有持仓。"""
        # 先买入
        buy_weights = pd.DataFrame({
            "ts_code": ["000001.SZ"],
            "weight": [0.5],
        })
        trader.sync_position(buy_weights, trade_date="2024-01-02")

        # 再清仓
        sell_weights = pd.DataFrame({
            "ts_code": ["000002.SZ"],  # 换股
            "weight": [0.3],
        })
        trader.sync_position(sell_weights, trade_date="2024-01-03")

        pos = trader.get_current_positions()
        codes = pos["ts_code"].tolist()
        assert "000001.SZ" not in codes
        assert "000002.SZ" in codes

    def test_lot_rounding(self, trader):
        """100股整手约束。"""
        weights = pd.DataFrame({
            "ts_code": ["600519.SH"],  # 茅台 100元/股
            "weight": [0.05],          # 50000元，目标500股 -> 500股
        })
        result = trader.sync_position(weights, trade_date="2024-01-02")

        pos = trader.get_current_positions()
        if not pos.empty:
            vol = pos.iloc[0]["volume"]
            assert vol % LOT_SIZE == 0

    def test_sync_position_multi_stock(self, trader):
        """多股同步。"""
        weights = pd.DataFrame({
            "ts_code": ["000001.SZ", "000002.SZ", "600036.SH"],
            "weight": [0.3, 0.3, 0.3],
        })
        result = trader.sync_position(weights, trade_date="2024-01-02")

        pos = trader.get_current_positions()
        assert len(pos) == 3

        # 总权重约 0.9，现金应剩 ~10%
        info = trader.get_account_info()
        assert info["available_cash"] > 0

    def test_sell_before_buy(self, trader):
        """先卖后买释放资金。"""
        # 满仓一只股
        weights1 = pd.DataFrame({
            "ts_code": ["000001.SZ"],
            "weight": [0.9],
        })
        trader.sync_position(weights1, trade_date="2024-01-02")

        # 换仓到另一只
        weights2 = pd.DataFrame({
            "ts_code": ["000002.SZ"],
            "weight": [0.9],
        })
        result = trader.sync_position(weights2, trade_date="2024-01-03")

        pos = trader.get_current_positions()
        codes = pos["ts_code"].tolist()
        assert "000002.SZ" in codes


# ============================================================
# 涨跌停限制
# ============================================================

class TestLimitConstraints:

    def test_limit_up_blocks_buy(self, db):
        """涨停不可买入。"""
        # 插入一只涨停股
        rows = [{
            "ts_code": "999999.SZ",
            "trade_date": date(2024, 1, 2),
            "open": 10.0, "high": 11.0, "low": 10.0, "close": 11.0,
            "volume": 100000, "amount": 11000000,
            "turnover_rate": 2.0, "pct_chg": 10.0,
            "adj_factor": 1.0,
            "is_limit_up": 1, "is_limit_down": 0,
        }]
        db.bulk_insert_daily_price(pd.DataFrame(rows))

        t = PaperTrader(db, account_name="limit_test")
        t.connect(initial_capital=1_000_000)

        weights = pd.DataFrame({
            "ts_code": ["999999.SZ"],
            "weight": [0.5],
        })
        result = t.sync_position(weights, trade_date="2024-01-02")
        assert result["failed"] >= 1

        pos = t.get_current_positions()
        assert pos.empty

    def test_limit_down_blocks_sell(self, db):
        """跌停不可卖出。"""
        # 第1天正常买入
        rows = [
            {
                "ts_code": "888888.SZ",
                "trade_date": date(2024, 1, 2),
                "open": 10.0, "high": 10.5, "low": 9.8, "close": 10.0,
                "volume": 100000, "amount": 10000000,
                "turnover_rate": 2.0, "pct_chg": 0.0,
                "adj_factor": 1.0,
                "is_limit_up": 0, "is_limit_down": 0,
            },
            # 第2天跌停
            {
                "ts_code": "888888.SZ",
                "trade_date": date(2024, 1, 3),
                "open": 9.0, "high": 9.0, "low": 9.0, "close": 9.0,
                "volume": 100000, "amount": 9000000,
                "turnover_rate": 2.0, "pct_chg": -10.0,
                "adj_factor": 1.0,
                "is_limit_up": 0, "is_limit_down": 1,
            },
        ]
        db.bulk_insert_daily_price(pd.DataFrame(rows))

        t = PaperTrader(db, account_name="limit_down_test")
        t.connect(initial_capital=1_000_000)

        # 第1天买入
        buy = pd.DataFrame({"ts_code": ["888888.SZ"], "weight": [0.5]})
        t.sync_position(buy, trade_date="2024-01-02")

        pos_before = t.get_current_positions()
        vol_before = pos_before.iloc[0]["volume"]

        # 第2天尝试卖出
        sell = pd.DataFrame({"ts_code": ["888888.SZ"], "weight": [0.0]})
        result = t.sync_position(sell, trade_date="2024-01-03")
        assert result["failed"] >= 1

        # 持仓不变
        pos_after = t.get_current_positions()
        assert pos_after.iloc[0]["volume"] == vol_before


# ============================================================
# 资金不足
# ============================================================

class TestInsufficientCash:

    def test_partial_fill_on_low_cash(self, db_with_prices):
        """资金不足时部分成交。"""
        t = PaperTrader(db_with_prices, account_name="low_cash")
        t.connect(initial_capital=5_000)  # 很少的钱

        # 尝试买 50% 仓位的茅台 (100元/股)
        weights = pd.DataFrame({
            "ts_code": ["600519.SH"],
            "weight": [0.5],
        })
        result = t.sync_position(weights, trade_date="2024-01-02")

        pos = t.get_current_positions()
        if not pos.empty:
            # 应该只买了能买到的部分
            vol = pos.iloc[0]["volume"]
            assert vol % LOT_SIZE == 0
            assert vol * 100 < 5000  # 买不了太多

    def test_reject_when_too_poor(self, db_with_prices):
        """完全买不起时不产生持仓。"""
        t = PaperTrader(db_with_prices, account_name="broke")
        t.connect(initial_capital=50)  # 50元买不起任何股

        weights = pd.DataFrame({
            "ts_code": ["600519.SH"],
            "weight": [0.5],
        })
        result = t.sync_position(weights, trade_date="2024-01-02")
        # 目标股数 = round_to_lot(0.5 * 50 / 100) = 0，不产生交易
        assert result["success"] == 0

        pos = t.get_current_positions()
        assert pos.empty

        # 现金不变
        info = t.get_account_info()
        assert info["available_cash"] == 50


# ============================================================
# 费用计算
# ============================================================

class TestFeeCalculation:

    def test_buy_fees(self, trader):
        """买入费用计算。"""
        fees = trader._calc_fees(100_000, "BUY")
        expected_commission = max(100_000 * 0.00075, MIN_COMMISSION)
        assert fees["commission"] == round(expected_commission, 2)
        assert fees["stamp_tax"] == 0  # 买入无印花税
        assert fees["total_cost"] == fees["commission"]

    def test_sell_fees(self, trader):
        """卖出费用计算。"""
        fees = trader._calc_fees(100_000, "SELL")
        expected_commission = max(100_000 * 0.00075, MIN_COMMISSION)
        expected_stamp_tax = 100_000 * 0.001
        assert fees["commission"] == round(expected_commission, 2)
        assert fees["stamp_tax"] == round(expected_stamp_tax, 2)
        assert fees["total_cost"] == round(expected_commission + expected_stamp_tax, 2)

    def test_min_commission(self, trader):
        """最低佣金5元。"""
        fees = trader._calc_fees(1000, "BUY")  # 1000 * 0.00075 = 0.75 < 5
        assert fees["commission"] == MIN_COMMISSION

    def test_cash_reduced_by_fees(self, trader):
        """交易后现金扣除费用。"""
        initial_cash = trader.get_account_info()["available_cash"]

        weights = pd.DataFrame({
            "ts_code": ["000001.SZ"],
            "weight": [0.3],
        })
        trader.sync_position(weights, trade_date="2024-01-02")

        pos = trader.get_current_positions()
        vol = pos.iloc[0]["volume"]
        # 成交金额 + 费用应该被扣除
        remaining = trader.get_account_info()["available_cash"]
        assert remaining < initial_cash


# ============================================================
# 回放模式
# ============================================================

class TestReplayMode:

    def test_replay_basic(self, db_with_prices):
        """基本回放流程。"""
        t = PaperTrader(db_with_prices, account_name="replay_test")
        t.connect(initial_capital=1_000_000)

        signals = {
            "2024-01-02": pd.DataFrame({
                "ts_code": ["000001.SZ", "000002.SZ"],
                "weight": [0.4, 0.4],
            }),
        }

        t.replay(signals, "2024-01-02", "2024-01-10")

        # 应该有净值记录
        nav = t.get_nav_series()
        assert len(nav) > 0

        # 应该有持仓
        pos = t.get_current_positions()
        assert len(pos) == 2

    def test_replay_nav_daily(self, db_with_prices):
        """回放模式每日记录净值。"""
        t = PaperTrader(db_with_prices, account_name="nav_daily")
        t.connect(initial_capital=1_000_000)

        signals = {
            "2024-01-02": pd.DataFrame({
                "ts_code": ["000001.SZ"],
                "weight": [0.5],
            }),
        }

        t.replay(signals, "2024-01-02", "2024-01-10")

        nav = t.get_nav_series()
        # 7个交易日应该有7条净值记录
        assert len(nav) >= 5  # 至少有5天

    def test_replay_with_rebalance(self, db_with_prices):
        """回放模式中间换仓。"""
        t = PaperTrader(db_with_prices, account_name="rebalance_test")
        t.connect(initial_capital=1_000_000)

        signals = {
            "2024-01-02": pd.DataFrame({
                "ts_code": ["000001.SZ"],
                "weight": [0.8],
            }),
            "2024-01-05": pd.DataFrame({
                "ts_code": ["000002.SZ"],
                "weight": [0.8],
            }),
        }

        t.replay(signals, "2024-01-02", "2024-01-10")

        pos = t.get_current_positions()
        codes = pos["ts_code"].tolist()
        # 最终应该只持有换仓后的股票
        assert "000002.SZ" in codes


# ============================================================
# 交易记录
# ============================================================

class TestTransactions:

    def test_transaction_recorded(self, trader):
        """交易记录写入。"""
        weights = pd.DataFrame({
            "ts_code": ["000001.SZ"],
            "weight": [0.3],
        })
        trader.sync_position(weights, trade_date="2024-01-02")

        txns = trader.get_transactions()
        assert len(txns) > 0
        assert "BUY" in txns["direction"].values

    def test_blocked_trade_recorded(self, db):
        """被阻断的交易也记录。"""
        rows = [{
            "ts_code": "777777.SZ",
            "trade_date": date(2024, 1, 2),
            "open": 10.0, "high": 11.0, "low": 10.0, "close": 11.0,
            "volume": 100000, "amount": 11000000,
            "turnover_rate": 2.0, "pct_chg": 10.0,
            "adj_factor": 1.0,
            "is_limit_up": 1, "is_limit_down": 0,
        }]
        db.bulk_insert_daily_price(pd.DataFrame(rows))

        t = PaperTrader(db, account_name="blocked_test")
        t.connect(initial_capital=1_000_000)

        weights = pd.DataFrame({
            "ts_code": ["777777.SZ"],
            "weight": [0.5],
        })
        t.sync_position(weights, trade_date="2024-01-02")

        txns = t.get_transactions()
        assert len(txns) == 1
        assert txns.iloc[0]["reason"] == "limit_up_blocked"


# ============================================================
# 对账
# ============================================================

class TestReconcile:

    def test_reconcile(self, trader):
        """对账功能。"""
        target = pd.DataFrame({
            "ts_code": ["000001.SZ", "000002.SZ"],
            "weight": [0.4, 0.4],
        })
        trader.sync_position(target, trade_date="2024-01-02")

        diff = trader.reconcile(target)
        assert not diff.empty
        assert "diff" in diff.columns


# ============================================================
# 辅助方法
# ============================================================

class TestHelpers:

    def test_round_to_lot(self):
        """整手取整。"""
        assert PaperTrader._round_to_lot(150) == 100
        assert PaperTrader._round_to_lot(250) == 200
        assert PaperTrader._round_to_lot(99) == 0
        assert PaperTrader._round_to_lot(1000) == 1000

    def test_parse_date(self):
        """日期解析。"""
        d = PaperTrader._parse_date("2024-01-02")
        assert d == date(2024, 1, 2)

        d2 = PaperTrader._parse_date(date(2024, 6, 15))
        assert d2 == date(2024, 6, 15)
