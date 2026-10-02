//! Tushare Pro API downloader for A-share data.
//!
//! Tushare API: POST https://api.tushare.pro with JSON body.
//! Rate limit: configurable (default 200 points/min).
//! Convention: no `fields=` parameter — keep all fields returned by API.

use std::collections::HashSet;
use std::sync::Arc;
use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};

use serde_json::{json, Value};
use sqlx::MySqlPool;
use tokio::sync::{Mutex, Semaphore};
use sqlx::Acquire;
use tracing::{info, warn};

use crate::http::ApiClient;
use crate::progress::ticker_progress;

const MAX_CONCURRENT: usize = 10;
const WRITE_ATTEMPTS: usize = 3;

/// A handful of Tushare endpoints (top_list, top_inst, margin, margin_detail,
/// moneyflow_hsgt — all "sensitive"/龙虎榜-adjacent interfaces) enforce a lower
/// per-interface throughput cap independent of the account's overall points
/// budget. Observed via live 40203 "频率超限" errors at the account's normal
/// (500/min) rate: their actual ceiling is ~200/min. Stay safely under it.
const RESTRICTED_RATE_LIMIT: u32 = 150;
const RESTRICTED_MAX_CONCURRENT: usize = 2;

#[derive(Clone)]
pub struct TushareDownloader {
    pub token: String,
    pub client: ApiClient,
    /// Slower-paced client for endpoints with a per-interface throughput cap
    /// below the account's general rate limit (see `RESTRICTED_RATE_LIMIT`).
    restricted_client: ApiClient,
    pub pool: MySqlPool,
    /// When set, all per-ticker methods only process this ts_code (e.g. "000001.SZ").
    pub only_ticker: Option<String>,
    replay_from: Option<chrono::NaiveDate>,
    failed: Arc<AtomicBool>,
    write_lock: Arc<Mutex<()>>,
}

impl TushareDownloader {
    pub fn new(token: String, pool: MySqlPool, rate_limit: u32) -> Self {
        let ip = std::env::var("QUANT_TUSHARE_API_IP").ok().map(|value| {
            value.parse::<std::net::IpAddr>().expect("QUANT_TUSHARE_API_IP must be an IP address")
        });
        let dns_override = ip.map(|ip| ("api.tushare.pro", ip));
        Self {
            token,
            client: ApiClient::with_dns_override(safe_tushare_rate(rate_limit), MAX_CONCURRENT, dns_override),
            restricted_client: ApiClient::with_dns_override(safe_tushare_rate(rate_limit), RESTRICTED_MAX_CONCURRENT, dns_override),
            pool,
            only_ticker: None,
            replay_from: None,
            failed: Arc::new(AtomicBool::new(false)),
            write_lock: Arc::new(Mutex::new(())),
        }
    }

    pub fn has_failed(&self) -> bool {
        self.failed.load(Ordering::SeqCst)
    }

    fn fail(&self, message: impl std::fmt::Display) {
        self.failed.store(true, Ordering::SeqCst);
        tracing::error!("Tushare update stopped: {message}");
    }

    /// Re-fetch market-wide daily tables from this date to repair old gaps.
    pub fn with_replay_from(mut self, date: Option<chrono::NaiveDate>) -> Self {
        self.replay_from = date;
        self
    }

    pub fn with_ticker(mut self, ticker: Option<&str>) -> Self {
        self.only_ticker = ticker.map(|s| s.to_string());
        self
    }

    /// Call Tushare Pro API. Returns rows as Vec<Value> (each row = JSON object).
    ///
    /// Some Tushare endpoints (e.g. `top_list`/`top_inst`) enforce a lower
    /// per-interface rate limit (observed: 40203 "频率超限", independent of the
    /// account's overall points-based rate limit) that our concurrent
    /// per-date downloads can burst past. Retries with exponential backoff
    /// on that specific error so a transient throttle doesn't get silently
    /// treated as "no data for this date" and marked done by the caller.
    async fn tushare_call(&self, api_name: &str, params: &Value) -> Vec<Value> {
        self.tushare_call_with_client(api_name, params, false).await
    }

    /// Like `tushare_call`, routed through the slower-paced client for
    /// endpoints with a known per-interface throughput cap (see
    /// `RESTRICTED_RATE_LIMIT`).
    async fn tushare_call_restricted(&self, api_name: &str, params: &Value) -> Vec<Value> {
        self.tushare_call_with_client(api_name, params, true).await
    }

    async fn tushare_call_with_client(&self, api_name: &str, params: &Value, restricted: bool) -> Vec<Value> {
        if self.has_failed() {
            warn!("Skipping {api_name}: an earlier operation failed");
            return Vec::new();
        }
        const MAX_RETRIES: u32 = 5;
        let mut attempt = 0u32;
        loop {
            if self.has_failed() {
                warn!("Cancelling {api_name} retry: update has failed");
                return Vec::new();
            }
            match self.tushare_call_once(api_name, params, restricted).await {
                Ok(rows) => return rows,
                Err(error) if error.contains("40203") && attempt < MAX_RETRIES => {
                    attempt += 1;
                    // A short per-worker backoff keeps the shared minute window full.
                    // Stop both client queues for a complete provider window instead.
                    let cooldown = std::time::Duration::from_secs(65);
                    self.client.cooldown(cooldown).await;
                    self.restricted_client.cooldown(cooldown).await;
                    warn!("Tushare {api_name}: rate-limited; pausing shared request queues for 65s (retry {attempt}/{MAX_RETRIES})");
                }
                Err(error) => {
                    self.fail(format!("{api_name}: {error}"));
                    return Vec::new();
                }
            }
        }
    }

    /// Single (non-retrying) Tushare API call.
    async fn tushare_call_once(&self, api_name: &str, params: &Value, restricted: bool) -> Result<Vec<Value>, String> {
        let body = json!({
            "api_name": api_name,
            "token": self.token,
            "params": params,
            "fields": "",
        });

        let url = "https://api.tushare.pro";
        let client = if restricted { &self.restricted_client } else { &self.client };
        let resp = client.post_json(url, &body).await?;
        let data = tushare_response_data(&resp)?;
        decode_tushare_rows(data).map_err(|error| format!("invalid response data: {error}"))
    }

    // ── DB helpers ──────────────────────────────────────────────────────


    async fn get_done_tickers(&self, table: &str) -> HashSet<String> {
        sqlx::query_scalar::<_, String>(
            "SELECT ticker FROM import_progress WHERE table_name = ?"
        ).bind(table).fetch_all(&self.pool).await.unwrap_or_else(|e| {
            self.fail(format!("Cannot read progress for {table}: {e}"));
            Vec::new()
        }).into_iter().collect()
    }

    async fn mark_done(&self, table: &str, ticker: &str) {
        if self.has_failed() {
            warn!("Not marking {table}/{ticker} complete: update failed");
            return;
        }
        // A filtered stock probe must never mark the full market date complete.
        if self.only_ticker.is_some() && ticker.len() == 8 && ticker.bytes().all(|b| b.is_ascii_digit()) {
            info!("Not marking market date {table}/{ticker} complete for a single-stock run");
            return;
        }
        if let Err(error) = sqlx::query(
            "INSERT INTO import_progress (table_name, ticker, completed_at) \
             VALUES (?, ?, NOW()) ON DUPLICATE KEY UPDATE completed_at = NOW()"
        ).bind(table).bind(ticker).execute(&self.pool).await {
            self.fail(format!("Cannot mark {table}/{ticker} complete: {error}"));
        }
    }

    async fn get_all_ts_codes(&self) -> Vec<String> {
        if let Some(t) = &self.only_ticker {
            return vec![t.clone()];
        }
        sqlx::query_scalar::<_, String>(
            "SELECT DISTINCT ts_code FROM a_stock_basic ORDER BY ts_code"
        ).fetch_all(&self.pool).await.unwrap_or_else(|e| {
            self.fail(format!("Cannot read stock list: {e}"));
            Vec::new()
        })
    }

