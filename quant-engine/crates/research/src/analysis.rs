//! Fixed-protocol money-flow factor event study. These returns are not portfolio NAV.
use crate::a_execution_price::EntryPriceModel;
use crate::data::{Bar, Dataset, Stock};
use chrono::{Datelike, NaiveDate};
use rayon::prelude::*;
use serde::Serialize;
use std::collections::{BTreeMap, HashMap};
use std::fs::{self, File};
use std::io::{BufWriter, Write};
use std::path::Path;

const N: usize = 10;
const HORIZONS: [usize; 4] = [1, 5, 10, 20];
const COST: f64 = 0.0045;
const FACTORS: [&str; N] = [
    "activity_3v20",
    "turnover_5v20",
    "net_pressure_1d",
    "net_pressure_5d",
    "inflow_days_5d",
    "inflow_concentration_5d",
    "relative_return_5d",
    "close_location_5d",
    "volatility_20d",
    "illiquidity_20d",
];

struct Series {
    factors: Vec<[f64; N]>,
    median_amount: Vec<f64>,
}

fn ratio(a: f64, b: f64) -> f64 {
    if a.is_finite() && b.is_finite() && b > 0.0 {
        a / b
    } else {
        f64::NAN
    }
}

fn mean(v: &[f64]) -> f64 {
    if v.is_empty() || v.iter().any(|x| !x.is_finite()) {
        f64::NAN
    } else {
        v.iter().sum::<f64>() / v.len() as f64
    }
}

fn finite_mean(v: impl Iterator<Item = f64>) -> f64 {
    let (sum, count) = v
        .filter(|x| x.is_finite())
        .fold((0.0, 0usize), |(s, n), x| (s + x, n + 1));
    if count == 0 {
        f64::NAN
    } else {
        sum / count as f64
    }
}

fn quantile(sorted: &[f64], q: f64) -> f64 {
    if sorted.is_empty() {
        return f64::NAN;
    }
    let position = (sorted.len() - 1) as f64 * q;
    let low = position.floor() as usize;
    let high = position.ceil() as usize;
    sorted[low] + (sorted[high] - sorted[low]) * (position - low as f64)
}

fn median(v: &[f64]) -> f64 {
    if v.is_empty() || v.iter().any(|x| !x.is_finite()) {
        return f64::NAN;
    }
    let mut values = v.to_vec();
    values.sort_unstable_by(f64::total_cmp);
    quantile(&values, 0.5)
}

fn sample_std(v: &[f64]) -> f64 {
    let m = mean(v);
    if v.len() < 2 || !m.is_finite() {
        return f64::NAN;
    }
    (v.iter().map(|x| (x - m).powi(2)).sum::<f64>() / (v.len() - 1) as f64).sqrt()
}

fn window(v: &[f64], end: usize, width: usize) -> &[f64] {
    if end < width {
        &[]
    } else {
        &v[end - width..end]
    }
}

fn indicators(stock: &Stock) -> Series {
    let bars = &stock.bars;
    let amount: Vec<_> = bars.iter().map(|b| b.amount * 1000.0).collect();
    let net: Vec<_> = bars.iter().map(|b| b.net * 10000.0).collect();
    let turnover: Vec<_> = bars.iter().map(|b| b.turnover_rate).collect();
    let adjusted: Vec<_> = bars
        .iter()
        .map(|b| {
            if b.close > 0.0 && b.adj_factor > 0.0 {
                b.close * b.adj_factor
            } else {
                f64::NAN
            }
        })
        .collect();
    let returns: Vec<_> = (0..bars.len())
        .map(|t| {
            if t > 0 {
                ratio(adjusted[t], adjusted[t - 1]) - 1.0
            } else {
                f64::NAN
            }
        })
        .collect();
    let illiquidity: Vec<_> = returns
        .iter()
        .zip(&amount)
        .map(|(&r, &a)| ratio(r.abs(), a) * 1e8)
        .collect();
    let location: Vec<_> = bars
        .iter()
        .map(|b| ratio(b.close - b.low, b.high - b.low))
        .collect();
    let positive: Vec<_> = net
        .iter()
        .map(|&x| if x.is_finite() { x.max(0.0) } else { f64::NAN })
        .collect();
    let inflows: Vec<_> = net
        .iter()
        .map(|&x| {
            if x.is_finite() {
                if x > 0.0 {
                    1.0
                } else {
                    0.0
                }
            } else {
                f64::NAN
            }
        })
        .collect();
    let mut result = Series {
        factors: Vec::with_capacity(bars.len()),
        median_amount: Vec::with_capacity(bars.len()),
    };
    for t in 0..bars.len() {
        let end = t + 1;
        let baseline_amount = if end >= 23 {
            median(window(&amount, end - 3, 20))
        } else {
            f64::NAN
        };
        let baseline_turnover = if end >= 25 {
            mean(window(&turnover, end - 5, 20))
        } else {
            f64::NAN
        };
        let positive_window = window(&positive, end, 5);
        let positive_sum = mean(positive_window) * 5.0;
        result.factors.push([
            ratio(mean(window(&amount, end, 3)), baseline_amount),
            ratio(mean(window(&turnover, end, 5)), baseline_turnover),
            ratio(net[t], amount[t]),
            ratio(mean(window(&net, end, 5)), mean(window(&amount, end, 5))),
            mean(window(&inflows, end, 5)),
            ratio(
                positive_window
                    .iter()
                    .copied()
                    .fold(f64::NEG_INFINITY, f64::max),
                positive_sum,
            ),
            if t >= 5 {
                ratio(adjusted[t], adjusted[t - 5]) - 1.0
            } else {
                f64::NAN
            },
            mean(window(&location, end, 5)),
            sample_std(window(&returns, end, 20)),
            mean(window(&illiquidity, end, 20)),
        ]);
        result.median_amount.push(median(window(&amount, end, 20)));
    }
    result
}

