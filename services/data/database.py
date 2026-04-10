"""
数据库模块（PostgreSQL）

所有 US model 列名必须与 _camel_to_snake(API字段) 完全一致，禁止手动起别名。
使用 SQLAlchemy ORM 读写，禁止 raw SQL。
"""

import logging
import math
from datetime import datetime
from typing import Optional

import pandas as pd
from sqlalchemy import (
    Column, Float, Integer, String, Text, Date, DateTime,
    UniqueConstraint, Index, create_engine, text, inspect,
)
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from services.config import DB_URL, LOG_LEVEL

logger = logging.getLogger(__name__)
logger.setLevel(LOG_LEVEL)

_FLOAT_MAX = 3.4e38


# ============================================================
# ORM 基类
# ============================================================

class Base(DeclarativeBase):
    pass


# ============================================================
# 美股表定义 — 列名 = _camel_to_snake(FMP API 字段)
# ============================================================

# --- 1. us_stock_basic (FMP stock-screener) ---
class USStockBasic(Base):
    """美股基本信息表"""
    __tablename__ = "us_stock_basic"

    id = Column(Integer, primary_key=True, autoincrement=True)
    # FMP: symbol → ticker (唯一的固定映射)
    ticker = Column(String(20), nullable=False)
    company_name = Column(String(500))
    market_cap = Column(Float)
    sector = Column(String(200))
    industry = Column(String(300))
    beta = Column(Float)
    price = Column(Float)
    last_annual_dividend = Column(Float)
    volume = Column(Float)
    exchange = Column(String(200))          # 全名: "New York Stock Exchange"
    exchange_short_name = Column(String(50)) # 简称: "NYSE"
    country = Column(String(50))
    is_etf = Column(Integer, default=0)
    is_fund = Column(Integer, default=0)
    is_actively_trading = Column(Integer, default=1)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    __table_args__ = (
        UniqueConstraint("ticker", name="uq_us_stock_basic_ticker"),
        Index("idx_us_stock_sector", "sector"),
        Index("idx_us_stock_is_etf", "is_etf"),
    )


# --- 2. us_daily_price (FMP historical-price-eod/full) ---
class USDailyPrice(Base):
    """美股日线行情表"""
    __tablename__ = "us_daily_price"

    id = Column(Integer, primary_key=True, autoincrement=True)
    ticker = Column(String(20), nullable=False)
    # FMP 返回 date，下载时必须 rename 为 trade_date（unique key 需要）
    trade_date = Column(Date, nullable=False)
    open = Column(Float)
    high = Column(Float)
    low = Column(Float)
    close = Column(Float)
    adj_close = Column(Float)
    volume = Column(Float)
    unadjusted_volume = Column(Float)
    change = Column(Float)
    change_percent = Column(Float)
    vwap = Column(Float)
    label = Column(String(50))
    change_over_time = Column(Float)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    __table_args__ = (
        UniqueConstraint("ticker", "trade_date", name="uq_us_daily_ticker_date"),
        Index("idx_us_daily_trade_date", "trade_date"),
        Index("idx_us_daily_ticker", "ticker"),
    )


