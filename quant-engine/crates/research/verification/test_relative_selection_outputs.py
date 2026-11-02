"""Independent artifact assertions only; production research is entirely Rust.

Run from repository root: python3 -m unittest discover -s
quant-engine/crates/research/verification -p test_relative_selection_outputs.py -v
"""
import csv
import gzip
import json
import math
import unittest
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
OUT = ROOT / "output/a_relative_selection_v2"
CACHE = ROOT / "cache/a_relative_selection_v1"


class RelativeSelectionOutputs(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.report = json.loads((OUT / "report.json").read_text())
        with (OUT / "curves.csv").open() as stream:
            cls.curves = list(csv.DictReader(stream))
        with (OUT / "targets.csv").open() as stream:
            cls.targets = list(csv.DictReader(stream))
        with gzip.open(CACHE / "metadata.json.gz", "rt") as stream:
            cls.calendar = json.load(stream)["dates"]

    def test_complete_and_nonempty(self):
        self.assertEqual(self.report["status"], "complete")
        self.assertEqual(len(self.report["simulations"]), 6)
        self.assertGreater(self.report["data"]["rows"], 1_000_000)
        self.assertEqual(len(self.curves), len({r["date"] for r in self.curves}))
        self.assertGreater(len(self.targets), 30_000)

    def test_target_timing_and_risk_controls(self):
        groups = defaultdict(list)
        for row in self.targets:
            groups[row["signal_date"]].append(row)
        self.assertEqual(len(groups), self.report["data"]["monthly_signals"])
        for date, rows in groups.items():
            index = self.calendar.index(date)
            self.assertEqual(len(rows), len({r["code"] for r in rows}))
            self.assertGreaterEqual(len(rows), 200)
            sector = defaultdict(float)
            size = liquidity = 0.0
            for row in rows:
                self.assertEqual(row["execution_date"], self.calendar[index + 1])
                self.assertNotEqual(date[:7], row["execution_date"][:7])
                weight = float(row["weight"])
                benchmark = float(row["benchmark_weight"])
                self.assertAlmostEqual(benchmark, 1 / len(rows), places=10)
                self.assertGreaterEqual(weight, 0.2 / len(rows) - 1e-11)
                self.assertLessEqual(weight, 1.8 / len(rows) + 1e-11)
                active = weight - benchmark
                sector[row["industry"]] += active
                size += active * float(row["log_cap"])
                liquidity += active * float(row["log_liquidity"])
            self.assertAlmostEqual(sum(float(r["weight"]) for r in rows), 1, places=8)
            self.assertLess(max(map(abs, sector.values())), 1e-8)
            self.assertLess(abs(size), 1e-7)
            self.assertLess(abs(liquidity), 1e-7)

    def test_statistics_reconcile_with_daily_curves(self):
        primary = [r for r in self.curves if r["date"] <= "2025-12-31"]
        years = len(primary) / 252
        for summary in self.report["simulations"]:
            name = summary["name"]
            nav = [float(r[name]) for r in primary]
            self.assertAlmostEqual(summary["primary_total_return"], nav[-1] - 1, places=10)
            self.assertAlmostEqual(summary["primary_CAGR"], nav[-1] ** (1 / years) - 1, places=10)
            peak = 1.0
            worst = 0.0
            for value in nav:
                peak = max(peak, value)
                worst = min(worst, value / peak - 1)
            self.assertAlmostEqual(summary["primary_max_drawdown"], worst, places=10)
            base = 1.0
            for year in range(2022, 2027):
                end = [r for r in self.curves if r["date"].startswith(str(year))][-1]
                value = float(end[name])
                self.assertAlmostEqual(summary["annual_returns"][str(year)], value / base - 1, places=10)
                base = value
        last = primary[-1]
        relative = float(last["selection_1x"]) / float(last["benchmark_1x"])
        self.assertAlmostEqual(self.report["primary_relative_CAGR"], relative ** (1 / years) - 1, places=10)

    def test_actual_fills_lots_and_fees(self):
        scheduled = {r["execution_date"] for r in self.targets}
        for sim in self.report["simulations"]:
            multiplier = int(sim["name"][-2])
            with (OUT / f"trades_{sim['name']}.csv").open() as stream:
                rows = list(csv.DictReader(stream))
            self.assertEqual(len(rows), sim["trades_full"])
            fees_total = 0.0
            for row in rows:
                self.assertIn(row["date"], scheduled)
                shares, price = int(row["shares"]), float(row["price"])
                self.assertGreater(shares, 0)
                self.assertGreater(price, 0)
                gross, fees = float(row["gross"]), float(row["fees"])
                self.assertAlmostEqual(gross, shares * price, delta=0.001)
                expected = max(gross * 0.0003 * multiplier, 5 * multiplier)
                if row["side"] == "BUY":
                    self.assertEqual(shares % 100, 0)
                else:
                    self.assertEqual(row["side"], "SELL")
                    expected += gross * 0.001 * multiplier
                self.assertAlmostEqual(fees, expected, delta=0.0001)
                fees_total += fees
            self.assertAlmostEqual(fees_total, sim["fees_full"], delta=len(rows) * 0.000051)

    def test_fixed_pass_gate_and_cash(self):
        r = self.report
        passed = (r["primary_relative_CAGR"] > 0
                  and r["primary_positive_years"] >= 3
                  and r["primary_relative_CAGR_block95CI"][0] > 0
                  and r["double_cost_primary_relative_CAGR"] > 0
                  and r["primary_endpoint_stale_positions"] == 0)
        self.assertEqual(r["verdict"] == "provisional_support_requires_fresh_validation", passed)
        for row in self.curves:
            for key, value in row.items():
                if key != "date":
                    self.assertTrue(math.isfinite(float(value)))
                    if key.endswith("cash_weight"):
                        self.assertGreaterEqual(float(value), 0)
                        self.assertLessEqual(float(value), 1)


if __name__ == "__main__":
    unittest.main()
