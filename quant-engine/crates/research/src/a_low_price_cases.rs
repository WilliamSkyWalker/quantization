//! Descriptive rally episodes: selection at detection close, never an executable signal.
use crate::{
    Error,
    data::{Bar, Dataset, Stock},
};
use chrono::{Datelike, NaiveDate};
use rayon::prelude::*;
use serde::Serialize;
use serde_json::json;
use std::{collections::HashSet, fmt::Write as _, path::Path};

fn price(b: &Bar) -> Option<f64> {
    let p = b.close * b.adj_factor;
    (b.close > 0.0 && b.adj_factor > 0.0 && p.is_finite() && b.vol > 0.0).then_some(p)
}
fn change(bars: &[Bar], a: usize, b: usize) -> Option<f64> {
    Some(price(&bars[b])? / price(&bars[a])? - 1.0)
}
fn mean(bars: &[Bar], f: impl Fn(&Bar) -> f64) -> Option<f64> {
    let values: Vec<_> = bars.iter().map(f).collect();
    (!values.is_empty() && values.iter().all(|v| v.is_finite()))
        .then(|| values.iter().sum::<f64>() / values.len() as f64)
}
fn pressure(bars: &[Bar]) -> Option<f64> {
    let amount = mean(bars, |b| b.amount)?;
    if amount <= 0.0 || bars.iter().any(|b| b.amount <= 0.0) {
        return None;
    }
    // Raw moneyflow net: 万元; daily amount: 千元.
    Some(mean(bars, |b| b.net)? * 10.0 / amount)
}

#[derive(Debug, Serialize)]
struct Event {
    code: String,
    name: String,
    base_date: NaiveDate,
    detection_date: NaiveDate,
    base_price: f64,
    base_float_cap_yi: f64,
    prior20_return: f64,
    rally5_return: f64,
    prior5_pressure: Option<f64>,
    rally5_pressure: Option<f64>,
    rally_amount_vs_prior20: f64,
    rally_turnover_pct: Option<f64>,
    rally_positive_flow_days: Option<usize>,
    post20_return: Option<f64>,
    post20_peak_return: Option<f64>,
    post20_max_drawdown: Option<f64>,
    #[serde(skip)]
    stock_index: usize,
    #[serde(skip)]
    t: usize,
}

fn stock_events(data: &Dataset, si: usize) -> Vec<Event> {
    let s = &data.stocks[si];
    let b = &s.bars;
    let mut out = Vec::new();
    let mut last = None;
    for t in 25..b.len() {
        if data.dates[t].year() < 2024 || last.is_some_and(|p| t - p <= 60) {
            continue;
        }
        let base = t - 5;
        // Require a full observed 20-day background plus the five-day rally.
        if !b[base].eligible
            || b[base].close > 5.0
            || !b[base].circ_mv.is_finite()
            || b[base].circ_mv <= 0.0
            || b[base].circ_mv > 500_000.0
            || b[base - 20..=t]
                .iter()
                .any(|v| price(v).is_none() || !v.amount.is_finite() || v.amount <= 0.0)
        {
            continue;
        }
        let Some(gain) = change(b, base, t) else {
            continue;
        };
        if gain < 0.20 {
            continue;
        }
        let rally = &b[base + 1..=t];
        let background = &b[base - 19..=base];
        let mut end_return = None;
        let mut peak_return = None;
        let mut drawdown = None;
        if t + 20 < b.len() && b[t..=t + 20].iter().all(|v| price(v).is_some()) {
            let initial = price(&b[t]).unwrap();
            let mut peak = initial;
            let mut dd: f64 = 0.0;
            for v in &b[t + 1..=t + 20] {
                let p = price(v).unwrap();
                peak = peak.max(p);
                dd = dd.min(p / peak - 1.0);
            }
            end_return = change(b, t, t + 20);
            peak_return = Some(peak / initial - 1.0);
            drawdown = Some(dd);
        }
        out.push(Event {
            code: s.code.clone(),
            name: s.name.clone(),
            base_date: data.dates[base],
            detection_date: data.dates[t],
            base_price: b[base].close,
            base_float_cap_yi: b[base].circ_mv / 10_000.0,
            prior20_return: change(b, base - 20, base).unwrap(),
            rally5_return: gain,
            prior5_pressure: pressure(&b[base - 4..=base]),
            rally5_pressure: pressure(rally),
            rally_amount_vs_prior20: mean(rally, |v| v.amount).unwrap()
                / mean(background, |v| v.amount).unwrap(),
            rally_turnover_pct: mean(rally, |v| v.turnover_rate),
            rally_positive_flow_days: rally
                .iter()
                .all(|v| v.net.is_finite())
                .then(|| rally.iter().filter(|v| v.net > 0.0).count()),
            post20_return: end_return,
            post20_peak_return: peak_return,
            post20_max_drawdown: drawdown,
            stock_index: si,
            t,
        });
        last = Some(t);
    }
    out
}