# --- 3. us_financial_data (FMP income-statement + balance-sheet + cash-flow 合并) ---
# 三表字段合并，去重后的完整列表
class USFinancialData(Base):
    """美股财务数据表（季报，IS+BS+CF 三表合并）"""
    __tablename__ = "us_financial_data"

    id = Column(Integer, primary_key=True, autoincrement=True)
    ticker = Column(String(20), nullable=False)
    # period_label 由下载代码从 fiscal_year + period 构造，用作 unique key
    period = Column(String(10), nullable=False)
    date = Column(Date)
    # --- 三表共有元数据 ---
    reported_currency = Column(String(10))
    cik = Column(String(20))
    filing_date = Column(Date)
    accepted_date = Column(String(30))
    fiscal_year = Column(String(10))
    # --- Income Statement ---
    revenue = Column(Float)
    cost_of_revenue = Column(Float)
    gross_profit = Column(Float)
    research_and_development_expenses = Column(Float)
    general_and_administrative_expenses = Column(Float)
    selling_and_marketing_expenses = Column(Float)
    selling_general_and_administrative_expenses = Column(Float)
    other_expenses = Column(Float)
    operating_expenses = Column(Float)
    cost_and_expenses = Column(Float)
    net_interest_income = Column(Float)
    interest_income = Column(Float)
    interest_expense = Column(Float)
    depreciation_and_amortization = Column(Float)
    ebitda = Column(Float)
    ebit = Column(Float)
    non_operating_income_excluding_interest = Column(Float)
    operating_income = Column(Float)
    total_other_income_expenses_net = Column(Float)
    income_before_tax = Column(Float)
    income_tax_expense = Column(Float)
    net_income_from_continuing_operations = Column(Float)
    net_income_from_discontinued_operations = Column(Float)
    other_adjustments_to_net_income = Column(Float)
    net_income = Column(Float)
    net_income_deductions = Column(Float)
    bottom_line_net_income = Column(Float)
    eps = Column(Float)
    eps_diluted = Column(Float)
    weighted_average_shs_out = Column(Float)
    weighted_average_shs_out_dil = Column(Float)
    # --- IS 比率 (gross_profit_ratio 等转成以下) ---
    gross_profit_ratio = Column(Float)
    operating_income_ratio = Column(Float)
    net_income_ratio = Column(Float)
    # --- Balance Sheet ---
    cash_and_cash_equivalents = Column(Float)
    short_term_investments = Column(Float)
    cash_and_short_term_investments = Column(Float)
    net_receivables = Column(Float)
    accounts_receivables = Column(Float)
    other_receivables = Column(Float)
    inventory = Column(Float)
    prepaids = Column(Float)
    other_current_assets = Column(Float)
    total_current_assets = Column(Float)
    property_plant_equipment_net = Column(Float)
    goodwill = Column(Float)
    intangible_assets = Column(Float)
    goodwill_and_intangible_assets = Column(Float)
    long_term_investments = Column(Float)
    tax_assets = Column(Float)
    other_non_current_assets = Column(Float)
    total_non_current_assets = Column(Float)
    other_assets = Column(Float)
    total_assets = Column(Float)
    total_payables = Column(Float)
    account_payables = Column(Float)
    other_payables = Column(Float)
    accrued_expenses = Column(Float)
    short_term_debt = Column(Float)
    capital_lease_obligations_current = Column(Float)
    tax_payables = Column(Float)
    deferred_revenue = Column(Float)
    other_current_liabilities = Column(Float)
    total_current_liabilities = Column(Float)
    long_term_debt = Column(Float)
    capital_lease_obligations_non_current = Column(Float)
    deferred_revenue_non_current = Column(Float)
    deferred_tax_liabilities_non_current = Column(Float)
    other_non_current_liabilities = Column(Float)
    total_non_current_liabilities = Column(Float)
    other_liabilities = Column(Float)
    capital_lease_obligations = Column(Float)
    total_liabilities = Column(Float)
    treasury_stock = Column(Float)
    preferred_stock = Column(Float)
    common_stock = Column(Float)
    retained_earnings = Column(Float)
    additional_paid_in_capital = Column(Float)
    accumulated_other_comprehensive_income_loss = Column(Float)
    other_total_stockholders_equity = Column(Float)
    total_stockholders_equity = Column(Float)
    total_equity = Column(Float)
    minority_interest = Column(Float)
    total_liabilities_and_total_equity = Column(Float)
    total_investments = Column(Float)
    total_debt = Column(Float)
    net_debt = Column(Float)
    # --- Cash Flow ---
    # net_income 已在 IS 定义，CF 中重复但 ORM 只定义一次
    # depreciation_and_amortization 同上
    deferred_income_tax = Column(Float)
    stock_based_compensation = Column(Float)
    change_in_working_capital = Column(Float)
    # accounts_receivables 已在 BS 定义
    # inventory 已在 BS 定义
    accounts_payables = Column(Float)  # CF 用的是 accountsPayables（BS 用 accountPayables）
    other_working_capital = Column(Float)
    other_non_cash_items = Column(Float)
    net_cash_provided_by_operating_activities = Column(Float)
    investments_in_property_plant_and_equipment = Column(Float)
    acquisitions_net = Column(Float)
    purchases_of_investments = Column(Float)
    sales_maturities_of_investments = Column(Float)
    other_investing_activities = Column(Float)
    net_cash_provided_by_investing_activities = Column(Float)
    net_debt_issuance = Column(Float)
    long_term_net_debt_issuance = Column(Float)
    short_term_net_debt_issuance = Column(Float)
    net_stock_issuance = Column(Float)
    net_common_stock_issuance = Column(Float)
    common_stock_issuance = Column(Float)
    common_stock_repurchased = Column(Float)
    net_preferred_stock_issuance = Column(Float)
    net_dividends_paid = Column(Float)
    common_dividends_paid = Column(Float)
    preferred_dividends_paid = Column(Float)
    other_financing_activities = Column(Float)
    net_cash_provided_by_financing_activities = Column(Float)
    effect_of_forex_changes_on_cash = Column(Float)
    net_change_in_cash = Column(Float)
    cash_at_end_of_period = Column(Float)
    cash_at_beginning_of_period = Column(Float)
    operating_cash_flow = Column(Float)
    capital_expenditure = Column(Float)
    free_cash_flow = Column(Float)
    income_taxes_paid = Column(Float)
    interest_paid = Column(Float)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    __table_args__ = (
        UniqueConstraint("ticker", "period", name="uq_us_financial_ticker_period"),
        Index("idx_us_financial_ticker", "ticker"),
        Index("idx_us_financial_date", "date"),
    )


