//! Bounded parallel SQL reads and complete, resumable raw moneyflow caches.
use crate::Error;
use chrono::{Datelike, NaiveDate, Utc};
use flate2::{read::GzDecoder, write::GzEncoder, Compression};
use rayon::prelude::*;
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use sqlx::{FromRow, MySqlPool};
use std::{
    collections::{BTreeMap, HashMap, HashSet},
    fs::File,
    io::{BufReader, BufWriter, Write},
    path::{Path, PathBuf},
    sync::Arc,
    time::{Duration, Instant},
};
use tracing::{info, warn};

pub struct Dataset {
    pub dates: Vec<NaiveDate>,
    pub stocks: Vec<Stock>,
}
pub struct Stock {
    pub code: String,
    pub name: String,
    pub bars: Vec<Bar>,
}
#[derive(Clone, Debug)]
pub struct Bar {
    pub open: f64,
    pub high: f64,
    pub low: f64,
    pub close: f64,
    pub vol: f64,
    pub amount: f64,
    pub adj_factor: f64,
    pub turnover_rate: f64,
    pub circ_mv: f64,
    pub net: f64,
    pub industry: Option<String>,
    pub eligible: bool,
}
impl Default for Bar {
    fn default() -> Self {
        Self {
            open: f64::NAN,
            high: f64::NAN,
            low: f64::NAN,
            close: f64::NAN,
            vol: f64::NAN,
            amount: f64::NAN,
            adj_factor: f64::NAN,
            turnover_rate: f64::NAN,
            circ_mv: f64::NAN,
            net: f64::NAN,
            industry: None,
            eligible: false,
        }
    }
}

const FIELDS: &[&str] = &[
    "ts_code",
    "trade_date",
    "buy_sm_vol",
    "buy_sm_amount",
    "sell_sm_vol",
    "sell_sm_amount",
    "buy_md_vol",
    "buy_md_amount",
    "sell_md_vol",
    "sell_md_amount",
    "buy_lg_vol",
    "buy_lg_amount",
    "sell_lg_vol",
    "sell_lg_amount",
    "buy_elg_vol",
    "buy_elg_amount",
    "sell_elg_vol",
    "sell_elg_amount",
    "net_mf_vol",
    "net_mf_amount",
];
#[derive(Serialize, Deserialize)]
struct RawDay {
    date: String,
    fetched_at: String,
    fields: Vec<String>,
    items: Vec<Vec<Value>>,
}
impl RawDay {
    fn validate(&self, expected: &str) -> Result<(usize, usize), Error> {
        if self.date != expected || self.items.is_empty() || self.items.len() >= 6000 {
            return Err(format!(
                "moneyflow {expected}: wrong date, empty or potentially truncated response"
            )
            .into());
        }
        let fields: HashSet<&str> = self.fields.iter().map(String::as_str).collect();
        if fields.len() != self.fields.len() || FIELDS.iter().any(|f| !fields.contains(f)) {
            return Err(format!("moneyflow {expected}: missing or duplicated raw fields").into());
        }
        let ci = self.fields.iter().position(|s| s == "ts_code").unwrap();
        let di = self.fields.iter().position(|s| s == "trade_date").unwrap();
        let ni = self
            .fields
            .iter()
            .position(|s| s == "net_mf_amount")
            .unwrap();
        let mut keys = HashSet::new();
        for row in &self.items {
            if row.len() != self.fields.len() || row[di].as_str() != Some(expected) {
                return Err(format!("moneyflow {expected}: malformed row/date").into());
            }
            let code = row[ci]
                .as_str()
                .filter(|c| !c.is_empty())
                .ok_or("invalid moneyflow stock code")?;
            if !keys.insert(code) {
                return Err(format!("moneyflow {expected}: duplicate key {code}").into());
            }
            for (i, value) in row.iter().enumerate() {
                if FIELDS[2..].contains(&self.fields[i].as_str())
                    && !value.is_null()
                    && !value.as_f64().is_some_and(f64::is_finite)
                {
                    return Err(format!(
                        "moneyflow {expected}: invalid numeric field {}",
                        self.fields[i]
                    )
                    .into());
                }
            }
        }
        Ok((ci, ni))
    }
}
fn cache_path(cache: &Path, date: NaiveDate) -> PathBuf {
    cache.join(format!("moneyflow_{}.json.gz", date.format("%Y%m%d")))
}
fn read_raw(path: &Path, date: NaiveDate) -> Result<RawDay, Error> {
    let raw: RawDay = serde_json::from_reader(BufReader::new(GzDecoder::new(File::open(path)?)))?;
    raw.validate(&date.format("%Y%m%d").to_string())?;
    Ok(raw)
}
async fn calendar(
    pool: &MySqlPool,
    start: NaiveDate,
    end: NaiveDate,
) -> Result<Vec<NaiveDate>, Error> {
    if end < start {
        return Err("end precedes start".into());
    }
    let rows:Vec<(NaiveDate,)> = sqlx::query_as("SELECT cal_date FROM a_trade_cal WHERE exchange='SSE' AND is_open=1 AND cal_date BETWEEN ? AND ? ORDER BY cal_date").bind(start).bind(end).fetch_all(pool).await?;
    let dates: Vec<_> = rows.into_iter().map(|r| r.0).collect();
    if dates.is_empty() || dates.windows(2).any(|p| p[0] >= p[1]) {
        return Err("empty or duplicate SSE calendar".into());
    }
    Ok(dates)
}

