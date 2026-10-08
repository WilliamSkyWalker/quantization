"""Independent tests for the Rust rolling-pair experiment; no data acquisition."""
import csv
import gzip
import json
import math
import os
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[4]
OUT = ROOT / os.environ.get('PAIR_VALIDATION_OUTPUT', 'output/a_pair_validation_v4')
CACHE = ROOT / 'cache/a_pair_validation_v1'
METHODS = ['daily_net', 'universal_net', 'monthly_net', 'hold_net',
           'daily_double_cost', 'hold_double_cost', 'daily_ideal', 'hold_ideal']


def read_gz(path):
    with gzip.open(path, 'rt') as f:
        return json.load(f)


class IndependentOutputTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.report = json.loads((OUT / 'report.json').read_text())
        cls.folds = [read_gz(OUT / f'fold_{y}.json.gz') for y in range(2022, 2027)]

    def test_source_snapshot(self):
        snapshot = json.loads((OUT / 'source.json').read_text())
        for key, path in [('research', 'research/src/a_pair_validation.rs'),
                          ('execution', 'backtest/src/universal.rs'),
                          ('shared_execution', 'backtest/src/a_exec.rs')]:
            self.assertEqual(snapshot[key], (ROOT / 'quant-engine/crates' / path).read_text())

    def test_historical_eligibility_and_selection(self):
        for f in self.folds:
            y = f['year']
            u = read_gz(CACHE / f'universe_{y}.json.gz')
            s = json.loads((OUT / f'selection_{y}.json').read_text())
            self.assertLess(max(u['train_dates']), min(u['test_dates']))
            self.assertEqual(s['training_end'], max(u['train_dates']))
            self.assertEqual(len(s['training']), 1770)
            ranked = sorted(range(1770), key=lambda i: (-s['training'][i]['score'], i))
            used, chosen = set(), []
            for i in ranked:
                t = s['training'][i]
                if t['i'] in used or t['j'] in used:
                    continue
                chosen.append(i)
                used.update([t['i'], t['j']])
                if len(chosen) == 10:
                    break
            self.assertEqual(chosen, s['selected_indices'])
            self.assertEqual([s['training'][i] for i in chosen], f['selected'])
            for group in s['random_indices']:
                assets = [a for i in group for a in [s['training'][i]['i'], s['training'][i]['j']]]
                self.assertEqual(len(set(assets)), 20)
            for member in u['members']:
                rows = read_gz(CACHE / f"{member['ts_code']}_2020_20260930.json.gz")
                train = [r for r in rows if min(u['train_dates']) <= r['trade_date'] <= max(u['train_dates'])]
                valid = [r for r in train if all(r[k] is not None and r[k] > 0 for k in
                         ['open', 'high', 'low', 'close', 'pre_close', 'vol', 'adj_factor'])]
                self.assertGreaterEqual(len(valid), math.ceil(.95 * len(u['train_dates'])))
                self.assertLessEqual(train[-1]['close'], 100)
                amount = [r['amount'] for r in train if r['amount'] is not None]
                self.assertAlmostEqual(sum(amount) / len(amount), member['liquidity'], places=5)

    def test_metrics_and_cost_benchmarks(self):
        for i, m in enumerate(METHODS):
            base, peak, dd, n = 1., 1., 0., 0
            for f in self.folds[:4]:
                for v in f['selected_curves'][i]:
                    self.assertTrue(math.isfinite(v) and v > 0)
                    nav = base * v
                    peak = max(peak, nav)
                    dd = min(dd, nav / peak - 1)
                    n += 1
                base *= f['selected_curves'][i][-1]
            metric = self.report['full_year_metrics'][m]
            self.assertAlmostEqual(base - 1, metric['total_return'], places=12)
            self.assertAlmostEqual(dd, metric['max_drawdown'], places=12)
            self.assertAlmostEqual(base ** (252 / n) - 1, metric['annualized_252_sessions'], places=12)
        for f in self.folds:
            with (OUT / f"all_pairs_{f['year']}.csv").open() as handle:
                rows = list(csv.DictReader(handle))
            chosen = [r for r in rows if r['selected'] == 'true']
            self.assertEqual(len(rows), 1770)
            self.assertEqual(len(chosen), 10)
            self.assertGreater(sum(int(r['fills']) for r in chosen), 0)
            self.assertEqual(sum(r['terminal_risk'] == 'true' for r in chosen), f['selected_terminal_risks'])
            for i, m in enumerate(METHODS):
                self.assertAlmostEqual(sum(1 + float(r[m]) for r in chosen) / 10,
                                       f['selected_curves'][i][-1], places=12)
            selection = json.loads((OUT / f"selection_{f['year']}.json").read_text())
            for k, indices in enumerate(selection['random_indices']):
                for i, m in enumerate(METHODS):
                    self.assertAlmostEqual(sum(1 + float(rows[j][m]) for j in indices) / 10,
                                           f['random_curves'][k][i][-1], places=12)
        with (OUT / 'summary.csv').open() as handle:
            for row in csv.DictReader(handle):
                y = next(x for x in self.report['years'] if str(x['year']) == row['year'])
                m = row['method']
                b = 'hold_double_cost' if 'double_cost' in m else ('hold_ideal' if 'ideal' in m else 'hold_net')
                self.assertAlmostEqual(float(row['hold_return']), y['returns'][b], places=12)

    def test_verdict_and_bootstrap(self):
        full = self.folds[:4]
        for i, v in enumerate(self.report['verdicts']):
            excess = sum(math.log(f['selected_curves'][i][-1] / f['selected_curves'][3][-1]) for f in full)
            positive = sum(f['selected_curves'][i][-1] > f['selected_curves'][3][-1] for f in full)
            random = [sum(math.log(f['random_curves'][k][i][-1] / f['random_curves'][k][3][-1])
                          for f in full) for k in range(100)]
            self.assertAlmostEqual(excess, v['cumulative_paired_log_excess'], places=12)
            self.assertEqual(positive, v['positive_full_years'])
            self.assertEqual(sum(x < excess for x in random) / 100, v['random_excess_percentile'])
            supported = (excess > 0 and positive >= 3 and v['annual_log_excess_95pct_block_bootstrap'][0] > 0
                         and excess > sorted(random)[94] and v['terminal_risks'] == 0)
            self.assertEqual(supported, v['supported'])
            monthly = {}
            for f in full:
                prev = [1., 1.]
                for d, a, b in zip(f['dates'], f['selected_curves'][i], f['selected_curves'][3]):
                    month = d[:7]
                    monthly[month] = monthly.get(month, 0) + math.log(a / prev[0]) - math.log(b / prev[1])
                    prev = [a, b]
            values = [monthly[k] for k in sorted(monthly)]
            self.assertEqual(len(values), 48)
            state, samples, mask = 20261008, [], (1 << 64) - 1
            for _ in range(2000):
                draw = []
                while len(draw) < len(values):
                    state ^= (state << 13) & mask
                    state ^= state >> 7
                    state ^= (state << 17) & mask
                    start = state % len(values)
                    draw.extend(values[(start + k) % len(values)] for k in range(min(3, len(values) - len(draw))))
                samples.append(sum(draw) / len(draw) * 12)
            samples.sort()
            for j, q in enumerate([.025, .975]):
                self.assertAlmostEqual(samples[math.floor(1999 * q + .5)],
                                       v['annual_log_excess_95pct_block_bootstrap'][j], places=12)

    def test_ideal_expert_identity_from_raw_prices(self):
        for f in self.folds:
            daily, hold, up = [], [], []
            for pair in f['selected']:
                codes = [f['universe'][pair[k]] for k in ['i', 'j']]
                data = [{r['trade_date']: r for r in read_gz(CACHE / f'{c}_2020_20260930.json.gz')
                         if all(r[k] is not None and r[k] > 0 for k in
                                ['open', 'high', 'low', 'close', 'pre_close', 'vol', 'adj_factor'])} for c in codes]
                self.assertTrue(all(f['dates'][0] in d for d in data))
                u = read_gz(CACHE / f"universe_{f['year']}.json.gz")
                experts, last = [1.] * 101, [None, None]
                for d in u['train_dates']:
                    now = [data[i][d]['close'] * data[i][d]['adj_factor'] if d in data[i] else last[i] for i in range(2)]
                    if all(x is not None for x in last):
                        r = [now[i] / last[i] for i in range(2)]
                        experts = [e * (j / 100 * r[0] + (1 - j / 100) * r[1]) for j, e in enumerate(experts)]
                    last = now
                prior, dwealth, legs = sum(experts), 1., [.5, .5]
                last = [data[i][f['dates'][0]]['open'] * data[i][f['dates'][0]]['adj_factor'] for i in range(2)]
                for dt in f['dates']:
                    now = [data[i][dt]['close'] * data[i][dt]['adj_factor'] if dt in data[i] else last[i] for i in range(2)]
                    r = [now[i] / last[i] for i in range(2)]
                    experts = [e * (j / 100 * r[0] + (1 - j / 100) * r[1]) for j, e in enumerate(experts)]
                    dwealth *= sum(r) / 2
                    legs = [legs[i] * r[i] for i in range(2)]
                    last = now
                daily.append(dwealth)
                hold.append(sum(legs))
                up.append(sum(experts) / prior)
            self.assertAlmostEqual(sum(daily) / 10, f['selected_curves'][6][-1], places=10)
            self.assertAlmostEqual(sum(hold) / 10, f['selected_curves'][7][-1], places=10)
            self.assertAlmostEqual(sum(up) / 10, f['selected_universal_ideal'][-1], places=10)


if __name__ == '__main__':
    unittest.main(verbosity=2)