# --- 4. us_key_metric (FMP key-metrics + metrics-ratios 合并) ---
# 两个端点字段合并去重
class USKeyMetric(Base):
    """美股季度关键指标表"""
    __tablename__ = "us_key_metric"

    id = Column(Integer, primary_key=True, autoincrement=True)
    ticker = Column(String(20), nullable=False)
    date = Column(Date, nullable=False)
    fiscal_year = Column(String(10))
    period = Column(String(10))
    reported_currency = Column(String(10))
    # --- key-metrics 端点 ---
    market_cap = Column(Float)
    enterprise_value = Column(Float)
    ev_to_sales = Column(Float)
    ev_to_operating_cash_flow = Column(Float)
    ev_to_free_cash_flow = Column(Float)
    ev_to_ebitda = Column(Float)
    net_debt_to_ebitda = Column(Float)
    current_ratio = Column(Float)
    income_quality = Column(Float)
    graham_number = Column(Float)
    graham_net_net = Column(Float)
    tax_burden = Column(Float)
    interest_burden = Column(Float)
    working_capital = Column(Float)
    invested_capital = Column(Float)
    return_on_assets = Column(Float)
    operating_return_on_assets = Column(Float)
    return_on_tangible_assets = Column(Float)
    return_on_equity = Column(Float)
    return_on_invested_capital = Column(Float)
    return_on_capital_employed = Column(Float)
    earnings_yield = Column(Float)
    free_cash_flow_yield = Column(Float)
    capex_to_operating_cash_flow = Column(Float)
    capex_to_depreciation = Column(Float)
    capex_to_revenue = Column(Float)
    sales_general_and_administrative_to_revenue = Column(Float)
    research_and_developement_to_revenue = Column(Float)  # FMP 拼写如此
    stock_based_compensation_to_revenue = Column(Float)
    intangibles_to_total_assets = Column(Float)
    average_receivables = Column(Float)
    average_payables = Column(Float)
    average_inventory = Column(Float)
    days_of_sales_outstanding = Column(Float)
    days_of_payables_outstanding = Column(Float)
    days_of_inventory_outstanding = Column(Float)
    operating_cycle = Column(Float)
    cash_conversion_cycle = Column(Float)
    free_cash_flow_to_equity = Column(Float)
    free_cash_flow_to_firm = Column(Float)
    tangible_asset_value = Column(Float)
    net_current_asset_value = Column(Float)
    # --- metrics-ratios 端点 ---
    gross_profit_margin = Column(Float)
    ebit_margin = Column(Float)
    ebitda_margin = Column(Float)
    operating_profit_margin = Column(Float)
    pretax_profit_margin = Column(Float)
    continuous_operations_profit_margin = Column(Float)
    net_profit_margin = Column(Float)
    bottom_line_profit_margin = Column(Float)
    receivables_turnover = Column(Float)
    payables_turnover = Column(Float)
    inventory_turnover = Column(Float)
    fixed_asset_turnover = Column(Float)
    asset_turnover = Column(Float)
    quick_ratio = Column(Float)
    solvency_ratio = Column(Float)
    cash_ratio = Column(Float)
    price_to_earnings_ratio = Column(Float)
    price_to_earnings_growth_ratio = Column(Float)
    forward_price_to_earnings_growth_ratio = Column(Float)
    price_to_book_ratio = Column(Float)
    price_to_sales_ratio = Column(Float)
    price_to_free_cash_flow_ratio = Column(Float)
    price_to_operating_cash_flow_ratio = Column(Float)
    debt_to_assets_ratio = Column(Float)
    debt_to_equity_ratio = Column(Float)
    debt_to_capital_ratio = Column(Float)
    long_term_debt_to_capital_ratio = Column(Float)
    financial_leverage_ratio = Column(Float)
    working_capital_turnover_ratio = Column(Float)
    operating_cash_flow_ratio = Column(Float)
    operating_cash_flow_sales_ratio = Column(Float)
    free_cash_flow_operating_cash_flow_ratio = Column(Float)
    debt_service_coverage_ratio = Column(Float)
    interest_coverage_ratio = Column(Float)
    short_term_operating_cash_flow_coverage_ratio = Column(Float)
    operating_cash_flow_coverage_ratio = Column(Float)
    capital_expenditure_coverage_ratio = Column(Float)
    dividend_paid_and_capex_coverage_ratio = Column(Float)
    dividend_payout_ratio = Column(Float)
    dividend_yield = Column(Float)
    dividend_yield_percentage = Column(Float)
    revenue_per_share = Column(Float)
    net_income_per_share = Column(Float)
    interest_debt_per_share = Column(Float)
    cash_per_share = Column(Float)
    book_value_per_share = Column(Float)
    tangible_book_value_per_share = Column(Float)
    shareholders_equity_per_share = Column(Float)
    operating_cash_flow_per_share = Column(Float)
    capex_per_share = Column(Float)
    free_cash_flow_per_share = Column(Float)
    net_income_per_ebt = Column(Float)
    ebt_per_ebit = Column(Float)
    price_to_fair_value = Column(Float)
    debt_to_market_cap = Column(Float)
    effective_tax_rate = Column(Float)
    enterprise_value_multiple = Column(Float)
    dividend_per_share = Column(Float)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    __table_args__ = (
        UniqueConstraint("ticker", "date", name="uq_us_key_metric_ticker_date"),
        Index("idx_us_key_metric_ticker", "ticker"),
        Index("idx_us_key_metric_date", "date"),
    )


# --- 5. us_industry_class (FMP profile, 只存行业分类) ---
class USIndustryClass(Base):
    """美股 GICS 行业分类表"""
    __tablename__ = "us_industry_class"

    id = Column(Integer, primary_key=True, autoincrement=True)
    ticker = Column(String(20), nullable=False)
    sector = Column(String(200))
    industry = Column(String(300))
    sub_industry = Column(String(300))
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    __table_args__ = (
        UniqueConstraint("ticker", name="uq_us_industry_ticker"),
        Index("idx_us_industry_sector", "sector"),
    )


# --- 6. us_earnings_surprise (FMP earnings-surprises) ---
class USEarningsSurprise(Base):
    """美股盈利惊喜表"""
    __tablename__ = "us_earnings_surprise"

    id = Column(Integer, primary_key=True, autoincrement=True)
    ticker = Column(String(20), nullable=False)
    date = Column(Date, nullable=False)
    eps_actual = Column(Float)
    eps_estimated = Column(Float)
    revenue_actual = Column(Float)
    revenue_estimated = Column(Float)
    # 计算字段（下载代码生成）
    surprise = Column(Float)
    surprise_pct = Column(Float)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    __table_args__ = (
        UniqueConstraint("ticker", "date", name="uq_us_earnings_surprise_ticker_date"),
        Index("idx_us_earnings_surprise_ticker", "ticker"),
    )


# --- 7. us_eps_estimate (FMP analyst-estimates) ---
class USEpsEstimate(Base):
    """美股 EPS 共识预期表"""
    __tablename__ = "us_eps_estimate"

    id = Column(Integer, primary_key=True, autoincrement=True)
    ticker = Column(String(20), nullable=False)
    date = Column(Date, nullable=False)
    estimated_eps_avg = Column(Float)
    estimated_eps_low = Column(Float)
    estimated_eps_high = Column(Float)
    number_analysts_estimated_eps = Column(Integer)
    estimated_revenue_avg = Column(Float)
    estimated_net_income_avg = Column(Float)
    # FMP 还返回更多 estimates 字段
    estimated_revenue_low = Column(Float)
    estimated_revenue_high = Column(Float)
    number_analysts_estimated_revenue = Column(Integer)
    estimated_ebitda_avg = Column(Float)
    estimated_ebitda_low = Column(Float)
    estimated_ebitda_high = Column(Float)
    estimated_ebit_avg = Column(Float)
    estimated_ebit_low = Column(Float)
    estimated_ebit_high = Column(Float)
    estimated_net_income_low = Column(Float)
    estimated_net_income_high = Column(Float)
    estimated_sga_expense_avg = Column(Float)
    estimated_sga_expense_low = Column(Float)
    estimated_sga_expense_high = Column(Float)
    number_analysts_estimated_ebitda = Column(Integer)
    number_analysts_estimated_ebit = Column(Integer)
    number_analysts_estimated_net_income = Column(Integer)
    number_analysts_estimated_sga_expense = Column(Integer)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    __table_args__ = (
        UniqueConstraint("ticker", "date", name="uq_us_eps_estimate_ticker_date"),
        Index("idx_us_eps_estimate_ticker", "ticker"),
    )


