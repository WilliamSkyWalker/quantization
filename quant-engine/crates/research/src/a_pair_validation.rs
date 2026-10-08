//! Frozen, historical-only pair selection with annual forward evaluation.
use crate::Error;
use chrono::{Datelike, NaiveDate};
use quant_backtest::{
    a_exec::{self, ACostConfig},
    universal::{self, ABar, Day, Strategy},
};
use quant_core::config::Config;
use rayon::prelude::*;
use serde::{Deserialize, Serialize};
use serde_json::json;
use std::{
    collections::{BTreeMap, BTreeSet},
    io::Write,
    path::Path,
};

const GRID: usize = 101;
const PAIRS: usize = 10;
const RANDOM: usize = 100;
const CAPITAL: f64 = 100_000.0; // Ten independent sleeves, total CNY 1m.
const METHODS: [&str; 8] = [
    "daily_net",
    "universal_net",
    "monthly_net",
    "hold_net",
    "daily_double_cost",
    "hold_double_cost",
    "daily_ideal",
    "hold_ideal",
];

#[derive(Clone, Serialize, Deserialize, sqlx::FromRow)]
struct Raw {
    ts_code: String,
    trade_date: NaiveDate,
    open: Option<f64>,
    high: Option<f64>,
    low: Option<f64>,
    close: Option<f64>,
    pre_close: Option<f64>,
    vol: Option<f64>,
    amount: Option<f64>,
    adj_factor: Option<f64>,
}
impl Raw {
    fn bar(&self) -> Option<ABar> {
        let fields = [
            self.open,
            self.high,
            self.low,
            self.close,
            self.pre_close,
            self.vol,
            self.adj_factor,
        ];
        if fields
            .iter()
            .any(|v| v.is_none_or(|x| !x.is_finite() || x <= 0.0))
        {
            return None;
        }
        let [open, high, low, close, pre_close, vol, adj_factor] = fields.map(Option::unwrap);
        Some(ABar {
            open,
            high,
            low,
            close,
            pre_close,
            vol,
            adj_factor,
            amount: self.amount.unwrap_or(f64::NAN),
            pct_chg: (close / pre_close - 1.0) * 100.0,
            turnover_rate: f64::NAN,
            pe_ttm: f64::NAN,
            pb: f64::NAN,
            ps_ttm: f64::NAN,
            dv_ttm: f64::NAN,
            total_mv: f64::NAN,
            circ_mv: f64::NAN,
        })
    }
}
#[derive(Clone, Serialize, Deserialize, sqlx::FromRow)]
struct Member {
    ts_code: String,
    liquidity: f64,
}
#[derive(Clone, Serialize, Deserialize)]
struct Universe {
    year: i32,
    train_dates: Vec<NaiveDate>,
    test_dates: Vec<NaiveDate>,
    members: Vec<Member>,
}
type Prices = BTreeMap<String, BTreeMap<NaiveDate, ABar>>;

#[derive(Clone, Serialize, Deserialize)]
struct Training {
    i: usize,
    j: usize,
    score: f64,
    divergence: f64,
    correlation: f64,
    relative_vol: f64,
    train_return: f64,
    train_best_single: f64,
}
struct Evaluation {
    curves: Vec<Vec<f64>>,
    universal_ideal: Vec<f64>,
    missing: usize,
    blocked: usize,
    fills: usize,
    terminal_risk: bool,
}
#[derive(Clone, Serialize, Deserialize)]
struct Fold {
    year: i32,
    dates: Vec<NaiveDate>,
    universe: Vec<String>,
    selected: Vec<Training>,
    selected_curves: Vec<Vec<f64>>,
    divergence_curves: Vec<Vec<f64>>,
    random_curves: Vec<Vec<Vec<f64>>>,
    selected_universal_ideal: Vec<f64>,
    pair_win_rate: f64,
    all_pair_win_rate: f64,
    selected_terminal_risks: usize,
    all_terminal_risks: usize,
    selected_missing_days: usize,
    selected_fills: usize,
}
fn date(y: i32, m: u32, d: u32) -> NaiveDate {
    NaiveDate::from_ymd_opt(y, m, d).unwrap()
}
fn save(path: &Path, value: &impl Serialize) -> Result<(), Error> {
    let temp = path.with_extension("tmp");
    let file = std::fs::File::create(&temp)?;
    let mut gz = flate2::write::GzEncoder::new(file, flate2::Compression::default());
    serde_json::to_writer(&mut gz, value)?;
    gz.flush()?;
    gz.finish()?.sync_all()?;
    std::fs::rename(temp, path)?;
    Ok(())
}
fn read<T: serde::de::DeserializeOwned>(path: &Path) -> Result<T, Error> {
    Ok(serde_json::from_reader(flate2::read::GzDecoder::new(
        std::fs::File::open(path)?,
    ))?)
}
fn freeze(path: &Path, value: &serde_json::Value) -> Result<(), Error> {
    if path.exists() {
        if serde_json::from_slice::<serde_json::Value>(&std::fs::read(path)?)? != *value {
            return Err("Protocol differs; use a fresh output directory".into());
        }
    } else {
        std::fs::write(path, serde_json::to_vec_pretty(value)?)?;
    }
    Ok(())
}

