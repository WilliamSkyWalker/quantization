"""
数据库模块

负责 MySQL 数据库的建表、读写操作封装。
使用 SQLAlchemy ORM 定义表结构，提供统一的数据存取接口。

表结构：
    - stock_basic: 股票基本信息表（代码、名称、上市日期、行业、市值等）
    - daily_price: 日线行情表（OHLCV + 复权因子）
    - financial_data: 财务数据表（PE、PB、ROE、营收、净利润，按季度）
    - industry_class: 行业分类表（申万一级 + 二级行业）
    - paper_account: 模拟盘账户状态
    - paper_position: 模拟盘当前持仓
    - paper_transaction: 模拟盘交易记录
    - paper_nav: 模拟盘每日净值
"""

import logging
from datetime import datetime
from typing import Optional

import math

import pandas as pd
from sqlalchemy import (
    Column,
    Float,
    Integer,
    String,
    Text,
    Date,
    DateTime,
    UniqueConstraint,
    Index,
    create_engine,
    text,
)
from sqlalchemy.dialects.mysql import MEDIUMTEXT
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from services.config import DB_URL, LOG_LEVEL

# 延迟导入舆情/Polymarket模型（避免循环依赖，在 init_tables 时触发）
_sentiment_models_loaded = False
_polymarket_models_loaded = False

# ============================================================
# 日志配置
# ============================================================

logger = logging.getLogger(__name__)
logger.setLevel(LOG_LEVEL)


_FLOAT_MAX = 3.4e38  # MySQL FLOAT upper bound


def _sanitize_records(records: list[dict]) -> list[dict]:
    """将 records 中的 NaN/NaT/inf/溢出值替换为 None，避免 pymysql 报错。"""
    for rec in records:
        for k, v in rec.items():
            if v is None:
                continue  # None 无需清理
            if isinstance(v, float):
                if math.isnan(v) or math.isinf(v) or abs(v) > _FLOAT_MAX:
                    rec[k] = None
            elif isinstance(v, pd.Timestamp) and pd.isna(v):
                rec[k] = None
    return records


# ============================================================
# ORM 基类
# ============================================================

class Base(DeclarativeBase):
    pass


# ============================================================
# 表定义
# ============================================================

class StockBasic(Base):
    """股票基本信息表"""
    __tablename__ = "stock_basic"

    id = Column(Integer, primary_key=True, autoincrement=True)
    ts_code = Column(String(20), nullable=False, comment="股票代码，如 000001.SZ")
    name = Column(String(50), nullable=False, comment="股票名称")
    market = Column(String(10), comment="市场：主板/创业板/科创板")
    list_date = Column(Date, comment="上市日期")
    delist_date = Column(Date, comment="退市日期，NULL表示未退市")
    is_st = Column(Integer, default=0, comment="是否ST：0=否，1=是")
    total_share = Column(Float, comment="总股本（万股）")
    float_share = Column(Float, comment="流通股本（万股）")
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    __table_args__ = (
        UniqueConstraint("ts_code", name="uq_stock_basic_ts_code"),
        Index("idx_stock_basic_list_date", "list_date"),
    )


class DailyPrice(Base):
    """日线行情表（存储未复权价格 + 复权因子，查询时动态计算前复权）"""
    __tablename__ = "daily_price"

    id = Column(Integer, primary_key=True, autoincrement=True)
    ts_code = Column(String(20), nullable=False, comment="股票代码")
    trade_date = Column(Date, nullable=False, comment="交易日期")
    open = Column(Float, comment="开盘价")
    high = Column(Float, comment="最高价")
    low = Column(Float, comment="最低价")
    close = Column(Float, comment="收盘价")
    volume = Column(Float, comment="成交量（手）")
    amount = Column(Float, comment="成交额（千元）")
    turnover_rate = Column(Float, comment="换手率（%）")
    pct_chg = Column(Float, comment="涨跌幅（%）")
    adj_factor = Column(Float, comment="复权因子")
    dv_ttm = Column(Float, comment="近12个月股息率（%）")
    pe_ttm = Column(Float, comment="滚动市盈率TTM")
    pb = Column(Float, comment="市净率")
    ps_ttm = Column(Float, comment="滚动市销率TTM")
    total_mv = Column(Float, comment="总市值（万元）")
    circ_mv = Column(Float, comment="流通市值（万元）")
    turnover_rate_f = Column(Float, comment="换手率（自由流通股本）")
    volume_ratio = Column(Float, comment="量比")
    is_limit_up = Column(Integer, default=0, comment="是否涨停：0=否，1=是")
    is_limit_down = Column(Integer, default=0, comment="是否跌停：0=否，1=是")
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    __table_args__ = (
        UniqueConstraint("ts_code", "trade_date", name="uq_daily_ts_code_date"),
        Index("idx_daily_trade_date", "trade_date"),
        Index("idx_daily_ts_code", "ts_code"),
    )


class FinancialData(Base):
    """财务数据表（季度频率）"""
    __tablename__ = "financial_data"

    id = Column(Integer, primary_key=True, autoincrement=True)
    ts_code = Column(String(20), nullable=False, comment="股票代码")
    ann_date = Column(Date, nullable=False, comment="公告日期（防止未来函数）")
    end_date = Column(Date, nullable=False, comment="报告期（如 2024-03-31）")
    pe_ttm = Column(Float, comment="市盈率TTM")
    pb = Column(Float, comment="市净率")
    roe_ttm = Column(Float, comment="ROE_TTM（%）")
    gross_margin = Column(Float, comment="毛利率（%）")
    revenue = Column(Float, comment="营业收入（元）")
    net_profit = Column(Float, comment="净利润（元）")
    bps = Column(Float, comment="每股净资产（元）")
    total_mv = Column(Float, comment="总市值（万元）")
    circ_mv = Column(Float, comment="流通市值（万元）")
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    __table_args__ = (
        UniqueConstraint("ts_code", "end_date", name="uq_financial_ts_code_end_date"),
        Index("idx_financial_ann_date", "ann_date"),
        Index("idx_financial_ts_code", "ts_code"),
    )


class IndustryClass(Base):
    """行业分类表（申万一级 + 二级行业）"""
    __tablename__ = "industry_class"

    id = Column(Integer, primary_key=True, autoincrement=True)
    ts_code = Column(String(20), nullable=False, comment="股票代码")
    industry_code = Column(String(20), comment="行业代码")
    industry_name = Column(String(50), nullable=False, comment="行业名称（申万一级）")
    l2_industry_code = Column(String(20), comment="二级行业代码")
    l2_industry_name = Column(String(50), comment="行业名称（申万二级）")
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    __table_args__ = (
        UniqueConstraint("ts_code", name="uq_industry_ts_code"),
        Index("idx_industry_name", "industry_name"),
    )


# ============================================================
# 商品期货价格表
# ============================================================

class CommodityPrice(Base):
    """商品期货主力合约日线行情"""
    __tablename__ = "commodity_price"

    id = Column(Integer, primary_key=True, autoincrement=True)
    commodity_code = Column(String(10), nullable=False, comment="品种代码，如 AU, CU")
    trade_date = Column(Date, nullable=False, comment="交易日期")
    ts_code = Column(String(20), comment="实际合约代码，如 AU2412.SHF")
    open = Column(Float, comment="开盘价")
    high = Column(Float, comment="最高价")
    low = Column(Float, comment="最低价")
    close = Column(Float, comment="收盘价")
    settle = Column(Float, comment="结算价")
    volume = Column(Float, comment="成交量（手）")
    amount = Column(Float, comment="成交额（万元）")
    oi = Column(Float, comment="持仓量（手）")
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    __table_args__ = (
        UniqueConstraint("commodity_code", "trade_date", name="uq_commodity_code_date"),
        Index("idx_commodity_trade_date", "trade_date"),
        Index("idx_commodity_code", "commodity_code"),
    )


# ============================================================
# 宏观经济指标表
# ============================================================

class MacroIndicator(Base):
    """宏观经济指标通用 KV 表（SHIBOR、CPI、PMI 等）"""
    __tablename__ = "macro_indicator"

    id = Column(Integer, primary_key=True, autoincrement=True)
    indicator_code = Column(String(30), nullable=False, comment="指标代码，如 SHIBOR_3M, CPI_YOY")
    report_date = Column(Date, nullable=False, comment="报告日期（月度数据存月末）")
    value = Column(Float, comment="指标值")
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    __table_args__ = (
        UniqueConstraint("indicator_code", "report_date", name="uq_macro_code_date"),
        Index("idx_macro_indicator_code", "indicator_code"),
        Index("idx_macro_report_date", "report_date"),
    )


# ============================================================
# 模拟盘表定义
# ============================================================

class PaperAccount(Base):
    """模拟盘账户"""
    __tablename__ = "paper_account"

    id = Column(Integer, primary_key=True, autoincrement=True)
    account_name = Column(String(50), nullable=False, comment="账户名称，如 default")
    initial_capital = Column(Float, nullable=False, comment="初始资金")
    cash = Column(Float, nullable=False, comment="当前可用现金")
    total_assets = Column(Float, nullable=False, comment="总资产（现金+持仓市值）")
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    __table_args__ = (
        UniqueConstraint("account_name", name="uq_paper_account_name"),
    )


class PaperPosition(Base):
    """模拟盘持仓"""
    __tablename__ = "paper_position"

    id = Column(Integer, primary_key=True, autoincrement=True)
    account_name = Column(String(50), nullable=False, comment="账户名称")
    ts_code = Column(String(20), nullable=False, comment="股票代码")
    volume = Column(Integer, nullable=False, comment="持仓股数")
    cost_basis = Column(Float, nullable=False, comment="持仓成本价（含佣金均摊）")
    current_price = Column(Float, comment="最新价格")
    market_value = Column(Float, comment="持仓市值")
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    __table_args__ = (
        UniqueConstraint("account_name", "ts_code", name="uq_paper_pos_acct_code"),
        Index("idx_paper_pos_account", "account_name"),
    )


class PaperTransaction(Base):
    """模拟盘交易记录"""
    __tablename__ = "paper_transaction"

    id = Column(Integer, primary_key=True, autoincrement=True)
    account_name = Column(String(50), nullable=False, comment="账户名称")
    trade_date = Column(Date, nullable=False, comment="交易日期")
    ts_code = Column(String(20), nullable=False, comment="股票代码")
    direction = Column(String(4), nullable=False, comment="BUY 或 SELL")
    target_volume = Column(Integer, comment="目标交易股数")
    filled_volume = Column(Integer, nullable=False, comment="实际成交股数")
    price = Column(Float, nullable=False, comment="成交价格（收盘价±滑点）")
    amount = Column(Float, nullable=False, comment="成交金额（不含费用）")
    commission = Column(Float, nullable=False, comment="佣金")
    stamp_tax = Column(Float, nullable=False, default=0, comment="印花税（仅卖出）")
    slippage_cost = Column(Float, nullable=False, default=0, comment="滑点成本")
    total_cost = Column(Float, nullable=False, comment="总交易费用")
    reason = Column(String(100), comment="交易原因")
    created_at = Column(DateTime, default=datetime.now)

    __table_args__ = (
        Index("idx_paper_txn_date", "account_name", "trade_date"),
        Index("idx_paper_txn_code", "ts_code"),
    )


class IndustryFactorConfig(Base):
    """行业因子权重配置表"""
    __tablename__ = "industry_factor_config"

    id = Column(Integer, primary_key=True, autoincrement=True)
    industry_name = Column(String(50), nullable=False, comment="行业名称，__DEFAULT__=默认")
    factor_name = Column(String(30), nullable=False, comment="因子名称")
    weight = Column(Float, nullable=False, default=1.0, comment="因子权重（带符号，反向因子为负）")
    description = Column(String(200), comment="配置说明")
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    __table_args__ = (
        UniqueConstraint("industry_name", "factor_name", name="uq_industry_factor"),
        Index("idx_ind_factor_industry", "industry_name"),
    )


class PaperNav(Base):
    """模拟盘每日净值"""
    __tablename__ = "paper_nav"

    id = Column(Integer, primary_key=True, autoincrement=True)
    account_name = Column(String(50), nullable=False, comment="账户名称")
    trade_date = Column(Date, nullable=False, comment="交易日期")
    cash = Column(Float, nullable=False, comment="当日现金")
    market_value = Column(Float, nullable=False, comment="当日持仓市值")
    total_assets = Column(Float, nullable=False, comment="当日总资产")
    nav = Column(Float, nullable=False, comment="单位净值（总资产/初始资金）")
    daily_pnl = Column(Float, comment="当日盈亏")
    daily_return = Column(Float, comment="当日收益率")
    n_holdings = Column(Integer, comment="持仓股票数")
    created_at = Column(DateTime, default=datetime.now)

    __table_args__ = (
        UniqueConstraint("account_name", "trade_date", name="uq_paper_nav_acct_date"),
        Index("idx_paper_nav_date", "trade_date"),
    )


# ============================================================
# 选股结果持久化表
# ============================================================