# --- 8. us_insider_trade (FMP insider-trading) ---
class USInsiderTrade(Base):
    """美股内部人交易表"""
    __tablename__ = "us_insider_trade"

    id = Column(Integer, primary_key=True, autoincrement=True)
    ticker = Column(String(20), nullable=False)
    filing_date = Column(String(30))
    transaction_date = Column(Date, nullable=False)
    reporting_cik = Column(String(20))
    company_cik = Column(String(20))
    transaction_type = Column(String(20))
    securities_owned = Column(Float)
    reporting_name = Column(String(200))
    type_of_owner = Column(String(200))
    acquisition_or_disposition = Column(String(5))
    direct_or_indirect = Column(String(5))
    form_type = Column(String(10))
    securities_transacted = Column(Float)
    price = Column(Float)
    security_name = Column(String(300))
    url = Column(String(500))
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    __table_args__ = (
        UniqueConstraint("ticker", "transaction_date", "reporting_name", "transaction_type",
                         name="uq_us_insider_ticker_date_name_type"),
        Index("idx_us_insider_ticker", "ticker"),
        Index("idx_us_insider_date", "transaction_date"),
    )


# --- 9. us_analyst_recommendation (FMP grades) ---
class USAnalystRecommendation(Base):
    """美股分析师评级表"""
    __tablename__ = "us_analyst_recommendation"

    id = Column(Integer, primary_key=True, autoincrement=True)
    ticker = Column(String(20), nullable=False)
    date = Column(Date, nullable=False)
    grading_company = Column(String(200))
    previous_grade = Column(String(50))
    new_grade = Column(String(50))
    action = Column(String(50))
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    __table_args__ = (
        UniqueConstraint("ticker", "date", "grading_company",
                         name="uq_us_analyst_ticker_date_company"),
        Index("idx_us_analyst_ticker", "ticker"),
        Index("idx_us_analyst_date", "date"),
    )


# --- 10. us_corporate_action (FMP dividends + splits) ---
class USCorporateAction(Base):
    """美股公司行动表（分红/拆股）"""
    __tablename__ = "us_corporate_action"

    id = Column(Integer, primary_key=True, autoincrement=True)
    ticker = Column(String(20), nullable=False)
    date = Column(Date, nullable=False)
    action_type = Column(String(20))  # dividend / split
    label = Column(String(200))
    # dividend fields
    adj_dividend = Column(Float)
    dividend = Column(Float)
    record_date = Column(String(20))
    payment_date = Column(String(20))
    declaration_date = Column(String(20))
    # split fields
    numerator = Column(Float)
    denominator = Column(Float)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    __table_args__ = (
        UniqueConstraint("ticker", "date", "action_type",
                         name="uq_us_corp_ticker_date_type"),
        Index("idx_us_corp_ticker", "ticker"),
    )


# --- 11. us_index_daily (FMP index EOD) ---
class USIndexDaily(Base):
    """美股指数日线表"""
    __tablename__ = "us_index_daily"

    id = Column(Integer, primary_key=True, autoincrement=True)
    index_code = Column(String(20), nullable=False)  # ^GSPC 等
    trade_date = Column(Date, nullable=False)
    open = Column(Float)
    high = Column(Float)
    low = Column(Float)
    close = Column(Float)
    volume = Column(Float)
    adj_close = Column(Float)
    change = Column(Float)
    change_percent = Column(Float)
    vwap = Column(Float)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    __table_args__ = (
        UniqueConstraint("index_code", "trade_date", name="uq_us_index_code_date"),
        Index("idx_us_index_trade_date", "trade_date"),
    )


# --- 12. us_commodity_price (FMP commodity EOD) ---
class USCommodityPrice(Base):
    """美股商品期货表"""
    __tablename__ = "us_commodity_price"

    id = Column(Integer, primary_key=True, autoincrement=True)
    commodity_symbol = Column(String(20), nullable=False)  # 原始商品代码
    trade_date = Column(Date, nullable=False)
    open = Column(Float)
    high = Column(Float)
    low = Column(Float)
    close = Column(Float)
    volume = Column(Float)
    adj_close = Column(Float)
    change = Column(Float)
    change_percent = Column(Float)
    vwap = Column(Float)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    __table_args__ = (
        UniqueConstraint("commodity_symbol", "trade_date", name="uq_us_commodity_symbol_date"),
        Index("idx_us_commodity_trade_date", "trade_date"),
    )


# --- 13. us_macro_indicator (FMP economic + treasury) ---
class USMacroIndicator(Base):
    """美股宏观经济指标表"""
    __tablename__ = "us_macro_indicator"

    id = Column(Integer, primary_key=True, autoincrement=True)
    indicator_code = Column(String(50), nullable=False)
    report_date = Column(Date, nullable=False)
    value = Column(Float)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    __table_args__ = (
        UniqueConstraint("indicator_code", "report_date", name="uq_us_macro_code_date"),
        Index("idx_us_macro_indicator_code", "indicator_code"),
    )


# --- 14. us_sec_filing ---
class USSecFiling(Base):
    """美股 SEC 公告表"""
    __tablename__ = "us_sec_filing"

    id = Column(Integer, primary_key=True, autoincrement=True)
    ticker = Column(String(20), nullable=False)
    filing_date = Column(Date, nullable=False)
    type = Column(String(20))
    title = Column(String(500))
    url = Column(String(500))
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    __table_args__ = (
        UniqueConstraint("ticker", "filing_date", "type",
                         name="uq_us_sec_ticker_date_type"),
        Index("idx_us_sec_ticker", "ticker"),
    )