fn cache_complete(cache: &Path) -> Result<bool, Error> {
    if !cache.join("calendar.json.gz").exists() || !cache.join("delist_dates.json.gz").exists() {
        return Ok(false);
    }
    for year in 2022..=2026 {
        let path = cache.join(format!("universe_{year}.json.gz"));
        if !path.exists() {
            return Ok(false);
        }
        let u: Universe = read(&path)?;
        if u.year != year {
            return Err("Cached universe year mismatch".into());
        }
        if u.members.iter().any(|m| {
            !cache
                .join(format!("{}_2020_20260930.json.gz", m.ts_code))
                .exists()
        }) {
            return Ok(false);
        }
    }
    Ok(true)
}

pub async fn run(config: &Config, cache: &Path, output: &Path) -> Result<(), Error> {
    let began = std::time::Instant::now();
    std::fs::create_dir_all(cache)?;
    std::fs::create_dir_all(output)?;
    let lock = std::fs::OpenOptions::new()
        .create(true)
        .truncate(false)
        .write(true)
        .open(output.join(".lock"))?;
    lock.try_lock()?;
    let mut costs = ACostConfig::from_a_share(&config.a_share);
    costs.initial_capital = CAPITAL;
    if costs.lot_size != 100
        || [
            costs.buy_commission,
            costs.sell_commission,
            costs.stamp_tax,
            costs.slippage,
            costs.min_commission,
        ]
        .iter()
        .any(|v| !v.is_finite() || *v < 0.0)
        || costs.slippage >= 0.1
    {
        return Err("Invalid execution parameters".into());
    }
    let protocol = json!({"version":1,"train_years":2,"evaluation_full_years":[2022,2023,2024,2025],"partial_diagnostic_end":"2026-09-30",
        "universe":"Each cutoff: SH60/SZ00 main-board stocks, first/last training quotes, >=95% valid training OHLC/pre_close/volume/adj_factor coverage, mean training amount >=100000 thousand CNY, last training price <=100 CNY. Top 60 by mean two-year amount, code tie-break; no current listing/status/industry filter.",
        "selection":"Rank all pairs by two-year net daily 50/50 log wealth minus maximum net single-asset log wealth; greedily take 10 disjoint pairs even when scores negative. No future eligibility check.",
        "secondary_selection":"Rank by mean Jensen log-diversification gap, same disjoint-pair rule; diagnostic only",
        "random":"100 deterministic shuffled matchings of 10 disjoint pairs from same yearly universe; seed 20261008 + year*1000 + sample",
        "training_score_execution":"Same open execution and estimated terminal liquidation charge as forward tests; no full-period optimum fitting",
        "capital":"10 sleeves of CNY100000, total CNY1000000, reset annually; concatenated normalized annual curves are a research index, not continuous-capital lot-accurate trading",
        "universal":"101 equal-prior experts, seeded using only preceding two years; previous-close weights executed at next open; no test-window reset/optimization",
        "ideal":"Fractional frictionless first test open to close, thereafter close-to-close; rebalance at closes; same-period hold comparator; ideal universal has exact expert-mixture identity",
        "costs":{"commission_buy":costs.buy_commission,"commission_sell":costs.sell_commission,"stamp_tax":costs.stamp_tax,"slippage":costs.slippage,"minimum_commission":costs.min_commission,"lot":costs.lot_size},
        "verdict":"Primary daily net selected strategy: >0 total paired log excess over hold, positive in >=3/4 full years, 95% moving-block bootstrap lower bound >0 (3-month blocks, 2000 draws), paired log excess above 95th percentile of 100 random matchings, no selected terminal-data risks. Universal evaluated separately with same hurdles. Otherwise NOT SUPPORTED; not proof of impossibility.",
        "limits":["Historical forward validation, not a pristine unseen holdout: earlier research has viewed portions of 2022-2026.","Current stored historical prices may omit delisted firms; no current-survivor filter does not prove source completeness.","Historical ST/limit prices unavailable: pair-wide conservative 4.5% opening gate; no same-day high/low lookahead.","Adjustment factors proxy reinvested total return via synthetic shares, not full corporate-action accounting.","Missing quotes stay stale and block pair trading; terminal missing or in-window delisting flagged, retained in denominators, and blocks a positive verdict.","Annual terminal liquidation is an estimated close-value cost, not guaranteed executable fills; no market impact; fixed configured stamp tax throughout.","Only four full forward years; correlated pairs; secondary strategies and 2026 are descriptive, not independent confirmations."]});
    freeze(&output.join("protocol.json"), &protocol)?;
    freeze(
        &output.join("source.json"),
        &json!({"research":include_str!("a_pair_validation.rs"),"execution":include_str!("../../backtest/src/universal.rs"),"shared_execution":include_str!("../../backtest/src/a_exec.rs")}),
    )?;
    let pool = if cache_complete(cache)? {
        tracing::info!("Complete input snapshot: offline replay without database connection");
        None
    } else {
        Some(quant_db::pool::create_pool(&config.database.url(), &config.database.schema, 4).await?)
    };
    let calendar_file = cache.join("calendar.json.gz");
    let dates: Vec<NaiveDate> = if calendar_file.exists() {
        read(&calendar_file)?
    } else {
        let v = quant_db::queries::a_read::get_a_trade_cal(
            pool.as_ref()
                .ok_or("Input cache disappeared during offline replay")?,
            "SSE",
            date(2020, 1, 1),
            date(2026, 9, 30),
        )
        .await?
        .into_iter()
        .map(|r| r.cal_date)
        .collect::<Vec<_>>();
        save(&calendar_file, &v)?;
        v
    };
    if dates.len() < 1600 || dates.windows(2).any(|w| w[0] >= w[1]) {
        return Err("Incomplete/unsorted calendar".into());
    }
    let mut universes = Vec::new();
    // Five bounded queries, historical price availability only.
    let mut tasks = tokio::task::JoinSet::new();
    let query_slots = std::sync::Arc::new(tokio::sync::Semaphore::new(4));
    for year in 2022..=2026 {
        let train: Vec<_> = dates
            .iter()
            .copied()
            .filter(|d| d.year() >= year - 2 && d.year() < year)
            .collect();
        let test: Vec<_> = dates.iter().copied().filter(|d| d.year() == year).collect();
        if train.len() < 450 || test.len() < 150 {
            return Err(format!("Insufficient calendar {year}").into());
        }
        let path = cache.join(format!("universe_{year}.json.gz"));
        let pool = pool.clone();
        let query_slots = query_slots.clone();
        tasks.spawn(async move {
            if path.exists(){return read::<Universe>(&path);}
            let _slot = query_slots.acquire_owned().await?;
            let sql="SELECT ts_code, AVG(amount) AS liquidity FROM a_daily_price WHERE trade_date>=? AND trade_date<=? AND (ts_code LIKE '60%.SH' OR ts_code LIKE '00%.SZ') GROUP BY ts_code HAVING SUM(open>0 AND high>0 AND low>0 AND close>0 AND pre_close>0 AND vol>0 AND adj_factor>0)>=? AND MAX(CASE WHEN trade_date=? THEN close END)>0 AND MAX(CASE WHEN trade_date=? THEN close END)>0 AND MAX(CASE WHEN trade_date=? THEN close END)<=100 AND AVG(amount)>=100000 ORDER BY liquidity DESC, ts_code LIMIT 60";
            let members=sqlx::query_as::<_,Member>(sql).bind(train[0]).bind(*train.last().unwrap()).bind((train.len() as f64*0.95).ceil() as i64).bind(train[0]).bind(*train.last().unwrap()).bind(*train.last().unwrap()).fetch_all(pool.as_ref().ok_or("Input cache disappeared during offline replay")?).await?;
            let universe=Universe{year,train_dates:train,test_dates:test,members};save(&path,&universe)?;Ok::<_,Error>(universe)
        });
    }
    while let Some(result) = tasks.join_next().await {
        let u = result??;
        tracing::info!(
            year = u.year,
            stocks = u.members.len(),
            "Training-only universe fixed"
        );
        if u.members.len() < PAIRS * 2 {
            return Err("Fewer than 20 eligible stocks".into());
        }
        universes.push(u);
    }
    universes.sort_by_key(|u| u.year);
    let codes: BTreeSet<_> = universes
        .iter()
        .flat_map(|u| u.members.iter().map(|m| m.ts_code.clone()))
        .collect();
    let metadata_file = cache.join("delist_dates.json.gz");
    let delist: BTreeMap<String, NaiveDate> = if metadata_file.exists() {
        read(&metadata_file)?
    } else {
        let m = quant_db::queries::a_read::get_all_a_stocks(
            pool.as_ref()
                .ok_or("Input cache disappeared during offline replay")?,
        )
        .await?
        .into_iter()
        .filter_map(|s| s.delist_date.map(|d| (s.ts_code, d)))
        .collect();
        save(&metadata_file, &m)?;
        m
    };
    let mut prices = Prices::new();
    let codes: Vec<_> = codes.into_iter().collect();
    let mut invalid = 0usize;
    for batch in codes.chunks(4) {
        let mut tasks = tokio::task::JoinSet::new();
        for code in batch {
            let code = code.clone();
            let pool = pool.clone();
            let path = cache.join(format!("{code}_2020_20260930.json.gz"));
            tasks.spawn(async move {
                let rows:Vec<Raw>=if path.exists(){read(&path)?}else{
                    let r=sqlx::query_as::<_,Raw>("SELECT ts_code,trade_date,open,high,low,close,pre_close,vol,amount,adj_factor FROM a_daily_price WHERE ts_code=? AND trade_date>='2020-01-01' AND trade_date<='2026-09-30' ORDER BY trade_date").bind(&code).fetch_all(pool.as_ref().ok_or("Input cache disappeared during offline replay")?).await?;save(&path,&r)?;r
                };Ok::<_,Error>((code,rows))
            });
        }
        while let Some(result) = tasks.join_next().await {
            let (code, rows) = result??;
            let mut bars = BTreeMap::new();
            let mut seen = BTreeSet::new();
            for r in rows {
                if r.ts_code != code || !seen.insert(r.trade_date) {
                    return Err("Wrong code or duplicate cache row".into());
                }
                if let Some(b) = r.bar() {
                    bars.insert(r.trade_date, b);
                } else {
                    invalid += 1;
                }
            }
            prices.insert(code, bars);
        }
    }
    if let Some(pool) = pool {
        pool.close().await;
    }
    tracing::info!(
        stocks = prices.len(),
        invalid_rows = invalid,
        seconds = began.elapsed().as_secs_f64(),
        "Input snapshot loaded"
    );
    std::fs::write(
        output.join("data_manifest.json"),
        serde_json::to_vec_pretty(
            &json!({"cache":std::fs::canonicalize(cache)?,"stocks":prices.len(),"invalid_rows_treated_missing":invalid,"universes":universes,"delist_dates":delist}),
        )?,
    )?;
    let workers = std::thread::available_parallelism()?.get().min(8);
    let threads = rayon::ThreadPoolBuilder::new()
        .num_threads(workers)
        .build()?;
    let mut folds = Vec::new();
    for u in &universes {
        let path = output.join(format!("fold_{}.json.gz", u.year));
        let fold = if path.exists() {
            read(&path)?
        } else {
            let f = threads.install(|| evaluate_fold(u, &prices, &delist, &costs, output))?;
            save(&path, &f)?;
            f
        };
        tracing::info!(
            year = u.year,
            seconds = began.elapsed().as_secs_f64(),
            "Forward fold complete"
        );
        folds.push(fold);
    }
    report(&folds, output, &protocol)?;
    println!(
        "Validation completed in {:.1}s: {}",
        began.elapsed().as_secs_f64(),
        output.join("report.html").display()
    );
    Ok(())
}

