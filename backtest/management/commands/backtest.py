"""Run backtest. Usage: python3 manage.py backtest --market us --start 2020-01-01 --end 2025-12-31"""
import logging
import time

from django.core.management.base import BaseCommand
from rich.console import Console
from rich.table import Table

logger = logging.getLogger(__name__)
console = Console()


class Command(BaseCommand):
    help = "运行回测"

    def add_arguments(self, parser):
        parser.add_argument("--market", default="cn", choices=["cn", "us"], help="市场")
        parser.add_argument("--start", default="2020-01-01", help="开始日期")
        parser.add_argument("--end", default="2025-12-31", help="结束日期")
        parser.add_argument("--capital", type=float, default=0, help="初始资金 (0=默认)")
        parser.add_argument(
            "--strategy-type", default="alpha",
            choices=["alpha", "beta", "baseline"],
            help="策略类型 (仅美股)",
        )

    def handle(self, *args, **opts):
        import pandas as pd
        import numpy as np

        market = opts["market"]
        start = opts["start"]
        end = opts["end"]
        capital = opts["capital"]
        strategy_type = opts["strategy_type"]

        # DatabaseManager 已废弃
        db = None  # DatabaseManager 已废弃

        if market == "us":
            console.print(f"[cyan]运行 US 回测 ({strategy_type}): {start} ~ {end}[/cyan]")
            from backtest.services.us_engine import USBacktestEngine
            if strategy_type == "beta":
                from backtest.services.us_beta import USBetaStrategy
                strategy = USBetaStrategy(db)
            elif strategy_type == "baseline":
                from backtest.services.us_baseline import USBaselineStrategy
                strategy = USBaselineStrategy(db)
            else:
                from backtest.services.us_strategy import USMultiFactorStrategy
                strategy = USMultiFactorStrategy(db)
            cap = capital if capital > 0 else 1_000_000
        else:
            console.print(f"[cyan]运行 CN 回测: {start} ~ {end}[/cyan]")
            from backtest.services.a_strategy import MultiFactorStrategy
            from backtest.services.a_engine import BacktestEngine
            strategy = MultiFactorStrategy(db)
            cap = capital if capital > 0 else 1_000_000

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

            nav_s = nav.copy()
            nav_s.index = pd.to_datetime(nav_s.index)
            yearly = Table(title="逐年收益")
            yearly.add_column("Year", style="cyan")
            yearly.add_column("策略", justify="right")
            yearly.add_column("基准", justify="right")
            yearly.add_column("超额", justify="right")
            yearly.add_column("最大回撤", justify="right")

            for yr in sorted(nav_s.index.year.unique()):
                yr_nav = nav_s[nav_s.index.year == yr]
                if len(yr_nav) < 2:
                    continue
                strat_ret = yr_nav.iloc[-1] / yr_nav.iloc[0] - 1
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
                        c = "green" if excess > 0 else "red"
                        excess_str = f"[{c}]{excess:+.2%}[/{c}]"

                yearly.add_row(str(yr), f"{strat_ret:.2%}", bm_ret_str, excess_str, f"{max_dd:.2%}")

            console.print()
            console.print(yearly)

        trades = result.get("trades")
        if trades is not None and hasattr(trades, 'height') and trades.height > 0:
            console.print(f"  总交易笔数: {trades.height}")
        elif trades is not None and hasattr(trades, 'empty') and not trades.empty:
            console.print(f"  总交易笔数: {len(trades)}")

        # 存库
        from backtest.services.us_saver import save_backtest_result
        st = strategy_type if market == "us" else "alpha"
        if save_backtest_result(market, st, start, end, result):
            console.print("  [dim]回测结果已保存到数据库[/dim]")
        else:
            console.print("  [yellow]回测结果保存失败[/yellow]")

        console.print(f"\n[green]总耗时: {t2-t0:.1f}s[/green]")