# --- 15. us_company_profile (FMP profile) ---
class USCompanyProfile(Base):
    """美股公司概况表"""
    __tablename__ = "us_company_profile"

    id = Column(Integer, primary_key=True, autoincrement=True)
    ticker = Column(String(20), nullable=False)
    price = Column(Float)
    market_cap = Column(Float)
    beta = Column(Float)
    last_dividend = Column(Float)
    range = Column(String(100))
    change = Column(Float)
    change_percentage = Column(Float)
    volume = Column(Float)
    average_volume = Column(Float)
    company_name = Column(String(500))
    currency = Column(String(20))
    cik = Column(String(30))
    isin = Column(String(30))
    cusip = Column(String(30))
    exchange_full_name = Column(String(200))
    exchange = Column(String(50))
    industry = Column(String(300))
    website = Column(String(500))
    description = Column(Text)
    ceo = Column(String(300))
    sector = Column(String(200))
    country = Column(String(50))
    full_time_employees = Column(String(50))
    phone = Column(String(100))
    address = Column(String(500))
    city = Column(String(200))
    state = Column(String(100))
    zip = Column(String(30))
    image = Column(String(500))
    ipo_date = Column(String(20))
    default_image = Column(Integer)
    is_etf = Column(Integer)
    is_actively_trading = Column(Integer)
    is_adr = Column(Integer)
    is_fund = Column(Integer)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    __table_args__ = (
        UniqueConstraint("ticker", name="uq_us_company_profile_ticker"),
        Index("idx_us_company_profile_sector", "sector"),
    )


# --- 16. us_historical_market_cap ---
class USHistoricalMarketCap(Base):
    """美股日频历史市值表"""
    __tablename__ = "us_historical_market_cap"

    id = Column(Integer, primary_key=True, autoincrement=True)
    ticker = Column(String(20), nullable=False)
    date = Column(Date, nullable=False)
    market_cap = Column(Float)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    __table_args__ = (
        UniqueConstraint("ticker", "date", name="uq_us_hist_mktcap_ticker_date"),
        Index("idx_us_hist_mktcap_ticker", "ticker"),
    )


# --- 17. us_shares_float ---
class USSharesFloat(Base):
    """美股流通股数据表"""
    __tablename__ = "us_shares_float"

    id = Column(Integer, primary_key=True, autoincrement=True)
    ticker = Column(String(20), nullable=False)
    date = Column(DateTime, nullable=False)
    free_float = Column(Float)
    float_shares = Column(Float)
    outstanding_shares = Column(Float)
    source = Column(String(500))
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    __table_args__ = (
        UniqueConstraint("ticker", "date", name="uq_us_shares_float_ticker_date"),
        Index("idx_us_shares_float_ticker", "ticker"),
    )


# --- 18. us_financial_score ---
class USFinancialScore(Base):
    """美股财务评分表"""
    __tablename__ = "us_financial_score"

    id = Column(Integer, primary_key=True, autoincrement=True)
    ticker = Column(String(20), nullable=False)
    reported_currency = Column(String(10))
    altman_z_score = Column(Float)
    piotroski_score = Column(Float)
    working_capital = Column(Float)
    total_assets = Column(Float)
    retained_earnings = Column(Float)
    ebit = Column(Float)
    market_cap = Column(Float)
    total_liabilities = Column(Float)
    revenue = Column(Float)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    __table_args__ = (
        UniqueConstraint("ticker", name="uq_us_financial_score_ticker"),
        Index("idx_us_financial_score_ticker", "ticker"),
    )


# --- 19. us_financial_growth ---
class USFinancialGrowth(Base):
    """美股财报增长率表"""
    __tablename__ = "us_financial_growth"

    id = Column(Integer, primary_key=True, autoincrement=True)
    ticker = Column(String(20), nullable=False)
    date = Column(Date, nullable=False)
    fiscal_year = Column(String(10))
    period = Column(String(10))
    reported_currency = Column(String(10))
    # _camel_to_snake 直接转换，不修正 FMP 不规范命名
    revenue_growth = Column(Float)
    gross_profit_growth = Column(Float)
    ebitgrowth = Column(Float)  # FMP: ebitgrowth (不规范)
    operating_income_growth = Column(Float)
    net_income_growth = Column(Float)
    epsgrowth = Column(Float)  # FMP: epsgrowth
    epsdiluted_growth = Column(Float)  # FMP: epsdilutedGrowth
    weighted_average_shares_growth = Column(Float)
    weighted_average_shares_diluted_growth = Column(Float)
    dividends_per_share_growth = Column(Float)
    operating_cash_flow_growth = Column(Float)
    receivables_growth = Column(Float)
    inventory_growth = Column(Float)
    asset_growth = Column(Float)
    book_valueper_share_growth = Column(Float)  # FMP: bookValueperShareGrowth
    debt_growth = Column(Float)
    rdexpense_growth = Column(Float)  # FMP: rdexpenseGrowth
    sgaexpenses_growth = Column(Float)  # FMP: sgaexpensesGrowth
    free_cash_flow_growth = Column(Float)
    ten_y_revenue_growth_per_share = Column(Float)
    five_y_revenue_growth_per_share = Column(Float)
    three_y_revenue_growth_per_share = Column(Float)
    ten_y_operating_cf_growth_per_share = Column(Float)
    five_y_operating_cf_growth_per_share = Column(Float)
    three_y_operating_cf_growth_per_share = Column(Float)
    ten_y_net_income_growth_per_share = Column(Float)
    five_y_net_income_growth_per_share = Column(Float)
    three_y_net_income_growth_per_share = Column(Float)
    ten_y_shareholders_equity_growth_per_share = Column(Float)
    five_y_shareholders_equity_growth_per_share = Column(Float)
    three_y_shareholders_equity_growth_per_share = Column(Float)
    ten_y_dividendper_share_growth_per_share = Column(Float)  # FMP 不规范
    five_y_dividendper_share_growth_per_share = Column(Float)
    three_y_dividendper_share_growth_per_share = Column(Float)
    ebitda_growth = Column(Float)
    growth_capital_expenditure = Column(Float)
    ten_y_bottom_line_net_income_growth_per_share = Column(Float)
    five_y_bottom_line_net_income_growth_per_share = Column(Float)
    three_y_bottom_line_net_income_growth_per_share = Column(Float)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    __table_args__ = (
        UniqueConstraint("ticker", "date", name="uq_us_financial_growth_ticker_date"),
        Index("idx_us_financial_growth_ticker", "ticker"),
    )


