#!/usr/bin/env python3
"""
量化系统 CLI — 薄壳调用 service 层

用法:
    python3 cli.py --help
    python3 cli.py db status
    python3 cli.py select --market us --date 2025-01-15
    python3 cli.py backtest --market us --start 2020-01-01 --end 2025-12-31
"""

import logging
import os
import sys
import time
from pathlib import Path
from typing import Optional

# 确保项目根目录在 sys.path
PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT))

# Django 最小化初始化（仅加载 settings，不启动 ASGI/WSGI）
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "core.settings")
import django
django.setup()

import typer
from rich.console import Console
from rich.table import Table

# 配置 logging 输出到 stdout（避免和 tqdm stderr 冲突导致换行错乱）
import tqdm
class _TqdmLoggingHandler(logging.StreamHandler):
    """让 logger 通过 tqdm.write 输出，避免和进度条交叉。"""
    def emit(self, record):
        try:
            msg = self.format(record)
            tqdm.tqdm.write(msg)
        except Exception:
            self.handleError(record)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[_TqdmLoggingHandler()],
)

logger = logging.getLogger(__name__)

console = Console()
app = typer.Typer(help="量化交易系统 CLI", no_args_is_help=True)


# ============================================================
# 公共工具
# ============================================================

def _get_db():
    from services.data.database import DatabaseManager
    db = DatabaseManager()
    db.init_tables()
    return db


# ============================================================
# db: 数据库相关
# ============================================================

db_app = typer.Typer(help="数据库操作")
app.add_typer(db_app, name="db")


