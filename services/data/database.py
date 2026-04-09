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
# PostgreSQL 兼容：MEDIUMTEXT → Text
MEDIUMTEXT = Text
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
    """将 records 中的 NaN/NaT/inf/溢出值替换为 None，bool 转 int（PG 兼容）。"""
    for rec in records:
        for k, v in rec.items():
            if v is None:
                continue
            if isinstance(v, bool):
                rec[k] = int(v)  # PG Integer 列不接受 Python bool
            elif isinstance(v, float):
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
    name = Column(String(500), comment="公司名")
    exchange = Column(String(50), comment="交易所 (NYSE/NASDAQ)")
    sector = Column(String(100), comment="GICS 行业大类")
    industry = Column(String(200), comment="GICS 子行业")
    ipo_date = Column(Date, comment="IPO 日期")
    market_cap = Column(Float, comment="市值")
    country = Column(String(10), comment="国家")
    is_active = Column(Integer, default=1, comment="是否活跃 (1/0)")
    is_etf = Column(Integer, default=0, comment="是否ETF (FMP isEtf)")
    is_fund = Column(Integer, default=0, comment="是否基金 (FMP isFund)")
    is_actively_trading = Column(Integer, default=1, comment="是否活跃交易 (FMP isActivelyTrading)")
    beta = Column(Float, comment="Beta 系数")
    price = Column(Float, comment="最新价格")
    last_annual_dividend = Column(Float, comment="最近年度股息")
    volume = Column(Float, comment="成交量")
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    __table_args__ = (
        UniqueConstraint("ticker", name="uq_us_stock_basic_ticker"),
        Index("idx_us_stock_sector", "sector"),
        Index("idx_us_stock_is_etf", "is_etf"),
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
    # --- FMP additional fields ---
    change = Column(Float, comment="绝对涨跌额")
    label = Column(String(50), comment="日期标签 (e.g. January 02, 2024)")
    change_over_time = Column(Float, comment="区间涨跌幅")
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
    # --- Common metadata ---
    reported_currency = Column(String(10), comment="报告货币")
    cik = Column(String(20), comment="CIK 编号")
    accepted_date = Column(DateTime, comment="SEC 接受日期")
    fiscal_year = Column(String(10), comment="财年")
    # --- Income Statement (existing) ---
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
    # --- Income Statement (new FMP fields) ---
    general_and_administrative_expenses = Column(Float, comment="一般及管理费用")
    selling_and_marketing_expenses = Column(Float, comment="销售及市场费用")
    other_expenses = Column(Float, comment="其他费用")
    operating_expenses = Column(Float, comment="营业费用合计")
    cost_and_expenses = Column(Float, comment="总成本及费用")
    net_interest_income = Column(Float, comment="净利息收入")
    interest_income = Column(Float, comment="利息收入")
    interest_expense = Column(Float, comment="利息费用")
    depreciation_and_amortization = Column(Float, comment="折旧和摊销")
    ebit = Column(Float, comment="EBIT")
    non_operating_income_excluding_interest = Column(Float, comment="非经营性收入（不含利息）")
    total_other_income_expenses_net = Column(Float, comment="其他收支净额")
    income_before_tax = Column(Float, comment="税前利润")
    income_tax_expense = Column(Float, comment="所得税费用")
    net_income_from_continuing_operations = Column(Float, comment="持续经营净利润")
    net_income_from_discontinued_operations = Column(Float, comment="终止经营净利润")
    other_adjustments_to_net_income = Column(Float, comment="净利润其他调整")
    net_income_deductions = Column(Float, comment="净利润扣除项")
    bottom_line_net_income = Column(Float, comment="底线净利润")
    weighted_average_shs_out_dil = Column(Float, comment="稀释加权平均股数")
    # --- Balance Sheet (existing) ---
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
    # --- Balance Sheet (new FMP fields) ---
    short_term_investments = Column(Float, comment="短期投资")
    cash_and_short_term_investments = Column(Float, comment="现金及短期投资")
    accounts_receivables = Column(Float, comment="应收账款（明细）")
    other_receivables = Column(Float, comment="其他应收")
    prepaids = Column(Float, comment="预付款")
    other_current_assets = Column(Float, comment="其他流动资产")
    property_plant_equipment_net = Column(Float, comment="固定资产净值")
    goodwill = Column(Float, comment="商誉")
    intangible_assets = Column(Float, comment="无形资产")
    goodwill_and_intangible_assets = Column(Float, comment="商誉及无形资产合计")
    long_term_investments = Column(Float, comment="长期投资")
    tax_assets = Column(Float, comment="税务资产")
    other_non_current_assets = Column(Float, comment="其他非流动资产")
    total_non_current_assets = Column(Float, comment="非流动资产合计")
    other_assets = Column(Float, comment="其他资产")
    total_payables = Column(Float, comment="应付合计")
    account_payables = Column(Float, comment="应付账款")
    other_payables = Column(Float, comment="其他应付")
    accrued_expenses = Column(Float, comment="应计费用")
    short_term_debt = Column(Float, comment="短期借款")
    capital_lease_obligations_current = Column(Float, comment="当期资本租赁义务")
    tax_payables = Column(Float, comment="应付税款")
    deferred_revenue = Column(Float, comment="递延收入")
    other_current_liabilities = Column(Float, comment="其他流动负债")
    capital_lease_obligations_non_current = Column(Float, comment="非流动资本租赁义务")
    deferred_revenue_non_current = Column(Float, comment="非流动递延收入")
    deferred_tax_liabilities_non_current = Column(Float, comment="非流动递延所得税负债")
    other_non_current_liabilities = Column(Float, comment="其他非流动负债")
    total_non_current_liabilities = Column(Float, comment="非流动负债合计")
    other_liabilities = Column(Float, comment="其他负债")
    capital_lease_obligations = Column(Float, comment="资本租赁义务合计")
    total_liabilities = Column(Float, comment="负债合计")
    treasury_stock = Column(Float, comment="库存股")
    preferred_stock = Column(Float, comment="优先股")
    common_stock = Column(Float, comment="普通股")
    additional_paid_in_capital = Column(Float, comment="资本公积")
    accumulated_other_comprehensive_income_loss = Column(Float, comment="累计其他综合收益/损失")
    other_total_stockholders_equity = Column(Float, comment="其他股东权益")
    total_stockholders_equity = Column(Float, comment="股东权益合计")
    minority_interest = Column(Float, comment="少数股东权益")
    total_liabilities_and_total_equity = Column(Float, comment="负债及股东权益合计")
    total_investments = Column(Float, comment="投资合计")
    net_debt = Column(Float, comment="净负债")
    # --- Cash Flow (existing) ---
    operating_cash_flow = Column(Float, comment="经营活动现金流")
    capital_expenditure = Column(Float, comment="资本支出")
    free_cash_flow = Column(Float, comment="自由现金流")
    dividends_paid = Column(Float, comment="已付股息")
    share_repurchased = Column(Float, comment="股票回购金额")
    # --- Cash Flow (new FMP fields) ---
    deferred_income_tax = Column(Float, comment="递延所得税")
    stock_based_compensation = Column(Float, comment="股权激励费用")
    change_in_working_capital = Column(Float, comment="营运资本变动")
    accounts_payables = Column(Float, comment="应付账款（现金流）")
    other_working_capital = Column(Float, comment="其他营运资本")
    other_non_cash_items = Column(Float, comment="其他非现金项目")
    net_cash_provided_by_operating_activities = Column(Float, comment="经营活动净现金流")
    investments_in_property_plant_and_equipment = Column(Float, comment="固定资产投资")
    acquisitions_net = Column(Float, comment="并购净额")
    purchases_of_investments = Column(Float, comment="投资购买")
    sales_maturities_of_investments = Column(Float, comment="投资出售/到期")
    other_investing_activities = Column(Float, comment="其他投资活动")
    net_cash_provided_by_investing_activities = Column(Float, comment="投资活动净现金流")
    net_debt_issuance = Column(Float, comment="债务净发行")
    long_term_net_debt_issuance = Column(Float, comment="长期债务净发行")
    short_term_net_debt_issuance = Column(Float, comment="短期债务净发行")
    net_stock_issuance = Column(Float, comment="股票净发行")
    net_common_stock_issuance = Column(Float, comment="普通股净发行")
    common_stock_issuance = Column(Float, comment="普通股发行")
    common_stock_repurchased = Column(Float, comment="普通股回购")
    net_preferred_stock_issuance = Column(Float, comment="优先股净发行")
    net_dividends_paid = Column(Float, comment="净股息支付")
    common_dividends_paid = Column(Float, comment="普通股股息支付")
    preferred_dividends_paid = Column(Float, comment="优先股股息支付")
    other_financing_activities = Column(Float, comment="其他融资活动")
    net_cash_provided_by_financing_activities = Column(Float, comment="融资活动净现金流")
    effect_of_forex_changes_on_cash = Column(Float, comment="汇率变动对现金的影响")
    net_change_in_cash = Column(Float, comment="现金净变动")
    cash_at_end_of_period = Column(Float, comment="期末现金")
    cash_at_beginning_of_period = Column(Float, comment="期初现金")
    income_taxes_paid = Column(Float, comment="已付所得税")
    interest_paid = Column(Float, comment="已付利息")
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
    # --- FMP grades additional fields ---
    previous_grade = Column(String(50), comment="前评级")
    action = Column(String(50), comment="评级动作 (upgrade/downgrade/init/reiterate)")
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
    # --- FMP additional fields ---
    revenue_actual = Column(Float, comment="实际营收")
    revenue_estimated = Column(Float, comment="预期营收")
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
    url = Column(String(500), comment="SEC 文件链接 (FMP 字段名)")
    reporting_cik = Column(String(20), comment="报告人 CIK")
    company_cik = Column(String(20), comment="公司 CIK")
    direct_or_indirect = Column(String(5), comment="D=直接/I=间接")
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
    # --- FMP key-metrics: additional fields ---
    reported_currency = Column(String(10), comment="报告货币")
    tax_burden = Column(Float, comment="税负比率")
    interest_burden = Column(Float, comment="利息负担比率")
    operating_return_on_assets = Column(Float, comment="经营资产回报率")
    return_on_tangible_assets = Column(Float, comment="有形资产回报率")
    free_cash_flow_to_equity = Column(Float, comment="FCFE 权益自由现金流")
    free_cash_flow_to_firm = Column(Float, comment="FCFF 企业自由现金流")
    # --- FMP metrics-ratios: additional fields ---
    ebit_margin = Column(Float, comment="EBIT 利润率")
    ebitda_margin = Column(Float, comment="EBITDA 利润率")
    continuous_operations_profit_margin = Column(Float, comment="持续经营利润率")
    bottom_line_profit_margin = Column(Float, comment="底线利润率")
    solvency_ratio = Column(Float, comment="偿债能力比率")
    price_to_earnings_growth_ratio = Column(Float, comment="PEG（价格/盈利增长）")
    forward_price_to_earnings_growth_ratio = Column(Float, comment="前瞻 PEG")
    price_to_free_cash_flow_ratio = Column(Float, comment="P/FCF (ratios)")
    price_to_operating_cash_flow_ratio = Column(Float, comment="P/OCF (ratios)")
    debt_to_capital_ratio = Column(Float, comment="负债/资本比率")
    long_term_debt_to_capital_ratio = Column(Float, comment="长期负债/资本比率")
    financial_leverage_ratio = Column(Float, comment="财务杠杆比率")
    working_capital_turnover_ratio = Column(Float, comment="营运资本周转率")
    operating_cash_flow_ratio = Column(Float, comment="经营现金流比率")
    debt_service_coverage_ratio = Column(Float, comment="偿债覆盖率")
    interest_coverage_ratio = Column(Float, comment="利息覆盖率 (ratios)")
    short_term_operating_cash_flow_coverage_ratio = Column(Float, comment="短期经营现金流覆盖率")
    operating_cash_flow_coverage_ratio = Column(Float, comment="经营现金流覆盖率")
    capital_expenditure_coverage_ratio = Column(Float, comment="资本支出覆盖率")
    dividend_paid_and_capex_coverage_ratio = Column(Float, comment="股息及资本支出覆盖率")
    dividend_yield_percentage = Column(Float, comment="股息率百分比")
    net_income_per_ebt = Column(Float, comment="净利润/EBT")
    price_to_fair_value = Column(Float, comment="价格/公允价值")
    debt_to_market_cap = Column(Float, comment="负债/市值")
    enterprise_value_multiple = Column(Float, comment="企业价值倍数")
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
    # FMP senate/house-trading 额外字段
    first_name = Column(String(100), comment="议员名")
    last_name = Column(String(100), comment="议员姓")
    district = Column(String(10), comment="州代码")
    link = Column(String(500), comment="披露链接")
    comment = Column(String(500), comment="备注")
    asset_type = Column(String(50), comment="资产类型 (Stock/Option 等)")
    owner = Column(String(50), comment="持有人类型 (Self/Joint/Child 等)")
    capital_gains_over_200_usd = Column(String(10), comment="资本利得超过$200")
    source = Column(String(20), comment="数据源 (uw/fmp_senate/fmp_house)")
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


# ============================================================
# FMP 新增数据表
# ============================================================

class USCompanyProfile(Base):
    """美股公司概况表（FMP: profile）"""
    __tablename__ = "us_company_profile"

    id = Column(Integer, primary_key=True, autoincrement=True)
    ticker = Column(String(20), nullable=False, comment="股票代码")
    price = Column(Float, comment="当前价格")
    market_cap = Column(Float, comment="市值")
    beta = Column(Float, comment="Beta 系数")
    last_dividend = Column(Float, comment="最近一次股息")
    price_range = Column(String(100), comment="52周价格区间")
    change = Column(Float, comment="价格变动")
    change_percentage = Column(Float, comment="价格变动百分比")
    volume = Column(Float, comment="成交量")
    average_volume = Column(Float, comment="平均成交量")
    company_name = Column(String(500), comment="公司名称")
    currency = Column(String(20), comment="货币")
    cik = Column(String(30), comment="CIK 编号")
    isin = Column(String(30), comment="ISIN 编号")
    cusip = Column(String(30), comment="CUSIP 编号")
    exchange_full_name = Column(String(200), comment="交易所全称")
    exchange = Column(String(50), comment="交易所简称")
    industry = Column(String(300), comment="行业")
    website = Column(String(500), comment="官网")
    description = Column(Text, comment="公司描述")
    ceo = Column(String(300), comment="CEO")
    sector = Column(String(200), comment="板块")
    country = Column(String(50), comment="国家")
    full_time_employees = Column(String(50), comment="全职员工数")
    phone = Column(String(100), comment="电话")
    address = Column(String(500), comment="地址")
    city = Column(String(200), comment="城市")
    state = Column(String(100), comment="州")
    zip = Column(String(30), comment="邮编")
    image = Column(String(500), comment="Logo 图片链接")
    ipo_date = Column(String(20), comment="IPO 日期")
    default_image = Column(Integer, comment="是否默认图片")
    is_etf = Column(Integer, comment="是否 ETF")
    is_actively_trading = Column(Integer, comment="是否活跃交易")
    is_adr = Column(Integer, comment="是否 ADR")
    is_fund = Column(Integer, comment="是否基金")
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    __table_args__ = (
        UniqueConstraint("ticker", name="uq_us_company_profile_ticker"),
        Index("idx_us_company_profile_sector", "sector"),
    )


class USHistoricalMarketCap(Base):
    """美股历史市值表（FMP: historical-market-cap）"""
    __tablename__ = "us_historical_market_cap"

    id = Column(Integer, primary_key=True, autoincrement=True)
    ticker = Column(String(20), nullable=False, comment="股票代码")
    date = Column(Date, nullable=False, comment="日期")
    market_cap = Column(Float, comment="市值")
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    __table_args__ = (
        UniqueConstraint("ticker", "date", name="uq_us_hist_mktcap_ticker_date"),
        Index("idx_us_hist_mktcap_ticker", "ticker"),
        Index("idx_us_hist_mktcap_date", "date"),
    )


class USSharesFloat(Base):
    """美股流通股数据表（FMP: shares-float）"""
    __tablename__ = "us_shares_float"

    id = Column(Integer, primary_key=True, autoincrement=True)
    ticker = Column(String(20), nullable=False, comment="股票代码")
    date = Column(Date, nullable=False, comment="日期")
    free_float = Column(Float, comment="自由流通比例")
    float_shares = Column(Float, comment="流通股数")
    outstanding_shares = Column(Float, comment="总股本")
    source = Column(String(500), comment="数据来源 (SEC URL)")
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    __table_args__ = (
        UniqueConstraint("ticker", "date", name="uq_us_shares_float_ticker_date"),
        Index("idx_us_shares_float_ticker", "ticker"),
    )


class USFinancialScore(Base):
    """美股财务评分表（FMP: financial-scores）"""
    __tablename__ = "us_financial_score"

    id = Column(Integer, primary_key=True, autoincrement=True)
    ticker = Column(String(20), nullable=False, comment="股票代码")
    reported_currency = Column(String(10), comment="报告货币")
    altman_z_score = Column(Float, comment="Altman Z-Score")
    piotroski_score = Column(Float, comment="Piotroski F-Score")
    working_capital = Column(Float, comment="营运资本")
    total_assets = Column(Float, comment="总资产")
    retained_earnings = Column(Float, comment="留存收益")
    ebit = Column(Float, comment="息税前利润")
    market_cap = Column(Float, comment="市值")
    total_liabilities = Column(Float, comment="总负债")
    revenue = Column(Float, comment="营收")
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    __table_args__ = (
        UniqueConstraint("ticker", name="uq_us_financial_score_ticker"),
        Index("idx_us_financial_score_ticker", "ticker"),
    )


class USFinancialGrowth(Base):
    """美股财务增长率表（FMP: financial-statement-growth）"""
    __tablename__ = "us_financial_growth"

    id = Column(Integer, primary_key=True, autoincrement=True)
    ticker = Column(String(20), nullable=False, comment="股票代码")
    date = Column(Date, nullable=False, comment="报告日期")
    fiscal_year = Column(Integer, comment="财年")
    period = Column(String(10), comment="期间（Q1/Q2/Q3/Q4/FY）")
    reported_currency = Column(String(10), comment="报告货币")
    revenue_growth = Column(Float, comment="营收增长率")
    gross_profit_growth = Column(Float, comment="毛利润增长率")
    ebit_growth = Column(Float, comment="EBIT 增长率")
    operating_income_growth = Column(Float, comment="营业利润增长率")
    net_income_growth = Column(Float, comment="净利润增长率")
    eps_growth = Column(Float, comment="EPS 增长率")
    eps_diluted_growth = Column(Float, comment="稀释 EPS 增长率")
    weighted_average_shares_growth = Column(Float, comment="加权平均股数增长率")
    weighted_average_shares_diluted_growth = Column(Float, comment="稀释加权平均股数增长率")
    dividends_per_share_growth = Column(Float, comment="每股股息增长率")
    operating_cash_flow_growth = Column(Float, comment="经营现金流增长率")
    receivables_growth = Column(Float, comment="应收账款增长率")
    inventory_growth = Column(Float, comment="存货增长率")
    asset_growth = Column(Float, comment="资产增长率")
    book_value_per_share_growth = Column(Float, comment="每股账面价值增长率")
    debt_growth = Column(Float, comment="负债增长率")
    rd_expense_growth = Column(Float, comment="研发费用增长率")
    sga_expenses_growth = Column(Float, comment="销售管理费用增长率")
    free_cash_flow_growth = Column(Float, comment="自由现金流增长率")
    ten_y_revenue_growth_per_share = Column(Float, comment="10年每股营收增长率")
    five_y_revenue_growth_per_share = Column(Float, comment="5年每股营收增长率")
    three_y_revenue_growth_per_share = Column(Float, comment="3年每股营收增长率")
    ten_y_operating_cf_growth_per_share = Column(Float, comment="10年每股经营现金流增长率")
    five_y_operating_cf_growth_per_share = Column(Float, comment="5年每股经营现金流增长率")
    three_y_operating_cf_growth_per_share = Column(Float, comment="3年每股经营现金流增长率")
    ten_y_net_income_growth_per_share = Column(Float, comment="10年每股净利润增长率")
    five_y_net_income_growth_per_share = Column(Float, comment="5年每股净利润增长率")
    three_y_net_income_growth_per_share = Column(Float, comment="3年每股净利润增长率")
    ten_y_shareholders_equity_growth_per_share = Column(Float, comment="10年每股股东权益增长率")
    five_y_shareholders_equity_growth_per_share = Column(Float, comment="5年每股股东权益增长率")
    three_y_shareholders_equity_growth_per_share = Column(Float, comment="3年每股股东权益增长率")
    ten_y_dividend_per_share_growth_per_share = Column(Float, comment="10年每股股息增长率")
    five_y_dividend_per_share_growth_per_share = Column(Float, comment="5年每股股息增长率")
    three_y_dividend_per_share_growth_per_share = Column(Float, comment="3年每股股息增长率")
    ebitda_growth = Column(Float, comment="EBITDA 增长率")
    growth_capital_expenditure = Column(Float, comment="资本支出增长率")
    ten_y_bottom_line_net_income_growth_per_share = Column(Float, comment="10年每股底线净利润增长率")
    five_y_bottom_line_net_income_growth_per_share = Column(Float, comment="5年每股底线净利润增长率")
    three_y_bottom_line_net_income_growth_per_share = Column(Float, comment="3年每股底线净利润增长率")
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    __table_args__ = (
        UniqueConstraint("ticker", "date", name="uq_us_fin_growth_ticker_date"),
        Index("idx_us_fin_growth_ticker", "ticker"),
        Index("idx_us_fin_growth_date", "date"),
    )


class USEnterpriseValue(Base):
    """美股企业价值表（FMP: enterprise-values）"""
    __tablename__ = "us_enterprise_value"

    id = Column(Integer, primary_key=True, autoincrement=True)
    ticker = Column(String(20), nullable=False, comment="股票代码")
    date = Column(Date, nullable=False, comment="日期")
    stock_price = Column(Float, comment="股价")
    number_of_shares = Column(Float, comment="股数")
    market_capitalization = Column(Float, comment="市值")
    minus_cash_and_cash_equivalents = Column(Float, comment="减：现金及等价物")
    add_total_debt = Column(Float, comment="加：总负债")
    enterprise_value = Column(Float, comment="企业价值")
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    __table_args__ = (
        UniqueConstraint("ticker", "date", name="uq_us_ev_ticker_date"),
        Index("idx_us_ev_ticker", "ticker"),
        Index("idx_us_ev_date", "date"),
    )


class USOwnerEarnings(Base):
    """美股所有者收益表（FMP: owner-earnings）"""
    __tablename__ = "us_owner_earnings"

    id = Column(Integer, primary_key=True, autoincrement=True)
    ticker = Column(String(20), nullable=False, comment="股票代码")
    date = Column(Date, nullable=False, comment="日期")
    reported_currency = Column(String(10), comment="报告货币")
    fiscal_year = Column(Integer, comment="财年")
    period = Column(String(10), comment="期间")
    average_ppe = Column(Float, comment="平均 PPE")
    maintenance_capex = Column(Float, comment="维护性资本支出")
    owners_earnings = Column(Float, comment="所有者收益")
    growth_capex = Column(Float, comment="成长性资本支出")
    owners_earnings_per_share = Column(Float, comment="每股所有者收益")
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    __table_args__ = (
        UniqueConstraint("ticker", "date", name="uq_us_owner_earnings_ticker_date"),
        Index("idx_us_owner_earnings_ticker", "ticker"),
    )


class USRevenueSegment(Base):
    """美股营收分部表（FMP: revenue-geographic/product-segmentation）"""
    __tablename__ = "us_revenue_segment"

    id = Column(Integer, primary_key=True, autoincrement=True)
    ticker = Column(String(20), nullable=False, comment="股票代码")
    date = Column(Date, nullable=False, comment="日期")
    segment_type = Column(String(20), nullable=False, comment="分部类型：geographic/product")
    segment_name = Column(String(200), nullable=False, comment="分部名称")
    revenue = Column(Float, comment="营收")
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    __table_args__ = (
        UniqueConstraint("ticker", "date", "segment_type", "segment_name",
                         name="uq_us_rev_segment"),
        Index("idx_us_rev_segment_ticker", "ticker"),
        Index("idx_us_rev_segment_date", "date"),
    )


class USDCFValuation(Base):
    """美股 DCF 估值表（FMP: dcf-advanced/dcf-levered）"""
    __tablename__ = "us_dcf_valuation"

    id = Column(Integer, primary_key=True, autoincrement=True)
    ticker = Column(String(20), nullable=False, comment="股票代码")
    date = Column(Date, nullable=False, comment="日期")
    dcf_type = Column(String(20), nullable=False, comment="DCF 类型：standard/levered")
    dcf = Column(Float, comment="DCF 估值")
    stock_price = Column(Float, comment="当前股价")
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    __table_args__ = (
        UniqueConstraint("ticker", "date", "dcf_type", name="uq_us_dcf_ticker_date_type"),
        Index("idx_us_dcf_ticker", "ticker"),
    )


class USStockPeer(Base):
    """美股同行公司表（FMP: peers）"""
    __tablename__ = "us_stock_peer"

    id = Column(Integer, primary_key=True, autoincrement=True)
    ticker = Column(String(20), nullable=False, comment="股票代码")
    peer_ticker = Column(String(20), nullable=False, comment="同行股票代码")
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    __table_args__ = (
        UniqueConstraint("ticker", "peer_ticker", name="uq_us_stock_peer"),
        Index("idx_us_stock_peer_ticker", "ticker"),
    )


class USESGRating(Base):
    """美股 ESG 评级表（FMP: esg-ratings）"""
    __tablename__ = "us_esg_rating"

    id = Column(Integer, primary_key=True, autoincrement=True)
    ticker = Column(String(20), nullable=False, comment="股票代码")
    cik = Column(String(20), comment="CIK 编号")
    company_name = Column(String(300), comment="公司名称")
    industry = Column(String(200), comment="行业")
    year = Column(Integer, nullable=False, comment="年份")
    esg_score = Column(Float, comment="ESG 综合分数")
    environment_score = Column(Float, comment="环境分数")
    social_score = Column(Float, comment="社会分数")
    governance_score = Column(Float, comment="治理分数")
    esg_risk_rating = Column(String(50), comment="ESG 风险评级")
    industry_rank = Column(String(50), comment="行业排名 (如 '4 out of 6')")
    fiscal_year = Column(Integer, comment="财年（FMP 用 fiscalYear）")
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    __table_args__ = (
        UniqueConstraint("ticker", "year", name="uq_us_esg_rating_ticker_year"),
        Index("idx_us_esg_rating_ticker", "ticker"),
    )


class USInstitutionalHolding(Base):
    """美股机构持仓表（FMP: form13F filings-extract）"""
    __tablename__ = "us_institutional_holding"

    id = Column(Integer, primary_key=True, autoincrement=True)
    ticker = Column(String(20), nullable=False, comment="股票代码")
    cik = Column(String(20), nullable=False, comment="机构 CIK 编号")
    filing_date = Column(Date, nullable=False, comment="申报日期")
    investor_name = Column(String(300), comment="投资者名称")
    security_name = Column(String(300), comment="证券名称")
    shares_number = Column(Float, comment="持股数量")
    total_invested_value = Column(Float, comment="总投资价值")
    ownership_percent = Column(Float, comment="持股比例")
    change_in_shares_number_percentage = Column(Float, comment="持股数量变化百分比")
    change_in_shares_number = Column(Float, comment="持股数量变化")
    is_new = Column(Integer, comment="是否新建仓")
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    __table_args__ = (
        UniqueConstraint("ticker", "cik", "filing_date",
                         name="uq_us_inst_holding_ticker_cik_date"),
        Index("idx_us_inst_holding_ticker", "ticker"),
        Index("idx_us_inst_holding_filing_date", "filing_date"),
    )


class USPriceTarget(Base):
    """美股分析师目标价共识表（FMP: price-target-consensus/summary）"""
    __tablename__ = "us_price_target"

    id = Column(Integer, primary_key=True, autoincrement=True)
    ticker = Column(String(20), nullable=False, comment="股票代码")
    target_high = Column(Float, comment="最高目标价")
    target_low = Column(Float, comment="最低目标价")
    target_consensus = Column(Float, comment="共识目标价")
    target_median = Column(Float, comment="中位数目标价")
    last_month = Column(Integer, comment="近一个月分析师数")
    last_month_avg_price_target = Column(Float, comment="近一个月平均目标价")
    last_quarter = Column(Integer, comment="近一季度分析师数")
    last_quarter_avg_price_target = Column(Float, comment="近一季度平均目标价")
    last_year = Column(Integer, comment="近一年分析师数")
    last_year_avg_price_target = Column(Float, comment="近一年平均目标价")
    all_time = Column(Integer, comment="历史总分析师数")
    all_time_avg_price_target = Column(Float, comment="历史平均目标价")
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    __table_args__ = (
        UniqueConstraint("ticker", name="uq_us_price_target_ticker"),
        Index("idx_us_price_target_ticker", "ticker"),
    )


class USPressRelease(Base):
    """美股新闻稿表（FMP: search-press-releases）"""
    __tablename__ = "us_press_release"

    id = Column(Integer, primary_key=True, autoincrement=True)
    ticker = Column(String(20), nullable=False, comment="股票代码")
    date = Column(Date, nullable=False, comment="日期")
    title = Column(String(500), nullable=False, comment="标题")
    text = Column(Text, comment="正文内容")
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    __table_args__ = (
        UniqueConstraint("ticker", "date", "title", name="uq_us_press_release"),
        Index("idx_us_press_release_ticker", "ticker"),
        Index("idx_us_press_release_date", "date"),
    )


class USInsiderStatistic(Base):
    """美股内部人交易统计表（FMP: insider-trade-statistics）"""
    __tablename__ = "us_insider_statistic"

    id = Column(Integer, primary_key=True, autoincrement=True)
    ticker = Column(String(20), nullable=False, comment="股票代码")
    year = Column(Integer, nullable=False, comment="年份")
    quarter = Column(Integer, nullable=False, comment="季度")
    purchases = Column(Integer, comment="买入次数")
    sales = Column(Integer, comment="卖出次数")
    buy_sell_ratio = Column(Float, comment="买卖比")
    total_bought = Column(Float, comment="总买入金额")
    total_sold = Column(Float, comment="总卖出金额")
    average_bought = Column(Float, comment="平均买入金额")
    average_sold = Column(Float, comment="平均卖出金额")
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    __table_args__ = (
        UniqueConstraint("ticker", "year", "quarter", name="uq_us_insider_stat"),
        Index("idx_us_insider_stat_ticker", "ticker"),
    )


class USEmployeeCount(Base):
    """美股员工数量历史表（FMP: historical-employee-count）"""
    __tablename__ = "us_employee_count"

    id = Column(Integer, primary_key=True, autoincrement=True)
    ticker = Column(String(20), nullable=False, comment="股票代码")
    period_of_report = Column(Date, nullable=False, comment="报告期")
    employee_count = Column(Integer, comment="员工数量")
    filing_date = Column(Date, comment="申报日期")
    accepted_date = Column(DateTime, comment="受理日期")
    source = Column(String(500), comment="数据来源")
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    __table_args__ = (
        UniqueConstraint("ticker", "period_of_report", name="uq_us_employee_count"),
        Index("idx_us_employee_count_ticker", "ticker"),
    )


class USIndexConstituent(Base):
    """美股指数成分股历史表（FMP: historical-sp-500 等）"""
    __tablename__ = "us_index_constituent"

    id = Column(Integer, primary_key=True, autoincrement=True)
    index_name = Column(String(20), nullable=False, comment="指数名称：sp500/nasdaq/dow")
    ticker = Column(String(20), nullable=False, comment="股票代码")
    date = Column(Date, nullable=False, comment="日期")
    date_added = Column(Date, comment="加入日期")
    added_security = Column(String(300), comment="加入的证券名称")
    removed_ticker = Column(String(20), comment="被移除的股票代码")
    removed_security = Column(String(300), comment="被移除的证券名称")
    reason = Column(String(500), comment="变更原因")
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    __table_args__ = (
        UniqueConstraint("index_name", "ticker", "date",
                         name="uq_us_index_constituent"),
        Index("idx_us_index_constituent_index", "index_name"),
        Index("idx_us_index_constituent_ticker", "ticker"),
    )


class USSymbolChange(Base):
    """美股代码变更表（FMP: symbol-changes-list）"""
    __tablename__ = "us_symbol_change"

    id = Column(Integer, primary_key=True, autoincrement=True)
    date = Column(Date, nullable=False, comment="变更日期")
    name = Column(String(300), comment="公司名称")
    old_symbol = Column(String(20), nullable=False, comment="旧代码")
    new_symbol = Column(String(20), nullable=False, comment="新代码")
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    __table_args__ = (
        UniqueConstraint("old_symbol", "new_symbol", "date",
                         name="uq_us_symbol_change"),
        Index("idx_us_symbol_change_date", "date"),
    )


class USDelisted(Base):
    """美股退市公司表（FMP: delisted-companies）"""
    __tablename__ = "us_delisted"

    id = Column(Integer, primary_key=True, autoincrement=True)
    ticker = Column(String(20), nullable=False, comment="股票代码")
    company_name = Column(String(300), comment="公司名称")
    exchange = Column(String(50), comment="交易所")
    ipo_date = Column(Date, comment="IPO 日期")
    delisted_date = Column(Date, comment="退市日期")
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    __table_args__ = (
        UniqueConstraint("ticker", name="uq_us_delisted_ticker"),
        Index("idx_us_delisted_date", "delisted_date"),
    )


class ImportProgress(Base):
    """导入进度表：记录每个 (table, ticker) 是否完整导入"""
    __tablename__ = "import_progress"

    id = Column(Integer, primary_key=True, autoincrement=True)
    table_name = Column(String(50), nullable=False, comment="目标表名")
    ticker = Column(String(20), nullable=False, comment="股票代码")
    completed_at = Column(DateTime, default=datetime.now, comment="完成时间")

    __table_args__ = (
        UniqueConstraint("table_name", "ticker", name="uq_import_progress"),
        Index("idx_import_progress_table", "table_name"),
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
                pool_size=70,
                max_overflow=20,
                pool_recycle=3600,
                pool_pre_ping=True,  # 使用前检测连接是否存活，防止远程 PG 超时断开
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
        # PostgreSQL: 检查 schema 是否存在（不尝试创建，避免权限问题）
        from services.config import DB_SCHEMA
        if DB_SCHEMA:
            try:
                result = self.query(
                    "SELECT 1 FROM information_schema.schemata WHERE schema_name = :s",
                    params={"s": DB_SCHEMA},
                )
                if result.empty:
                    logger.warning(f"Schema '{DB_SCHEMA}' 不存在，请让 DBA 创建: "
                                   f"CREATE SCHEMA {DB_SCHEMA} AUTHORIZATION <user>")
                    return
            except Exception as e:
                logger.warning(f"检查 schema 失败: {e}")

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
                cols = [r[0] for r in conn.execute(text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'daily_price'"
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
            ("policy_article", "content", "TEXT"),
            ("policy_analysis", "impact_type", "VARCHAR(30)"),
        ]
        try:
            with self.engine.connect() as conn:
                for table, col_name, col_type in migrations:
                    cols = [r[0] for r in conn.execute(text(
                        f"SELECT column_name FROM information_schema.columns "
                        f"WHERE table_name = '{table}'"
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
        批量 upsert 日线行情（PostgreSQL ON CONFLICT DO UPDATE）。
        适用于增量更新场景，遇到重复自动更新，比逐条 ORM upsert 快 50-100 倍。

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
        # open 是 PostgreSQL 保留字，需要双引号
        col_names = ", ".join([f'"{c}"' for c in existing_cols])
        update_clause = ", ".join([f'"{c}" = EXCLUDED."{c}"' for c in update_cols])

        sql = text(
            f"INSERT INTO daily_price ({col_names}) VALUES ({placeholders}) "
            f'ON CONFLICT ("ts_code", "trade_date") DO UPDATE SET {update_clause}'
        )

        records = _sanitize_records(df[existing_cols].to_dict("records"))
        batch_size = 1000
        with self.engine.begin() as conn:
            for i in range(0, len(records), batch_size):
                batch = records[i:i + batch_size]
                conn.execute(sql, batch)

        logger.info(f"daily_price: 批量upsert {len(records)} 条记录")

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
                f"UPDATE financial_data AS f "
                f"SET {', '.join(set_parts)} "
                f"FROM ("
                f"  SELECT ts_code, MAX(end_date) AS latest_end "
                f"  FROM financial_data "
                f"  WHERE ts_code IN ('{codes_str}') "
                f"  GROUP BY ts_code"
                f") sub "
                f"WHERE f.ts_code = sub.ts_code AND f.end_date = sub.latest_end"
            )
            with self.engine.begin() as conn:
                conn.execute(text(sql))
            logger.info(f"financial_data: 批量更新 {len(updates)} 条估值数据")
        else:
            logger.debug("batch_update_financial: 无有效估值数据需更新，跳过")

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
        col_names = ", ".join([f'"{c}"' for c in existing_cols])
        update_clause = ", ".join([f'"{c}" = EXCLUDED."{c}"' for c in update_cols])

        sql = text(
            f"INSERT INTO commodity_price ({col_names}) VALUES ({placeholders}) "
            f'ON CONFLICT ("commodity_code", "trade_date") DO UPDATE SET {update_clause}'
        )

        records = _sanitize_records(df[existing_cols].to_dict("records"))
        batch_size = 1000
        with self.engine.begin() as conn:
            for i in range(0, len(records), batch_size):
                batch = records[i:i + batch_size]
                conn.execute(sql, batch)
        logger.info(f"commodity_price: 批量upsert {len(records)} 条记录")

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

        cols = ["indicator_code", "report_date", "value", "updated_at"]
        df = df.copy()
        df["updated_at"] = datetime.now()
        existing_cols = [c for c in cols if c in df.columns]
        update_cols = [c for c in existing_cols if c not in ("indicator_code", "report_date")]

        placeholders = ", ".join([f":{c}" for c in existing_cols])
        col_names = ", ".join([f'"{c}"' for c in existing_cols])
        update_clause = ", ".join([f'"{c}" = EXCLUDED."{c}"' for c in update_cols])

        sql = text(
            f"INSERT INTO macro_indicator ({col_names}) VALUES ({placeholders}) "
            f'ON CONFLICT ("indicator_code", "report_date") DO UPDATE SET {update_clause}'
        )

        records = _sanitize_records(df[existing_cols].to_dict("records"))
        batch_size = 1000
        with self.engine.begin() as conn:
            for i in range(0, len(records), batch_size):
                batch = records[i:i + batch_size]
                conn.execute(sql, batch)
        logger.info(f"macro_indicator: 批量upsert {len(records)} 条记录")

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
        批量 upsert 政策文章（PostgreSQL ON CONFLICT DO UPDATE）。

        Args:
            articles: 文章字典列表。

        Returns:
            新增文章数（近似值）。
        """
        if not articles:
            logger.debug("bulk_upsert_policy_articles: 文章列表为空，跳过")
            return 0

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
        col_names = ", ".join([f'"{c}"' for c in existing_cols])
        update_clause = ", ".join([f'"{c}" = EXCLUDED."{c}"' for c in update_cols])

        sql = text(
            f"INSERT INTO policy_article ({col_names}) VALUES ({placeholders}) "
            f'ON CONFLICT ("url") DO UPDATE SET {update_clause}'
        )

        batch_size = 500
        with self.engine.begin() as conn:
            for i in range(0, len(articles), batch_size):
                batch = articles[i:i + batch_size]
                conn.execute(sql, batch)

        logger.info(f"policy_article: 批量upsert {len(articles)} 条")
        return len(articles)  # 近似值

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

    def get_us_tickers(self, active_only: bool = True, stocks_only: bool = False) -> list[str]:
        """获取美股代码列表。

        Args:
            active_only: 只返回活跃 ticker
            stocks_only: 只返回普通股（排除 ETF/基金，用 FMP 的 isEtf/isFund 标记）
        """
        conditions = []
        if active_only:
            conditions.append("is_active = 1")
        if stocks_only:
            conditions.append("(is_etf = 0 OR is_etf IS NULL)")
            conditions.append("(is_fund = 0 OR is_fund IS NULL)")
        sql = "SELECT ticker FROM us_stock_basic"
        if conditions:
            sql += " WHERE " + " AND ".join(conditions)
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
        """通用快速 bulk upsert — PostgreSQL INSERT ... ON CONFLICT DO UPDATE。"""
        if df.empty:
            logger.debug(f"{table_name}: DataFrame 为空，跳过写入")
            return
        df = df.copy()
        # Date columns → date only, NaT → None
        for col in (date_cols or []):
            if col in df.columns:
                converted = pd.to_datetime(df[col], errors="coerce")
                df[col] = converted.apply(lambda x: x.date() if pd.notna(x) else None)
        # Datetime columns → Python datetime
        for col in (datetime_cols or []):
            if col in df.columns:
                df[col] = pd.to_datetime(df[col], errors="coerce")
                df[col] = df[col].apply(lambda x: x.to_pydatetime() if pd.notna(x) else None)
        # Auto-detect: any remaining string columns with ISO datetime patterns
        for col in df.columns:
            if str(df[col].dtype) in ("object", "string", "str") and col not in (date_cols or []):
                sample = df[col].dropna().head(5)
                if not sample.empty and sample.astype(str).str.match(r"\d{4}-\d{2}-\d{2}T").any():
                    df[col] = pd.to_datetime(df[col], errors="coerce")
                    df[col] = df[col].apply(lambda x: x.to_pydatetime() if pd.notna(x) else None)
        df["updated_at"] = datetime.now()

        # PostgreSQL: bool → int（PG Integer 列不接受 Python bool）
        for col in df.columns:
            if df[col].dtype == bool or str(df[col].dtype) == "boolean":
                df[col] = df[col].astype(int)

        # Only keep columns that exist in the table (缓存避免重复查 information_schema)
        if not hasattr(self, '_table_cols_cache'):
            self._table_cols_cache = {}
        if table_name not in self._table_cols_cache:
            try:
                table_cols_df = self.query(
                    "SELECT column_name FROM information_schema.columns "
                    f"WHERE table_name = '{table_name}'"
                )
                self._table_cols_cache[table_name] = set(table_cols_df["column_name"].tolist())
            except Exception:
                self._table_cols_cache[table_name] = None
        try:
            valid_cols = self._table_cols_cache[table_name]
            if valid_cols is None:
                raise Exception("cached None")
        except Exception:
            valid_cols = set(df.columns)
        cols = [c for c in df.columns if c in valid_cols and c != "id"]
        if not cols:
            logger.warning(f"{table_name}: DataFrame 列 {list(df.columns)} 与表列 {valid_cols} 无交集，跳过")
            return

        records = _sanitize_records(df[cols].to_dict("records"))

        update_cols = [c for c in cols if c not in unique_keys and c != "id"]
        col_names = ", ".join([f'"{c}"' for c in cols])
        conflict_keys = ", ".join([f'"{c}"' for c in unique_keys])
        update_clause = ", ".join([f'"{c}" = EXCLUDED."{c}"' for c in update_cols])

        # 使用 psycopg2 execute_values 批量写入（比 executemany 快 10-50 倍）
        from psycopg2.extras import execute_values
        sql_str = (
            f'INSERT INTO "{table_name}" ({col_names}) VALUES %s '
            f"ON CONFLICT ({conflict_keys}) DO UPDATE SET {update_clause}"
        )

        def _do_write(sql, vals, tbl, bs):
            for attempt in range(3):
                try:
                    raw_conn = self.engine.raw_connection()
                    try:
                        cursor = raw_conn.cursor()
                        execute_values(cursor, sql, vals, page_size=bs)
                        raw_conn.commit()
                    finally:
                        raw_conn.close()
                    logger.debug(f"_fast_bulk_upsert: {tbl} 批次写入成功")
                    return
                except Exception as e:
                    if "deadlock" in str(e).lower() and attempt < 2:
                        import time as _time
                        _time.sleep(0.5 * (attempt + 1))
                        continue
                    raise

        # 异步写入线程池（多线程并行写 DB）
        if not hasattr(self, '_write_pool'):
            from concurrent.futures import ThreadPoolExecutor as _TPE
            self._write_pool = _TPE(max_workers=50)
            self._write_futures = []

        for i in range(0, len(records), batch_size):
            batch = records[i:i + batch_size]
            values = [tuple(rec.get(c) for c in cols) for rec in batch]
            fut = self._write_pool.submit(_do_write, sql_str, values, table_name, batch_size)
            self._write_futures.append(fut)

        # 清理已完成的 future + 背压：待写任务超 200 个时等待一批完成
        self._write_futures = [f for f in self._write_futures if not f.done()]
        if len(self._write_futures) > 1000:
            from concurrent.futures import wait, FIRST_COMPLETED
            done, self._write_futures = wait(self._write_futures, return_when=FIRST_COMPLETED)
            self._write_futures = list(self._write_futures)
            for f in done:
                if f.exception():
                    logger.warning(f"异步写入失败: {f.exception()}")

        logger.info(f"{table_name}: 批量upsert {len(records)} 条记录")

    def flush_writes(self):
        """等待所有异步写入完成。"""
        if hasattr(self, '_write_futures'):
            from concurrent.futures import wait
            wait(self._write_futures)
            # 检查是否有失败的
            for f in self._write_futures:
                if f.exception():
                    logger.warning(f"异步写入有失败: {f.exception()}")
            self._write_futures.clear()

    def mark_import_done(self, table_name: str, ticker: str):
        """标记某个 (table, ticker) 导入完成。"""
        from sqlalchemy import text
        sql = text(
            'INSERT INTO "import_progress" ("table_name", "ticker", "completed_at") '
            'VALUES (:table_name, :ticker, :completed_at) '
            'ON CONFLICT ("table_name", "ticker") DO UPDATE SET "completed_at" = EXCLUDED."completed_at"'
        )
        with self.engine.begin() as conn:
            conn.execute(sql, {"table_name": table_name, "ticker": ticker, "completed_at": datetime.now()})

    def get_import_done_tickers(self, table_name: str) -> set[str]:
        """获取某个表已完成导入的 ticker 集合。"""
        try:
            result = self.query(
                "SELECT ticker FROM import_progress WHERE table_name = :t",
                params={"t": table_name},
            )
            return set(result["ticker"].tolist()) if not result.empty else set()
        except Exception:
            return set()

    def _fast_bulk_upsert_sync(self, table_name: str, df: pd.DataFrame,
                          unique_keys: list[str], date_cols: list[str] = None,
                          datetime_cols: list[str] = None,
                          batch_size: int = 500):
        """同步版 _fast_bulk_upsert，用于需要立即确认写入的场景。"""
        # 调用异步版本后立即 flush
        self._fast_bulk_upsert(table_name, df, unique_keys, date_cols, datetime_cols, batch_size)
        self.flush_writes()

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
            table_cols_df = self.query(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'us_dark_pool'"
            )
            valid_cols = set(table_cols_df["column_name"].tolist())
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
        col_names = ", ".join([f'"{c}"' for c in cols])
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

    # ----------------------------------------------------------
    # FMP 新增表 upsert 方法
    # ----------------------------------------------------------

    def upsert_us_company_profile(self, df: pd.DataFrame):
        """批量写入/更新美股公司概况。"""
        self._fast_bulk_upsert("us_company_profile", df, ["ticker"],
                               date_cols=["ipo_date"])

    def upsert_us_historical_market_cap(self, df: pd.DataFrame):
        """批量写入/更新美股历史市值。"""
        self._fast_bulk_upsert("us_historical_market_cap", df, ["ticker", "date"],
                               date_cols=["date"])

    def upsert_us_shares_float(self, df: pd.DataFrame):
        """批量写入/更新美股流通股数据。"""
        self._fast_bulk_upsert("us_shares_float", df, ["ticker", "date"],
                               date_cols=["date"])

    def upsert_us_financial_score(self, df: pd.DataFrame):
        """批量写入/更新美股财务评分。"""
        self._fast_bulk_upsert("us_financial_score", df, ["ticker"])

    def upsert_us_financial_growth(self, df: pd.DataFrame):
        """批量写入/更新美股财务增长率。"""
        self._fast_bulk_upsert("us_financial_growth", df, ["ticker", "date"],
                               date_cols=["date"])

    def upsert_us_enterprise_value(self, df: pd.DataFrame):
        """批量写入/更新美股企业价值。"""
        self._fast_bulk_upsert("us_enterprise_value", df, ["ticker", "date"],
                               date_cols=["date"])

    def upsert_us_owner_earnings(self, df: pd.DataFrame):
        """批量写入/更新美股所有者收益。"""
        self._fast_bulk_upsert("us_owner_earnings", df, ["ticker", "date"],
                               date_cols=["date"])

    def upsert_us_revenue_segment(self, df: pd.DataFrame):
        """批量写入/更新美股营收分部数据。"""
        self._fast_bulk_upsert("us_revenue_segment", df,
                               ["ticker", "date", "segment_type", "segment_name"],
                               date_cols=["date"])

    def upsert_us_dcf_valuation(self, df: pd.DataFrame):
        """批量写入/更新美股 DCF 估值。"""
        self._fast_bulk_upsert("us_dcf_valuation", df, ["ticker", "date", "dcf_type"],
                               date_cols=["date"])

    def upsert_us_stock_peer(self, df: pd.DataFrame):
        """批量写入/更新美股同行公司。"""
        self._fast_bulk_upsert("us_stock_peer", df, ["ticker", "peer_ticker"])

    def upsert_us_esg_rating(self, df: pd.DataFrame):
        """批量写入/更新美股 ESG 评级。"""
        self._fast_bulk_upsert("us_esg_rating", df, ["ticker", "year"])

    def upsert_us_institutional_holding(self, df: pd.DataFrame):
        """批量写入/更新美股机构持仓。"""
        self._fast_bulk_upsert("us_institutional_holding", df,
                               ["ticker", "cik", "filing_date"],
                               date_cols=["filing_date"])

    def upsert_us_price_target(self, df: pd.DataFrame):
        """批量写入/更新美股分析师目标价共识。"""
        self._fast_bulk_upsert("us_price_target", df, ["ticker"])

    def upsert_us_press_release(self, df: pd.DataFrame):
        """批量写入/更新美股新闻稿。"""
        self._fast_bulk_upsert("us_press_release", df, ["ticker", "date", "title"],
                               date_cols=["date"])

    def upsert_us_insider_statistic(self, df: pd.DataFrame):
        """批量写入/更新美股内部人交易统计。"""
        self._fast_bulk_upsert("us_insider_statistic", df, ["ticker", "year", "quarter"])

    def upsert_us_employee_count(self, df: pd.DataFrame):
        """批量写入/更新美股员工数量历史。"""
        self._fast_bulk_upsert("us_employee_count", df, ["ticker", "period_of_report"],
                               date_cols=["period_of_report", "filing_date"],
                               datetime_cols=["accepted_date"])

    def upsert_us_index_constituent(self, df: pd.DataFrame):
        """批量写入/更新美股指数成分股历史。"""
        self._fast_bulk_upsert("us_index_constituent", df,
                               ["index_name", "ticker", "date"],
                               date_cols=["date", "date_added"])

    def upsert_us_symbol_change(self, df: pd.DataFrame):
        """批量写入/更新美股代码变更。"""
        self._fast_bulk_upsert("us_symbol_change", df,
                               ["old_symbol", "new_symbol", "date"],
                               date_cols=["date"])

    def upsert_us_delisted(self, df: pd.DataFrame):
        """批量写入/更新美股退市公司。"""
        self._fast_bulk_upsert("us_delisted", df, ["ticker"],
                               date_cols=["ipo_date", "delisted_date"])

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