fn adjusted_open(bar: &Bar) -> f64 {
    if bar.open.is_finite()
        && bar.open > 0.0
        && bar.vol.is_finite()
        && bar.vol > 0.0
        && bar.adj_factor.is_finite()
        && bar.adj_factor > 0.0
    {
        bar.open * bar.adj_factor
    } else {
        f64::NAN
    }
}

fn forward_return(
    dates: &[NaiveDate],
    bars: &[Bar],
    t: usize,
    horizon: usize,
    entry_price: EntryPriceModel,
) -> f64 {
    let entry = t + 2;
    let exit = entry + horizon;
    if exit >= dates.len() || dates[exit].year() != dates[t].year() {
        return f64::NAN;
    }
    ratio(
        adjusted_open(&bars[exit]),
        entry_price.adjusted_entry(&bars[entry]),
    ) - 1.0
}

/// A minimum of ten *other* eligible observations, not ten including self.
fn peer_demean(values: &[f64], eligible: &[bool], industries: &[Option<&str>]) -> Vec<f64> {
    let mut totals: HashMap<&str, (f64, usize)> = HashMap::new();
    for ((&value, &allowed), &industry) in values.iter().zip(eligible).zip(industries) {
        if allowed && value.is_finite() {
            if let Some(industry) = industry {
                let total = totals.entry(industry).or_default();
                total.0 += value;
                total.1 += 1;
            }
        }
    }
    values
        .iter()
        .zip(eligible)
        .zip(industries)
        .map(|((&value, &allowed), &industry)| {
            if !allowed || !value.is_finite() {
                return f64::NAN;
            }
            match industry.and_then(|i| totals.get(i)) {
                Some(&(total, count)) if count >= 11 => {
                    value - (total - value) / (count - 1) as f64
                }
                _ => f64::NAN,
            }
        })
        .collect()
}

/// Average ranks preserve ties; missing values retain NaN.
fn ranks(values: &[f64]) -> Vec<f64> {
    let mut order: Vec<_> = values
        .iter()
        .enumerate()
        .filter(|(_, v)| v.is_finite())
        .map(|(i, _)| i)
        .collect();
    order.sort_unstable_by(|&a, &b| values[a].total_cmp(&values[b]));
    let mut result = vec![f64::NAN; values.len()];
    let mut i = 0;
    while i < order.len() {
        let mut j = i + 1;
        while j < order.len() && values[order[i]] == values[order[j]] {
            j += 1;
        }
        let rank = (i + j - 1) as f64 * 0.5 + 1.0;
        for k in i..j {
            result[order[k]] = rank;
        }
        i = j;
    }
    result
}

fn correlation(a: &[f64], b: &[f64]) -> f64 {
    let (mut n, mut ax, mut bx) = (0usize, 0.0, 0.0);
    for (&x, &y) in a.iter().zip(b) {
        if x.is_finite() && y.is_finite() {
            n += 1;
            ax += x;
            bx += y;
        }
    }
    if n < 2 {
        return f64::NAN;
    }
    let (am, bm) = (ax / n as f64, bx / n as f64);
    let (mut aa, mut bb, mut ab) = (0.0, 0.0, 0.0);
    for (&x, &y) in a.iter().zip(b) {
        if x.is_finite() && y.is_finite() {
            aa += (x - am).powi(2);
            bb += (y - bm).powi(2);
            ab += (x - am) * (y - bm);
        }
    }
    if aa <= 0.0 || bb <= 0.0 {
        f64::NAN
    } else {
        ab / (aa * bb).sqrt()
    }
}

#[derive(Clone, Debug)]
struct Daily {
    n: usize,
    coverage: f64,
    label_coverage: f64,
    ic: f64,
    spread: f64,
    high_net: f64,
    low_net: f64,
    high_excess: f64,
    high_gross: f64,
    low_gross: f64,
    high_n: usize,
    low_n: usize,
}

