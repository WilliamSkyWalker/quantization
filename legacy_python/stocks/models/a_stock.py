"""
A 股数据 Django Models — managed=False 映射 PostgreSQL 表。

字段与 Tushare Pro API 输出字段一一对应（snake_case 原名），
实现"API 返回什么就存什么"。部分端点根据官方文档和常见实战字段补齐。

unique keys（用于 UpsertManager.upsert 分流 bulk_create/update）：
    - AStockBasic: (ts_code)
    - ADailyPrice / AIndexDaily: (ts_code, trade_date)
    - AFinancial*: (ts_code, end_date, report_type)
    - AIndustryClass: (ts_code, src, level)
    - AMacroIndicator: (indicator, report_date, freq)
    - ACommodityPrice: (ts_code, trade_date)
    - AInsiderTrade: (ts_code, change_date, holder_name)
    - AResearchReport: (report_id)
    - ATradeCal: (exchange, cal_date)
"""

from django.db import models


# ============================================================
# 1. stock_basic — 股票基础信息（Tushare stock_basic 全字段）
# ============================================================

class AStockBasic(models.Model):
    ts_code = models.CharField(max_length=20)
    symbol = models.CharField(max_length=20, blank=True, null=True)
    name = models.CharField(max_length=100, blank=True, null=True)
    area = models.CharField(max_length=50, blank=True, null=True)
    industry = models.CharField(max_length=100, blank=True, null=True)
    fullname = models.CharField(max_length=200, blank=True, null=True)
    enname = models.CharField(max_length=200, blank=True, null=True)
    cnspell = models.CharField(max_length=50, blank=True, null=True)
    market = models.CharField(max_length=50, blank=True, null=True)
    exchange = models.CharField(max_length=20, blank=True, null=True)
    curr_type = models.CharField(max_length=10, blank=True, null=True)
    list_status = models.CharField(max_length=5, blank=True, null=True)
    list_date = models.DateField(null=True)
    delist_date = models.DateField(null=True)
    is_hs = models.CharField(max_length=5, blank=True, null=True)
    act_name = models.CharField(max_length=200, blank=True, null=True)
    act_ent_type = models.CharField(max_length=100, blank=True, null=True)
    # 内部沉淀字段（非 Tushare 原生）：
    is_st = models.IntegerField(default=0)        # 名字含 ST/*ST 标记
    board = models.CharField(max_length=20, blank=True, null=True)  # 主板/创业板/科创板
    total_share = models.FloatField(null=True)    # daily_basic 最新快照
    float_share = models.FloatField(null=True)
    free_share = models.FloatField(null=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        managed = False
        db_table = "a_stock_basic"


# ============================================================
# 2. daily_price — 日线行情（daily + daily_basic + adj_factor 合表）
# ============================================================

class ADailyPrice(models.Model):
    ts_code = models.CharField(max_length=20)
    trade_date = models.DateField()
    # --- pro.daily 原始字段 ---
    open = models.FloatField(null=True)
    high = models.FloatField(null=True)
    low = models.FloatField(null=True)
    close = models.FloatField(null=True)
    pre_close = models.FloatField(null=True)
    change = models.FloatField(null=True)
    pct_chg = models.FloatField(null=True)
    vol = models.FloatField(null=True)          # 成交量（手）
    amount = models.FloatField(null=True)       # 成交额（千元）
    # --- pro.daily_basic 原始字段 ---
    turnover_rate = models.FloatField(null=True)
    turnover_rate_f = models.FloatField(null=True)
    volume_ratio = models.FloatField(null=True)
    pe = models.FloatField(null=True)
    pe_ttm = models.FloatField(null=True)
    pb = models.FloatField(null=True)
    ps = models.FloatField(null=True)
    ps_ttm = models.FloatField(null=True)
    dv_ratio = models.FloatField(null=True)
    dv_ttm = models.FloatField(null=True)
    total_share = models.FloatField(null=True)
    float_share = models.FloatField(null=True)
    free_share = models.FloatField(null=True)
    total_mv = models.FloatField(null=True)
    circ_mv = models.FloatField(null=True)
    # --- pro.adj_factor ---
    adj_factor = models.FloatField(null=True)
    # --- 内部沉淀 ---
    is_limit_up = models.IntegerField(default=0)
    is_limit_down = models.IntegerField(default=0)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        managed = False
        db_table = "a_daily_price"
        unique_together = (("ts_code", "trade_date"),)


# ============================================================
# 3. index_daily — 指数日线（分离于股票 daily_price）
# ============================================================

class AIndexDaily(models.Model):
    ts_code = models.CharField(max_length=20)
    trade_date = models.DateField()
    close = models.FloatField(null=True)
    open = models.FloatField(null=True)
    high = models.FloatField(null=True)
    low = models.FloatField(null=True)
    pre_close = models.FloatField(null=True)
    change = models.FloatField(null=True)
    pct_chg = models.FloatField(null=True)
    vol = models.FloatField(null=True)
    amount = models.FloatField(null=True)
    # 申万 sw_daily 额外字段
    pe = models.FloatField(null=True)
    pb = models.FloatField(null=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        managed = False
        db_table = "a_index_daily"
        unique_together = (("ts_code", "trade_date"),)


# ============================================================
# 4. a_financial_income — 利润表（Tushare income 全字段）
# ============================================================

class AFinancialIncome(models.Model):
    ts_code = models.CharField(max_length=20)
    ann_date = models.DateField(null=True)
    f_ann_date = models.DateField(null=True)
    end_date = models.DateField()
    report_type = models.CharField(max_length=10, blank=True, null=True)
    comp_type = models.CharField(max_length=10, blank=True, null=True)
    end_type = models.CharField(max_length=10, blank=True, null=True)
    basic_eps = models.FloatField(null=True)
    diluted_eps = models.FloatField(null=True)
    total_revenue = models.FloatField(null=True)
    revenue = models.FloatField(null=True)
    int_income = models.FloatField(null=True)
    prem_earned = models.FloatField(null=True)
    comm_income = models.FloatField(null=True)
    n_commis_income = models.FloatField(null=True)
    n_oth_income = models.FloatField(null=True)
    n_oth_b_income = models.FloatField(null=True)
    prem_income = models.FloatField(null=True)
    out_prem = models.FloatField(null=True)
    une_prem_reser = models.FloatField(null=True)
    reins_income = models.FloatField(null=True)
    n_sec_tb_income = models.FloatField(null=True)
    n_sec_uw_income = models.FloatField(null=True)
    n_asset_mg_income = models.FloatField(null=True)
    oth_b_income = models.FloatField(null=True)
    fv_value_chg_gain = models.FloatField(null=True)
    invest_income = models.FloatField(null=True)
    ass_invest_income = models.FloatField(null=True)
    forex_gain = models.FloatField(null=True)
    total_cogs = models.FloatField(null=True)
    oper_cost = models.FloatField(null=True)
    int_exp = models.FloatField(null=True)
    comm_exp = models.FloatField(null=True)
    biz_tax_surchg = models.FloatField(null=True)
    sell_exp = models.FloatField(null=True)
    admin_exp = models.FloatField(null=True)
    fin_exp = models.FloatField(null=True)
    assets_impair_loss = models.FloatField(null=True)
    prem_refund = models.FloatField(null=True)
    compens_payout = models.FloatField(null=True)
    reser_insur_liab = models.FloatField(null=True)
    div_payt = models.FloatField(null=True)
    reins_exp = models.FloatField(null=True)
    oper_exp = models.FloatField(null=True)
    compens_payout_refu = models.FloatField(null=True)
    insur_reser_refu = models.FloatField(null=True)
    reins_cost_refund = models.FloatField(null=True)
    other_bus_cost = models.FloatField(null=True)
    operate_profit = models.FloatField(null=True)
    non_oper_income = models.FloatField(null=True)
    non_oper_exp = models.FloatField(null=True)
    nca_disploss = models.FloatField(null=True)
    total_profit = models.FloatField(null=True)
    income_tax = models.FloatField(null=True)
    n_income = models.FloatField(null=True)
    n_income_attr_p = models.FloatField(null=True)
    minority_gain = models.FloatField(null=True)
    oth_compr_income = models.FloatField(null=True)
    t_compr_income = models.FloatField(null=True)
    compr_inc_attr_p = models.FloatField(null=True)
    compr_inc_attr_m_s = models.FloatField(null=True)
    ebit = models.FloatField(null=True)
    ebitda = models.FloatField(null=True)
    insurance_exp = models.FloatField(null=True)
    undist_profit = models.FloatField(null=True)
    distable_profit = models.FloatField(null=True)
    rd_exp = models.FloatField(null=True)
    fin_exp_int_exp = models.FloatField(null=True)
    fin_exp_int_inc = models.FloatField(null=True)
    transfer_surplus_rese = models.FloatField(null=True)
    transfer_housing_imprest = models.FloatField(null=True)
    transfer_oth = models.FloatField(null=True)
    adj_lossgain = models.FloatField(null=True)
    withdra_legal_surplus = models.FloatField(null=True)
    withdra_legal_pubfund = models.FloatField(null=True)
    withdra_biz_devfund = models.FloatField(null=True)
    withdra_rese_fund = models.FloatField(null=True)
    withdra_oth_ersu = models.FloatField(null=True)
    workers_welfare = models.FloatField(null=True)
    distr_profit_shrhder = models.FloatField(null=True)
    prfshare_payable_dvd = models.FloatField(null=True)
    comshare_payable_dvd = models.FloatField(null=True)
    capit_comstock_div = models.FloatField(null=True)
    net_after_nr_lp_correct = models.FloatField(null=True)
    credit_impa_loss = models.FloatField(null=True)
    net_expo_hedging_benefits = models.FloatField(null=True)
    oth_impair_loss_assets = models.FloatField(null=True)
    total_opcost = models.FloatField(null=True)
    amodcost_fin_assets = models.FloatField(null=True)
    oth_income = models.FloatField(null=True)
    asset_disp_income = models.FloatField(null=True)
    continued_net_profit = models.FloatField(null=True)
    end_net_profit = models.FloatField(null=True)
    update_flag = models.CharField(max_length=5, blank=True, null=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        managed = False
        db_table = "a_financial_income"
        unique_together = (("ts_code", "end_date", "report_type"),)


# ============================================================
# 5. a_financial_balance — 资产负债表（Tushare balancesheet 全字段）
# ============================================================

class AFinancialBalance(models.Model):
    ts_code = models.CharField(max_length=20)
    ann_date = models.DateField(null=True)
    f_ann_date = models.DateField(null=True)
    end_date = models.DateField()
    report_type = models.CharField(max_length=10, blank=True, null=True)
    comp_type = models.CharField(max_length=10, blank=True, null=True)
    end_type = models.CharField(max_length=10, blank=True, null=True)
    # --- 股本/权益 ---
    total_share = models.FloatField(null=True)
    cap_rese = models.FloatField(null=True)
    undistr_porfit = models.FloatField(null=True)
    surplus_rese = models.FloatField(null=True)
    special_rese = models.FloatField(null=True)
    money_cap = models.FloatField(null=True)
    trad_asset = models.FloatField(null=True)
    notes_receiv = models.FloatField(null=True)
    accounts_receiv = models.FloatField(null=True)
    oth_receiv = models.FloatField(null=True)
    prepayment = models.FloatField(null=True)
    div_receiv = models.FloatField(null=True)
    int_receiv = models.FloatField(null=True)
    inventories = models.FloatField(null=True)
    amor_exp = models.FloatField(null=True)
    nca_within_1y = models.FloatField(null=True)
    sett_rsrv = models.FloatField(null=True)
    loanto_oth_bank_fi = models.FloatField(null=True)
    premium_receiv = models.FloatField(null=True)
    reinsur_receiv = models.FloatField(null=True)
    reinsur_res_receiv = models.FloatField(null=True)
    pur_resale_fa = models.FloatField(null=True)
    oth_cur_assets = models.FloatField(null=True)
    total_cur_assets = models.FloatField(null=True)
    fa_avail_for_sale = models.FloatField(null=True)
    htm_invest = models.FloatField(null=True)
    lt_eqt_invest = models.FloatField(null=True)
    invest_real_estate = models.FloatField(null=True)
    time_deposits = models.FloatField(null=True)
    oth_assets = models.FloatField(null=True)
    lt_rec = models.FloatField(null=True)
    fix_assets = models.FloatField(null=True)
    cip = models.FloatField(null=True)
    const_materials = models.FloatField(null=True)
    fixed_assets_disp = models.FloatField(null=True)
    produc_bio_assets = models.FloatField(null=True)
    oil_and_gas_assets = models.FloatField(null=True)
    intan_assets = models.FloatField(null=True)
    r_and_d = models.FloatField(null=True)
    goodwill = models.FloatField(null=True)
    lt_amor_exp = models.FloatField(null=True)
    defer_tax_assets = models.FloatField(null=True)
    decr_in_disbur = models.FloatField(null=True)
    oth_nca = models.FloatField(null=True)
    total_nca = models.FloatField(null=True)
    cash_reser_cb = models.FloatField(null=True)
    depos_in_oth_bfi = models.FloatField(null=True)
    prec_metals = models.FloatField(null=True)
    deriv_assets = models.FloatField(null=True)
    rr_reins_une_prem = models.FloatField(null=True)
    rr_reins_outstd_cla = models.FloatField(null=True)
    rr_reins_lins_liab = models.FloatField(null=True)
    rr_reins_lthins_liab = models.FloatField(null=True)
    refund_depos = models.FloatField(null=True)
    ph_pledge_loans = models.FloatField(null=True)
    refund_cap_depos = models.FloatField(null=True)
    indep_acct_assets = models.FloatField(null=True)
    client_depos = models.FloatField(null=True)
    client_prov = models.FloatField(null=True)
    transac_seat_fee = models.FloatField(null=True)
    invest_as_receiv = models.FloatField(null=True)
    total_assets = models.FloatField(null=True)
    # --- 负债 ---
    lt_borr = models.FloatField(null=True)
    st_borr = models.FloatField(null=True)
    cb_borr = models.FloatField(null=True)
    depos_ib_deposits = models.FloatField(null=True)
    loan_oth_bank = models.FloatField(null=True)
    trading_fl = models.FloatField(null=True)
    notes_payable = models.FloatField(null=True)
    acct_payable = models.FloatField(null=True)
    adv_receipts = models.FloatField(null=True)
    sold_for_repur_fa = models.FloatField(null=True)
    comm_payable = models.FloatField(null=True)
    payroll_payable = models.FloatField(null=True)
    taxes_payable = models.FloatField(null=True)
    int_payable = models.FloatField(null=True)
    div_payable = models.FloatField(null=True)
    oth_payable = models.FloatField(null=True)
    acc_exp = models.FloatField(null=True)
    deferred_inc = models.FloatField(null=True)
    st_bonds_payable = models.FloatField(null=True)
    payable_to_reinsurer = models.FloatField(null=True)
    rsrv_insur_cont = models.FloatField(null=True)
    acting_trading_sec = models.FloatField(null=True)
    acting_uw_sec = models.FloatField(null=True)
    non_cur_liab_due_1y = models.FloatField(null=True)
    oth_cur_liab = models.FloatField(null=True)
    total_cur_liab = models.FloatField(null=True)
    bond_payable = models.FloatField(null=True)
    lt_payable = models.FloatField(null=True)
    specific_payables = models.FloatField(null=True)
    estimated_liab = models.FloatField(null=True)
    defer_tax_liab = models.FloatField(null=True)
    defer_inc_non_cur_liab = models.FloatField(null=True)
    oth_ncl = models.FloatField(null=True)
    total_ncl = models.FloatField(null=True)
    depos_oth_bfi = models.FloatField(null=True)
    deriv_liab = models.FloatField(null=True)
    depos = models.FloatField(null=True)
    agency_bus_liab = models.FloatField(null=True)
    oth_liab = models.FloatField(null=True)
    prem_receiv_adva = models.FloatField(null=True)
    depos_received = models.FloatField(null=True)
    ph_invest = models.FloatField(null=True)
    reser_une_prem = models.FloatField(null=True)
    reser_outstd_claims = models.FloatField(null=True)
    reser_lins_liab = models.FloatField(null=True)
    reser_lthins_liab = models.FloatField(null=True)
    indept_acc_liab = models.FloatField(null=True)
    pledge_borr = models.FloatField(null=True)
    indem_payable = models.FloatField(null=True)
    policy_div_payable = models.FloatField(null=True)
    total_liab = models.FloatField(null=True)
    # --- 权益总计 ---
    treasury_share = models.FloatField(null=True)
    ordin_risk_reser = models.FloatField(null=True)
    forex_differ = models.FloatField(null=True)
    invest_loss_unconf = models.FloatField(null=True)
    minority_int = models.FloatField(null=True)
    total_hldr_eqy_exc_min_int = models.FloatField(null=True)
    total_hldr_eqy_inc_min_int = models.FloatField(null=True)
    total_liab_hldr_eqy = models.FloatField(null=True)
    lt_payroll_payable = models.FloatField(null=True)
    oth_comp_income = models.FloatField(null=True)
    oth_eqt_tools = models.FloatField(null=True)
    oth_eqt_tools_p_shr = models.FloatField(null=True)
    lending_funds = models.FloatField(null=True)
    acc_receivable = models.FloatField(null=True)
    st_fin_payable = models.FloatField(null=True)
    payables = models.FloatField(null=True)
    hfs_assets = models.FloatField(null=True)
    hfs_sales = models.FloatField(null=True)
    cost_fin_assets = models.FloatField(null=True)
    fair_value_fin_assets = models.FloatField(null=True)
    contract_assets = models.FloatField(null=True)
    contract_liab = models.FloatField(null=True)
    accounts_receiv_bill = models.FloatField(null=True)
    accounts_pay = models.FloatField(null=True)
    oth_rcv_total = models.FloatField(null=True)
    fix_assets_total = models.FloatField(null=True)
    cip_total = models.FloatField(null=True)
    oth_pay_total = models.FloatField(null=True)
    long_pay_total = models.FloatField(null=True)
    debt_invest = models.FloatField(null=True)
    oth_debt_invest = models.FloatField(null=True)
    oth_eq_invest = models.FloatField(null=True)
    oth_illiq_fin_assets = models.FloatField(null=True)
    oth_eq_ppbond = models.FloatField(null=True)
    receiv_financing = models.FloatField(null=True)
    use_right_assets = models.FloatField(null=True)
    lease_liab = models.FloatField(null=True)
    update_flag = models.CharField(max_length=5, blank=True, null=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        managed = False
        db_table = "a_financial_balance"
        unique_together = (("ts_code", "end_date", "report_type"),)


# ============================================================
# 6. a_financial_cashflow — 现金流量表（Tushare cashflow 全字段）
# ============================================================

class AFinancialCashflow(models.Model):
    ts_code = models.CharField(max_length=20)
    ann_date = models.DateField(null=True)
    f_ann_date = models.DateField(null=True)
    end_date = models.DateField()
    comp_type = models.CharField(max_length=10, blank=True, null=True)
    report_type = models.CharField(max_length=10, blank=True, null=True)
    end_type = models.CharField(max_length=10, blank=True, null=True)
    net_profit = models.FloatField(null=True)
    finan_exp = models.FloatField(null=True)
    c_fr_sale_sg = models.FloatField(null=True)
    recp_tax_rends = models.FloatField(null=True)
    n_depos_incr_fi = models.FloatField(null=True)
    n_incr_loans_cb = models.FloatField(null=True)
    n_inc_borr_oth_fi = models.FloatField(null=True)
    prem_fr_orig_contr = models.FloatField(null=True)
    n_incr_insured_dep = models.FloatField(null=True)
    n_reinsur_prem = models.FloatField(null=True)
    n_incr_disp_tfa = models.FloatField(null=True)
    ifc_cash_incr = models.FloatField(null=True)
    n_incr_disp_faas = models.FloatField(null=True)
    n_incr_loans_oth_bank = models.FloatField(null=True)
    n_cap_incr_repur = models.FloatField(null=True)
    c_fr_oth_operate_a = models.FloatField(null=True)
    c_inf_fr_operate_a = models.FloatField(null=True)
    c_paid_goods_s = models.FloatField(null=True)
    c_paid_to_for_empl = models.FloatField(null=True)
    c_paid_for_taxes = models.FloatField(null=True)
    n_incr_clt_loan_adv = models.FloatField(null=True)
    n_incr_dep_cbob = models.FloatField(null=True)
    c_pay_claims_orig_inco = models.FloatField(null=True)
    pay_handling_chrg = models.FloatField(null=True)
    pay_comm_insur_plcy = models.FloatField(null=True)
    oth_cash_pay_oper_act = models.FloatField(null=True)
    st_cash_out_act = models.FloatField(null=True)
    n_cashflow_act = models.FloatField(null=True)
    oth_recp_ral_inv_act = models.FloatField(null=True)
    c_disp_withdrwl_invest = models.FloatField(null=True)
    c_recp_return_invest = models.FloatField(null=True)
    n_recp_disp_fiolta = models.FloatField(null=True)
    n_recp_disp_sobu = models.FloatField(null=True)
    stot_inflows_inv_act = models.FloatField(null=True)
    c_pay_acq_const_fiolta = models.FloatField(null=True)
    c_paid_invest = models.FloatField(null=True)
    n_disp_subs_oth_biz = models.FloatField(null=True)
    oth_pay_ral_inv_act = models.FloatField(null=True)
    n_incr_pledge_loan = models.FloatField(null=True)
    stot_out_inv_act = models.FloatField(null=True)
    n_cashflow_inv_act = models.FloatField(null=True)
    c_recp_borrow = models.FloatField(null=True)
    proc_issue_bonds = models.FloatField(null=True)
    oth_cash_recp_ral_fnc_act = models.FloatField(null=True)
    stot_cash_in_fnc_act = models.FloatField(null=True)
    free_cashflow = models.FloatField(null=True)
    c_prepay_amt_borr = models.FloatField(null=True)
    c_pay_dist_dpcp_int_exp = models.FloatField(null=True)
    incl_dvd_profit_paid_sc_ms = models.FloatField(null=True)
    oth_cashpay_ral_fnc_act = models.FloatField(null=True)
    stot_cashout_fnc_act = models.FloatField(null=True)
    n_cash_flows_fnc_act = models.FloatField(null=True)
    eff_fx_flu_cash = models.FloatField(null=True)
    n_incr_cash_cash_equ = models.FloatField(null=True)
    c_cash_equ_beg_period = models.FloatField(null=True)
    c_cash_equ_end_period = models.FloatField(null=True)
    c_recp_cap_contrib = models.FloatField(null=True)
    incl_cash_rec_saims = models.FloatField(null=True)
    uncon_invest_loss = models.FloatField(null=True)
    prov_depr_assets = models.FloatField(null=True)
    depr_fa_coga_dpba = models.FloatField(null=True)
    amort_intang_assets = models.FloatField(null=True)
    lt_amort_deferred_exp = models.FloatField(null=True)
    decr_deferred_exp = models.FloatField(null=True)
    incr_acc_exp = models.FloatField(null=True)
    loss_disp_fiolta = models.FloatField(null=True)
    loss_scr_fa = models.FloatField(null=True)
    loss_fv_chg = models.FloatField(null=True)
    invest_loss = models.FloatField(null=True)
    decr_def_inc_tax_assets = models.FloatField(null=True)
    incr_def_inc_tax_liab = models.FloatField(null=True)
    decr_inventories = models.FloatField(null=True)
    decr_oper_payable = models.FloatField(null=True)
    incr_oper_payable = models.FloatField(null=True)
    others = models.FloatField(null=True)
    im_net_cashflow_oper_act = models.FloatField(null=True)
    conv_debt_into_cap = models.FloatField(null=True)
    conv_copbonds_due_within_1y = models.FloatField(null=True)
    fa_fnc_leases = models.FloatField(null=True)
    im_n_incr_cash_equ = models.FloatField(null=True)
    net_dism_capital_add = models.FloatField(null=True)
    net_cash_rece_sec = models.FloatField(null=True)
    credit_impa_loss = models.FloatField(null=True)
    use_right_asset_dep = models.FloatField(null=True)
    oth_loss_asset = models.FloatField(null=True)
    end_bal_cash = models.FloatField(null=True)
    beg_bal_cash = models.FloatField(null=True)
    end_bal_cash_equ = models.FloatField(null=True)
    beg_bal_cash_equ = models.FloatField(null=True)
    update_flag = models.CharField(max_length=5, blank=True, null=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        managed = False
        db_table = "a_financial_cashflow"
        unique_together = (("ts_code", "end_date", "report_type"),)


# ============================================================
# 7. a_financial_indicator — 财务指标（Tushare fina_indicator 全字段）
# ============================================================

class AFinancialIndicator(models.Model):
    ts_code = models.CharField(max_length=20)
    ann_date = models.DateField(null=True)
    end_date = models.DateField()
    eps = models.FloatField(null=True)
    dt_eps = models.FloatField(null=True)
    total_revenue_ps = models.FloatField(null=True)
    revenue_ps = models.FloatField(null=True)
    capital_rese_ps = models.FloatField(null=True)
    surplus_rese_ps = models.FloatField(null=True)
    undist_profit_ps = models.FloatField(null=True)
    extra_item = models.FloatField(null=True)
    profit_dedt = models.FloatField(null=True)
    gross_margin = models.FloatField(null=True)
    current_ratio = models.FloatField(null=True)
    quick_ratio = models.FloatField(null=True)
    cash_ratio = models.FloatField(null=True)
    invturn_days = models.FloatField(null=True)
    arturn_days = models.FloatField(null=True)
    inv_turn = models.FloatField(null=True)
    ar_turn = models.FloatField(null=True)
    ca_turn = models.FloatField(null=True)
    fa_turn = models.FloatField(null=True)
    assets_turn = models.FloatField(null=True)
    op_income = models.FloatField(null=True)
    valuechange_income = models.FloatField(null=True)
    interst_income = models.FloatField(null=True)
    daa = models.FloatField(null=True)
    ebit = models.FloatField(null=True)
    ebitda = models.FloatField(null=True)
    fcff = models.FloatField(null=True)
    fcfe = models.FloatField(null=True)
    current_exint = models.FloatField(null=True)
    noncurrent_exint = models.FloatField(null=True)
    interestdebt = models.FloatField(null=True)
    netdebt = models.FloatField(null=True)
    tangible_asset = models.FloatField(null=True)
    working_capital = models.FloatField(null=True)
    networking_capital = models.FloatField(null=True)
    invest_capital = models.FloatField(null=True)
    retained_earnings = models.FloatField(null=True)
    diluted2_eps = models.FloatField(null=True)
    bps = models.FloatField(null=True)
    ocfps = models.FloatField(null=True)
    retainedps = models.FloatField(null=True)
    cfps = models.FloatField(null=True)
    ebit_ps = models.FloatField(null=True)
    fcff_ps = models.FloatField(null=True)
    fcfe_ps = models.FloatField(null=True)
    netprofit_margin = models.FloatField(null=True)
    grossprofit_margin = models.FloatField(null=True)
    cogs_of_sales = models.FloatField(null=True)
    expense_of_sales = models.FloatField(null=True)
    profit_to_gr = models.FloatField(null=True)
    saleexp_to_gr = models.FloatField(null=True)
    adminexp_of_gr = models.FloatField(null=True)
    finaexp_of_gr = models.FloatField(null=True)
    impai_ttm = models.FloatField(null=True)
    gc_of_gr = models.FloatField(null=True)
    op_of_gr = models.FloatField(null=True)
    ebit_of_gr = models.FloatField(null=True)
    roe = models.FloatField(null=True)
    roe_waa = models.FloatField(null=True)
    roe_dt = models.FloatField(null=True)
    roa = models.FloatField(null=True)
    npta = models.FloatField(null=True)
    roic = models.FloatField(null=True)
    roe_yearly = models.FloatField(null=True)
    roa2_yearly = models.FloatField(null=True)
    roe_avg = models.FloatField(null=True)
    opincome_of_ebt = models.FloatField(null=True)
    investincome_of_ebt = models.FloatField(null=True)
    n_op_profit_of_ebt = models.FloatField(null=True)
    tax_to_ebt = models.FloatField(null=True)
    dtprofit_to_profit = models.FloatField(null=True)
    salescash_to_or = models.FloatField(null=True)
    ocf_to_or = models.FloatField(null=True)
    ocf_to_opincome = models.FloatField(null=True)
    capitalized_to_da = models.FloatField(null=True)
    debt_to_assets = models.FloatField(null=True)
    assets_to_eqt = models.FloatField(null=True)
    dp_assets_to_eqt = models.FloatField(null=True)
    ca_to_assets = models.FloatField(null=True)
    nca_to_assets = models.FloatField(null=True)
    tbassets_to_totalassets = models.FloatField(null=True)
    int_to_talcap = models.FloatField(null=True)
    eqt_to_talcapital = models.FloatField(null=True)
    currentdebt_to_debt = models.FloatField(null=True)
    longdeb_to_debt = models.FloatField(null=True)
    ocf_to_shortdebt = models.FloatField(null=True)
    debt_to_eqt = models.FloatField(null=True)
    eqt_to_debt = models.FloatField(null=True)
    eqt_to_interestdebt = models.FloatField(null=True)
    tangibleasset_to_debt = models.FloatField(null=True)
    tangasset_to_intdebt = models.FloatField(null=True)
    tangibleasset_to_netdebt = models.FloatField(null=True)
    ocf_to_debt = models.FloatField(null=True)
    ocf_to_interestdebt = models.FloatField(null=True)
    ocf_to_netdebt = models.FloatField(null=True)
    ebit_to_interest = models.FloatField(null=True)
    longdebt_to_workingcapital = models.FloatField(null=True)
    ebitda_to_debt = models.FloatField(null=True)
    turn_days = models.FloatField(null=True)
    roa_yearly = models.FloatField(null=True)
    roa_dp = models.FloatField(null=True)
    fixed_assets = models.FloatField(null=True)
    profit_prefin_exp = models.FloatField(null=True)
    non_op_profit = models.FloatField(null=True)
    op_to_ebt = models.FloatField(null=True)
    nop_to_ebt = models.FloatField(null=True)
    ocf_to_profit = models.FloatField(null=True)
    cash_to_liqdebt = models.FloatField(null=True)
    cash_to_liqdebt_withinterest = models.FloatField(null=True)
    op_to_liqdebt = models.FloatField(null=True)
    op_to_debt = models.FloatField(null=True)
    roic_yearly = models.FloatField(null=True)
    total_fa_trun = models.FloatField(null=True)
    profit_to_op = models.FloatField(null=True)
    q_opincome = models.FloatField(null=True)
    q_investincome = models.FloatField(null=True)
    q_dtprofit = models.FloatField(null=True)
    q_eps = models.FloatField(null=True)
    q_netprofit_margin = models.FloatField(null=True)
    q_gsprofit_margin = models.FloatField(null=True)
    q_exp_to_sales = models.FloatField(null=True)
    q_profit_to_gr = models.FloatField(null=True)
    q_saleexp_to_gr = models.FloatField(null=True)
    q_adminexp_to_gr = models.FloatField(null=True)
    q_finaexp_to_gr = models.FloatField(null=True)
    q_impair_to_gr_ttm = models.FloatField(null=True)
    q_gc_to_gr = models.FloatField(null=True)
    q_op_to_gr = models.FloatField(null=True)
    q_roe = models.FloatField(null=True)
    q_dt_roe = models.FloatField(null=True)
    q_npta = models.FloatField(null=True)
    q_opincome_to_ebt = models.FloatField(null=True)
    q_investincome_to_ebt = models.FloatField(null=True)
    q_dtprofit_to_profit = models.FloatField(null=True)
    q_salescash_to_or = models.FloatField(null=True)
    q_ocf_to_sales = models.FloatField(null=True)
    q_ocf_to_or = models.FloatField(null=True)
    basic_eps_yoy = models.FloatField(null=True)
    dt_eps_yoy = models.FloatField(null=True)
    cfps_yoy = models.FloatField(null=True)
    op_yoy = models.FloatField(null=True)
    ebt_yoy = models.FloatField(null=True)
    netprofit_yoy = models.FloatField(null=True)
    dt_netprofit_yoy = models.FloatField(null=True)
    ocf_yoy = models.FloatField(null=True)
    roe_yoy = models.FloatField(null=True)
    bps_yoy = models.FloatField(null=True)
    assets_yoy = models.FloatField(null=True)
    eqt_yoy = models.FloatField(null=True)
    tr_yoy = models.FloatField(null=True)
    or_yoy = models.FloatField(null=True)
    q_gr_yoy = models.FloatField(null=True)
    q_gr_qoq = models.FloatField(null=True)
    q_sales_yoy = models.FloatField(null=True)
    q_sales_qoq = models.FloatField(null=True)
    q_op_yoy = models.FloatField(null=True)
    q_op_qoq = models.FloatField(null=True)
    q_profit_yoy = models.FloatField(null=True)
    q_profit_qoq = models.FloatField(null=True)
    q_netprofit_yoy = models.FloatField(null=True)
    q_netprofit_qoq = models.FloatField(null=True)
    equity_yoy = models.FloatField(null=True)
    rd_exp = models.FloatField(null=True)
    update_flag = models.CharField(max_length=5, blank=True, null=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        managed = False
        db_table = "a_financial_indicator"
        unique_together = (("ts_code", "end_date"),)


# ============================================================
# 8. industry_class — 申万行业分类（index_classify + index_member 合表）
# ============================================================

class AIndustryClass(models.Model):
    ts_code = models.CharField(max_length=20)
    src = models.CharField(max_length=20, default="SW2021")   # SW2014/SW2021
    level = models.CharField(max_length=5, default="L1")      # L1/L2/L3
    index_code = models.CharField(max_length=20, blank=True, null=True)   # 申万指数代码
    index_name = models.CharField(max_length=100, blank=True, null=True)  # 行业名
    in_date = models.DateField(null=True)
    out_date = models.DateField(null=True)
    is_new = models.CharField(max_length=5, blank=True, null=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        managed = False
        db_table = "a_industry_class"
        unique_together = (("ts_code", "src", "level", "index_code", "in_date"),)


# ============================================================
# 9. macro_indicator — 宏观指标汇总表
# ============================================================

class AMacroIndicator(models.Model):
    indicator = models.CharField(max_length=50)      # shibor/lpr/cpi/ppi/pmi/m1/m2/gdp/us_tycr_10y 等
    report_date = models.DateField()
    freq = models.CharField(max_length=10, default="D")  # D/M/Q/Y
    value = models.FloatField(null=True)
    extra = models.JSONField(null=True)              # 备用字段（如 pmi 分项）
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        managed = False
        db_table = "a_macro_indicator"
        unique_together = (("indicator", "report_date", "freq"),)


# ============================================================
# 10. commodity_price — 商品期货主力合约日线（Tushare fut_daily 全字段）
# ============================================================

class ACommodityPrice(models.Model):
    ts_code = models.CharField(max_length=20)       # CU.SHF 等主力合约代码
    name = models.CharField(max_length=50, blank=True, null=True)
    trade_date = models.DateField()
    pre_close = models.FloatField(null=True)
    pre_settle = models.FloatField(null=True)
    open = models.FloatField(null=True)
    high = models.FloatField(null=True)
    low = models.FloatField(null=True)
    close = models.FloatField(null=True)
    settle = models.FloatField(null=True)
    change1 = models.FloatField(null=True)
    change2 = models.FloatField(null=True)
    vol = models.FloatField(null=True)
    amount = models.FloatField(null=True)
    oi = models.FloatField(null=True)
    oi_chg = models.FloatField(null=True)
    delv_settle = models.FloatField(null=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        managed = False
        db_table = "a_commodity_price"
        unique_together = (("ts_code", "trade_date"),)


# ============================================================
# 11. insider_transaction — 高管/股东增减持（AkShare / Eastmoney）
# ============================================================

class AInsiderTrade(models.Model):
    """AkShare stock_ggcg_em 全字段（16 列）。"""
    ts_code = models.CharField(max_length=20)
    name = models.CharField(max_length=100, blank=True, null=True)             # 名称
    change_date = models.DateField()                                            # 变动开始日
    change_end_date = models.DateField(null=True)                               # 变动截止日
    ann_date = models.DateField(null=True)                                      # 公告日
    holder_name = models.CharField(max_length=500)                              # 股东名称
    holder_type = models.CharField(max_length=50, blank=True, null=True)        # 增减方向
    change_vol = models.FloatField(null=True)                                   # 变动数量
    change_ratio = models.FloatField(null=True)                                 # 占总股本比例
    hold_ratio = models.FloatField(null=True)                                   # 占流通股比例
    total_share = models.FloatField(null=True)                                  # 变动后持股总数
    total_share_ratio = models.FloatField(null=True)                            # 变动后占总股本比例
    float_share = models.FloatField(null=True)                                  # 变动后持流通股数
    float_share_ratio = models.FloatField(null=True)                            # 变动后占流通股比例
    latest_price = models.FloatField(null=True)                                 # 最新价
    change_pct = models.FloatField(null=True)                                   # 涨跌幅
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        managed = False
        db_table = "a_insider_transaction"
        unique_together = (("ts_code", "change_date", "holder_name", "holder_type"),)


# ============================================================
# 12. research_report — 研报（AkShare）
# ============================================================

class AResearchReport(models.Model):
    """东方财富研报 API 全 51 列（reportapi.eastmoney.com/report/list）。"""
    # --- 核心标识 ---
    info_code = models.CharField(max_length=50)                                  # infoCode（unique key）
    ts_code = models.CharField(max_length=20, blank=True, null=True)             # 从 stockCode 转换
    stock_code = models.CharField(max_length=20, blank=True, null=True)          # stockCode 原值
    stock_name = models.CharField(max_length=100, blank=True, null=True)         # stockName
    # --- 发布信息 ---
    publish_date = models.DateTimeField(null=True)                               # publishDate
    title = models.CharField(max_length=500, blank=True, null=True)
    column = models.CharField(max_length=50, blank=True, null=True)
    count = models.IntegerField(null=True)
    report_type = models.CharField(max_length=50, blank=True, null=True)         # reportType
    encode_url = models.CharField(max_length=500, blank=True, null=True)         # encodeUrl
    market = models.CharField(max_length=20, blank=True, null=True)
    # --- 机构 ---
    org_code = models.CharField(max_length=50, blank=True, null=True)            # orgCode
    org_name = models.CharField(max_length=200, blank=True, null=True)           # orgName
    org_s_name = models.CharField(max_length=200, blank=True, null=True)         # orgSName
    org_type = models.CharField(max_length=50, blank=True, null=True)            # orgType
    # --- 研究员 ---
    researcher = models.CharField(max_length=500, blank=True, null=True)
    author = models.CharField(max_length=500, blank=True, null=True)
    author_id = models.CharField(max_length=200, blank=True, null=True)          # authorID
    # --- 评级 ---
    em_rating_code = models.CharField(max_length=20, blank=True, null=True)      # emRatingCode
    em_rating_name = models.CharField(max_length=50, blank=True, null=True)      # emRatingName
    em_rating_value = models.FloatField(null=True)                               # emRatingValue
    rating_change = models.CharField(max_length=50, blank=True, null=True)       # ratingChange
    last_em_rating_code = models.CharField(max_length=20, blank=True, null=True) # lastEmRatingCode
    last_em_rating_name = models.CharField(max_length=50, blank=True, null=True) # lastEmRatingName
    last_em_rating_value = models.FloatField(null=True)                          # lastEmRatingValue
    s_rating_code = models.CharField(max_length=20, blank=True, null=True)       # sRatingCode
    s_rating_name = models.CharField(max_length=50, blank=True, null=True)       # sRatingName
    # --- 行业 ---
    industry_code = models.CharField(max_length=50, blank=True, null=True)       # industryCode
    industry_name = models.CharField(max_length=100, blank=True, null=True)      # industryName
    em_industry_code = models.CharField(max_length=50, blank=True, null=True)    # emIndustryCode
    indv_indu_code = models.CharField(max_length=50, blank=True, null=True)      # indvInduCode
    indv_indu_name = models.CharField(max_length=100, blank=True, null=True)     # indvInduName
    indv_is_new = models.CharField(max_length=10, blank=True, null=True)         # indvIsNew
    # --- 目标价 ---
    indv_aim_price_t = models.FloatField(null=True)                              # indvAimPriceT（最高）
    indv_aim_price_l = models.FloatField(null=True)                              # indvAimPriceL（最低）
    # --- EPS 预测 ---
    predict_this_year_eps = models.FloatField(null=True)                         # predictThisYearEps
    predict_this_year_pe = models.FloatField(null=True)                          # predictThisYearPe
    predict_next_year_eps = models.FloatField(null=True)                         # predictNextYearEps
    predict_next_year_pe = models.FloatField(null=True)                          # predictNextYearPe
    predict_next_two_year_eps = models.FloatField(null=True)                     # predictNextTwoYearEps
    predict_next_two_year_pe = models.FloatField(null=True)                      # predictNextTwoYearPe
    predict_last_year_eps = models.FloatField(null=True)                         # predictLastYearEps
    predict_last_year_pe = models.FloatField(null=True)                          # predictLastYearPe
    actual_last_year_eps = models.FloatField(null=True)                          # actualLastYearEps
    actual_last_two_year_eps = models.FloatField(null=True)                      # actualLastTwoYearEps
    # --- 新股 ---
    new_purchase_date = models.CharField(max_length=30, blank=True, null=True)   # newPurchaseDate
    new_listing_date = models.CharField(max_length=30, blank=True, null=True)    # newListingDate
    new_issue_price = models.FloatField(null=True)                               # newIssuePrice
    new_pe_issue_a = models.FloatField(null=True)                                # newPeIssueA
    # --- 附件 ---
    attach_type = models.CharField(max_length=20, blank=True, null=True)         # attachType
    attach_size = models.CharField(max_length=20, blank=True, null=True)         # attachSize
    attach_pages = models.CharField(max_length=20, blank=True, null=True)        # attachPages
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        managed = False
        db_table = "a_research_report"
        unique_together = (("info_code",),)


# ============================================================
# 13. trade_cal — 交易日历缓存（Tushare trade_cal 全字段）
# ============================================================

class ATradeCal(models.Model):
    exchange = models.CharField(max_length=10)    # SSE/SZSE
    cal_date = models.DateField()
    is_open = models.IntegerField()                # 0/1
    pretrade_date = models.DateField(null=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        managed = False
        db_table = "a_trade_cal"
        unique_together = (("exchange", "cal_date"),)


# ============================================================
# 14~17. 纸面交易 — account / position / transaction / nav
# ============================================================

class APaperAccount(models.Model):
    """A 股纸面账户（多账户支持）。"""
    account_name = models.CharField(max_length=50)
    initial_capital = models.FloatField()
    cash = models.FloatField()
    total_assets = models.FloatField()
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        managed = False
        db_table = "a_paper_account"
        unique_together = (("account_name",),)


class APaperPosition(models.Model):
    account_name = models.CharField(max_length=50)
    ts_code = models.CharField(max_length=20)
    name = models.CharField(max_length=100, blank=True, null=True)
    volume = models.IntegerField(default=0)
    cost_basis = models.FloatField(null=True)        # 成本价（含佣金平摊）
    current_price = models.FloatField(null=True)
    market_value = models.FloatField(null=True)
    weight = models.FloatField(null=True)
    unrealized_pnl = models.FloatField(null=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        managed = False
        db_table = "a_paper_position"
        unique_together = (("account_name", "ts_code"),)


class APaperTransaction(models.Model):
    account_name = models.CharField(max_length=50)
    trade_date = models.DateField()
    ts_code = models.CharField(max_length=20)
    name = models.CharField(max_length=100, blank=True, null=True)
    direction = models.CharField(max_length=10)       # BUY/SELL
    target_volume = models.IntegerField(null=True)
    filled_volume = models.IntegerField(null=True)
    price = models.FloatField(null=True)
    amount = models.FloatField(null=True)
    commission = models.FloatField(null=True)
    stamp_tax = models.FloatField(null=True)
    slippage_cost = models.FloatField(null=True)
    total_cost = models.FloatField(null=True)
    reason = models.CharField(max_length=200, blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        managed = False
        db_table = "a_paper_transaction"


class APaperNav(models.Model):
    account_name = models.CharField(max_length=50)
    trade_date = models.DateField()
    cash = models.FloatField(null=True)
    market_value = models.FloatField(null=True)
    total_assets = models.FloatField(null=True)
    nav = models.FloatField()
    daily_pnl = models.FloatField(null=True)
    daily_return = models.FloatField(null=True)
    n_holdings = models.IntegerField(null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        managed = False
        db_table = "a_paper_nav"
        unique_together = (("account_name", "trade_date"),)


# ============================================================
# 17. industry_factor_config — 行业因子权重配置
# ============================================================

class AIndustryFactorConfig(models.Model):
    industry_name = models.CharField(max_length=100)
    factor_name = models.CharField(max_length=50)
    weight = models.FloatField()
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        managed = False
        db_table = "a_industry_factor_config"
        unique_together = (("industry_name", "factor_name"),)


# ============================================================
# 18. watchlist — A 股自选股
# ============================================================

class AWatchlist(models.Model):
    ts_code = models.CharField(max_length=20)
    name = models.CharField(max_length=100, blank=True, null=True)
    notes = models.TextField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        managed = False
        db_table = "a_watchlist"
        unique_together = (("ts_code",),)


# ============================================================
# 19. selection_result — A 股选股历史结果
# ============================================================

class ASelectionResult(models.Model):
    date = models.DateField()
    total = models.IntegerField(default=0)
    top_stocks = models.TextField(blank=True, null=True)     # JSON
    by_industry = models.TextField(blank=True, null=True)    # JSON
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        managed = False
        db_table = "a_selection_result"
        unique_together = (("date",),)


# ============================================================
# 20. factor_snapshot — A 股因子截面快照（每次选股写入）
# ============================================================

class AFactorSnapshot(models.Model):
    date = models.DateField()
    ts_code = models.CharField(max_length=20)
    score = models.FloatField(null=True)
    factors = models.TextField(blank=True, null=True)        # JSON (每个因子的值)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        managed = False
        db_table = "a_factor_snapshot"
        unique_together = (("date", "ts_code"),)
