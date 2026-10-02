//! On-demand Tushare minute snapshots; no SQL writes or trading decisions.
use crate::Error;
use chrono::{DateTime, Duration as ChronoDuration, FixedOffset, NaiveDateTime, Utc};
use flate2::{Compression, read::GzDecoder, write::GzEncoder};
use quant_download::http::ApiClient;
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use std::{
    collections::HashSet,
    fs::File,
    io::{BufReader, BufWriter, Write},
    path::{Path, PathBuf},
    sync::Arc,
    time::Duration,
};
use tokio::sync::OnceCell;

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Mode {
    History,
    Realtime,
    Today,
}

#[derive(Clone, Debug)]
pub struct MinuteRequest {
    pub mode: Mode,
    pub codes: Vec<String>,
    pub frequency: u32,
    /// Exchange-local timestamps (Asia/Shanghai).
    pub start: Option<NaiveDateTime>,
    pub end: Option<NaiveDateTime>,
    pub cache_dir: Option<PathBuf>,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct MinuteChunk {
    pub api_name: String,
    pub params: Value,
    /// Original HTTP observation time, retained when reusing historical cache.
    pub observed_at: DateTime<Utc>,
    pub timezone: String,
    pub volume_unit: String,
    pub amount_unit: String,
    /// Complete provider envelope, including extra fields and request metadata.
    pub payload: Value,
}

#[derive(Debug, Serialize)]
pub struct MinuteResponse {
    pub chunks: Vec<MinuteChunk>,
    pub row_count: usize,
    pub cache_hits: usize,
}

#[derive(Clone)]
pub struct MinuteClient {
    token: Arc<String>,
    rate: u32,
    http: Arc<OnceCell<ApiClient>>,
}

#[derive(Clone, Debug)]
struct Query {
    api: &'static str,
    params: Value,
    cache: Option<PathBuf>,
}

impl MinuteClient {
    pub async fn from_env() -> Result<Self, Error> {
        let token = std::env::var("TUSHARE_TOKEN").map_err(|_| "TUSHARE_TOKEN missing")?;
        if token.trim().is_empty() {
            return Err("TUSHARE_TOKEN is empty".into());
        }
        let rate = std::env::var("TUSHARE_RATE_LIMIT")
            .ok()
            .map(|v| v.parse::<u32>())
            .transpose()?
            .unwrap_or(120)
            .clamp(1, 120);
        Ok(Self {
            token: Arc::new(token),
            rate,
            http: Arc::new(OnceCell::new()),
        })
    }

    async fn http(&self) -> Result<&ApiClient, Error> {
        self.http
            .get_or_try_init(|| async {
                let ip = crate::data::tushare_ip().await?;
                Ok(ApiClient::with_dns_override(
                    self.rate,
                    4,
                    ip.map(|ip| ("api.tushare.pro", ip)),
                ))
            })
            .await
    }

    pub async fn query(&self, request: &MinuteRequest) -> Result<MinuteResponse, Error> {
        let queries = plan(request, Utc::now())?;
        let mut pending = queries.into_iter().enumerate();
        let mut tasks = tokio::task::JoinSet::new();
        let mut chunks = Vec::new();
        let mut cache_hits = 0;
        loop {
            while tasks.len() < 4 {
                let Some((index, query)) = pending.next() else {
                    break;
                };
                let client = self.clone();
                tasks.spawn(async move {
                    let (chunk, hit) = client.one(query).await?;
                    Ok::<_, Error>((index, chunk, hit))
                });
            }
            let Some(result) = tasks.join_next().await else {
                break;
            };
            let (index, chunk, hit) = result??;
            cache_hits += usize::from(hit);
            chunks.push((index, chunk));
        }
        chunks.sort_by_key(|(index, _)| *index);
        let chunks: Vec<_> = chunks.into_iter().map(|(_, c)| c).collect();
        let row_count = chunks
            .iter()
            .map(|c| c.payload["data"]["items"].as_array().map_or(0, Vec::len))
            .sum();
        Ok(MinuteResponse {
            chunks,
            row_count,
            cache_hits,
        })
    }