fn days(dates: &[NaiveDate], a: &str, b: &str, prices: &Prices) -> Vec<Day> {
    dates
        .iter()
        .map(|d| Day {
            date: *d,
            bars: [prices[a].get(d).cloned(), prices[b].get(d).cloned()],
        })
        .collect()
}

// Cost at the final close: a conservative notional charge for annual reset.
fn curve(result: universal::Result, cost: &ACostConfig) -> Vec<f64> {
    let last = result.points.last().unwrap();
    let first = last.nav * last.actual_first;
    let second = (last.nav - last.cash - first).max(0.0);
    let liquidation = [first, second]
        .iter()
        .filter(|v| **v > 0.0)
        .map(|v| v * cost.slippage + a_exec::calc_sell_fees(v * (1.0 - cost.slippage), cost))
        .sum::<f64>();
    let mut nav: Vec<_> = result
        .points
        .iter()
        .map(|p| p.nav / cost.initial_capital)
        .collect();
    *nav.last_mut().unwrap() -= liquidation / cost.initial_capital;
    assert!(nav.iter().all(|v| v.is_finite() && *v > 0.0));
    nav
}
fn ending(v: &[f64]) -> f64 {
    *v.last().unwrap()
}

fn train(i: usize, j: usize, u: &Universe, prices: &Prices, cost: &ACostConfig) -> Training {
    let codes = [u.members[i].ts_code.clone(), u.members[j].ts_code.clone()];
    let bars = days(&u.train_dates, &codes[0], &codes[1], prices);
    let net = ending(&curve(
        universal::simulate(&bars, &codes, cost, Strategy::Fixed(0.5, 1), GRID),
        cost,
    ));
    let best = [0.0, 1.0]
        .iter()
        .map(|w| {
            ending(&curve(
                universal::simulate(&bars, &codes, cost, Strategy::Hold(*w), GRID),
                cost,
            ))
        })
        .fold(0.0, f64::max);
    let (rel, _, _) = relatives(&bars, false);
    let n = rel.len() as f64;
    let mean = [0, 1].map(|i| rel.iter().map(|r| r[i] - 1.0).sum::<f64>() / n);
    let var = [0, 1].map(|i| {
        rel.iter()
            .map(|r| (r[i] - 1.0 - mean[i]).powi(2))
            .sum::<f64>()
            / n
    });
    let cov = rel
        .iter()
        .map(|r| (r[0] - 1.0 - mean[0]) * (r[1] - 1.0 - mean[1]))
        .sum::<f64>()
        / n;
    let div = rel
        .iter()
        .map(|r| ((r[0] + r[1]) / 2.0).ln() - 0.5 * (r[0].ln() + r[1].ln()))
        .sum::<f64>()
        / n
        * 252.0;
    Training {
        i,
        j,
        score: (net / best).ln(),
        divergence: div,
        correlation: if var[0] * var[1] > 0.0 {
            cov / (var[0] * var[1]).sqrt()
        } else {
            0.0
        },
        relative_vol: ((var[0] + var[1] - 2.0 * cov).max(0.0) * 252.0).sqrt(),
        train_return: net - 1.0,
        train_best_single: best - 1.0,
    }
}