    async fn get_table_columns(&self, table: &str) -> HashSet<String> {
        let sql = format!(
            "SELECT column_name FROM information_schema.columns WHERE table_schema = DATABASE() AND table_name = '{table}'"
        );
        let rows: Vec<(String,)> = sqlx::query_as(&sql)
            .fetch_all(&self.pool).await.unwrap_or_else(|e| {
                self.fail(format!("Cannot read {table} metadata: {e}"));
                Vec::new()
            });
        rows.into_iter().map(|(c,)| c).collect()
    }

    async fn get_ticker_latest(&self, table: &str, date_field: &str) -> std::collections::HashMap<String, String> {
        let sql = format!(
            "SELECT ts_code, DATE_FORMAT(MAX({date_field}), '%Y%m%d') as latest FROM {table} GROUP BY ts_code"
        );
        let rows: Vec<(String, Option<String>)> = sqlx::query_as(&sql)
            .fetch_all(&self.pool).await.unwrap_or_else(|e| {
                self.fail(format!("Cannot read latest dates for {table}: {e}"));
                Vec::new()
            });
        rows.into_iter()
            .filter_map(|(t, d)| d.map(|d| (t, d.trim().to_string())))
            .collect()
    }

    async fn upsert_rows(&self, table: &str, rows: &[Value], unique_keys: &[&str]) -> usize {
        let _write_guard = self.write_lock.lock().await;
        if self.has_failed() || rows.is_empty() { return 0; }
        let first = match rows[0].as_object() {
            Some(m) => m,
            None => { self.fail(format!("Invalid row in {table}")); return 0; }
        };

        let db_columns = self.get_table_columns(table).await;
        if self.has_failed() || db_columns.is_empty() {
            self.fail(format!("No database columns available for {table}"));
            return 0;
        }
        // created_at / updated_at 由 DB trigger 自动维护（quant.set_updated_at），
        // 应用层一律不写。
        let columns: Vec<String> = first.keys()
            .filter(|k| {
                let k = k.as_str();
                k != "id" && k != "updated_at" && k != "created_at"
                    && (db_columns.is_empty() || db_columns.contains(k))
            })
            .cloned().collect();
        if columns.is_empty() {
            self.fail(format!("No writable columns for {table}"));
            return 0;
        }

        // API corrections can repeat a natural key within one batch. Keep the
        // final occurrence so the inserted value matches the latest payload.
        let deduped = deduplicate_rows(rows, unique_keys);

        let col_list = columns.iter().map(|c| format!("`{c}`")).collect::<Vec<_>>().join(", ");
        let update_set: String = columns.iter()
            .filter(|c| !unique_keys.contains(&c.as_str()))
            .map(|c| format!("`{c}` = VALUES(`{c}`)"))
            .collect::<Vec<_>>().join(", ");

        let chunk_size = 50;
        let mut statements = Vec::new();

        for chunk in deduped.chunks(chunk_size) {
            let mut values_clauses = Vec::with_capacity(chunk.len());
            for row in chunk {
                let obj = match row.as_object() { Some(m) => m, None => continue };
                let vals: Vec<String> = columns.iter().map(|col| {
                    to_sql_literal(obj.get(col).unwrap_or(&Value::Null))
                }).collect();
                values_clauses.push(format!("({})", vals.join(",")));
            }
            if values_clauses.is_empty() { continue; }

            let sql = if update_set.is_empty() {
                format!("INSERT IGNORE INTO {table} ({col_list}) VALUES {}",
                    values_clauses.join(","))
            } else {
                format!("INSERT INTO {table} ({col_list}) VALUES {} ON DUPLICATE KEY UPDATE {update_set}",
                    values_clauses.join(","))
            };

            statements.push(sql);
        }
        // One API batch is atomic: failed chunks cannot advance MAX(date).
        for attempt in 1..=WRITE_ATTEMPTS {
            match self.write_batch(&statements).await {
                Ok(total) => return total,
                Err(error) if retryable_write_error(&error) && attempt < WRITE_ATTEMPTS => {
                    warn!("{table}: retrying rolled-back batch after lock conflict ({attempt}/{WRITE_ATTEMPTS}): {error}");
                    tokio::time::sleep(std::time::Duration::from_secs(attempt as u64)).await;
                }
                Err(error) => {
                    self.fail(format!("Upsert {table} failed; batch rolled back: {error}"));
                    return 0;
                }
            }
        }
        unreachable!("write attempts always return")
    }

    async fn write_batch(&self, statements: &[String]) -> Result<usize, sqlx::Error> {
        let mut conn = self.pool.acquire().await?;
        // Apply only to this transaction; do not alter global database settings.
        sqlx::query("SET TRANSACTION ISOLATION LEVEL READ COMMITTED")
            .execute(&mut *conn).await?;
        let mut tx = conn.begin().await?;
        let mut total = 0;
        for sql in statements {
            match sqlx::query(sql).execute(&mut *tx).await {
                Ok(result) => total += result.rows_affected() as usize,
                Err(error) => {
                    tx.rollback().await?;
                    return Err(error);
                }
            }
        }
        tx.commit().await?;
        Ok(total)
    }

    async fn pending_trade_dates(&self, table: &str, start: &str, incremental: bool) -> Vec<String> {
        let today = chrono::Utc::now().with_timezone(
            &chrono::FixedOffset::east_opt(8 * 3600).unwrap()
        ).date_naive();
        let latest: Option<chrono::NaiveDate> = if incremental {
            let sql = if self.only_ticker.is_some() && table != "a_margin" && table != "a_moneyflow_hsgt" {
                format!("SELECT MAX(trade_date) FROM {table} WHERE ts_code = ?")
            } else {
                format!("SELECT MAX(trade_date) FROM {table}")
            };
            let mut query = sqlx::query_scalar(&sql);
            if sql.contains('?') { query = query.bind(self.only_ticker.as_deref().unwrap()); }
            match query.fetch_one(&self.pool).await {
                Ok(date) => date,
                Err(error) => {
                    self.fail(format!("Cannot read latest date for {table}: {error}"));
                    return Vec::new();
                }
            }
        } else { None };
        let latest = match (latest, self.replay_from) {
            (Some(latest), Some(replay)) => Some(latest.min(replay)),
            (None, Some(replay)) => Some(replay),
            (latest, None) => latest,
        };
        // Replay the last date to repair interrupted old non-transactional batches.
        let start = latest.map(|d| d.format("%Y%m%d").to_string()).unwrap_or_else(|| start.to_string());
        let calendar = self.tushare_call("trade_cal", &json!({
            "exchange": "SSE", "start_date": start,
            "end_date": today.format("%Y%m%d").to_string(), "is_open": 1,
        })).await;
        let dates = calendar.iter().filter_map(|r| r.get("cal_date").and_then(Value::as_str)).collect();
        let mut pending = match select_trade_dates(dates, latest, today) {
            Ok(dates) => dates,
            Err(error) => { self.fail(error); return Vec::new(); }
        };
        if !incremental {
            let done = self.get_done_tickers(table).await;
            pending.retain(|d| !done.contains(d));
        }
        info!("{table}: {} dates pending, first={:?}, last={:?}", pending.len(), pending.first(), pending.last());
        pending
    }

    // Commit market dates in order. A failed earlier date must not be skipped
    // on the next run because a later concurrent date advanced MAX(trade_date).
    async fn run_dates<F, Fut>(&self, dates: Vec<String>, label: &str, task: F) -> usize
    where F: Fn(TushareDownloader, String) -> Fut,
          Fut: std::future::Future<Output = usize> {
        let mut total = 0;
        for date in dates {
            if self.has_failed() {
                warn!("Stopping {label} before {date}: previous operation failed");
                break;
            }
            total += task(self.clone(), date).await;
        }
        total
    }