fn median(mut values: Vec<f64>) -> Option<f64> {
    if values.is_empty() {
        return None;
    }
    values.sort_by(f64::total_cmp);
    let n = values.len();
    Some(if n % 2 == 0 {
        (values[n / 2 - 1] + values[n / 2]) / 2.0
    } else {
        values[n / 2]
    })
}
fn summary(events: &[&Event]) -> serde_json::Value {
    let pre: Vec<_> = events.iter().filter_map(|e| e.prior5_pressure).collect();
    let during: Vec<_> = events.iter().filter_map(|e| e.rally5_pressure).collect();
    let post: Vec<_> = events.iter().filter_map(|e| e.post20_return).collect();
    let frac = |values: &[f64]| {
        if values.is_empty() {
            None
        } else {
            Some(values.iter().filter(|v| **v > 0.0).count() as f64 / values.len() as f64)
        }
    };
    json!({"events":events.len(),"pre_flow_valid":pre.len(),"pre_flow_positive_fraction":frac(&pre),
        "rally_flow_valid":during.len(),"rally_flow_positive_fraction":frac(&during),
        "post20_valid":post.len(),"post20_positive_fraction":frac(&post),"post20_return_median":median(post),
        "rally_amount_ratio_median":median(events.iter().map(|e|e.rally_amount_vs_prior20).collect())})
}
fn pct(v: Option<f64>) -> String {
    v.map_or("缺失".into(), |v| format!("{:.1}%", v * 100.0))
}
fn escape(s: &str) -> String {
    s.replace('&', "&amp;")
        .replace('<', "&lt;")
        .replace('>', "&gt;")
        .replace('"', "&quot;")
}

fn chart(s: &Stock, t: usize) -> String {
    let lo = t - 25;
    let hi = (t + 20).min(s.bars.len() - 1);
    let initial = price(&s.bars[t - 5]).unwrap();
    let values: Vec<_> = (lo..=hi)
        .filter_map(|i| price(&s.bars[i]).map(|p| (i, p / initial * 100.0)))
        .collect();
    let min = values.iter().map(|v| v.1).fold(f64::INFINITY, f64::min) * 0.98;
    let max = values.iter().map(|v| v.1).fold(f64::NEG_INFINITY, f64::max) * 1.02;
    let x = |i: usize| 40.0 + (i - lo) as f64 / 45.0 * 680.0;
    let y = |p: f64| 180.0 - (p - min) / (max - min) * 160.0;
    let mut svg = format!(
        "<svg viewBox='0 0 750 290' role='img' aria-label='复权价格与每日资金净流入占成交额'><rect x='{:.1}' y='10' width='{:.1}' height='265' fill='#fff3cb'/><text x='40' y='12'>价格（上涨前=100）</text><line x1='40' x2='720' y1='235' y2='235' stroke='#94a3b8'/>",
        x(t - 5),
        x(t) - x(t - 5)
    );
    // Separate segments: missing bars never receive a fabricated connecting price.
    let mut prev = None;
    for (i, p) in values {
        if let Some((pi, pp)) = prev {
            if i == pi + 1 {
                write!(svg,"<line x1='{:.1}' y1='{:.1}' x2='{:.1}' y2='{:.1}' stroke='#2563eb' stroke-width='2'/>",x(pi),y(pp),x(i),y(p)).unwrap();
            }
        }
        prev = Some((i, p));
    }
    let flows: Vec<_> = (lo..=hi)
        .filter_map(|i| pressure(&s.bars[i..=i]).map(|p| (i, p)))
        .collect();
    let scale = flows.iter().map(|(_, p)| p.abs()).fold(0.001, f64::max);
    for (i, p) in flows {
        let h = p.abs() / scale * 32.0;
        write!(
            svg,
            "<rect x='{:.1}' y='{:.1}' width='6' height='{h:.1}' fill='{}'/>",
            x(i) - 3.0,
            if p >= 0.0 { 235.0 - h } else { 235.0 },
            if p >= 0.0 { "#dc2626" } else { "#059669" }
        )
        .unwrap();
    }
    write!(svg,"<text x='40' y='199'>净流入/成交额（各图纵轴 ±{:.1}%）</text><text x='40' y='287'>前20天</text><text x='{:.1}' y='287'>上涨5天</text><text x='{:.1}' y='287'>识别日</text><text x='650' y='287'>后20天</text></svg>",scale*100.0,x(t-5),x(t)).unwrap();
    svg
}