/// Returns, expert state and most recent adjusted closes; no access beyond bars.
fn relatives(bars: &[Day], first_open: bool) -> (Vec<[f64; 2]>, Vec<f64>, [f64; 2]) {
    let mut last = [0.0; 2];
    let mut logs = vec![0.0; GRID];
    let mut ret = Vec::new();
    if first_open {
        for i in 0..2 {
            last[i] = bars[0].bars[i]
                .as_ref()
                .map_or(0.0, |b| b.open * b.adj_factor);
        }
    }
    for day in bars {
        let mut now = last;
        for i in 0..2 {
            if let Some(b) = &day.bars[i] {
                now[i] = b.close * b.adj_factor;
            }
        }
        if last.iter().all(|v| *v > 0.0) && now.iter().all(|v| *v > 0.0) {
            let r = [now[0] / last[0], now[1] / last[1]];
            universal::learn(&mut logs, r);
            ret.push(r);
        } else if first_open {
            ret.push([1.0, 1.0]);
        }
        last = now;
    }
    (ret, logs, last)
}

fn ideal(bars: &[Day], seed: &[f64]) -> (Vec<f64>, Vec<f64>, Vec<f64>) {
    let (returns, _, _) = relatives(bars, true);
    assert_eq!(returns.len(), bars.len());
    let mut logs = seed.to_vec();
    let (mut daily, mut up) = (1.0, 1.0);
    let mut legs = [0.5, 0.5];
    let (mut d, mut h, mut p) = (Vec::new(), Vec::new(), Vec::new());
    for r in returns {
        let w = universal::mixture(&logs);
        up *= w * r[0] + (1.0 - w) * r[1];
        daily *= 0.5 * (r[0] + r[1]);
        for i in 0..2 {
            legs[i] *= r[i];
        }
        universal::learn(&mut logs, r);
        d.push(daily);
        h.push(legs[0] + legs[1]);
        p.push(up);
    }
    (d, h, p)
}