    /// Run per-item tasks concurrently.
    async fn run_concurrent<F, Fut>(&self, items: Vec<String>, label: &str, task_fn: F) -> usize
    where
        F: Fn(TushareDownloader, String) -> Fut + Send + Sync + 'static,
        Fut: std::future::Future<Output = usize> + Send + 'static,
    {
        if items.is_empty() { return 0; }
        info!("{label}: {} items ({MAX_CONCURRENT} concurrent)", items.len());
        let pb = Arc::new(ticker_progress(items.len() as u64, label));
        let total = Arc::new(AtomicUsize::new(0));
        let sem = Arc::new(Semaphore::new(MAX_CONCURRENT));
        let task_fn = Arc::new(task_fn);

        let mut handles = Vec::with_capacity(items.len());
        for item in items {
            let dl = self.clone();
            let sem = sem.clone();
            let total = total.clone();
            let pb = pb.clone();
            let task_fn = task_fn.clone();
            handles.push(tokio::spawn(async move {
                let _permit = sem.acquire().await.unwrap();
                if dl.has_failed() { return; }
                let n = task_fn(dl, item).await;
                total.fetch_add(n, Ordering::Relaxed);
                pb.inc(1);
            }));
        }
        for h in handles {
            if let Err(error) = h.await { self.fail(format!("{label} worker failed: {error}")); }
        }
        let t = total.load(Ordering::Relaxed);
        pb.finish_with_message(format!("{t} rows"));
        t
    }

    // ═══════════════════════════════════════════════════════════════════
    // DOWNLOAD METHODS
    // ═══════════════════════════════════════════════════════════════════

    /// Download A-share stock list.
    /// Python: download_tushare_stock_list — adds is_st, board, list_status; filters 沪深 A 股.
    pub async fn download_stock_list(&self) -> usize {
        info!("Downloading Tushare stock list...");

        let mut all_rows: Vec<Value> = Vec::new();
        for (status, label) in [("L", "L"), ("D", "D")] {
            let mut data = self.tushare_call("stock_basic", &json!({"list_status": status})).await;
            // list_status is a query param, not returned by API — add it manually
            for row in &mut data {
                if let Some(obj) = row.as_object_mut() {
                    obj.insert("list_status".to_string(), Value::String(label.to_string()));
                }
            }
            all_rows.extend(data);
        }

        if all_rows.is_empty() {
            warn!("stock_basic returned empty");
            return 0;
        }

        // Filter to 沪深 A 股: ts_code starts with 00/30/60/68
        let filtered: Vec<Value> = all_rows.into_iter().filter(|row| {
            row.get("ts_code").and_then(|v| v.as_str())
                .map(|c| c.starts_with("00") || c.starts_with("30") || c.starts_with("60") || c.starts_with("68"))
                .unwrap_or(false)
        }).collect();

        // Add derived fields: is_st (from name), board (from ts_code)
        let rows: Vec<Value> = filtered.into_iter().map(|mut row| {
            if let Some(obj) = row.as_object_mut() {
                // is_st: check if name contains ST keywords
                let name = obj.get("name").and_then(|v| v.as_str()).unwrap_or("");
                let is_st = if name.contains("ST") || name.contains("*ST") || name.contains("S*ST") || name.contains("SST") { 1 } else { 0 };
                obj.insert("is_st".to_string(), Value::Number(is_st.into()));

                // board: detect from ts_code prefix
                let ts_code = obj.get("ts_code").and_then(|v| v.as_str()).unwrap_or("");
                let board = detect_board(ts_code);
                obj.insert("board".to_string(), Value::String(board.to_string()));
            }
            row
        }).collect();

        let total = self.upsert_rows("a_stock_basic", &rows, &["ts_code"]).await;
        info!("Stock list: {total} rows");
        total
    }

    /// Download trading calendar.
    pub async fn download_trade_cal(&self) -> usize {
        let mut total = 0;
        for exchange in ["SSE", "SZSE"] {
            let data = self.tushare_call("trade_cal", &json!({
                "exchange": exchange, "start_date": "20000101", "end_date": "20261231",
            })).await;
            if !data.is_empty() {
                total += self.upsert_rows("a_trade_cal", &data, &["exchange", "cal_date"]).await;
            }
        }
        info!("Trade calendar: {total} rows");
        total
    }

    /// Download daily prices in chronological order, committing each market date atomically.
    pub async fn download_daily_prices(&self, start_date: &str, incremental: bool) -> usize {
        let pending = self.pending_trade_dates("a_daily_price", start_date, incremental).await;

        if pending.is_empty() {
            info!("All trade dates done for a_daily_price");
            return 0;
        }

        let only = self.only_ticker.clone();
        self.run_dates(pending, "A-Share Daily", move |dl, date| {
            let only = only.clone();
            async move {
            let daily = dl.tushare_call("daily", &json!({"trade_date": &date})).await;
            let basic = dl.tushare_call("daily_basic", &json!({"trade_date": &date})).await;
            let adj = dl.tushare_call("adj_factor", &json!({"trade_date": &date})).await;
            if dl.has_failed() { return 0; }
            if daily.is_empty() {
                dl.fail(format!("daily returned no rows for open date {date}; retry later"));
                return 0;
            }
            let merged = merge_daily(&daily, &basic, &adj);

            // Filter to 沪深 A 股 + add is_limit_up/is_limit_down
            let processed: Vec<Value> = merged.into_iter().filter_map(|mut row| {
                let obj = row.as_object_mut()?;
                let ts_code = obj.get("ts_code").and_then(|v| v.as_str()).unwrap_or("");
                if let Some(t) = &only { if ts_code != t { return None; } }
                if !(ts_code.starts_with("00") || ts_code.starts_with("30") || ts_code.starts_with("60") || ts_code.starts_with("68")) {
                    return None;
                }
                // Derive limit up/down from pct_chg + board
                let pct_chg = obj.get("pct_chg").and_then(|v| v.as_f64()).unwrap_or(0.0);
                let board = detect_board(ts_code);
                let limit = if board == "创业板" || board == "科创板" { 20.0 } else { 10.0 };
                let threshold = limit - 0.05;
                obj.insert("is_limit_up".to_string(), Value::Number(if pct_chg >= threshold { 1 } else { 0 }.into()));
                obj.insert("is_limit_down".to_string(), Value::Number(if pct_chg <= -threshold { 1 } else { 0 }.into()));
                Some(row)
            }).collect();

            let mut n = 0;
            if !processed.is_empty() {
                n = dl.upsert_rows("a_daily_price", &processed, &["ts_code", "trade_date"]).await;
            }
            dl.mark_done("a_daily_price", &date).await;
            n
            }
        }).await
    }

    /// Shared driver for market-wide, per-trade-date Tushare endpoints (one API
    /// call per date, concurrent across dates) — same incremental/mark_done
    /// contract as `download_daily_prices`.
    ///
    /// `ts_code_field`: `Some(field)` applies `self.only_ticker` filtering to
    /// rows carrying a per-stock dimension (e.g. "ts_code"); `None` for
    /// endpoints with no per-stock dimension (e.g. exchange- or market-level
    /// aggregates), where `only_ticker` has no meaning and is ignored.
    async fn download_by_trade_date(
        &self,
        api_name: &str,
        table: &str,
        unique_keys: &[&str],
        start_date: &str,
        incremental: bool,
        ts_code_field: Option<&str>,
        restricted: bool,
    ) -> usize {
        let pending = self.pending_trade_dates(table, start_date, incremental).await;

        if pending.is_empty() {
            info!("All trade dates done for {table}");
            return 0;
        }

        let label = format!("Tushare {api_name}");
        let api_name = api_name.to_string();
        let table = table.to_string();
        let unique_keys: Vec<String> = unique_keys.iter().map(|s| s.to_string()).collect();
        let only = self.only_ticker.clone();
        let ts_code_field = ts_code_field.map(|s| s.to_string());

        self.run_dates(pending, &label, move |dl, date| {
            let api_name = api_name.clone();
            let table = table.clone();
            let unique_keys = unique_keys.clone();
            let only = only.clone();
            let ts_code_field = ts_code_field.clone();
            async move {
                let data = if restricted {
                    dl.tushare_call_restricted(&api_name, &json!({"trade_date": &date})).await
                } else {
                    dl.tushare_call(&api_name, &json!({"trade_date": &date})).await
                };
                let processed: Vec<Value> = match (&only, &ts_code_field) {
                    (Some(ticker), Some(field)) => data.into_iter().filter(|row| {
                        row.get(field).and_then(|v| v.as_str()).map(|c| c == ticker).unwrap_or(false)
                    }).collect(),
                    _ => data,
                };

                let mut n = 0;
                if !processed.is_empty() {
                    let uk_refs: Vec<&str> = unique_keys.iter().map(|s| s.as_str()).collect();
                    n = dl.upsert_rows(&table, &processed, &uk_refs).await;
                }
                if !processed.is_empty() {
                    dl.mark_done(&table, &date).await;
                } else {
                    info!("{table}/{date}: no published rows; leaving date unmarked");
                }

                n
            }
        }).await
    }

