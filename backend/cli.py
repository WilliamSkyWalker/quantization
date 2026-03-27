#!/usr/bin/env python3
"""
量化系统 CLI — 薄壳调用 service 层

用法:
    python3 backend/cli.py --help
    python3 backend/cli.py db status
    python3 backend/cli.py select --market us --date 2025-01-15
    python3 backend/cli.py backtest --market us --start 2020-01-01 --end 2025-12-31
"""

import os
import sys
import time
from pathlib import Path
from typing import Optional

# 确保项目根目录在 sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# Django 最小化初始化（仅加载 settings，不启动 ASGI/WSGI）
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "backend.core.settings")
import django
django.setup()

import typer
from rich.console import Console
from rich.table import Table

console = Console()
app = typer.Typer(help="量化交易系统 CLI", no_args_is_help=True)


# ============================================================
# 公共工具
# ============================================================

def _get_db():
    from backend.services.data.database import DatabaseManager
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


@data_app.command("download")
def data_download(
    market: str = typer.Option("cn", help="市场: cn (A股) 或 us (美股)"),
    target: str = typer.Option("all", help="下载目标: all, list, daily, financial, industry, index, macro, analyst, commodity, research"),
):
    """下载数据"""
    db = _get_db()

    if market == "us":
        from backend.services.data.fmp_downloader import FMPDownloader
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
            "simfin": lambda: _download_simfin(db),
            "edgar": lambda: _download_edgar(db),
            "historical": lambda: _download_historical(db),
        }
    else:
        from backend.services.data.downloader import TushareDownloader
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

    def _download_simfin(db_inst):
        from backend.services.data.simfin_downloader import SimFinDownloader
        dl_sf = SimFinDownloader(db_inst)
        return dl_sf.download_financials(force=True)

    def _download_edgar(db_inst):
        from backend.services.data.edgar_downloader import EdgarDownloader
        dl_ed = EdgarDownloader(db_inst)
        return dl_ed.download_financials()

    def _download_historical(db_inst):
        from backend.services.data.historical_universe import (
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
):
    """增量更新数据"""
    db = _get_db()

    if market == "us":
        from backend.services.data.fmp_downloader import FMPDownloader
        from backend.services.data.fred_downloader import FREDDownloader
        dl = FMPDownloader(db)
        console.print("[cyan]增量更新美股数据...[/cyan]")
        t0 = time.time()
        n1 = dl.update_daily_prices()
        n2 = dl.update_financial_data()
        n3 = dl.update_index_daily()
        n4 = dl.update_analyst_recommendations()
        try:
            fred = FREDDownloader(db)
            n5 = fred.update()
        except Exception as e:
            console.print(f"[yellow]FRED 更新跳过: {e}[/yellow]")
            n5 = 0
        elapsed = time.time() - t0
        console.print(f"[green]完成[/green] {elapsed:.1f}s — daily:{n1}, fin:{n2}, idx:{n3}, analyst:{n4}, macro:{n5}")
    else:
        from backend.services.data.downloader import TushareDownloader
        from backend.services.data.updater import FinancialUpdater
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
        from backend.services.data.us_cleaner import get_us_clean_universe
        df = get_us_clean_universe(db, date)
        id_col, name_col, ind_col = "ticker", "name", "sector"
    else:
        from backend.services.data.cleaner import get_clean_universe
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
        from backend.services.strategy.us_multi_factor import USMultiFactorStrategy
        strategy = USMultiFactorStrategy(db)
        result = strategy.select_stocks(date)
        id_col = "ticker"
    else:
        from backend.services.strategy.multi_factor import MultiFactorStrategy
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
    strategy_type: str = typer.Option("alpha", help="策略类型: alpha 或 beta (仅美股)"),
):
    """运行回测"""
    db = _get_db()

    if market == "us":
        console.print(f"[cyan]运行 US 回测 ({strategy_type}): {start} ~ {end}[/cyan]")
        from backend.services.strategy.us_backtest import USBacktestEngine
        if strategy_type == "beta":
            from backend.services.strategy.us_beta_strategy import USBetaStrategy
            strategy = USBetaStrategy(db)
        else:
            from backend.services.strategy.us_multi_factor import USMultiFactorStrategy
            strategy = USMultiFactorStrategy(db)
        cap = capital if capital > 0 else 1000000
    else:
        console.print(f"[cyan]运行 CN 回测: {start} ~ {end}[/cyan]")
        from backend.services.strategy.multi_factor import MultiFactorStrategy
        from backend.services.strategy.backtest import BacktestEngine
        strategy = MultiFactorStrategy(db)
        cap = capital if capital > 0 else 1000000

    t0 = time.time()
    console.print("  生成信号...")
    signals = strategy.generate_signals(start, end)
    t1 = time.time()
    console.print(f"  信号生成完成: {len(signals)} 个调仓日 ({t1-t0:.1f}s)")

    console.print("  运行回测...")
    if market == "us":
        engine = USBacktestEngine(db=db, initial_capital=cap)
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

    nav = result.get("nav")
    if nav is not None and not nav.empty:
        console.print(f"\n  NAV 数据点: {len(nav)}, 最终净值: {nav.iloc[-1]:.4f}")

    trades = result.get("trades")
    if trades is not None and not trades.empty:
        console.print(f"  总交易笔数: {len(trades)}")

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
        from backend.services.data.us_cleaner import get_us_clean_universe
        from backend.services.us_factors import value, quality, growth, momentum, technical, macro, analyst, polymarket, accruals
        universe = get_us_clean_universe(db, date)
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
        from backend.services.data.cleaner import get_clean_universe
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
        from backend.services.us_factors.base import USFactorBase
        if not USFactorBase._static_cache.get("_bulk_daily"):
            console.print("  [dim]预加载数据...[/dim]")
            USFactorBase.preload_for_backtest(db, date, date)
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
        from backend.services.strategy.us_multi_factor import USMultiFactorStrategy
        strategy = USMultiFactorStrategy(db)
        result = strategy.select_stocks(date)
        id_col = "ticker"
    else:
        from backend.services.strategy.multi_factor import MultiFactorStrategy
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
    market: str = typer.Option("cn", help="市场: cn 或 us"),
):
    """查看模拟账户状态"""
    db = _get_db()

    if market == "us":
        from backend.services.execution.us_paper_trader import USPaperTrader
        trader = USPaperTrader(db)
        trader.connect()
    else:
        from backend.services.execution.paper_trader import PaperTrader
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
    market: str = typer.Option("cn", help="市场: cn 或 us"),
    date: str = typer.Option("", help="交易日期"),
):
    """执行模拟交易（选股+调仓）"""
    if not date:
        from datetime import datetime
        date = datetime.now().strftime("%Y-%m-%d")

    db = _get_db()

    console.print(f"[cyan]执行 {market.upper()} 模拟交易: {date}[/cyan]")
    t0 = time.time()

    if market == "us":
        from backend.services.strategy.us_multi_factor import USMultiFactorStrategy
        from backend.services.execution.us_paper_trader import USPaperTrader
        strategy = USMultiFactorStrategy(db)
        result = strategy.select_stocks(date)
        if result is None or result.empty:
            console.print("[yellow]无选股结果，跳过交易[/yellow]")
            return
        trader = USPaperTrader(db)
        trader.connect()
        n = trader.sync_position(result[["ticker", "weight"]])
        trader.update_nav()
    else:
        from backend.services.strategy.multi_factor import MultiFactorStrategy
        from backend.services.execution.paper_trader import PaperTrader
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
    market: str = typer.Option("cn", help="市场: cn 或 us"),
):
    """重置模拟账户"""
    db = _get_db()

    if market == "us":
        from backend.services.execution.us_paper_trader import USPaperTrader
        trader = USPaperTrader(db)
        trader.connect()
        trader.reset()
    else:
        from backend.services.execution.paper_trader import PaperTrader
        trader = PaperTrader(db)
        trader.connect()
        trader.reset_account()

    console.print(f"[green]{market.upper()} 模拟账户已重置[/green]")


# ============================================================
# 入口
# ============================================================

if __name__ == "__main__":
    app()