class SelectionResult(Base):
    """选股结果持久化表（每日一条，支持历史查看）"""
    __tablename__ = "selection_result"

    id = Column(Integer, primary_key=True, autoincrement=True)
    date = Column(Date, nullable=False, comment="选股日期")
    total = Column(Integer, comment="参与打分的股票总数")
    top_stocks = Column(Text, comment="Top N 选股结果 JSON")
    by_industry = Column(MEDIUMTEXT, comment="分行业选股结果 JSON")
    created_at = Column(DateTime, default=datetime.now, comment="首次计算时间")
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now, comment="最近更新时间")

    __table_args__ = (
        UniqueConstraint("date", name="uq_selection_result_date"),
        Index("idx_selection_result_date", "date"),
    )


# ============================================================
# 因子快照表
# ============================================================

class BacktestResult(Base):
    """回测结果持久化表"""
    __tablename__ = "backtest_result"

    id = Column(Integer, primary_key=True, autoincrement=True)
    market = Column(String(10), default="cn", comment="市场: cn 或 us")
    strategy_type = Column(String(20), comment="策略类型: alpha, beta, baseline")
    start_date = Column(Date, nullable=False, comment="回测开始日期")
    end_date = Column(Date, nullable=False, comment="回测结束日期")
    summary = Column(Text, comment="绩效指标 JSON dict")
    nav = Column(MEDIUMTEXT, comment="净值曲线 JSON [{date, nav}]")
    benchmark = Column(MEDIUMTEXT, comment="基准净值 JSON [{date, nav}]")
    trades = Column(MEDIUMTEXT, comment="交易记录 JSON")
    monthly = Column(Text, comment="月度收益 JSON")
    drawdown = Column(MEDIUMTEXT, comment="回撤序列 JSON [{date, drawdown}]")
    attribution = Column(Text, comment="行业归因 JSON")
    holdings = Column(Text, comment="最新持仓 JSON")
    created_at = Column(DateTime, default=datetime.now)

    __table_args__ = (
        Index("idx_backtest_result_dates", "start_date", "end_date"),
    )


class FactorSnapshot(Base):
    """因子快照表（选股时同步存储全量股票的因子值，供因子明细接口秒查）"""
    __tablename__ = "factor_snapshot"

    id = Column(Integer, primary_key=True, autoincrement=True)
    date = Column(Date, nullable=False, comment="选股日期")
    ts_code = Column(String(20), nullable=False, comment="股票代码")
    score = Column(Float, comment="综合得分")
    factors = Column(Text, comment="因子值 JSON {factor_name: value}")
    created_at = Column(DateTime, default=datetime.now)

    __table_args__ = (
        UniqueConstraint("date", "ts_code", name="uq_factor_snapshot_date_code"),
        Index("idx_factor_snapshot_date", "date"),
        Index("idx_factor_snapshot_code", "ts_code"),
    )


# ============================================================
# 券商研报表
# ============================================================

class ResearchReport(Base):
    """券商研报评级表"""
    __tablename__ = "research_report"

    id = Column(Integer, primary_key=True, autoincrement=True)
    ts_code = Column(String(20), nullable=False, comment="股票代码")
    stock_name = Column(String(50), comment="股票名称")
    institution = Column(String(100), nullable=False, comment="研究机构")
    analyst = Column(String(100), comment="分析师")
    title = Column(String(500), comment="研报标题")
    rating = Column(String(20), comment="最新评级（买入/增持/中性/减持/卖出）")
    rating_score = Column(Float, comment="评级分数 1~5")
    report_date = Column(Date, nullable=False, comment="研报日期")
    info_code = Column(String(50), comment="东方财富研报 infoCode，用于拼接原文链接")
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    __table_args__ = (
        UniqueConstraint("ts_code", "institution", "report_date", "title",
                         name="uq_research_report"),
        Index("idx_research_ts_code", "ts_code"),
        Index("idx_research_report_date", "report_date"),
        Index("idx_research_institution", "institution"),
    )


# ============================================================
# 美股表定义
# ============================================================

class USStockBasic(Base):
    """美股基本信息表"""
    __tablename__ = "us_stock_basic"

    id = Column(Integer, primary_key=True, autoincrement=True)
    ticker = Column(String(20), nullable=False, comment="股票代码 (AAPL)")
    name = Column(String(200), comment="公司名")
    exchange = Column(String(20), comment="交易所 (NYSE/NASDAQ)")
    sector = Column(String(100), comment="GICS 行业大类")
    industry = Column(String(200), comment="GICS 子行业")
    ipo_date = Column(Date, comment="IPO 日期")
    market_cap = Column(Float, comment="市值")
    country = Column(String(10), comment="国家")
    is_active = Column(Integer, default=1, comment="是否活跃 (1/0)")
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    __table_args__ = (
        UniqueConstraint("ticker", name="uq_us_stock_basic_ticker"),
        Index("idx_us_stock_sector", "sector"),
    )


class USDailyPrice(Base):
    """美股日线行情表"""
    __tablename__ = "us_daily_price"

    id = Column(Integer, primary_key=True, autoincrement=True)
    ticker = Column(String(20), nullable=False, comment="股票代码")
    trade_date = Column(Date, nullable=False, comment="交易日期")
    open = Column(Float, comment="开盘价")
    high = Column(Float, comment="最高价")
    low = Column(Float, comment="最低价")
    close = Column(Float, comment="收盘价")
    adj_close = Column(Float, comment="复权收盘价")
    volume = Column(Float, comment="成交量")
    change_pct = Column(Float, comment="涨跌幅 %")
    vwap = Column(Float, comment="VWAP 成交量加权均价")
    unadjusted_volume = Column(Float, comment="未调整成交量")
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    __table_args__ = (
        UniqueConstraint("ticker", "trade_date", name="uq_us_daily_ticker_date"),
        Index("idx_us_daily_trade_date", "trade_date"),
        Index("idx_us_daily_ticker", "ticker"),
    )


class USFinancialData(Base):
    """美股财务数据表（季报，含利润表/资产负债表/现金流关键字段）"""
    __tablename__ = "us_financial_data"

    id = Column(Integer, primary_key=True, autoincrement=True)
    ticker = Column(String(20), nullable=False, comment="股票代码")
    period = Column(String(10), nullable=False, comment="报告期 (2024-Q3)")
    date = Column(Date, comment="报告期末日")
    filing_date = Column(Date, comment="提交日（防前视偏差）")
    # --- Income Statement ---
    revenue = Column(Float, comment="营收")
    cost_of_revenue = Column(Float, comment="营业成本")
    gross_profit = Column(Float, comment="毛利润")
    operating_income = Column(Float, comment="营业利润")
    net_income = Column(Float, comment="净利润")
    eps = Column(Float, comment="每股收益")
    eps_diluted = Column(Float, comment="稀释每股收益")
    ebitda = Column(Float, comment="EBITDA")
    gross_margin = Column(Float, comment="毛利率")
    operating_margin = Column(Float, comment="营业利润率")
    net_margin = Column(Float, comment="净利率")
    rd_expenses = Column(Float, comment="研发费用")
    sga_expenses = Column(Float, comment="销售管理费用")
    weighted_avg_shares = Column(Float, comment="加权平均股数")
    # --- Balance Sheet ---
    total_assets = Column(Float, comment="总资产")
    total_equity = Column(Float, comment="股东权益")
    total_debt = Column(Float, comment="总负债")
    total_current_assets = Column(Float, comment="流动资产")
    total_current_liabilities = Column(Float, comment="流动负债")
    cash_and_equivalents = Column(Float, comment="现金及等价物")
    net_receivables = Column(Float, comment="应收账款")
    inventory = Column(Float, comment="存货")
    long_term_debt = Column(Float, comment="长期负债")
    retained_earnings = Column(Float, comment="留存收益")
    # --- Cash Flow ---
    operating_cash_flow = Column(Float, comment="经营活动现金流")
    capital_expenditure = Column(Float, comment="资本支出")
    free_cash_flow = Column(Float, comment="自由现金流")
    dividends_paid = Column(Float, comment="已付股息")
    share_repurchased = Column(Float, comment="股票回购金额")
    # --- Ratios ---
    roe = Column(Float, comment="ROE")
    pe_ratio = Column(Float, comment="PE")
    pb_ratio = Column(Float, comment="PB")
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    __table_args__ = (
        UniqueConstraint("ticker", "period", name="uq_us_financial_ticker_period"),
        Index("idx_us_financial_ticker", "ticker"),
        Index("idx_us_financial_date", "date"),
    )


class USIndustryClass(Base):
    """美股 GICS 行业分类表"""
    __tablename__ = "us_industry_class"

    id = Column(Integer, primary_key=True, autoincrement=True)
    ticker = Column(String(20), nullable=False, comment="股票代码")
    sector = Column(String(100), comment="GICS Sector")
    industry = Column(String(200), comment="GICS Industry")
    sub_industry = Column(String(200), comment="GICS Sub-Industry")
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    __table_args__ = (
        UniqueConstraint("ticker", name="uq_us_industry_ticker"),
        Index("idx_us_industry_sector", "sector"),
    )


class USIndexDaily(Base):
    """美股指数日线表"""
    __tablename__ = "us_index_daily"

    id = Column(Integer, primary_key=True, autoincrement=True)
    index_code = Column(String(20), nullable=False, comment="^GSPC, ^IXIC, ^DJI")
    trade_date = Column(Date, nullable=False, comment="交易日期")
    open = Column(Float, comment="开盘价")
    high = Column(Float, comment="最高价")
    low = Column(Float, comment="最低价")
    close = Column(Float, comment="收盘价")
    volume = Column(Float, comment="成交量")
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    __table_args__ = (
        UniqueConstraint("index_code", "trade_date", name="uq_us_index_code_date"),
        Index("idx_us_index_trade_date", "trade_date"),
        Index("idx_us_index_code", "index_code"),
    )


class USMacroIndicator(Base):
    """美股宏观经济指标表（FRED）"""
    __tablename__ = "us_macro_indicator"

    id = Column(Integer, primary_key=True, autoincrement=True)
    indicator_code = Column(String(50), nullable=False, comment="FRED series ID")
    report_date = Column(Date, nullable=False, comment="报告日期")
    value = Column(Float, comment="指标值")
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    __table_args__ = (
        UniqueConstraint("indicator_code", "report_date", name="uq_us_macro_code_date"),
        Index("idx_us_macro_indicator_code", "indicator_code"),
        Index("idx_us_macro_report_date", "report_date"),
    )


class USCommodityPrice(Base):
    """美股商品期货表"""
    __tablename__ = "us_commodity_price"

    id = Column(Integer, primary_key=True, autoincrement=True)
    symbol = Column(String(20), nullable=False, comment="GCUSD (金), CLUSD (油) 等")
    trade_date = Column(Date, nullable=False, comment="交易日期")
    open = Column(Float, comment="开盘价")
    high = Column(Float, comment="最高价")
    low = Column(Float, comment="最低价")
    close = Column(Float, comment="收盘价")
    volume = Column(Float, comment="成交量")
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    __table_args__ = (
        UniqueConstraint("symbol", "trade_date", name="uq_us_commodity_symbol_date"),
        Index("idx_us_commodity_trade_date", "trade_date"),
        Index("idx_us_commodity_symbol", "symbol"),
    )


class USAnalystRecommendation(Base):
    """美股分析师评级表"""
    __tablename__ = "us_analyst_recommendation"

    id = Column(Integer, primary_key=True, autoincrement=True)
    ticker = Column(String(20), nullable=False, comment="股票代码")
    date = Column(Date, nullable=False, comment="日期")
    analyst_company = Column(String(200), comment="券商名")
    analyst_name = Column(String(200), comment="分析师")
    rating = Column(String(50), comment="Buy/Hold/Sell 等")
    price_target = Column(Float, comment="目标价")
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    __table_args__ = (
        UniqueConstraint("ticker", "date", "analyst_company",
                         name="uq_us_analyst_ticker_date_company"),
        Index("idx_us_analyst_ticker", "ticker"),
        Index("idx_us_analyst_date", "date"),
    )


class USSecFiling(Base):
    """美股 SEC 公告表"""
    __tablename__ = "us_sec_filing"

    id = Column(Integer, primary_key=True, autoincrement=True)
    ticker = Column(String(20), nullable=False, comment="股票代码")
    filing_date = Column(Date, nullable=False, comment="提交日期")
    type = Column(String(20), comment="10-K, 10-Q, 8-K 等")
    title = Column(String(500), comment="标题")
    url = Column(String(500), comment="链接")
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    __table_args__ = (
        UniqueConstraint("ticker", "filing_date", "type",
                         name="uq_us_sec_ticker_date_type"),
        Index("idx_us_sec_ticker", "ticker"),
        Index("idx_us_sec_date", "filing_date"),
    )