    /// Download Dragon-Tiger list daily detail (龙虎榜每日交易明细).
    /// Unique key includes `reason`: a stock can be listed under multiple
    /// trigger reasons the same day (verified duplicate ts_code+trade_date
    /// rows via a live API probe call — not assumed from documentation).
    pub async fn download_top_list(&self, start_date: &str, incremental: bool) -> usize {
        self.download_by_trade_date(
            "top_list", "a_top_list", &["ts_code", "trade_date", "reason"],
            start_date, incremental, Some("ts_code"), true,
        ).await
    }

    /// Download Dragon-Tiger list institutional/seat detail (龙虎榜机构成交明细).
    /// Unique key includes `exalter` (席位名) + `reason`: (ts_code, trade_date)
    /// alone is NOT unique — the same seat can carry multiple rows per stock
    /// per day, one per trigger reason (verified via a live API probe call).
    pub async fn download_top_inst(&self, start_date: &str, incremental: bool) -> usize {
        self.download_by_trade_date(
            "top_inst", "a_top_inst", &["ts_code", "trade_date", "exalter", "reason"],
            start_date, incremental, Some("ts_code"), true,
        ).await
    }

    /// Download margin trading summary by exchange (融资融券交易汇总).
    /// Market-wide: one row per (trade_date, exchange_id), no per-stock
    /// dimension, so `only_ticker` filtering does not apply.
    pub async fn download_margin(&self, start_date: &str, incremental: bool) -> usize {
        self.download_by_trade_date(
            "margin", "a_margin", &["trade_date", "exchange_id"],
            start_date, incremental, None, true,
        ).await
    }

    /// Download margin trading detail by stock (融资融券交易明细).
    pub async fn download_margin_detail(&self, start_date: &str, incremental: bool) -> usize {
        self.download_by_trade_date(
            "margin_detail", "a_margin_detail", &["ts_code", "trade_date"],
            start_date, incremental, Some("ts_code"), true,
        ).await
    }

    /// Download Shanghai/Shenzhen-Hong Kong Stock Connect northbound/southbound
    /// flow (沪深港通资金流向). Market-wide: one row per trade_date, no
    /// per-stock dimension.
    pub async fn download_moneyflow_hsgt(&self, start_date: &str, incremental: bool) -> usize {
        self.download_by_trade_date(
            "moneyflow_hsgt", "a_moneyflow_hsgt", &["trade_date"],
            start_date, incremental, None, true,
        ).await
    }


    /// Download financial/event table (per ts_code, concurrent).
    ///
    /// Successful per-stock fetches are checkpointed for 12 hours, including
    /// empty successful responses. Failures never advance the checkpoint.
    async fn download_financial_table(&self, api_name: &str, table: &str, unique_keys: &[&str], incremental: bool, _staleness_field: &str, restricted: bool) -> usize {
        let ts_codes = self.get_all_ts_codes().await;

        // A financial report's period is not its fetch time. Persist successful
        // fetch checkpoints, so a restart does not re-fetch every completed stock.
        let done: HashSet<String> = if incremental {
            match sqlx::query_scalar::<_, String>(
                "SELECT ticker FROM import_progress WHERE table_name = ? AND completed_at >= NOW() - INTERVAL 12 HOUR"
            ).bind(table).fetch_all(&self.pool).await {
                Ok(rows) => rows.into_iter().collect(),
                Err(error) => { self.fail(format!("Cannot read {table} checkpoints: {error}")); return 0; }
            }
        } else { self.get_done_tickers(table).await };
        let pending: Vec<String> = ts_codes.into_iter().filter(|t| !done.contains(t)).collect();

        let api_name = api_name.to_string();
        let table = table.to_string();
        let unique_keys: Vec<String> = unique_keys.iter().map(|s| s.to_string()).collect();

        self.run_concurrent(pending, &format!("Tushare {api_name}"), move |dl, ts_code| {
            let api_name = api_name.clone();
            let table = table.clone();
            let unique_keys = unique_keys.clone();
            async move {
                let data = if restricted {
                    dl.tushare_call_restricted(&api_name, &json!({"ts_code": &ts_code})).await
                } else {
                    dl.tushare_call(&api_name, &json!({"ts_code": &ts_code})).await
                };
                let mut n = 0;
                if !data.is_empty() {
                    let uk_refs: Vec<&str> = unique_keys.iter().map(|s| s.as_str()).collect();
                    n = dl.upsert_rows(&table, &data, &uk_refs).await;
                }
                dl.mark_done(&table, &ts_code).await;
                n
            }
        }).await
    }

    pub async fn download_income(&self, incremental: bool) -> usize {
        self.download_financial_table("income", "a_financial_income",
            &["ts_code", "end_date", "report_type"], incremental, "end_date", false).await
    }

    pub async fn download_balancesheet(&self, incremental: bool) -> usize {
        self.download_financial_table("balancesheet", "a_financial_balance",
            &["ts_code", "end_date", "report_type"], incremental, "end_date", false).await
    }

    pub async fn download_cashflow(&self, incremental: bool) -> usize {
        self.download_financial_table("cashflow", "a_financial_cashflow",
            &["ts_code", "end_date", "report_type"], incremental, "end_date", false).await
    }

    pub async fn download_fina_indicator(&self, incremental: bool) -> usize {
        self.download_financial_table("fina_indicator", "a_financial_indicator",
            &["ts_code", "end_date"], incremental, "end_date", false).await
    }

    /// Download earnings forecast (业绩预告). Event-driven factor input.
    pub async fn download_forecast(&self, incremental: bool) -> usize {
        self.download_financial_table("forecast", "a_forecast",
            &["ts_code", "ann_date", "end_date"], incremental, "ann_date", true).await
    }

    /// Download earnings express (业绩快报). Event-driven factor input.
    pub async fn download_express(&self, incremental: bool) -> usize {
        self.download_financial_table("express", "a_express",
            &["ts_code", "ann_date", "end_date"], incremental, "ann_date", true).await
    }

    /// Download shareholder increase/decrease disclosures (股东增减持).
    /// Event-driven factor input.
    pub async fn download_stk_holdertrade(&self, incremental: bool) -> usize {
        self.download_financial_table("stk_holdertrade", "a_stk_holdertrade",
            &["ts_code", "ann_date", "holder_name", "in_de", "change_vol"], incremental, "ann_date", true).await
    }

    /// Download share buyback disclosures (股票回购). Event-driven factor input.
    pub async fn download_repurchase(&self, incremental: bool) -> usize {
        self.download_financial_table("repurchase", "a_repurchase",
            &["ts_code", "ann_date", "proc"], incremental, "ann_date", true).await
    }

    /// Download restricted-share unlock schedule (限售股解禁). Event-driven
    /// factor input. Staleness keyed off `float_date` (only date column this
    /// table has consistently populated — `ann_date` can be null).
    pub async fn download_share_float(&self, incremental: bool) -> usize {
        self.download_financial_table("share_float", "a_share_float",
            &["ts_code", "float_date", "holder_name", "share_type"], incremental, "float_date", true).await
    }

