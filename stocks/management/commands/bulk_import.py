"""三家 API 批量导入. Usage: python3 manage.py bulk_import --source fmp --target all"""
import logging
import time

from django.core.management.base import BaseCommand
from rich.console import Console

logger = logging.getLogger(__name__)
console = Console()


_ALL_FMP_TABLES = [
    "us_stock_basic", "us_industry_class", "us_company_profile",
    "us_daily_price", "us_financial_data", "us_key_metric",
    "us_financial_score", "us_financial_growth", "us_enterprise_value",
    "us_owner_earnings", "us_revenue_segment", "us_dcf_valuation",
    "us_earnings_surprise", "us_eps_estimate",
    "us_analyst_recommendation", "us_price_target",
    "us_insider_trade", "us_insider_statistic",
    "us_corporate_action", "us_shares_float",
    "us_stock_peer", "us_esg_rating", "us_employee_count",
    "us_index_daily", "us_index_constituent",
    "us_commodity_price", "us_macro_indicator",
    "us_symbol_change", "us_delisted", "us_congress_trade",
]

_CLEAN_MAP = {
    ("fmp", "all"): _ALL_FMP_TABLES,
    ("fmp", "stock-list"): ["us_stock_basic", "us_industry_class"],
    ("fmp", "profiles"): ["us_industry_class", "us_company_profile"],
    ("fmp", "earnings"): ["us_earnings_surprise"],
    ("fmp", "estimates"): ["us_eps_estimate"],
    ("fmp", "financial-quarterly"): ["us_financial_data"],
    ("fmp", "metrics"): ["us_key_metric"],
    ("fmp", "ratios"): ["us_key_metric"],
    ("fmp", "prices"): ["us_daily_price"],
    ("fmp", "insider"): ["us_insider_trade"],
    ("fmp", "insider-stats"): ["us_insider_statistic"],
    ("fmp", "analyst-grades"): ["us_analyst_recommendation"],
    ("fmp", "price-targets"): ["us_price_target"],
    ("fmp", "dividends"): ["us_corporate_action"],
    ("fmp", "scores"): ["us_financial_score"],
    ("fmp", "growth"): ["us_financial_growth"],
    ("fmp", "ev"): ["us_enterprise_value"],
    ("fmp", "owner-earnings"): ["us_owner_earnings"],
    ("fmp", "dcf"): ["us_dcf_valuation"],
    ("fmp", "peers"): ["us_stock_peer"],
    ("fmp", "esg"): ["us_esg_rating"],
    ("fmp", "float"): ["us_shares_float"],
    ("fmp", "employee"): ["us_employee_count"],
    ("fmp", "index"): ["us_index_daily", "us_index_constituent"],
    ("fmp", "commodity"): ["us_commodity_price"],
    ("fmp", "macro"): ["us_macro_indicator"],
    ("fmp", "delisted"): ["us_delisted"],
    ("fmp", "symbol-changes"): ["us_symbol_change"],
    ("fmp", "congress"): ["us_congress_trade"],
    ("quiver", "all"): ["us_lobbying", "us_gov_contract"],
    ("quiver", "lobbying"): ["us_lobbying"],
    ("quiver", "gov-contracts"): ["us_gov_contract"],
}


def _clean_tables(source: str, target: str):
    tables = _CLEAN_MAP.get((source, target), [])
    if not tables:
        return
    from django.db import connection
    console.print(f"[yellow]清空表: {', '.join(tables)}[/yellow]")
    with connection.cursor() as cursor:
        for table in tables:
            try:
                cursor.execute(f'TRUNCATE TABLE "{table}" CASCADE')
                console.print(f"  [dim]✓ {table} 已清空[/dim]")
            except Exception as e:
                logger.warning(f"清空表 {table} 失败: {e}")
                console.print(f"  [red]✗ {table} 清空失败: {e}[/red]")


class Command(BaseCommand):
    help = "三家 API 批量导入（FMP/UW/Fiscal.ai/Quiver）"

    def add_arguments(self, parser):
        parser.add_argument("--source", default="fmp", help="数据源: fmp, quiver")
        parser.add_argument("--target", default="all", help="下载目标")
        parser.add_argument("--start-year", type=int, default=1995, help="起始年份")
        parser.add_argument("--clean", action="store_true", help="导入前清空对应表")

    def handle(self, *args, **opts):
        source = opts["source"]
        target = opts["target"]
        start_year = opts["start_year"]
        clean = opts["clean"]

        from stocks.services.downloaders.bulk import BulkDownloader
        dl = BulkDownloader()

        if clean:
            _clean_tables(source, target)

        dispatch = {
            ("fmp", "all"): lambda: dl.download_fmp_all(start_year),
            ("fmp", "stock-list"): dl.download_fmp_stock_list,
            ("fmp", "company-profiles"): dl.download_fmp_company_profiles,
            ("fmp", "prices"): lambda: dl.download_fmp_daily_prices(start_year),
            ("fmp", "financial-quarterly"): dl.download_fmp_financial_quarterly,
            ("fmp", "metrics"): dl.download_fmp_key_metrics,
            ("fmp", "ratios"): dl.download_fmp_ratios,
            ("fmp", "growth"): dl.download_fmp_financial_growth,
            ("fmp", "ev"): dl.download_fmp_enterprise_values,
            ("fmp", "owner-earnings"): dl.download_fmp_owner_earnings,
            ("fmp", "earnings"): dl.download_fmp_earnings_surprises,
            ("fmp", "estimates"): dl.download_fmp_eps_estimates,
            ("fmp", "insider"): dl.download_fmp_insider_trading,
            ("fmp", "insider-stats"): dl.download_fmp_insider_statistics,
            ("fmp", "analyst-grades"): dl.download_fmp_analyst_grades,
            ("fmp", "price-targets"): dl.download_fmp_price_targets,
            ("fmp", "dividends"): dl.download_fmp_dividends_splits,
            ("fmp", "scores"): dl.download_fmp_financial_scores,
            ("fmp", "float"): dl.download_fmp_shares_float,
            ("fmp", "esg"): dl.download_fmp_esg_ratings,
            ("fmp", "dcf"): dl.download_fmp_dcf_valuations,
            ("fmp", "peers"): dl.download_fmp_stock_peers,
            ("fmp", "employee"): dl.download_fmp_employee_count,
            ("fmp", "index"): lambda: dl.download_fmp_index_daily(start_year),
            ("fmp", "index-history"): dl.download_fmp_index_constituents_history,
            ("fmp", "commodity"): lambda: dl.download_fmp_commodity_prices(start_year),
            ("fmp", "macro"): dl.download_fmp_macro,
            ("fmp", "delisted"): dl.download_fmp_delisted_companies,
            ("fmp", "symbol-changes"): dl.download_fmp_symbol_changes,
            ("fmp", "congress"): dl.download_fmp_congress_trading,
            ("quiver", "all"): dl.download_quiver_all,
            ("quiver", "lobbying"): dl.download_quiver_lobbying,
            ("quiver", "gov-contracts"): dl.download_quiver_gov_contracts,
        }

        key = (source, target)
        if key not in dispatch:
            console.print(f"[red]未知组合: --source={source} --target={target}[/red]")
            return

        console.print(f"[cyan]批量导入 {source.upper()} {target}...[/cyan]")
        t0 = time.time()
        result = dispatch[key]()
        elapsed = time.time() - t0
        console.print(f"[green]完成[/green]，耗时 {elapsed:.1f}s，结果: {result}")
