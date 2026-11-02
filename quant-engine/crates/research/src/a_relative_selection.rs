//! Pre-specified monthly price-signal experiment, independent of archived Cover work.
mod data;
use crate::Error;
use chrono::{Datelike, NaiveDate, Utc};
use data::{Data, Stock};
use quant_backtest::a_exec::{self, ACostConfig, QuoteSource, Side};
use quant_core::config::Config;
use quant_factors::a_share::cache::ABar;
use rayon::prelude::*;
use rustc_hash::FxHashMap;
use serde_json::{Value, json};
use std::{
    collections::{BTreeMap, HashMap},
    io::Write,
    path::Path,
};

const CAPITAL: f64 = 10_000_000.0;
#[derive(Clone)]
struct Member {
    stock: usize,
    industry: String,
    cap: f64,
    liquidity: f64,
    factors: [f64; 3],
}
struct Signal {
    observed: usize,
    execution: usize,
    members: Vec<Member>,
    weights: Vec<f64>,
    neutral: Vec<Vec<f64>>,
    exposures: [f64; 3],
}
fn valid(x: f64) -> bool {
    x.is_finite() && x > 0.0
}
fn mean(x: &[f64]) -> f64 {
    x.iter().sum::<f64>() / x.len() as f64
}
fn dot(a: &[f64], b: &[f64]) -> f64 {
    a.iter().zip(b).map(|(x, y)| x * y).sum()
}
fn rank(values: &[f64]) -> Vec<f64> {
    let mut order: Vec<_> = (0..values.len()).collect();
    order.sort_by(|&a, &b| values[a].total_cmp(&values[b]).then(a.cmp(&b)));
    let mut out = vec![0.0; values.len()];
    let mut i = 0;
    while i < order.len() {
        let mut end = i + 1;
        while end < order.len() && values[order[end]] == values[order[i]] {
            end += 1
        }
        for &j in &order[i..end] {
            out[j] = ((i + end - 1) as f64 / 2.0 + 0.5) / values.len() as f64 - 0.5;
        }
        i = end;
    }
    out
}
// Modified Gram-Schmidt: industry indicators + centered log cap/liquidity.
// The same projection is used for every signal, without looking at future returns.
fn basis(m: &[Member]) -> Vec<Vec<f64>> {
    let mut industries: BTreeMap<&str, Vec<usize>> = BTreeMap::new();
    for (i, r) in m.iter().enumerate() {
        industries.entry(&r.industry).or_default().push(i);
    }
    let mut columns = vec![];
    for ids in industries.values() {
        let mut x = vec![0.0; m.len()];
        for &i in ids {
            x[i] = 1.0 / (ids.len() as f64).sqrt();
        }
        columns.push(x);
    }
    for x in [
        m.iter().map(|r| r.cap).collect::<Vec<_>>(),
        m.iter().map(|r| r.liquidity).collect(),
    ] {
        let residual = project(&x, &columns);
        let norm = dot(&residual, &residual).sqrt();
        if norm > 1e-9 {
            columns.push(residual.iter().map(|v| v / norm).collect());
        }
    }
    columns
}
fn project(x: &[f64], basis: &[Vec<f64>]) -> Vec<f64> {
    let mut r = x.to_vec();
    // A second pass limits loss of orthogonality with nearly collinear exposures.
    for _ in 0..2 {
        for q in basis {
            let b = dot(&r, q);
            for (v, q) in r.iter_mut().zip(q) {
                *v -= b * q;
            }
        }
    }
    r
}
fn weights(score: &[f64]) -> Vec<f64> {
    let max = score.iter().map(|x| x.abs()).fold(0.0, f64::max);
    score
        .iter()
        .map(|s| (1.0 + if max > 1e-12 { 0.8 * s / max } else { 0.0 }) / score.len() as f64)
        .collect()
}
fn member(s: &Stock, k: usize, day: NaiveDate) -> Option<Member> {
    if k < 120
        || s.listed.is_none_or(|d| (day - d).num_days() < 365)
        || s.delisted.is_some_and(|d| d <= day)
    {
        return None;
    }
    let industry = s.industry(day)?.to_owned();
    let b = s.bars[k];
    let prices: Vec<_> = s.bars[k - 120..=k]
        .iter()
        .map(|b| b.close * b.adj)
        .collect();
    if prices.iter().any(|p| !valid(*p)) || !valid(b.cap) {
        return None;
    }
    let amounts: Vec<_> = s.bars[k - 19..=k]
        .iter()
        .map(|b| b.amount * 1000.0)
        .collect();
    if amounts.iter().any(|v| !valid(*v)) || mean(&amounts) < 20_000_000.0 {
        return None;
    }
    let returns: Vec<_> = prices[100..]
        .windows(2)
        .map(|w| (w[1] / w[0]).ln())
        .collect();
    let mu = mean(&returns);
    let vol = (returns.iter().map(|r| (r - mu).powi(2)).sum::<f64>() / 20.0).sqrt();
    Some(Member {
        stock: 0,
        industry,
        cap: b.cap.ln(),
        liquidity: mean(&amounts).ln(),
        factors: [
            -(prices[120] / prices[115] - 1.0),
            prices[115] / prices[0] - 1.0,
            -vol,
        ],
    })
}
fn signals(d: &Data) -> Result<Vec<Signal>, Error> {
    let dates: Vec<_> = (1..d.dates.len())
        .filter(|&i| d.dates[i].year() >= 2022 && d.dates[i].month() != d.dates[i - 1].month())
        .collect();
    let result: Vec<_> = dates
        .par_iter()
        .map(|&execution| {
            let k = execution - 1;
            let mut members: Vec<_> = d
                .stocks
                .iter()
                .enumerate()
                .filter_map(|(i, s)| {
                    member(s, k, d.dates[k]).map(|mut m| {
                        m.stock = i;
                        m
                    })
                })
                .collect();
            members.sort_by(|a, b| b.cap.total_cmp(&a.cap).then(a.stock.cmp(&b.stock)));
            members.truncate(800);
            let mut count = HashMap::new();
            for m in &members {
                *count.entry(m.industry.clone()).or_insert(0) += 1;
            }
            members.retain(|m| count[&m.industry] >= 5);
            if members.len() < 200 {
                return Err(format!(
                    "{}: only {} eligible industry-covered stocks",
                    d.dates[k],
                    members.len()
                )
                .into());
            }
            let controls = basis(&members);
            let neutral: Vec<_> = (0..3)
                .map(|f| {
                    project(
                        &rank(&members.iter().map(|m| m.factors[f]).collect::<Vec<_>>()),
                        &controls,
                    )
                })
                .collect();
            let score: Vec<_> = (0..members.len())
                .map(|i| neutral.iter().map(|x| x[i]).sum::<f64>() / 3.0)
                .collect();
            let weights = weights(&score);
            let n = members.len() as f64;
            let active: Vec<_> = weights.iter().map(|w| w - 1.0 / n).collect();
            let cap = dot(&active, &members.iter().map(|r| r.cap).collect::<Vec<_>>());
            let liquidity = dot(
                &active,
                &members.iter().map(|r| r.liquidity).collect::<Vec<_>>(),
            );
            let mut sectors = HashMap::<&str, f64>::new();
            for (m, a) in members.iter().zip(&active) {
                *sectors.entry(&m.industry).or_default() += a;
            }
            let industry = sectors.values().map(|v| v.abs()).fold(0.0, f64::max);
            if (weights.iter().sum::<f64>() - 1.0).abs() > 1e-9
                || cap.abs() > 1e-9
                || liquidity.abs() > 1e-9
                || industry > 1e-9
            {
                return Err("neutrality constraint failed".into());
            }
            Ok(Signal {
                observed: k,
                execution,
                members,
                weights,
                neutral,
                exposures: [industry, cap, liquidity],
            })
        })
        .collect::<Vec<Result<Signal, Error>>>();
    result.into_iter().collect()
}
struct Quotes(FxHashMap<String, ABar>);
impl QuoteSource for Quotes {
    fn bar(&self, c: &str) -> Option<&ABar> {
        self.0.get(c)
    }
    fn is_st(&self, _: &str) -> bool {
        false
    }
}
fn quote(b: data::Bar) -> Option<ABar> {
    if !valid(b.open) || !valid(b.pre_close) || !valid(b.adj) {
        return None;
    }
    // Synthetic open-only bar: full-day high/low/volume cannot affect open fills.
    Some(ABar {
        open: b.open,
        high: b.open,
        low: b.open,
        close: b.open,
        pre_close: b.pre_close,
        pct_chg: (b.open / b.pre_close - 1.0) * 100.0,
        vol: 1.0,
        amount: 0.0,
        adj_factor: b.adj,
        turnover_rate: f64::NAN,
        pe_ttm: f64::NAN,
        pb: f64::NAN,
        ps_ttm: f64::NAN,
        dv_ttm: f64::NAN,
        total_mv: f64::NAN,
        circ_mv: f64::NAN,
    })
}
struct Simulation {
    name: String,
    nav: Vec<f64>,
    cash_ratio: Vec<f64>,
    fees: f64,
    turnover: f64,
    fills: usize,
    blocked: usize,
    capped: usize,
    stale_days: usize,
    stale_counts: Vec<usize>,
    year_end_stale: BTreeMap<i32, Vec<Value>>,
    terminal_stale: f64,
    corporate_actions: usize,
    writeoffs: usize,
    trade_csv: String,
}
fn simulate(
    d: &Data,
    signals: &[Signal],
    active: bool,
    cost_multiplier: f64,
) -> Result<Simulation, Error> {
    let name = format!(
        "{}_{}x",
        if active { "selection" } else { "benchmark" },
        cost_multiplier
    );
    let mut result = Simulation {
        name,
        nav: vec![],
        cash_ratio: vec![],
        fees: 0.0,
        turnover: 0.0,
        fills: 0,
        blocked: 0,
        capped: 0,
        stale_days: 0,
        stale_counts: vec![],
        year_end_stale: BTreeMap::new(),
        terminal_stale: 0.0,
        corporate_actions: 0,
        writeoffs: 0,
        trade_csv: "date,code,side,shares,price,gross,fees\n".to_owned(),
    };
    let mut costs = ACostConfig::default();
    costs.initial_capital = CAPITAL;
    costs.buy_commission = 0.0003 * cost_multiplier;
    costs.sell_commission = costs.buy_commission;
    costs.stamp_tax = 0.001 * cost_multiplier;
    costs.slippage = 0.001 * cost_multiplier;
    costs.min_commission = 5.0 * cost_multiplier;
    costs.lot_size = 100;
    let index: HashMap<_, _> = d
        .stocks
        .iter()
        .enumerate()
        .map(|(i, s)| (s.code.as_str(), i))
        .collect();
    let schedules: HashMap<_, _> = signals.iter().map(|s| (s.execution, s)).collect();
    let mut positions = FxHashMap::<String, i64>::default();
    let mut cash = CAPITAL;
    let mut marks = vec![f64::NAN; d.stocks.len()];
    let mut adj = vec![f64::NAN; d.stocks.len()];
    for k in signals[0].execution..d.dates.len() {
        for i in 0..d.stocks.len() {
            let s = &d.stocks[i];
            let b = s.bars[k];
            if s.delisted.is_some_and(|day| day <= d.dates[k]) {
                if positions.remove(&s.code).is_some() {
                    result.writeoffs += 1;
                }
                continue;
            }
            if valid(adj[i]) && valid(b.adj) && (adj[i] - b.adj).abs() > 1e-10 {
                let ratio = b.adj / adj[i];
                if let Some(held) = positions.get_mut(&s.code) {
                    // Total-return approximation, NOT an actual corporate action ledger.
                    if !valid(b.open) {
                        return Err(format!(
                            "corporate action without open {} {}",
                            s.code, d.dates[k]
                        )
                        .into());
                    }
                    let shares = *held as f64 * ratio;
                    *held = shares.floor() as i64;
                    cash += (shares - shares.floor()) * b.open;
                    result.corporate_actions += 1;
                }
                if valid(marks[i]) {
                    marks[i] /= ratio;
                }
            }
            if valid(b.adj) {
                adj[i] = b.adj;
            }
        }
        if let Some(signal) = schedules.get(&k) {
            let mut quotes = Quotes(FxHashMap::default());
            for s in &d.stocks {
                if s.delisted.is_none_or(|day| day > d.dates[k]) {
                    if let Some(b) = quote(s.bars[k]) {
                        quotes.0.insert(s.code.clone(), b);
                    }
                }
            }
            let value = cash
                + positions
                    .iter()
                    .map(|(c, q)| {
                        let i = index[c.as_str()];
                        let price = quotes.bar(c).map(|b| b.open).unwrap_or(marks[i]);
                        *q as f64 * price
                    })
                    .sum::<f64>();
            if !valid(value) {
                return Err("invalid open portfolio valuation".into());
            }
            let target: FxHashMap<_, _> = signal
                .members
                .iter()
                .enumerate()
                .map(|(j, m)| {
                    (
                        d.stocks[m.stock].code.clone(),
                        if active {
                            signal.weights[j]
                        } else {
                            1.0 / signal.members.len() as f64
                        },
                    )
                })
                .collect();
            let mut orders = a_exec::plan_orders(&positions, &target, value, &quotes, &costs);
            orders.sort_by(|a, b| {
                (a.side == Side::Buy)
                    .cmp(&(b.side == Side::Buy))
                    .then(a.ts_code.cmp(&b.ts_code))
            });
            for mut order in orders {
                let i = index[order.ts_code.as_str()];
                let s = &d.stocks[i];
                let Some(q) = quotes.bar(&order.ts_code) else {
                    result.blocked += 1;
                    continue;
                };
                let gap = q.open / q.pre_close - 1.0;
                // Historical ST not available: conservative directional 4.5% opening gate.
                if (order.side == Side::Buy && gap >= 0.045)
                    || (order.side == Side::Sell && gap <= -0.045)
                {
                    result.blocked += 1;
                    continue;
                }
                let amounts: Vec<_> = s.bars[k - 20..k]
                    .iter()
                    .map(|b| b.amount * 1000.0)
                    .collect();
                if amounts.iter().any(|v| !valid(*v)) {
                    result.blocked += 1;
                    continue;
                }
                let cap = a_exec::round_to_lot((0.01 * mean(&amounts) / q.open) as i64, 100);
                if order.shares > cap {
                    order.shares = cap;
                    result.capped += 1;
                }
                if order.side == Side::Buy {
                    // Corporate-action credits can leave odd-lot holdings. The shared
                    // planner rounds target holdings, not the incremental order.
                    order.shares = a_exec::round_to_lot(order.shares, costs.lot_size);
                    while order.shares > 0 {
                        let gross = order.shares as f64 * q.open * (1.0 + costs.slippage);
                        if gross + a_exec::calc_buy_fees(gross, &costs) <= cash {
                            break;
                        }
                        order.shares -= 100;
                    }
                }
                if order.shares <= 0 {
                    result.blocked += 1;
                    continue;
                }
                let fills =
                    a_exec::execute_orders(&[order], &mut positions, &mut cash, &quotes, &costs);
                if fills.is_empty() {
                    result.blocked += 1;
                }
                for f in fills {
                    result.fees += f.fees;
                    result.turnover += f.gross / value;
                    result.fills += 1;
                    use std::fmt::Write;
                    writeln!(
                        result.trade_csv,
                        "{},{},{},{},{:.8},{:.4},{:.4}",
                        d.dates[k],
                        f.ts_code,
                        f.side.as_str(),
                        f.shares,
                        f.price,
                        f.gross,
                        f.fees
                    )?;
                }
            }
        }
        for (i, s) in d.stocks.iter().enumerate() {
            if valid(s.bars[k].close) {
                marks[i] = s.bars[k].close;
            }
        }
        let mut stale = 0.0;
        let mut stale_count = 0;
        let mut value = cash;
        for (c, q) in &positions {
            let i = index[c.as_str()];
            let holding = *q as f64 * marks[i];
            value += holding;
            if !valid(d.stocks[i].bars[k].close) {
                stale += holding;
                result.stale_days += 1;
                stale_count += 1;
            }
        }
        if !valid(value) || cash < -1e-6 {
            return Err("invalid close NAV/cash".into());
        }
        result.nav.push(value / CAPITAL);
        result.cash_ratio.push(cash / value);
        result.terminal_stale = stale / value;
        result.stale_counts.push(stale_count);
        if k + 1 == d.dates.len() || d.dates[k + 1].year() != d.dates[k].year() {
            let mut rows = vec![];
            for (code, shares) in &positions {
                let i = index[code.as_str()];
                let stock = &d.stocks[i];
                if !valid(stock.bars[k].close) {
                    let last = (0..k)
                        .rev()
                        .find(|&j| valid(stock.bars[j].close))
                        .map(|j| d.dates[j]);
                    rows.push(json!({"code":code,"last_quote":last,"shares":shares,"mark":marks[i],"nav_weight":*shares as f64*marks[i]/value}));
                }
            }
            rows.sort_by(|a, b| a["code"].as_str().cmp(&b["code"].as_str()));
            result.year_end_stale.insert(d.dates[k].year(), rows);
        }
    }
    if result.fills == 0 {
        return Err("no executed trades".into());
    }
    Ok(result)
}
fn corr(a: &[f64], b: &[f64]) -> Option<f64> {
    if a.len() < 10 {
        return None;
    }
    let am = mean(a);
    let bm = mean(b);
    let a: Vec<_> = a.iter().map(|v| v - am).collect();
    let b: Vec<_> = b.iter().map(|v| v - bm).collect();
    let den = (dot(&a, &a) * dot(&b, &b)).sqrt();
    (den > 1e-12).then(|| dot(&a, &b) / den)
}
fn drawdown(nav: &[f64]) -> f64 {
    let mut peak = 1.0_f64;
    let mut dd = 0.0_f64;
    for &v in nav {
        peak = peak.max(v);
        dd = dd.min(v / peak - 1.0);
    }
    dd
}
fn year_returns(dates: &[NaiveDate], nav: &[f64]) -> BTreeMap<i32, f64> {
    let mut out = BTreeMap::new();
    let mut base = 1.0;
    for i in 0..dates.len() {
        if i + 1 == dates.len() || dates[i + 1].year() != dates[i].year() {
            out.insert(dates[i].year(), nav[i] / base - 1.0);
            base = nav[i];
        }
    }
    out
}
fn bootstrap(months: &[f64]) -> [f64; 2] {
    // Circular moving blocks, 3 months, deterministic seed; inference is descriptive.
    let mut seed = 20261008u64;
    let mut samples = Vec::with_capacity(2000);
    for _ in 0..2000 {
        let mut sampled = vec![];
        while sampled.len() < months.len() {
            seed = seed.wrapping_mul(6364136223846793005).wrapping_add(1);
            let start = (seed >> 32) as usize % months.len();
            for j in 0..3 {
                if sampled.len() < months.len() {
                    sampled.push(months[(start + j) % months.len()]);
                }
            }
        }
        samples.push((mean(&sampled) * 12.0).exp() - 1.0);
    }
    samples.sort_by(f64::total_cmp);
    [samples[49], samples[1949]]
}
fn freeze(output: &Path) -> Result<(), Error> {
    let protocol = json!({"version":1,"warmup":"2021","primary":"2022-01-01..2025-12-31","diagnostic":"2026-01-01..2026-09-30","historical_data_previously_seen":true,
        "universe":"SH60/SZ00, listed >=365 calendar days, 121 consecutive valid adjusted closes, 20-day mean amount >=CNY20m, historical industry known, top800 float cap then remove industries with <5 names",
        "signals":["negative close-to-close 5 session return","adjusted close(t-5)/close(t-120)-1","negative population stdev of 20 daily log returns"],
        "model":"equal weight of three cross-sectional centered percentile ranks, residualized on historical industry indicators + log float cap + log 20-day mean CNY amount; no fitted parameters or winner selection",
        "weights":"(1 + 0.8 * composite / max(abs(composite))) / N; exact target industry, log cap, log liquidity neutrality vs same-universe equal weight",
        "execution":"month end close signal, next session open, monthly only, 100-share buy lots, sells before buys, CNY10m, no leverage, no forced terminal liquidation",
        "costs":{"commission_each_side":0.0003,"sell_tax_conservative_all_dates":0.001,"slippage_each_side":0.001,"minimum_commission_CNY":5,"multipliers":[0,1,2]},
        "liquidity":"orders capped at 1% of prior20-day mean turnover; no current-day high/low/volume used; opening directional 4.5% gate due to missing historical ST; blocked remainder not retried until next scheduled rebalance",
        "corporate_actions":"adj_factor ratio synthetic share credits, fractional part cashed at open; approximate total-return ledger",
        "missing":"missing quotes cannot trade; carry last mark; delisted holdings written off on recorded delisting date; report stale and coverage gaps",
        "benchmark":"same-universe monthly equal weight, identical costs/gates/lots/capital; NOT CSI index tracking",
        "pass":"primary relative CAGR>0, >=3 of 4 positive annual relative returns, 3-month-block bootstrap 95% lower bound>0, double-cost relative CAGR>0, no primary terminal stale holdings; support is provisional because survivor/ST/corporate-action/PIT gaps remain",
        "uncertainty":"2000 deterministic circular block bootstraps of primary monthly log relative returns; not a correction for previous strategy exploration"});
    std::fs::create_dir_all(output)?;
    let path = output.join("protocol.json");
    if path.exists() {
        let old: Value = serde_json::from_slice(&std::fs::read(&path)?)?;
        if old != protocol {
            return Err("protocol differs: use a new version/output, never overwrite historical specification".into());
        }
    } else {
        crate::atomic_json(&path, &protocol)?;
    }
    Ok(())
}
pub async fn run(config: &Config, cache: &Path, output: &Path) -> Result<(), Error> {
    freeze(output)?;
    let lock = output.join("run.lock");
    let _lock = std::fs::OpenOptions::new()
        .create_new(true)
        .write(true)
        .open(&lock)?;
    let result = run_inner(config, cache, output).await;
    if let Err(e) = &result {
        crate::atomic_json(
            &output.join("status.json"),
            &json!({"status":"failed","error":e.to_string()}),
        )?;
    }
    std::fs::remove_file(lock)?;
    result
}
async fn run_inner(config: &Config, cache: &Path, output: &Path) -> Result<(), Error> {
    let began = std::time::Instant::now();
    crate::atomic_json(
        &output.join("status.json"),
        &json!({"status":"running","started":Utc::now().to_rfc3339()}),
    )?;
    let d = data::load(config, cache).await?;
    let pool = rayon::ThreadPoolBuilder::new().num_threads(6).build()?;
    let signals = pool.install(|| signals(&d))?;
    if signals.is_empty() {
        return Err("no monthly signals".into());
    }
    tracing::info!(
        months = signals.len(),
        "Frozen risk-neutral monthly targets ready"
    );
    let mut target = std::io::BufWriter::new(std::fs::File::create(output.join("targets.csv"))?);
    writeln!(
        target,
        "signal_date,execution_date,code,industry,log_cap,log_liquidity,reversal,momentum,low_vol,weight,benchmark_weight"
    )?;
    for s in &signals {
        for (i, m) in s.members.iter().enumerate() {
            writeln!(
                target,
                "{},{},{},{},{:.12},{:.12},{:.12},{:.12},{:.12},{:.12},{:.12}",
                d.dates[s.observed],
                d.dates[s.execution],
                d.stocks[m.stock].code,
                m.industry,
                m.cap,
                m.liquidity,
                m.factors[0],
                m.factors[1],
                m.factors[2],
                s.weights[i],
                1.0 / s.members.len() as f64
            )?;
        }
    }
    target.flush()?;
    let variants = [
        (false, 0.0),
        (true, 0.0),
        (false, 1.0),
        (true, 1.0),
        (false, 2.0),
        (true, 2.0),
    ];
    let sims = pool
        .install(|| {
            variants
                .par_iter()
                .map(|&(a, c)| simulate(&d, &signals, a, c))
                .collect::<Vec<_>>()
        })
        .into_iter()
        .collect::<Result<Vec<_>, _>>()?;
    let dates = &d.dates[signals[0].execution..];
    let primary_end = dates
        .iter()
        .rposition(|d| d.year() == 2025)
        .ok_or("missing primary period")?;
    let years = (primary_end + 1) as f64 / 252.0;
    let mut summaries = vec![];
    for sim in &sims {
        std::fs::write(
            output.join(format!("trades_{}.csv", sim.name)),
            &sim.trade_csv,
        )?;
        summaries.push(json!({"name":sim.name,"primary_total_return":sim.nav[primary_end]-1.0,"primary_CAGR":sim.nav[primary_end].powf(1.0/years)-1.0,"primary_max_drawdown":drawdown(&sim.nav[..=primary_end]),"annual_returns":year_returns(dates,&sim.nav),"full_total_return":sim.nav.last().unwrap()-1.0,"fees_full":sim.fees,"trades_full":sim.fills,"gross_turnover_full":sim.turnover,"blocked_orders_full":sim.blocked,"capacity_capped_orders_full":sim.capped,"stale_holding_days_full":sim.stale_days,"terminal_stale_weight_full":sim.terminal_stale,"corporate_action_approximations_full":sim.corporate_actions,"delisting_writeoffs_full":sim.writeoffs,"mean_cash_weight_full":mean(&sim.cash_ratio)}));
    }
    let mut curves = std::io::BufWriter::new(std::fs::File::create(output.join("curves.csv"))?);
    write!(curves, "date")?;
    for s in &sims {
        write!(curves, ",{},{}_cash_weight", s.name, s.name)?;
    }
    writeln!(curves)?;
    for (k, day) in dates.iter().enumerate() {
        write!(curves, "{day}")?;
        for s in &sims {
            write!(curves, ",{:.12},{:.12}", s.nav[k], s.cash_ratio[k])?;
        }
        writeln!(curves)?;
    }
    curves.flush()?;
    let benchmark = &sims[2];
    let strategy = &sims[3];
    let relative: Vec<_> = strategy
        .nav
        .iter()
        .zip(&benchmark.nav)
        .map(|(a, b)| a / b)
        .collect();
    let annual = year_returns(dates, &relative);
    let mut monthly = vec![];
    let mut prev = 1.0;
    for k in 0..=primary_end {
        if k == primary_end || dates[k + 1].month() != dates[k].month() {
            monthly.push((relative[k] / prev).ln());
            prev = relative[k];
        }
    }
    let ci = bootstrap(&monthly);
    let relative_cagr = relative[primary_end].powf(1.0 / years) - 1.0;
    let double_cagr = (sims[5].nav[primary_end] / sims[4].nav[primary_end]).powf(1.0 / years) - 1.0;
    let positives = annual
        .iter()
        .filter(|(y, r)| **y <= 2025 && **r > 0.0)
        .count();
    let stale_primary = sims[2].stale_counts[primary_end] + sims[3].stale_counts[primary_end];
    let pass = relative_cagr > 0.0
        && double_cagr > 0.0
        && positives >= 3
        && ci[0] > 0.0
        && stale_primary == 0;
    let mut ic_rows = vec![];
    for pair in signals.windows(2) {
        let s = &pair[0];
        let next = &pair[1];
        let mut labels = vec![];
        let mut indices = vec![];
        for (i, m) in s.members.iter().enumerate() {
            let stock = &d.stocks[m.stock];
            let a = stock.bars[s.execution];
            let b = stock.bars[next.execution];
            if valid(a.open * a.adj) && valid(b.open * b.adj) {
                indices.push(i);
                labels.push(b.open * b.adj / (a.open * a.adj) - 1.0);
            }
        }
        let members: Vec<_> = indices.iter().map(|&i| s.members[i].clone()).collect();
        let controls = basis(&members);
        let neutral_labels = project(&rank(&labels), &controls);
        let raw: Vec<_> = (0..3)
            .map(|f| {
                corr(
                    &rank(&members.iter().map(|m| m.factors[f]).collect::<Vec<_>>()),
                    &rank(&labels),
                )
            })
            .collect();
        let neutral: Vec<_> = (0..3)
            .map(|f| {
                corr(
                    &project(
                        &indices.iter().map(|&i| s.neutral[f][i]).collect::<Vec<_>>(),
                        &controls,
                    ),
                    &neutral_labels,
                )
            })
            .collect();
        ic_rows.push(json!({"signal_date":d.dates[s.observed],"entry":d.dates[s.execution],"exit":d.dates[next.execution],"members":s.members.len(),"labels":labels.len(),"raw_rank_ic":raw,"partial_rank_ic":neutral}));
    }
    crate::atomic_json(&output.join("factor_diagnostics.json"), &json!(ic_rows))?;
    let mut factor_summary = vec![];
    for (f, name) in ["reversal_5", "momentum_120_skip5", "low_vol_20"]
        .iter()
        .enumerate()
    {
        let mut raw = vec![];
        let mut partial = vec![];
        for row in &ic_rows {
            if row["exit"].as_str().is_some_and(|s| s <= "2025-12-31") {
                if let Some(v) = row["raw_rank_ic"][f].as_f64() {
                    raw.push(v);
                }
                if let Some(v) = row["partial_rank_ic"][f].as_f64() {
                    partial.push(v);
                }
            }
        }
        factor_summary.push(json!({"signal":name,"primary_complete_months":partial.len(),"mean_raw_rank_ic":if raw.is_empty(){None}else{Some(mean(&raw))},"mean_partial_rank_ic":if partial.is_empty(){None}else{Some(mean(&partial))}}));
    }
    crate::atomic_json(&output.join("factor_summary.json"), &json!(factor_summary))?;
    crate::atomic_json(
        &output.join("stale_positions.json"),
        &json!(
            sims.iter()
                .map(|s| (&s.name, &s.year_end_stale))
                .collect::<BTreeMap<_, _>>()
        ),
    )?;
    let report = json!({"status":"complete","verdict":if pass{"provisional_support_requires_fresh_validation"}else{"no_robust_net_edge"},"primary_relative_CAGR":relative_cagr,"primary_relative_total":relative[primary_end]-1.0,"primary_relative_CAGR_block95CI":ci,"primary_positive_years":positives,"annual_relative_returns":annual,"double_cost_primary_relative_CAGR":double_cagr,"primary_endpoint_stale_positions":stale_primary,"simulations":summaries,
        "data":{"stocks":d.stocks.len(),"rows":d.rows,"calendar_days":d.dates.len(),"first":d.dates.first(),"last":d.dates.last(),"monthly_signals":signals.len(),"minimum_universe":signals.iter().map(|s|s.members.len()).min(),"maximum_universe":signals.iter().map(|s|s.members.len()).max(),"missing_industry_start_rows":d.missing_industry_start,"max_target_control_error":signals.iter().flat_map(|s|s.exposures).map(f64::abs).fold(0.0,f64::max)},
        "limitations":["historical intervals previously inspected, not a pristine holdout","historical ST unavailable; conservative open gate is only a proxy","delisted-stock database coverage not independently certified","industry membership effective dates are not vendor publication timestamps","adjustment-factor share conversion is not an exact corporate-action cash ledger","target exposure neutrality can drift through rounding, blocked fills and subsequent returns","day bars cannot establish executable auction volume; costs/capacity are assumptions","IC uses available endpoint quotes only and may have missing-label bias; IC is not portfolio P&L"],"elapsed_seconds":began.elapsed().as_secs()});
    crate::atomic_json(&output.join("report.json"), &report)?;
    crate::atomic_json(
        &output.join("status.json"),
        &json!({"status":"complete","ended":Utc::now().to_rfc3339()}),
    )?;
    println!("{}", serde_json::to_string_pretty(&report)?);
    Ok(())
}
#[cfg(test)]
mod tests {
    use super::*;
    fn fixture() -> (Data, Vec<Signal>) {
        let start = NaiveDate::from_ymd_opt(2022, 1, 1).unwrap();
        let dates: Vec<_> = (0..150)
            .map(|i| start + chrono::Duration::days(i))
            .collect();
        let stocks = (0..2)
            .map(|i| Stock {
                code: format!("60000{i}.SH"),
                listed: Some(start - chrono::Duration::days(1000)),
                delisted: None,
                industries: vec![(start, None, "bank".into())],
                bars: vec![
                    data::Bar {
                        open: 10.0,
                        close: 10.0,
                        pre_close: 10.0,
                        amount: 1_000_000.0,
                        adj: 1.0,
                        cap: 100_000.0
                    };
                    150
                ],
            })
            .collect();
        let members = (0..2)
            .map(|i| Member {
                stock: i,
                industry: "bank".into(),
                cap: 1.0,
                liquidity: 1.0,
                factors: [0.0; 3],
            })
            .collect();
        (
            Data {
                dates,
                stocks,
                rows: 300,
                missing_industry_start: 0,
            },
            vec![Signal {
                observed: 120,
                execution: 121,
                members,
                weights: vec![0.5; 2],
                neutral: vec![vec![0.0; 2]; 3],
                exposures: [0.0; 3],
            }],
        )
    }
    #[test]
    fn execution_costs_and_benchmark_share_accounting() {
        let (d, s) = fixture();
        let free = simulate(&d, &s, true, 0.0).unwrap();
        let net = simulate(&d, &s, true, 1.0).unwrap();
        let benchmark = simulate(&d, &s, false, 1.0).unwrap();
        assert!((free.nav.last().unwrap() - 1.0).abs() < 1e-12);
        assert!(net.nav.last().unwrap() < free.nav.last().unwrap());
        assert_eq!(net.nav, benchmark.nav);
        assert!(net.fills > 0 && net.fees > 0.0);
    }
    #[test]
    fn split_and_missing_quote_do_not_destroy_position_value() {
        let (mut d, s) = fixture();
        for stock in &mut d.stocks {
            for b in &mut stock.bars[130..] {
                b.open = 5.0;
                b.close = 5.0;
                b.pre_close = 5.0;
                b.adj = 2.0;
            }
            stock.bars[140].close = f64::NAN;
        }
        let sim = simulate(&d, &s, true, 0.0).unwrap();
        assert!((sim.nav.last().unwrap() - 1.0).abs() < 1e-12);
        assert_eq!(sim.corporate_actions, 2);
        assert_eq!(sim.stale_days, 2);
    }
    #[test]
    fn rebalance_after_fractional_adjustment_buys_whole_lots() {
        let (mut d, mut s) = fixture();
        s[0].weights = vec![0.4, 0.6];
        for stock in &mut d.stocks {
            for b in &mut stock.bars[130..] {
                b.adj = 1.0001234;
                b.open = 10.0 / b.adj;
                b.close = b.open;
                b.pre_close = b.open;
            }
        }
        s.push(Signal {
            observed: 140,
            execution: 141,
            members: s[0].members.clone(),
            weights: vec![0.5; 2],
            neutral: vec![vec![0.0; 2]; 3],
            exposures: [0.0; 3],
        });
        let sim = simulate(&d, &s, true, 0.0).unwrap();
        let mut buys = 0;
        for line in sim.trade_csv.lines().skip(1) {
            let fields: Vec<_> = line.split(',').collect();
            if fields[2] == "BUY" {
                buys += 1;
                assert_eq!(fields[3].parse::<i64>().unwrap() % 100, 0);
            }
        }
        assert!(buys > 2);
    }
    #[test]
    fn residual_targets_control_industry_size_liquidity() {
        let members: Vec<_> = (0..60)
            .map(|i| Member {
                stock: i,
                industry: format!("sector{}", i % 3),
                cap: (i as f64 + 2.0).ln(),
                liquidity: ((i * 7 % 31) as f64 + 5.0).ln(),
                factors: [0.0; 3],
            })
            .collect();
        let x: Vec<_> = (0..60).map(|i| (i as f64 * 0.7).sin()).collect();
        let q = basis(&members);
        let score = project(&x, &q);
        let w = weights(&score);
        let a: Vec<_> = w.iter().map(|w| w - 1.0 / 60.0).collect();
        for control in q {
            assert!(dot(&a, &control).abs() < 1e-12);
        }
        assert!((w.iter().sum::<f64>() - 1.0).abs() < 1e-12);
        assert!(
            w.iter()
                .all(|w| *w >= 0.2 / 60.0 - 1e-12 && *w <= 1.8 / 60.0 + 1e-12)
        );
    }
    #[test]
    fn ranks_average_ties() {
        assert_eq!(rank(&[2.0, 1.0, 2.0, 3.0]), vec![0.0, -0.375, 0.0, 0.375]);
    }
    #[test]
    fn signal_cannot_see_future_bars() {
        let day = NaiveDate::from_ymd_opt(2022, 12, 30).unwrap();
        let mut s = Stock {
            code: "600000.SH".into(),
            listed: Some(NaiveDate::from_ymd_opt(2000, 1, 1).unwrap()),
            delisted: None,
            industries: vec![(
                NaiveDate::from_ymd_opt(2000, 1, 1).unwrap(),
                None,
                "bank".into(),
            )],
            bars: (0..140)
                .map(|i| data::Bar {
                    close: 10.0 + i as f64 / 100.0,
                    open: 10.0,
                    pre_close: 10.0,
                    amount: 50_000.0,
                    adj: 1.0,
                    cap: 100_000.0,
                })
                .collect(),
        };
        let old = member(&s, 120, day).unwrap();
        for b in &mut s.bars[121..] {
            b.close = 999999.0;
            b.cap = 1.0;
            b.amount = 0.0;
        }
        assert_eq!(old.factors, member(&s, 120, day).unwrap().factors);
    }
    #[test]
    fn open_quotes_do_not_use_close() {
        let mut b = data::Bar {
            open: 10.0,
            close: 1.0,
            pre_close: 10.0,
            amount: 0.0,
            adj: 1.0,
            cap: 1.0,
        };
        let q = quote(b).unwrap();
        b.close = 1000.0;
        assert_eq!(q.high, quote(b).unwrap().high);
        assert_eq!(q.close, 10.0);
    }
    #[test]
    fn relative_ci_has_known_sign() {
        let ci = bootstrap(&vec![0.001; 48]);
        assert!(ci[0] > 0.0);
        assert!((ci[1] - (0.012_f64.exp() - 1.0)).abs() < 1e-12);
    }
}