    /// Download industry classification (Shenwan).
    pub async fn download_industry(&self) -> usize {
        info!("Downloading Shenwan industry classification...");
        let mut total = 0;
        for src in ["SW2021", "SW2014"] {
            for level in ["L1", "L2"] {
                let indices = self.tushare_call("index_classify", &json!({"level": level, "src": src})).await;
                let n_idx = indices.len();
                info!("Industry {src}/{level}: {n_idx} indices, fetching members...");
                if n_idx == 0 { continue; }
                let mut sub_total = 0;
                for (i, idx) in indices.iter().enumerate() {
                    let index_code = match idx.get("index_code").and_then(|v| v.as_str()) {
                        Some(c) => c.to_string(), None => continue,
                    };
                    // Tushare `index_classify` returns `industry_name`, not
                    // `index_name`; retain it in both DB fields for consumers
                    // of either legacy name.
                    let industry_name = idx.get("industry_name")
                        .or_else(|| idx.get("index_name"))
                        .and_then(|value| value.as_str())
                        .unwrap_or("");
                    if industry_name.is_empty() {
                        warn!("Industry {src}/{level}/{index_code}: missing industry_name");
                        continue;
                    }

                    let members = self.tushare_call("index_member", &json!({"index_code": &index_code})).await;
                    let rows = match build_industry_member_rows(
                        src,
                        level,
                        idx,
                        industry_name,
                        &members,
                        self.only_ticker.as_deref(),
                    ) {
                        Ok(rows) => rows,
                        Err(error) => {
                            warn!("Industry {src}/{level}/{index_code}: invalid member response: {error}");
                            continue;
                        }
                    };
                    if !rows.is_empty() {
                        sub_total += self.upsert_rows("a_industry_class", &rows,
                            &["ts_code", "src", "level", "index_code", "in_date"]).await;
                    }
                    if (i + 1) % 30 == 0 || i + 1 == n_idx {
                        info!("Industry {src}/{level}: {}/{n_idx} indices, {sub_total} rows so far", i + 1);
                    }
                }
                total += sub_total;
            }
        }
        info!("Industry class: {total} rows");
        total
    }

    /// Download index daily prices.
    pub async fn download_index_daily(&self, start_date: &str) -> usize {
        let indices = [
            ("000001.SH", "上证综指"), ("000300.SH", "沪深300"),
            ("399001.SZ", "深证成指"), ("399006.SZ", "创业板指"),
            ("000688.SH", "科创50"),
        ];
        let mut total = 0;
        for (code, name) in &indices {
            let data = self.tushare_call("index_daily", &json!({"ts_code": code, "start_date": start_date})).await;
            if !data.is_empty() {
                total += self.upsert_rows("a_index_daily", &data, &["ts_code", "trade_date"]).await;
            }
            info!("Index {name} ({code}): done");
        }
        total
    }

    /// Download macro indicators.
    /// Collect all rows in memory first, then ONE upsert_rows call — avoids
    /// 20k+ single-row INSERTs (~5h before this fix vs ~10s after).
    pub async fn download_macro(&self) -> usize {
        info!("Downloading A-share macro indicators...");
        let mut all_rows: Vec<Value> = Vec::new();

        let data = self.tushare_call("shibor", &json!({"start_date": "20060101"})).await;
        for row in &data {
            let date = match row.get("date").and_then(|v| v.as_str()) { Some(d) => d, None => continue };
            for (field, code) in [("on", "SHIBOR_ON"), ("1w", "SHIBOR_1W"), ("1m", "SHIBOR_1M"), ("3m", "SHIBOR_3M")] {
                if let Some(val) = row.get(field).and_then(|v| v.as_f64()) {
                    all_rows.push(json!({"indicator": code, "report_date": date, "freq": "D", "value": val}));
                }
            }
        }
        info!("  shibor: {} rows queued", all_rows.len());

        let cpi_start = all_rows.len();
        let data = self.tushare_call("cn_cpi", &json!({"start_m": "200001"})).await;
        for row in &data {
            let month = match row.get("month").and_then(|v| v.as_str()) { Some(m) => m, None => continue };
            // Tushare cn_cpi/cn_ppi.month is "YYYYMM" (e.g. "202603"). Older docs say
            // "YYYY.MM" but actual API returns no separator — handle both defensively.
            let normalized = month.replace('.', "");
            let date = if normalized.len() == 6 {
                format!("{}-{}-01", &normalized[..4], &normalized[4..])
            } else {
                continue;
            };
            if let Some(val) = row.get("nt_yoy").and_then(|v| v.as_f64()) {
                all_rows.push(json!({"indicator": "CPI_YOY", "report_date": date, "freq": "M", "value": val}));
            }
        }
        info!("  cn_cpi: {} rows queued", all_rows.len() - cpi_start);

        let ppi_start = all_rows.len();
        let data = self.tushare_call("cn_ppi", &json!({"start_m": "200001"})).await;
        for row in &data {
            let month = match row.get("month").and_then(|v| v.as_str()) { Some(m) => m, None => continue };
            // Tushare cn_cpi/cn_ppi.month is "YYYYMM" (e.g. "202603"). Older docs say
            // "YYYY.MM" but actual API returns no separator — handle both defensively.
            let normalized = month.replace('.', "");
            let date = if normalized.len() == 6 {
                format!("{}-{}-01", &normalized[..4], &normalized[4..])
            } else {
                continue;
            };
            if let Some(val) = row.get("ppi_yoy").and_then(|v| v.as_f64()) {
                all_rows.push(json!({"indicator": "PPI_YOY", "report_date": date, "freq": "M", "value": val}));
            }
        }
        info!("  cn_ppi: {} rows queued", all_rows.len() - ppi_start);

        // Single batched upsert — chunk inside upsert_rows handles >200 rows.
        let mut total = 0;
        if !all_rows.is_empty() {
            total = self.upsert_rows("a_macro_indicator", &all_rows,
                &["indicator", "report_date", "freq"]).await;
        }

        info!("Macro indicators: {total} rows");
        total
    }

    /// Download A-share commodity futures (主力合约 daily bars).
    ///
    /// Algorithm (per symbol):
    ///   1. `fut_mapping(SYMBOL.EXCHANGE)` → maps each trade_date to the active main contract
    ///   2. Group mapping rows by `mapping_ts_code` (each = one continuous segment)
    ///   3. For each segment, `fut_daily(contract_code)` for that date range
    ///   4. Filter daily bars to dates where contract was actually the main one
    ///   5. Tag with `ts_code = SYMBOL.EXCHANGE` (not contract code) for stable identity
    ///
    /// Incremental: per-symbol latest trade_date in `a_commodity_price` → resume from there.
    pub async fn download_commodity(&self, incremental: bool) -> usize {
        // 16 symbols (matches Python services/config.COMMODITY_SYMBOLS).
        // (symbol, exchange)
        let pairs: &[(&str, &str)] = &[
            ("AU","SHF"),("AG","SHF"),("CU","SHF"),("AL","SHF"),
            ("ZN","SHF"),("PB","SHF"),("NI","SHF"),("SN","SHF"),
            ("RB","SHF"),
            ("I","DCE"),("J","DCE"),("JM","DCE"),
            ("SC","INE"),
            ("SA","ZCE"),("MA","ZCE"),
        ];

        let latest_by_code = if incremental {
            self.get_ticker_latest("a_commodity_price", "trade_date").await
        } else {
            std::collections::HashMap::new()
        };

        let default_start = "20150101".to_string();
        let today = chrono::Local::now().format("%Y%m%d").to_string();

        let mut sym_meta: std::collections::HashMap<String, (String, String)> = std::collections::HashMap::new();
        for (sym, ex) in pairs {
            let ts_code = format!("{sym}.{ex}");
            let start = latest_by_code.get(&ts_code)
                .map(|d| d.replace('-', ""))
                .and_then(|d| chrono::NaiveDate::parse_from_str(&d, "%Y%m%d").ok())
                .and_then(|nd| nd.succ_opt())
                .map(|nd| nd.format("%Y%m%d").to_string())
                .unwrap_or_else(|| default_start.clone());
            sym_meta.insert(sym.to_string(), (ex.to_string(), start));
        }
        let sym_meta = Arc::new(sym_meta);
        let end_date = today.clone();
        let symbols: Vec<String> = pairs.iter().map(|(s, _)| s.to_string()).collect();

        self.run_concurrent(
            symbols,
            "Tushare commodity",
            move |dl, sym| {
                let sym_meta = sym_meta.clone();
                let end = end_date.clone();
                async move {
                    let (exchange, start) = match sym_meta.get(&sym) {
                        Some(m) => m.clone(),
                        None => return 0,
                    };
                    if start > end {
                        return 0;
                    }
                    let ts_code = format!("{sym}.{exchange}");

                    // Step 1: fut_mapping
                    let mapping = dl.tushare_call("fut_mapping", &json!({
                        "ts_code": &ts_code,
                        "start_date": &start,
                        "end_date": &end,
                    })).await;
                    if mapping.is_empty() {
                        return 0;
                    }

                    // Step 2: group by mapping_ts_code, find date range per group.
                    use std::collections::BTreeMap;
                    let mut groups: BTreeMap<String, (String, String, std::collections::HashSet<String>)> = BTreeMap::new();
                    for row in &mapping {
                        let trade_date = match row.get("trade_date").and_then(|v| v.as_str()) {
                            Some(d) => d.to_string(), None => continue,
                        };
                        let contract = match row.get("mapping_ts_code").and_then(|v| v.as_str()) {
                            Some(c) => c.to_string(), None => continue,
                        };
                        let entry = groups.entry(contract).or_insert_with(||
                            (trade_date.clone(), trade_date.clone(), std::collections::HashSet::new()));
                        if trade_date < entry.0 { entry.0 = trade_date.clone(); }
                        if trade_date > entry.1 { entry.1 = trade_date.clone(); }
                        entry.2.insert(trade_date);
                    }

                    // Step 3+4: per-segment fut_daily, filter by valid dates, accumulate.
                    let mut all_rows: Vec<Value> = Vec::new();
                    let mut seen_dates: std::collections::HashSet<String> = std::collections::HashSet::new();
                    for (contract, (seg_start, seg_end, valid_dates)) in &groups {
                        let daily = dl.tushare_call("fut_daily", &json!({
                            "ts_code": contract,
                            "start_date": seg_start,
                            "end_date": seg_end,
                        })).await;
                        for row in daily {
                            let trade_date = match row.get("trade_date").and_then(|v| v.as_str()) {
                                Some(d) => d.to_string(), None => continue,
                            };
                            if !valid_dates.contains(&trade_date) { continue; }
                            // Dedup across segments (rare but possible at boundaries).
                            if !seen_dates.insert(trade_date.clone()) { continue; }
                            // Stamp stable identity instead of contract code.
                            let mut o = match row.as_object() { Some(o) => o.clone(), None => continue };
                            o.insert("ts_code".to_string(), Value::String(ts_code.clone()));
                            o.insert("name".to_string(), Value::String(sym.clone()));
                            all_rows.push(Value::Object(o));
                        }
                    }

                    if all_rows.is_empty() {
                        return 0;
                    }
                    dl.upsert_rows("a_commodity_price", &all_rows, &["ts_code", "trade_date"]).await
                }
            },
        ).await
    }

