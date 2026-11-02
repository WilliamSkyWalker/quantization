//! Immutable, resumable read-only SQL projection. No moneyflow/financial dependency.
use crate::Error;
use chrono::{Datelike, NaiveDate};
use flate2::{Compression, read::GzDecoder, write::GzEncoder};
use quant_core::config::Config;
use serde::{Deserialize, Serialize, de::DeserializeOwned};
use sqlx::FromRow;
use std::{
    collections::{BTreeMap, HashMap, HashSet},
    fs::File,
    io::{BufReader, BufWriter},
    path::Path,
};

#[derive(Serialize, Deserialize, FromRow)]
pub struct Basic {
    pub ts_code: String,
    pub list_date: Option<NaiveDate>,
    pub delist_date: Option<NaiveDate>,
}
#[derive(Serialize, Deserialize, FromRow)]
pub struct Industry {
    ts_code: String,
    index_code: Option<String>,
    in_date: Option<String>,
    out_date: Option<String>,
}
#[derive(Serialize, Deserialize)]
struct Metadata {
    dates: Vec<NaiveDate>,
    basics: Vec<Basic>,
    industries: Vec<Industry>,
}
#[derive(Serialize, Deserialize, FromRow)]
struct Raw {
    ts_code: String,
    trade_date: NaiveDate,
    open: Option<f64>,
    close: Option<f64>,
    pre_close: Option<f64>,
    amount: Option<f64>,
    adj_factor: Option<f64>,
    circ_mv: Option<f64>,
}
#[derive(Clone, Copy)]
pub struct Bar {
    pub open: f64,
    pub close: f64,
    pub pre_close: f64,
    pub amount: f64,
    pub adj: f64,
    pub cap: f64,
}
impl Default for Bar {
    fn default() -> Self {
        Self {
            open: f64::NAN,
            close: f64::NAN,
            pre_close: f64::NAN,
            amount: f64::NAN,
            adj: f64::NAN,
            cap: f64::NAN,
        }
    }
}
pub struct Stock {
    pub code: String,
    pub listed: Option<NaiveDate>,
    pub delisted: Option<NaiveDate>,
    pub bars: Vec<Bar>,
    pub industries: Vec<(NaiveDate, Option<NaiveDate>, String)>,
}
impl Stock {
    pub fn industry(&self, day: NaiveDate) -> Option<&str> {
        self.industries
            .iter()
            .rev()
            .find(|(lo, hi, _)| day >= *lo && hi.is_none_or(|d| day <= d))
            .map(|v| v.2.as_str())
    }
}
pub struct Data {
    pub dates: Vec<NaiveDate>,
    pub stocks: Vec<Stock>,
    pub rows: usize,
    pub missing_industry_start: usize,
}
fn read<T: DeserializeOwned>(path: &Path) -> Result<T, Error> {
    Ok(serde_json::from_reader(BufReader::new(GzDecoder::new(
        File::open(path)?,
    )))?)
}
fn write<T: Serialize>(path: &Path, value: &T) -> Result<(), Error> {
    let temp = path.with_extension("tmp");
    let mut encoder = GzEncoder::new(BufWriter::new(File::create(&temp)?), Compression::fast());
    serde_json::to_writer(&mut encoder, value)?;
    use std::io::Write;
    encoder.finish()?.flush()?;
    std::fs::rename(temp, path)?;
    Ok(())
}
fn parse(s: Option<&str>) -> Result<Option<NaiveDate>, Error> {
    s.filter(|s| !s.is_empty())
        .map(|s| {
            NaiveDate::parse_from_str(s, "%Y%m%d")
                .or_else(|_| NaiveDate::parse_from_str(s, "%Y-%m-%d"))
                .map_err(Into::into)
        })
        .transpose()
}
const SQL: &str = "SELECT ts_code,trade_date,open,close,pre_close,amount,adj_factor,circ_mv FROM a_daily_price WHERE trade_date BETWEEN ? AND ? AND (ts_code LIKE '60%.SH' OR ts_code LIKE '00%.SZ')";
pub async fn load(config: &Config, cache: &Path) -> Result<Data, Error> {
    std::fs::create_dir_all(cache)?;
    let meta_path = cache.join("metadata.json.gz");
    let complete = meta_path.exists()
        && (2021..=2026).all(|y| cache.join(format!("prices_{y}.json.gz")).exists());
    if !complete {
        let pool =
            quant_db::pool::create_pool(&config.database.url(), &config.database.schema, 4).await?;
        // Verify the exact projection on one trading day before expanding reads.
        let sample = sqlx::query_as::<_, Raw>(SQL)
            .bind("2025-01-02")
            .bind("2025-01-02")
            .fetch_all(&pool)
            .await?;
        if sample.is_empty()
            || sample
                .iter()
                .any(|r| r.trade_date.to_string() != "2025-01-02")
        {
            return Err("invalid one-day SQL projection".into());
        }
        tracing::info!(
            rows = sample.len(),
            "Verified one-day read-only price projection"
        );
        if !meta_path.exists() {
            let (dates,basics,industries)=tokio::try_join!(
                sqlx::query_as::<_,(NaiveDate,)>("SELECT cal_date FROM a_trade_cal WHERE exchange='SSE' AND is_open=1 AND cal_date BETWEEN '2021-01-01' AND '2026-09-30' ORDER BY cal_date").fetch_all(&pool),
                sqlx::query_as::<_,Basic>("SELECT ts_code,list_date,delist_date FROM a_stock_basic").fetch_all(&pool),
                sqlx::query_as::<_,Industry>("SELECT ts_code,index_code,in_date,out_date FROM a_industry_class WHERE src='SW2021' AND level='L1' ORDER BY in_date,index_code").fetch_all(&pool)
            )?;
            write(
                &meta_path,
                &Metadata {
                    dates: dates.into_iter().map(|r| r.0).collect(),
                    basics,
                    industries,
                },
            )?;
        }
        let mut years =
            (2021..=2026).filter(|y| !cache.join(format!("prices_{y}.json.gz")).exists());
        let mut tasks = tokio::task::JoinSet::new();
        loop {
            while tasks.len() < 4 {
                let Some(y) = years.next() else { break };
                let pool = pool.clone();
                let path = cache.join(format!("prices_{y}.json.gz"));
                tasks.spawn(async move {
                    let hi = if y == 2026 {
                        "2026-09-30".to_owned()
                    } else {
                        format!("{y}-12-31")
                    };
                    let rows = sqlx::query_as::<_, Raw>(SQL)
                        .bind(format!("{y}-01-01"))
                        .bind(hi)
                        .fetch_all(&pool)
                        .await?;
                    if rows.is_empty() {
                        return Err::<(), Error>(format!("empty SQL year {y}").into());
                    }
                    tracing::info!(year = y, rows = rows.len(), "Read historical prices");
                    tokio::task::spawn_blocking(move || write(&path, &rows)).await??;
                    Ok::<_, Error>(())
                });
            }
            match tasks.join_next().await {
                Some(r) => r??,
                None => break,
            }
        }
        pool.close().await;
    }
    let meta: Metadata = read(&meta_path)?;
    if meta.dates.len() < 1000 || meta.dates.windows(2).any(|w| w[0] >= w[1]) {
        return Err("invalid price calendar".into());
    }
    let index: HashMap<_, _> = meta
        .dates
        .iter()
        .enumerate()
        .map(|(i, d)| (*d, i))
        .collect();
    let basics: HashMap<_, _> = meta
        .basics
        .into_iter()
        .map(|b| (b.ts_code.clone(), b))
        .collect();
    let mut stocks = BTreeMap::<String, Stock>::new();
    let mut rows_count = 0;
    // Sequential assembly bounds peak memory; SQL fetch/compression above is bounded concurrent.
    for year in 2021..=2026 {
        let rows: Vec<Raw> = read(&cache.join(format!("prices_{year}.json.gz")))?;
        let mut keys = HashSet::new();
        for r in rows {
            if r.trade_date.year() != year {
                return Err("cached price year mismatch".into());
            }
            let &i = index.get(&r.trade_date).ok_or("price outside calendar")?;
            if !keys.insert((r.ts_code.clone(), i)) {
                return Err("duplicate price key".into());
            }
            let s = stocks.entry(r.ts_code.clone()).or_insert_with(|| Stock {
                listed: basics.get(&r.ts_code).and_then(|b| b.list_date),
                delisted: basics.get(&r.ts_code).and_then(|b| b.delist_date),
                code: r.ts_code,
                bars: vec![Bar::default(); meta.dates.len()],
                industries: vec![],
            });
            let finite = |v: Option<f64>| v.filter(|v| v.is_finite()).unwrap_or(f64::NAN);
            s.bars[i] = Bar {
                open: finite(r.open),
                close: finite(r.close),
                pre_close: finite(r.pre_close),
                amount: finite(r.amount),
                adj: finite(r.adj_factor),
                cap: finite(r.circ_mv),
            };
            rows_count += 1;
        }
    }
    let mut missing_industry_start = 0;
    for row in meta.industries {
        let Some(s) = stocks.get_mut(&row.ts_code) else {
            continue;
        };
        let Some(code) = row.index_code.filter(|s| !s.is_empty()) else {
            continue;
        };
        let Some(lo) = parse(row.in_date.as_deref())? else {
            missing_industry_start += 1;
            continue;
        };
        s.industries
            .push((lo, parse(row.out_date.as_deref())?, code));
    }
    for s in stocks.values_mut() {
        s.industries.sort();
    }
    tracing::info!(
        stocks = stocks.len(),
        rows = rows_count,
        "Historical mainboard cache loaded"
    );
    Ok(Data {
        dates: meta.dates,
        stocks: stocks.into_values().collect(),
        rows: rows_count,
        missing_industry_start,
    })
}