fn evaluate(
    t: &Training,
    u: &Universe,
    prices: &Prices,
    delist: &BTreeMap<String, NaiveDate>,
    cost: &ACostConfig,
) -> Evaluation {
    let codes = [
        u.members[t.i].ts_code.clone(),
        u.members[t.j].ts_code.clone(),
    ];
    let train = days(&u.train_dates, &codes[0], &codes[1], prices);
    let (_, seed, last) = relatives(&train, false);
    let bars = days(&u.test_dates, &codes[0], &codes[1], prices);
    let mut curves = Vec::new();
    let (mut missing, mut blocked, mut fills) = (0, 0, 0);
    for (k, s) in [
        Strategy::Fixed(0.5, 1),
        Strategy::Universal,
        Strategy::Fixed(0.5, 21),
        Strategy::Hold(0.5),
    ]
    .iter()
    .enumerate()
    {
        let r = universal::simulate_seeded(&bars, &codes, cost, *s, GRID, Some(&seed), Some(last));
        if k == 0 {
            missing = r.missing_pair_days;
            blocked = r.blocked_open_days;
            fills = r.fills;
        }
        curves.push(curve(r, cost));
    }
    let mut double = cost.clone();
    double.buy_commission *= 2.0;
    double.sell_commission *= 2.0;
    double.stamp_tax *= 2.0;
    double.slippage *= 2.0;
    double.min_commission *= 2.0;
    for s in [Strategy::Fixed(0.5, 1), Strategy::Hold(0.5)] {
        curves.push(curve(
            universal::simulate(&bars, &codes, &double, s, GRID),
            &double,
        ));
    }
    let (daily, hold, universal_ideal) = ideal(&bars, &seed);
    curves.push(daily);
    curves.push(hold);
    let end = *u.test_dates.last().unwrap();
    let terminal_risk = codes.iter().any(|c| {
        !prices[c].contains_key(&end)
            || !prices[c].contains_key(&u.test_dates[0])
            || delist.get(c).is_some_and(|d| *d <= end)
    });
    Evaluation {
        curves,
        universal_ideal,
        missing,
        blocked,
        fills,
        terminal_risk,
    }
}

fn select(training: &[Training], divergence: bool) -> Vec<usize> {
    let mut order: Vec<_> = (0..training.len()).collect();
    order.sort_by(|a, b| {
        let score = |i: usize| {
            if divergence {
                training[i].divergence
            } else {
                training[i].score
            }
        };
        score(*b).total_cmp(&score(*a)).then_with(|| a.cmp(b))
    });
    let mut used = BTreeSet::new();
    let mut picked = Vec::new();
    for index in order {
        let t = &training[index];
        if used.contains(&t.i) || used.contains(&t.j) {
            continue;
        }
        used.insert(t.i);
        used.insert(t.j);
        picked.push(index);
        if picked.len() == PAIRS {
            break;
        }
    }
    assert_eq!(picked.len(), PAIRS);
    picked
}
struct Rng(u64);
impl Rng {
    fn next(&mut self) -> u64 {
        self.0 ^= self.0 << 13;
        self.0 ^= self.0 >> 7;
        self.0 ^= self.0 << 17;
        self.0
    }
    fn index(&mut self, n: usize) -> usize {
        (self.next() % n as u64) as usize
    }
}
fn random_pairs(training: &[Training], n: usize, seed: u64) -> Vec<usize> {
    let map: BTreeMap<_, _> = training
        .iter()
        .enumerate()
        .map(|(k, t)| ((t.i, t.j), k))
        .collect();
    let mut ids: Vec<_> = (0..n).collect();
    let mut rng = Rng(seed);
    for i in (1..n).rev() {
        let j = rng.index(i + 1);
        ids.swap(i, j);
    }
    ids[..2 * PAIRS]
        .chunks(2)
        .map(|p| map[&(p[0].min(p[1]), p[0].max(p[1]))])
        .collect()
}
fn group(indices: &[usize], evaluations: &[Evaluation]) -> Vec<Vec<f64>> {
    (0..METHODS.len())
        .map(|m| {
            (0..evaluations[0].curves[m].len())
                .map(|d| {
                    indices
                        .iter()
                        .map(|i| evaluations[*i].curves[m][d])
                        .sum::<f64>()
                        / indices.len() as f64
                })
                .collect()
        })
        .collect()
}