    /// Download all A-share data (full).
    pub async fn download_all(&self, start_date: &str) -> usize {
        let mut total = 0;
        total += self.download_stock_list().await;
        if self.has_failed() { return total; }
        total += self.download_trade_cal().await;
        if self.has_failed() { return total; }
        total += self.download_industry().await;
        if self.has_failed() { return total; }
        total += self.download_index_daily(start_date).await;
        if self.has_failed() { return total; }
        total += self.download_macro().await;
        if self.has_failed() { return total; }
        total += self.download_daily_prices(start_date, false).await;
        if self.has_failed() { return total; }
        total += self.download_income(false).await;
        if self.has_failed() { return total; }
        total += self.download_balancesheet(false).await;
        if self.has_failed() { return total; }
        total += self.download_cashflow(false).await;
        if self.has_failed() { return total; }
        total += self.download_fina_indicator(false).await;
        if self.has_failed() { return total; }
        total += self.download_commodity(false).await;
        if self.has_failed() { return total; }
        total += self.download_top_list(start_date, false).await;
        if self.has_failed() { return total; }
        total += self.download_top_inst(start_date, false).await;
        if self.has_failed() { return total; }
        total += self.download_margin(start_date, false).await;
        if self.has_failed() { return total; }
        total += self.download_margin_detail(start_date, false).await;
        if self.has_failed() { return total; }
        total += self.download_moneyflow_hsgt(start_date, false).await;
        if self.has_failed() { return total; }
        total += self.download_forecast(false).await;
        if self.has_failed() { return total; }
        total += self.download_express(false).await;
        if self.has_failed() { return total; }
        total += self.download_stk_holdertrade(false).await;
        if self.has_failed() { return total; }
        total += self.download_repurchase(false).await;
        if self.has_failed() { return total; }
        total += self.download_share_float(false).await;
        if self.has_failed() { return total; }
        info!("Tushare download_all total: {total}");
        total
    }

    /// Incremental update all A-share data.
    pub async fn update_all(&self) -> usize {
        let mut total = 0;
        total += self.download_stock_list().await;
        if self.has_failed() { return total; }
        total += self.download_trade_cal().await;
        if self.has_failed() { return total; }
        total += self.download_industry().await;
        if self.has_failed() { return total; }
        total += self.download_daily_prices("20200101", true).await;
        if self.has_failed() { return total; }
        total += self.download_income(true).await;
        if self.has_failed() { return total; }
        total += self.download_balancesheet(true).await;
        if self.has_failed() { return total; }
        total += self.download_cashflow(true).await;
        if self.has_failed() { return total; }
        total += self.download_fina_indicator(true).await;
        if self.has_failed() { return total; }
        total += self.download_index_daily("20200101").await;
        if self.has_failed() { return total; }
        total += self.download_macro().await;
        if self.has_failed() { return total; }
        total += self.download_commodity(true).await;
        if self.has_failed() { return total; }
        total += self.download_top_list("20200101", true).await;
        if self.has_failed() { return total; }
        total += self.download_top_inst("20200101", true).await;
        if self.has_failed() { return total; }
        total += self.download_margin("20200101", true).await;
        if self.has_failed() { return total; }
        total += self.download_margin_detail("20200101", true).await;
        if self.has_failed() { return total; }
        total += self.download_moneyflow_hsgt("20200101", true).await;
        if self.has_failed() { return total; }
        total += self.download_forecast(true).await;
        if self.has_failed() { return total; }
        total += self.download_express(true).await;
        if self.has_failed() { return total; }
        total += self.download_stk_holdertrade(true).await;
        if self.has_failed() { return total; }
        total += self.download_repurchase(true).await;
        if self.has_failed() { return total; }
        total += self.download_share_float(true).await;
        if self.has_failed() { return total; }
        info!("Tushare update_all total: {total}");
        total
    }
}

// ═══════════════════════════════════════════════════════════════════════
// HELPERS
// ═══════════════════════════════════════════════════════════════════════

fn safe_tushare_rate(configured: u32) -> u32 {
    // 200/min endpoints also apply to income and industry membership.
    // Zero must not disable the provider cap.
    if configured == 0 { RESTRICTED_RATE_LIMIT } else { configured.min(RESTRICTED_RATE_LIMIT) }
}

fn retryable_write_error(error: &sqlx::Error) -> bool {
    error.as_database_error()
        .and_then(|e| e.try_downcast_ref::<sqlx::mysql::MySqlDatabaseError>())
        .is_some_and(|e| matches!(e.number(), 1205 | 1213))
}

fn select_trade_dates(dates: Vec<&str>, latest: Option<chrono::NaiveDate>, today: chrono::NaiveDate) -> Result<Vec<String>, String> {
    let mut selected = Vec::new();
    for value in dates {
        let date = chrono::NaiveDate::parse_from_str(value, "%Y%m%d")
            .or_else(|_| chrono::NaiveDate::parse_from_str(value, "%Y-%m-%d"))
            .map_err(|e| format!("Invalid trading date {value}: {e}"))?;
        if date <= today && latest.is_none_or(|cutoff| date >= cutoff) {
            selected.push(date.format("%Y%m%d").to_string());
        }
    }
    selected.sort();
    selected.dedup();
    Ok(selected)
}