class USCorporateAction(Base):
    """美股公司行动表（分红/拆股）"""
    __tablename__ = "us_corporate_action"

    id = Column(Integer, primary_key=True, autoincrement=True)
    ticker = Column(String(20), nullable=False, comment="股票代码")
    date = Column(Date, nullable=False, comment="生效日")
    action_type = Column(String(20), comment="dividend / split")
    label = Column(String(200), comment="描述")
    value = Column(Float, comment="分红金额 or 拆股比例")
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    __table_args__ = (
        UniqueConstraint("ticker", "date", "action_type",
                         name="uq_us_corp_ticker_date_type"),
        Index("idx_us_corp_ticker", "ticker"),
        Index("idx_us_corp_date", "date"),
    )


class USEarningsSurprise(Base):
    """美股盈利惊喜表（FMP API: actual vs estimated EPS）"""
    __tablename__ = "us_earnings_surprise"

    id = Column(Integer, primary_key=True, autoincrement=True)
    ticker = Column(String(20), nullable=False, comment="股票代码")
    date = Column(Date, nullable=False, comment="财报公布日")
    actual_eps = Column(Float, comment="实际 EPS")
    estimated_eps = Column(Float, comment="市场预期 EPS")
    surprise = Column(Float, comment="惊喜 = actual - estimated")
    surprise_pct = Column(Float, comment="惊喜百分比 = surprise / |estimated|")
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    __table_args__ = (
        UniqueConstraint("ticker", "date", name="uq_us_earnings_surprise_ticker_date"),
        Index("idx_us_earnings_surprise_ticker", "ticker"),
        Index("idx_us_earnings_surprise_date", "date"),
    )


class USEpsEstimate(Base):
    """美股 EPS 共识预期表（FMP API: analyst estimates forward）"""
    __tablename__ = "us_eps_estimate"

    id = Column(Integer, primary_key=True, autoincrement=True)
    ticker = Column(String(20), nullable=False, comment="股票代码")
    date = Column(Date, nullable=False, comment="预期对应的财报期末日")
    eps_avg = Column(Float, comment="EPS 共识均值")
    eps_low = Column(Float, comment="EPS 最低预期")
    eps_high = Column(Float, comment="EPS 最高预期")
    num_analysts = Column(Integer, comment="覆盖分析师数")
    revenue_avg = Column(Float, comment="Revenue 共识均值")
    net_income_avg = Column(Float, comment="Net Income 共识均值")
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    __table_args__ = (
        UniqueConstraint("ticker", "date", name="uq_us_eps_estimate_ticker_date"),
        Index("idx_us_eps_estimate_ticker", "ticker"),
        Index("idx_us_eps_estimate_date", "date"),
    )


class USInsiderTrade(Base):
    """美股内部人交易表（FMP API: SEC Form 4）"""
    __tablename__ = "us_insider_trade"

    id = Column(Integer, primary_key=True, autoincrement=True)
    ticker = Column(String(20), nullable=False, comment="股票代码")
    filing_date = Column(DateTime, comment="SEC 提交日期")
    transaction_date = Column(Date, nullable=False, comment="交易日期")
    reporting_name = Column(String(200), comment="报告人姓名")
    type_of_owner = Column(String(100), comment="officer/director/10% owner")
    transaction_type = Column(String(20), comment="P-Purchase/S-Sale/M-Exempt 等")
    acquisition_or_disposition = Column(String(5), comment="A=买入/D=卖出")
    securities_transacted = Column(Float, comment="交易股数")
    price = Column(Float, comment="交易价格")
    securities_owned = Column(Float, comment="交易后持有股数")
    security_name = Column(String(200), comment="证券名称 (Common Stock / RSU 等)")
    form_type = Column(String(10), comment="表格类型 (4/4A)")
    link = Column(String(500), comment="SEC 文件链接")
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    __table_args__ = (
        UniqueConstraint("ticker", "transaction_date", "reporting_name", "transaction_type",
                         name="uq_us_insider_ticker_date_name_type"),
        Index("idx_us_insider_ticker", "ticker"),
        Index("idx_us_insider_date", "transaction_date"),
    )


class USKeyMetric(Base):
    """美股季度关键指标表（FMP bulk: key-metrics + ratios）"""
    __tablename__ = "us_key_metric"

    id = Column(Integer, primary_key=True, autoincrement=True)
    ticker = Column(String(20), nullable=False, comment="股票代码")
    date = Column(Date, nullable=False, comment="报告期末日")
    period = Column(String(10), comment="Q1/Q2/Q3/Q4/FY")
    calendar_year = Column(String(10), comment="日历年")
    # --- Valuation ---
    market_cap = Column(Float, comment="市值")
    enterprise_value = Column(Float, comment="企业价值")
    pe_ratio = Column(Float, comment="PE")
    pb_ratio = Column(Float, comment="PB")
    ps_ratio = Column(Float, comment="PS")
    price_to_fcf = Column(Float, comment="P/FCF")
    peg_ratio = Column(Float, comment="PEG")
    ev_to_ebitda = Column(Float, comment="EV/EBITDA")
    ev_to_sales = Column(Float, comment="EV/Sales")
    ev_to_fcf = Column(Float, comment="EV/FCF")
    ev_to_ocf = Column(Float, comment="EV/OCF")
    earnings_yield = Column(Float, comment="盈利收益率")
    fcf_yield = Column(Float, comment="自由现金流收益率")
    dividend_yield = Column(Float, comment="股息率")
    dividend_per_share = Column(Float, comment="每股股息")
    payout_ratio = Column(Float, comment="分红比例")
    # --- Profitability ---
    roe = Column(Float, comment="ROE")
    roa = Column(Float, comment="ROA")
    roic = Column(Float, comment="ROIC")
    roce = Column(Float, comment="ROCE")
    gross_profit_margin = Column(Float, comment="毛利率")
    operating_profit_margin = Column(Float, comment="营业利润率")
    net_profit_margin = Column(Float, comment="净利率")
    pretax_profit_margin = Column(Float, comment="税前利润率")
    effective_tax_rate = Column(Float, comment="有效税率")
    # --- Per Share ---
    revenue_per_share = Column(Float, comment="每股营收")
    net_income_per_share = Column(Float, comment="每股净利润")
    free_cash_flow_per_share = Column(Float, comment="每股自由现金流")
    operating_cash_flow_per_share = Column(Float, comment="每股经营现金流")
    book_value_per_share = Column(Float, comment="每股净资产")
    tangible_book_value_per_share = Column(Float, comment="每股有形净资产")
    cash_per_share = Column(Float, comment="每股现金")
    shareholders_equity_per_share = Column(Float, comment="每股股东权益")
    interest_debt_per_share = Column(Float, comment="每股有息负债")
    capex_per_share = Column(Float, comment="每股资本支出")
    # --- Leverage & Liquidity ---
    current_ratio = Column(Float, comment="流动比率")
    quick_ratio = Column(Float, comment="速动比率")
    cash_ratio = Column(Float, comment="现金比率")
    debt_to_equity = Column(Float, comment="负债/股东权益")
    debt_to_assets = Column(Float, comment="负债/总资产")
    debt_to_capitalization = Column(Float, comment="总负债/资本化")
    lt_debt_to_capitalization = Column(Float, comment="长期负债/资本化")
    lt_debt_to_total_asset = Column(Float, comment="长期负债/总资产")
    debt_ratio = Column(Float, comment="负债率")
    equity_multiplier = Column(Float, comment="权益乘数")
    interest_coverage = Column(Float, comment="利息覆盖率")
    net_debt_to_ebitda = Column(Float, comment="净负债/EBITDA")
    # --- Efficiency & Cycle ---
    inventory_turnover = Column(Float, comment="存货周转率")
    receivables_turnover = Column(Float, comment="应收周转率")
    payables_turnover = Column(Float, comment="应付周转率")
    fixed_asset_turnover = Column(Float, comment="固定资产周转率")
    asset_turnover = Column(Float, comment="总资产周转率")
    operating_cycle = Column(Float, comment="营业周期(天)")
    cash_conversion_cycle = Column(Float, comment="现金转换周期(天)")
    days_sales_outstanding = Column(Float, comment="应收天数")
    days_inventory_outstanding = Column(Float, comment="存货天数")
    days_payables_outstanding = Column(Float, comment="应付天数")
    # --- Capex & SBC ---
    capex_to_revenue = Column(Float, comment="Capex/Revenue")
    capex_to_depreciation = Column(Float, comment="Capex/折旧")
    capex_to_ocf = Column(Float, comment="Capex/经营现金流")
    sbc_to_revenue = Column(Float, comment="股权激励/Revenue")
    # --- Quality & Value ---
    income_quality = Column(Float, comment="收入质量 (OCF/NI)")
    graham_number = Column(Float, comment="格雷厄姆数")
    graham_net_net = Column(Float, comment="格雷厄姆净净值")
    working_capital = Column(Float, comment="营运资本")
    tangible_asset_value = Column(Float, comment="有形资产价值")
    net_current_asset_value = Column(Float, comment="净流动资产价值")
    invested_capital = Column(Float, comment="投入资本")
    average_receivables = Column(Float, comment="平均应收")
    average_payables = Column(Float, comment="平均应付")
    average_inventory = Column(Float, comment="平均存货")
    # --- Expense Ratios ---
    rd_to_revenue = Column(Float, comment="研发/营收")
    sga_to_revenue = Column(Float, comment="SGA/营收")
    intangibles_to_total_assets = Column(Float, comment="无形资产/总资产")
    # --- Cash Flow Ratios (from ratios endpoint) ---
    price_to_ocf = Column(Float, comment="P/OCF")
    ocf_to_sales = Column(Float, comment="OCF/Sales")
    fcf_to_ocf = Column(Float, comment="FCF/OCF")
    cash_flow_coverage = Column(Float, comment="现金流覆盖率")
    cash_flow_to_debt = Column(Float, comment="现金流/负债")
    short_term_coverage = Column(Float, comment="短期覆盖率")
    ebt_per_ebit = Column(Float, comment="EBT/EBIT")
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    __table_args__ = (
        UniqueConstraint("ticker", "date", name="uq_us_key_metric_ticker_date"),
        Index("idx_us_key_metric_ticker", "ticker"),
        Index("idx_us_key_metric_date", "date"),
    )


class USOptionsFlow(Base):
    """美股期权异常活动表（Unusual Whales: flow-alerts）"""
    __tablename__ = "us_options_flow"

    id = Column(Integer, primary_key=True, autoincrement=True)
    ticker = Column(String(20), nullable=False, comment="股票代码")
    alert_id = Column(String(50), comment="UW alert ID")
    date = Column(DateTime, nullable=False, comment="时间戳")
    contract_type = Column(String(10), comment="call/put")
    strike = Column(Float, comment="行权价")
    expiry = Column(Date, comment="到期日")
    premium = Column(Float, comment="权利金总额")
    volume = Column(Integer, comment="成交量")
    open_interest = Column(Integer, comment="未平仓合约")
    sentiment = Column(String(20), comment="bullish/bearish/neutral")
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    __table_args__ = (
        UniqueConstraint("alert_id", name="uq_us_options_flow_alert_id"),
        Index("idx_us_options_flow_ticker", "ticker"),
        Index("idx_us_options_flow_date", "date"),
    )


class USDarkPool(Base):
    """美股暗池交易表（Unusual Whales: darkpool）"""
    __tablename__ = "us_dark_pool"

    id = Column(Integer, primary_key=True, autoincrement=True)
    ticker = Column(String(20), nullable=False, comment="股票代码")
    date = Column(DateTime, nullable=False, comment="时间戳")
    price = Column(Float, comment="成交价")
    size = Column(Float, comment="成交量（股）")
    notional = Column(Float, comment="名义金额")
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    __table_args__ = (
        Index("idx_us_dark_pool_ticker", "ticker"),
        Index("idx_us_dark_pool_date", "date"),
    )


class USCongressTrade(Base):
    """美股国会交易表（Unusual Whales: congress）"""
    __tablename__ = "us_congress_trade"

    id = Column(Integer, primary_key=True, autoincrement=True)
    ticker = Column(String(20), comment="股票代码")
    politician = Column(String(200), comment="议员姓名")
    office = Column(String(100), comment="职位")
    transaction_date = Column(Date, comment="交易日期")
    disclosure_date = Column(Date, comment="披露日期")
    trade_type = Column(String(50), comment="purchase/sale")
    amount = Column(String(50), comment="金额区间")
    asset_description = Column(String(500), comment="资产描述")
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    __table_args__ = (
        UniqueConstraint("ticker", "politician", "transaction_date", "trade_type",
                         name="uq_us_congress_trade"),
        Index("idx_us_congress_ticker", "ticker"),
        Index("idx_us_congress_date", "transaction_date"),
    )


class USNews(Base):
    """美股新闻表（Massive + Unusual Whales）"""
    __tablename__ = "us_news"

    id = Column(Integer, primary_key=True, autoincrement=True)
    source = Column(String(20), nullable=False, comment="massive/uw")
    title = Column(String(1000), comment="标题")
    url = Column(String(500), comment="链接")
    published_at = Column(DateTime, comment="发布时间")
    tickers = Column(String(500), comment="相关 ticker（逗号分隔）")
    summary = Column(Text, comment="摘要")
    sentiment = Column(String(20), comment="positive/negative/neutral")
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    __table_args__ = (
        UniqueConstraint("source", "url", name="uq_us_news_source_url"),
        Index("idx_us_news_published", "published_at"),
    )