fn evaluate_fold(
    u: &Universe,
    prices: &Prices,
    delist: &BTreeMap<String, NaiveDate>,
    cost: &ACostConfig,
    output: &Path,
) -> Result<Fold, Error> {
    let began = std::time::Instant::now();
    let pairs: Vec<_> = (0..u.members.len())
        .flat_map(|i| (i + 1..u.members.len()).map(move |j| (i, j)))
        .collect();
    let training: Vec<_> = pairs
        .par_iter()
        .map(|(i, j)| train(*i, *j, u, prices, cost))
        .collect();
    let selected = select(&training, false);
    let divergence = select(&training, true);
    let random: Vec<_> = (0..RANDOM)
        .map(|k| {
            random_pairs(
                &training,
                u.members.len(),
                20261008 + u.year as u64 * 1000 + k as u64,
            )
        })
        .collect();
    // Persist locked choices BEFORE computing any forward outcome.
    freeze(
        &output.join(format!("selection_{}.json", u.year)),
        &json!({"year":u.year,"training_end":u.train_dates.last(),"universe":u.members,"training":training,"selected_indices":selected,"divergence_indices":divergence,"random_indices":random}),
    )?;
    tracing::info!(
        year = u.year,
        pairs = training.len(),
        seconds = began.elapsed().as_secs_f64(),
        "Training scores and selections frozen"
    );
    let evaluations: Vec<_> = training
        .par_iter()
        .map(|t| evaluate(t, u, prices, delist, cost))
        .collect();
    let mut csv = String::from(
        "first,second,selected,training_score,training_divergence,training_correlation,training_relative_vol,terminal_risk,missing_days,blocked_days,fills",
    );
    for m in METHODS {
        csv.push_str(&format!(",{m}"));
    }
    csv.push('\n');
    for (k, (t, e)) in training.iter().zip(&evaluations).enumerate() {
        csv.push_str(&format!(
            "{},{},{},{},{},{},{},{},{},{},{}",
            u.members[t.i].ts_code,
            u.members[t.j].ts_code,
            selected.contains(&k),
            t.score,
            t.divergence,
            t.correlation,
            t.relative_vol,
            e.terminal_risk,
            e.missing,
            e.blocked,
            e.fills
        ));
        for c in &e.curves {
            csv.push_str(&format!(",{}", ending(c) - 1.0));
        }
        csv.push('\n');
    }
    std::fs::write(output.join(format!("all_pairs_{}.csv", u.year)), csv)?;
    let selected_curves = group(&selected, &evaluations);
    let divergence_curves = group(&divergence, &evaluations);
    let random_curves = random.iter().map(|idx| group(idx, &evaluations)).collect();
    let selected_universal_ideal = (0..u.test_dates.len())
        .map(|d| {
            selected
                .iter()
                .map(|i| evaluations[*i].universal_ideal[d])
                .sum::<f64>()
                / PAIRS as f64
        })
        .collect();
    let wins = |e: &Evaluation| ending(&e.curves[0]) > ending(&e.curves[3]);
    let fold = Fold {
        year: u.year,
        dates: u.test_dates.clone(),
        universe: u.members.iter().map(|m| m.ts_code.clone()).collect(),
        selected: selected.iter().map(|i| training[*i].clone()).collect(),
        selected_curves,
        divergence_curves,
        random_curves,
        selected_universal_ideal,
        pair_win_rate: selected.iter().filter(|i| wins(&evaluations[**i])).count() as f64
            / PAIRS as f64,
        all_pair_win_rate: evaluations.iter().filter(|e| wins(e)).count() as f64
            / evaluations.len() as f64,
        selected_terminal_risks: selected
            .iter()
            .filter(|i| evaluations[**i].terminal_risk)
            .count(),
        all_terminal_risks: evaluations.iter().filter(|e| e.terminal_risk).count(),
        selected_missing_days: selected.iter().map(|i| evaluations[*i].missing).sum(),
        selected_fills: selected.iter().map(|i| evaluations[*i].fills).sum(),
    };
    Ok(fold)
}

fn quantile(values: &[f64], p: f64) -> f64 {
    let mut v = values.to_vec();
    v.sort_by(f64::total_cmp);
    v[((v.len() - 1) as f64 * p).round() as usize]
}
fn bootstrap(monthly: &[f64]) -> [f64; 2] {
    let mut rng = Rng(20261008);
    let mut means = Vec::new();
    for _ in 0..2000 {
        let mut sample = Vec::new();
        while sample.len() < monthly.len() {
            let start = rng.index(monthly.len());
            for k in 0..3 {
                sample.push(monthly[(start + k) % monthly.len()]);
                if sample.len() == monthly.len() {
                    break;
                }
            }
        }
        means.push(sample.iter().sum::<f64>() / sample.len() as f64 * 12.0);
    }
    [quantile(&means, 0.025), quantile(&means, 0.975)]
}
fn monthly_excess(folds: &[Fold], method: usize) -> Vec<f64> {
    let mut months = BTreeMap::new();
    for f in folds.iter().filter(|f| f.year <= 2025) {
        let mut prior = [1.0, 1.0];
        for (i, d) in f.dates.iter().enumerate() {
            let now = [f.selected_curves[method][i], f.selected_curves[3][i]];
            *months.entry((d.year(), d.month())).or_insert(0.0) +=
                (now[0] / prior[0]).ln() - (now[1] / prior[1]).ln();
            prior = now;
        }
    }
    months.into_values().collect()
}
fn metric(curves: impl Iterator<Item = Vec<f64>>) -> serde_json::Value {
    let (mut base, mut peak, mut dd, mut n) = (1.0_f64, 1.0_f64, 0.0_f64, 0usize);
    for v in curves {
        for x in &v {
            let nav = base * x;
            peak = peak.max(nav);
            dd = dd.min(nav / peak - 1.0);
            n += 1;
        }
        base *= ending(&v);
    }
    json!({"total_return":base-1.0,"annualized_252_sessions":base.powf(252.0/n as f64)-1.0,"max_drawdown":dd})
}