fn merge_daily(daily: &[Value], basic: &[Value], adj: &[Value]) -> Vec<Value> {
    use std::collections::HashMap;
    let mut basic_map: HashMap<&str, &Value> = HashMap::new();
    for row in basic {
        if let Some(code) = row.get("ts_code").and_then(|v| v.as_str()) { basic_map.insert(code, row); }
    }

    let mut adj_map: HashMap<&str, &Value> = HashMap::new();
    for row in adj {
        if let Some(code) = row.get("ts_code").and_then(|v| v.as_str()) { adj_map.insert(code, row); }
    }

    daily.iter().filter_map(|row| {
        let code = row.get("ts_code").and_then(|v| v.as_str())?;
        let mut merged = row.as_object()?.clone();
        if let Some(b) = basic_map.get(code) {
            if let Some(b_obj) = b.as_object() {
                for (k, v) in b_obj {
                    if k != "ts_code" && k != "trade_date" && !merged.contains_key(k) { merged.insert(k.clone(), v.clone()); }
                }
            }
        }
        if let Some(a) = adj_map.get(code) {
            if let Some(a_obj) = a.as_object() {
                for (k, v) in a_obj {
                    if k != "ts_code" && k != "trade_date" && !merged.contains_key(k) { merged.insert(k.clone(), v.clone()); }
                }
            }
        }
        Some(Value::Object(merged))
    }).collect()
}

/// Decode Tushare's columnar `data` response without dropping fields.
///
/// A mismatched row is rejected as a corrupted batch rather than partially
/// mapped into the database, where omitted trailing fields look like nulls.
fn decode_tushare_rows(data: &Value) -> Result<Vec<Value>, String> {
    let fields = data.get("fields")
        .and_then(Value::as_array)
        .ok_or_else(|| "missing data.fields array".to_string())?;
    let fields: Vec<&str> = fields.iter()
        .map(|field| field.as_str().ok_or_else(|| "non-string field name".to_string()))
        .collect::<Result<_, _>>()?;

    let items = data.get("items")
        .and_then(Value::as_array)
        .ok_or_else(|| "missing data.items array".to_string())?;
    let mut rows = Vec::with_capacity(items.len());
    for (row_index, row) in items.iter().enumerate() {
        let values = row.as_array()
            .ok_or_else(|| format!("item {row_index} is not an array"))?;
        if values.len() != fields.len() {
            return Err(format!(
                "item {row_index} has {} values for {} fields",
                values.len(),
                fields.len(),
            ));
        }

        let mut object = serde_json::Map::with_capacity(fields.len());
        for (field, value) in fields.iter().zip(values) {
            object.insert((*field).to_string(), value.clone());
        }
        rows.push(Value::Object(object));
    }
    Ok(rows)
}

/// Extract data only from a successful Tushare response.
fn tushare_response_data(response: &Value) -> Result<&Value, String> {
    let code = response.get("code").and_then(Value::as_i64).unwrap_or(-1);
    if code != 0 {
        let message = response.get("msg").and_then(Value::as_str).unwrap_or("unknown error");
        return Err(format!("API code {code}: {message}"));
    }
    response.get("data")
        .filter(|data| !data.is_null())
        .ok_or_else(|| "successful response has no data object".to_string())
}

/// Deduplicate a batch by natural key while retaining each key's final row.
fn deduplicate_rows<'a>(rows: &'a [Value], unique_keys: &[&str]) -> Vec<&'a Value> {
    let mut seen = HashSet::new();
    let mut deduplicated = Vec::new();
    for row in rows.iter().rev() {
        let object = match row.as_object() {
            Some(object) => object,
            None => continue,
        };
        let key: Vec<String> = unique_keys.iter()
            .map(|key| object.get(*key).map(Value::to_string).unwrap_or_default())
            .collect();
        if seen.insert(key) {
            deduplicated.push(row);
        }
    }
    deduplicated.reverse();
    deduplicated
}

fn build_industry_member_rows(
    src: &str,
    level: &str,
    classification: &Value,
    industry_name: &str,
    members: &[Value],
    only_ticker: Option<&str>,
) -> Result<Vec<Value>, String> {
    let index_code = classification.get("index_code")
        .and_then(Value::as_str)
        .ok_or_else(|| "classification has no index_code".to_string())?;
    let industry_code = classification.get("industry_code").cloned().unwrap_or(Value::Null);
    let is_pub = classification.get("is_pub").cloned().unwrap_or(Value::Null);
    let parent_code = classification.get("parent_code").cloned().unwrap_or(Value::Null);
    let mut rows = Vec::with_capacity(members.len());
    for (row_index, member) in members.iter().enumerate() {
        let ts_code = member.get("con_code")
            .or_else(|| member.get("ts_code"))
            .and_then(Value::as_str)
            .ok_or_else(|| format!("item {row_index} has no con_code or ts_code"))?;
        if only_ticker.is_some_and(|ticker| ticker != ts_code) {
            continue;
        }
        let in_date = normalize_industry_date(
            member.get("in_date").and_then(Value::as_str),
            "in_date",
            row_index,
        )?;
        let out_date = normalize_industry_date(
            member.get("out_date").and_then(Value::as_str),
            "out_date",
            row_index,
        )?;
        rows.push(json!({
            "ts_code": ts_code,
            "index_code": index_code,
            "index_name": industry_name,
            "industry_name": industry_name,
            "industry_code": industry_code.clone(),
            "is_pub": is_pub.clone(),
            "parent_code": parent_code.clone(),
            "src": src,
            "level": level,
            "in_date": in_date,
            "out_date": out_date,
            "is_new": member.get("is_new").and_then(Value::as_str).unwrap_or(""),
        }));
    }
    Ok(rows)
}

fn normalize_industry_date(
    value: Option<&str>,
    field: &str,
    row_index: usize,
) -> Result<String, String> {
    let Some(value) = value else {
        return Ok(String::new());
    };
    if value.is_empty() {
        return Ok(String::new());
    }
    chrono::NaiveDate::parse_from_str(value, "%Y%m%d")
        .or_else(|_| chrono::NaiveDate::parse_from_str(value, "%Y-%m-%d"))
        .map(|date| date.format("%Y-%m-%d").to_string())
        .map_err(|error| format!("item {row_index} has invalid {field}={value}: {error}"))
}

/// Detect board type from ts_code prefix (Python: _detect_board).
fn detect_board(ts_code: &str) -> &'static str {
    let code = ts_code.split('.').next().unwrap_or("");
    if code.starts_with("00") || code.starts_with("60") { "主板" }
    else if code.starts_with("30") { "创业板" }
    else if code.starts_with("68") { "科创板" }
    else { "其他" }
}