pub(crate) async fn tushare_ip() -> Result<Option<std::net::IpAddr>, Error> {
    if let Ok(value) = std::env::var("QUANT_TUSHARE_API_IP") {
        return Ok(Some(value.parse()?));
    }
    if let Ok(Ok(mut addresses)) = tokio::time::timeout(
        Duration::from_secs(3),
        tokio::net::lookup_host(("api.tushare.pro", 443)),
    )
    .await
    {
        if let Some(address) = addresses.next() {
            return Ok(Some(address.ip()));
        }
    }
    // A fresh, process-scoped resolver fallback retains HTTPS hostname validation.
    // In particular, never persist a proxy's synthetic address between runs.
    for server in ["223.5.5.5", "1.1.1.1"] {
        let output = tokio::time::timeout(
            Duration::from_secs(4),
            tokio::process::Command::new("dig")
                .args([
                    "+time=2",
                    "+tries=1",
                    "+short",
                    &format!("@{server}"),
                    "api.tushare.pro",
                    "A",
                ])
                .kill_on_drop(true)
                .output(),
        )
        .await;
        if let Ok(Ok(output)) = output {
            if output.status.success() {
                for line in String::from_utf8_lossy(&output.stdout).lines() {
                    if let Ok(ip) = line.trim().parse() {
                        info!("Using freshly resolved Tushare DNS override");
                        return Ok(Some(ip));
                    }
                }
            }
        }
    }
    Err("Tushare DNS resolution failed with system resolver and fallback resolvers; set a freshly resolved QUANT_TUSHARE_API_IP".into())
}

