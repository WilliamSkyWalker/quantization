"""批量导入. Usage: python3 manage.py bulk_import --source fmp/tushare/akshare --target all"""
import logging
import time

from django.core.management.base import BaseCommand
from rich.console import Console
from services.config import DATA_START_DATE

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
    "us_price_target_detail",
]

_ALL_TUSHARE_TABLES = [
    "a_stock_basic", "a_trade_cal", "a_daily_price", "a_index_daily",
    "a_financial_income", "a_financial_balance",
    "a_financial_cashflow", "a_financial_indicator",
    "a_industry_class", "a_macro_indicator", "a_commodity_price",
]

_ALL_AKSHARE_TABLES = ["a_research_report", "a_insider_transaction"]

# 这些 FMP 端点只返回"当前快照"，不是历史时序——单跑只会拿最新 1 条/票
# 如要积累时序，每日/每周 cron 跑形成历史
_FMP_SNAPSHOT_ONLY_TARGETS = {
    "dcf": "us_dcf_valuation — DCF 估值（当前一日快照）",
    "scores": "us_financial_score — Piotroski/Altman 当前快照",
    "float": "us_shares_float — 流通股本当前快照",
    "peers": "us_stock_peer — 同业列表当前快照（不变化）",
    "price-targets": "us_price_target — 分析师目标价当前快照",
    "esg": "us_esg_rating — ESG 评分（年频，但单跑只拿最新）",
}

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
    ("fmp", "price-target-detail"): ["us_price_target_detail"],
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
    ("fmp", "press-releases"): ["us_press_release"],
    ("fmp", "sec-filings"): ["us_sec_filing"],
    ("fmp", "revenue-segments"): ["us_revenue_segment"],
    ("quiver", "13f-holdings"): ["us_institutional_holder"],
    ("quiver", "all"): ["us_lobbying", "us_gov_contract", "us_dark_pool_volume"],
    ("quiver", "lobbying"): ["us_lobbying"],
    ("quiver", "gov-contracts"): ["us_gov_contract"],
    ("quiver", "dark-pool"): ["us_dark_pool_volume"],
    # Tushare (A 股)
    ("tushare", "all"): _ALL_TUSHARE_TABLES,
    ("tushare", "stock-list"): ["a_stock_basic"],
    ("tushare", "trade-cal"): ["a_trade_cal"],
    ("tushare", "prices"): ["a_daily_price"],
    ("tushare", "income"): ["a_financial_income"],
    ("tushare", "balancesheet"): ["a_financial_balance"],
    ("tushare", "cashflow"): ["a_financial_cashflow"],
    ("tushare", "fina-indicator"): ["a_financial_indicator"],
    ("tushare", "industry"): ["a_industry_class"],
    ("tushare", "index"): ["a_index_daily"],
    ("tushare", "commodity"): ["a_commodity_price"],
    ("tushare", "macro"): ["a_macro_indicator"],
    # AkShare (A 股辅助)
    ("akshare", "all"): _ALL_AKSHARE_TABLES,
    ("akshare", "research-report"): ["a_research_report"],
    ("akshare", "insider"): ["a_insider_transaction"],
}


def _clean_tables(source: str, target: str):
    tables = _CLEAN_MAP.get((source, target), [])
    if not tables:
        return
    from django.db import connection
    from stocks.models import ImportProgress
    console.print(f"[yellow]清空表: {', '.join(tables)}[/yellow]")
    with connection.cursor() as cursor:
        for table in tables:
            try:
                cursor.execute(f'TRUNCATE TABLE "{table}" CASCADE')
                console.print(f"  [dim]✓ {table} 已清空[/dim]")
            except Exception as e:
                logger.warning(f"清空表 {table} 失败: {e}")
                console.print(f"  [red]✗ {table} 清空失败: {e}[/red]")
                continue
            # 同步清除 import_progress 中该表的断点标记，否则下次 _skip_done_tickers 会全部跳过
            try:
                qs = ImportProgress.objects.filter(table_name=table)
                n_marks = qs.count()
                if n_marks:
                    qs.delete()
                    console.print(f"    [dim]+ import_progress 清除 {n_marks} 条 ticker 标记[/dim]")
            except Exception as e:
                logger.warning(f"清除 import_progress({table}) 失败: {e}")


