"""
全因子分析（IC / Fama-MacBeth / 因子衰减）。

Usage:
    python3 manage.py factor_analysis --start 2012-01-01 --end 2025-12-31 --workers 6
"""

import subprocess
import sys
from pathlib import Path

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "全因子分析（IC / Fama-MacBeth / 衰减），委托 scripts/factor_analysis.py"

    def add_arguments(self, parser):
        parser.add_argument("--start", required=True, help="开始日期")
        parser.add_argument("--end", required=True, help="结束日期")
        parser.add_argument("--workers", type=int, default=6, help="并发 worker 数")
        parser.add_argument("--skip-fm", action="store_true", help="跳过 Fama-MacBeth")
        parser.add_argument("--skip-decay", action="store_true", help="跳过因子衰减")

    def handle(self, *args, **opts):
        script = Path(__file__).resolve().parent.parent.parent.parent / "scripts" / "factor_analysis.py"

        cmd = [
            sys.executable, str(script),
            "--start", opts["start"],
            "--end", opts["end"],
            "--workers", str(opts["workers"]),
        ]
        if opts["skip_fm"]:
            cmd.append("--skip-fm")
        if opts["skip_decay"]:
            cmd.append("--skip-decay")

        self.stdout.write(f"Running: {' '.join(cmd)}\n")
        subprocess.run(cmd, check=False)