@db_app.command("status")
def db_status():
    """查看所有数据表的行数和日期范围"""
    db = _get_db()
    tables = [
        # A股
        ("stock_basic", "ts_code", None),
        ("daily_price", "ts_code", "trade_date"),
        ("financial_data", "ts_code", "end_date"),
        ("industry_class", "ts_code", None),
        ("index_daily", None, "trade_date"),
        # 美股
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

    for table, ticker_col, date_col in tables:
        try:
            row = db.query(f"SELECT COUNT(*) as cnt FROM {table}").iloc[0]
            cnt = int(row["cnt"])

            tickers = "-"
            if ticker_col and cnt > 0:
                r = db.query(f"SELECT COUNT(DISTINCT {ticker_col}) as n FROM {table}")
                tickers = str(int(r.iloc[0]["n"]))

            dates = "-"
            if date_col and cnt > 0:
                r = db.query(f"SELECT MIN({date_col}) as mn, MAX({date_col}) as mx FROM {table}")
                dates = f"{r.iloc[0]['mn']} ~ {r.iloc[0]['mx']}"

            t.add_row(table, f"{cnt:,}", tickers, dates)
        except Exception as e:
            logger.warning(f"db_status: 查询表 {table} 失败: {e}")
            t.add_row(table, "[red]ERROR[/red]", "-", str(e)[:40])

    console.print(t)


@db_app.command("init")
def db_init():
    """初始化/迁移数据库表"""
    db = _get_db()
    console.print("[green]数据库表初始化完成[/green]")


# ============================================================
# data: 数据下载/更新
# ============================================================

data_app = typer.Typer(help="数据下载和更新")
app.add_typer(data_app, name="data")


def _clean_tables_for_import(db, source: str, target: str):
    """导入前清空对应表。"""
    # 所有 FMP 美股表（含新增）
    _ALL_FMP_TABLES = [
        "us_stock_basic", "us_industry_class", "us_company_profile",
        "us_daily_price",
        "us_financial_data", "us_key_metric",
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
    table_map = {
        ("fmp", "all"): _ALL_FMP_TABLES,
        ("fmp", "stock-list"): ["us_stock_basic", "us_industry_class"],
        ("fmp", "profiles"): ["us_industry_class", "us_company_profile"],
        ("fmp", "earnings"): ["us_earnings_surprise"],
        ("fmp", "estimates"): ["us_eps_estimate"],
        ("fmp", "income"): ["us_financial_data"],
        ("fmp", "financial-quarterly"): ["us_financial_data"],
        ("fmp", "metrics"): ["us_key_metric"],
        ("fmp", "ratios"): ["us_key_metric"],
        ("fmp", "prices"): ["us_daily_price"],
        # market-cap 已废弃（数据源切到 us_enterprise_value）
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
        ("uw", "all"): ["us_options_flow", "us_dark_pool", "us_congress_trade"],
        ("uw", "options"): ["us_options_flow"],
        ("uw", "darkpool"): ["us_dark_pool"],
        ("uw", "congress"): ["us_congress_trade"],
        ("uw", "news"): [],
        ("fiscal", "all"): ["us_daily_ratio"],
        ("fiscal", "ratios"): ["us_daily_ratio"],
        ("quiver", "all"): ["us_lobbying", "us_gov_contract"],
        ("quiver", "lobbying"): ["us_lobbying"],
        ("quiver", "gov-contracts"): ["us_gov_contract"],
        ("all", "all"): _ALL_FMP_TABLES + [
            "us_options_flow", "us_dark_pool", "us_daily_ratio",
        ],
    }
    tables = table_map.get((source, target), [])
    if not tables:
        return

    console.print(f"[yellow]清空表: {', '.join(tables)}[/yellow]")
    from sqlalchemy import text as sa_text
    with db.get_session() as session:
        for table in tables:
            try:
                session.execute(sa_text(f'TRUNCATE TABLE "{table}" CASCADE'))
                console.print(f"  [dim]✓ {table} 已清空[/dim]")
            except Exception as e:
                logger.warning(f"_clean_tables_for_import: 清空表 {table} 失败: {e}")
                console.print(f"  [red]✗ {table} 清空失败: {e}[/red]")
        # 清 news 按 source 过滤
        if source == "uw" and target in ("all", "news"):
            try:
                session.execute(sa_text("DELETE FROM us_news WHERE source = 'uw'"))
                console.print("  [dim]✓ us_news (source=uw) 已清空[/dim]")
            except Exception as e:
                logger.warning(f"_clean_tables_for_import: 清空 us_news 失败: {e}")
        session.commit()


@data_app.command("bulk-import")
def data_bulk_import(
    source: str = typer.Option("fmp", help="数据源: fmp, uw, fiscal, quiver, all"),
    target: str = typer.Option("all", help="下载目标 (fmp: all/stock-list/earnings/estimates/income/metrics/ratios/prices/profiles/insider/dividends; uw: all/options/darkpool/congress/news; fiscal: all/ratios)"),
    start_year: int = typer.Option(1995, help="起始年份 (FMP bulk)"),
    clean: bool = typer.Option(False, "--clean", help="导入前清空对应表（全量替换旧数据）"),
):
    """三家 API 批量导入（FMP/UW/Fiscal.ai）"""
    db = _get_db()
    from services.data.bulk_downloader import BulkDownloader
    dl = BulkDownloader()

    if clean:
        _clean_tables_for_import(db, source, target)

    dispatch = {
        # FMP
        ("fmp", "all"): lambda: dl.download_fmp_all(start_year),
        ("fmp", "stock-list"): dl.download_fmp_stock_list,
        ("fmp", "company-profiles"): dl.download_fmp_company_profiles,
        ("fmp", "prices"): lambda: dl.download_fmp_daily_prices(start_year),
        # ("fmp", "market-cap") 已废弃 —— us_enterprise_value 已含全历史季度市值
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
        # Quiver
        ("quiver", "all"): dl.download_quiver_all,
        ("quiver", "lobbying"): dl.download_quiver_lobbying,
        ("quiver", "gov-contracts"): dl.download_quiver_gov_contracts,
        # TODO: UW/Fiscal/AV 需要迁移到新架构
    }

    key = (source, target)
    if key not in dispatch:
        console.print(f"[red]未知组合: --source={source} --target={target}[/red]")
        console.print(f"可选 source: fmp, uw, fiscal, all")
        raise typer.Exit(1)

    console.print(f"[cyan]批量导入 {source.upper()} {target}...[/cyan]")
    t0 = time.time()
    result = dispatch[key]()
    elapsed = time.time() - t0
    console.print(f"[green]完成[/green]，耗时 {elapsed:.1f}s，结果: {result}")


# ============================================================
# polymarket: 历史数据下载
# ============================================================

polymarket_app = typer.Typer(help="Polymarket 历史数据")
app.add_typer(polymarket_app, name="polymarket")


@polymarket_app.command("history")
def polymarket_history(
    limit: int = typer.Option(0, help="最大下载市场数, 0=全部"),
    min_volume: int = typer.Option(0, help="最低交易量过滤"),
    fidelity: int = typer.Option(60, help="价格快照粒度（分钟）"),
    skip_existing: bool = typer.Option(True, help="跳过已有快照的市场"),
    concurrency: int = typer.Option(50, help="并发下载线程数"),
    discover_only: bool = typer.Option(False, help="只发现市场，不下价格"),
):
    """从 Gamma + CLOB 下载 Polymarket 已结算市场历史（events + price snapshots）"""
    from services.polymarket.history import PolymarketHistoryDownloader
    dl = PolymarketHistoryDownloader()

    task_id = f"cli_polymarket_{int(time.time())}"
    console.print(f"[cyan]发现已结算市场 (limit={limit}, min_volume={min_volume})...[/cyan]")
    markets = dl.discover_resolved_markets(task_id, limit=limit, min_volume=min_volume)
    console.print(f"[green]发现 {len(markets)} 个市场[/green]")

    if discover_only:
        return

    console.print(f"[cyan]批量下载历史价格 (fidelity={fidelity}min, concurrency={concurrency})...[/cyan]")
    t0 = time.time()
    result = dl.download_batch(
        task_id, markets=markets, limit=limit, fidelity=fidelity,
        skip_existing=skip_existing, concurrency=concurrency,
    )
    elapsed = time.time() - t0
    console.print(f"[green]完成[/green]，耗时 {elapsed:.1f}s，结果: {result}")


@data_app.command("download")
def data_download(
    market: str = typer.Option("cn", help="市场: cn (A股) 或 us (美股)"),
    target: str = typer.Option("all", help="下载目标: all, list, daily, financial, industry, index, macro, analyst, commodity, research"),
    old_source: bool = typer.Option(False, "--old-source", help="使用旧数据源 (yfinance)"),
):
    """下载数据（默认使用新 FMP API，--old-source 回退到 yfinance）"""
    db = _get_db()

    if market == "us" and not old_source:
        from services.data.bulk_downloader import BulkDownloader
        dl = BulkDownloader()
        dispatch = {
            "all": lambda: dl.download_fmp_all_bulk(1995),
            "list": dl.download_fmp_stock_list,
            "daily": lambda: dl._download_fmp_prices_per_ticker(2015, 2026),
            "financial": lambda: dl.download_fmp_income_statement_bulk(),
            "industry": dl.download_fmp_profiles,
            "analyst": lambda: dl.download_fmp_eps_estimates_bulk(),
            "earnings": lambda: dl.download_fmp_earnings_surprises_bulk(),
            "insider": dl.download_fmp_insider_trading,
            "macro": lambda: _download_fred(db),
        }
    elif market == "us":
        from services.data.fmp_downloader import FMPDownloader
        dl = FMPDownloader(db)
        dispatch = {
            "all": dl.download_all,
            "list": dl.download_stock_list,
            "daily": dl.download_daily_prices,
            "financial": dl.download_financial_data,
            "industry": dl.download_industry_class,
            "index": dl.download_index_daily,
            "analyst": dl.download_analyst_recommendations,
            "commodity": lambda: dl.download_commodity_prices(),
            "corporate": dl.download_corporate_actions,
            "earnings": dl.download_earnings_surprises,
            "estimates": dl.download_eps_estimates,
            "macro": lambda: _download_fred(db),
            "simfin": lambda: _download_simfin(db),
            "edgar": lambda: _download_edgar(db),
            "historical": lambda: _download_historical(db),
        }
    else:
        from services.data.downloader import TushareDownloader
        dl = TushareDownloader(db)
        dispatch = {
            "all": lambda: (dl.download_stock_list(), dl.download_daily_prices()),
            "list": dl.download_stock_list,
            "daily": dl.download_daily_prices,
            "index": lambda: dl.download_index_daily("000300.SH"),
        }

    if target not in dispatch:
        console.print(f"[red]未知目标: {target}[/red]，可选: {', '.join(dispatch.keys())}")
        raise typer.Exit(1)

    def _download_fred(db_inst):
        from services.data.fred_downloader import FREDDownloader
        fred = FREDDownloader(db_inst)
        return fred.download_all()

    def _download_simfin(db_inst):
        from services.data.simfin_downloader import SimFinDownloader
        dl_sf = SimFinDownloader(db_inst)
        return dl_sf.download_financials(force=True)

    def _download_edgar(db_inst):
        from services.data.edgar_downloader import EdgarDownloader
        dl_ed = EdgarDownloader(db_inst)
        return dl_ed.download_financials()

    def _download_historical(db_inst):
        from services.data.historical_universe import (
            build_historical_universe, download_historical_prices, download_historical_financials
        )
        n1 = build_historical_universe(db_inst)
        console.print(f"  [cyan]Added {n1} historical tickers, downloading prices...[/cyan]")
        n2 = download_historical_prices(db_inst)
        console.print(f"  [cyan]Prices: {n2}, downloading EDGAR financials...[/cyan]")
        n3 = download_historical_financials(db_inst)
        return f"tickers={n1}, prices={n2}, financials={n3}"

    console.print(f"[cyan]下载 {market.upper()} {target}...[/cyan]")
    t0 = time.time()
    result = dispatch[target]()
    elapsed = time.time() - t0
    console.print(f"[green]完成[/green]，耗时 {elapsed:.1f}s，结果: {result}")


@data_app.command("update")
def data_update(
    market: str = typer.Option("cn", help="市场: cn 或 us"),
    old_source: bool = typer.Option(False, "--old-source", help="使用旧数据源 (yfinance)"),
):
    """增量更新数据"""
    db = _get_db()

    if market == "us" and not old_source:
        # 六源增量更新
        from services.data.bulk_downloader import BulkDownloader
        from services.data.fred_downloader import FREDDownloader
        dl = BulkDownloader()
        console.print("[cyan]增量更新美股数据（六源：FMP/UW/Fiscal/Quiver/AV/FRED）...[/cyan]")
        t0 = time.time()
        current_year = time.localtime().tm_year
        results = {}

        # --- FMP ---
        def _try(name, fn):
            try:
                console.print(f"  [dim]FMP {name}...[/dim]")
                results[name] = fn()
            except Exception as e:
                logger.warning(f"data_update: FMP {name} 跳过: {e}")
                console.print(f"[yellow]FMP {name} 跳过: {e}[/yellow]")

        _try("stock_list", dl.download_fmp_stock_list)
        _try("prices", lambda: dl.download_fmp_daily_prices(current_year))
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

        # --- UW ---
        try:
            console.print("  [dim]Unusual Whales...[/dim]")
            results["uw"] = dl.download_uw_all()
        except Exception as e:
            logger.warning(f"data_update: UW 跳过: {e}")
            console.print(f"[yellow]UW 跳过: {e}[/yellow]")

        # --- Fiscal.ai ---
        try:
            console.print("  [dim]Fiscal.ai...[/dim]")
            results["fiscal"] = dl.download_fiscal_all()
        except Exception as e:
            logger.warning(f"data_update: Fiscal 跳过: {e}")
            console.print(f"[yellow]Fiscal 跳过: {e}[/yellow]")

        # --- Quiver ---
        try:
            console.print("  [dim]Quiver (lobbying/gov/wsb)...[/dim]")
            results["quiver"] = dl.download_quiver_all()
        except Exception as e:
            logger.warning(f"data_update: Quiver 跳过: {e}")
            console.print(f"[yellow]Quiver 跳过: {e}[/yellow]")

        # --- Alpha Vantage ---
        try:
            console.print("  [dim]Alpha Vantage (新闻/期权)...[/dim]")
            results["av_options"] = dl.download_av_options_snapshot()
            results["av_news"] = dl.download_av_news_sentiment()
        except Exception as e:
            logger.warning(f"data_update: AV 跳过: {e}")
            console.print(f"[yellow]AV 跳过: {e}[/yellow]")

        # --- FRED ---
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
        # 老数据源 (yfinance)
        from services.data.fmp_downloader import FMPDownloader
        from services.data.fred_downloader import FREDDownloader
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
            logger.warning(f"data_update: Earnings surprises 更新跳过: {e}")
            console.print(f"[yellow]Earnings surprises 更新跳过: {e}[/yellow]")
            n6 = 0
        try:
            n7 = dl.update_eps_estimates()
        except Exception as e:
            logger.warning(f"data_update: EPS estimates 更新跳过: {e}")
            console.print(f"[yellow]EPS estimates 更新跳过: {e}[/yellow]")
            n7 = 0
        try:
            fred = FREDDownloader(db)
            n5 = fred.update()
        except Exception as e:
            logger.warning(f"data_update: FRED 更新跳过: {e}")
            console.print(f"[yellow]FRED 更新跳过: {e}[/yellow]")
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


# ============================================================
# universe: 股票池
# ============================================================

@app.command("universe")
def universe(
    market: str = typer.Option("cn", help="市场: cn 或 us"),
    date: str = typer.Option("", help="日期 (YYYY-MM-DD)，默认今天"),
    limit: int = typer.Option(20, help="显示前 N 只"),
):
    """查看可交易股票池"""
    if not date:
        from datetime import datetime
        date = datetime.now().strftime("%Y-%m-%d")

    db = _get_db()

    if market == "us":
        from services.data.us_cleaner import get_us_clean_universe
        df = get_us_clean_universe(date)
        id_col, name_col, ind_col = "ticker", "name", "sector"
    else:
        from services.data.cleaner import get_clean_universe
        df = get_clean_universe(db, date)
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


# ============================================================
# select: 选股
# ============================================================

@app.command("select")
def select(
    market: str = typer.Option("cn", help="市场: cn 或 us"),
    date: str = typer.Option("", help="日期 (YYYY-MM-DD)，默认今天"),
    top: int = typer.Option(0, help="只显示前 N 只 (0=全部)"),
):
    """运行多因子选股"""
    if not date:
        from datetime import datetime
        date = datetime.now().strftime("%Y-%m-%d")

    db = _get_db()

    console.print(f"[cyan]运行 {market.upper()} 选股: {date}[/cyan]")
    t0 = time.time()

    if market == "us":
        from services.strategy.us_multi_factor import USMultiFactorStrategy
        strategy = USMultiFactorStrategy(db)
        result = strategy.select_stocks(date)
        id_col = "ticker"
    else:
        from services.strategy.multi_factor import MultiFactorStrategy
        strategy = MultiFactorStrategy(db)
        result = strategy.select_stocks(date)
        id_col = "ts_code"

    elapsed = time.time() - t0

    if result is None or result.empty:
        console.print(f"[yellow]无选股结果 ({elapsed:.1f}s)[/yellow]")
        return

    console.print(f"[green]选出 {len(result)} 只股票 ({elapsed:.1f}s)[/green]\n")

    show = result.head(top) if top > 0 else result

    t = Table()
    t.add_column("#", justify="right")
    if "side" in result.columns:
        t.add_column("Side")
    t.add_column(id_col, style="cyan")
    if "name" in result.columns:
        t.add_column("Name")
    t.add_column("Score", justify="right")
    t.add_column("Weight", justify="right")
    if "sector" in result.columns:
        t.add_column("Sector")
    elif "industry_name" in result.columns:
        t.add_column("Industry")

    for i, (_, row) in enumerate(show.iterrows(), 1):
        cols = [str(i)]
        if "side" in result.columns:
            side = str(row.get("side", ""))
            side_str = f"[green]{side}[/green]" if side == "LONG" else f"[red]{side}[/red]"
            cols.append(side_str)
        cols.append(str(row[id_col]))
        if "name" in result.columns:
            cols.append(str(row.get("name", "")))
        cols.append(f"{row['score']:.3f}")
        w = row['weight']
        w_str = f"{w*100:+.1f}%" if w < 0 else f"{w*100:.1f}%"
        cols.append(w_str)
        if "sector" in result.columns:
            cols.append(str(row.get("sector", "")))
        elif "industry_name" in result.columns:
            cols.append(str(row.get("industry_name", "")))
        t.add_row(*cols)

    console.print(t)

    # Summary for long-short
    if "side" in result.columns:
        n_long = (result["weight"] > 0).sum()
        n_short = (result["weight"] < 0).sum()
        long_total = result.loc[result["weight"] > 0, "weight"].sum()
        short_total = result.loc[result["weight"] < 0, "weight"].sum()
        console.print(f"  [green]Long: {n_long} stocks ({long_total:.1%})[/green]  "
                      f"[red]Short: {n_short} stocks ({short_total:+.1%})[/red]  "
                      f"Net: {long_total + short_total:.1%}")


# ============================================================
# backtest: 回测
# ============================================================

@app.command("backtest")
def backtest(
    market: str = typer.Option("cn", help="市场: cn 或 us"),
    start: str = typer.Option("2020-01-01", help="开始日期"),
    end: str = typer.Option("2025-12-31", help="结束日期"),
    capital: float = typer.Option(0, help="初始资金 (0=默认)"),
    strategy_type: str = typer.Option("alpha", help="策略类型: alpha, beta, baseline (仅美股)"),
):
    """运行回测"""
    db = _get_db()

    if market == "us":
        console.print(f"[cyan]运行 US 回测 ({strategy_type}): {start} ~ {end}[/cyan]")
        from services.strategy.us_backtest import USBacktestEngine
        if strategy_type == "beta":
            from services.strategy.us_beta_strategy import USBetaStrategy
            strategy = USBetaStrategy(db)
        elif strategy_type == "baseline":
            from services.strategy.us_baseline_strategy import USBaselineStrategy
            strategy = USBaselineStrategy(db)
        else:
            from services.strategy.us_multi_factor import USMultiFactorStrategy
            strategy = USMultiFactorStrategy(db)
        cap = capital if capital > 0 else 1000000
    else:
        console.print(f"[cyan]运行 CN 回测: {start} ~ {end}[/cyan]")
        from services.strategy.multi_factor import MultiFactorStrategy
        from services.strategy.backtest import BacktestEngine
        strategy = MultiFactorStrategy(db)
        cap = capital if capital > 0 else 1000000

    t0 = time.time()
    console.print("  生成信号...")
    signals = strategy.generate_signals(start, end)
    t1 = time.time()
    console.print(f"  信号生成完成: {len(signals)} 个调仓日 ({t1-t0:.1f}s)")

    console.print("  运行回测...")
    if market == "us":
        engine = USBacktestEngine(
            initial_capital=cap,
            risk_controls=(strategy_type != "baseline"),
        )
    else:
        engine = BacktestEngine(initial_capital=cap)
    result = engine.run(signals, start, end)
    t2 = time.time()
    console.print(f"  回测完成 ({t2-t1:.1f}s)")

    # 显示统计
    stats = result.get("stats", {})
    if stats:
        console.print()
        t = Table(title="回测绩效")
        t.add_column("Metric", style="cyan")
        t.add_column("Value", justify="right")
        for k, v in stats.items():
            t.add_row(str(k), str(v))
        console.print(t)

        # FF5 Alpha 醒目输出
        ff5_alpha = stats.get("ff5_alpha_annual")
        ff5_t = stats.get("ff5_alpha_t_stat")
        if ff5_alpha is not None and ff5_t is not None:
            sig = "***" if abs(ff5_t) >= 2.58 else "**" if abs(ff5_t) >= 1.96 else "*" if abs(ff5_t) >= 1.65 else ""
            color = "green" if ff5_alpha > 0 else "red"
            console.print(f"\n  [bold {color}]FF5 Alpha: {ff5_alpha:.2%}/年 (t={ff5_t:.2f}{sig})[/bold {color}]")
            console.print(f"  β_mkt={stats.get('ff5_beta_mkt',0):.2f}, β_rmw={stats.get('ff5_beta_rmw',0):.2f}, R²={stats.get('ff5_r_squared',0):.2f}")

    nav = result.get("nav")
    benchmark_nav = result.get("benchmark_nav")
    if nav is not None and not nav.empty:
        console.print(f"\n  NAV 数据点: {len(nav)}, 最终净值: {nav.iloc[-1]:.4f}")

        # 逐年收益 breakdown
        import numpy as np
        import pandas as pd
        nav_s = nav.copy()
        nav_s.index = pd.to_datetime(nav_s.index)
        yearly = Table(title="逐年收益")
        yearly.add_column("Year", style="cyan")
        yearly.add_column("策略", justify="right")
        yearly.add_column("基准", justify="right")
        yearly.add_column("超额", justify="right")
        yearly.add_column("最大回撤", justify="right")

        years = sorted(nav_s.index.year.unique())
        for yr in years:
            yr_nav = nav_s[nav_s.index.year == yr]
            if len(yr_nav) < 2:
                continue
            strat_ret = yr_nav.iloc[-1] / yr_nav.iloc[0] - 1
            # 年内最大回撤
            peak = yr_nav.cummax()
            dd = (yr_nav - peak) / peak
            max_dd = dd.min()

            bm_ret_str = "-"
            excess_str = "-"
            if benchmark_nav is not None and not benchmark_nav.empty:
                bm_s = benchmark_nav.copy()
                bm_s.index = pd.to_datetime(bm_s.index)
                yr_bm = bm_s[bm_s.index.year == yr]
                if len(yr_bm) >= 2:
                    bm_ret = yr_bm.iloc[-1] / yr_bm.iloc[0] - 1
                    bm_ret_str = f"{bm_ret:.2%}"
                    excess = strat_ret - bm_ret
                    color = "green" if excess > 0 else "red"
                    excess_str = f"[{color}]{excess:+.2%}[/{color}]"

            yearly.add_row(str(yr), f"{strat_ret:.2%}", bm_ret_str, excess_str, f"{max_dd:.2%}")

        console.print()
        console.print(yearly)

    trades = result.get("trades")
    if trades is not None and not trades.empty:
        console.print(f"  总交易笔数: {len(trades)}")

    # 存库
    from services.strategy.backtest_saver import save_backtest_result
    st = strategy_type if market == "us" else "alpha"
    if save_backtest_result(market, st, start, end, result):
        console.print("  [dim]回测结果已保存到数据库[/dim]")
    else:
        console.print("  [yellow]回测结果保存失败[/yellow]")

    console.print(f"\n[green]总耗时: {t2-t0:.1f}s[/green]")


# ============================================================
# factor: 因子调试
# ============================================================

factor_app = typer.Typer(help="因子计算与调试")
app.add_typer(factor_app, name="factor")


@factor_app.command("calc")
def factor_calc(
    market: str = typer.Option("us", help="市场: cn 或 us"),
    name: str = typer.Argument(help="因子名 (如 EP, MOM_1M, US_MACRO_CYCLE)"),
    date: str = typer.Option("", help="日期"),
    top: int = typer.Option(10, help="显示前 N 只"),
):
    """计算单个因子"""
    if not date:
        from datetime import datetime
        date = datetime.now().strftime("%Y-%m-%d")

    db = _get_db()

    if market == "us":
        from services.data.us_cleaner import get_us_clean_universe
        from services.us_factors import value, quality, growth, momentum, technical, macro, analyst, polymarket, accruals
        universe = get_us_clean_universe(date)
        id_col = "ticker"
        factor_map = {
            "EP": value.EP, "BP": value.BP, "DIV_YIELD": value.DivYield,
            "ROE_TTM": quality.RoeTTM, "GROSS_MARGIN": quality.GrossMargin,
            "PROFIT_STB": quality.ProfitStability, "MARGIN_TREND": quality.MarginTrend,
            "NET_PROFIT_YOY": growth.NetProfitYoY, "REVENUE_YOY": growth.RevenueYoY,
            "NET_PROFIT_CAGR_3Y": growth.NetProfitCAGR3Y,
            "MOM_1M": momentum.Mom1M, "MOM_3M": momentum.Mom3M,
            "MOM_12M": momentum.Mom12M, "REV_5D": momentum.Rev5D,
            "RESIDUAL_MOM": momentum.ResidualMom,
            "TURN_20D": technical.Turn20D, "VOL_20D": technical.Vol20D,
            "PRICE_DEV_60D": technical.PriceDev60D, "IVOL": technical.Ivol,
            "SIZE": technical.Size, "VOL_PRICE_DIV": technical.VolPriceDiv,
            "US_MACRO_CYCLE": macro.USMacroCycle, "US_MACRO_LIQD": macro.USMacroLiqd,
            "US_MACRO_INFL": macro.USMacroInfl, "US_MACRO_EXTR": macro.USMacroExtr,
            "US_ANALYST_RATING": analyst.USAnalystRating,
            "US_ANALYST_COVERAGE": analyst.USAnalystCoverage,
            "ACCRUALS": accruals.Accruals, "BUYBACK_YIELD": accruals.BuybackYield,
            "POLYMARKET_SENT": polymarket.PolymarketSent,
        }
    else:
        from services.data.cleaner import get_clean_universe
        universe = get_clean_universe(db, date)
        id_col = "ts_code"
        console.print("[yellow]A股因子 CLI 映射暂未实现，请先用 --market us[/yellow]")
        raise typer.Exit(1)

    if universe.empty:
        console.print("[yellow]股票池为空[/yellow]")
        raise typer.Exit(1)

    name_upper = name.upper()
    if name_upper not in factor_map:
        console.print(f"[red]未知因子: {name}[/red]")
        console.print(f"可选: {', '.join(sorted(factor_map.keys()))}")
        raise typer.Exit(1)

    factor_cls = factor_map[name_upper]
    factor = factor_cls(db)

    # 预加载数据（动量/技术因子需要 rolling stats）
    if market == "us":
        from services.us_factors.base import USFactorBase
        if not USFactorBase._static_cache.get("_bulk_daily"):
            console.print("  [dim]预加载数据...[/dim]")
            USFactorBase.preload_for_backtest(date, date)
            USFactorBase.precompute_rolling_stats()

    console.print(f"[cyan]计算 {name_upper}: date={date}, universe={len(universe)}[/cyan]")
    t0 = time.time()
    result = factor.compute(date, universe)
    elapsed = time.time() - t0

    if result.empty:
        console.print(f"[yellow]因子结果为空 ({elapsed:.1f}s)[/yellow]")
        return

    valid = result["factor_value"].notna().sum()
    console.print(f"[green]完成: {valid}/{len(result)} 有效值 ({elapsed:.1f}s)[/green]\n")

    # 统计摘要
    vals = result["factor_value"].dropna()
    if not vals.empty:
        console.print(f"  mean={vals.mean():.4f}  std={vals.std():.4f}  "
                      f"min={vals.min():.4f}  max={vals.max():.4f}  "
                      f"median={vals.median():.4f}\n")

    # Top/Bottom
    sorted_df = result.dropna(subset=["factor_value"]).sort_values("factor_value", ascending=False)

    t = Table(title=f"Top {top} — {name_upper}")
    t.add_column("#", justify="right")
    t.add_column(id_col, style="cyan")
    t.add_column("Factor Value", justify="right")
    for i, (_, row) in enumerate(sorted_df.head(top).iterrows(), 1):
        t.add_row(str(i), str(row[id_col]), f"{row['factor_value']:.4f}")
    console.print(t)

    console.print()
    t2 = Table(title=f"Bottom {top} — {name_upper}")
    t2.add_column("#", justify="right")
    t2.add_column(id_col, style="cyan")
    t2.add_column("Factor Value", justify="right")
    for i, (_, row) in enumerate(sorted_df.tail(top).iterrows(), 1):
        t2.add_row(str(i), str(row[id_col]), f"{row['factor_value']:.4f}")
    console.print(t2)


@factor_app.command("list")
def factor_list(
    market: str = typer.Option("us", help="市场: cn 或 us"),
):
    """列出所有可用因子"""
    if market == "us":
        categories = {
            "value": ["EP", "BP", "DIV_YIELD"],
            "quality": ["ROE_TTM", "GROSS_MARGIN", "PROFIT_STB", "MARGIN_TREND"],
            "growth": ["NET_PROFIT_YOY", "REVENUE_YOY", "NET_PROFIT_CAGR_3Y"],
            "momentum": ["MOM_1M", "MOM_3M", "MOM_12M", "REV_5D", "RESIDUAL_MOM"],
            "technical": ["TURN_20D", "VOL_20D", "PRICE_DEV_60D", "SIZE", "VOL_PRICE_DIV"],
            "macro": ["US_MACRO_CYCLE", "US_MACRO_LIQD", "US_MACRO_INFL", "US_MACRO_EXTR"],
            "analyst": ["US_ANALYST_RATING", "US_ANALYST_COVERAGE"],
        }
    else:
        categories = {
            "value": ["EP", "BP", "DIV_YIELD"],
            "quality": ["ROE_TTM", "GROSS_MARGIN", "PROFIT_STB", "MARGIN_TREND"],
            "growth": ["NET_PROFIT_YOY", "REVENUE_YOY", "NET_PROFIT_CAGR_3Y"],
            "momentum": ["MOM_1M", "MOM_3M", "MOM_12M", "REV_5D", "IND_MOM", "RESIDUAL_MOM", "CMDTY_MOM"],
            "technical": ["TURN_20D", "VOL_20D", "PRICE_DEV_60D", "SIZE", "VOL_PRICE_DIV"],
            "macro": ["MACRO_CYCLE", "MACRO_LIQD", "MACRO_INFL", "MACRO_EXTR"],
            "sentiment": ["POLICY_SENT", "POLICY_INTENSITY", "ANALYST_RATING", "ANALYST_COVERAGE"],
        }

    t = Table(title=f"{market.upper()} 因子列表")
    t.add_column("Category", style="cyan")
    t.add_column("Factors")
    t.add_column("Count", justify="right")
    total = 0
    for cat, factors in categories.items():
        t.add_row(cat, ", ".join(factors), str(len(factors)))
        total += len(factors)
    t.add_row("[bold]Total[/bold]", "", f"[bold]{total}[/bold]")
    console.print(t)


@factor_app.command("intra-sector")
def factor_intra_sector(
    start: str = typer.Option("2012-01-01", help="开始日期"),
    end: str = typer.Option("2023-12-31", help="结束日期"),
    freq: int = typer.Option(1, help="采样频率（月）"),
    factors: str = typer.Option("", help="因子列表，逗号分隔（默认全部）"),
):
    """行业内截面 IC 测试：验证因子在同一行业内是否有选股能力"""
    import numpy as np
    import pandas as pd

    db = _get_db()

    from services.factors.evaluation import FactorEvaluator
    from services.us_factors.base import USFactorBase
    from services.data.us_cleaner import get_us_clean_universe

    evaluator = FactorEvaluator(db, market="us")
    factor_map = evaluator._get_factor_map()
    factor_list = [f.strip() for f in factors.split(",") if f.strip()] or None
    if factor_list:
        factor_map = {k: v for k, v in factor_map.items() if k in factor_list}

    console.print(f"[cyan]行业内截面 IC 测试: {start} ~ {end}, {len(factor_map)} 因子[/cyan]")

    # 预加载
    evaluator._preload(start, end)

    # 评估日期
    eval_dates = evaluator._get_eval_dates(start, end, freq)
    if len(eval_dates) < 2:
        console.print("[red]评估日期不足[/red]")
        raise typer.Exit(1)

    console.print(f"  评估日期: {len(eval_dates)} 个")

    # 收集数据
    bulk_daily = USFactorBase._static_cache.get("_bulk_daily")
    if bulk_daily is None or bulk_daily.empty:
        console.print("[red]无预加载日线数据[/red]")
        raise typer.Exit(1)

    # {sector: {factor: [ic_values]}}
    sector_ic = {}
    all_ic = {}  # {factor: [ic_values]} 全市场对照

    t0 = time.time()
    for i, date_str in enumerate(eval_dates[:-1]):
        next_date = eval_dates[i + 1]
        date_dt = pd.to_datetime(date_str)
        next_dt = pd.to_datetime(next_date)

        # 股票池 + 行业
        universe = get_us_clean_universe(date_str)
        if universe.empty or len(universe) < 50:
            continue

        # 前瞻收益
        mask1 = (bulk_daily["trade_date"] >= date_dt - pd.Timedelta(days=5)) & \
                (bulk_daily["trade_date"] <= date_dt)
        px1 = bulk_daily[mask1].sort_values("trade_date").groupby("ticker").tail(1)[["ticker", "adj_close"]]
        mask2 = (bulk_daily["trade_date"] >= next_dt - pd.Timedelta(days=5)) & \
                (bulk_daily["trade_date"] <= next_dt)
        px2 = bulk_daily[mask2].sort_values("trade_date").groupby("ticker").tail(1)[["ticker", "adj_close"]]
        if px1.empty or px2.empty:
            continue
        px1.columns = ["ticker", "px1"]
        px2.columns = ["ticker", "px2"]
        ret = px1.merge(px2, on="ticker")
        ret["fwd_ret"] = ret["px2"] / ret["px1"] - 1

        # 计算因子
        for fname, fcls in factor_map.items():
            try:
                fv = fcls(db).compute(date_str, universe)
            except Exception:
                continue
            if fv.empty or fv["factor_value"].notna().sum() < 30:
                continue

            merged = fv[["ticker", "factor_value"]].merge(ret[["ticker", "fwd_ret"]], on="ticker")
            merged = merged.merge(universe[["ticker", "sector"]], on="ticker", how="left")
            merged = merged.dropna(subset=["factor_value", "fwd_ret", "sector"])

            # 全市场 IC
            if len(merged) >= 30:
                ic_all = merged["factor_value"].corr(merged["fwd_ret"], method="spearman")
                if not np.isnan(ic_all):
                    all_ic.setdefault(fname, []).append(ic_all)

            # 行业内 IC
            for sector, grp in merged.groupby("sector"):
                if len(grp) < 15:
                    continue
                ic = grp["factor_value"].corr(grp["fwd_ret"], method="spearman")
                if not np.isnan(ic):
                    sector_ic.setdefault(sector, {}).setdefault(fname, []).append(ic)

        if (i + 1) % 12 == 0:
            console.print(f"  进度: {i+1}/{len(eval_dates)-1} ({date_str})")

    elapsed = time.time() - t0
    console.print(f"[green]完成 ({elapsed:.1f}s)[/green]\n")

    # 汇总：每个行业的平均 IC
    sectors = sorted(sector_ic.keys())
    factor_names = sorted(factor_map.keys())

    # 1. 全市场 IC 表
    console.print("[bold]全市场截面 IC（对照）[/bold]")
    t_all = Table()
    t_all.add_column("Factor", style="cyan")
    t_all.add_column("IC Mean", justify="right")
    t_all.add_column("ICIR", justify="right")
    t_all.add_column("N", justify="right")
    for fname in factor_names:
        ics = all_ic.get(fname, [])
        if ics:
            mean_ic = np.mean(ics)
            icir = mean_ic / (np.std(ics) + 1e-10)
            t_all.add_row(fname, f"{mean_ic:.4f}", f"{icir:.4f}", str(len(ics)))
        else:
            t_all.add_row(fname, "-", "-", "0")
    console.print(t_all)

    # 2. 行业内 IC 表
    console.print(f"\n[bold]行业内截面 IC（关键测试）[/bold]")
    for sector in sectors:
        s_data = sector_ic[sector]
        has_sig = False
        rows = []
        for fname in factor_names:
            ics = s_data.get(fname, [])
            if len(ics) >= 6:
                mean_ic = np.mean(ics)
                icir = mean_ic / (np.std(ics) + 1e-10)
                sig = "**" if abs(icir) >= 0.3 else "*" if abs(icir) >= 0.15 else ""
                if abs(icir) >= 0.15:
                    has_sig = True
                rows.append((fname, f"{mean_ic:.4f}", f"{icir:.4f}{sig}", str(len(ics))))
            else:
                rows.append((fname, "-", "-", str(len(ics))))

        if has_sig:
            t_sec = Table(title=f"{sector}")
            t_sec.add_column("Factor", style="cyan")
            t_sec.add_column("IC Mean", justify="right")
            t_sec.add_column("ICIR", justify="right")
            t_sec.add_column("N", justify="right")
            for row in rows:
                t_sec.add_row(*row)
            console.print(t_sec)

    # 3. 汇总：行业内平均 ICIR
    console.print(f"\n[bold]行业内平均 |ICIR|（跨行业均值）[/bold]")
    t_sum = Table()
    t_sum.add_column("Factor", style="cyan")
    t_sum.add_column("全市场 ICIR", justify="right")
    t_sum.add_column("行业内均 |ICIR|", justify="right")
    t_sum.add_column("结论", justify="right")
    for fname in factor_names:
        # 全市场
        ics_all = all_ic.get(fname, [])
        icir_all = np.mean(ics_all) / (np.std(ics_all) + 1e-10) if ics_all else 0

        # 行业内平均
        sector_icirs = []
        for sector in sectors:
            ics = sector_ic.get(sector, {}).get(fname, [])
            if len(ics) >= 6:
                sector_icirs.append(abs(np.mean(ics) / (np.std(ics) + 1e-10)))
        avg_intra = np.mean(sector_icirs) if sector_icirs else 0

        verdict = "[green]有选股力[/green]" if avg_intra >= 0.15 else "[red]无选股力[/red]" if avg_intra < 0.05 else "[yellow]弱[/yellow]"
        t_sum.add_row(fname, f"{icir_all:.4f}", f"{avg_intra:.4f}", verdict)
    console.print(t_sum)


@factor_app.command("eval")
def factor_eval(
    market: str = typer.Option("us", help="市场: cn 或 us"),
    start: str = typer.Option("2020-01-01", help="开始日期"),
    end: str = typer.Option("2025-12-31", help="结束日期"),
    freq: int = typer.Option(1, help="采样频率（月）"),
    factors: str = typer.Option("", help="因子列表，逗号分隔（默认全部）"),
    no_plot: bool = typer.Option(False, "--no-plot", help="不生成图表"),
):
    """运行因子评估（IC/ICIR/分层/IC Decay/换手率/相关性）"""
    db = _get_db()

    import numpy as np
    from services.factors.evaluation import FactorEvaluator
    evaluator = FactorEvaluator(db, market=market)

    factor_list_arg = [f.strip() for f in factors.split(",") if f.strip()] or None

    console.print(f"[cyan]运行 {market.upper()} 因子评估: {start} ~ {end}, freq={freq}m[/cyan]")
    t0 = time.time()

    report = evaluator.run_all(
        start=start, end=end, freq_months=freq,
        factors=factor_list_arg, plot=not no_plot,
    )

    elapsed = time.time() - t0
    console.print(f"[green]完成 ({elapsed:.1f}s)[/green]\n")

    # 汇总表格
    t = Table(title=f"{market.upper()} 因子评估 ({start} ~ {end})")
    t.add_column("Factor", style="cyan")
    t.add_column("IC Mean", justify="right")
    t.add_column("IC Std", justify="right")
    t.add_column("ICIR", justify="right")
    t.add_column("IC>0", justify="right")
    t.add_column("Q5-Q1 Ann", justify="right")
    t.add_column("Top Turn", justify="right")
    t.add_column("N", justify="right")

    for fname, data in report.items():
        if fname.startswith("_"):
            logger.debug(f"factor_eval: 跳过内部键 {fname}")
            continue
        ic_mean = data.get("ic_mean", np.nan)
        icir = data.get("icir", np.nan)
        spread = data.get("quantile_spread_annual", np.nan)

        # 颜色标注
        ic_str = f"{ic_mean:.4f}" if not np.isnan(ic_mean) else "-"
        icir_str = f"{icir:.4f}" if not np.isnan(icir) else "-"
        spread_str = f"{spread:.2%}" if not np.isnan(spread) else "-"

        if not np.isnan(icir) and abs(icir) >= 0.5:
            icir_str = f"[green]{icir_str}[/green]" if icir > 0 else f"[red]{icir_str}[/red]"

        t.add_row(
            fname,
            ic_str,
            f"{data.get('ic_std', np.nan):.4f}" if not np.isnan(data.get("ic_std", np.nan)) else "-",
            icir_str,
            f"{data.get('ic_positive_rate', np.nan):.0%}" if not np.isnan(data.get("ic_positive_rate", np.nan)) else "-",
            spread_str,
            f"{data.get('top_turnover', np.nan):.0%}" if not np.isnan(data.get("top_turnover", np.nan)) else "-",
            str(data.get("num_periods", 0)),
        )

    console.print(t)

    # IC Decay 汇总
    console.print()
    t2 = Table(title="IC Decay (horizons: 1/5/10/20/60 days)")
    t2.add_column("Factor", style="cyan")
    t2.add_column("1d", justify="right")
    t2.add_column("5d", justify="right")
    t2.add_column("10d", justify="right")
    t2.add_column("20d", justify="right")
    t2.add_column("60d", justify="right")

    for fname, data in report.items():
        if fname.startswith("_"):
            logger.debug(f"factor_eval: 跳过内部键 {fname}")
            continue
        decay = data.get("ic_decay", [])
        decay_map = {d["horizon"]: d["ic_mean"] for d in decay if isinstance(d, dict)}
        vals = []
        for h in [1, 5, 10, 20, 60]:
            v = decay_map.get(h, np.nan)
            vals.append(f"{v:.4f}" if not np.isnan(v) else "-")
        t2.add_row(fname, *vals)

    console.print(t2)

    # 相关性矩阵提示
    corr = report.get("_correlation_matrix")
    if corr is not None and not corr.empty:
        console.print(f"\n[dim]因子相关性矩阵已保存到 output/factor_corr_{market}.png[/dim]")


# ============================================================
# score: 查看单只股票得分
# ============================================================

@app.command("score")
def score(
    stock: str = typer.Argument(help="股票代码 (如 AAPL 或 000001.SZ)"),
    date: str = typer.Option("", help="日期"),
    market: str = typer.Option("", help="市场: cn/us (自动检测)"),
):
    """查看单只股票的综合得分和因子明细"""
    if not date:
        from datetime import datetime
        date = datetime.now().strftime("%Y-%m-%d")

    # 自动检测市场
    if not market:
        market = "cn" if "." in stock else "us"

    db = _get_db()

    console.print(f"[cyan]计算 {stock} 得分: {date} ({market.upper()})[/cyan]")
    t0 = time.time()

    if market == "us":
        from services.strategy.us_multi_factor import USMultiFactorStrategy
        strategy = USMultiFactorStrategy(db)
        result = strategy.select_stocks(date)
        id_col = "ticker"
    else:
        from services.strategy.multi_factor import MultiFactorStrategy
        strategy = MultiFactorStrategy(db)
        result = strategy.select_stocks(date)
        id_col = "ts_code"

    elapsed = time.time() - t0

    if result is None or result.empty:
        console.print(f"[yellow]无选股结果 ({elapsed:.1f}s)[/yellow]")
        return

    row = result[result[id_col] == stock]
    if row.empty:
        # 也许没入选 Top-N，但可以展示排名
        console.print(f"[yellow]{stock} 未入选 Top-N ({elapsed:.1f}s)[/yellow]")
        console.print(f"  入选 {len(result)} 只: {', '.join(result[id_col].head(5).tolist())}...")
    else:
        r = row.iloc[0]
        console.print(f"\n[green]{stock} 入选! ({elapsed:.1f}s)[/green]")
        rank = (result["score"] >= r["score"]).sum()
        console.print(f"  得分: {r['score']:.4f} (排名 {rank}/{len(result)})")
        console.print(f"  权重: {r['weight']*100:.2f}%")
        if "sector" in result.columns:
            console.print(f"  行业: {r.get('sector', '-')}")


# ============================================================
# paper: 模拟交易
# ============================================================

paper_app = typer.Typer(help="模拟交易")
app.add_typer(paper_app, name="paper")


@paper_app.command("status")
def paper_status(
    market: str = typer.Option("cn", help="市场: cn, us 或 alpaca"),
):
    """查看模拟账户状态"""
    db = _get_db()

    if market == "alpaca":
        from services.execution.alpaca_trader import AlpacaTrader
        trader = AlpacaTrader(db)
        trader.connect()
        console.print(f"\n[cyan]Alpaca 模拟账户[/cyan]")
        console.print(trader.get_position_report())
        return
    elif market == "us":
        from services.execution.us_paper_trader import USPaperTrader
        trader = USPaperTrader(db)
        trader.connect()
    else:
        from services.execution.paper_trader import PaperTrader
        trader = PaperTrader(db)
        trader.connect()

    info = trader.get_account_info()
    console.print(f"\n[cyan]{market.upper()} 模拟账户[/cyan]")
    for k, v in info.items():
        if isinstance(v, float):
            console.print(f"  {k}: {v:,.2f}")
        else:
            console.print(f"  {k}: {v}")

    positions = trader.get_current_positions()
    if positions is not None and not positions.empty:
        console.print(f"\n[cyan]持仓 ({len(positions)} 只):[/cyan]")
        console.print(positions.to_string())
    else:
        console.print("\n  [dim]空仓[/dim]")


@paper_app.command("trade")
def paper_trade(
    market: str = typer.Option("cn", help="市场: cn, us 或 alpaca"),
    date: str = typer.Option("", help="交易日期"),
):
    """执行模拟交易（选股+调仓）"""
    if not date:
        from datetime import datetime
        date = datetime.now().strftime("%Y-%m-%d")

    db = _get_db()

    console.print(f"[cyan]执行 {market.upper()} 模拟交易: {date}[/cyan]")
    t0 = time.time()

    if market in ("us", "alpaca"):
        from services.strategy.us_multi_factor import USMultiFactorStrategy
        strategy = USMultiFactorStrategy(db)
        result = strategy.select_stocks(date)
        if result is None or result.empty:
            console.print("[yellow]无选股结果，跳过交易[/yellow]")
            return

        if market == "alpaca":
            from services.execution.alpaca_trader import AlpacaTrader
            trader = AlpacaTrader(db)
        else:
            from services.execution.us_paper_trader import USPaperTrader
            trader = USPaperTrader(db)

        trader.connect()
        n = trader.sync_position(result[["ticker", "weight"]])
        trader.update_nav()
    else:
        from services.strategy.multi_factor import MultiFactorStrategy
        from services.execution.paper_trader import PaperTrader
        strategy = MultiFactorStrategy(db)
        result = strategy.select_stocks(date)
        if result is None or result.empty:
            console.print("[yellow]无选股结果，跳过交易[/yellow]")
            return
        trader = PaperTrader(db)
        trader.connect()
        n = trader.sync_position(result[["ts_code", "weight"]])

    elapsed = time.time() - t0
    console.print(f"[green]交易完成: {n} 笔 ({elapsed:.1f}s)[/green]")


@paper_app.command("reset")
def paper_reset(
    market: str = typer.Option("cn", help="市场: cn, us 或 alpaca"),
):
    """重置模拟账户"""
    db = _get_db()

    if market == "alpaca":
        from services.execution.alpaca_trader import AlpacaTrader
        trader = AlpacaTrader(db)
        trader.connect()
        trader.reset()
    elif market == "us":
        from services.execution.us_paper_trader import USPaperTrader
        trader = USPaperTrader(db)
        trader.connect()
        trader.reset()
    else:
        from services.execution.paper_trader import PaperTrader
        trader = PaperTrader(db)
        trader.connect()
        trader.reset_account()

    console.print(f"[green]{market.upper()} 模拟账户已重置[/green]")


# ============================================================
# 入口
# ============================================================

if __name__ == "__main__":
    app()
