"""Polymarket 历史数据下载。Usage: python3 manage.py polymarket --min-volume 1000000"""

from django.core.management.base import BaseCommand
from rich.console import Console


class Command(BaseCommand):
    help = "从 Gamma + CLOB 下载 Polymarket 已结算市场历史"

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=0, help="最大下载市场数, 0=全部")
        parser.add_argument("--min-volume", type=int, default=0, help="最低交易量过滤")
        parser.add_argument("--fidelity", type=int, default=60, help="价格快照粒度（分钟）")
        parser.add_argument("--skip-existing", action="store_true", default=True)
        parser.add_argument("--concurrency", type=int, default=50, help="并发下载线程数")
        parser.add_argument("--discover-only", action="store_true", help="只发现市场，不下价格")

    def handle(self, *args, **opts):
        import time
        console = Console()

        from sentiment.services.polymarket.history import PolymarketHistoryDownloader
        dl = PolymarketHistoryDownloader()

        task_id = f"cli_polymarket_{int(time.time())}"
        console.print(f"[cyan]发现已结算市场 (limit={opts['limit']}, min_volume={opts['min_volume']})...[/cyan]")
        markets = dl.discover_resolved_markets(task_id, limit=opts["limit"], min_volume=opts["min_volume"])
        console.print(f"[green]发现 {len(markets)} 个市场[/green]")

        if opts["discover_only"]:
            return

        console.print(f"[cyan]批量下载历史价格...[/cyan]")
        t0 = time.time()
        result = dl.download_batch(
            task_id, markets=markets, limit=opts["limit"],
            fidelity=opts["fidelity"], skip_existing=opts["skip_existing"],
            concurrency=opts["concurrency"],
        )
        console.print(f"[green]完成[/green]，耗时 {time.time()-t0:.1f}s，结果: {result}")