pub async fn fetch(
    pool: &MySqlPool,
    cache: &Path,
    start: NaiveDate,
    end: NaiveDate,
) -> Result<(), Error> {
    let timer = Instant::now();
    let dates = calendar(pool, start, end).await?;
    std::fs::create_dir_all(cache)?;
    let lock = std::fs::OpenOptions::new()
        .create(true)
        .truncate(false)
        .write(true)
        .open(cache.join(".fetch.lock"))?;
    lock.try_lock()
        .map_err(|e| format!("moneyflow cache is locked: {e}"))?;
    let checks: Vec<Result<(NaiveDate, Option<usize>), Error>> = dates
        .par_iter()
        .map(|&date| {
            let path = cache_path(cache, date);
            if path.exists() {
                Ok((date, Some(read_raw(&path, date)?.items.len())))
            } else {
                Ok((date, None))
            }
        })
        .collect();
    let mut pending = Vec::new();
    let mut rows = 0;
    let mut completed = 0;
    for check in checks {
        let (date, count) = check?;
        if let Some(n) = count {
            rows += n;
            completed += 1;
        } else {
            pending.push(date);
        }
    }
    info!(
        completed,
        total = dates.len(),
        rows,
        "Validated raw moneyflow cache"
    );
    if pending.is_empty() {
        return Ok(());
    }
    let token = Arc::new(
        std::env::var("TUSHARE_TOKEN")
            .map_err(|_| "TUSHARE_TOKEN missing; load core env before research")?,
    );
    let rate = std::env::var("TUSHARE_RATE_LIMIT")
        .ok()
        .map(|v| v.parse::<u32>())
        .transpose()?
        .unwrap_or(120)
        .clamp(1, 120);
    let ip = tushare_ip().await?;
    let client = quant_download::http::ApiClient::with_dns_override(
        rate,
        4,
        ip.map(|ip| ("api.tushare.pro", ip)),
    );
    let mut tasks = tokio::task::JoinSet::new();
    let mut pending = pending.into_iter();
    loop {
        while tasks.len() < 4 {
            let Some(date) = pending.next() else { break };
            let client = client.clone();
            let token = token.clone();
            let path = cache_path(cache, date);
            tasks.spawn(async move {
                let day=date.format("%Y%m%d").to_string();
                for attempt in 0..5 {
                    let payload=client.post_json("https://api.tushare.pro/",&json!({"api_name":"moneyflow","token":token.as_str(),"params":{"trade_date":day},"fields":""})).await.map_err(|e|format!("moneyflow {day}: {e}"))?;
                    if payload.get("code").and_then(Value::as_i64)!=Some(0) {
                        let code=payload.get("code").and_then(Value::as_i64);
                        let msg=payload.get("msg").and_then(Value::as_str).unwrap_or("");
                        if attempt<4 && (code==Some(429) || msg.contains("每分钟") || msg.contains("频次")) { warn!(date=%day,attempt=attempt+1,"Provider throttle; shared 65s cooldown");client.cooldown(Duration::from_secs(65)).await;continue; }
                        return Err::<usize,Error>(format!("moneyflow {day}: API rejected request, code={code:?}").into());
                    }
                    let data=payload.get("data").ok_or("missing moneyflow data")?;
                    let raw=RawDay {date:day.clone(),fetched_at:Utc::now().to_rfc3339(),fields:serde_json::from_value(data["fields"].clone())?,items:serde_json::from_value(data["items"].clone())?};
                    raw.validate(&day)?;let count=raw.items.len();
                    tokio::task::spawn_blocking(move || ->Result<(),Error>{
                        let temp=path.with_extension(format!("tmp.{}",std::process::id()));
                        let file=File::create(&temp)?;let mut gzip=GzEncoder::new(BufWriter::new(file),Compression::fast());serde_json::to_writer(&mut gzip,&raw)?;
                        let mut writer=gzip.finish()?;writer.flush()?;writer.get_ref().sync_all()?;std::fs::rename(temp,&path)?;Ok(())
                    }).await??;
                    return Ok(count);
                }
                Err("moneyflow exhausted throttle retries".into())
            });
        }
        let Some(result) = tasks.join_next().await else {
            break;
        };
        rows += result??;
        completed += 1;
        if completed % 25 == 0 || completed == dates.len() {
            info!(
                completed,
                total = dates.len(),
                rows,
                elapsed_s = timer.elapsed().as_secs(),
                "Moneyflow fetch progress"
            );
        }
    }
    Ok(())
}

#[derive(FromRow)]
struct Price {
    ts_code: String,
    trade_date: NaiveDate,
    open: Option<f64>,
    high: Option<f64>,
    low: Option<f64>,
    close: Option<f64>,
    vol: Option<f64>,
    amount: Option<f64>,
    adj_factor: Option<f64>,
    turnover_rate: Option<f64>,
    circ_mv: Option<f64>,
}
#[derive(FromRow)]
struct Basic {
    ts_code: String,
    name: Option<String>,
    list_date: Option<NaiveDate>,
    delist_date: Option<NaiveDate>,
}
#[derive(FromRow)]
struct Industry {
    ts_code: String,
    index_code: Option<String>,
    in_date: Option<String>,
    out_date: Option<String>,
}
fn parse_date(value: Option<&str>) -> Result<Option<NaiveDate>, Error> {
    value
        .filter(|v| !v.is_empty())
        .map(|s| {
            NaiveDate::parse_from_str(s, "%Y%m%d")
                .or_else(|_| NaiveDate::parse_from_str(s, "%Y-%m-%d"))
                .map_err(Into::into)
        })
        .transpose()
}
fn finite(v: Option<f64>) -> f64 {
    v.filter(|v| v.is_finite()).unwrap_or(f64::NAN)
}