fn cross_section(
    factor: &[f64],
    returns: &[f64],
    excess: &[f64],
    eligible: &[bool],
) -> (Daily, Vec<f64>) {
    let eligible_count = eligible.iter().filter(|&&x| x).count();
    let covered = factor
        .iter()
        .zip(eligible)
        .filter(|(x, e)| **e && x.is_finite())
        .count();
    let paired: Vec<_> = (0..factor.len())
        .map(|i| {
            if eligible[i]
                && factor[i].is_finite()
                && returns[i].is_finite()
                && excess[i].is_finite()
            {
                factor[i]
            } else {
                f64::NAN
            }
        })
        .collect();
    let n = paired.iter().filter(|x| x.is_finite()).count();
    let xranks = ranks(&paired);
    let yranks = ranks(
        &paired
            .iter()
            .zip(excess)
            .map(|(x, &y)| if x.is_finite() { y } else { f64::NAN })
            .collect::<Vec<_>>(),
    );
    let mut daily = Daily {
        n,
        coverage: ratio(covered as f64, eligible_count as f64),
        label_coverage: ratio(n as f64, covered as f64),
        ic: if n >= 100 {
            correlation(&xranks, &yranks)
        } else {
            f64::NAN
        },
        spread: f64::NAN,
        high_net: f64::NAN,
        low_net: f64::NAN,
        high_excess: f64::NAN,
        high_gross: f64::NAN,
        low_gross: f64::NAN,
        high_n: 0,
        low_n: 0,
    };
    if n >= 100 {
        let mut sorted: Vec<_> = paired.iter().copied().filter(|x| x.is_finite()).collect();
        sorted.sort_unstable_by(f64::total_cmp);
        let (low, high) = (quantile(&sorted, 0.2), quantile(&sorted, 0.8));
        if high > low {
            let (mut hr, mut lr, mut he) = (0.0, 0.0, 0.0);
            for (i, &x) in paired.iter().enumerate() {
                if x >= high {
                    daily.high_n += 1;
                    hr += returns[i];
                    he += excess[i];
                }
                if x <= low {
                    daily.low_n += 1;
                    lr += returns[i];
                }
            }
            daily.high_gross = hr / daily.high_n as f64;
            daily.low_gross = lr / daily.low_n as f64;
            daily.spread = daily.high_gross - daily.low_gross;
            daily.high_net = daily.high_gross - COST;
            daily.low_net = daily.low_gross - COST;
            daily.high_excess = he / daily.high_n as f64 - COST;
        }
    }
    (daily, xranks)
}

struct DateResult {
    date: NaiveDate,
    daily: Vec<Daily>,
    eligible: usize,
    liquid: usize,
    moneyflow: usize,
    industry: usize,
    labels: [usize; 4],
    correlations: [[f64; N]; N],
    exposures: [[f64; 3]; N],
}

fn date_result(
    data: &Dataset,
    series: &[Series],
    t: usize,
    entry_price: EntryPriceModel,
) -> DateResult {
    let eligible: Vec<_> = data
        .stocks
        .iter()
        .map(|s| s.bars[t].eligible && data.dates[t].year() >= 2024)
        .collect();
    let liquid: Vec<_> = eligible
        .iter()
        .zip(series)
        .map(|(&e, s)| e && s.median_amount[t] >= 5e6)
        .collect();
    let industries: Vec<_> = data
        .stocks
        .iter()
        .map(|s| s.bars[t].industry.as_deref())
        .collect();
    let mut factors: Vec<Vec<f64>> = (0..N)
        .map(|f| series.iter().map(|s| s.factors[t][f]).collect())
        .collect();
    factors[6] = peer_demean(&factors[6], &eligible, &industries);
    let mut result = DateResult {
        date: data.dates[t],
        daily: Vec::with_capacity(80),
        eligible: eligible.iter().filter(|&&x| x).count(),
        liquid: liquid.iter().filter(|&&x| x).count(),
        moneyflow: data
            .stocks
            .iter()
            .zip(&eligible)
            .filter(|(s, e)| **e && s.bars[t].net.is_finite())
            .count(),
        industry: industries
            .iter()
            .zip(&eligible)
            .filter(|(i, e)| **e && i.is_some())
            .count(),
        labels: [0; 4],
        correlations: [[f64::NAN; N]; N],
        exposures: [[f64::NAN; 3]; N],
    };
    let mut diagnostic_ranks = Vec::with_capacity(N);
    for (h, &horizon) in HORIZONS.iter().enumerate() {
        let returns: Vec<_> = data
            .stocks
            .iter()
            .map(|s| forward_return(&data.dates, &s.bars, t, horizon, entry_price))
            .collect();
        let excess = peer_demean(&returns, &eligible, &industries);
        result.labels[h] = returns
            .iter()
            .zip(&eligible)
            .filter(|(r, e)| **e && r.is_finite())
            .count();
        for factor in &factors {
            for (segment, mask) in [&eligible, &liquid].iter().enumerate() {
                let (daily, ranks) = cross_section(factor, &returns, &excess, mask);
                result.daily.push(daily);
                if horizon == 5 && segment == 0 {
                    diagnostic_ranks.push(ranks);
                }
            }
        }
    }
    let controls: [Vec<f64>; 3] = std::array::from_fn(|c| {
        ranks(
            &data
                .stocks
                .iter()
                .enumerate()
                .map(|(s, stock)| {
                    if !eligible[s] {
                        return f64::NAN;
                    }
                    match c {
                        0 => stock.bars[t].circ_mv,
                        1 => series[s].median_amount[t],
                        _ => stock.bars[t].close,
                    }
                })
                .collect::<Vec<_>>(),
        )
    });
    for a in 0..N {
        for b in a..N {
            let corr = correlation(&diagnostic_ranks[a], &diagnostic_ranks[b]);
            result.correlations[a][b] = corr;
            result.correlations[b][a] = corr;
        }
        for (c, control) in controls.iter().enumerate() {
            result.exposures[a][c] = correlation(&diagnostic_ranks[a], control);
        }
    }
    result
}

fn option(value: f64) -> Option<f64> {
    value.is_finite().then_some(value)
}