class USDailyRatio(Base):
    """美股日频估值比率表（Fiscal.ai: daily-ratios）"""
    __tablename__ = "us_daily_ratio"

    id = Column(Integer, primary_key=True, autoincrement=True)
    ticker = Column(String(20), nullable=False, comment="股票代码")
    date = Column(Date, nullable=False, comment="日期")
    pe_ratio = Column(Float, comment="PE")
    pb_ratio = Column(Float, comment="PB")
    ps_ratio = Column(Float, comment="PS")
    ev_to_ebitda = Column(Float, comment="EV/EBITDA")
    dividend_yield = Column(Float, comment="股息率")
    market_cap = Column(Float, comment="市值")
    enterprise_value = Column(Float, comment="企业价值")
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    __table_args__ = (
        UniqueConstraint("ticker", "date", name="uq_us_daily_ratio_ticker_date"),
        Index("idx_us_daily_ratio_ticker", "ticker"),
        Index("idx_us_daily_ratio_date", "date"),
    )


class USShortInterest(Base):
    """美股空头数据表（Massive: short-interest）"""
    __tablename__ = "us_short_interest"

    id = Column(Integer, primary_key=True, autoincrement=True)
    ticker = Column(String(20), nullable=False, comment="股票代码")
    date = Column(Date, nullable=False, comment="日期")
    short_interest = Column(Float, comment="做空股数")
    short_interest_ratio = Column(Float, comment="做空比例（days to cover）")
    short_percent_of_float = Column(Float, comment="做空占流通比例")
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    __table_args__ = (
        UniqueConstraint("ticker", "date", name="uq_us_short_interest_ticker_date"),
        Index("idx_us_short_interest_ticker", "ticker"),
        Index("idx_us_short_interest_date", "date"),
    )


class USLobbyingActivity(Base):
    """美股游说活动表（Quiver: lobbying）"""
    __tablename__ = "us_lobbying"

    id = Column(Integer, primary_key=True, autoincrement=True)
    ticker = Column(String(20), nullable=False, comment="股票代码")
    date = Column(Date, nullable=False, comment="披露日期")
    amount = Column(Float, comment="游说金额")
    client = Column(String(300), comment="客户（委托方）")
    registrant = Column(String(300), comment="游说公司")
    issue = Column(Text, comment="议题分类")
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    __table_args__ = (
        UniqueConstraint("ticker", "date", "registrant", name="uq_us_lobbying"),
        Index("idx_us_lobbying_ticker", "ticker"),
        Index("idx_us_lobbying_date", "date"),
    )


class USGovContract(Base):
    """美股政府合同表（Quiver: govcontracts）"""
    __tablename__ = "us_gov_contract"

    id = Column(Integer, primary_key=True, autoincrement=True)
    ticker = Column(String(20), nullable=False, comment="股票代码")
    year = Column(Integer, nullable=False, comment="财年")
    quarter = Column(Integer, nullable=False, comment="季度")
    amount = Column(Float, comment="合同金额")
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    __table_args__ = (
        UniqueConstraint("ticker", "year", "quarter", name="uq_us_gov_contract"),
        Index("idx_us_gov_contract_ticker", "ticker"),
    )


class USWsbSentiment(Base):
    """美股 WallStreetBets 情绪表（Quiver: wallstreetbets）"""
    __tablename__ = "us_wsb_sentiment"

    id = Column(Integer, primary_key=True, autoincrement=True)
    ticker = Column(String(20), nullable=False, comment="股票代码")
    date = Column(Date, nullable=False, comment="日期")
    mentions = Column(Integer, comment="提及次数")
    rank = Column(Integer, comment="排名")
    sentiment = Column(Float, comment="情绪分数")
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    __table_args__ = (
        UniqueConstraint("ticker", "date", name="uq_us_wsb_sentiment"),
        Index("idx_us_wsb_ticker", "ticker"),
        Index("idx_us_wsb_date", "date"),
    )


class USNewsSentiment(Base):
    """美股新闻情绪表（Alpha Vantage: NEWS_SENTIMENT）"""
    __tablename__ = "us_news_sentiment"

    id = Column(Integer, primary_key=True, autoincrement=True)
    ticker = Column(String(20), nullable=False, comment="股票代码")
    date = Column(Date, nullable=False, comment="日期")
    sentiment_score = Column(Float, comment="ticker 情绪分数 (-1~1)")
    relevance_score = Column(Float, comment="相关性分数 (0~1)")
    article_count = Column(Integer, comment="文章数量")
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    __table_args__ = (
        UniqueConstraint("ticker", "date", name="uq_us_news_sentiment"),
        Index("idx_us_news_sentiment_ticker", "ticker"),
        Index("idx_us_news_sentiment_date", "date"),
    )


class USOptionsSnapshot(Base):
    """美股期权快照表（Alpha Vantage: HISTORICAL_OPTIONS 聚合）"""
    __tablename__ = "us_options_snapshot"

    id = Column(Integer, primary_key=True, autoincrement=True)
    ticker = Column(String(20), nullable=False, comment="股票代码")
    date = Column(Date, nullable=False, comment="日期")
    avg_call_iv = Column(Float, comment="ATM 看涨平均 IV")
    avg_put_iv = Column(Float, comment="ATM 看跌平均 IV")
    iv_skew = Column(Float, comment="IV skew (put_iv - call_iv)")
    put_call_volume_ratio = Column(Float, comment="看跌/看涨成交量比")
    put_call_oi_ratio = Column(Float, comment="看跌/看涨持仓量比")
    total_volume = Column(Integer, comment="总成交量")
    total_open_interest = Column(Integer, comment="总持仓量")
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    __table_args__ = (
        UniqueConstraint("ticker", "date", name="uq_us_options_snapshot"),
        Index("idx_us_options_snapshot_ticker", "ticker"),
        Index("idx_us_options_snapshot_date", "date"),
    )


class Watchlist(Base):
    """自选股表"""
    __tablename__ = "watchlist"

    id = Column(Integer, primary_key=True, autoincrement=True)
    ts_code = Column(String(20), nullable=False, comment="股票代码")
    name = Column(String(50), comment="股票名称（冗余，方便列表展示）")
    notes = Column(String(500), comment="用户备注")
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    __table_args__ = (
        UniqueConstraint("ts_code", name="uq_watchlist_ts_code"),
    )


# ============================================================
# US Paper Trading Tables
# ============================================================

class USPaperAccount(Base):
    """US stock paper trading account"""
    __tablename__ = "us_paper_account"

    id = Column(Integer, primary_key=True, autoincrement=True)
    account_name = Column(String(50), default="default")
    initial_capital = Column(Float, nullable=False)
    cash = Column(Float, nullable=False)
    total_assets = Column(Float)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    __table_args__ = (
        UniqueConstraint("account_name", name="uq_us_paper_account_name"),
    )


class USPaperPosition(Base):
    """US stock paper trading position"""
    __tablename__ = "us_paper_position"

    id = Column(Integer, primary_key=True, autoincrement=True)
    account_id = Column(Integer, nullable=False)
    ticker = Column(String(20), nullable=False)
    volume = Column(Integer, default=0)
    cost_basis = Column(Float, default=0)
    market_value = Column(Float, default=0)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    __table_args__ = (
        UniqueConstraint("account_id", "ticker", name="uq_us_paper_pos_account_ticker"),
    )


class USPaperTransaction(Base):
    """US stock paper trading transaction"""
    __tablename__ = "us_paper_transaction"

    id = Column(Integer, primary_key=True, autoincrement=True)
    account_id = Column(Integer, nullable=False)
    ticker = Column(String(20), nullable=False)
    direction = Column(String(10), comment="BUY/SELL")
    volume = Column(Integer)
    price = Column(Float)
    amount = Column(Float)
    fees = Column(Float, default=0)
    trade_date = Column(Date)
    created_at = Column(DateTime, default=datetime.now)

    __table_args__ = (
        Index("idx_us_paper_tx_account", "account_id"),
    )


class USPaperNav(Base):
    """US stock paper trading daily NAV"""
    __tablename__ = "us_paper_nav"

    id = Column(Integer, primary_key=True, autoincrement=True)
    account_id = Column(Integer, nullable=False)
    nav_date = Column(Date, nullable=False)
    nav = Column(Float)
    total_assets = Column(Float)
    created_at = Column(DateTime, default=datetime.now)

    __table_args__ = (
        UniqueConstraint("account_id", "nav_date", name="uq_us_paper_nav_date"),
    )


# ============================================================
# 数据库管理类
# ============================================================