# --- 20. us_enterprise_value ---
class USEnterpriseValue(Base):
    """美股企业价值时间序列表"""
    __tablename__ = "us_enterprise_value"

    id = Column(Integer, primary_key=True, autoincrement=True)
    ticker = Column(String(20), nullable=False)
    date = Column(Date, nullable=False)
    stock_price = Column(Float)
    number_of_shares = Column(Float)
    market_capitalization = Column(Float)
    minus_cash_and_cash_equivalents = Column(Float)
    add_total_debt = Column(Float)
    enterprise_value = Column(Float)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    __table_args__ = (
        UniqueConstraint("ticker", "date", name="uq_us_enterprise_value_ticker_date"),
        Index("idx_us_enterprise_value_ticker", "ticker"),
    )


# --- 21. us_owner_earnings ---
class USOwnerEarnings(Base):
    """美股 Owner Earnings 表"""
    __tablename__ = "us_owner_earnings"

    id = Column(Integer, primary_key=True, autoincrement=True)
    ticker = Column(String(20), nullable=False)
    date = Column(Date, nullable=False)
    reported_currency = Column(String(10))
    fiscal_year = Column(String(10))
    period = Column(String(10))
    average_ppe = Column(Float)
    maintenance_capex = Column(Float)
    owners_earnings = Column(Float)
    growth_capex = Column(Float)
    owners_earnings_per_share = Column(Float)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    __table_args__ = (
        UniqueConstraint("ticker", "date", name="uq_us_owner_earnings_ticker_date"),
        Index("idx_us_owner_earnings_ticker", "ticker"),
    )


# --- 22. us_insider_statistic ---
class USInsiderStatistic(Base):
    """美股 Insider 交易统计表"""
    __tablename__ = "us_insider_statistic"

    id = Column(Integer, primary_key=True, autoincrement=True)
    ticker = Column(String(20), nullable=False)
    cik = Column(String(20))
    year = Column(Integer)
    quarter = Column(Integer)
    acquired_transactions = Column(Float)
    disposed_transactions = Column(Float)
    acquired_disposed_ratio = Column(Float)
    total_acquired = Column(Float)
    total_disposed = Column(Float)
    average_acquired = Column(Float)
    average_disposed = Column(Float)
    total_purchases = Column(Float)
    total_sales = Column(Float)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    __table_args__ = (
        UniqueConstraint("ticker", "year", "quarter", name="uq_us_insider_stat"),
        Index("idx_us_insider_stat_ticker", "ticker"),
    )


# --- 23. us_employee_count ---
class USEmployeeCount(Base):
    """美股员工数量历史表"""
    __tablename__ = "us_employee_count"

    id = Column(Integer, primary_key=True, autoincrement=True)
    ticker = Column(String(20), nullable=False)
    cik = Column(String(20))
    acceptance_time = Column(String(30))
    period_of_report = Column(Date, nullable=False)
    company_name = Column(String(500))
    form_type = Column(String(20))
    filing_date = Column(Date)
    employee_count = Column(Integer)
    source = Column(String(500))
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    __table_args__ = (
        UniqueConstraint("ticker", "period_of_report", name="uq_us_employee_count"),
        Index("idx_us_employee_count_ticker", "ticker"),
    )


# --- 24. us_price_target ---
class USPriceTarget(Base):
    """美股分析师目标价表"""
    __tablename__ = "us_price_target"

    id = Column(Integer, primary_key=True, autoincrement=True)
    ticker = Column(String(20), nullable=False)
    target_high = Column(Float)
    target_low = Column(Float)
    target_consensus = Column(Float)
    target_median = Column(Float)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    __table_args__ = (
        UniqueConstraint("ticker", name="uq_us_price_target_ticker"),
    )


# --- 25. us_esg_rating ---
class USESGRating(Base):
    """美股 ESG 评级表"""
    __tablename__ = "us_esg_rating"

    id = Column(Integer, primary_key=True, autoincrement=True)
    ticker = Column(String(20), nullable=False)
    cik = Column(String(20))
    company_name = Column(String(500))
    industry = Column(String(300))
    fiscal_year = Column(Integer)
    esg_risk_rating = Column(String(50))
    industry_rank = Column(String(50))
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    __table_args__ = (
        UniqueConstraint("ticker", "fiscal_year", name="uq_us_esg_rating_ticker_year"),
        Index("idx_us_esg_rating_ticker", "ticker"),
    )


# --- 26. us_dcf_valuation ---
class USDCFValuation(Base):
    """美股 DCF 估值表"""
    __tablename__ = "us_dcf_valuation"

    id = Column(Integer, primary_key=True, autoincrement=True)
    ticker = Column(String(20), nullable=False)
    date = Column(Date, nullable=False)
    dcf = Column(Float)
    stock_price = Column(Float)  # FMP 返回 "Stock Price" → _camel_to_snake 不处理空格
    dcf_type = Column(String(20))  # standard / levered，下载代码手动加
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    __table_args__ = (
        UniqueConstraint("ticker", "date", "dcf_type", name="uq_us_dcf_ticker_date_type"),
        Index("idx_us_dcf_ticker", "ticker"),
    )


