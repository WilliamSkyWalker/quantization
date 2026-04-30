"""
A 股表 DDL 生成器 — 从 Django models 生成 DROP + CREATE + UNIQUE INDEX。

用法:
    python3 scripts/generate_ashare_ddl.py > scripts/migrate_ashare_schema.sql

生成后用户手动执行:
    psql $DATABASE_URL -f scripts/migrate_ashare_schema.sql
"""

import os
import sys
from pathlib import Path

import django

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "core.settings")
django.setup()

from django.db import connection  # noqa: E402

from stocks.models.a_stock import (  # noqa: E402
    AStockBasic, ADailyPrice, AIndexDaily,
    AFinancialIncome, AFinancialBalance, AFinancialCashflow, AFinancialIndicator,
    AIndustryClass, AMacroIndicator, ACommodityPrice,
    AInsiderTrade, AResearchReport, ATradeCal,
    APaperAccount, APaperPosition, APaperTransaction, APaperNav, AIndustryFactorConfig,
    AWatchlist, ASelectionResult, AFactorSnapshot,
)

MODELS = [
    AStockBasic, ADailyPrice, AIndexDaily,
    AFinancialIncome, AFinancialBalance, AFinancialCashflow, AFinancialIndicator,
    AIndustryClass, AMacroIndicator, ACommodityPrice,
    AInsiderTrade, AResearchReport, ATradeCal,
    APaperAccount, APaperPosition, APaperTransaction, APaperNav, AIndustryFactorConfig,
    AWatchlist, ASelectionResult, AFactorSnapshot,
]


def main():
    print("-- ========================================================")
    print("-- A 股表 DDL — drop + recreate")
    print("-- 从 stocks/models/a_stock.py 自动生成（scripts/generate_ashare_ddl.py）")
    print("-- !!! 执行前请备份数据库 !!!  pg_dump $DATABASE_URL > ashare_backup.sql")
    print("-- ========================================================")
    print()

    # 1. DROP 旧表
    print("-- 1. DROP 旧表（CASCADE 删除依赖外键）")
    for M in MODELS:
        print(f'DROP TABLE IF EXISTS "{M._meta.db_table}" CASCADE;')
    print()

    # 2. CREATE TABLE（通过 schema_editor 收集 SQL）
    print("-- 2. CREATE TABLE")
    with connection.schema_editor(collect_sql=True) as ed:
        for M in MODELS:
            M._meta.managed = True
            ed.create_model(M)
        for sql in ed.collected_sql:
            print(sql)
    print()

    # 3. UNIQUE INDEX（来自 unique_together，schema_editor 对 managed=False 不自动生成）
    print("-- 3. UNIQUE INDEX（对应 models Meta.unique_together）")
    for M in MODELS:
        for uk in M._meta.unique_together:
            cols = ", ".join(f'"{c}"' for c in uk)
            idx_name = f"uq_{M._meta.db_table}_{'_'.join(uk)}"[:60]
            print(f'CREATE UNIQUE INDEX IF NOT EXISTS "{idx_name}" ON "{M._meta.db_table}" ({cols});')
    print()

    # 4. 常用非 unique 索引（加速 FactorBase 查询）
    print("-- 4. 查询加速索引")
    hot_indexes = [
        ("a_daily_price", ["trade_date"]),
        ("a_daily_price", ["ts_code"]),
        ("a_index_daily", ["trade_date"]),
        ("a_financial_income", ["ts_code", "ann_date"]),
        ("a_financial_balance", ["ts_code", "ann_date"]),
        ("a_financial_cashflow", ["ts_code", "ann_date"]),
        ("a_financial_indicator", ["ts_code", "ann_date"]),
        ("a_industry_class", ["index_code"]),
        ("a_macro_indicator", ["indicator", "report_date"]),
        ("a_commodity_price", ["trade_date"]),
        ("a_insider_transaction", ["change_date"]),
        ("a_research_report", ["ts_code", "publish_date"]),
        ("a_trade_cal", ["cal_date"]),
    ]
    for table, cols in hot_indexes:
        cols_str = ", ".join(f'"{c}"' for c in cols)
        idx_name = f"idx_{table}_{'_'.join(cols)}"[:60]
        print(f'CREATE INDEX IF NOT EXISTS "{idx_name}" ON "{table}" ({cols_str});')

    print()
    print("-- 完成")


if __name__ == "__main__":
    main()
