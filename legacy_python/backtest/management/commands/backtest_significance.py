"""跑 PSR / DSR / Bootstrap CI 显著性校正。

Usage:
    python3 manage.py backtest_significance --result-id 13
    python3 manage.py backtest_significance --result-id 13 --n-trials 50
    python3 manage.py backtest_significance --result-id 13 --window 2021-01-01:2025-12-31
"""

import logging

from django.core.management.base import BaseCommand
from rich.console import Console
from rich.table import Table

from backtest.models import BacktestResult
from backtest.services.us_significance import run_significance_check, slice_returns_by_date

logger = logging.getLogger(__name__)
console = Console()


class Command(BaseCommand):
    help = "回测显著性校正：PSR / DSR / Bootstrap CI"

    def add_arguments(self, parser):
        parser.add_argument("--result-id", type=int, required=True, help="backtest_result.id")
        parser.add_argument(
            "--n-trials",
            type=int,
            default=50,
            help="DSR 的 N (尝试过的策略数，默认 50)",
        )
        parser.add_argument(
            "--sr-threshold",
            type=float,
            default=0.0,
            help="PSR 测试阈值 (年化 Sharpe，默认 0)",
        )
        parser.add_argument(
            "--bootstrap-n",
            type=int,
            default=1000,
            help="Block bootstrap 迭代次数 (默认 1000)",
        )
        parser.add_argument(
            "--block-size",
            type=int,
            default=21,
            help="Bootstrap 块长度，单位交易日 (默认 21 ≈ 1 月)",
        )
        parser.add_argument(
            "--window",
            default=None,
            help='日期窗口 "YYYY-MM-DD:YYYY-MM-DD"，留空跑全样本',
        )
        parser.add_argument(
            "--also-benchmark",
            action="store_true",
            help="同时跑 benchmark 序列做对照",
        )

    def handle(self, *args, **opts):
        result_id = opts["result_id"]
        n_trials = opts["n_trials"]
        sr_threshold = opts["sr_threshold"]
        boot_n = opts["bootstrap_n"]
        block_size = opts["block_size"]
        window = opts["window"]
        also_bench = opts["also_benchmark"]

        try:
            r = BacktestResult.objects.get(id=result_id)
        except BacktestResult.DoesNotExist:
            self.stderr.write(self.style.ERROR(f"backtest_result id={result_id} 不存在"))
            return

        start_filter = end_filter = None
        if window:
            try:
                start_filter, end_filter = window.split(":")
            except ValueError:
                self.stderr.write(
                    self.style.ERROR("--window 格式应为 YYYY-MM-DD:YYYY-MM-DD")
                )
                return

        console.print(
            f"[bold]Backtest #{result_id}[/bold] — market={r.market} strategy={r.strategy_type} "
            f"period={r.start_date} → {r.end_date}"
        )
        if window:
            console.print(f"[dim]子窗口: {window}[/dim]")
        console.print()

        strat_returns = slice_returns_by_date(r.nav, start_filter, end_filter)
        if strat_returns.size < 30:
            self.stderr.write(self.style.ERROR(f"策略 returns 太少 ({strat_returns.size}), 跳过"))
            return

        strat_report = run_significance_check(
            strat_returns,
            n_trials=n_trials,
            sr_threshold_annual=sr_threshold,
            bootstrap_n_iter=boot_n,
            bootstrap_block_size=block_size,
        )

        bench_report = None
        if also_bench and r.benchmark:
            bench_returns = slice_returns_by_date(r.benchmark, start_filter, end_filter)
            if bench_returns.size >= 30:
                bench_report = run_significance_check(
                    bench_returns,
                    n_trials=1,
                    sr_threshold_annual=sr_threshold,
                    bootstrap_n_iter=boot_n,
                    bootstrap_block_size=block_size,
                )

        table = Table(show_lines=True)
        table.add_column("指标", style="cyan")
        table.add_column("策略", style="green")
        if bench_report:
            table.add_column("基准 (^GSPC)", style="yellow")
        table.add_column("解读", style="dim")

        def fmt(v: float, fmt_str: str = "{:+.4f}") -> str:
            if v != v:  # NaN
                return "—"
            return fmt_str.format(v)

        rows = [
            ("样本天数 (T)", "{:d}".format(strat_report.n_obs), "样本量"),
            ("Sharpe (per-period)", fmt(strat_report.sr_per_period), "未年化"),
            ("Sharpe (annualized)", fmt(strat_report.sr_annualized), "× √252"),
            (
                "Skewness (γ_3)",
                fmt(strat_report.skewness),
                "≠ 0 → 收益分布偏斜",
            ),
            (
                "Kurtosis (γ_4, Pearson)",
                fmt(strat_report.kurtosis_pearson),
                "正态 = 3，> 5 偏厚尾",
            ),
            (
                f"PSR (vs SR* = {sr_threshold})",
                fmt(strat_report.psr, "{:.4f}"),
                "> 0.95 显著",
            ),
            (
                f"DSR (N = {n_trials})",
                fmt(strat_report.dsr, "{:.4f}"),
                "> 0.95 显著",
            ),
            (
                f"E[max SR_N] (annualized)",
                fmt(strat_report.expected_max_sr_annualized),
                "试错 N 次的期望最大 SR",
            ),
            (
                "Bootstrap CI 95% low (annual)",
                fmt(strat_report.bootstrap_ci_low_annualized),
                f"{strat_report.bootstrap_n_iter} 次, block={strat_report.bootstrap_block_size}",
            ),
            (
                "Bootstrap CI 95% high (annual)",
                fmt(strat_report.bootstrap_ci_high_annualized),
                "下界 < 0 = 不显著",
            ),
        ]

        for row in rows:
            label, strat_val, hint = row
            if bench_report:
                key = label.split(" ")[0]
                bench_val = self._lookup_bench(bench_report, label)
                table.add_row(label, strat_val, bench_val, hint)
            else:
                table.add_row(label, strat_val, hint)

        console.print(table)

        # 结论
        console.print()
        console.print("[bold]结论[/bold]")
        psr = strat_report.psr
        dsr = strat_report.dsr
        sr_ann = strat_report.sr_annualized
        ci_low = strat_report.bootstrap_ci_low_annualized
        verdicts = []
        if psr == psr:
            verdicts.append(
                f"  • PSR = {psr:.3f} {'✅' if psr > 0.95 else '⚠️ ' if psr > 0.80 else '❌'}"
                f"  → SR > {sr_threshold} 的概率"
            )
        if dsr == dsr:
            verdicts.append(
                f"  • DSR = {dsr:.3f} {'✅' if dsr > 0.95 else '⚠️ ' if dsr > 0.80 else '❌'}"
                f"  → 在试错 N={n_trials} 后仍显著的概率"
            )
        if ci_low == ci_low:
            verdicts.append(
                f"  • Bootstrap CI 95%: [{ci_low:+.3f}, {strat_report.bootstrap_ci_high_annualized:+.3f}] "
                f"{'✅' if ci_low > 0 else '❌ 下界 < 0'}"
            )
        for v in verdicts:
            console.print(v)

    @staticmethod
    def _lookup_bench(bench_report, label: str) -> str:
        """从 benchmark report 取对应字段（粗糙文本匹配）"""
        def fmt(v: float, fmt_str: str = "{:+.4f}") -> str:
            if v != v:
                return "—"
            return fmt_str.format(v)

        if "样本天数" in label:
            return f"{bench_report.n_obs}"
        if "per-period" in label:
            return fmt(bench_report.sr_per_period)
        if "annualized" in label and "Sharpe" in label:
            return fmt(bench_report.sr_annualized)
        if "Skewness" in label:
            return fmt(bench_report.skewness)
        if "Kurtosis" in label:
            return fmt(bench_report.kurtosis_pearson)
        if "PSR" in label:
            return fmt(bench_report.psr, "{:.4f}")
        if "DSR" in label:
            return "—"  # benchmark 无试错
        if "E[max" in label:
            return "—"
        if "Bootstrap CI 95% low" in label:
            return fmt(bench_report.bootstrap_ci_low_annualized)
        if "Bootstrap CI 95% high" in label:
            return fmt(bench_report.bootstrap_ci_high_annualized)
        return "—"