fn monthly_ci(days: &[(NaiveDate, f64)]) -> [Option<f64>; 2] {
    let mut months = BTreeMap::<(i32, u32), (f64, usize)>::new();
    for &(date, value) in days {
        if value.is_finite() {
            let m = months.entry((date.year(), date.month())).or_default();
            m.0 += value;
            m.1 += 1;
        }
    }
    let monthly: Vec<_> = months.values().map(|&(s, n)| s / n as f64).collect();
    if monthly.len() < 6 {
        return [None, None];
    }
    // Fixed xorshift64* bootstrap, 2,000 draws, seed 42. Resample calendar-month means.
    let mut state = 42u64;
    let mut draws = Vec::with_capacity(2000);
    for _ in 0..2000 {
        let mut sum = 0.0;
        for _ in 0..monthly.len() {
            state ^= state >> 12;
            state ^= state << 25;
            state ^= state >> 27;
            let next = state.wrapping_mul(2685821657736338717);
            sum += monthly[(next % monthly.len() as u64) as usize];
        }
        draws.push(sum / monthly.len() as f64);
    }
    draws.sort_unstable_by(f64::total_cmp);
    [
        option(quantile(&draws, 0.025)),
        option(quantile(&draws, 0.975)),
    ]
}

#[derive(Serialize)]
struct Summary {
    factor: &'static str,
    horizon: usize,
    segment: &'static str,
    year: i32,
    observations: usize,
    dates: usize,
    coverage: Option<f64>,
    label_coverage: Option<f64>,
    rank_ic: Option<f64>,
    rank_ic_monthly_ci: [Option<f64>; 2],
    high_minus_low: Option<f64>,
    spread_monthly_ci: [Option<f64>; 2],
    high_net_return: Option<f64>,
    low_net_return: Option<f64>,
    high_industry_excess: Option<f64>,
}

fn summarize(
    results: &[DateResult],
    index: usize,
    factor: &'static str,
    horizon: usize,
    segment: &'static str,
) -> Vec<Summary> {
    let mut years = BTreeMap::<i32, Vec<&DateResult>>::new();
    for result in results {
        years.entry(result.date.year()).or_default().push(result);
    }
    years
        .into_iter()
        .map(|(year, days)| {
            let avg =
                |f: fn(&Daily) -> f64| option(finite_mean(days.iter().map(|d| f(&d.daily[index]))));
            Summary {
                factor,
                horizon,
                segment,
                year,
                observations: days.iter().map(|d| d.daily[index].n).sum(),
                dates: days
                    .iter()
                    .filter(|d| d.daily[index].ic.is_finite())
                    .count(),
                coverage: avg(|d| d.coverage),
                label_coverage: avg(|d| d.label_coverage),
                rank_ic: avg(|d| d.ic),
                rank_ic_monthly_ci: monthly_ci(
                    &days
                        .iter()
                        .map(|d| (d.date, d.daily[index].ic))
                        .collect::<Vec<_>>(),
                ),
                high_minus_low: avg(|d| d.spread),
                spread_monthly_ci: monthly_ci(
                    &days
                        .iter()
                        .map(|d| (d.date, d.daily[index].spread))
                        .collect::<Vec<_>>(),
                ),
                high_net_return: avg(|d| d.high_net),
                low_net_return: avg(|d| d.low_net),
                high_industry_excess: avg(|d| d.high_excess),
            }
        })
        .collect()
}

fn number(n: f64) -> String {
    if n.is_finite() {
        n.to_string()
    } else {
        String::new()
    }
}
fn csv(s: &str) -> String {
    if s.contains([',', '"', '\r', '\n']) {
        format!("\"{}\"", s.replace('"', "\"\""))
    } else {
        s.to_owned()
    }
}

/// Each file is replaced atomically; a failed run never leaves a partial CSV/JSON.
fn atomic_file(
    path: &Path,
    write: impl FnOnce(&mut BufWriter<File>) -> Result<(), crate::Error>,
) -> Result<(), crate::Error> {
    let temp = path.with_extension(format!("tmp-{}", std::process::id()));
    let mut writer = BufWriter::new(File::create(&temp)?);
    write(&mut writer)?;
    writer.flush()?;
    writer.get_ref().sync_all()?;
    drop(writer);
    fs::rename(temp, path)?;
    Ok(())
}