pub async fn load(
    pool: &MySqlPool,
    cache: &Path,
    start: NaiveDate,
    end: NaiveDate,
) -> Result<Dataset, Error> {
    let timer = Instant::now();
    let (dates, basics, industries) = tokio::try_join!(
        calendar(pool, start, end),
        async {
            Ok::<_, Error>(
                sqlx::query_as::<_, Basic>(
                    "SELECT ts_code,name,list_date,delist_date FROM a_stock_basic",
                )
                .fetch_all(pool)
                .await?,
            )
        },
        async {
            Ok::<_,Error>(sqlx::query_as::<_,Industry>("SELECT ts_code,index_code,in_date,out_date FROM a_industry_class WHERE src='SW2021' AND level='L1' ORDER BY in_date,index_code").fetch_all(pool).await?)
        }
    )?;
    let date_index: HashMap<_, _> = dates.iter().enumerate().map(|(i, &d)| (d, i)).collect();
    let basics: HashMap<_, _> = basics.into_iter().map(|b| (b.ts_code.clone(), b)).collect();
    let mut stocks: BTreeMap<String, Stock> = BTreeMap::new();
    let mut tasks = tokio::task::JoinSet::new();
    let mut years = start.year()..=end.year();
    let mut price_rows = 0usize;
    loop {
        while tasks.len() < 4 {
            let Some(year) = years.next() else { break };
            let pool = pool.clone();
            tasks.spawn(async move {let lo=start.max(NaiveDate::from_ymd_opt(year,1,1).unwrap());let hi=end.min(NaiveDate::from_ymd_opt(year,12,31).unwrap());
                let rows=sqlx::query_as::<_,Price>("SELECT ts_code,trade_date,open,high,low,close,vol,amount,adj_factor,turnover_rate,circ_mv FROM a_daily_price WHERE trade_date BETWEEN ? AND ? AND (ts_code LIKE '%.SH' OR ts_code LIKE '%.SZ')").bind(lo).bind(hi).fetch_all(&pool).await?;Ok::<_,sqlx::Error>((year,rows))});
        }
        let Some(result) = tasks.join_next().await else {
            break;
        };
        let (year, rows) = result??;
        price_rows += rows.len();
        let mut keys = HashSet::with_capacity(rows.len());
        for row in rows {
            let Some(&di) = date_index.get(&row.trade_date) else {
                return Err(format!("price on non-trading date {}", row.trade_date).into());
            };
            if !keys.insert((row.ts_code.clone(), di)) {
                return Err("duplicate daily price key".into());
            }
            let stock = stocks.entry(row.ts_code.clone()).or_insert_with(|| Stock {
                name: basics
                    .get(&row.ts_code)
                    .and_then(|b| b.name.clone())
                    .unwrap_or_default(),
                code: row.ts_code,
                bars: vec![Bar::default(); dates.len()],
            });
            stock.bars[di] = Bar {
                open: finite(row.open),
                high: finite(row.high),
                low: finite(row.low),
                close: finite(row.close),
                vol: finite(row.vol),
                amount: finite(row.amount),
                adj_factor: finite(row.adj_factor),
                turnover_rate: finite(row.turnover_rate),
                circ_mv: finite(row.circ_mv),
                ..Bar::default()
            };
        }
        info!(
            year,
            price_rows,
            stocks = stocks.len(),
            elapsed_s = timer.elapsed().as_secs(),
            "Loaded SQL prices"
        );
    }
    if stocks.is_empty() {
        return Err("no SH/SZ price history".into());
    }
    for industry in industries {
        let Some(stock) = stocks.get_mut(&industry.ts_code) else {
            continue;
        };
        let Some(code) = industry.index_code.filter(|v| !v.is_empty()) else {
            continue;
        };
        // Unknown membership start cannot safely establish historical membership.
        let Some(lo) = parse_date(industry.in_date.as_deref())? else {
            continue;
        };
        let hi = parse_date(industry.out_date.as_deref())?;
        // Match the existing factor cache: both membership endpoints inclusive;
        // rows sorted by in_date let the latest membership win an overlap.
        for (date, bar) in dates.iter().zip(&mut stock.bars) {
            if *date >= lo && hi.is_none_or(|hi| *date <= hi) {
                bar.industry = Some(code.clone());
            }
        }
    }
    let mut flow_rows = 0usize;
    for chunk in dates.chunks(32) {
        let parts: Vec<Result<_, Error>> = chunk
            .par_iter()
            .map(|&date| {
                let raw = read_raw(&cache_path(cache, date), date)?;
                let ci = raw.fields.iter().position(|s| s == "ts_code").unwrap();
                let ni = raw
                    .fields
                    .iter()
                    .position(|s| s == "net_mf_amount")
                    .unwrap();
                let rows = raw
                    .items
                    .into_iter()
                    .map(|row| {
                        (
                            row[ci].as_str().unwrap().to_owned(),
                            row[ni].as_f64().unwrap_or(f64::NAN),
                        )
                    })
                    .collect::<Vec<_>>();
                Ok((date, rows))
            })
            .collect();
        for part in parts {
            let (date, rows) = part?;
            flow_rows += rows.len();
            let di = date_index[&date];
            for (code, net) in rows {
                if let Some(stock) = stocks.get_mut(&code) {
                    stock.bars[di].net = net;
                }
            }
        }
    }
    let mut stocks: Vec<_> = stocks.into_values().collect();
    stocks.par_iter_mut().for_each(|stock| {
        let basic = basics.get(&stock.code);
        let mut prior = 0usize;
        for (date, bar) in dates.iter().zip(&mut stock.bars) {
            let positive = bar.close > 0.0 && bar.vol > 0.0 && bar.amount > 0.0;
            let listed = basic
                .and_then(|b| b.list_date)
                .is_none_or(|d| date.signed_duration_since(d).num_days() >= 60);
            let not_delisted = basic.and_then(|b| b.delist_date).is_none_or(|d| *date < d);
            bar.eligible = date.year() >= 2024 && prior >= 60 && positive && listed && not_delisted;
            if bar.close.is_finite() {
                prior += 1;
            }
        }
    });
    info!(
        dates = dates.len(),
        stocks = stocks.len(),
        price_rows,
        flow_rows,
        elapsed_s = timer.elapsed().as_secs(),
        "Research dataset ready; raw units retained"
    );
    Ok(Dataset { dates, stocks })
}

