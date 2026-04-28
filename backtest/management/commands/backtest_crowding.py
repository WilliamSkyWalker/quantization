"""跑策略 NAV 的 crowding 监控（skill 第 10 节简化版本）。

Usage:
    python3 manage.py backtest_crowding --result-id 13
    python3 manage.py backtest_crowding --result-id 13 --export crowding_id13.csv
"""

import logging

from django.core.management.base import BaseCommand
from rich.console import Console
from rich.table import Table

from backtest.models import BacktestResult
from backtest.services.us_crowding import (
    CrowdingThresholds,
    compute_crowding_timeseries,
    summarize_crowding,
)

logger = logging.getLogger(__name__)
console = Console()


class Command(BaseCommand):
    help = "策略 crowding 监控：4 个 NAV-only 指标 + 复合 alert"

    def add_arguments(self, parser):
        parser.add_argument("--result-id", type=int, required=True, help="backtest_result.id")
        parser.add_argument("--export", default=None, help="导出 CSV 路径")
        parser.add_argument(
            "--kurtosis-threshold", type=float, default=10.0,
            help="60D kurtosis alert 阈值"
        )
        parser.add_argument(
            "--autocorr-threshold", type=float, default=0.10,
            help="60D 1D autocorr alert 阈值"
        )
        parser.add_argument(
            "--sharpe-pct-threshold", type=float, default=0.95,
            help="252D Sharpe 历史分位 alert 阈值"
        )
        parser.add_argument(
            "--nasdaq-spy-diff", type=float, default=0.20,
            help="NASDAQ corr − SPY corr 差异 alert 阈值"
        )

    def handle(self, *args, **opts):
        result_id = opts["result_id"]
        try:
            r = BacktestResult.objects.get(id=result_id)
        except BacktestResult.DoesNotExist:
            self.stderr.write(self.style.ERROR(f"backtest_result id={result_id} 不存在"))
            return

        thresholds = CrowdingThresholds(
            kurtosis_60d=opts["kurtosis_threshold"],
            autocorr_60d=opts["autocorr_threshold"],
            sharpe_252d_percentile=opts["sharpe_pct_threshold"],
            nasdaq_spy_corr_diff=opts["nasdaq_spy_diff"],
        )

        console.print(
            f"[bold]Backtest #{result_id}[/bold] crowding 检查 — "
            f"{r.start_date} → {r.end_date}"
        )
        console.print(
            f"[dim]阈值: kurt > {thresholds.kurtosis_60d} | "
            f"autocorr > {thresholds.autocorr_60d:+.2f} | "
            f"Sharpe pct > {thresholds.sharpe_252d_percentile:.0%} | "
            f"NASDAQ-SPY corr > {thresholds.nasdaq_spy_corr_diff:+.2f}[/dim]"
        )
        console.print()

        df = compute_crowding_timeseries(r.nav, thresholds)
        if df.empty:
            self.stderr.write(self.style.ERROR("NAV 解析为空"))
            return

        summary = summarize_crowding(df)
        if summary.get("empty"):
            self.stderr.write(self.style.ERROR("无足够数据计算指标"))
            return

        # 当前指标表
        cur_table = Table(title="最新值", show_lines=True)
        cur_table.add_column("指标", style="cyan")
        cur_table.add_column("值", style="green")
        cur_table.add_column("阈值", style="dim")
        cur_table.add_column("命中", style="bold")

        rows_cur = [
            (
                "60D Kurtosis",
                f"{summary['last_kurtosis_60d']:+.2f}",
                f"> {thresholds.kurtosis_60d}",
                "🚨" if summary["last_kurtosis_60d"] > thresholds.kurtosis_60d else "  ",
            ),
            (
                "60D 1D Autocorr",
                f"{summary['last_autocorr_60d']:+.4f}",
                f"> {thresholds.autocorr_60d:+.2f}",
                "🚨" if summary["last_autocorr_60d"] > thresholds.autocorr_60d else "  ",
            ),
            (
                "252D Sharpe (annual)",
                f"{summary['last_sharpe_252d']:+.3f}",
                "—",
                "  ",
            ),
            (
                "252D Sharpe pct rank",
                f"{summary['last_sharpe_252d_pct_rank']:.2%}",
                f"> {thresholds.sharpe_252d_percentile:.0%}",
                "🚨" if summary["last_sharpe_252d_pct_rank"] > thresholds.sharpe_252d_percentile else "  ",
            ),
            (
                "NASDAQ − SPY corr",
                f"{summary['last_nasdaq_minus_spy_corr']:+.4f}",
                f"> {thresholds.nasdaq_spy_corr_diff:+.2f}",
                "🚨" if summary["last_nasdaq_minus_spy_corr"] > thresholds.nasdaq_spy_corr_diff else "  ",
            ),
        ]
        for row in rows_cur:
            cur_table.add_row(*row)
        console.print(cur_table)

        console.print()
        console.print(
            f"[bold]最新日期 {summary['last_date']}: "
            f"{summary['last_n_alerts']}/4 项命中"
            f"{' — ⚠️ 高拥挤' if summary['last_crowding_high'] else ''}[/bold]"
        )
        console.print(
            f"[dim]历史拥挤天数: {summary['n_high_crowding_days']}"
            f" ({summary['high_crowding_pct_of_history']:.1%} of {summary['n_obs_with_full_metrics']} 有效观测日)[/dim]"
        )
        console.print()

        # Top kurtosis 期
        kurt_table = Table(title="Top 5 Kurtosis 峰值", show_lines=False)
        kurt_table.add_column("日期", style="cyan")
        kurt_table.add_column("60D Kurtosis", style="green")
        kurt_table.add_column("命中数", style="bold")
        for row in summary["top_kurtosis_dates"]:
            d = row["date"]
            if hasattr(d, "strftime"):
                d = d.strftime("%Y-%m-%d")
            kurt_table.add_row(str(d), f"{row['kurtosis_60d']:+.2f}", f"{int(row['n_alerts'])}/4")
        console.print(kurt_table)

        # Top Sharpe pct rank
        shp_table = Table(title="Top 5 Sharpe 历史高位", show_lines=False)
        shp_table.add_column("日期", style="cyan")
        shp_table.add_column("252D Sharpe", style="green")
        shp_table.add_column("Pct Rank", style="yellow")
        shp_table.add_column("命中数", style="bold")
        for row in summary["top_sharpe_dates"]:
            d = row["date"]
            if hasattr(d, "strftime"):
                d = d.strftime("%Y-%m-%d")
            shp_table.add_row(
                str(d),
                f"{row['sharpe_252d']:+.3f}",
                f"{row['sharpe_252d_pct_rank']:.2%}",
                f"{int(row['n_alerts'])}/4",
            )
        console.print(shp_table)

        if opts["export"]:
            df.to_csv(opts["export"])
            console.print(f"[dim]已导出: {opts['export']}[/dim]")
