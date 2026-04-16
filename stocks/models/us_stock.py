"""
美股数据 Django Models — managed=False 映射现有 PostgreSQL 表。

所有模型字段与 services/data/database.py 中的 SQLAlchemy 定义一一对应。
"""

from django.db import models


# --- 1. us_stock_basic ---
class USStockBasic(models.Model):
    ticker = models.CharField(max_length=20)
    company_name = models.CharField(max_length=500, blank=True, null=True)
    market_cap = models.FloatField(null=True)
    sector = models.CharField(max_length=200, blank=True, null=True)
    industry = models.CharField(max_length=300, blank=True, null=True)
    beta = models.FloatField(null=True)
    price = models.FloatField(null=True)
    last_annual_dividend = models.FloatField(null=True)
    volume = models.FloatField(null=True)
    exchange = models.CharField(max_length=200, blank=True, null=True)
    exchange_short_name = models.CharField(max_length=50, blank=True, null=True)
    country = models.CharField(max_length=50, blank=True, null=True)
    is_etf = models.IntegerField(default=0)
    is_fund = models.IntegerField(default=0)
    is_actively_trading = models.IntegerField(default=1)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        managed = False
        db_table = "us_stock_basic"


# --- 2. us_daily_price ---
class USDailyPrice(models.Model):
    ticker = models.CharField(max_length=20)
    trade_date = models.DateField()
    open = models.FloatField(null=True)
    high = models.FloatField(null=True)
    low = models.FloatField(null=True)
    close = models.FloatField(null=True)
    adj_close = models.FloatField(null=True)
    volume = models.FloatField(null=True)
    unadjusted_volume = models.FloatField(null=True)
    change = models.FloatField(null=True)
    change_percent = models.FloatField(null=True)
    vwap = models.FloatField(null=True)
    label = models.CharField(max_length=50, blank=True, null=True)
    change_over_time = models.FloatField(null=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        managed = False
        db_table = "us_daily_price"


# --- 3. us_financial_data (IS+BS+CF 三表合并) ---
class USFinancialData(models.Model):
    ticker = models.CharField(max_length=20)
    period = models.CharField(max_length=10)
    date = models.DateField(null=True)
    reported_currency = models.CharField(max_length=10, blank=True, null=True)
    cik = models.CharField(max_length=20, blank=True, null=True)
    filing_date = models.DateField(null=True)
    accepted_date = models.CharField(max_length=30, blank=True, null=True)
    fiscal_year = models.CharField(max_length=10, blank=True, null=True)
    # --- Income Statement ---
    revenue = models.FloatField(null=True)
    cost_of_revenue = models.FloatField(null=True)
    gross_profit = models.FloatField(null=True)
    research_and_development_expenses = models.FloatField(null=True)
    general_and_administrative_expenses = models.FloatField(null=True)
    selling_and_marketing_expenses = models.FloatField(null=True)
    selling_general_and_administrative_expenses = models.FloatField(null=True)
    other_expenses = models.FloatField(null=True)
    operating_expenses = models.FloatField(null=True)
    cost_and_expenses = models.FloatField(null=True)
    net_interest_income = models.FloatField(null=True)
    interest_income = models.FloatField(null=True)
    interest_expense = models.FloatField(null=True)
    depreciation_and_amortization = models.FloatField(null=True)
    ebitda = models.FloatField(null=True)
    ebit = models.FloatField(null=True)
    non_operating_income_excluding_interest = models.FloatField(null=True)
    operating_income = models.FloatField(null=True)
    total_other_income_expenses_net = models.FloatField(null=True)
    income_before_tax = models.FloatField(null=True)
    income_tax_expense = models.FloatField(null=True)
    net_income_from_continuing_operations = models.FloatField(null=True)
    net_income_from_discontinued_operations = models.FloatField(null=True)
    other_adjustments_to_net_income = models.FloatField(null=True)
    net_income = models.FloatField(null=True)
    net_income_deductions = models.FloatField(null=True)
    bottom_line_net_income = models.FloatField(null=True)
    eps = models.FloatField(null=True)
    eps_diluted = models.FloatField(null=True)
    weighted_average_shs_out = models.FloatField(null=True)
    weighted_average_shs_out_dil = models.FloatField(null=True)
    gross_profit_ratio = models.FloatField(null=True)
    operating_income_ratio = models.FloatField(null=True)
    net_income_ratio = models.FloatField(null=True)
    # --- Balance Sheet ---
    cash_and_cash_equivalents = models.FloatField(null=True)
    short_term_investments = models.FloatField(null=True)
    cash_and_short_term_investments = models.FloatField(null=True)
    net_receivables = models.FloatField(null=True)
    accounts_receivables = models.FloatField(null=True)
    other_receivables = models.FloatField(null=True)
    inventory = models.FloatField(null=True)
    prepaids = models.FloatField(null=True)
    other_current_assets = models.FloatField(null=True)
    total_current_assets = models.FloatField(null=True)
    property_plant_equipment_net = models.FloatField(null=True)
    goodwill = models.FloatField(null=True)
    intangible_assets = models.FloatField(null=True)
    goodwill_and_intangible_assets = models.FloatField(null=True)
    long_term_investments = models.FloatField(null=True)
    tax_assets = models.FloatField(null=True)
    other_non_current_assets = models.FloatField(null=True)
    total_non_current_assets = models.FloatField(null=True)
    other_assets = models.FloatField(null=True)
    total_assets = models.FloatField(null=True)
    total_payables = models.FloatField(null=True)
    account_payables = models.FloatField(null=True)
    other_payables = models.FloatField(null=True)
    accrued_expenses = models.FloatField(null=True)
    short_term_debt = models.FloatField(null=True)
    capital_lease_obligations_current = models.FloatField(null=True)
    tax_payables = models.FloatField(null=True)
    deferred_revenue = models.FloatField(null=True)
    other_current_liabilities = models.FloatField(null=True)
    total_current_liabilities = models.FloatField(null=True)
    long_term_debt = models.FloatField(null=True)
    capital_lease_obligations_non_current = models.FloatField(null=True)
    deferred_revenue_non_current = models.FloatField(null=True)
    deferred_tax_liabilities_non_current = models.FloatField(null=True)
    other_non_current_liabilities = models.FloatField(null=True)
    total_non_current_liabilities = models.FloatField(null=True)
    other_liabilities = models.FloatField(null=True)
    capital_lease_obligations = models.FloatField(null=True)
    total_liabilities = models.FloatField(null=True)
    treasury_stock = models.FloatField(null=True)
    preferred_stock = models.FloatField(null=True)
    common_stock = models.FloatField(null=True)
    retained_earnings = models.FloatField(null=True)
    additional_paid_in_capital = models.FloatField(null=True)
    accumulated_other_comprehensive_income_loss = models.FloatField(null=True)
    other_total_stockholders_equity = models.FloatField(null=True)
    total_stockholders_equity = models.FloatField(null=True)
    total_equity = models.FloatField(null=True)
    minority_interest = models.FloatField(null=True)
    total_liabilities_and_total_equity = models.FloatField(null=True)
    total_investments = models.FloatField(null=True)
    total_debt = models.FloatField(null=True)
    net_debt = models.FloatField(null=True)
    # --- Cash Flow ---
    deferred_income_tax = models.FloatField(null=True)
    stock_based_compensation = models.FloatField(null=True)
    change_in_working_capital = models.FloatField(null=True)
    accounts_payables = models.FloatField(null=True)
    other_working_capital = models.FloatField(null=True)
    other_non_cash_items = models.FloatField(null=True)
    net_cash_provided_by_operating_activities = models.FloatField(null=True)
    investments_in_property_plant_and_equipment = models.FloatField(null=True)
    acquisitions_net = models.FloatField(null=True)
    purchases_of_investments = models.FloatField(null=True)
    sales_maturities_of_investments = models.FloatField(null=True)
    other_investing_activities = models.FloatField(null=True)
    net_cash_provided_by_investing_activities = models.FloatField(null=True)
    net_debt_issuance = models.FloatField(null=True)
    long_term_net_debt_issuance = models.FloatField(null=True)
    short_term_net_debt_issuance = models.FloatField(null=True)
    net_stock_issuance = models.FloatField(null=True)
    net_common_stock_issuance = models.FloatField(null=True)
    common_stock_issuance = models.FloatField(null=True)
    common_stock_repurchased = models.FloatField(null=True)
    net_preferred_stock_issuance = models.FloatField(null=True)
    net_dividends_paid = models.FloatField(null=True)
    common_dividends_paid = models.FloatField(null=True)
    preferred_dividends_paid = models.FloatField(null=True)
    other_financing_activities = models.FloatField(null=True)
    net_cash_provided_by_financing_activities = models.FloatField(null=True)
    effect_of_forex_changes_on_cash = models.FloatField(null=True)
    net_change_in_cash = models.FloatField(null=True)
    cash_at_end_of_period = models.FloatField(null=True)
    cash_at_beginning_of_period = models.FloatField(null=True)
    operating_cash_flow = models.FloatField(null=True)
    capital_expenditure = models.FloatField(null=True)
    free_cash_flow = models.FloatField(null=True)
    income_taxes_paid = models.FloatField(null=True)
    interest_paid = models.FloatField(null=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        managed = False
        db_table = "us_financial_data"


# --- 4. us_key_metric ---
class USKeyMetric(models.Model):
    ticker = models.CharField(max_length=20)
    date = models.DateField()
    fiscal_year = models.CharField(max_length=10, blank=True, null=True)
    period = models.CharField(max_length=10, blank=True, null=True)
    reported_currency = models.CharField(max_length=10, blank=True, null=True)
    # key-metrics
    enterprise_value = models.FloatField(null=True)
    ev_to_sales = models.FloatField(null=True)
    ev_to_operating_cash_flow = models.FloatField(null=True)
    ev_to_free_cash_flow = models.FloatField(null=True)
    ev_to_ebitda = models.FloatField(null=True)
    net_debt_to_ebitda = models.FloatField(null=True)
    current_ratio = models.FloatField(null=True)
    income_quality = models.FloatField(null=True)
    graham_number = models.FloatField(null=True)
    graham_net_net = models.FloatField(null=True)
    tax_burden = models.FloatField(null=True)
    interest_burden = models.FloatField(null=True)
    working_capital = models.FloatField(null=True)
    invested_capital = models.FloatField(null=True)
    return_on_assets = models.FloatField(null=True)
    operating_return_on_assets = models.FloatField(null=True)
    return_on_tangible_assets = models.FloatField(null=True)
    return_on_equity = models.FloatField(null=True)
    return_on_invested_capital = models.FloatField(null=True)
    return_on_capital_employed = models.FloatField(null=True)
    earnings_yield = models.FloatField(null=True)
    free_cash_flow_yield = models.FloatField(null=True)
    capex_to_operating_cash_flow = models.FloatField(null=True)
    capex_to_depreciation = models.FloatField(null=True)
    capex_to_revenue = models.FloatField(null=True)
    sales_general_and_administrative_to_revenue = models.FloatField(null=True)
    research_and_developement_to_revenue = models.FloatField(null=True)
    stock_based_compensation_to_revenue = models.FloatField(null=True)
    intangibles_to_total_assets = models.FloatField(null=True)
    average_receivables = models.FloatField(null=True)
    average_payables = models.FloatField(null=True)
    average_inventory = models.FloatField(null=True)
    days_of_sales_outstanding = models.FloatField(null=True)
    days_of_payables_outstanding = models.FloatField(null=True)
    days_of_inventory_outstanding = models.FloatField(null=True)
    operating_cycle = models.FloatField(null=True)
    cash_conversion_cycle = models.FloatField(null=True)
    free_cash_flow_to_equity = models.FloatField(null=True)
    free_cash_flow_to_firm = models.FloatField(null=True)
    tangible_asset_value = models.FloatField(null=True)
    net_current_asset_value = models.FloatField(null=True)
    # metrics-ratios
    gross_profit_margin = models.FloatField(null=True)
    ebit_margin = models.FloatField(null=True)
    ebitda_margin = models.FloatField(null=True)
    operating_profit_margin = models.FloatField(null=True)
    pretax_profit_margin = models.FloatField(null=True)
    continuous_operations_profit_margin = models.FloatField(null=True)
    net_profit_margin = models.FloatField(null=True)
    bottom_line_profit_margin = models.FloatField(null=True)
    receivables_turnover = models.FloatField(null=True)
    payables_turnover = models.FloatField(null=True)
    inventory_turnover = models.FloatField(null=True)
    fixed_asset_turnover = models.FloatField(null=True)
    asset_turnover = models.FloatField(null=True)
    quick_ratio = models.FloatField(null=True)
    solvency_ratio = models.FloatField(null=True)
    cash_ratio = models.FloatField(null=True)
    price_to_earnings_ratio = models.FloatField(null=True)
    price_to_earnings_growth_ratio = models.FloatField(null=True)
    forward_price_to_earnings_growth_ratio = models.FloatField(null=True)
    price_to_book_ratio = models.FloatField(null=True)
    price_to_sales_ratio = models.FloatField(null=True)
    price_to_free_cash_flow_ratio = models.FloatField(null=True)
    price_to_operating_cash_flow_ratio = models.FloatField(null=True)
    debt_to_assets_ratio = models.FloatField(null=True)
    debt_to_equity_ratio = models.FloatField(null=True)
    debt_to_capital_ratio = models.FloatField(null=True)
    long_term_debt_to_capital_ratio = models.FloatField(null=True)
    financial_leverage_ratio = models.FloatField(null=True)
    working_capital_turnover_ratio = models.FloatField(null=True)
    operating_cash_flow_ratio = models.FloatField(null=True)
    operating_cash_flow_sales_ratio = models.FloatField(null=True)
    free_cash_flow_operating_cash_flow_ratio = models.FloatField(null=True)
    debt_service_coverage_ratio = models.FloatField(null=True)
    interest_coverage_ratio = models.FloatField(null=True)
    short_term_operating_cash_flow_coverage_ratio = models.FloatField(null=True)
    operating_cash_flow_coverage_ratio = models.FloatField(null=True)
    capital_expenditure_coverage_ratio = models.FloatField(null=True)
    dividend_paid_and_capex_coverage_ratio = models.FloatField(null=True)
    dividend_payout_ratio = models.FloatField(null=True)
    dividend_yield = models.FloatField(null=True)
    dividend_yield_percentage = models.FloatField(null=True)
    revenue_per_share = models.FloatField(null=True)
    net_income_per_share = models.FloatField(null=True)
    interest_debt_per_share = models.FloatField(null=True)
    cash_per_share = models.FloatField(null=True)
    book_value_per_share = models.FloatField(null=True)
    tangible_book_value_per_share = models.FloatField(null=True)
    shareholders_equity_per_share = models.FloatField(null=True)
    operating_cash_flow_per_share = models.FloatField(null=True)
    capex_per_share = models.FloatField(null=True)
    free_cash_flow_per_share = models.FloatField(null=True)
    net_income_per_ebt = models.FloatField(null=True)
    ebt_per_ebit = models.FloatField(null=True)
    price_to_fair_value = models.FloatField(null=True)
    debt_to_market_cap = models.FloatField(null=True)
    effective_tax_rate = models.FloatField(null=True)
    enterprise_value_multiple = models.FloatField(null=True)
    dividend_per_share = models.FloatField(null=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        managed = False
        db_table = "us_key_metric"


# --- 5. us_industry_class ---
class USIndustryClass(models.Model):
    ticker = models.CharField(max_length=20)
    sector = models.CharField(max_length=200, blank=True, null=True)
    industry = models.CharField(max_length=300, blank=True, null=True)
    sub_industry = models.CharField(max_length=300, blank=True, null=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        managed = False
        db_table = "us_industry_class"


# --- 6. us_earnings_surprise ---
class USEarningsSurprise(models.Model):
    ticker = models.CharField(max_length=20)
    date = models.DateField()
    eps_actual = models.FloatField(null=True)
    eps_estimated = models.FloatField(null=True)
    revenue_actual = models.FloatField(null=True)
    revenue_estimated = models.FloatField(null=True)
    surprise = models.FloatField(null=True)
    surprise_pct = models.FloatField(null=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        managed = False
        db_table = "us_earnings_surprise"


# --- 7. us_eps_estimate ---
class USEpsEstimate(models.Model):
    ticker = models.CharField(max_length=20)
    date = models.DateField()
    estimated_eps_avg = models.FloatField(null=True)
    estimated_eps_low = models.FloatField(null=True)
    estimated_eps_high = models.FloatField(null=True)
    number_analysts_estimated_eps = models.IntegerField(null=True)
    estimated_revenue_avg = models.FloatField(null=True)
    estimated_net_income_avg = models.FloatField(null=True)
    estimated_revenue_low = models.FloatField(null=True)
    estimated_revenue_high = models.FloatField(null=True)
    number_analyst_estimated_revenue = models.IntegerField(null=True)
    estimated_ebitda_avg = models.FloatField(null=True)
    estimated_ebitda_low = models.FloatField(null=True)
    estimated_ebitda_high = models.FloatField(null=True)
    estimated_ebit_avg = models.FloatField(null=True)
    estimated_ebit_low = models.FloatField(null=True)
    estimated_ebit_high = models.FloatField(null=True)
    estimated_net_income_low = models.FloatField(null=True)
    estimated_net_income_high = models.FloatField(null=True)
    estimated_sga_expense_avg = models.FloatField(null=True)
    estimated_sga_expense_low = models.FloatField(null=True)
    estimated_sga_expense_high = models.FloatField(null=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        managed = False
        db_table = "us_eps_estimate"


# --- 8. us_insider_trade ---
class USInsiderTrade(models.Model):
    ticker = models.CharField(max_length=20)
    filing_date = models.CharField(max_length=30, blank=True, null=True)
    transaction_date = models.DateField()
    reporting_cik = models.CharField(max_length=20, blank=True, null=True)
    company_cik = models.CharField(max_length=20, blank=True, null=True)
    transaction_type = models.CharField(max_length=20, blank=True, null=True)
    securities_owned = models.FloatField(null=True)
    reporting_name = models.CharField(max_length=200, blank=True, null=True)
    type_of_owner = models.CharField(max_length=200, blank=True, null=True)
    acquisition_or_disposition = models.CharField(max_length=5, blank=True, null=True)
    direct_or_indirect = models.CharField(max_length=5, blank=True, null=True)
    form_type = models.CharField(max_length=10, blank=True, null=True)
    securities_transacted = models.FloatField(null=True)
    price = models.FloatField(null=True)
    security_name = models.CharField(max_length=300, blank=True, null=True)
    url = models.CharField(max_length=500, blank=True, null=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        managed = False
        db_table = "us_insider_trade"


# --- 9. us_analyst_recommendation ---
class USAnalystRecommendation(models.Model):
    ticker = models.CharField(max_length=20)
    date = models.DateField()
    grading_company = models.CharField(max_length=200, blank=True, null=True)
    previous_grade = models.CharField(max_length=50, blank=True, null=True)
    new_grade = models.CharField(max_length=50, blank=True, null=True)
    action = models.CharField(max_length=50, blank=True, null=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        managed = False
        db_table = "us_analyst_recommendation"


# --- 10. us_corporate_action ---
class USCorporateAction(models.Model):
    ticker = models.CharField(max_length=20)
    date = models.DateField()
    action_type = models.CharField(max_length=20, blank=True, null=True)
    label = models.CharField(max_length=200, blank=True, null=True)
    adj_dividend = models.FloatField(null=True)
    dividend = models.FloatField(null=True)
    record_date = models.CharField(max_length=20, blank=True, null=True)
    payment_date = models.CharField(max_length=20, blank=True, null=True)
    declaration_date = models.CharField(max_length=20, blank=True, null=True)
    numerator = models.FloatField(null=True)
    denominator = models.FloatField(null=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        managed = False
        db_table = "us_corporate_action"


# --- 11. us_index_daily ---
class USIndexDaily(models.Model):
    index_code = models.CharField(max_length=20)
    trade_date = models.DateField()
    open = models.FloatField(null=True)
    high = models.FloatField(null=True)
    low = models.FloatField(null=True)
    close = models.FloatField(null=True)
    volume = models.FloatField(null=True)
    adj_close = models.FloatField(null=True)
    change = models.FloatField(null=True)
    change_percent = models.FloatField(null=True)
    vwap = models.FloatField(null=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        managed = False
        db_table = "us_index_daily"


# --- 12. us_commodity_price ---
class USCommodityPrice(models.Model):
    commodity_symbol = models.CharField(max_length=20)
    trade_date = models.DateField()
    open = models.FloatField(null=True)
    high = models.FloatField(null=True)
    low = models.FloatField(null=True)
    close = models.FloatField(null=True)
    volume = models.FloatField(null=True)
    adj_close = models.FloatField(null=True)
    change = models.FloatField(null=True)
    change_percent = models.FloatField(null=True)
    vwap = models.FloatField(null=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        managed = False
        db_table = "us_commodity_price"


# --- 13. us_macro_indicator ---
class USMacroIndicator(models.Model):
    indicator_code = models.CharField(max_length=50)
    report_date = models.DateField()
    value = models.FloatField(null=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        managed = False
        db_table = "us_macro_indicator"


# --- 14. us_sec_filing ---
class USSecFiling(models.Model):
    ticker = models.CharField(max_length=20)
    filing_date = models.DateField()
    type = models.CharField(max_length=20, blank=True, null=True)
    title = models.CharField(max_length=500, blank=True, null=True)
    url = models.CharField(max_length=500, blank=True, null=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        managed = False
        db_table = "us_sec_filing"


# --- 15. us_company_profile ---
class USCompanyProfile(models.Model):
    ticker = models.CharField(max_length=20)
    price = models.FloatField(null=True)
    market_cap = models.FloatField(null=True)
    beta = models.FloatField(null=True)
    last_dividend = models.FloatField(null=True)
    range = models.CharField(max_length=100, blank=True, null=True)
    change = models.FloatField(null=True)
    change_percentage = models.FloatField(null=True)
    volume = models.FloatField(null=True)
    average_volume = models.FloatField(null=True)
    company_name = models.CharField(max_length=500, blank=True, null=True)
    currency = models.CharField(max_length=20, blank=True, null=True)
    cik = models.CharField(max_length=30, blank=True, null=True)
    isin = models.CharField(max_length=30, blank=True, null=True)
    cusip = models.CharField(max_length=30, blank=True, null=True)
    exchange_full_name = models.CharField(max_length=200, blank=True, null=True)
    exchange = models.CharField(max_length=50, blank=True, null=True)
    industry = models.CharField(max_length=300, blank=True, null=True)
    website = models.CharField(max_length=500, blank=True, null=True)
    description = models.TextField(blank=True, null=True)
    ceo = models.CharField(max_length=300, blank=True, null=True)
    sector = models.CharField(max_length=200, blank=True, null=True)
    country = models.CharField(max_length=50, blank=True, null=True)
    full_time_employees = models.CharField(max_length=50, blank=True, null=True)
    phone = models.CharField(max_length=100, blank=True, null=True)
    address = models.CharField(max_length=500, blank=True, null=True)
    city = models.CharField(max_length=200, blank=True, null=True)
    state = models.CharField(max_length=100, blank=True, null=True)
    zip = models.CharField(max_length=30, blank=True, null=True)
    image = models.CharField(max_length=500, blank=True, null=True)
    ipo_date = models.CharField(max_length=20, blank=True, null=True)
    default_image = models.IntegerField(null=True)
    is_etf = models.IntegerField(null=True)
    is_actively_trading = models.IntegerField(null=True)
    is_adr = models.IntegerField(null=True)
    is_fund = models.IntegerField(null=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        managed = False
        db_table = "us_company_profile"


# --- 17. us_shares_float ---
class USSharesFloat(models.Model):
    ticker = models.CharField(max_length=20)
    date = models.DateTimeField()
    free_float = models.FloatField(null=True)
    float_shares = models.FloatField(null=True)
    outstanding_shares = models.FloatField(null=True)
    source = models.CharField(max_length=500, blank=True, null=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        managed = False
        db_table = "us_shares_float"


# --- 18. us_financial_score ---
class USFinancialScore(models.Model):
    ticker = models.CharField(max_length=20)
    reported_currency = models.CharField(max_length=10, blank=True, null=True)
    altman_z_score = models.FloatField(null=True)
    piotroski_score = models.FloatField(null=True)
    working_capital = models.FloatField(null=True)
    total_assets = models.FloatField(null=True)
    retained_earnings = models.FloatField(null=True)
    ebit = models.FloatField(null=True)
    market_cap = models.FloatField(null=True)
    total_liabilities = models.FloatField(null=True)
    revenue = models.FloatField(null=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        managed = False
        db_table = "us_financial_score"


# --- 19. us_financial_growth ---
class USFinancialGrowth(models.Model):
    ticker = models.CharField(max_length=20)
    date = models.DateField()
    fiscal_year = models.CharField(max_length=10, blank=True, null=True)
    period = models.CharField(max_length=10, blank=True, null=True)
    reported_currency = models.CharField(max_length=10, blank=True, null=True)
    revenue_growth = models.FloatField(null=True)
    gross_profit_growth = models.FloatField(null=True)
    ebitgrowth = models.FloatField(null=True)
    operating_income_growth = models.FloatField(null=True)
    net_income_growth = models.FloatField(null=True)
    epsgrowth = models.FloatField(null=True)
    epsdiluted_growth = models.FloatField(null=True)
    weighted_average_shares_growth = models.FloatField(null=True)
    weighted_average_shares_diluted_growth = models.FloatField(null=True)
    dividends_per_share_growth = models.FloatField(null=True)
    operating_cash_flow_growth = models.FloatField(null=True)
    receivables_growth = models.FloatField(null=True)
    inventory_growth = models.FloatField(null=True)
    asset_growth = models.FloatField(null=True)
    book_valueper_share_growth = models.FloatField(null=True)
    debt_growth = models.FloatField(null=True)
    rdexpense_growth = models.FloatField(null=True)
    sgaexpenses_growth = models.FloatField(null=True)
    free_cash_flow_growth = models.FloatField(null=True)
    ten_y_revenue_growth_per_share = models.FloatField(null=True)
    five_y_revenue_growth_per_share = models.FloatField(null=True)
    three_y_revenue_growth_per_share = models.FloatField(null=True)
    ten_y_operating_cf_growth_per_share = models.FloatField(null=True)
    five_y_operating_cf_growth_per_share = models.FloatField(null=True)
    three_y_operating_cf_growth_per_share = models.FloatField(null=True)
    ten_y_net_income_growth_per_share = models.FloatField(null=True)
    five_y_net_income_growth_per_share = models.FloatField(null=True)
    three_y_net_income_growth_per_share = models.FloatField(null=True)
    ten_y_shareholders_equity_growth_per_share = models.FloatField(null=True)
    five_y_shareholders_equity_growth_per_share = models.FloatField(null=True)
    three_y_shareholders_equity_growth_per_share = models.FloatField(null=True)
    ten_y_dividendper_share_growth_per_share = models.FloatField(null=True)
    five_y_dividendper_share_growth_per_share = models.FloatField(null=True)
    three_y_dividendper_share_growth_per_share = models.FloatField(null=True)
    ebitda_growth = models.FloatField(null=True)
    growth_capital_expenditure = models.FloatField(null=True)
    ten_y_bottom_line_net_income_growth_per_share = models.FloatField(null=True)
    five_y_bottom_line_net_income_growth_per_share = models.FloatField(null=True)
    three_y_bottom_line_net_income_growth_per_share = models.FloatField(null=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        managed = False
        db_table = "us_financial_growth"


# --- 20. us_enterprise_value ---
class USEnterpriseValue(models.Model):
    ticker = models.CharField(max_length=20)
    date = models.DateField()
    stock_price = models.FloatField(null=True)
    number_of_shares = models.FloatField(null=True)
    market_capitalization = models.FloatField(null=True)
    minus_cash_and_cash_equivalents = models.FloatField(null=True)
    add_total_debt = models.FloatField(null=True)
    enterprise_value = models.FloatField(null=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        managed = False
        db_table = "us_enterprise_value"


# --- 21. us_owner_earnings ---
class USOwnerEarnings(models.Model):
    ticker = models.CharField(max_length=20)
    date = models.DateField()
    reported_currency = models.CharField(max_length=10, blank=True, null=True)
    fiscal_year = models.CharField(max_length=10, blank=True, null=True)
    period = models.CharField(max_length=10, blank=True, null=True)
    average_ppe = models.FloatField(null=True)
    maintenance_capex = models.FloatField(null=True)
    owners_earnings = models.FloatField(null=True)
    growth_capex = models.FloatField(null=True)
    owners_earnings_per_share = models.FloatField(null=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        managed = False
        db_table = "us_owner_earnings"


# --- 22. us_insider_statistic ---
class USInsiderStatistic(models.Model):
    ticker = models.CharField(max_length=20)
    cik = models.CharField(max_length=20, blank=True, null=True)
    year = models.IntegerField(null=True)
    quarter = models.IntegerField(null=True)
    acquired_transactions = models.FloatField(null=True)
    disposed_transactions = models.FloatField(null=True)
    acquired_disposed_ratio = models.FloatField(null=True)
    total_acquired = models.FloatField(null=True)
    total_disposed = models.FloatField(null=True)
    average_acquired = models.FloatField(null=True)
    average_disposed = models.FloatField(null=True)
    total_purchases = models.FloatField(null=True)
    total_sales = models.FloatField(null=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        managed = False
        db_table = "us_insider_statistic"


# --- 23. us_employee_count ---
class USEmployeeCount(models.Model):
    ticker = models.CharField(max_length=20)
    cik = models.CharField(max_length=20, blank=True, null=True)
    acceptance_time = models.CharField(max_length=30, blank=True, null=True)
    period_of_report = models.DateField()
    company_name = models.CharField(max_length=500, blank=True, null=True)
    form_type = models.CharField(max_length=20, blank=True, null=True)
    filing_date = models.DateField(null=True)
    employee_count = models.IntegerField(null=True)
    source = models.CharField(max_length=500, blank=True, null=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        managed = False
        db_table = "us_employee_count"


# --- 24. us_price_target ---
class USPriceTarget(models.Model):
    ticker = models.CharField(max_length=20)
    target_high = models.FloatField(null=True)
    target_low = models.FloatField(null=True)
    target_consensus = models.FloatField(null=True)
    target_median = models.FloatField(null=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        managed = False
        db_table = "us_price_target"


# --- 25. us_esg_rating ---
class USESGRating(models.Model):
    ticker = models.CharField(max_length=20)
    cik = models.CharField(max_length=20, blank=True, null=True)
    company_name = models.CharField(max_length=500, blank=True, null=True)
    industry = models.CharField(max_length=300, blank=True, null=True)
    fiscal_year = models.IntegerField(null=True)
    esg_risk_rating = models.CharField(max_length=50, blank=True, null=True)
    industry_rank = models.CharField(max_length=50, blank=True, null=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        managed = False
        db_table = "us_esg_rating"


# --- 26. us_dcf_valuation ---
class USDCFValuation(models.Model):
    ticker = models.CharField(max_length=20)
    date = models.DateField()
    dcf = models.FloatField(null=True)
    stock_price = models.FloatField(null=True)
    dcf_type = models.CharField(max_length=20, blank=True, null=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        managed = False
        db_table = "us_dcf_valuation"


# --- 27. us_stock_peer ---
class USStockPeer(models.Model):
    ticker = models.CharField(max_length=20)
    peer_ticker = models.CharField(max_length=20)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        managed = False
        db_table = "us_stock_peer"


# --- 28. us_revenue_segment ---
class USRevenueSegment(models.Model):
    ticker = models.CharField(max_length=20)
    date = models.DateField()
    segment_type = models.CharField(max_length=20, blank=True, null=True)
    segment_name = models.CharField(max_length=300, blank=True, null=True)
    revenue = models.FloatField(null=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        managed = False
        db_table = "us_revenue_segment"


# --- 29. us_index_constituent ---
class USIndexConstituent(models.Model):
    index_name = models.CharField(max_length=20)
    ticker = models.CharField(max_length=20)
    date = models.DateField(null=True)
    date_added = models.CharField(max_length=30, blank=True, null=True)
    added_security = models.CharField(max_length=300, blank=True, null=True)
    removed_ticker = models.CharField(max_length=20, blank=True, null=True)
    removed_security = models.CharField(max_length=300, blank=True, null=True)
    reason = models.CharField(max_length=500, blank=True, null=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        managed = False
        db_table = "us_index_constituent"


# --- 30. us_symbol_change ---
class USSymbolChange(models.Model):
    date = models.DateField(null=True)
    company_name = models.CharField(max_length=500, blank=True, null=True)
    old_symbol = models.CharField(max_length=20, blank=True, null=True)
    new_symbol = models.CharField(max_length=20, blank=True, null=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        managed = False
        db_table = "us_symbol_change"


# --- 31. us_delisted ---
class USDelisted(models.Model):
    ticker = models.CharField(max_length=20)
    company_name = models.CharField(max_length=500, blank=True, null=True)
    exchange = models.CharField(max_length=50, blank=True, null=True)
    ipo_date = models.CharField(max_length=20, blank=True, null=True)
    delisted_date = models.CharField(max_length=20, blank=True, null=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        managed = False
        db_table = "us_delisted"


# --- 32. us_congress_trade ---
class USCongressTrade(models.Model):
    ticker = models.CharField(max_length=20, blank=True, null=True)
    disclosure_date = models.CharField(max_length=20, blank=True, null=True)
    transaction_date = models.DateField(null=True)
    first_name = models.CharField(max_length=100, blank=True, null=True)
    last_name = models.CharField(max_length=100, blank=True, null=True)
    office = models.CharField(max_length=200, blank=True, null=True)
    district = models.CharField(max_length=10, blank=True, null=True)
    owner = models.CharField(max_length=50, blank=True, null=True)
    asset_description = models.CharField(max_length=500, blank=True, null=True)
    asset_type = models.CharField(max_length=50, blank=True, null=True)
    type = models.CharField(max_length=50, blank=True, null=True)
    amount = models.CharField(max_length=50, blank=True, null=True)
    capital_gains_over200usd = models.CharField(max_length=10, blank=True, null=True)
    comment = models.CharField(max_length=500, blank=True, null=True)
    link = models.CharField(max_length=500, blank=True, null=True)
    source = models.CharField(max_length=20, blank=True, null=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        managed = False
        db_table = "us_congress_trade"


# --- 33. us_press_release ---
class USPressRelease(models.Model):
    ticker = models.CharField(max_length=20)
    published_date = models.CharField(max_length=30, blank=True, null=True)
    publisher = models.CharField(max_length=200, blank=True, null=True)
    title = models.CharField(max_length=500, blank=True, null=True)
    image = models.CharField(max_length=500, blank=True, null=True)
    site = models.CharField(max_length=200, blank=True, null=True)
    text = models.TextField(blank=True, null=True)
    url = models.CharField(max_length=500, blank=True, null=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        managed = False
        db_table = "us_press_release"


# --- 34. us_news ---
class USNews(models.Model):
    source = models.CharField(max_length=20)
    title = models.CharField(max_length=1000, blank=True, null=True)
    url = models.CharField(max_length=500, blank=True, null=True)
    published_at = models.DateTimeField(null=True)
    tickers = models.CharField(max_length=500, blank=True, null=True)
    summary = models.TextField(blank=True, null=True)
    sentiment = models.CharField(max_length=20, blank=True, null=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        managed = False
        db_table = "us_news"


# --- 35. us_lobbying ---
class USLobbying(models.Model):
    ticker = models.CharField(max_length=20)
    date = models.DateField()
    amount = models.FloatField(null=True)
    client = models.CharField(max_length=500, blank=True, null=True)
    registrant = models.CharField(max_length=500, blank=True, null=True)
    issue = models.TextField(blank=True, null=True)
    specific_issue = models.TextField(blank=True, null=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        managed = False
        db_table = "us_lobbying"


# --- 36. us_gov_contract ---
class USGovContract(models.Model):
    ticker = models.CharField(max_length=20)
    year = models.IntegerField()
    quarter = models.IntegerField()
    amount = models.FloatField(null=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        managed = False
        db_table = "us_gov_contract"


# --- 37. us_dark_pool_volume（Quiver /historical/offexchange/{ticker}） ---
class USDarkPoolVolume(models.Model):
    """日频 Off-Exchange (dark pool / ATS) 短卖与总成交量数据。

    数据源：Quiver Quant `/historical/offexchange/{ticker}`（替代 FMP short-interest，
    后者订阅不含）。深度：2010-至今每日，AAPL 实测 3926 条。

    字段说明：
        otc_short  — 场外短卖股数（可作 short interest 日频代理）
        otc_total  — 场外总成交股数
        dpi        — Dark Pool Indicator = otc_short / otc_total（Quiver 已计算）

    用于因子：DARK_POOL_SHORT（otc_short / 流通股，short squeeze 候选）、
    DPI 趋势（DPI 提升 = 机构偷偷做空）、OTC_VOLUME_RATIO（场外成交占比）。
    """
    ticker = models.CharField(max_length=20)
    date = models.DateField()
    otc_short = models.FloatField(null=True)
    otc_total = models.FloatField(null=True)
    dpi = models.FloatField(null=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        managed = False
        db_table = "us_dark_pool_volume"


# --- 38. us_institutional_holder（FMP /stable/institutional-holder/symbol-positions-summary） ---
class USInstitutionalHolder(models.Model):
    """季度 13F 持仓汇总（机构持有 ticker 的统计）。

    用于因子：HF_CROWDING（hedge fund 共持度）、INST_OWNERSHIP_DELTA（季度变化）、
    NEW_POSITIONS（新建仓）、CLOSED_POSITIONS（清仓）。
    """
    ticker = models.CharField(max_length=20)
    date = models.DateField()  # quarter end date
    investors_holding = models.IntegerField(null=True)  # 持有的机构数
    investors_holding_change = models.IntegerField(null=True)  # 季度变化
    number_of_13f_shares = models.FloatField(null=True)
    number_of_13f_shares_change = models.FloatField(null=True)
    total_invested = models.FloatField(null=True)
    total_invested_change = models.FloatField(null=True)
    ownership_percent = models.FloatField(null=True)
    ownership_percent_change = models.FloatField(null=True)
    new_positions = models.IntegerField(null=True)
    increased_positions = models.IntegerField(null=True)
    closed_positions = models.IntegerField(null=True)
    reduced_positions = models.IntegerField(null=True)
    total_calls = models.FloatField(null=True)
    total_puts = models.FloatField(null=True)
    put_call_ratio = models.FloatField(null=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        managed = False
        db_table = "us_institutional_holder"


# --- 39. import_progress ---
class ImportProgress(models.Model):
    table_name = models.CharField(max_length=50)
    ticker = models.CharField(max_length=20)
    completed_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        managed = False
        db_table = "import_progress"