pub async fn run(
    config: &quant_core::config::Config,
    cache: &Path,
    output: &Path,
) -> Result<(), Error> {
    let started = std::time::Instant::now();
    rayon::ThreadPoolBuilder::new()
        .num_threads(std::thread::available_parallelism()?.get().min(8))
        .build_global()?;
    std::fs::create_dir_all(output)?;
    let lock = std::fs::OpenOptions::new()
        .create(true)
        .truncate(false)
        .write(true)
        .open(output.join(".cases.lock"))?;
    lock.try_lock()?;
    crate::atomic_json(&output.join("run_state.json"), &json!({"status":"running"}))?;
    let protocol = json!({"study":"descriptive historical rally cases, not a strategy or causal attribution",
        "start":"2023-10-01","end":"2026-09-30","selection":"first eligible rolling 5-session adjusted close gain >=20%; base raw close <=5 CNY; base float market cap <=50 yi CNY; 60-session cooldown per stock",
        "background":"20 sessions ending at base; prior5 flow ends at base; rally flow covers base+1 through detection",
        "forward":"20 sessions after detection, complete observed prices required; missing remains null; peak/drawdown use adjusted CLOSE, not intraday high/low",
        "showcase":"2026 complete-forward events: six largest rally5 gains, three lowest and three highest post20 returns, unique stocks; chosen retrospectively",
        "limitations":["historical ST status not filtered; stock names are current metadata","survivorship and missing data may affect sample","only selected rallies: cannot estimate predictive success without a non-rally control group","daily net flow is provider trade classification, not proof of institutional accumulation","no minute timing, fill, cost, or portfolio return modeled"]});
    crate::atomic_json(&output.join("protocol.json"), &protocol)?;
    let pool =
        quant_db::pool::create_pool(&config.database.url(), &config.database.schema, 4).await?;
    let data = crate::data::load(
        &pool,
        cache,
        NaiveDate::from_ymd_opt(2023, 10, 1).unwrap(),
        NaiveDate::from_ymd_opt(2026, 9, 30).unwrap(),
    )
    .await?;
    let mut events: Vec<_> = (0..data.stocks.len())
        .into_par_iter()
        .flat_map_iter(|si| stock_events(&data, si))
        .collect();
    events.sort_by(|a, b| {
        a.detection_date
            .cmp(&b.detection_date)
            .then(a.code.cmp(&b.code))
    });
    if events.is_empty() {
        return Err("no qualifying rally cases".into());
    }
    let mut picked = Vec::new();
    let mut used = HashSet::new();
    for mode in 0..3 {
        let mut candidates: Vec<_> = events
            .iter()
            .enumerate()
            .filter(|(_, e)| e.detection_date.year() == 2026 && e.post20_return.is_some())
            .collect();
        candidates.sort_by(|(_, a), (_, b)| {
            match mode {
                0 => b.rally5_return.total_cmp(&a.rally5_return),
                1 => a
                    .post20_return
                    .unwrap()
                    .total_cmp(&b.post20_return.unwrap()),
                _ => b
                    .post20_return
                    .unwrap()
                    .total_cmp(&a.post20_return.unwrap()),
            }
            .then(a.code.cmp(&b.code))
        });
        let mut count = 0;
        for (i, e) in candidates {
            if used.insert(e.code.clone()) {
                picked.push(i);
                count += 1;
                if count == if mode == 0 { 6 } else { 3 } {
                    break;
                }
            }
        }
    }
    let yearly:Vec<_>=(2024..=2026).map(|year|json!({"year":year,"stats":summary(&events.iter().filter(|e|e.detection_date.year()==year).collect::<Vec<_>>())})).collect();
    let report = json!({"protocol":protocol,"summary":yearly,"showcase":picked.iter().map(|i|&events[*i]).collect::<Vec<_>>(),"events":events});
    crate::atomic_json(&output.join("cases.json"), &report)?;
    let paths:Vec<_>=picked.iter().map(|&i|{let e=&events[i];let s=&data.stocks[e.stock_index];
        json!({"code":e.code,"detection_date":e.detection_date,"daily":(e.t-25..=(e.t+20).min(s.bars.len()-1)).map(|d|{let b=&s.bars[d];json!({"date":data.dates[d],"relative_day":d as i64-e.t as i64,"close":b.close,"adjusted_close":price(b),"amount_cny":b.amount*1000.0,"net_cny":b.net*10000.0,"flow_pressure":pressure(&s.bars[d..=d]),"turnover_pct":b.turnover_rate})}).collect::<Vec<_>>()})}).collect();
    crate::atomic_json(&output.join("showcase_daily.json"), &json!(paths))?;
    let mut html = String::from(
        "<!doctype html><meta charset='utf-8'><title>低价小市值上涨案例</title><style>body{font:16px system-ui;max-width:1100px;margin:32px auto;padding:0 20px;background:#f5f7fb;color:#17243b}article{background:white;padding:24px;border-radius:12px;margin:24px 0}svg{width:100%;max-height:360px}svg text{font-size:12px;fill:#64748b}table{border-collapse:collapse;width:100%;font-size:14px}th,td{padding:8px;border-bottom:1px solid #ddd;text-align:right}th:first-child,td:first-child{text-align:left}.note{color:#526078;line-height:1.7}</style><h1>低价、小流通市值：上涨过程复盘</h1><p class='note'>截至 2026-09-30。上涨前 ≤5 元、流通市值 ≤50 亿元，5 日复权收盘涨幅 ≥20%；同股间隔超过60个交易日。黄色区域为上涨5天，蓝线为复权价格，红/绿柱为正/负净流入占成交额。横轴相对识别日。案例按已发生的涨幅及后续表现挑选，不代表交易信号或预测成功率；名称为当前元数据，未筛历史ST。资金流数据不能单独证明上涨原因。</p>",
    );
    for &i in &picked {
        let e = &events[i];
        write!(html,"<article><h2>{} {}</h2><p>{} → {} · 起点 {:.2} 元 · 流通市值 {:.1} 亿元</p><table><tr><th>前20日涨幅</th><th>上涨5日</th><th>前5日净流入占比</th><th>上涨5日净流入占比</th><th>成交额放大</th><th>后20日涨幅</th><th>后20日最大回撤</th></tr><tr><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{:.2}倍</td><td>{}</td><td>{}</td></tr></table>{}</article>",escape(&e.name),e.code,e.base_date,e.detection_date,e.base_price,e.base_float_cap_yi,pct(Some(e.prior20_return)),pct(Some(e.rally5_return)),pct(e.prior5_pressure),pct(e.rally5_pressure),e.rally_amount_vs_prior20,pct(e.post20_return),pct(e.post20_max_drawdown),chart(&data.stocks[e.stock_index],e.t)).unwrap();
    }
    std::fs::write(output.join("report.html.tmp"), html)?;
    std::fs::rename(output.join("report.html.tmp"), output.join("report.html"))?;
    crate::atomic_json(
        &output.join("run_state.json"),
        &json!({"status":"complete","events":events.len(),"showcase":picked.len(),"elapsed_seconds":started.elapsed().as_secs_f64()}),
    )?;
    println!(
        "{}",
        serde_json::to_string_pretty(
            &json!({"summary":yearly,"showcase":picked.iter().map(|i|&events[*i]).collect::<Vec<_>>(),"output":output})
        )?
    );
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn flow_units_and_missing_are_preserved() {
        let b = Bar {
            amount: 1000.0,
            net: 10.0,
            ..Bar::default()
        };
        assert_eq!(pressure(&[b.clone()]), Some(0.1));
        assert_eq!(pressure(&[b, Bar::default()]), None);
    }
    #[test]
    fn selection_uses_historical_size_and_does_not_require_future_prices() {
        let mut data = Dataset {
            dates: (0..110)
                .map(|i| NaiveDate::from_ymd_opt(2026, 1, 1).unwrap() + chrono::Duration::days(i))
                .collect(),
            stocks: vec![Stock {
                code: "TEST".into(),
                name: "Test".into(),
                bars: vec![
                    Bar {
                        close: 2.0,
                        adj_factor: 1.0,
                        vol: 100.0,
                        amount: 100.0,
                        net: -1.0,
                        circ_mv: 100000.0,
                        eligible: true,
                        ..Bar::default()
                    };
                    110
                ],
            }],
        };
        for b in &mut data.stocks[0].bars[30..] {
            b.close = 2.6;
            b.circ_mv = 900000.0;
        }
        let events = stock_events(&data, 0);
        assert_eq!(events.len(), 1);
        assert_eq!(events[0].t, 30);
        assert!(events[0].rally5_pressure.unwrap() < 0.0);
        assert_eq!(events[0].post20_return, Some(0.0));
        data.stocks[0].bars[31] = Bar::default();
        let missing = stock_events(&data, 0);
        assert_eq!(missing.len(), 1);
        assert_eq!(missing[0].t, 30);
        assert_eq!(missing[0].post20_return, None);
        assert_eq!(missing[0].rally5_return, events[0].rally5_return);
        data.stocks[0].bars[25].circ_mv = 600000.0;
        assert!(stock_events(&data, 0).is_empty());
    }
}
