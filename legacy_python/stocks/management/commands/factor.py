"""
因子调试工具。

Usage:
    python3 manage.py factor calc EP --date 2025-01-15
    python3 manage.py factor list
    python3 manage.py factor eval --start 2020-01-01 --end 2025-12-31
    python3 manage.py factor intra-sector --start 2012-01-01 --end 2023-12-31
"""

import logging
import time
from datetime import datetime

import numpy as np
import pandas as pd
from django.core.management.base import BaseCommand
from rich.console import Console
from rich.table import Table

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "因子调试 (calc/list/eval/intra-sector)"

    def add_arguments(self, parser):
        sub = parser.add_subparsers(dest="action")

        # calc
        p_calc = sub.add_parser("calc", help="计算单个因子")
        p_calc.add_argument("name", help="因子名 (如 EP, MOM_1M)")
        p_calc.add_argument("--market", default="us", choices=["cn", "us"])
        p_calc.add_argument("--date", default="")
        p_calc.add_argument("--top", type=int, default=10)

        # list
        p_list = sub.add_parser("list", help="列出所有可用因子")
        p_list.add_argument("--market", default="us", choices=["cn", "us"])

        # eval
        p_eval = sub.add_parser("eval", help="因子评估 (IC/ICIR)")
        p_eval.add_argument("--market", default="us", choices=["cn", "us"])
        p_eval.add_argument("--start", default="2020-01-01")
        p_eval.add_argument("--end", default="2025-12-31")
        p_eval.add_argument("--freq", type=int, default=1, help="采样频率（月）")
        p_eval.add_argument("--factors", default="", help="因子列表，逗号分隔")
        p_eval.add_argument("--no-plot", action="store_true")

        # intra-sector
        p_intra = sub.add_parser("intra-sector", help="行业内截面 IC 测试")
        p_intra.add_argument("--start", default="2012-01-01")
        p_intra.add_argument("--end", default="2023-12-31")
        p_intra.add_argument("--freq", type=int, default=1)
        p_intra.add_argument("--factors", default="")

    def handle(self, *args, **opts):
        action = opts.get("action")
        if action == "calc":
            self._calc(opts)
        elif action == "list":
            self._list(opts)
        elif action == "eval":
            self._eval(opts)
        elif action == "intra-sector":
            self._intra_sector(opts)
        else:
            self.stderr.write("Usage: python3 manage.py factor {calc,list,eval,intra-sector}\n")

    def _calc(self, opts):
        console = Console()
        market = opts["market"]
        date = opts["date"] or datetime.now().strftime("%Y-%m-%d")
        top = opts["top"]
        name_upper = opts["name"].upper()

        factor_map = self._get_factor_map(market)
        if name_upper not in factor_map:
            console.print(f"[red]未知因子: {name_upper}[/red]")
            console.print(f"可选: {', '.join(sorted(factor_map.keys()))}")
            return

        universe, id_col = self._get_universe(market, date)
        if universe.empty:
            console.print("[yellow]股票池为空[/yellow]")
            return

        # 预加载
        if market == "us":
            from stocks.services.factors.us_base import USFactorBase
            if not USFactorBase._static_cache.get("_bulk_daily"):
                console.print("  [dim]预加载数据...[/dim]")
                USFactorBase.preload_for_backtest(date, date)
                USFactorBase.precompute_rolling_stats()

        console.print(f"[cyan]计算 {name_upper}: date={date}, universe={len(universe)}[/cyan]")
        t0 = time.time()
        factor = factor_map[name_upper](None)
        result = factor.compute(date, universe)
        elapsed = time.time() - t0

        if result.empty:
            console.print(f"[yellow]因子结果为空 ({elapsed:.1f}s)[/yellow]")
            return

        valid = result["factor_value"].notna().sum()
        console.print(f"[green]完成: {valid}/{len(result)} 有效值 ({elapsed:.1f}s)[/green]\n")

        vals = result["factor_value"].dropna()
        if not vals.empty:
            console.print(f"  mean={vals.mean():.4f}  std={vals.std():.4f}  "
                          f"min={vals.min():.4f}  max={vals.max():.4f}\n")

        sorted_df = result.dropna(subset=["factor_value"]).sort_values("factor_value", ascending=False)
        t = Table(title=f"Top {top} — {name_upper}")
        t.add_column("#", justify="right")
        t.add_column(id_col, style="cyan")
        t.add_column("Factor Value", justify="right")
        for i, (_, row) in enumerate(sorted_df.head(top).iterrows(), 1):
            t.add_row(str(i), str(row[id_col]), f"{row['factor_value']:.4f}")
        console.print(t)

    def _list(self, opts):
        console = Console()
        market = opts["market"]

        if market == "us":
            from backtest.services.us_strategy import USMultiFactorStrategy
            cats = USMultiFactorStrategy._LEGACY_FACTOR_CATEGORIES.copy()
            import stocks.services.factors.signals  # noqa: F401
            from stocks.services.factors.us_registry import get_registered
            for sig_name, sig_cls in get_registered().items():
                cats.setdefault(sig_cls.category, [])
                if sig_name not in cats[sig_cls.category]:
                    cats[sig_cls.category].append(sig_name)
        else:
            cats = {
                "value": ["EP", "BP", "DIV_YIELD"],
                "quality": ["ROE_TTM", "GROSS_MARGIN", "PROFIT_STB", "MARGIN_TREND"],
                "growth": ["NET_PROFIT_YOY", "REVENUE_YOY", "NET_PROFIT_CAGR_3Y"],
                "momentum": ["MOM_1M", "MOM_3M", "MOM_12M", "REV_5D"],
                "technical": ["TURN_20D", "VOL_20D", "SIZE"],
            }

        t = Table(title=f"{market.upper()} 因子列表")
        t.add_column("Category", style="cyan")
        t.add_column("Factors")
        t.add_column("Count", justify="right")
        total = 0
        for cat, factors in cats.items():
            t.add_row(cat, ", ".join(factors), str(len(factors)))
            total += len(factors)
        t.add_row("[bold]Total[/bold]", "", f"[bold]{total}[/bold]")
        console.print(t)

    def _eval(self, opts):
        console = Console()
        from stocks.services.factors.a_evaluation import FactorEvaluator
        evaluator = FactorEvaluator(None, market=opts["market"])
        factor_list = [f.strip() for f in opts["factors"].split(",") if f.strip()] or None

        console.print(f"[cyan]{opts['market'].upper()} 因子评估: {opts['start']} ~ {opts['end']}[/cyan]")
        t0 = time.time()
        report = evaluator.run_all(
            start=opts["start"], end=opts["end"], freq_months=opts["freq"],
            factors=factor_list, plot=not opts["no_plot"],
        )
        console.print(f"[green]完成 ({time.time()-t0:.1f}s)[/green]\n")

        t = Table(title="因子评估")
        t.add_column("Factor", style="cyan")
        t.add_column("IC Mean", justify="right")
        t.add_column("ICIR", justify="right")
        t.add_column("N", justify="right")
        for fname, data in report.items():
            if fname.startswith("_"):
                continue
            ic_mean = data.get("ic_mean", np.nan)
            icir = data.get("icir", np.nan)
            t.add_row(fname,
                       f"{ic_mean:.4f}" if not np.isnan(ic_mean) else "-",
                       f"{icir:.4f}" if not np.isnan(icir) else "-",
                       str(data.get("num_periods", 0)))
        console.print(t)

    def _intra_sector(self, opts):
        """行业内截面 IC — 委托 scripts/factor_analysis.py 的逻辑太长，保持引用。"""
        console = Console()
        console.print("[yellow]行业内 IC 测试请用:[/yellow]")
        console.print(f"  python3 manage.py factor_analysis --start {opts['start']} --end {opts['end']} --workers 6")

    @staticmethod
    def _get_factor_map(market):
        if market == "us":
            from stocks.services.factors import us_value as value
            from stocks.services.factors import us_growth as growth
            from stocks.services.factors import us_momentum as momentum
            from stocks.services.factors import us_technical as technical
            from stocks.services.factors import us_analyst as analyst
            from stocks.services.factors import us_accruals as accruals
            import stocks.services.factors.signals  # noqa: F401
            from stocks.services.factors.us_registry import get_registered

            fmap = {
                "EP": value.EP, "BP": value.BP, "DIV_YIELD": value.DivYield,
                "NET_PROFIT_YOY": growth.NetProfitYoY, "REVENUE_YOY": growth.RevenueYoY,
                "NET_PROFIT_CAGR_3Y": growth.NetProfitCAGR3Y,
                "MOM_1M": momentum.Mom1M, "MOM_3M": momentum.Mom3M,
                "MOM_12M": momentum.Mom12M, "REV_5D": momentum.Rev5D,
                "TURN_20D": technical.Turn20D, "VOL_20D": technical.Vol20D,
                "IVOL": technical.Ivol, "SIZE": technical.Size,
                "US_ANALYST_RATING": analyst.USAnalystRating,
                "US_ANALYST_COVERAGE": analyst.USAnalystCoverage,
                "BUYBACK_YIELD": accruals.BuybackYield,
            }
            for sig_name, sig_cls in get_registered().items():
                fmap[sig_name] = sig_cls
            return fmap
        return {}

    @staticmethod
    def _get_universe(market, date):
        if market == "us":
            from stocks.services.us_cleaner import get_us_clean_universe
            return get_us_clean_universe(date), "ticker"
        from stocks.services.a_cleaner import get_clean_universe
        return get_clean_universe(None, date), "ts_code"