fn to_sql_literal(val: &Value) -> String {
    match val {
        Value::Null => "NULL".to_string(),
        Value::Bool(b) => if *b { "1".to_string() } else { "0".to_string() },
        Value::Number(n) => n.to_string(),
        // Empty string → NULL: Tushare returns "" for missing date/numeric fields.
        // Without this, PG rejects "" cast to date/numeric and the row fails.
        Value::String(s) if s.is_empty() => "NULL".to_string(),
        Value::String(s) => format!("'{}'", s.replace('\'', "''")),
        _ => format!("'{}'", val.to_string().replace('\'', "''")),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn provider_cap_cannot_be_disabled_or_exceeded() {
        assert_eq!(safe_tushare_rate(500), 150);
        assert_eq!(safe_tushare_rate(0), 150);
        assert_eq!(safe_tushare_rate(60), 60);
    }

    #[test]
    fn incremental_dates_normalize_sort_and_replay_boundary_without_future_dates() {
        let latest = chrono::NaiveDate::from_ymd_opt(2026, 8, 28).unwrap();
        let today = chrono::NaiveDate::from_ymd_opt(2026, 9, 26).unwrap();
        let dates = select_trade_dates(vec![
            "20260925", "20260820", "20260828", "2026-08-28", "20260928", "20260831",
        ], Some(latest), today).unwrap();
        assert_eq!(dates, ["20260828", "20260831", "20260925"]);
        assert!(select_trade_dates(vec!["20260230"], None, today).is_err());
    }

    #[tokio::test]
    async fn failed_date_stops_later_dates_and_does_not_mark_progress() {
        let pool = sqlx::mysql::MySqlPoolOptions::new()
            .connect_lazy("mysql://test:test@127.0.0.1:1/test").unwrap();
        let dl = TushareDownloader::new(String::new(), pool, 200);
        let visited = Arc::new(std::sync::Mutex::new(Vec::new()));
        let captured = visited.clone();
        dl.run_dates(vec!["20260828".into(), "20260831".into()], "test", move |dl, date| {
            let captured = captured.clone();
            async move {
                captured.lock().unwrap().push(date);
                dl.fail("simulated API or SQL failure");
                0
            }
        }).await;
        assert_eq!(*visited.lock().unwrap(), ["20260828"]);
        assert!(dl.clone().has_failed());
        // A failed run must return before touching this unreachable database.
        tokio::time::timeout(std::time::Duration::from_millis(100),
            dl.mark_done("a_daily_price", "20260828")).await.unwrap();
    }

    #[tokio::test]
    #[ignore = "requires QUANT_TEST_DATABASE_URL; uses session-local temporary tables"]
    async fn mysql_batch_rolls_back_on_failure_and_classifies_lock_conflicts() {
        let url = std::env::var("QUANT_TEST_DATABASE_URL").expect("test DB URL");
        let pool = sqlx::mysql::MySqlPoolOptions::new().max_connections(1).connect(&url).await.unwrap();
        sqlx::query("CREATE TEMPORARY TABLE quant_update_test (id INT PRIMARY KEY, value INT NOT NULL) ENGINE=InnoDB")
            .execute(&pool).await.unwrap();
        let dl = TushareDownloader::new(String::new(), pool.clone(), 200);
        let error = dl.write_batch(&[
            "INSERT INTO quant_update_test VALUES (1, 1)".into(),
            "INSERT INTO quant_update_test VALUES (2, NULL)".into(),
        ]).await.unwrap_err();
        assert!(!retryable_write_error(&error));
        let count: i64 = sqlx::query_scalar("SELECT COUNT(*) FROM quant_update_test").fetch_one(&pool).await.unwrap();
        assert_eq!(count, 0, "earlier chunks must roll back");
        assert_eq!(dl.write_batch(&["INSERT INTO quant_update_test VALUES (1, 1)".into()]).await.unwrap(), 1);
        // Exercise the same MySQL error-number path as a lock timeout/deadlock.
        for number in [1205, 1213] {
            // MySQL SIGNAL is unavailable through the prepared-statement protocol.
            let error = sqlx::raw_sql(&format!(
                "SIGNAL SQLSTATE 'HY000' SET MYSQL_ERRNO = {number}, MESSAGE_TEXT = 'test lock conflict'"
            )).execute(&pool).await.unwrap_err();
            assert!(retryable_write_error(&error));
            let count: i64 = sqlx::query_scalar("SELECT COUNT(*) FROM quant_update_test").fetch_one(&pool).await.unwrap();
            assert_eq!(count, 1);
        }
        pool.close().await;
    }

    #[test]
    fn decodes_every_tushare_field_without_filtering() {
        let data = json!({
            "fields": ["ts_code", "trade_date", "open", "extra"],
            "items": [["000001.SZ", "20240830", 10.5, null]],
        });

        let rows = decode_tushare_rows(&data).expect("valid Tushare response");

        assert_eq!(rows.len(), 1);
        let row = rows[0].as_object().expect("decoded object");
        assert_eq!(row.len(), 4);
        assert_eq!(row.get("ts_code"), Some(&json!("000001.SZ")));
        assert_eq!(row.get("trade_date"), Some(&json!("20240830")));
        assert_eq!(row.get("open"), Some(&json!(10.5)));
        assert_eq!(row.get("extra"), Some(&Value::Null));
    }

    #[test]
    fn rejects_truncated_tushare_rows() {
        let data = json!({
            "fields": ["ts_code", "trade_date", "open"],
            "items": [["000001.SZ", "20240830"]],
        });

        let error = decode_tushare_rows(&data).expect_err("truncated rows must fail");

        assert!(error.contains("2 values for 3 fields"));
    }

    #[test]
    fn api_error_response_is_not_decoded_as_missing_fields() {
        let response = json!({
            "code": -2001,
            "msg": "permission denied",
            "data": null,
        });

        let error = tushare_response_data(&response).expect_err("API error must be surfaced");
        assert_eq!(error, "API code -2001: permission denied");
    }

    #[test]
    fn successful_null_data_is_reported_explicitly() {
        let response = json!({"code": 0, "msg": "", "data": null});

        let error = tushare_response_data(&response).expect_err("null data must be rejected");
        assert_eq!(error, "successful response has no data object");
    }

    #[test]
    fn merge_daily_keeps_all_sources_and_daily_precedence() {
        let daily = vec![json!({
            "ts_code": "000001.SZ",
            "trade_date": "20240830",
            "close": 10.0,
        })];
        let basic = vec![json!({
            "ts_code": "000001.SZ",
            "trade_date": "20240830",
            "close": 99.0,
            "pe_ttm": 8.0,
        })];
        let adj = vec![json!({
            "ts_code": "000001.SZ",
            "trade_date": "20240830",
            "adj_factor": 2.0,
        })];

        let merged = merge_daily(&daily, &basic, &adj);

        let row = merged[0].as_object().expect("merged object");
        assert_eq!(row.len(), 5);
        assert_eq!(row.get("close"), Some(&json!(10.0)));
        assert_eq!(row.get("pe_ttm"), Some(&json!(8.0)));
        assert_eq!(row.get("adj_factor"), Some(&json!(2.0)));
    }

    #[test]
    fn deduplication_keeps_latest_row_for_each_natural_key() {
        let rows = vec![
            json!({"ts_code": "000001.SZ", "trade_date": "20240830", "close": 10.0}),
            json!({"ts_code": "000002.SZ", "trade_date": "20240830", "close": 20.0}),
            json!({"ts_code": "000001.SZ", "trade_date": "20240830", "close": 11.0}),
        ];

        let deduplicated = deduplicate_rows(&rows, &["ts_code", "trade_date"]);

        assert_eq!(deduplicated.len(), 2);
        assert_eq!(deduplicated[0].get("ts_code"), Some(&json!("000002.SZ")));
        assert_eq!(deduplicated[1].get("close"), Some(&json!(11.0)));
    }

    #[test]
    fn industry_members_keep_tushare_classification_and_dates() {
        let members = vec![json!({
            "index_code": "801010.SI",
            "con_code": "000001.SZ",
            "in_date": "20210616",
            "out_date": null,
            "is_new": "Y",
        })];

        let rows = build_industry_member_rows(
            "SW2021",
            "L1",
            &json!({
                "index_code": "801010.SI",
                "industry_code": "801010",
                "is_pub": "1",
                "parent_code": "801000",
            }),
            "农林牧渔",
            &members,
            None,
        ).expect("valid industry members");

        assert_eq!(rows.len(), 1);
        let row = rows[0].as_object().expect("industry member row");
        assert_eq!(row.get("industry_name"), Some(&json!("农林牧渔")));
        assert_eq!(row.get("index_name"), Some(&json!("农林牧渔")));
        assert_eq!(row.get("industry_code"), Some(&json!("801010")));
        assert_eq!(row.get("is_pub"), Some(&json!("1")));
        assert_eq!(row.get("parent_code"), Some(&json!("801000")));
        assert_eq!(row.get("in_date"), Some(&json!("2021-06-16")));
        assert_eq!(row.get("out_date"), Some(&json!("")));
        assert_eq!(row.get("is_new"), Some(&json!("Y")));
    }

    #[test]
    fn industry_member_rows_reject_invalid_dates() {
        let members = vec![json!({
            "con_code": "000001.SZ",
            "in_date": "2021-99-99",
            "out_date": null,
        })];

        let error = build_industry_member_rows(
            "SW2021",
            "L1",
            &json!({"index_code": "801010.SI"}),
            "农林牧渔",
            &members,
            None,
        ).expect_err("invalid dates must reject the batch");

        assert!(error.contains("invalid in_date"));
    }

    #[test]
    fn sql_literals_quote_tushare_text_safely() {
        assert_eq!(to_sql_literal(&json!("O'Reilly")), "'O''Reilly'");
        assert_eq!(to_sql_literal(&json!("")), "NULL");
        assert_eq!(to_sql_literal(&Value::Null), "NULL");
    }
}