    async fn one(&self, query: Query) -> Result<(MinuteChunk, bool), Error> {
        if let Some(path) = query.cache.clone() {
            let expected = query.clone();
            if let Some(chunk) =
                tokio::task::spawn_blocking(move || read_cache(&path, &expected)).await??
            {
                return Ok((chunk, true));
            }
        }
        let client = self.http().await?;
        for attempt in 0..5 {
            let payload = client.post_json("https://api.tushare.pro/", &json!({"api_name":query.api,"token":self.token.as_str(),"params":query.params,"fields":""})).await
                .map_err(|e|format!("{}: {}",query.api,e.replace(self.token.as_str(),"[REDACTED]")))?;
            if payload["code"].as_i64() != Some(0) {
                let code = payload["code"].as_i64();
                let message = payload["msg"].as_str().unwrap_or("");
                if provider_throttled(code, message) && attempt < 4 {
                    tracing::warn!(
                        api = query.api,
                        attempt = attempt + 1,
                        provider_message = %message.replace(self.token.as_str(), "[REDACTED]"),
                        "Minute API throttle; shared 65-second cooldown"
                    );
                    client.cooldown(Duration::from_secs(65)).await;
                    continue;
                }
                return Err(format!(
                    "{}: API rejected request, code={code:?}: {}",
                    query.api,
                    message.replace(self.token.as_str(), "[REDACTED]")
                )
                .into());
            }
            let count = validate_payload(&query, &payload)?;
            let chunk = MinuteChunk {
                api_name: query.api.into(),
                params: query.params.clone(),
                observed_at: Utc::now(),
                timezone: "Asia/Shanghai".into(),
                volume_unit: "shares".into(),
                amount_unit: "CNY".into(),
                payload,
            };
            if count == 0 {
                tracing::warn!(api=query.api,params=%query.params,"Empty minute response: no completeness cache written");
            } else if let Some(path) = query.cache {
                let saved = chunk.clone();
                tokio::task::spawn_blocking(move || write_cache(&path, &saved)).await??;
            }
            return Ok((chunk, false));
        }
        Err("Minute API exhausted throttle retries".into())
    }
}

fn plan(request: &MinuteRequest, now: DateTime<Utc>) -> Result<Vec<Query>, Error> {
    if ![1, 5, 15, 30, 60].contains(&request.frequency) {
        return Err("minute frequency must be 1, 5, 15, 30 or 60".into());
    }
    if request.codes.is_empty() || request.codes.len() > 300 {
        return Err("provide 1..=300 stock codes per on-demand query".into());
    }
    let mut unique = HashSet::new();
    for code in &request.codes {
        let Some((digits, exchange)) = code.split_once('.') else {
            return Err("stock code must look like 600000.SH".into());
        };
        if digits.len() != 6
            || !digits.bytes().all(|c| c.is_ascii_digit())
            || !["SH", "SZ", "BJ"].contains(&exchange)
            || !unique.insert(code)
        {
            return Err("invalid or duplicate stock code".into());
        }
    }
    let mut queries = Vec::new();
    match request.mode {
        Mode::History => {
            let start = request.start.ok_or("history requires start and end")?;
            let end = request.end.ok_or("history requires start and end")?;
            if end < start {
                return Err("end precedes start".into());
            }
            if start.nanosecond() != 0 || end.nanosecond() != 0 {
                return Err("minute request timestamps require whole seconds".into());
            }
            let chunks_per_code =
                end.date().signed_duration_since(start.date()).num_days() / 20 + 1;
            if chunks_per_code * request.codes.len() as i64 > 256 {
                return Err(
                    "on-demand query exceeds 256 chunks; narrow dates or candidate list".into(),
                );
            }
            let today = now
                .with_timezone(&FixedOffset::east_opt(8 * 3600).unwrap())
                .date_naive();
            for code in &request.codes {
                let mut low = start;
                loop {
                    let boundary = low
                        .date()
                        .checked_add_signed(ChronoDuration::days(20))
                        .ok_or("date range overflow")?
                        .and_hms_opt(0, 0, 0)
                        .unwrap();
                    let high = end.min(boundary - ChronoDuration::seconds(1));
                    let params = json!({"ts_code":code,"freq":format!("{}min",request.frequency),"start_date":low.format("%Y-%m-%d %H:%M:%S").to_string(),"end_date":high.format("%Y-%m-%d %H:%M:%S").to_string()});
                    let cache = request
                        .cache_dir
                        .as_ref()
                        .filter(|_| high.date() < today)
                        .map(|dir| {
                            dir.join(format!(
                                "stk_mins_v1_{}_{}min_{}_{}.json.gz",
                                code,
                                request.frequency,
                                low.format("%Y%m%dT%H%M%S"),
                                high.format("%Y%m%dT%H%M%S")
                            ))
                        });
                    queries.push(Query {
                        api: "stk_mins",
                        params,
                        cache,
                    });
                    if high == end {
                        break;
                    }
                    low = boundary;
                }
            }
        }
        Mode::Realtime | Mode::Today => {
            if request.start.is_some() || request.end.is_some() || request.cache_dir.is_some() {
                return Err("live modes accept neither date bounds nor persistent cache; snapshots stay in memory".into());
            }
            let freq = format!("{}MIN", request.frequency);
            if request.mode == Mode::Realtime {
                queries.push(Query {
                    api: "rt_min",
                    params: json!({"ts_code":request.codes.join(","),"freq":freq}),
                    cache: None,
                });
            } else {
                for code in &request.codes {
                    queries.push(Query {
                        api: "rt_min_daily",
                        params: json!({"ts_code":code,"freq":freq}),
                        cache: None,
                    });
                }
            }
        }
    }
    Ok(queries)
}

// Tushare reuses 40203 for both access denial and frequency restrictions.
// Unknown business errors are terminal; only recognized throttles are retried.
fn provider_throttled(code: Option<i64>, message: &str) -> bool {
    let lower = message.to_lowercase();
    let permission = message.contains("权限") || lower.contains("permission");
    // A minute cooldown cannot recover hour/day/month or trial quotas. Fail
    // explicitly instead of spending five retries on an unavailable allowance.
    let long_quota = ["小时", "每天", "每日", "/天", "每月", "/月", "试用"]
        .iter()
        .any(|word| message.contains(word))
        || ["hour", "daily", "per day", "monthly", "trial"]
            .iter()
            .any(|word| lower.contains(word));
    !permission
        && !long_quota
        && (code == Some(429)
            || message.contains("每分钟")
            || message.contains("频次")
            || lower.contains("rate limit"))
}

use chrono::Timelike;
fn parse_time(value: &str) -> Result<NaiveDateTime, Error> {
    Ok(NaiveDateTime::parse_from_str(value, "%Y-%m-%d %H:%M:%S")?)
}

fn validate_payload(query: &Query, payload: &Value) -> Result<usize, Error> {
    if payload["code"].as_i64() != Some(0) {
        return Err("cached minute response is not successful".into());
    }
    let data = &payload["data"];
    let fields: Vec<String> = serde_json::from_value(data["fields"].clone())?;
    let rows = data["items"]
        .as_array()
        .ok_or("minute response lacks items")?;
    let limit = if query.api == "stk_mins" { 8000 } else { 1000 };
    if rows.len() >= limit || data["has_more"].as_bool() == Some(true) {
        return Err(format!(
            "{}: potentially truncated response; narrow request",
            query.api
        )
        .into());
    }
    let unique: HashSet<_> = fields.iter().map(String::as_str).collect();
    if unique.len() != fields.len() {
        return Err("duplicate minute response fields".into());
    }
    let time_field = if query.api == "stk_mins" {
        "trade_time"
    } else if unique.contains("time") {
        "time"
    } else {
        "trade_time"
    };
    // Today's endpoint documentation disagrees between its table and example.
    // Preserve whichever stock-code field the provider returns, without renaming raw data.
    let code_field =
        if query.api == "rt_min_daily" && !unique.contains("ts_code") && unique.contains("code") {
            "code"
        } else {
            "ts_code"
        };
    for field in [
        code_field, time_field, "open", "close", "high", "low", "vol", "amount",
    ] {
        if !unique.contains(field) {
            return Err(format!("{}: required field {field} missing", query.api).into());
        }
    }
    let index = |field: &str| fields.iter().position(|f| f == field).unwrap();
    let codes: HashSet<_> = query.params["ts_code"]
        .as_str()
        .ok_or("query stock code missing")?
        .split(',')
        .collect();
    let bounds = if query.api == "stk_mins" {
        Some((
            parse_time(query.params["start_date"].as_str().ok_or("start missing")?)?,
            parse_time(query.params["end_date"].as_str().ok_or("end missing")?)?,
        ))
    } else {
        None
    };
    let mut keys = HashSet::new();
    for row in rows {
        let row = row.as_array().ok_or("minute row is not an array")?;
        if row.len() != fields.len() {
            return Err("minute field/row width mismatch".into());
        }
        let code = row[index(code_field)]
            .as_str()
            .ok_or("invalid minute stock code")?;
        let timestamp = parse_time(
            row[index(time_field)]
                .as_str()
                .ok_or("invalid minute timestamp")?,
        )?;
        if !codes.contains(code)
            || !keys.insert((code, timestamp))
            || bounds.is_some_and(|(low, high)| timestamp < low || timestamp > high)
        {
            return Err(
                "unexpected code, duplicate minute key or timestamp outside requested range".into(),
            );
        }
        if let Some(i) = fields.iter().position(|f| f == "freq") {
            if row[i] != query.params["freq"] {
                return Err("minute frequency mismatch".into());
            }
        }
        for field in ["open", "close", "high", "low", "vol", "amount"] {
            let value = &row[index(field)];
            if !value.is_null() && !value.as_f64().is_some_and(|v| v.is_finite() && v >= 0.0) {
                return Err(format!("invalid minute numeric field {field}").into());
            }
        }
    }
    Ok(rows.len())
}

fn read_cache(path: &Path, query: &Query) -> Result<Option<MinuteChunk>, Error> {
    let file = match File::open(path) {
        Ok(file) => file,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(None),
        Err(e) => return Err(e.into()),
    };
    let chunk: MinuteChunk = serde_json::from_reader(BufReader::new(GzDecoder::new(file)))?;
    if chunk.api_name != query.api
        || chunk.params != query.params
        || chunk.timezone != "Asia/Shanghai"
        || chunk.volume_unit != "shares"
        || chunk.amount_unit != "CNY"
    {
        return Err("minute cache metadata does not match request".into());
    }
    if validate_payload(query, &chunk.payload)? == 0 {
        return Err("empty minute cache cannot establish completeness".into());
    }
    Ok(Some(chunk))
}

fn write_cache(path: &Path, chunk: &MinuteChunk) -> Result<(), Error> {
    let parent = path.parent().ok_or("minute cache parent missing")?;
    std::fs::create_dir_all(parent)?;
    let temporary = path.with_extension(format!(
        "tmp.{}.{}",
        std::process::id(),
        Utc::now()
            .timestamp_nanos_opt()
            .ok_or("clock outside nanosecond range")?
    ));
    let result = (|| -> Result<(), Error> {
        let file = std::fs::OpenOptions::new()
            .create_new(true)
            .write(true)
            .open(&temporary)?;
        let mut gzip = GzEncoder::new(BufWriter::new(file), Compression::default());
        serde_json::to_writer(&mut gzip, chunk)?;
        let mut writer = gzip.finish()?;
        writer.flush()?;
        writer.get_ref().sync_all()?;
        std::fs::rename(&temporary, path)?;
        Ok(())
    })();
    if result.is_err() {
        let _ = std::fs::remove_file(&temporary);
    }
    result
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn permission_and_frequency_errors_can_share_a_code() {
        assert!(!provider_throttled(
            Some(40203),
            "抱歉，您访问接口(stk_mins)频率超限(1次/小时)，具体频次详情：https://tushare.pro/document/1?doc_id=108。"
        ));
        assert!(!provider_throttled(Some(40203), "每天频次已用完"));
        assert!(!provider_throttled(
            Some(40203),
            "抱歉，您没有接口(rt_min_daily)访问权限"
        ));
        assert!(provider_throttled(
            Some(40203),
            "抱歉，您每分钟最多访问该接口120次"
        ));
        assert!(provider_throttled(Some(429), "too many requests"));
        assert!(!provider_throttled(Some(40203), "unknown business failure"));
    }

    #[tokio::test]
    async fn captured_historical_response_can_be_replayed_without_network() {
        let payload: Value =
            serde_json::from_str(include_str!("../tests/fixtures/stk_mins.json")).unwrap();
        let mut request = request();
        let dir = std::env::temp_dir().join(format!(
            "quant-minute-captured-{}-{}",
            std::process::id(),
            Utc::now().timestamp_nanos_opt().unwrap()
        ));
        request.cache_dir = Some(dir.clone());
        let query = plan(&request, Utc::now()).unwrap().remove(0);
        assert_eq!(validate_payload(&query, &payload).unwrap(), 241);
        let chunk = MinuteChunk {
            api_name: query.api.into(),
            params: query.params.clone(),
            observed_at: Utc::now(),
            timezone: "Asia/Shanghai".into(),
            volume_unit: "shares".into(),
            amount_unit: "CNY".into(),
            payload,
        };
        write_cache(query.cache.as_ref().unwrap(), &chunk).unwrap();
        let client = MinuteClient {
            token: Arc::new("unused-test-token".into()),
            rate: 120,
            http: Arc::new(OnceCell::new()),
        };
        let response = client.query(&request).await.unwrap();
        assert_eq!(response.cache_hits, 1);
        assert_eq!(response.row_count, 241);
        assert_eq!(response.chunks[0].payload, chunk.payload);
        assert!(
            client.http.get().is_none(),
            "cache reuse must not initialize network/DNS"
        );
        std::fs::remove_dir_all(dir).unwrap();
    }

    #[test]
    fn captured_live_and_denied_responses_match_actual_schemas() {
        let mut request = request();
        request.mode = Mode::Realtime;
        request.start = None;
        request.end = None;
        let query = plan(&request, Utc::now()).unwrap().remove(0);
        let payload: Value =
            serde_json::from_str(include_str!("../tests/fixtures/rt_min.json")).unwrap();
        assert_eq!(validate_payload(&query, &payload).unwrap(), 1);
        let denied: Value =
            serde_json::from_str(include_str!("../tests/fixtures/rt_min_daily_denied.json"))
                .unwrap();
        assert_eq!(denied["code"], 40203);
        assert!(!provider_throttled(
            denied["code"].as_i64(),
            denied["msg"].as_str().unwrap()
        ));
    }
    fn request() -> MinuteRequest {
        MinuteRequest {
            mode: Mode::History,
            codes: vec!["600000.SH".into()],
            frequency: 1,
            start: Some(parse_time("2026-09-30 09:00:00").unwrap()),
            end: Some(parse_time("2026-09-30 16:00:00").unwrap()),
            cache_dir: None,
        }
    }
    fn payload() -> Value {
        json!({"code":0,"request_id":"retained","data":{"count":0,"has_more":false,"fields":["ts_code","trade_time","close","open","high","low","vol","amount","extra"],"items":[["600000.SH","2026-09-30 15:00:00",9.48,9.48,9.48,9.48,1491300.0,14137524.0,"retained"]]}})
    }
    #[test]
    fn modes_use_distinct_parameters() {
        let mut r = request();
        let q = plan(&r, Utc::now()).unwrap();
        assert_eq!(q[0].params["freq"], "1min");
        r.mode = Mode::Realtime;
        r.start = None;
        r.end = None;
        r.codes.push("000001.SZ".into());
        let q = plan(&r, Utc::now()).unwrap();
        assert_eq!(q.len(), 1);
        assert_eq!(q[0].params["freq"], "1MIN");
        assert_eq!(q[0].params["ts_code"], "600000.SH,000001.SZ");
        r.mode = Mode::Today;
        assert_eq!(plan(&r, Utc::now()).unwrap().len(), 2);
        r.cache_dir = Some("cache".into());
        assert!(plan(&r, Utc::now()).is_err());
    }
    #[test]
    fn historical_chunks_have_no_gap_or_overlap_and_no_today_cache() {
        let mut r = request();
        r.start = Some(parse_time("2026-09-01 09:00:00").unwrap());
        r.cache_dir = Some("cache".into());
        let now = DateTime::parse_from_rfc3339("2026-09-30T08:00:00Z")
            .unwrap()
            .with_timezone(&Utc);
        let q = plan(&r, now).unwrap();
        assert_eq!(q.len(), 2);
        assert_eq!(q[0].params["end_date"], "2026-09-20 23:59:59");
        assert_eq!(q[1].params["start_date"], "2026-09-21 00:00:00");
        assert!(q[0].cache.is_some());
        assert!(q[1].cache.is_none());
    }
    #[test]
    fn refuses_unbounded_or_duplicate_requests() {
        let mut r = request();
        r.codes.push(r.codes[0].clone());
        assert!(plan(&r, Utc::now()).is_err());
        r.codes.pop();
        r.start = Some(parse_time("2000-01-01 00:00:00").unwrap());
        assert!(plan(&r, Utc::now()).is_err());
    }
    #[test]
    fn accepts_full_raw_fields_and_zero_count_metadata() {
        let q = plan(&request(), Utc::now()).unwrap().remove(0);
        assert_eq!(validate_payload(&q, &payload()).unwrap(), 1);
        let mut p = payload();
        p["data"]["items"][0][2] = Value::Null;
        assert!(validate_payload(&q, &p).is_ok());
        p["data"]["items"][0][2] = json!("NaN");
        assert!(validate_payload(&q, &p).is_err());
    }
    #[test]
    fn rejects_duplicate_wrong_date_schema_and_truncation() {
        let q = plan(&request(), Utc::now()).unwrap().remove(0);
        let mut p = payload();
        let row = p["data"]["items"][0].clone();
        p["data"]["items"].as_array_mut().unwrap().push(row);
        assert!(validate_payload(&q, &p).is_err());
        p = payload();
        p["data"]["items"][0][1] = json!("2026-09-29 15:00:00");
        assert!(validate_payload(&q, &p).is_err());
        p = payload();
        p["data"]["fields"][0] = json!("unknown");
        assert!(validate_payload(&q, &p).is_err());
        p = payload();
        p["data"]["has_more"] = json!(true);
        assert!(validate_payload(&q, &p).is_err());
        p = payload();
        p["code"] = json!(40203);
        assert!(validate_payload(&q, &p).is_err());
    }
    #[test]
    fn live_snapshots_can_be_stale_without_claiming_closed_bars() {
        let mut r = request();
        r.mode = Mode::Realtime;
        r.start = None;
        r.end = None;
        let mut q = plan(&r, Utc::now()).unwrap().remove(0);
        let mut p = payload();
        p["data"]["fields"][1] = json!("time");
        assert_eq!(validate_payload(&q, &p).unwrap(), 1);
        q.api = "rt_min_daily";
        p["data"]["fields"][0] = json!("code");
        assert_eq!(validate_payload(&q, &p).unwrap(), 1);
    }
    #[test]
    fn cache_roundtrip_preserves_raw_envelope_and_checks_request() {
        let q = plan(&request(), Utc::now()).unwrap().remove(0);
        let chunk = MinuteChunk {
            api_name: q.api.into(),
            params: q.params.clone(),
            observed_at: Utc::now(),
            timezone: "Asia/Shanghai".into(),
            volume_unit: "shares".into(),
            amount_unit: "CNY".into(),
            payload: payload(),
        };
        let dir = std::env::temp_dir().join(format!(
            "quant-minute-test-{}-{}",
            std::process::id(),
            Utc::now().timestamp_nanos_opt().unwrap()
        ));
        let path = dir.join("test.json.gz");
        write_cache(&path, &chunk).unwrap();
        let saved = read_cache(&path, &q).unwrap().unwrap();
        assert_eq!(saved.payload, chunk.payload);
        assert_eq!(saved.observed_at, chunk.observed_at);
        let mut bad = q.clone();
        bad.params["freq"] = json!("5min");
        assert!(read_cache(&path, &bad).is_err());
        let mut empty = chunk;
        empty.payload["data"]["items"] = json!([]);
        write_cache(&path, &empty).unwrap();
        assert!(read_cache(&path, &q).is_err());
        std::fs::remove_dir_all(dir).unwrap();
    }
}
