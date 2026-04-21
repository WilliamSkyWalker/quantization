"""运行选股。Usage: python3 manage.py select --market us --date 2025-01-15"""

from datetime import datetime

from django.core.management.base import BaseCommand
from rich.console import Console
from rich.table import Table


class Command(BaseCommand):
    help = "运行多因子选股"

    def add_arguments(self, parser):
        parser.add_argument("--market", default="us", choices=["cn", "us"])
        parser.add_argument("--date", default="", help="日期 (YYYY-MM-DD)")
        parser.add_argument("--top", type=int, default=0, help="只显示前 N 只 (0=全部)")

    def handle(self, *args, **opts):
        import time
        console = Console()
        date = opts["date"] or datetime.now().strftime("%Y-%m-%d")
        market = opts["market"]
        top = opts["top"]

        console.print(f"[cyan]运行 {market.upper()} 选股: {date}[/cyan]")
        t0 = time.time()

        if market == "us":
            from backtest.services.us_strategy import USMultiFactorStrategy
            strategy = USMultiFactorStrategy(None)
            result = strategy.select_stocks(date)
            id_col = "ticker"
        else:
            from backtest.services.a_strategy import MultiFactorStrategy
            strategy = MultiFactorStrategy(None)
            result = strategy.select_stocks(date)
            id_col = "ts_code"

        elapsed = time.time() - t0

        # Handle both polars (US) and pandas (CN) results
        is_empty = (result is None or
                    (hasattr(result, 'is_empty') and result.is_empty()) or
                    (hasattr(result, 'empty') and result.empty))
        if is_empty:
            console.print(f"[yellow]无选股结果 ({elapsed:.1f}s)[/yellow]")
            return

        n_stocks = result.height if hasattr(result, 'height') else len(result)
        console.print(f"[green]选出 {n_stocks} 只股票 ({elapsed:.1f}s)[/green]\n")
        show = result.head(top) if top > 0 else result

        t = Table()
        t.add_column("#", justify="right")
        if "side" in result.columns:
            t.add_column("Side")
        t.add_column(id_col, style="cyan")
        t.add_column("Score", justify="right")
        t.add_column("Weight", justify="right")

        # Use iter_rows for polars, iterrows for pandas
        if hasattr(show, 'iter_rows'):
            rows_iter = enumerate(show.iter_rows(named=True), 1)
        else:
            rows_iter = ((i, row.to_dict()) for i, (_, row) in enumerate(show.iterrows(), 1))

        for i, row in rows_iter:
            cols = [str(i)]
            if "side" in result.columns:
                side = str(row.get("side", ""))
                cols.append(f"[green]{side}[/green]" if side == "LONG" else f"[red]{side}[/red]")
            cols.append(str(row[id_col]))
            cols.append(f"{row['score']:.3f}")
            w = row['weight']
            cols.append(f"{w*100:+.1f}%" if w < 0 else f"{w*100:.1f}%")
            t.add_row(*cols)

        console.print(t)

        if "side" in result.columns:
            if hasattr(result, 'get_column'):
                # polars
                import polars as pl
                weights = result.get_column("weight")
                n_long = (weights > 0).sum()
                n_short = (weights < 0).sum()
                long_total = result.filter(pl.col("weight") > 0).get_column("weight").sum()
                short_total = result.filter(pl.col("weight") < 0).get_column("weight").sum()
            else:
                # pandas
                n_long = (result["weight"] > 0).sum()
                n_short = (result["weight"] < 0).sum()
                long_total = result.loc[result["weight"] > 0, "weight"].sum()
                short_total = result.loc[result["weight"] < 0, "weight"].sum()
            console.print(
                f"  [green]Long: {n_long} ({long_total:.1%})[/green]  "
                f"[red]Short: {n_short} ({short_total:+.1%})[/red]  "
                f"Net: {long_total + short_total:.1%}"
            )