class DatabaseManager:
    """
    数据库管理器

    提供建表、批量写入、查询等操作的统一接口。
    所有写入操作使用 upsert 语义（存在则更新，不存在则插入）。

    用法:
        db = DatabaseManager()
        db.init_tables()
        db.upsert_stock_basic(df)
        result = db.query("SELECT * FROM stock_basic LIMIT 10")
    """

    def __init__(self, db_url: str = DB_URL):
        """
        初始化数据库连接。

        Args:
            db_url: SQLAlchemy 数据库连接字符串，默认使用 settings.py 中的配置。
        """
        engine_kwargs = {"echo": False}
        # 连接池参数仅适用于 MySQL 等数据库，SQLite 不支持
        if not db_url.startswith("sqlite"):
            engine_kwargs.update(
                pool_size=15,
                max_overflow=20,
                pool_recycle=3600,
            )
        self.engine = create_engine(db_url, **engine_kwargs)
        self.SessionLocal = sessionmaker(bind=self.engine)
        # 日志中隐藏密码
        safe_url = db_url.split("@")[-1] if "@" in db_url else db_url
        logger.info(f"数据库连接已建立: ...@{safe_url}")

        # PostgreSQL 外部数据源（只读，延迟初始化）
        self._pg_engine = None

    @property
    def pg_engine(self):
        """PostgreSQL 只读连接（延迟初始化）。"""
        if self._pg_engine is None:
            from services.config import PG_URL
            if not PG_URL:
                raise RuntimeError("PG_URL 未配置，请在 .env 中设置 PG_HOST 等参数")
            self._pg_engine = create_engine(
                PG_URL, echo=False, pool_size=5, max_overflow=5, pool_recycle=3600,
            )
            safe = PG_URL.split("@")[-1] if "@" in PG_URL else PG_URL
            logger.info(f"PostgreSQL 连接已建立: ...@{safe}")
        return self._pg_engine

    def pg_query(self, sql: str, params: dict = None) -> "pd.DataFrame":
        """从 PostgreSQL 外部数据源执行只读查询。"""
        import pandas as pd
        with self.pg_engine.connect() as conn:
            return pd.read_sql(text(sql), conn, params=params)

    def init_tables(self):
        """
        创建所有表。如果表已存在则跳过。
        对已有表自动补齐新增列（如 adj_factor）。
        """
        # 确保舆情模型注册到 Base.metadata
        global _sentiment_models_loaded, _polymarket_models_loaded
        if not _sentiment_models_loaded:
            try:
                import services.sentiment.models  # noqa: F401
                _sentiment_models_loaded = True
            except ImportError:
                logger.debug("init_tables: sentiment 模块未安装，跳过")
        if not _polymarket_models_loaded:
            try:
                import services.polymarket.models  # noqa: F401
                _polymarket_models_loaded = True
            except ImportError:
                logger.debug("init_tables: polymarket 模块未安装，跳过")

        try:
            Base.metadata.create_all(self.engine)
        except Exception as e:
            # 部分表（如 sentiment 模块）可能因索引长度等问题创建失败，
            # 跳过并逐表重试核心表
            logger.warning(f"全量建表异常({e})，逐表重试核心表")
            for table in Base.metadata.sorted_tables:
                try:
                    table.create(self.engine, checkfirst=True)
                except Exception as e:
                    logger.debug(f"跳过表 {table.name} 创建失败: {e}")
        self._ensure_adj_factor_column()
        self._ensure_new_columns()
        logger.info("数据库表初始化完成")

    def _ensure_adj_factor_column(self):
        """已有 daily_price 表自动补齐 adj_factor 列。"""
        try:
            with self.engine.connect() as conn:
                # 检查列是否存在
                if str(self.engine.url).startswith("sqlite"):
                    cols = [r[1] for r in conn.execute(text("PRAGMA table_info(daily_price)")).fetchall()]
                else:
                    cols = [r[0] for r in conn.execute(text(
                        "SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS "
                        "WHERE TABLE_NAME='daily_price' AND TABLE_SCHEMA=DATABASE()"
                    )).fetchall()]
                if "adj_factor" not in cols:
                    conn.execute(text("ALTER TABLE daily_price ADD COLUMN adj_factor FLOAT"))
                    conn.commit()
                    logger.info("daily_price 表: 已添加 adj_factor 列")
        except Exception as e:
            logger.debug(f"adj_factor 列检查跳过: {e}")

    def _ensure_new_columns(self):
        """已有表自动补齐新增列：financial_data.bps, stock_basic.total_share/float_share。"""
        migrations = [
            ("financial_data", "bps", "FLOAT"),
            ("stock_basic", "total_share", "FLOAT"),
            ("stock_basic", "float_share", "FLOAT"),
            ("industry_class", "l2_industry_code", "VARCHAR(20)"),
            ("industry_class", "l2_industry_name", "VARCHAR(50)"),
            ("policy_article", "content", "LONGTEXT"),
            ("policy_analysis", "impact_type", "VARCHAR(30)"),
        ]
        try:
            with self.engine.connect() as conn:
                for table, col_name, col_type in migrations:
                    if str(self.engine.url).startswith("sqlite"):
                        cols = [r[1] for r in conn.execute(text(f"PRAGMA table_info({table})")).fetchall()]
                    else:
                        cols = [r[0] for r in conn.execute(text(
                            f"SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS "
                            f"WHERE TABLE_NAME='{table}' AND TABLE_SCHEMA=DATABASE()"
                        )).fetchall()]
                    if col_name not in cols:
                        conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {col_name} {col_type}"))
                        conn.commit()
                        logger.info(f"{table} 表: 已添加 {col_name} 列")
        except Exception as e:
            logger.debug(f"新列迁移检查跳过: {e}")

    def get_session(self) -> Session:
        """
        获取数据库 Session。

        Returns:
            SQLAlchemy Session 实例。
        """
        return self.SessionLocal()

    # ----------------------------------------------------------
    # 通用查询
    # ----------------------------------------------------------

    def query(self, sql: str, params: Optional[dict] = None) -> pd.DataFrame:
        """
        执行 SQL 查询，返回 DataFrame。

        Args:
            sql: SQL 查询语句。
            params: 查询参数字典（可选）。

        Returns:
            查询结果 DataFrame。
        """
        with self.engine.connect() as conn:
            result = pd.read_sql(text(sql), conn, params=params)
        return result

    def table_count(self, table_name: str) -> int:
        """
        获取表的行数。

        Args:
            table_name: 表名。

        Returns:
            行数。
        """
        with self.engine.connect() as conn:
            result = conn.execute(text(f"SELECT COUNT(*) FROM {table_name}"))
            return result.scalar()

    # ----------------------------------------------------------
    # 股票基本信息
    # ----------------------------------------------------------

    def upsert_stock_basic(self, df: pd.DataFrame):
        """
        批量写入/更新股票基本信息。

        Args:
            df: 包含以下列的 DataFrame:
                - ts_code: 股票代码
                - name: 股票名称
                - market: 市场
                - list_date: 上市日期
                - delist_date: 退市日期（可选）
                - is_st: 是否ST
        """
        if df.empty:
            logger.warning("upsert_stock_basic: 输入DataFrame为空，跳过")
            return

        records = _sanitize_records(df.to_dict("records"))
        with self.get_session() as session:
            for record in records:
                existing = session.query(StockBasic).filter_by(
                    ts_code=record["ts_code"]
                ).first()
                if existing:
                    for key, value in record.items():
                        if key != "ts_code" and hasattr(existing, key):
                            setattr(existing, key, value)
                elif "name" in record:
                    # 只在有必填字段时才新增记录
                    session.add(StockBasic(**record))
            session.commit()
        logger.info(f"stock_basic: 写入/更新 {len(records)} 条记录")

    def get_stock_list(self, exclude_st: bool = True) -> pd.DataFrame:
        """
        获取股票列表。

        Args:
            exclude_st: 是否剔除ST股票，默认True。

        Returns:
            股票列表 DataFrame。
        """
        sql = "SELECT * FROM stock_basic WHERE delist_date IS NULL"
        if exclude_st:
            sql += " AND is_st = 0"
        return self.query(sql)

    # ----------------------------------------------------------
    # 日线行情
    # ----------------------------------------------------------

    def upsert_daily_price(self, df: pd.DataFrame):
        """
        批量写入/更新日线行情数据。
        使用 pandas to_sql 的 replace 策略，按股票分批写入。

        Args:
            df: 包含以下列的 DataFrame:
                - ts_code, trade_date, open, high, low, close,
                  volume, amount, turnover_rate, pct_chg,
                  is_limit_up, is_limit_down
        """
        if df.empty:
            logger.warning("upsert_daily_price: 输入DataFrame为空，跳过")
            return

        # 确保 trade_date 是 date 类型
        if "trade_date" in df.columns:
            df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.date

        records = _sanitize_records(df.to_dict("records"))

        with self.get_session() as session:
            batch_size = 500
            for i in range(0, len(records), batch_size):
                batch = records[i:i + batch_size]
                for record in batch:
                    existing = session.query(DailyPrice).filter_by(
                        ts_code=record["ts_code"],
                        trade_date=record["trade_date"],
                    ).first()
                    if existing:
                        for key, value in record.items():
                            if key not in ("ts_code", "trade_date") and hasattr(existing, key):
                                setattr(existing, key, value)
                    else:
                        session.add(DailyPrice(**record))
                session.flush()
            session.commit()
        logger.info(f"daily_price: 写入/更新 {len(records)} 条记录")

    def bulk_insert_daily_price(self, df: pd.DataFrame):
        """
        批量插入日线行情（仅新增，速度更快）。
        适用于首次全量下载场景，不做去重检查。

        Args:
            df: 日线行情 DataFrame。
        """
        if df.empty:
            logger.debug("bulk_insert_daily_price: 输入DataFrame为空，跳过")
            return

        if "trade_date" in df.columns:
            df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.date

        # 添加 updated_at 列
        df = df.copy()
        df["updated_at"] = datetime.now()

        df.to_sql(
            "daily_price",
            self.engine,
            if_exists="append",
            index=False,
            method="multi",
        )
        logger.info(f"daily_price: 批量插入 {len(df)} 条记录")

    def bulk_upsert_daily_price(self, df: pd.DataFrame):
        """
        批量 upsert 日线行情（MySQL ON DUPLICATE KEY UPDATE）。
        适用于增量更新场景，遇到重复自动更新，比逐条 ORM upsert 快 50-100 倍。

        SQLite 回退到 bulk_insert（忽略重复）。

        Args:
            df: 日线行情 DataFrame。
        """
        if df.empty:
            logger.debug("bulk_upsert_daily_price: 输入DataFrame为空，跳过")
            return

        if "trade_date" in df.columns:
            df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.date

        df = df.copy()
        df["updated_at"] = datetime.now()

        is_mysql = not str(self.engine.url).startswith("sqlite")

        if is_mysql:
            # MySQL: INSERT ... ON DUPLICATE KEY UPDATE
            cols = [
                "ts_code", "trade_date", "open", "high", "low", "close",
                "volume", "amount", "turnover_rate", "pct_chg",
                "adj_factor", "dv_ttm", "pe_ttm", "pb", "ps_ttm",
                "total_mv", "circ_mv", "turnover_rate_f", "volume_ratio",
                "is_limit_up", "is_limit_down", "updated_at",
            ]
            existing_cols = [c for c in cols if c in df.columns]
            update_cols = [c for c in existing_cols if c not in ("ts_code", "trade_date")]

            placeholders = ", ".join([f":{c}" for c in existing_cols])
            # open 是 MySQL 保留字，需要反引号
            col_names = ", ".join([f"`{c}`" for c in existing_cols])
            update_clause = ", ".join([f"`{c}` = VALUES(`{c}`)" for c in update_cols])

            sql = text(
                f"INSERT INTO daily_price ({col_names}) VALUES ({placeholders}) "
                f"ON DUPLICATE KEY UPDATE {update_clause}"
            )

            records = _sanitize_records(df[existing_cols].to_dict("records"))
            batch_size = 1000
            with self.engine.begin() as conn:
                for i in range(0, len(records), batch_size):
                    batch = records[i:i + batch_size]
                    conn.execute(sql, batch)

            logger.info(f"daily_price: 批量upsert {len(records)} 条记录")
        else:
            # SQLite: 回退到 INSERT OR IGNORE
            try:
                df.to_sql(
                    "daily_price", self.engine,
                    if_exists="append", index=False, method="multi",
                )
            except Exception as e:
                # 忽略重复键错误
                logger.debug(f"bulk_upsert_daily_price: SQLite 插入忽略重复: {e}")
            logger.info(f"daily_price: 批量插入(SQLite) {len(df)} 条记录")

    def batch_update_financial(self, updates: list[dict], end_date: str = None):
        """
        批量更新财务数据的估值字段（PE/PB/市值）。

        对每只股票更新其自身最新报告期的记录（而非固定全局 end_date），
        避免因各股票披露进度不同导致更新失败。

        Args:
            updates: [{"ts_code": "000001.SZ", "pe_ttm": 8.5, "pb": 0.7, ...}, ...]
            end_date: 已废弃，保留参数兼容性但不再使用。
        """
        if not updates:
            logger.debug("batch_update_financial: 更新列表为空，跳过")
            return

        is_mysql = not str(self.engine.url).startswith("sqlite")

        if is_mysql:
            value_cols = ["pe_ttm", "pb", "total_mv", "circ_mv"]
            ts_codes = [u["ts_code"] for u in updates]
            codes_str = "','".join(ts_codes)

            set_parts = []
            for col in value_cols:
                cases = []
                for u in updates:
                    val = u.get(col)
                    if val is not None:
                        cases.append(f"WHEN f.ts_code = '{u['ts_code']}' THEN {val}")
                if cases:
                    case_sql = " ".join(cases)
                    set_parts.append(f"f.{col} = CASE {case_sql} ELSE f.{col} END")

            if set_parts:
                sql = (
                    f"UPDATE financial_data f "
                    f"INNER JOIN ("
                    f"  SELECT ts_code, MAX(end_date) AS latest_end "
                    f"  FROM financial_data "
                    f"  WHERE ts_code IN ('{codes_str}') "
                    f"  GROUP BY ts_code"
                    f") sub ON f.ts_code = sub.ts_code AND f.end_date = sub.latest_end "
                    f"SET {', '.join(set_parts)}"
                )
                with self.engine.begin() as conn:
                    conn.execute(text(sql))
                logger.info(f"financial_data: 批量更新 {len(updates)} 条估值数据")
        else:
            # SQLite 逐条：取每只股票最新报告期的记录
            with self.get_session() as session:
                for u in updates:
                    existing = session.query(FinancialData).filter_by(
                        ts_code=u["ts_code"],
                    ).order_by(FinancialData.end_date.desc()).first()
                    if existing:
                        for col in ["pe_ttm", "pb", "total_mv", "circ_mv"]:
                            if col in u and u[col] is not None:
                                setattr(existing, col, u[col])
                session.commit()

    def get_daily_price(
        self,
        ts_code: Optional[str] = None,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
    ) -> pd.DataFrame:
        """
        查询日线行情。

        Args:
            ts_code: 股票代码（可选，为空则查全市场）。
            start_date: 起始日期，格式 YYYYMMDD（可选）。
            end_date: 结束日期，格式 YYYYMMDD（可选）。

        Returns:
            日线行情 DataFrame。
        """
        conditions = []
        if ts_code:
            conditions.append(f"ts_code = '{ts_code}'")
        if start_date:
            conditions.append(f"trade_date >= '{start_date}'")
        if end_date:
            conditions.append(f"trade_date <= '{end_date}'")

        sql = "SELECT * FROM daily_price"
        if conditions:
            sql += " WHERE " + " AND ".join(conditions)
        sql += " ORDER BY trade_date"

        return self.query(sql)

    def get_daily_price_qfq(
        self,
        ts_code: Optional[str] = None,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
    ) -> pd.DataFrame:
        """
        查询前复权日线行情（动态计算）。

        使用 adj_factor 将存储的未复权价格转换为前复权价格：
            qfq_price = price * adj_factor / latest_adj_factor

        Args:
            ts_code: 股票代码（可选）。
            start_date: 起始日期 YYYYMMDD（可选）。
            end_date: 结束日期 YYYYMMDD（可选）。

        Returns:
            前复权日线行情 DataFrame。
        """
        df = self.get_daily_price(ts_code, start_date, end_date)
        if df.empty or "adj_factor" not in df.columns:
            return df

        price_cols = ["open", "high", "low", "close"]
        for code, grp in df.groupby("ts_code"):
            latest_adj = grp["adj_factor"].iloc[-1]
            if pd.notna(latest_adj) and latest_adj != 0:
                mask = df["ts_code"] == code
                for col in price_cols:
                    if col in df.columns:
                        df.loc[mask, col] = df.loc[mask, col] * df.loc[mask, "adj_factor"] / latest_adj

        return df

    def get_latest_trade_date(self, ts_code: Optional[str] = None) -> Optional[str]:
        """
        获取数据库中最新的交易日期。

        Args:
            ts_code: 股票代码（可选）。

        Returns:
            最新交易日期字符串（YYYY-MM-DD），无数据返回 None。
        """
        sql = "SELECT MAX(trade_date) as max_date FROM daily_price"
        if ts_code:
            sql += f" WHERE ts_code = '{ts_code}'"
        result = self.query(sql)
        max_date = result["max_date"].iloc[0]
        if pd.isna(max_date):
            logger.debug("get_latest_trade_date: daily_price 表为空")
            return None
        return str(max_date)

    # ----------------------------------------------------------
    # 财务数据
    # ----------------------------------------------------------

    def upsert_financial_data(self, df: pd.DataFrame):
        """
        批量写入/更新财务数据。

        Args:
            df: 包含以下列的 DataFrame:
                - ts_code, ann_date, end_date, pe_ttm, pb, roe_ttm,
                  gross_margin, revenue, net_profit, total_mv, circ_mv
        """
        if df.empty:
            logger.warning("upsert_financial_data: 输入DataFrame为空，跳过")
            return

        for col in ["ann_date", "end_date"]:
            if col in df.columns:
                df[col] = pd.to_datetime(df[col]).dt.date

        records = _sanitize_records(df.to_dict("records"))
        # NaN → None（pymysql 不接受 float('nan')，MySQL 需要 NULL）
        for record in records:
            for key, value in record.items():
                if isinstance(value, float) and pd.isna(value):
                    record[key] = None
        with self.get_session() as session:
            for record in records:
                existing = session.query(FinancialData).filter_by(
                    ts_code=record["ts_code"],
                    end_date=record["end_date"],
                ).first()
                if existing:
                    for key, value in record.items():
                        if key not in ("ts_code", "end_date") and hasattr(existing, key):
                            setattr(existing, key, value)
                else:
                    session.add(FinancialData(**record))
            session.commit()
        logger.info(f"financial_data: 写入/更新 {len(records)} 条记录")

    # ----------------------------------------------------------
    # 行业分类
    # ----------------------------------------------------------

    def upsert_industry_class(self, df: pd.DataFrame):
        """
        批量写入/更新行业分类。

        Args:
            df: 包含以下列的 DataFrame:
                - ts_code, industry_code（可选）, industry_name
        """
        if df.empty:
            logger.warning("upsert_industry_class: 输入DataFrame为空，跳过")
            return

        records = _sanitize_records(df.to_dict("records"))
        # NaN → None（pymysql 不接受 float('nan')，MySQL 需要 NULL）
        for record in records:
            for key, value in record.items():
                if isinstance(value, float) and pd.isna(value):
                    record[key] = None
        with self.get_session() as session:
            for record in records:
                existing = session.query(IndustryClass).filter_by(
                    ts_code=record["ts_code"]
                ).first()
                if existing:
                    for key, value in record.items():
                        if key != "ts_code" and hasattr(existing, key):
                            setattr(existing, key, value)
                else:
                    session.add(IndustryClass(**record))
            session.commit()
        logger.info(f"industry_class: 写入/更新 {len(records)} 条记录")

    def get_industry_map(self) -> pd.DataFrame:
        """
        获取全市场行业分类映射。

        Returns:
            DataFrame，包含 ts_code 和 industry_name 列。
        """
        return self.query("SELECT ts_code, industry_name, l2_industry_name FROM industry_class")

    # ----------------------------------------------------------
    # 商品期货价格
    # ----------------------------------------------------------

    def upsert_commodity_price(self, df: pd.DataFrame):
        """
        批量写入/更新商品期货价格。

        Args:
            df: 包含 commodity_code, trade_date, ts_code, open, high, low,
                close, settle, volume, amount, oi 列的 DataFrame。
        """
        if df.empty:
            logger.warning("upsert_commodity_price: 输入DataFrame为空，跳过")
            return

        if "trade_date" in df.columns:
            df = df.copy()
            df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.date

        is_mysql = not str(self.engine.url).startswith("sqlite")

        if is_mysql:
            cols = [
                "commodity_code", "trade_date", "ts_code",
                "open", "high", "low", "close", "settle",
                "volume", "amount", "oi", "updated_at",
            ]
            df = df.copy()
            df["updated_at"] = datetime.now()
            existing_cols = [c for c in cols if c in df.columns]
            update_cols = [c for c in existing_cols if c not in ("commodity_code", "trade_date")]

            placeholders = ", ".join([f":{c}" for c in existing_cols])
            col_names = ", ".join([f"`{c}`" for c in existing_cols])
            update_clause = ", ".join([f"`{c}` = VALUES(`{c}`)" for c in update_cols])

            sql = text(
                f"INSERT INTO commodity_price ({col_names}) VALUES ({placeholders}) "
                f"ON DUPLICATE KEY UPDATE {update_clause}"
            )

            records = _sanitize_records(df[existing_cols].to_dict("records"))
            batch_size = 1000
            with self.engine.begin() as conn:
                for i in range(0, len(records), batch_size):
                    batch = records[i:i + batch_size]
                    conn.execute(sql, batch)
            logger.info(f"commodity_price: 批量upsert {len(records)} 条记录")
        else:
            # SQLite: 逐条 upsert
            records = _sanitize_records(df.to_dict("records"))
            with self.get_session() as session:
                for record in records:
                    existing = session.query(CommodityPrice).filter_by(
                        commodity_code=record["commodity_code"],
                        trade_date=record["trade_date"],
                    ).first()
                    if existing:
                        for key, value in record.items():
                            if key not in ("commodity_code", "trade_date") and hasattr(existing, key):
                                setattr(existing, key, value)
                    else:
                        session.add(CommodityPrice(**{
                            k: v for k, v in record.items() if hasattr(CommodityPrice, k)
                        }))
                session.commit()
            logger.info(f"commodity_price: 写入/更新 {len(records)} 条记录")

    def get_commodity_price_history(
        self,
        commodity_codes: list[str],
        end_date: str,
        lookback_days: int,
    ) -> pd.DataFrame:
        """
        查询商品价格历史。

        Args:
            commodity_codes: 品种代码列表，如 ["AU", "CU"]。
            end_date: 截止日期，格式 YYYY-MM-DD。
            lookback_days: 向前回看的自然日天数。

        Returns:
            DataFrame，包含 commodity_code, trade_date, close, settle, oi 列。
        """
        start_date = (
            pd.to_datetime(end_date) - pd.Timedelta(days=lookback_days)
        ).strftime("%Y-%m-%d")

        codes_str = "','".join(commodity_codes)
        sql = (
            f"SELECT commodity_code, trade_date, close, settle, oi "
            f"FROM commodity_price "
            f"WHERE commodity_code IN ('{codes_str}') "
            f"AND trade_date >= '{start_date}' "
            f"AND trade_date <= '{end_date}' "
            f"ORDER BY commodity_code, trade_date"
        )
        return self.query(sql)

    def get_latest_commodity_date(self) -> Optional[str]:
        """获取商品价格表最新日期。"""
        try:
            result = self.query("SELECT MAX(trade_date) as max_date FROM commodity_price")
            max_date = result["max_date"].iloc[0]
            if pd.isna(max_date):
                logger.debug("get_latest_commodity_date: 商品价格表为空")
                return None
            return str(max_date)
        except Exception as e:
            logger.debug(f"get_latest_commodity_date: 查询失败: {e}")
            return None

    # ----------------------------------------------------------
    # 宏观经济指标
    # ----------------------------------------------------------

    def upsert_macro_indicator(self, df: pd.DataFrame):
        """
        批量写入/更新宏观经济指标。

        Args:
            df: 包含 indicator_code, report_date, value 列的 DataFrame。
        """
        if df.empty:
            logger.warning("upsert_macro_indicator: 输入DataFrame为空，跳过")
            return

        if "report_date" in df.columns:
            df = df.copy()
            df["report_date"] = pd.to_datetime(df["report_date"]).dt.date

        is_mysql = not str(self.engine.url).startswith("sqlite")

        if is_mysql:
            cols = ["indicator_code", "report_date", "value", "updated_at"]
            df = df.copy()
            df["updated_at"] = datetime.now()
            existing_cols = [c for c in cols if c in df.columns]
            update_cols = [c for c in existing_cols if c not in ("indicator_code", "report_date")]

            placeholders = ", ".join([f":{c}" for c in existing_cols])
            col_names = ", ".join([f"`{c}`" for c in existing_cols])
            update_clause = ", ".join([f"`{c}` = VALUES(`{c}`)" for c in update_cols])

            sql = text(
                f"INSERT INTO macro_indicator ({col_names}) VALUES ({placeholders}) "
                f"ON DUPLICATE KEY UPDATE {update_clause}"
            )

            records = _sanitize_records(df[existing_cols].to_dict("records"))
            batch_size = 1000
            with self.engine.begin() as conn:
                for i in range(0, len(records), batch_size):
                    batch = records[i:i + batch_size]
                    conn.execute(sql, batch)
            logger.info(f"macro_indicator: 批量upsert {len(records)} 条记录")
        else:
            # SQLite: 逐条 upsert
            records = _sanitize_records(df.to_dict("records"))
            with self.get_session() as session:
                for record in records:
                    existing = session.query(MacroIndicator).filter_by(
                        indicator_code=record["indicator_code"],
                        report_date=record["report_date"],
                    ).first()
                    if existing:
                        if "value" in record:
                            existing.value = record["value"]
                    else:
                        session.add(MacroIndicator(**{
                            k: v for k, v in record.items() if hasattr(MacroIndicator, k)
                        }))
                session.commit()
            logger.info(f"macro_indicator: 写入/更新 {len(records)} 条记录")

    def get_macro_indicator_history(
        self,
        indicator_code: str,
        end_date: str,
        lookback_months: int = 36,
    ) -> pd.DataFrame:
        """
        查询宏观指标历史序列。

        Args:
            indicator_code: 指标代码，如 "SHIBOR_3M"。
            end_date: 截止日期，格式 YYYY-MM-DD。
            lookback_months: 向前回看的月数。

        Returns:
            DataFrame，包含 report_date, value 列，按日期升序。
        """
        start_date = (
            pd.to_datetime(end_date) - pd.DateOffset(months=lookback_months)
        ).strftime("%Y-%m-%d")

        sql = (
            "SELECT report_date, value FROM macro_indicator "
            f"WHERE indicator_code = '{indicator_code}' "
            f"AND report_date >= '{start_date}' "
            f"AND report_date <= '{end_date}' "
            "ORDER BY report_date"
        )
        return self.query(sql)

    def get_latest_macro_date(self, indicator_code: Optional[str] = None) -> Optional[str]:
        """
        获取宏观指标表最新日期。

        Args:
            indicator_code: 指定指标代码（可选，不指定则取全局最新）。

        Returns:
            最新日期字符串（YYYY-MM-DD），无数据返回 None。
        """
        try:
            sql = "SELECT MAX(report_date) as max_date FROM macro_indicator"
            if indicator_code:
                sql += f" WHERE indicator_code = '{indicator_code}'"
            result = self.query(sql)
            max_date = result["max_date"].iloc[0]
            if pd.isna(max_date):
                logger.debug("get_latest_macro_date: 宏观指标表为空")
                return None
            return str(max_date)
        except Exception as e:
            logger.debug(f"get_latest_macro_date: 查询失败: {e}")
            return None

    # ----------------------------------------------------------
    # 行业因子权重配置
    # ----------------------------------------------------------

    def upsert_industry_factor_config(self, records: list[dict]):
        """
        批量写入/更新行业因子权重配置。

        按 (industry_name, factor_name) 做 upsert。

        Args:
            records: 每条记录包含 industry_name, factor_name, weight, description(可选)。
        """
        if not records:
            logger.debug("upsert_industry_factor_config: 记录列表为空，跳过")
            return

        with self.get_session() as session:
            for record in records:
                existing = session.query(IndustryFactorConfig).filter_by(
                    industry_name=record["industry_name"],
                    factor_name=record["factor_name"],
                ).first()
                if existing:
                    existing.weight = record["weight"]
                    if "description" in record:
                        existing.description = record["description"]
                else:
                    session.add(IndustryFactorConfig(**record))
            session.commit()
        logger.info(f"industry_factor_config: 写入/更新 {len(records)} 条记录")

    def get_industry_factor_weights(self) -> pd.DataFrame:
        """
        获取全部行业因子权重配置。

        Returns:
            DataFrame，包含 industry_name, factor_name, weight 列。
        """
        return self.query(
            "SELECT industry_name, factor_name, weight FROM industry_factor_config"
        )


    # ----------------------------------------------------------
    # 券商研报
    # ----------------------------------------------------------

    def upsert_research_reports(self, reports: list[dict]) -> dict:
        """
        批量写入/更新券商研报（按唯一键去重）。

        Args:
            reports: 研报字典列表，每条包含 ts_code, stock_name, institution,
                     analyst, title, rating, rating_score, report_date。

        Returns:
            dict: {"new": 新增数, "updated": 更新数}。
        """
        if not reports:
            return {"new": 0, "updated": 0}

        new_count = 0
        updated_count = 0
        with self.get_session() as session:
            for record in reports:
                # 确保 report_date 是 date 类型
                if isinstance(record.get("report_date"), str):
                    record["report_date"] = pd.to_datetime(record["report_date"]).date()

                existing = session.query(ResearchReport).filter_by(
                    ts_code=record["ts_code"],
                    institution=record["institution"],
                    report_date=record["report_date"],
                    title=record.get("title", ""),
                ).first()
                if existing:
                    changed = False
                    for key, value in record.items():
                        if key not in ("ts_code", "institution", "report_date", "title") and hasattr(existing, key):
                            if getattr(existing, key) != value:
                                setattr(existing, key, value)
                                changed = True
                    if changed:
                        updated_count += 1
                else:
                    session.add(ResearchReport(**{
                        k: v for k, v in record.items() if hasattr(ResearchReport, k)
                    }))
                    new_count += 1
            session.commit()
        logger.info(f"research_report: 写入/更新 {len(reports)} 条，新增 {new_count}，更新 {updated_count}")
        return {"new": new_count, "updated": updated_count}

    def get_research_reports(
        self,
        end_date: str,
        lookback_days: int = 90,
        ts_codes: Optional[list[str]] = None,
    ) -> pd.DataFrame:
        """
        按日期范围查询券商研报。

        Args:
            end_date: 截止日期，格式 YYYY-MM-DD。
            lookback_days: 向前回看的自然日天数。
            ts_codes: 股票代码列表（可选，限定范围）。

        Returns:
            DataFrame，包含全部研报字段。
        """
        start_date = (
            pd.to_datetime(end_date) - pd.Timedelta(days=lookback_days)
        ).strftime("%Y-%m-%d")

        sql = (
            "SELECT ts_code, stock_name, institution, analyst, title, "
            "rating, rating_score, report_date "
            "FROM research_report "
            f"WHERE report_date >= '{start_date}' "
            f"AND report_date <= '{end_date}'"
        )

        if ts_codes:
            codes_str = "','".join(ts_codes)
            sql += f" AND ts_code IN ('{codes_str}')"

        sql += " ORDER BY report_date DESC"
        return self.query(sql)

    def get_research_report_stats(
        self,
        end_date: str,
        lookback_days: int = 90,
    ) -> pd.DataFrame:
        """
        按股票聚合券商研报统计（平均评级、研报数、机构数）。

        Args:
            end_date: 截止日期，格式 YYYY-MM-DD。
            lookback_days: 向前回看的自然日天数。

        Returns:
            DataFrame，包含 ts_code, avg_rating, report_count, institution_count 列。
        """
        start_date = (
            pd.to_datetime(end_date) - pd.Timedelta(days=lookback_days)
        ).strftime("%Y-%m-%d")
        # 安全裕度：排除选股日当天发布的研报，防止未来函数
        safe_end = (
            pd.to_datetime(end_date) - pd.Timedelta(days=1)
        ).strftime("%Y-%m-%d")

        sql = (
            "SELECT ts_code, "
            "AVG(rating_score) as avg_rating, "
            "COUNT(*) as report_count, "
            "COUNT(DISTINCT institution) as institution_count "
            "FROM research_report "
            f"WHERE report_date >= '{start_date}' "
            f"AND report_date <= '{safe_end}' "
            "AND rating_score IS NOT NULL "
            "GROUP BY ts_code"
        )
        return self.query(sql)

    # ----------------------------------------------------------
    # 舆情数据
    # ----------------------------------------------------------

    def upsert_policy_articles(self, articles: list[dict]) -> int:
        """
        批量写入/更新政策文章（URL 去重）。

        Args:
            articles: 文章字典列表，每条包含 source, tier, title, url,
                      publish_date, category, summary, content_hash。

        Returns:
            新增文章数。
        """
        if not articles:
            logger.debug("upsert_policy_articles: 文章列表为空，跳过")
            return 0

        from services.sentiment.models import PolicyArticle

        new_count = 0
        with self.get_session() as session:
            for record in articles:
                existing = session.query(PolicyArticle).filter_by(
                    url=record["url"]
                ).first()
                if existing:
                    for key, value in record.items():
                        if key != "url" and hasattr(existing, key):
                            setattr(existing, key, value)
                else:
                    session.add(PolicyArticle(**record))
                    new_count += 1
            session.commit()
        logger.info(f"policy_article: 写入/更新 {len(articles)} 条，新增 {new_count} 条")
        return new_count

    def bulk_upsert_policy_articles(self, articles: list[dict]) -> int:
        """
        批量 upsert 政策文章（MySQL ON DUPLICATE KEY UPDATE）。

        Args:
            articles: 文章字典列表。

        Returns:
            新增文章数（近似值）。
        """
        if not articles:
            logger.debug("bulk_upsert_policy_articles: 文章列表为空，跳过")
            return 0

        is_mysql = not str(self.engine.url).startswith("sqlite")

        if is_mysql:
            cols = [
                "source", "tier", "title", "url", "publish_date",
                "category", "summary", "content", "content_hash", "scraped_at", "updated_at",
            ]
            existing_cols = [c for c in cols if c in articles[0]]
            # 确保有 scraped_at 和 updated_at
            now = datetime.now()
            for a in articles:
                a.setdefault("scraped_at", now)
                a["updated_at"] = now

            update_cols = [c for c in existing_cols if c != "url"]
            placeholders = ", ".join([f":{c}" for c in existing_cols])
            col_names = ", ".join([f"`{c}`" for c in existing_cols])
            update_clause = ", ".join([f"`{c}` = VALUES(`{c}`)" for c in update_cols])

            sql = text(
                f"INSERT INTO policy_article ({col_names}) VALUES ({placeholders}) "
                f"ON DUPLICATE KEY UPDATE {update_clause}"
            )

            batch_size = 500
            with self.engine.begin() as conn:
                for i in range(0, len(articles), batch_size):
                    batch = articles[i:i + batch_size]
                    conn.execute(sql, batch)

            logger.info(f"policy_article: 批量upsert {len(articles)} 条")
            return len(articles)  # 近似值
        else:
            return self.upsert_policy_articles(articles)

    def get_articles_without_content(
        self,
        source: Optional[str] = None,
        limit: int = 100,
    ) -> list[dict]:
        """
        查询正文为空的文章（用于补录全文）。

        Args:
            source: 可选来源过滤。
            limit: 最大返回数量。

        Returns:
            文章字典列表，包含 id, url, source。
        """
        conditions = "(content IS NULL OR content = '')"
        params: dict = {"lim": limit}
        if source:
            conditions += " AND source = :source"
            params["source"] = source
        sql = (
            f"SELECT id, url, source FROM policy_article "
            f"WHERE {conditions} "
            f"ORDER BY publish_date DESC LIMIT :lim"
        )
        df = self.query(sql, params=params)
        if df.empty:
            logger.debug("get_articles_without_content: 无需补录正文的文章")
            return []
        return df.to_dict("records")

    def update_article_content(self, article_id: int, content: str):
        """
        更新单篇文章的正文。

        Args:
            article_id: 文章 ID。
            content: 正文内容。
        """
        from services.sentiment.models import PolicyArticle

        with self.get_session() as session:
            article = session.query(PolicyArticle).filter_by(id=article_id).first()
            if article:
                article.content = content
                article.updated_at = datetime.now()
                session.commit()

    def get_latest_scrape_date(self, source: str) -> Optional[str]:
        """
        查询某来源最新文章的发布日期。

        Args:
            source: 来源标识，如 'gov_cn'。

        Returns:
            最新日期字符串（YYYY-MM-DD），无数据返回 None。
        """
        sql = "SELECT MAX(publish_date) as max_date FROM policy_article WHERE source = :source"
        result = self.query(sql, params={"source": source})
        max_date = result["max_date"].iloc[0]
        if pd.isna(max_date):
            logger.debug(f"get_latest_article_date: source={source} 无数据")
            return None
        return str(max_date)

    def upsert_policy_analysis(self, records: list[dict]) -> int:
        """
        批量写入/更新政策分析结果（按 article_id + analysis_type 去重）。

        Args:
            records: 每条包含 article_id, analysis_type, industries, sentiment,
                     intensity, keywords_hit, summary_text(可选), analyzed_at。

        Returns:
            写入记录数。
        """
        if not records:
            logger.debug("upsert_policy_analysis: 记录列表为空，跳过")
            return 0

        from services.sentiment.models import PolicyAnalysis

        with self.get_session() as session:
            for record in records:
                existing = session.query(PolicyAnalysis).filter_by(
                    article_id=record["article_id"],
                    analysis_type=record["analysis_type"],
                ).first()
                if existing:
                    for key, value in record.items():
                        if key not in ("article_id", "analysis_type") and hasattr(existing, key):
                            setattr(existing, key, value)
                else:
                    session.add(PolicyAnalysis(**record))
            session.commit()
        logger.info(f"policy_analysis: 写入/更新 {len(records)} 条记录")
        return len(records)

    def get_policy_analysis(
        self,
        end_date: str,
        lookback_days: int = 7,
        analysis_type: Optional[str] = None,
    ) -> pd.DataFrame:
        """
        查询政策分析结果（关联文章的 publish_date + tier）。

        Args:
            end_date: 截止日期，格式 YYYY-MM-DD。
            lookback_days: 向前回看的自然日天数。
            analysis_type: 筛选分析类型（keyword/llm），None 则全部。

        Returns:
            DataFrame，包含 article_id, analysis_type, industries, sentiment,
            intensity, keywords_hit, publish_date, tier 列。
        """
        start_date = (
            pd.to_datetime(end_date) - pd.Timedelta(days=lookback_days)
        ).strftime("%Y-%m-%d")

        sql = (
            "SELECT pa.article_id, pa.analysis_type, pa.industries, "
            "pa.sentiment, pa.intensity, pa.keywords_hit, "
            "pa.affected_stocks, "
            "a.publish_date, a.tier "
            "FROM policy_analysis pa "
            "JOIN policy_article a ON pa.article_id = a.id "
            f"WHERE a.publish_date >= '{start_date}' "
            f"AND a.publish_date <= '{end_date}'"
        )
        if analysis_type:
            sql += f" AND pa.analysis_type = '{analysis_type}'"
        sql += " ORDER BY a.publish_date DESC"
        return self.query(sql)

    def get_unanalyzed_articles(
        self,
        analysis_type: str = "keyword",
        limit: int = 500,
    ) -> list[dict]:
        """
        查询未做指定类型分析的文章。

        Args:
            analysis_type: 分析类型（keyword/llm）。
            limit: 最大返回数量。

        Returns:
            文章字典列表，包含 id, title, summary, source, tier, category, publish_date。
        """
        sql = (
            "SELECT a.id, a.title, a.summary, a.content, a.source, a.tier, "
            "a.category, a.publish_date "
            "FROM policy_article a "
            "LEFT JOIN policy_analysis pa "
            "ON a.id = pa.article_id AND pa.analysis_type = :atype "
            "WHERE pa.id IS NULL "
            "ORDER BY a.publish_date DESC "
            "LIMIT :lim"
        )
        df = self.query(sql, params={"atype": analysis_type, "lim": limit})
        if df.empty:
            logger.debug(f"get_unanalyzed_articles: 无未分析文章 (type={analysis_type})")
            return []
        # 转换日期为字符串
        for col in ["publish_date"]:
            if col in df.columns:
                df[col] = df[col].astype(str)
        return df.to_dict("records")

    # ----------------------------------------------------------
    # 美股数据
    # ----------------------------------------------------------

    def upsert_us_stock_basic(self, df: pd.DataFrame):
        """批量写入/更新美股基本信息（快速 raw SQL）。"""
        self._fast_bulk_upsert("us_stock_basic", df, ["ticker"])

    def get_us_tickers(self, active_only: bool = True) -> list[str]:
        """获取美股代码列表。"""
        sql = "SELECT ticker FROM us_stock_basic"
        if active_only:
            sql += " WHERE is_active = 1"
        df = self.query(sql)
        return df["ticker"].tolist() if not df.empty else []

    def deactivate_us_stocks_not_in(self, active_tickers: set[str]):
        """将不在 active_tickers 集合中的美股标记为 inactive。"""
        with self.get_session() as session:
            rows = session.query(USStockBasic).filter(
                USStockBasic.is_active == 1,
                USStockBasic.ticker.notin_(active_tickers),
            ).all()
            if rows:
                for row in rows:
                    row.is_active = 0
                session.commit()
                logger.info(f"us_stock_basic: 标记 {len(rows)} 只非 NASDAQ 100 股票为 inactive")

    def bulk_upsert_us_daily_price(self, df: pd.DataFrame):
        """批量 upsert 美股日线行情（快速 raw SQL）。"""
        self._fast_bulk_upsert("us_daily_price", df, ["ticker", "trade_date"],
                               date_cols=["trade_date"])

    def upsert_us_financial_data(self, df: pd.DataFrame):
        """批量写入/更新美股财务数据（快速 raw SQL）。"""
        self._fast_bulk_upsert("us_financial_data", df, ["ticker", "period"],
                               date_cols=["date", "filing_date"])

    def upsert_us_industry_class(self, df: pd.DataFrame):
        """批量写入/更新美股行业分类（快速 raw SQL）。"""
        self._fast_bulk_upsert("us_industry_class", df, ["ticker"])

    def bulk_upsert_us_index_daily(self, df: pd.DataFrame):
        """批量 upsert 美股指数日线（快速 raw SQL）。"""
        self._fast_bulk_upsert("us_index_daily", df, ["index_code", "trade_date"],
                               date_cols=["trade_date"])

    def upsert_us_macro_indicator(self, df: pd.DataFrame):
        """批量 upsert 美股宏观经济指标（快速 raw SQL）。"""
        self._fast_bulk_upsert("us_macro_indicator", df,
                               ["indicator_code", "report_date"], date_cols=["report_date"])

    def bulk_upsert_us_commodity_price(self, df: pd.DataFrame):
        """批量 upsert 美股商品期货（快速 raw SQL）。"""
        self._fast_bulk_upsert("us_commodity_price", df,
                               ["symbol", "trade_date"], date_cols=["trade_date"])

    def upsert_us_analyst_recommendation(self, df: pd.DataFrame):
        """批量写入/更新美股分析师评级（快速 raw SQL）。"""
        self._fast_bulk_upsert("us_analyst_recommendation", df,
                               ["ticker", "date", "analyst_company"], date_cols=["date"])

    def upsert_us_sec_filing(self, df: pd.DataFrame):
        """批量写入/更新美股 SEC 公告（快速 raw SQL）。"""
        self._fast_bulk_upsert("us_sec_filing", df,
                               ["ticker", "filing_date", "type"], date_cols=["filing_date"])

    def upsert_us_corporate_action(self, df: pd.DataFrame):
        """批量写入/更新美股公司行动（快速 raw SQL）。"""
        self._fast_bulk_upsert("us_corporate_action", df,
                               ["ticker", "date", "action_type"], date_cols=["date"])

    def upsert_us_earnings_surprise(self, df: pd.DataFrame):
        """批量写入/更新美股盈利惊喜数据（快速 raw SQL）。"""
        self._fast_bulk_upsert("us_earnings_surprise", df, ["ticker", "date"], date_cols=["date"])

    def upsert_us_eps_estimate(self, df: pd.DataFrame):
        """批量写入/更新美股 EPS 共识预期数据（快速 raw SQL）。"""
        self._fast_bulk_upsert("us_eps_estimate", df, ["ticker", "date"], date_cols=["date"])

    def _fast_bulk_upsert(self, table_name: str, df: pd.DataFrame,
                          unique_keys: list[str], date_cols: list[str] = None,
                          datetime_cols: list[str] = None,
                          batch_size: int = 2000):
        """通用快速 bulk upsert — MySQL INSERT ... ON DUPLICATE KEY UPDATE。"""
        if df.empty:
            logger.debug(f"{table_name}: DataFrame 为空，跳过写入")
            return
        df = df.copy()
        # Date columns → date only, NaT → None
        for col in (date_cols or []):
            if col in df.columns:
                converted = pd.to_datetime(df[col], errors="coerce")
                df[col] = converted.apply(lambda x: x.date() if pd.notna(x) else None)
        # Datetime columns → MySQL compatible format (strip T/Z from ISO 8601)
        for col in (datetime_cols or []):
            if col in df.columns:
                df[col] = pd.to_datetime(df[col], errors="coerce")
                # Convert to Python datetime (MySQL compatible)
                df[col] = df[col].apply(lambda x: x.to_pydatetime() if pd.notna(x) else None)
        # Auto-detect: any remaining string columns with ISO datetime patterns
        for col in df.columns:
            if str(df[col].dtype) in ("object", "string", "str") and col not in (date_cols or []):
                sample = df[col].dropna().head(5)
                if not sample.empty and sample.astype(str).str.match(r"\d{4}-\d{2}-\d{2}T").any():
                    df[col] = pd.to_datetime(df[col], errors="coerce")
                    df[col] = df[col].apply(lambda x: x.to_pydatetime() if pd.notna(x) else None)
        df["updated_at"] = datetime.now()

        # Only keep columns that exist in the table
        try:
            table_cols_df = self.query(f"SHOW COLUMNS FROM `{table_name}`")
            valid_cols = set(table_cols_df["Field"].tolist())
        except Exception:
            valid_cols = set(df.columns)
        cols = [c for c in df.columns if c in valid_cols and c != "id"]
        if not cols:
            logger.warning(f"{table_name}: DataFrame 列 {list(df.columns)} 与表列 {valid_cols} 无交集，跳过")
            return

        records = _sanitize_records(df[cols].to_dict("records"))

        update_cols = [c for c in cols if c not in unique_keys and c != "id"]
        placeholders = ", ".join([f":{c}" for c in cols])
        col_names = ", ".join([f"`{c}`" for c in cols])
        update_clause = ", ".join([f"`{c}` = VALUES(`{c}`)" for c in update_cols])

        sql = text(
            f"INSERT INTO `{table_name}` ({col_names}) VALUES ({placeholders}) "
            f"ON DUPLICATE KEY UPDATE {update_clause}"
        )

        for i in range(0, len(records), batch_size):
            batch = records[i:i + batch_size]
            for attempt in range(3):
                try:
                    with self.engine.begin() as conn:
                        conn.execute(sql, batch)
                    logger.debug(f"_fast_bulk_upsert: {table_name} 批次写入成功")
                    break
                except Exception as e:
                    if "Deadlock" in str(e) and attempt < 2:
                        import time as _time
                        _time.sleep(0.5 * (attempt + 1))
                        logger.debug(f"_fast_bulk_upsert: {table_name} 死锁重试 (attempt {attempt+1})")
                        continue
                    raise
        logger.info(f"{table_name}: 批量upsert {len(records)} 条记录")

    def _bulk_upsert_generic(self, model_class, df: pd.DataFrame, unique_keys: list[str],
                                date_cols: list[str] = None):
        """通用批量 upsert — 使用快速 raw SQL。"""
        self._fast_bulk_upsert(model_class.__tablename__, df, unique_keys, date_cols)

    def upsert_us_insider_trade(self, df: pd.DataFrame):
        self._bulk_upsert_generic(
            USInsiderTrade, df,
            ["ticker", "transaction_date", "reporting_name", "transaction_type"],
            date_cols=["transaction_date"],
        )

    def upsert_us_key_metric(self, df: pd.DataFrame):
        self._bulk_upsert_generic(USKeyMetric, df, ["ticker", "date"], date_cols=["date"])

    def upsert_us_options_flow(self, df: pd.DataFrame):
        self._fast_bulk_upsert("us_options_flow", df, ["alert_id"],
                               date_cols=["expiry"], datetime_cols=["date"])

    def upsert_us_dark_pool(self, df: pd.DataFrame):
        """暗池数据 — bulk insert（append only, 无唯一键）。"""
        if df.empty:
            logger.debug("us_dark_pool: DataFrame 为空，跳过写入")
            return
        df = df.copy()
        # Auto-detect ISO datetime strings
        for col in df.columns:
            if str(df[col].dtype) in ("object", "string", "str"):
                sample = df[col].dropna().head(5)
                if not sample.empty and sample.astype(str).str.match(r"\d{4}-\d{2}-\d{2}T").any():
                    df[col] = pd.to_datetime(df[col], errors="coerce")
                    df[col] = df[col].apply(lambda x: x.to_pydatetime() if pd.notna(x) else None)
        # String numeric fields → float
        for col in ["price", "size", "notional"]:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")
        df["updated_at"] = datetime.now()
        # Get valid columns
        try:
            table_cols_df = self.query("SHOW COLUMNS FROM us_dark_pool")
            valid_cols = set(table_cols_df["Field"].tolist())
        except Exception as e:
            logger.debug(f"upsert_us_dark_pool: 获取 us_dark_pool 表结构失败，使用 DataFrame 列名: {e}")
            valid_cols = set(df.columns)
        cols = [c for c in df.columns if c in valid_cols and c != "id"]
        records = _sanitize_records(df[cols].to_dict("records"))
        for record in records:
            for key, value in record.items():
                if value is pd.NaT:
                    record[key] = None
                elif isinstance(value, float) and (pd.isna(value) or value == float('inf') or value == float('-inf')):
                    record[key] = None
        placeholders = ", ".join([f":{c}" for c in cols])
        col_names = ", ".join([f"`{c}`" for c in cols])
        sql = text(f"INSERT INTO us_dark_pool ({col_names}) VALUES ({placeholders})")
        for i in range(0, len(records), 2000):
            batch = records[i:i + 2000]
            for attempt in range(3):
                try:
                    with self.engine.begin() as conn:
                        conn.execute(sql, batch)
                    logger.debug("upsert_us_dark_pool: 批次写入成功")
                    break
                except Exception as e:
                    if "Deadlock" in str(e) and attempt < 2:
                        import time as _time
                        _time.sleep(0.5)
                        logger.debug(f"upsert_us_dark_pool: 死锁重试 (attempt {attempt+1})")
                        continue
                    raise
        logger.info(f"us_dark_pool: 插入 {len(records)} 条记录")

    def upsert_us_congress_trade(self, df: pd.DataFrame):
        self._bulk_upsert_generic(
            USCongressTrade, df,
            ["ticker", "politician", "transaction_date", "trade_type"],
            date_cols=["transaction_date", "disclosure_date"],
        )

    def upsert_us_news(self, df: pd.DataFrame):
        self._bulk_upsert_generic(USNews, df, ["source", "url"])

    def upsert_us_daily_ratio(self, df: pd.DataFrame):
        self._bulk_upsert_generic(USDailyRatio, df, ["ticker", "date"], date_cols=["date"])

    def upsert_us_short_interest(self, df: pd.DataFrame):
        self._bulk_upsert_generic(USShortInterest, df, ["ticker", "date"], date_cols=["date"])

    def upsert_us_lobbying(self, df: pd.DataFrame):
        self._bulk_upsert_generic(USLobbyingActivity, df, ["ticker", "date", "registrant"], date_cols=["date"])

    def upsert_us_gov_contract(self, df: pd.DataFrame):
        self._bulk_upsert_generic(USGovContract, df, ["ticker", "year", "quarter"])

    def upsert_us_wsb_sentiment(self, df: pd.DataFrame):
        self._bulk_upsert_generic(USWsbSentiment, df, ["ticker", "date"], date_cols=["date"])

    def upsert_us_news_sentiment(self, df: pd.DataFrame):
        self._bulk_upsert_generic(USNewsSentiment, df, ["ticker", "date"], date_cols=["date"])

    def upsert_us_options_snapshot(self, df: pd.DataFrame):
        self._bulk_upsert_generic(USOptionsSnapshot, df, ["ticker", "date"], date_cols=["date"])

    def get_latest_us_trade_date(self, ticker: Optional[str] = None) -> Optional[str]:
        """获取美股日线最新交易日期。"""
        try:
            sql = "SELECT MAX(trade_date) as max_date FROM us_daily_price"
            if ticker:
                sql += f" WHERE ticker = '{ticker}'"
            result = self.query(sql)
            max_date = result["max_date"].iloc[0]
            if pd.isna(max_date):
                logger.debug("get_latest_us_trade_date: us_daily_price 表为空")
                return None
            return str(max_date)
        except Exception as e:
            logger.debug(f"get_latest_us_trade_date: 查询失败: {e}")
            return None

    def upsert_scrape_log(self, record: dict):
        """
        写入/更新抓取日志。

        Args:
            record: 包含 id(可选), source, started_at, finished_at,
                    articles_found, articles_new, status, error_message。
        """
        from services.sentiment.models import ScrapeLog

        with self.get_session() as session:
            if "id" in record and record["id"]:
                existing = session.query(ScrapeLog).filter_by(id=record["id"]).first()
                if existing:
                    for key, value in record.items():
                        if key != "id" and hasattr(existing, key):
                            setattr(existing, key, value)
                    session.commit()
                    return
            session.add(ScrapeLog(**{k: v for k, v in record.items() if k != "id" or v}))
            session.commit()


# ============================================================
# 便捷函数
# ============================================================

def get_db() -> DatabaseManager:
    """
    获取 DatabaseManager 单例（简化调用）。

    Returns:
        DatabaseManager 实例。
    """
    if not hasattr(get_db, "_instance"):
        get_db._instance = DatabaseManager()
        get_db._instance.init_tables()
    return get_db._instance


if __name__ == "__main__":
    # 测试：初始化数据库并打印表信息
    logging.basicConfig(
        level=LOG_LEVEL,
        format="%(asctime)s | %(name)-20s | %(levelname)-7s | %(message)s",
    )

    db = DatabaseManager()
    db.init_tables()

    # 验证表是否创建成功
    with db.engine.connect() as conn:
        tables = conn.execute(text("SHOW TABLES")).fetchall()
        print("已创建的表:")
        for t in tables:
            print(f"  - {t[0]}")