# --- 27. us_stock_peer ---
class USStockPeer(Base):
    """美股同行可比公司表"""
    __tablename__ = "us_stock_peer"

    id = Column(Integer, primary_key=True, autoincrement=True)
    ticker = Column(String(20), nullable=False)
    peer_ticker = Column(String(20), nullable=False)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    __table_args__ = (
        UniqueConstraint("ticker", "peer_ticker", name="uq_us_stock_peer"),
        Index("idx_us_stock_peer_ticker", "ticker"),
    )


# --- 28. us_revenue_segment ---
class USRevenueSegment(Base):
    """美股收入拆分表"""
    __tablename__ = "us_revenue_segment"

    id = Column(Integer, primary_key=True, autoincrement=True)
    ticker = Column(String(20), nullable=False)
    date = Column(Date, nullable=False)
    segment_type = Column(String(20))  # geographic / product
    segment_name = Column(String(300))
    revenue = Column(Float)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    __table_args__ = (
        UniqueConstraint("ticker", "date", "segment_type", "segment_name",
                         name="uq_us_revenue_segment"),
        Index("idx_us_revenue_segment_ticker", "ticker"),
    )


# --- 29. us_index_constituent ---
class USIndexConstituent(Base):
    """美股指数成分股历史表"""
    __tablename__ = "us_index_constituent"

    id = Column(Integer, primary_key=True, autoincrement=True)
    index_name = Column(String(20), nullable=False)  # sp500/nasdaq/dow
    ticker = Column(String(20), nullable=False)
    date = Column(Date)
    date_added = Column(String(30))
    added_security = Column(String(300))
    removed_ticker = Column(String(20))
    removed_security = Column(String(300))
    reason = Column(String(500))
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    __table_args__ = (
        UniqueConstraint("index_name", "ticker", "date", name="uq_us_index_constituent"),
        Index("idx_us_index_constituent_name", "index_name"),
    )


# --- 30. us_symbol_change ---
class USSymbolChange(Base):
    """美股代码变更历史表"""
    __tablename__ = "us_symbol_change"

    id = Column(Integer, primary_key=True, autoincrement=True)
    date = Column(Date)
    company_name = Column(String(500))
    old_symbol = Column(String(20))
    new_symbol = Column(String(20))
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    __table_args__ = (
        UniqueConstraint("old_symbol", "new_symbol", "date", name="uq_us_symbol_change"),
    )


# --- 31. us_delisted ---
class USDelisted(Base):
    """美股退市公司表"""
    __tablename__ = "us_delisted"

    id = Column(Integer, primary_key=True, autoincrement=True)
    ticker = Column(String(20), nullable=False)
    company_name = Column(String(500))
    exchange = Column(String(50))
    ipo_date = Column(String(20))
    delisted_date = Column(String(20))
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    __table_args__ = (
        UniqueConstraint("ticker", name="uq_us_delisted_ticker"),
    )


# --- 32. us_congress_trade (FMP senate/house trading) ---
class USCongressTrade(Base):
    """美股国会交易表"""
    __tablename__ = "us_congress_trade"

    id = Column(Integer, primary_key=True, autoincrement=True)
    ticker = Column(String(20))
    disclosure_date = Column(String(20))
    transaction_date = Column(Date)
    first_name = Column(String(100))
    last_name = Column(String(100))
    office = Column(String(200))
    district = Column(String(10))
    owner = Column(String(50))
    asset_description = Column(String(500))
    asset_type = Column(String(50))
    type = Column(String(50))  # Purchase/Sale
    amount = Column(String(50))
    capital_gains_over200usd = Column(String(10))
    comment = Column(String(500))
    link = Column(String(500))
    source = Column(String(20))  # fmp_senate / fmp_house
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    __table_args__ = (
        UniqueConstraint("ticker", "transaction_date", "first_name", "last_name", "type",
                         name="uq_us_congress_trade"),
        Index("idx_us_congress_ticker", "ticker"),
    )


# --- 33. us_press_release ---
class USPressRelease(Base):
    """美股公告/新闻稿表"""
    __tablename__ = "us_press_release"

    id = Column(Integer, primary_key=True, autoincrement=True)
    ticker = Column(String(20), nullable=False)
    published_date = Column(String(30))
    publisher = Column(String(200))
    title = Column(String(500))
    image = Column(String(500))
    site = Column(String(200))
    text = Column(Text)
    url = Column(String(500))
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    __table_args__ = (
        Index("idx_us_press_release_ticker", "ticker"),
    )


# --- 34. us_news ---
class USNews(Base):
    """美股新闻表"""
    __tablename__ = "us_news"

    id = Column(Integer, primary_key=True, autoincrement=True)
    source = Column(String(20), nullable=False)
    title = Column(String(1000))
    url = Column(String(500))
    published_at = Column(DateTime)
    tickers = Column(String(500))
    summary = Column(Text)
    sentiment = Column(String(20))
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    __table_args__ = (
        UniqueConstraint("source", "url", name="uq_us_news_source_url"),
        Index("idx_us_news_published", "published_at"),
    )


# --- 35. import_progress (断点续跑标记) ---
class ImportProgress(Base):
    """导入进度表"""
    __tablename__ = "import_progress"

    id = Column(Integer, primary_key=True, autoincrement=True)
    table_name = Column(String(50), nullable=False)
    ticker = Column(String(20), nullable=False)
    completed_at = Column(DateTime, default=datetime.now)

    __table_args__ = (
        UniqueConstraint("table_name", "ticker", name="uq_import_progress"),
        Index("idx_import_progress_table", "table_name"),
    )


# ============================================================
# DatabaseManager
# ============================================================

