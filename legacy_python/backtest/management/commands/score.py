"""查看单只股票得分。Usage: python3 manage.py score AAPL --date 2025-01-15"""

from datetime import datetime

from django.core.management.base import BaseCommand
from rich.console import Console


class Command(BaseCommand):
    help = "查看单只股票的综合得分和因子明细"

    def add_arguments(self, parser):
        parser.add_argument("stock", help="股票代码 (如 AAPL 或 000001.SZ)")
        parser.add_argument("--date", default="", help="日期 (YYYY-MM-DD)")
        parser.add_argument("--market", default="", help="市场: cn/us (自动检测)")

    def handle(self, *args, **opts):
        import time
        console = Console()
        stock = opts["stock"]
        date = opts["date"] or datetime.now().strftime("%Y-%m-%d")
        market = opts["market"] or ("cn" if "." in stock else "us")

        console.print(f"[cyan]计算 {stock} 得分: {date} ({market.upper()})[/cyan]")
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

        if result is None or result.empty:
            console.print(f"[yellow]无选股结果 ({elapsed:.1f}s)[/yellow]")
            return

        row = result[result[id_col] == stock]
        if row.empty:
            console.print(f"[yellow]{stock} 未入选 Top-N ({elapsed:.1f}s)[/yellow]")
            console.print(f"  入选 {len(result)} 只: {', '.join(result[id_col].head(5).tolist())}...")
        else:
            r = row.iloc[0]
            rank = (result["score"] >= r["score"]).sum()
            console.print(f"\n[green]{stock} 入选! ({elapsed:.1f}s)[/green]")
            console.print(f"  得分: {r['score']:.4f} (排名 {rank}/{len(result)})")
            console.print(f"  权重: {r['weight']*100:.2f}%")