pub fn analyze(
    data: &Dataset,
    output: &Path,
    entry_price: EntryPriceModel,
) -> Result<(), crate::Error> {
    let started = std::time::Instant::now();
    if data.dates.is_empty() || data.stocks.is_empty() {
        return Err("Analysis input is empty".into());
    }
    if data.dates.windows(2).any(|d| d[0] >= d[1])
        || data.stocks.iter().any(|s| s.bars.len() != data.dates.len())
    {
        return Err("Analysis dates must be sorted/unique and stock bars calendar-aligned".into());
    }
    fs::create_dir_all(output)?;
    eprintln!(
        "flow research: computing 10 indicators for {} stocks, {} dates, Rayon workers={}",
        data.stocks.len(),
        data.dates.len(),
        rayon::current_num_threads()
    );
    let series: Vec<_> = data.stocks.par_iter().map(indicators).collect();
    eprintln!(
        "flow research: indicator stage {:.1}s; evaluating dates in parallel",
        started.elapsed().as_secs_f64()
    );
    let indices: Vec<_> = data
        .dates
        .iter()
        .enumerate()
        .filter(|(_, d)| d.year() >= 2024)
        .map(|(t, _)| t)
        .collect();
    if indices.is_empty() {
        return Err("No evaluation dates on or after 2024-01-01".into());
    }
    let completed = std::sync::atomic::AtomicUsize::new(0);
    let results: Vec<_> = indices
        .par_iter()
        .map(|&t| {
            let result = date_result(data, &series, t, entry_price);
            let done = completed.fetch_add(1, std::sync::atomic::Ordering::Relaxed) + 1;
            if done % 50 == 0 || done == indices.len() {
                eprintln!(
                    "flow research: evaluated {done}/{} dates, elapsed {:.1}s",
                    indices.len(),
                    started.elapsed().as_secs_f64()
                );
            }
            result
        })
        .collect();
    if results
        .iter()
        .all(|r| r.daily.iter().all(|d| !d.ic.is_finite()))
    {
        return Err("No usable factor dates (need >=100 paired stocks and >=10 other industry peers); no report written".into());
    }
    eprintln!(
        "flow research: {} dates evaluated in {:.1}s; writing reports",
        results.len(),
        started.elapsed().as_secs_f64()
    );
    atomic_file(&output.join("coverage.csv"), |w| {
        writeln!(
            w,
            "date,eligible,liquid,moneyflow,industry,labels_1d,labels_5d,labels_10d,labels_20d"
        )?;
        for r in &results {
            writeln!(
                w,
                "{},{},{},{},{},{},{},{},{}",
                r.date,
                r.eligible,
                r.liquid,
                r.moneyflow,
                r.industry,
                r.labels[0],
                r.labels[1],
                r.labels[2],
                r.labels[3]
            )?;
        }
        Ok(())
    })?;
    let mut summaries = Vec::new();
    for (h, &horizon) in HORIZONS.iter().enumerate() {
        for (f, &factor) in FACTORS.iter().enumerate() {
            for (s, &segment) in ["all", "liquid"].iter().enumerate() {
                let index = h * N * 2 + f * 2 + s;
                atomic_file(
                    &output.join(format!("daily_{factor}_{horizon}d_{segment}.csv")),
                    |w| {
                        writeln!(w,"date,n,ic,coverage,label_coverage,spread,high_net,low_net,high_excess,high_gross,low_gross,high_n,low_n")?;
                        for r in &results {
                            let d = &r.daily[index];
                            writeln!(
                                w,
                                "{},{},{},{},{},{},{},{},{},{},{},{},{}",
                                r.date,
                                d.n,
                                number(d.ic),
                                number(d.coverage),
                                number(d.label_coverage),
                                number(d.spread),
                                number(d.high_net),
                                number(d.low_net),
                                number(d.high_excess),
                                number(d.high_gross),
                                number(d.low_gross),
                                d.high_n,
                                d.low_n
                            )?;
                        }
                        Ok(())
                    },
                )?;
                summaries.extend(summarize(&results, index, factor, horizon, segment));
            }
        }
    }
    atomic_file(&output.join("factor_results.json"), |w| {
        serde_json::to_writer_pretty(&mut *w, &summaries)?;
        writeln!(w)?;
        Ok(())
    })?;
    atomic_file(&output.join("factor_results.csv"), |w| {
        writeln!(w,"factor,horizon,segment,year,observations,dates,coverage,label_coverage,rank_ic,rank_ic_monthly_ci,high_minus_low,spread_monthly_ci,high_net_return,low_net_return,high_industry_excess")?;
        for s in &summaries {
            writeln!(
                w,
                "{},{},{},{},{},{},{},{},{},{},{},{},{},{},{}",
                s.factor,
                s.horizon,
                s.segment,
                s.year,
                s.observations,
                s.dates,
                number(s.coverage.unwrap_or(f64::NAN)),
                number(s.label_coverage.unwrap_or(f64::NAN)),
                number(s.rank_ic.unwrap_or(f64::NAN)),
                csv(&serde_json::to_string(&s.rank_ic_monthly_ci)?),
                number(s.high_minus_low.unwrap_or(f64::NAN)),
                csv(&serde_json::to_string(&s.spread_monthly_ci)?),
                number(s.high_net_return.unwrap_or(f64::NAN)),
                number(s.low_net_return.unwrap_or(f64::NAN)),
                number(s.high_industry_excess.unwrap_or(f64::NAN))
            )?;
        }
        Ok(())
    })?;
    atomic_file(&output.join("factor_correlations.csv"), |w| {
        writeln!(w, "factor,{}", FACTORS.join(","))?;
        for (a, name) in FACTORS.iter().enumerate() {
            write!(w, "{name}")?;
            for b in 0..N {
                write!(
                    w,
                    ",{}",
                    number(finite_mean(results.iter().map(|r| r.correlations[a][b])))
                )?;
            }
            writeln!(w)?;
        }
        Ok(())
    })?;
    atomic_file(&output.join("factor_exposures.csv"), |w| {
        writeln!(w, "factor,size,liquidity,price")?;
        for (f, name) in FACTORS.iter().enumerate() {
            writeln!(
                w,
                "{},{},{},{}",
                name,
                number(finite_mean(results.iter().map(|r| r.exposures[f][0]))),
                number(finite_mean(results.iter().map(|r| r.exposures[f][1]))),
                number(finite_mean(results.iter().map(|r| r.exposures[f][2])))
            )?;
        }
        Ok(())
    })?;
    let t = data.dates.len() - 1;
    let eligible: Vec<_> = data
        .stocks
        .iter()
        .map(|s| s.bars[t].eligible && data.dates[t].year() >= 2024)
        .collect();
    let industry: Vec<_> = data
        .stocks
        .iter()
        .map(|s| s.bars[t].industry.as_deref())
        .collect();
    let relative = peer_demean(
        &series.iter().map(|s| s.factors[t][6]).collect::<Vec<_>>(),
        &eligible,
        &industry,
    );
    atomic_file(&output.join("latest_dashboard.csv"), |w| {
        writeln!(
            w,
            "ts_code,{},net_amount_cny,amount_cny,close,eligible,name,date",
            FACTORS.join(",")
        )?;
        for (s, stock) in data.stocks.iter().enumerate() {
            write!(w, "{}", csv(&stock.code))?;
            for f in 0..N {
                write!(
                    w,
                    ",{}",
                    number(if f == 6 {
                        relative[s]
                    } else {
                        series[s].factors[t][f]
                    })
                )?;
            }
            let b = &stock.bars[t];
            writeln!(
                w,
                ",{},{},{},{},{},{}",
                number(b.net * 10000.0),
                number(b.amount * 1000.0),
                number(b.close),
                eligible[s],
                csv(&stock.name),
                data.dates[t]
            )?;
        }
        Ok(())
    })?;
    // Commit marker is written last; it documents intentional implementation refinements.
    atomic_file(&output.join("analysis_manifest.json"), |w| {
        serde_json::to_writer_pretty(
            &mut *w,
            &serde_json::json!({
                "implementation":"quant-research Rust", "completed_at":chrono::Utc::now().to_rfc3339(),
                "stocks":data.stocks.len(),"calendar_dates":data.dates.len(),"evaluation_dates":results.len(),
                "factor_summaries":summaries.len(),"elapsed_seconds":started.elapsed().as_secs_f64(),
                "industry_min_other_peers":10,"label_censoring":"observation and exit in same calendar year",
                "entry_price":entry_price.as_str(),"entry_price_description":entry_price.description(),
                "entry_uses_full_day_range":entry_price.uses_full_day_range(),
                "entry_time":"observation t close, signal t+1, entry t+2; exit t+2+h open",
                "entry_limitation":if entry_price.uses_full_day_range() {
                    "Full-day high/low OHLC fill-price scenario only; unavailable before the day ends, not a live entry signal or minute-level executable backtest"
                } else {
                    "Open-price event return; not a minute-level executable backtest"
                },
                "bootstrap":"calendar-month means, 2000 resamples, xorshift64* seed 42; percentile 95% CI; no multiple-testing adjustment",
                "diagnostics":"mean daily correlations of paired 5-day factor ranks; controls ranked on eligible universe",
                "cost":COST,"cost_application":"each high/low long event return minus fixed roundtrip cost including slippage; entry proxy adds no further slippage; spread is gross high-minus-low, not a tradable long-short portfolio",
                "limitations":["overlapping event returns, not portfolio NAV","historical ST and limit-fill availability not modeled","no market impact","missing delisted observations can bias estimates","2026 is not pristine out-of-sample"],
            }),
        )?;
        writeln!(w)?;
        Ok(())
    })?;
    eprintln!(
        "flow research: complete in {:.1}s, {} summaries; no directions or weights fitted",
        started.elapsed().as_secs_f64(),
        summaries.len()
    );
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn stock(n: usize) -> Stock {
        Stock {
            code: "000001.SZ".into(),
            name: "Test, name".into(),
            bars: vec![
                Bar {
                    open: 10.0,
                    high: 11.0,
                    low: 9.0,
                    close: 10.0,
                    vol: 100.0,
                    amount: 1000.0,
                    adj_factor: 1.0,
                    turnover_rate: 1.0,
                    circ_mv: 100000.0,
                    net: 10.0,
                    industry: Some("test".into()),
                    eligible: true,
                };
                n
            ],
        }
    }

    fn equal_nan(a: f64, b: f64) -> bool {
        (a.is_nan() && b.is_nan()) || (a - b).abs() < 1e-12
    }

    #[test]
    fn units_missing_and_zero_are_distinct() {
        let mut s = stock(30);
        let original = indicators(&s);
        assert!((original.factors[20][2] - 0.1).abs() < 1e-12);
        assert!((original.factors[20][3] - 0.1).abs() < 1e-12);
        assert_eq!(original.factors[20][4], 1.0);
        assert!((original.factors[20][5] - 0.2).abs() < 1e-12);
        s.bars[20].net = f64::NAN;
        let missing = indicators(&s);
        for f in [2, 3, 4, 5] {
            assert!(missing.factors[20][f].is_nan(), "factor {f}");
        }
        s.bars[20].net = 0.0;
        let zero = indicators(&s);
        assert_eq!(zero.factors[20][2], 0.0);
        assert_eq!(zero.factors[20][4], 0.8);
        s.bars[20].amount = 0.0;
        assert!(indicators(&s).factors[20][2].is_nan());
    }

    #[test]
    fn strict_calendar_windows_and_baseline_exclude_recent_days() {
        let mut s = stock(30);
        for b in &mut s.bars[20..23] {
            b.amount = 10000.0;
        }
        assert_eq!(indicators(&s).factors[22][0], 10.0);
        for b in &mut s.bars[20..25] {
            b.turnover_rate = 3.0;
        }
        assert_eq!(indicators(&s).factors[24][1], 3.0);
        s.bars[3].amount = f64::NAN;
        assert!(indicators(&s).factors[22][0].is_nan());
    }

    #[test]
    fn factors_do_not_observe_future_and_volatility_is_sample_std() {
        let mut s = stock(60);
        for (t, b) in s.bars.iter_mut().enumerate() {
            b.close = 10.0 + t as f64 * 0.1;
        }
        let original = indicators(&s);
        for b in &mut s.bars[30..] {
            b.close = 999.0;
            b.amount = 0.0;
            b.net = f64::NAN;
            b.turnover_rate = 5.0;
        }
        let changed = indicators(&s);
        for t in 0..30 {
            for f in 0..N {
                assert!(
                    equal_nan(original.factors[t][f], changed.factors[t][f]),
                    "t={t} f={f}"
                );
            }
        }
        assert!((sample_std(&[1.0, 2.0, 3.0]) - 1.0).abs() < 1e-12);
    }

    #[test]
    fn execution_lag_adjustment_year_censor_and_suspension() {
        let start = NaiveDate::from_ymd_opt(2024, 12, 24).unwrap();
        let dates: Vec<_> = (0..15).map(|i| start + chrono::Duration::days(i)).collect();
        let mut s = stock(15);
        s.bars[2].open = 5.0;
        s.bars[2].adj_factor = 2.0;
        s.bars[3].open = 11.0;
        assert!((forward_return(&dates, &s.bars, 0, 1, EntryPriceModel::Open) - 0.1).abs() < 1e-12);
        s.bars[0].close = 99999.0;
        s.bars[1].open = 99999.0;
        assert!((forward_return(&dates, &s.bars, 0, 1, EntryPriceModel::Open) - 0.1).abs() < 1e-12);
        assert!(forward_return(&dates, &s.bars, 5, 1, EntryPriceModel::Open).is_nan());
        assert!(forward_return(&dates, &s.bars, 14, 1, EntryPriceModel::Open).is_nan());
        s.bars[2].vol = 0.0;
        assert!(forward_return(&dates, &s.bars, 0, 1, EntryPriceModel::Open).is_nan());
    }

    #[test]
    fn daily_range_proxy_changes_entry_only_and_preserves_observation_factors() {
        let start = NaiveDate::from_ymd_opt(2024, 1, 1).unwrap();
        let dates: Vec<_> = (0..60).map(|i| start + chrono::Duration::days(i)).collect();
        let mut s = stock(dates.len());
        let t = 30;
        let original = indicators(&s).factors[t];
        s.bars[t + 2].low = 9.0;
        s.bars[t + 2].high = 12.0;
        s.bars[t + 2].adj_factor = 2.0;
        s.bars[t + 3].open = 24.2;
        let model = EntryPriceModel::DailyRangeTwoThirds;
        assert_eq!(model.adjusted_entry(&s.bars[t + 2]), 22.0);
        assert!((forward_return(&dates, &s.bars, t, 1, model) - 0.1).abs() < 1e-12);
        // Neither the observation day's range nor the exit day's range is the fill.
        for day in [t, t + 1, t + 3] {
            s.bars[day].low = 1.0;
            s.bars[day].high = 1000.0;
        }
        assert!((forward_return(&dates, &s.bars, t, 1, model) - 0.1).abs() < 1e-12);
        // Restore observation data; future entry/exit changes must not alter factors.
        s.bars[t].low = 9.0;
        s.bars[t].high = 11.0;
        let changed = indicators(&s).factors[t];
        for f in 0..N {
            assert!(equal_nan(original[f], changed[f]), "factor {f}");
        }
        s.bars[t + 2].high = 15.0;
        assert!((forward_return(&dates, &s.bars, t, 1, model) - (24.2 / 26.0 - 1.0)).abs() < 1e-12);
    }

    #[test]
    fn daily_range_proxy_excludes_invalid_missing_and_suspended_entry() {
        let start = NaiveDate::from_ymd_opt(2024, 1, 1).unwrap();
        let dates: Vec<_> = (0..5).map(|i| start + chrono::Duration::days(i)).collect();
        let model = EntryPriceModel::DailyRangeTwoThirds;
        for (low, high, vol, adj) in [
            (12.0, 9.0, 100.0, 1.0),
            (f64::NAN, 12.0, 100.0, 1.0),
            (9.0, f64::NAN, 100.0, 1.0),
            (9.0, 12.0, 0.0, 1.0),
            (9.0, 12.0, 100.0, f64::NAN),
        ] {
            let mut s = stock(dates.len());
            s.bars[2].low = low;
            s.bars[2].high = high;
            s.bars[2].vol = vol;
            s.bars[2].adj_factor = adj;
            assert!(forward_return(&dates, &s.bars, 0, 1, model).is_nan());
        }
    }

    #[test]
    fn leave_one_out_requires_ten_actual_other_peers() {
        let values: Vec<_> = (0..11).map(|i| i as f64).collect();
        let mask = vec![true; 11];
        let industry = vec![Some("same"); 11];
        let excess = peer_demean(&values, &mask, &industry);
        assert_eq!(excess[0], -5.5);
        assert_eq!(excess[10], 5.5);
        let mut mask = mask;
        mask[10] = false;
        assert!(peer_demean(&values, &mask, &industry)
            .iter()
            .all(|x| x.is_nan()));
    }

    #[test]
    fn ranks_keep_ties_no_artificial_spread_and_minimum_pairs() {
        assert_eq!(ranks(&[2.0, 1.0, 2.0, 4.0]), vec![2.5, 1.0, 2.5, 4.0]);
        let y: Vec<_> = (0..120).map(|i| i as f64 / 1000.0).collect();
        let flat = vec![1.0; 120];
        let mask = vec![true; 120];
        let (result, _) = cross_section(&flat, &y, &y, &mask);
        assert_eq!(result.n, 120);
        assert!(result.ic.is_nan());
        assert!(result.spread.is_nan());
        let (ranked, _) = cross_section(&y, &y, &y, &mask);
        assert!((ranked.ic - 1.0).abs() < 1e-12);
        assert_eq!(ranked.high_n, 24);
        assert_eq!(ranked.low_n, 24);
        assert!((ranked.high_net - (ranked.high_gross - COST)).abs() < 1e-12);
        let (small, _) = cross_section(&y[..99], &y[..99], &y[..99], &mask[..99]);
        assert!(small.ic.is_nan());
        assert!(small.spread.is_nan());
    }

    #[test]
    fn monthly_bootstrap_is_deterministic_and_requires_six_months() {
        let mut days = Vec::new();
        for month in 1..=12 {
            days.push((
                NaiveDate::from_ymd_opt(2024, month, 1).unwrap(),
                month as f64 / 100.0,
            ));
        }
        assert_eq!(monthly_ci(&days[..5]), [None, None]);
        assert_eq!(monthly_ci(&days), monthly_ci(&days));
        let ci = monthly_ci(&days);
        assert!(ci[0].unwrap() < 0.065 && ci[1].unwrap() > 0.065);
    }

    #[test]
    fn synthetic_end_to_end_writes_usable_reports() {
        let start = NaiveDate::from_ymd_opt(2023, 10, 1).unwrap();
        let dates: Vec<_> = (0..165)
            .map(|i| start + chrono::Duration::days(i))
            .collect();
        let stocks: Vec<_> = (0..120)
            .map(|i| {
                let mut s = stock(dates.len());
                s.code = format!("{i:06}.SZ");
                for (t, b) in s.bars.iter_mut().enumerate() {
                    let p = 10.0 * (1.0 + i as f64 * 0.0001).powi(t as i32);
                    b.open = p;
                    b.close = p;
                    b.low = p * 0.98;
                    b.high = p * 1.02;
                    b.amount = 10000.0 + i as f64 * 50.0;
                    b.circ_mv = 100000.0 + i as f64 * 1000.0;
                    b.net = (i as f64 - 60.0) * 10.0 + (t % 7) as f64;
                    b.turnover_rate = 1.0 + i as f64 * 0.01;
                    b.eligible = t >= 60;
                }
                s
            })
            .collect();
        let data = Dataset { dates, stocks };
        let dir = std::env::temp_dir().join(format!(
            "quant-flow-analysis-test-{}-{}",
            std::process::id(),
            chrono::Utc::now().timestamp_nanos_opt().unwrap()
        ));
        analyze(&data, &dir, EntryPriceModel::Open).unwrap();
        let results: serde_json::Value =
            serde_json::from_slice(&fs::read(dir.join("factor_results.json")).unwrap()).unwrap();
        assert_eq!(results.as_array().unwrap().len(), 80);
        let pressure = results
            .as_array()
            .unwrap()
            .iter()
            .find(|v| {
                v["factor"] == "net_pressure_5d" && v["horizon"] == 5 && v["segment"] == "all"
            })
            .unwrap();
        assert!(pressure["rank_ic"].as_f64().unwrap() > 0.999);
        assert!(pressure["observations"].as_u64().unwrap() > 1000);
        assert!(
            pressure["high_net_return"].as_f64().unwrap()
                > pressure["low_net_return"].as_f64().unwrap()
        );
        let dashboard = fs::read_to_string(dir.join("latest_dashboard.csv")).unwrap();
        assert_eq!(dashboard.lines().count(), 121);
        assert!(dashboard.contains("\"Test, name\""));
        let coverage = fs::read_to_string(dir.join("coverage.csv")).unwrap();
        assert_eq!(coverage.lines().count(), 74);
        assert!(dir.join("daily_net_pressure_5d_5d_all.csv").exists());
        assert!(dir.join("analysis_manifest.json").exists());
        let proxy_dir = dir.join("daily-range");
        analyze(&data, &proxy_dir, EntryPriceModel::DailyRangeTwoThirds).unwrap();
        let manifest: serde_json::Value =
            serde_json::from_slice(&fs::read(proxy_dir.join("analysis_manifest.json")).unwrap())
                .unwrap();
        assert_eq!(
            manifest["entry_price"],
            EntryPriceModel::DailyRangeTwoThirds.as_str()
        );
        assert_eq!(manifest["entry_uses_full_day_range"], true);
        assert_eq!(manifest["factor_summaries"], 80);
        assert!(manifest["entry_limitation"]
            .as_str()
            .unwrap()
            .contains("not a live entry signal"));
        assert_eq!(
            dashboard,
            fs::read_to_string(proxy_dir.join("latest_dashboard.csv")).unwrap()
        );
        let proxy_results: serde_json::Value =
            serde_json::from_slice(&fs::read(proxy_dir.join("factor_results.json")).unwrap())
                .unwrap();
        let proxy_pressure = proxy_results
            .as_array()
            .unwrap()
            .iter()
            .find(|v| {
                v["factor"] == "net_pressure_5d" && v["horizon"] == 5 && v["segment"] == "all"
            })
            .unwrap();
        assert!(
            proxy_pressure["high_net_return"].as_f64().unwrap()
                < pressure["high_net_return"].as_f64().unwrap()
        );
        fs::remove_dir_all(dir).unwrap();
    }

    #[test]
    fn unusable_input_fails_instead_of_claiming_success() {
        let data = Dataset {
            dates: vec![],
            stocks: vec![],
        };
        assert!(analyze(
            &data,
            Path::new("/tmp/unused-flow-test"),
            EntryPriceModel::Open
        )
        .is_err());
    }
}