class Command(BaseCommand):
    help = "三家 API 批量导入（FMP/UW/Fiscal.ai/Quiver）"

    def add_arguments(self, parser):
        parser.add_argument("--source", default="fmp", help="数据源: fmp, quiver, tushare, akshare")
        parser.add_argument("--target", default="all", help="下载目标")
        parser.add_argument("--start-year", type=int, default=1995, help="[FMP] 起始年份")
        parser.add_argument("--start-date", default=None, help="[Tushare] 起始日期 YYYYMMDD")
        parser.add_argument("--clean", action="store_true", help="导入前清空对应表")

    def handle(self, *args, **opts):
        source = opts["source"]
        target = opts["target"]
        start_year = opts["start_year"]
        start_date = opts["start_date"]
        clean = opts["clean"]

        # Snapshot-only 端点提醒
        if source == "fmp" and target in _FMP_SNAPSHOT_ONLY_TARGETS:
            console.print(
                f"[yellow]⚠ {target} 是 snapshot-only 端点：{_FMP_SNAPSHOT_ONLY_TARGETS[target]}[/yellow]\n"
                f"[yellow]  本次只会拿今日 1 条/票。要积累时序请加 cron：[/yellow]\n"
                f"[yellow]  0 6 * * 1 cd $REPO && python3 manage.py bulk_import --source fmp --target {target}[/yellow]"
            )

        if clean:
            _clean_tables(source, target)

        # === A 股 Tushare / AkShare 分支（风格对齐美股 BulkDownloader） ===
        if source in ("tushare", "akshare"):
            from stocks.services.downloaders.a_bulk import AShareBulkDownloader
            dl = AShareBulkDownloader(incremental=False, start_date=start_date or DATA_START_DATE)
            dispatch_t = {
                ("tushare", "all"): lambda: dl.download_tushare_all(start_date),
                ("tushare", "stock-list"): dl.download_tushare_stock_list,
                ("tushare", "trade-cal"): lambda: dl.download_tushare_trade_cal(start_date),
                ("tushare", "prices"): lambda: dl.download_tushare_daily_prices(start_date),
                ("tushare", "index"): lambda: dl.download_tushare_index(start_date),
                ("tushare", "income"): dl.download_tushare_income,
                ("tushare", "balancesheet"): dl.download_tushare_balancesheet,
                ("tushare", "cashflow"): dl.download_tushare_cashflow,
                ("tushare", "fina-indicator"): dl.download_tushare_fina_indicator,
                ("tushare", "industry"): dl.download_tushare_industry,
                ("tushare", "commodity"): lambda: dl.download_tushare_commodity(start_date),
                ("tushare", "macro"): lambda: dl.download_tushare_macro(start_date),
                ("akshare", "all"): dl.download_akshare_all,
                ("akshare", "research-report"): dl.download_akshare_research_reports,
                ("akshare", "insider"): dl.download_akshare_insider,
            }
            key = (source, target)
            if key not in dispatch_t:
                console.print(f"[red]未知组合: --source={source} --target={target}[/red]")
                return
            console.print(f"[cyan]批量导入 {source.upper()} {target}...[/cyan]")
            t0 = time.time()
            result = dispatch_t[key]()
            console.print(f"[green]完成[/green]，耗时 {time.time()-t0:.1f}s，结果: {result}")
            return


        # === 美股 FMP / Quiver 分支 ===
        from stocks.services.downloaders.us_bulk import BulkDownloader
        dl = BulkDownloader()

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
            ("fmp", "price-target-detail"): dl.download_fmp_price_target_detail,
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
            ("fmp", "press-releases"): dl.download_fmp_press_releases,
            ("fmp", "sec-filings"): dl.download_fmp_sec_filings,
            ("fmp", "revenue-segments"): dl.download_fmp_revenue_segments,
            ("quiver", "all"): dl.download_quiver_all,
            ("quiver", "13f-holdings"): dl.download_quiver_institutional_holders,
            ("quiver", "dark-pool"): dl.download_quiver_dark_pool,
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

    # ----------------------------------------------------------
    # A 股 Tushare / AkShare 分发
