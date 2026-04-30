"""查看数据表状态。Usage: python3 manage.py db_status"""

from django.core.management.base import BaseCommand
from django.db import connection
from rich.console import Console
from rich.table import Table


class Command(BaseCommand):
    help = "查看所有数据表的行数和日期范围"

    def handle(self, *args, **opts):
        console = Console()
        tables = [
            ("a_stock_basic", "ts_code", None),
            ("a_daily_price", "ts_code", "trade_date"),
            ("a_financial_income", "ts_code", "end_date"),
            ("a_industry_class", "ts_code", None),
            ("a_index_daily", None, "trade_date"),
            ("us_stock_basic", "ticker", None),
            ("us_daily_price", "ticker", "trade_date"),
            ("us_financial_data", "ticker", "date"),
            ("us_industry_class", "ticker", None),
            ("us_index_daily", None, "trade_date"),
            ("us_macro_indicator", None, "report_date"),
            ("us_analyst_recommendation", "ticker", "date"),
            ("us_corporate_action", "ticker", "date"),
        ]

        t = Table(title="数据表状态")
        t.add_column("Table", style="cyan")
        t.add_column("Rows", justify="right")
        t.add_column("Tickers", justify="right")
        t.add_column("Date Range")

        with connection.cursor() as cursor:
            for table, ticker_col, date_col in tables:
                try:
                    cursor.execute(f'SELECT COUNT(*) FROM "{table}"')
                    cnt = cursor.fetchone()[0]

                    tickers = "-"
                    if ticker_col and cnt > 0:
                        cursor.execute(f'SELECT COUNT(DISTINCT "{ticker_col}") FROM "{table}"')
                        tickers = str(cursor.fetchone()[0])

                    dates = "-"
                    if date_col and cnt > 0:
                        cursor.execute(f'SELECT MIN("{date_col}"), MAX("{date_col}") FROM "{table}"')
                        mn, mx = cursor.fetchone()
                        dates = f"{mn} ~ {mx}"

                    t.add_row(table, f"{cnt:,}", tickers, dates)
                except Exception as e:
                    t.add_row(table, "[red]ERROR[/red]", "-", str(e)[:40])

        console.print(t)