fn report(folds: &[Fold], output: &Path, protocol: &serde_json::Value) -> Result<(), Error> {
    let full: Vec<_> = folds.iter().filter(|f| f.year <= 2025).collect();
    let mut verdicts = Vec::new();
    for m in [0, 1] {
        let excess = full
            .iter()
            .map(|f| (ending(&f.selected_curves[m]) / ending(&f.selected_curves[3])).ln())
            .sum::<f64>();
        let positive = full
            .iter()
            .filter(|f| ending(&f.selected_curves[m]) > ending(&f.selected_curves[3]))
            .count();
        let random: Vec<_> = (0..RANDOM)
            .map(|k| {
                full.iter()
                    .map(|f| (ending(&f.random_curves[k][m]) / ending(&f.random_curves[k][3])).ln())
                    .sum::<f64>()
            })
            .collect();
        let ci = bootstrap(&monthly_excess(folds, m));
        let percentile = random.iter().filter(|x| **x < excess).count() as f64 / RANDOM as f64;
        let risks = full
            .iter()
            .map(|f| f.selected_terminal_risks)
            .sum::<usize>();
        let supported = excess > 0.0
            && positive >= 3
            && ci[0] > 0.0
            && excess > quantile(&random, 0.95)
            && risks == 0;
        verdicts.push(json!({"method":METHODS[m],"supported":supported,"cumulative_paired_log_excess":excess,"positive_full_years":positive,"full_years":full.len(),"annual_log_excess_95pct_block_bootstrap":ci,"random_excess_percentile":percentile,"random_log_excess_p05":quantile(&random,0.05),"random_log_excess_median":quantile(&random,0.5),"random_log_excess_p95":quantile(&random,0.95),"terminal_risks":risks}));
    }
    let metrics: BTreeMap<_, _> = METHODS
        .iter()
        .enumerate()
        .map(|(m, name)| {
            (
                *name,
                metric(full.iter().map(|f| f.selected_curves[m].clone())),
            )
        })
        .collect();
    let summary = json!({"protocol":protocol,"verdicts":verdicts,"full_year_metrics":metrics,"universal_ideal":metric(full.iter().map(|f|f.selected_universal_ideal.clone())),"years":folds.iter().map(|f|json!({"year":f.year,"universe_size":f.universe.len(),"candidate_pairs":f.universe.len()*(f.universe.len()-1)/2,"selected_pairs":f.selected.iter().map(|t|json!({"first":f.universe[t.i],"second":f.universe[t.j],"training_score":t.score,"correlation":t.correlation,"relative_vol":t.relative_vol})).collect::<Vec<_>>(),"returns":METHODS.iter().enumerate().map(|(i,m)|(*m,ending(&f.selected_curves[i])-1.0)).collect::<BTreeMap<_,_>>(),"divergence_selector_returns":METHODS.iter().enumerate().map(|(i,m)|(*m,ending(&f.divergence_curves[i])-1.0)).collect::<BTreeMap<_,_>>(),"selected_pair_win_rate":f.pair_win_rate,"all_pair_win_rate":f.all_pair_win_rate,"selected_terminal_risks":f.selected_terminal_risks,"all_terminal_risks":f.all_terminal_risks,"selected_missing_pair_days":f.selected_missing_days,"selected_daily_fills":f.selected_fills})).collect::<Vec<_>>()});
    std::fs::write(
        output.join("report.json"),
        serde_json::to_vec_pretty(&summary)?,
    )?;
    let mut csv =
        String::from("year,method,return,hold_return,excess_percentage_points,pair_win_rate\n");
    let mut html = String::from(
        "<!doctype html><html lang=zh-CN><meta charset=utf-8><title>A 股滚动配对验证</title><style>body{font:16px system-ui;max-width:1200px;margin:40px auto;padding:20px;color:#123}table{border-collapse:collapse;width:100%;margin:20px 0}td,th{padding:10px;text-align:right;border-bottom:1px solid #ddd}th{background:#edf3fa}p{line-height:1.7}pre{white-space:pre-wrap;background:#f1f4f8;padding:20px}</style><h1>A 股滚动配对验证</h1><p>此前两年筛选，下一年验证。2022–2025 为四个完整年度；2026 截至 9 月仅作诊断。每年从历史成交额前 60 名合格主板股票中选 10 组不重复股票对，每组 10 万元，年度重置。含整手、佣金、最低费用、固定税率、滑点及估算期末清仓成本；跨年净值为标准化研究指数。</p><p><a href=protocol.json>预先固定的研究规则</a> · <a href=report.json>完整数据与股票名单</a> · <a href=summary.csv>年度汇总</a> · <a href=curves.csv>每日组合净值</a></p>",
    );
    for v in &verdicts {
        html.push_str(&format!("<h2>{}: {}</h2><p>正超额年度 {}/4；相对随机配对的超额百分位 {:.0}%；年化对数超额 95% 区块自助区间 [{:.2}%, {:.2}%]。终点数据风险 {} 组。</p>",v["method"].as_str().unwrap(),if v["supported"].as_bool().unwrap(){"通过预设检验（有条件支持）"}else{"未通过预设检验（不支持稳定优势）"},v["positive_full_years"],v["random_excess_percentile"].as_f64().unwrap()*100.0,v["annual_log_excess_95pct_block_bootstrap"][0].as_f64().unwrap()*100.0,v["annual_log_excess_95pct_block_bootstrap"][1].as_f64().unwrap()*100.0,v["terminal_risks"]));
    }
    html.push_str("<table><tr><th>年度</th><th>每日等权净收益</th><th>通用组合净收益</th><th>每21日等权净收益</th><th>等权持有净收益</th><th>每日等权优于持有的配对占比</th></tr>");
    let mut curves = String::from("date,year");
    for m in METHODS {
        curves.push_str(&format!(",{m}"));
    }
    curves.push_str(",universal_ideal\n");
    let mut bases = vec![1.0; METHODS.len() + 1];
    for f in folds {
        let r: Vec<_> = f.selected_curves.iter().map(|v| ending(v) - 1.0).collect();
        html.push_str(&format!("<tr><td>{}</td><td>{:.2}%</td><td>{:.2}%</td><td>{:.2}%</td><td>{:.2}%</td><td>{:.0}%</td></tr>",f.year,r[0]*100.0,r[1]*100.0,r[2]*100.0,r[3]*100.0,f.pair_win_rate*100.0));
        for (i, m) in METHODS.iter().enumerate() {
            let benchmark = match i {
                4 | 5 => 5,
                6 | 7 => 7,
                _ => 3,
            };
            csv.push_str(&format!(
                "{},{},{},{},{},{}\n",
                f.year,
                m,
                r[i],
                r[benchmark],
                (r[i] - r[benchmark]) * 100.0,
                f.pair_win_rate
            ));
        }
        for (d, date) in f.dates.iter().enumerate() {
            curves.push_str(&format!("{date},{}", f.year));
            for (m, base) in bases.iter().enumerate().take(METHODS.len()) {
                curves.push_str(&format!(",{}", base * f.selected_curves[m][d]));
            }
            curves.push_str(&format!(
                ",{}\n",
                bases[METHODS.len()] * f.selected_universal_ideal[d]
            ));
        }
        for (m, base) in bases.iter_mut().enumerate().take(METHODS.len()) {
            *base *= ending(&f.selected_curves[m]);
        }
        bases[METHODS.len()] *= ending(&f.selected_universal_ideal);
    }
    html.push_str("</table><h2>局限</h2><p>本实验是历史向前验证，不是从未看过的数据。未按当前上市状态过滤，但原始库退市历史可能不完整。复权因子近似公司行动；缺报价采用旧估值，开盘偏离前收4.5%暂停整对交易；终点缺报价或区间退市保留并标记，禁止据此宣称通过。实际队列成交、精确历史ST、股息税和市场冲击未完整建模。四个完整年份的统计证据有限，辅助指标不能代替主检验。</p><h2>完整年度汇总</h2><pre>");
    html.push_str(&serde_json::to_string_pretty(&metrics)?);
    html.push_str("</pre></html>");
    std::fs::write(output.join("summary.csv"), csv)?;
    std::fs::write(output.join("curves.csv"), curves)?;
    std::fs::write(output.join("report.html"), html)?;
    println!(
        "{}",
        serde_json::to_string_pretty(&json!({"verdicts":verdicts,"full_year_metrics":metrics}))?
    );
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn seeded_ideal_matches_weighted_expert_wealth() {
        let bars: Vec<_> = [(10.0, 12.0), (12.0, 9.0), (9.0, 11.0)]
            .iter()
            .enumerate()
            .map(|(i, (open, close))| {
                let raw = |o, c| Raw {
                    ts_code: "fixture".into(),
                    trade_date: date(2022, 1, i as u32 + 1),
                    open: Some(o),
                    high: Some(o.max(c)),
                    low: Some(o.min(c)),
                    close: Some(c),
                    pre_close: Some(o),
                    vol: Some(100.0),
                    amount: Some(1000.0),
                    adj_factor: Some(1.0),
                };
                Day {
                    date: date(2022, 1, i as u32 + 1),
                    bars: [raw(*open, *close).bar(), raw(10.0, 10.0).bar()],
                }
            })
            .collect();
        let mut seed = vec![0.0; GRID];
        universal::learn(&mut seed, [1.7, 0.8]);
        let prior_sum = seed.iter().map(|x| x.exp()).sum::<f64>();
        let (d, h, p) = ideal(&bars, &seed);
        let mut posterior = seed.clone();
        for r in [[1.2, 1.0], [0.75, 1.0], [11.0 / 9.0, 1.0]] {
            universal::learn(&mut posterior, r);
        }
        assert!(
            (ending(&p) - posterior.iter().map(|x| x.exp()).sum::<f64>() / prior_sum).abs() < 1e-12
        );
        assert!((ending(&h) - 1.05).abs() < 1e-12);
        assert!((ending(&d) - 1.1 * 0.875 * (1.0 + 11.0 / 9.0) / 2.0).abs() < 1e-12);
    }
    #[test]
    fn disjoint_selection_and_random_are_reproducible() {
        let ts: Vec<_> = (0..24)
            .flat_map(|i| {
                (i + 1..24).map(move |j| Training {
                    i,
                    j,
                    score: (i + j) as f64,
                    divergence: (i * j) as f64,
                    correlation: 0.0,
                    relative_vol: 0.0,
                    train_return: 0.0,
                    train_best_single: 0.0,
                })
            })
            .collect();
        for picked in [
            select(&ts, false),
            select(&ts, true),
            random_pairs(&ts, 24, 42),
        ] {
            let codes: BTreeSet<_> = picked.iter().flat_map(|i| [ts[*i].i, ts[*i].j]).collect();
            assert_eq!(codes.len(), 20);
        }
        assert_eq!(random_pairs(&ts, 24, 42), random_pairs(&ts, 24, 42));
    }
    #[test]
    fn bootstrap_preserves_constant_signal() {
        let bounds = bootstrap(&[0.01; 48]);
        assert!((bounds[0] - 0.12).abs() < 1e-12);
        assert!((bounds[1] - 0.12).abs() < 1e-12);
    }
    #[test]
    fn annual_curve_stitch_retains_boundary_drawdown() {
        let m = metric([vec![0.8, 1.2], vec![0.5, 1.0]].into_iter());
        assert!((m["total_return"].as_f64().unwrap() - 0.2).abs() < 1e-12);
        assert!((m["max_drawdown"].as_f64().unwrap() + 0.5).abs() < 1e-12);
    }
}
