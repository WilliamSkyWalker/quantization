#!/usr/bin/env python3
"""Regenerate crates/db/src/models/a_financial_rows.rs from migrate_ashare_schema.sql.

Run after DDL changes:
    cd quant-engine
    python3 scripts/gen_a_financial_rows.py > crates/db/src/models/a_financial_rows.rs
    cargo test -p quant-db --lib   # field count test will catch mismatches
"""
import re
import sys
from pathlib import Path

DDL = Path(__file__).resolve().parents[2] / "scripts" / "migrate_ashare_schema.sql"

TABLES = {
    "a_financial_income":    "AFinancialIncomeRow",
    "a_financial_balance":   "AFinancialBalanceRow",
    "a_financial_cashflow":  "AFinancialCashflowRow",
    "a_financial_indicator": "AFinancialIndicatorRow",
}


def parse_table(sql: str, table: str):
    m = re.search(rf'CREATE TABLE "{table}" \((.*?)\);', sql, re.DOTALL)
    if not m:
        return None
    body = m.group(1)
    cols = []
    for part in re.split(r',(?=\s*"[a-z_]+")', body):
        col_m = re.match(r'"(\w+)"\s+(.+)', part.strip())
        if not col_m:
            continue
        name, rest = col_m.group(1), col_m.group(2)
        type_m = re.match(
            r'(varchar\(\d+\)|double precision|bigint|integer|timestamp[^,]*?|date|text)',
            rest,
        )
        typ = type_m.group(1) if type_m else "?"
        null = "NOT NULL" if "NOT NULL" in rest else "NULL"
        cols.append((name, typ, null))
    return cols


def map_type(typ: str, null: str) -> str:
    if typ.startswith("varchar") or typ == "text":
        rs = "String"
    elif typ == "double precision":
        rs = "f64"
    elif typ == "date":
        rs = "NaiveDate"
    elif typ == "bigint":
        rs = "i64"
    elif typ == "integer":
        rs = "i32"
    elif typ.startswith("timestamp"):
        rs = "NaiveDateTime"
    else:
        rs = "?"
    return rs if null == "NOT NULL" else f"Option<{rs}>"


def main() -> None:
    sql = DDL.read_text()
    print("// === Auto-generated full-field models for A-share financial tables ===")
    print(f"// Source: {DDL.name}")
    print("// Regenerate via: python3 scripts/gen_a_financial_rows.py")
    for table, struct in TABLES.items():
        cols = parse_table(sql, table)
        if cols is None:
            print(f"// WARN: {table} not found", file=sys.stderr)
            continue
        print(f"\n// ── {table} ({len(cols)} cols)")
        print("#[derive(Debug, Clone, FromRow)]")
        print(f"pub struct {struct} {{")
        for name, typ, null in cols:
            print(f"    pub {name}: {map_type(typ, null)},")
        print("}")


if __name__ == "__main__":
    main()