class DatabaseManager:
    """数据库管理器 — 使用 SQLAlchemy ORM 读写"""

    def __init__(self, db_url: str = DB_URL):
        engine_kwargs = {
            "echo": False,
            "pool_size": 70,
            "max_overflow": 20,
            "pool_recycle": 3600,
            "pool_pre_ping": True,
        }
        self.engine = create_engine(db_url, **engine_kwargs)
        self.SessionLocal = sessionmaker(bind=self.engine)
        safe_url = db_url.split("@")[-1] if "@" in db_url else db_url
        logger.info(f"数据库连接已建立: ...@{safe_url}")

    def get_session(self) -> Session:
        return self.SessionLocal()

    def init_tables(self):
        """创建所有表（已存在则跳过）"""
        from services.config import DB_SCHEMA
        if DB_SCHEMA:
            try:
                with self.get_session() as session:
                    result = session.execute(
                        text("SELECT 1 FROM information_schema.schemata WHERE schema_name = :s"),
                        {"s": DB_SCHEMA},
                    )
                    if not result.fetchone():
                        logger.warning(f"Schema '{DB_SCHEMA}' 不存在")
                        return
            except Exception as e:
                logger.warning(f"检查 schema 失败: {e}")

        Base.metadata.create_all(self.engine)
        logger.info("所有表创建/检查完成")

    def query(self, sql: str, params: dict = None) -> pd.DataFrame:
        """执行查询，返回 DataFrame。"""
        with self.engine.connect() as conn:
            return pd.read_sql(text(sql), conn, params=params)

    # --- ORM 批量 upsert ---
    def _get_write_pool(self):
        """懒初始化异步写入线程池。"""
        if not hasattr(self, "_write_pool"):
            from concurrent.futures import ThreadPoolExecutor
            self._write_pool = ThreadPoolExecutor(max_workers=50)
            self._write_futures = []
        return self._write_pool

    def upsert(self, model_class, records: list[dict], unique_keys: list[str]):
        """通用 ORM upsert — PostgreSQL INSERT ON CONFLICT DO UPDATE。

        数据准备同步，写 DB 异步（提交到写入线程池）。
        """
        if not records:
            return

        from sqlalchemy.dialects.postgresql import insert

        table = model_class.__table__
        valid_cols = {c.name for c in table.columns} - {"id"}

        # 清理数据（同步）
        cleaned = []
        for rec in records:
            clean = {}
            for k, v in rec.items():
                if k not in valid_cols:
                    continue
                if isinstance(v, bool):
                    v = int(v)
                elif isinstance(v, float) and (math.isnan(v) or math.isinf(v) or abs(v) > _FLOAT_MAX):
                    v = None
                elif isinstance(v, pd.Timestamp):
                    v = v.to_pydatetime() if pd.notna(v) else None
                clean[k] = v
            clean["updated_at"] = datetime.now()
            cleaned.append(clean)

        update_cols = {c: getattr(insert(table).excluded, c)
                       for c in valid_cols if c not in unique_keys and c != "id"}

        # 异步写入
        pool = self._get_write_pool()
        batch_size = 2000
        for i in range(0, len(cleaned), batch_size):
            batch = cleaned[i:i + batch_size]
            def _do_write(b=batch, t=table, uk=unique_keys, uc=update_cols):
                stmt = insert(t).values(b).on_conflict_do_update(
                    index_elements=uk, set_=uc,
                )
                with self.engine.begin() as conn:
                    conn.execute(stmt)
            fut = pool.submit(_do_write)
            self._write_futures.append(fut)

        # 清理已完成 future + 背压
        self._write_futures = [f for f in self._write_futures if not f.done()]
        if len(self._write_futures) > 1000:
            from concurrent.futures import wait, FIRST_COMPLETED
            done, self._write_futures = wait(self._write_futures, return_when=FIRST_COMPLETED)
            self._write_futures = list(self._write_futures)

        logger.info(f"{model_class.__tablename__}: upsert {len(cleaned)} 条")

    def upsert_df(self, model_class, df: pd.DataFrame, unique_keys: list[str]):
        """DataFrame 版 upsert — 自动转 records。"""
        if df.empty:
            logger.debug(f"{model_class.__tablename__}: DataFrame 为空，跳过")
            return
        records = df.to_dict("records")
        self.upsert(model_class, records, unique_keys)

    # --- 便捷方法 ---
    def get_us_tickers(self, active_only: bool = True, stocks_only: bool = False) -> list[str]:
        """获取美股代码列表。"""
        with self.get_session() as session:
            q = session.query(USStockBasic.ticker)
            if active_only:
                q = q.filter(USStockBasic.is_actively_trading == 1)
            if stocks_only:
                q = q.filter(USStockBasic.is_etf == 0, USStockBasic.is_fund == 0)
            return [r[0] for r in q.all()]

    def mark_import_done(self, table_name: str, ticker: str):
        """标记导入完成。"""
        from sqlalchemy.dialects.postgresql import insert
        stmt = insert(ImportProgress.__table__).values(
            table_name=table_name, ticker=ticker, completed_at=datetime.now()
        ).on_conflict_do_update(
            index_elements=["table_name", "ticker"],
            set_={"completed_at": datetime.now()},
        )
        with self.engine.begin() as conn:
            conn.execute(stmt)

    def get_import_done_tickers(self, table_name: str) -> set[str]:
        """获取已完成导入的 ticker 集合。"""
        with self.get_session() as session:
            results = session.query(ImportProgress.ticker).filter(
                ImportProgress.table_name == table_name
            ).all()
            return {r[0] for r in results}

    def flush_writes(self):
        """等待所有异步写入完成。"""
        if hasattr(self, "_write_futures"):
            from concurrent.futures import wait
            wait(self._write_futures)
            for f in self._write_futures:
                if f.exception():
                    logger.warning(f"异步写入失败: {f.exception()}")
            self._write_futures.clear()


# ============================================================
# 模块级便捷函数
# ============================================================

_db_instance = None


def get_db() -> DatabaseManager:
    global _db_instance
    if _db_instance is None:
        _db_instance = DatabaseManager()
        _db_instance.init_tables()
    return _db_instance