#[cfg(test)]
mod tests {
    use super::*;
    fn sample() -> RawDay {
        let mut row = vec![json!(0); FIELDS.len()];
        row[0] = json!("000001.SZ");
        row[1] = json!("20240104");
        RawDay {
            date: "20240104".into(),
            fetched_at: "2026-10-02T00:00:00Z".into(),
            fields: FIELDS.iter().map(|s| s.to_string()).collect(),
            items: vec![row],
        }
    }
    #[test]
    fn raw_cache_requires_complete_unique_same_day_rows() {
        let mut raw = sample();
        assert!(raw.validate("20240104").is_ok());
        raw.items.push(raw.items[0].clone());
        assert!(raw.validate("20240104").is_err());
        raw.items.pop();
        raw.items[0][1] = json!("20240105");
        assert!(raw.validate("20240104").is_err());
        raw.items[0][1] = json!("20240104");
        raw.fields.pop();
        raw.items[0].pop();
        assert!(raw.validate("20240104").is_err());
    }
    #[test]
    fn missing_flow_is_nan_and_invalid_numbers_fail() {
        let mut raw = sample();
        raw.items[0][19] = Value::Null;
        assert!(raw.validate("20240104").is_ok());
        assert!(Bar::default().net.is_nan());
        raw.items[0][19] = json!("bad");
        assert!(raw.validate("20240104").is_err());
    }
    #[test]
    fn empty_and_truncated_cache_rejected() {
        let mut raw = sample();
        raw.items.clear();
        assert!(raw.validate("20240104").is_err());
        raw = sample();
        raw.items = vec![raw.items[0].clone(); 6000];
        assert!(raw.validate("20240104").is_err());
    }
}
