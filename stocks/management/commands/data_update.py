"""增量更新数据. Usage: python3 manage.py data_update --market us"""
import logging
import time

from django.core.management.base import BaseCommand
from rich.console import Console

logger = logging.getLogger(__name__)
console = Console()


class Command(BaseCommand):
    help = "增量更新数据"

    def add_arguments(self, parser):
        parser.add_argument("--market", default="cn", choices=["cn", "us"], help="市场")
        parser.add_argument("--old-source", action="store_true", help="使用旧数据源 (yfinance)")

    def handle(self, *args, **opts):
        market = opts["market"]
        old_source = opts["old_source"]

        from services.data.database import DatabaseManager
        db = DatabaseManager()
        db.init_tables()

        if market == "us" and not old_source:
            from stocks.services.downloaders.bulk import BulkDownloader
            from stocks.services.downloaders.fred import FREDDownloader
            dl = BulkDownloader(incremental=True)
            console.print("[cyan]增量更新美股数据（六源：FMP/UW/Fiscal/Quiver/AV/FRED）...[/cyan]")
            t0 = time.time()
            current_year = time.localtime().tm_year
            results = {}

            def _try(name, fn):
                try:
                    console.print(f"  [dim]FMP {name}...[/dim]")
                    results[name] = fn()
                except Exception as e:
                    logger.warning(f"data_update: FMP {name} 跳过: {e}")
                    console.print(f"[yellow]FMP {name} 跳过: {e}[/yellow]")

            _try("stock_list", dl.download_fmp_stock_list)
            _try("prices", lambda: dl.download_fmp_daily_prices(incremental=True))
            _try("financial", lambda: dl.download_fmp_financial_quarterly(limit=8))
            _try("key_metrics", dl.download_fmp_key_metrics)
            _try("ratios", dl.download_fmp_ratios)
            _try("earnings", dl.download_fmp_earnings_surprises)
            _try("estimates", dl.download_fmp_eps_estimates)
            _try("analyst_grades", dl.download_fmp_analyst_grades)
            _try("price_targets", dl.download_fmp_price_targets)
            _try("insider", dl.download_fmp_insider_trading)
            _try("company_profiles", dl.download_fmp_company_profiles)
            _try("dividends", dl.download_fmp_dividends_splits)
            _try("financial_scores", dl.download_fmp_financial_scores)
            _try("shares_float", dl.download_fmp_shares_float)
            _try("dcf", dl.download_fmp_dcf_valuations)
            _try("peers", dl.download_fmp_stock_peers)
            _try("index", lambda: dl.download_fmp_index_daily(current_year))

            for label, fn in [
                ("Unusual Whales", dl.download_uw_all),
                ("Fiscal.ai", dl.download_fiscal_all),
                ("Quiver", dl.download_quiver_all),
            ]:
                try:
                    console.print(f"  [dim]{label}...[/dim]")
                    results[label] = fn()
                except Exception as e:
                    logger.warning(f"data_update: {label} 跳过: {e}")
                    console.print(f"[yellow]{label} 跳过: {e}[/yellow]")

            try:
                console.print("  [dim]Alpha Vantage (新闻/期权)...[/dim]")
                results["av_options"] = dl.download_av_options_snapshot()
                results["av_news"] = dl.download_av_news_sentiment()
            except Exception as e:
                logger.warning(f"data_update: AV 跳过: {e}")
                console.print(f"[yellow]AV 跳过: {e}[/yellow]")

            try:
                console.print("  [dim]FRED 宏观...[/dim]")
                fred = FREDDownloader(db)
                results["macro"] = fred.update()
            except Exception as e:
                logger.warning(f"data_update: FRED 跳过: {e}")
                console.print(f"[yellow]FRED 跳过: {e}[/yellow]")

            elapsed = time.time() - t0
            console.print(f"[green]完成[/green] {elapsed:.1f}s — {results}")

        elif market == "us":
            from stocks.services.downloaders.fmp import FMPDownloader
            from stocks.services.downloaders.fred import FREDDownloader
            dl = FMPDownloader(db)
            console.print("[cyan]增量更新美股数据 (yfinance, old-source)...[/cyan]")
            t0 = time.time()
            n1 = dl.update_daily_prices()
            n2 = dl.update_financial_data()
            n3 = dl.update_index_daily()
            n4 = dl.update_analyst_recommendations()
            try:
                n6 = dl.update_earnings_surprises()
            except Exception as e:
                logger.warning(f"data_update: Earnings surprises 跳过: {e}")
                n6 = 0
            try:
                n7 = dl.update_eps_estimates()
            except Exception as e:
                logger.warning(f"data_update: EPS estimates 跳过: {e}")
                n7 = 0
            try:
                fred = FREDDownloader(db)
                n5 = fred.update()
            except Exception as e:
                logger.warning(f"data_update: FRED 跳过: {e}")
                n5 = 0
            elapsed = time.time() - t0
            console.print(f"[green]完成[/green] {elapsed:.1f}s — daily:{n1}, fin:{n2}, idx:{n3}, analyst:{n4}, earnings:{n6}, estimates:{n7}, macro:{n5}")
        else:
            from services.data.downloader import TushareDownloader
            from services.data.updater import FinancialUpdater
            dl = TushareDownloader(db)
            updater = FinancialUpdater(db)
            console.print("[cyan]增量更新 A股数据...[/cyan]")
            t0 = time.time()
            dl.download_stock_list()
            n1 = dl.update_daily_prices()
            dl.update_index_daily("000300.SH")
            n2 = updater.update_financial_data()
            elapsed = time.time() - t0
            console.print(f"[green]完成[/green] {elapsed:.1f}s — daily:{n1}, financial:{n2}")
