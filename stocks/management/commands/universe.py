"""查看股票池。Usage: python3 manage.py universe --market us --date 2025-01-15"""

from datetime import datetime

from django.core.management.base import BaseCommand
from rich.console import Console
from rich.table import Table


class Command(BaseCommand):
    help = "查看可交易股票池"

    def add_arguments(self, parser):
        parser.add_argument("--market", default="us", choices=["cn", "us"])
        parser.add_argument("--date", default="", help="日期 (YYYY-MM-DD)")
        parser.add_argument("--limit", type=int, default=20, help="显示前 N 只")

    def handle(self, *args, **opts):
        console = Console()
        date = opts["date"] or datetime.now().strftime("%Y-%m-%d")
        market = opts["market"]
        limit = opts["limit"]

        if market == "us":
            from stocks.services.us_cleaner import get_us_clean_universe
            df = get_us_clean_universe(date)
            id_col, name_col, ind_col = "ticker", "name", "sector"
        else:
            from stocks.services.a_cleaner import get_clean_universe
            df = get_clean_universe(None, date)
            id_col, name_col, ind_col = "ts_code", "name", "industry_name"

        console.print(f"[cyan]{market.upper()} 股票池: {len(df)} 只 (date={date})[/cyan]")
        if df.empty:
            return

        t = Table()
        t.add_column(id_col, style="cyan")
        if name_col in df.columns:
            t.add_column("Name")
        if ind_col in df.columns:
            t.add_column("Industry")

        for _, row in df.head(limit).iterrows():
            cols = [str(row.get(id_col, ""))]
            if name_col in df.columns:
                cols.append(str(row.get(name_col, "")))
            if ind_col in df.columns:
                cols.append(str(row.get(ind_col, "")))
            t.add_row(*cols)

        console.print(t)
        if len(df) > limit:
            console.print(f"  ... 共 {len(df)} 只，已显示前 {limit} 只")
