//! Tushare Pro API downloader for A-share data.
//!
//! Tushare API: POST https://api.tushare.pro with JSON body.
//! Rate limit: configurable (default 200 points/min).
//! Convention: no `fields=` parameter — keep all fields returned by API.

use std::collections::HashSet;
use std::sync::Arc;
use std::sync::atomic::{AtomicUsize, Ordering};

use serde_json::{json, Value};
use sqlx::PgPool;
use tokio::sync::Semaphore;
use tracing::{info, warn};

use crate::http::ApiClient;
use crate::progress::ticker_progress;

const MAX_CONCURRENT: usize = 10; // Tushare rate limit is stricter than FMP

#[derive(Clone)]
pub struct TushareDownloader {
    pub token: String,
    pub client: ApiClient,
    pub pool: PgPool,
    /// When set, all per-ticker methods only process this ts_code (e.g. "000001.SZ").
    pub only_ticker: Option<String>,
}

impl TushareDownloader {
    pub fn new(token: String, pool: PgPool, rate_limit: u32) -> Self {
        Self {
            token,
            client: ApiClient::new(rate_limit, MAX_CONCURRENT),
            pool,
            only_ticker: None,
        }
    }

    pub fn with_ticker(mut self, ticker: Option<&str>) -> Self {
        self.only_ticker = ticker.map(|s| s.to_string());
        self
    }

    /// Call Tushare Pro API. Returns rows as Vec<Value> (each row = JSON object).
    async fn tushare_call(&self, api_name: &str, params: &Value) -> Vec<Value> {
        let body = json!({
            "api_name": api_name,
            "token": self.token,
            "params": params,
            "fields": "",
        });

        let url = "https://api.tushare.pro";
        let resp = match self.client.post_json(url, &body).await {
            Ok(v) => v,
            Err(e) => { warn!("Tushare {api_name}: {e}"); return vec![]; }
        };

        let data = match resp.get("data") {
            Some(d) => d,
            None => {
                let msg = resp.get("msg").and_then(|v| v.as_str()).unwrap_or("unknown error");
                warn!("Tushare {api_name}: {msg}");
                return vec![];
            }
        };

        let fields = match data.get("fields").and_then(|v| v.as_array()) {
            Some(f) => f.iter().filter_map(|v| v.as_str().map(|s| s.to_string())).collect::<Vec<_>>(),
            None => return vec![],
        };

        let items = match data.get("items").and_then(|v| v.as_array()) {
            Some(i) => i,
            None => return vec![],
        };

        items.iter().filter_map(|row| {
            let arr = row.as_array()?;
            let mut obj = serde_json::Map::new();
            for (i, field) in fields.iter().enumerate() {
                if i < arr.len() {
                    obj.insert(field.clone(), arr[i].clone());
                }
            }
            Some(Value::Object(obj))
        }).collect()
    }

    // ── DB helpers ──────────────────────────────────────────────────────

    async fn get_done_tickers(&self, table: &str) -> HashSet<String> {
        sqlx::query_scalar::<_, String>(
            "SELECT ticker FROM import_progress WHERE table_name = $1"
        ).bind(table).fetch_all(&self.pool).await.unwrap_or_default().into_iter().collect()
    }

    async fn mark_done(&self, table: &str, ticker: &str) {
        sqlx::query(
            "INSERT INTO import_progress (table_name, ticker, completed_at) \
             VALUES ($1, $2, NOW()) ON CONFLICT (table_name, ticker) DO UPDATE SET completed_at = NOW()"
        ).bind(table).bind(ticker).execute(&self.pool).await.ok();
    }

    async fn get_all_ts_codes(&self) -> Vec<String> {
        if let Some(t) = &self.only_ticker {
            return vec![t.clone()];
        }
        sqlx::query_scalar::<_, String>(
            "SELECT ts_code FROM a_stock_basic WHERE list_status = 'L'"
        ).fetch_all(&self.pool).await.unwrap_or_default()
    }

    async fn get_table_columns(&self, table: &str) -> HashSet<String> {
        let sql = format!(
            "SELECT column_name FROM information_schema.columns WHERE table_schema = current_schema() AND table_name = '{table}'"
        );
        let rows: Vec<(String,)> = sqlx::query_as(&sql)
            .fetch_all(&self.pool).await.unwrap_or_default();
        rows.into_iter().map(|(c,)| c).collect()
    }

    async fn get_ticker_latest(&self, table: &str, date_field: &str) -> std::collections::HashMap<String, String> {
        let sql = format!(
            "SELECT ts_code, MAX({date_field})::text as latest FROM {table} GROUP BY ts_code"
        );
        let rows: Vec<(String, Option<String>)> = sqlx::query_as(&sql)
            .fetch_all(&self.pool).await.unwrap_or_default();
        rows.into_iter()
            .filter_map(|(t, d)| d.map(|d| (t, d.trim().to_string())))
            .collect()
    }

    async fn upsert_rows(&self, table: &str, rows: &[Value], unique_keys: &[&str]) -> usize {
        if rows.is_empty() { return 0; }
        let first = match rows[0].as_object() { Some(m) => m, None => return 0 };

        let db_columns = self.get_table_columns(table).await;
        let has_updated_at = db_columns.contains("updated_at");
        let mut columns: Vec<String> = first.keys()
            .filter(|k| {
                let k = k.as_str();
                k != "id" && k != "updated_at" && (db_columns.is_empty() || db_columns.contains(k))
            })
            .cloned().collect();
        if columns.is_empty() { return 0; }
        // Add updated_at = NOW() for tables that require it
        if has_updated_at {
            columns.push("updated_at".to_string());
        }

        // Dedup
        let mut seen = HashSet::new();
        let deduped: Vec<&Value> = rows.iter().filter(|row| {
            if let Some(obj) = row.as_object() {
                let key: Vec<String> = unique_keys.iter()
                    .map(|k| obj.get(*k).map(|v| v.to_string()).unwrap_or_default()).collect();
                seen.insert(key)
            } else { false }
        }).collect();

        let col_list = columns.join(", ");
        let conflict_cols = unique_keys.join(", ");
        let update_set: String = columns.iter()
            .filter(|c| !unique_keys.contains(&c.as_str()))
            .map(|c| format!("{c} = EXCLUDED.{c}"))
            .collect::<Vec<_>>().join(", ");

        let chunk_size = 200;
        let mut total = 0usize;
        let mut error_count = 0usize;

        for chunk in deduped.chunks(chunk_size) {
            let mut values_clauses = Vec::with_capacity(chunk.len());
            for row in chunk {
                let obj = match row.as_object() { Some(m) => m, None => continue };
                let vals: Vec<String> = columns.iter().map(|col| {
                    if col == "updated_at" { "NOW()".to_string() }
                    else { to_sql_literal(obj.get(col).unwrap_or(&Value::Null)) }
                }).collect();
                values_clauses.push(format!("({})", vals.join(",")));
            }
            if values_clauses.is_empty() { continue; }

            let sql = if update_set.is_empty() {
                format!("INSERT INTO {table} ({col_list}) VALUES {} ON CONFLICT ({conflict_cols}) DO NOTHING",
                    values_clauses.join(","))
            } else {
                format!("INSERT INTO {table} ({col_list}) VALUES {} ON CONFLICT ({conflict_cols}) DO UPDATE SET {update_set}",
                    values_clauses.join(","))
            };

            match sqlx::query(&sql).execute(&self.pool).await {
                Ok(r) => total += r.rows_affected() as usize,
                Err(e) => {
                    error_count += 1;
                    if error_count <= 3 { tracing::error!("Upsert {table} failed: {e}"); }
                    if error_count == 3 { tracing::error!("Upsert {table}: suppressing further errors"); }
                }
            }
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
                let n = task_fn(dl, item).await;
                total.fetch_add(n, Ordering::Relaxed);
                pb.inc(1);
            }));
        }
        for h in handles { h.await.ok(); }
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

    /// Download daily prices (per trade_date, full market). Concurrent by date.
    pub async fn download_daily_prices(&self, start_date: &str, incremental: bool) -> usize {
        let cal_data = self.tushare_call("trade_cal", &json!({
            "exchange": "SSE", "start_date": start_date, "end_date": "20261231", "is_open": 1,
        })).await;

        let trade_dates: Vec<String> = cal_data.iter().filter_map(|v| {
            v.get("cal_date").and_then(|d| d.as_str()).map(|s| s.to_string())
        }).collect();

        let pending: Vec<String> = if incremental {
            // Find latest date in DB, only process after that
            let latest: Option<String> = sqlx::query_scalar(
                "SELECT MAX(trade_date)::text FROM a_daily_price"
            ).fetch_one(&self.pool).await.ok().flatten();
            let cutoff = latest.unwrap_or_default();
            trade_dates.into_iter().filter(|d| d.as_str() > cutoff.as_str()).collect()
        } else {
            let done = self.get_done_tickers("a_daily_price").await;
            trade_dates.into_iter().filter(|d| !done.contains(d.as_str())).collect()
        };

        if pending.is_empty() {
            info!("All trade dates done for a_daily_price");
            return 0;
        }

        let only = self.only_ticker.clone();
        self.run_concurrent(pending, "A-Share Daily", move |dl, date| {
            let only = only.clone();
            async move {
            let daily = dl.tushare_call("daily", &json!({"trade_date": &date})).await;
            let basic = dl.tushare_call("daily_basic", &json!({"trade_date": &date})).await;
            let adj = dl.tushare_call("adj_factor", &json!({"trade_date": &date})).await;
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

    /// Download financial table (per ts_code, concurrent).
    async fn download_financial_table(&self, api_name: &str, table: &str, unique_keys: &[&str], incremental: bool) -> usize {
        let ts_codes = self.get_all_ts_codes().await;

        let pending: Vec<String> = if incremental {
            let latest = self.get_ticker_latest(table, "end_date").await;
            let today = chrono::Local::now().format("%Y%m%d").to_string();
            ts_codes.into_iter().filter(|t| {
                match latest.get(t) {
                    Some(d) => d.as_str() < &today[..8], // stale if older than today
                    None => true, // no data yet
                }
            }).collect()
        } else {
            let done = self.get_done_tickers(table).await;
            ts_codes.into_iter().filter(|t| !done.contains(t)).collect()
        };

        let api_name = api_name.to_string();
        let table = table.to_string();
        let unique_keys: Vec<String> = unique_keys.iter().map(|s| s.to_string()).collect();
        let is_incremental = incremental;

        self.run_concurrent(pending, &format!("Tushare {api_name}"), move |dl, ts_code| {
            let api_name = api_name.clone();
            let table = table.clone();
            let unique_keys = unique_keys.clone();
            async move {
                let data = dl.tushare_call(&api_name, &json!({"ts_code": &ts_code})).await;
                let mut n = 0;
                if !data.is_empty() {
                    let uk_refs: Vec<&str> = unique_keys.iter().map(|s| s.as_str()).collect();
                    n = dl.upsert_rows(&table, &data, &uk_refs).await;
                }
                if !is_incremental {
                    dl.mark_done(&table, &ts_code).await;
                }
                n
            }
        }).await
    }

    pub async fn download_income(&self, incremental: bool) -> usize {
        self.download_financial_table("income", "a_financial_income",
            &["ts_code", "end_date", "report_type"], incremental).await
    }

    pub async fn download_balancesheet(&self, incremental: bool) -> usize {
        self.download_financial_table("balancesheet", "a_financial_balance",
            &["ts_code", "end_date", "report_type"], incremental).await
    }

    pub async fn download_cashflow(&self, incremental: bool) -> usize {
        self.download_financial_table("cashflow", "a_financial_cashflow",
            &["ts_code", "end_date", "report_type"], incremental).await
    }

    pub async fn download_fina_indicator(&self, incremental: bool) -> usize {
        self.download_financial_table("fina_indicator", "a_financial_indicator",
            &["ts_code", "end_date"], incremental).await
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
                    let index_name = idx.get("index_name").and_then(|v| v.as_str()).unwrap_or("");
                    let industry_name = idx.get("industry_name").and_then(|v| v.as_str()).unwrap_or(index_name);

                    let members = self.tushare_call("index_member", &json!({"index_code": &index_code})).await;
                    let rows: Vec<Value> = members.iter().filter_map(|m| {
                        let ts_code = m.get("con_code").or(m.get("ts_code")).and_then(|v| v.as_str())?;
                        if let Some(t) = &self.only_ticker { if ts_code != t { return None; } }
                        Some(json!({
                            "ts_code": ts_code, "index_code": &index_code,
                            "index_name": index_name, "industry_name": industry_name,
                            "src": src, "level": level,
                            "in_date": m.get("in_date").and_then(|v| v.as_str()).unwrap_or(""),
                            "out_date": m.get("out_date").and_then(|v| v.as_str()).unwrap_or(""),
                        }))
                    }).collect();
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
        total += self.download_trade_cal().await;
        total += self.download_industry().await;
        total += self.download_index_daily(start_date).await;
        total += self.download_macro().await;
        total += self.download_daily_prices(start_date, false).await;
        total += self.download_income(false).await;
        total += self.download_balancesheet(false).await;
        total += self.download_cashflow(false).await;
        total += self.download_fina_indicator(false).await;
        total += self.download_commodity(false).await;
        info!("Tushare download_all total: {total}");
        total
    }

    /// Incremental update all A-share data.
    pub async fn update_all(&self) -> usize {
        let mut total = 0;
        total += self.download_stock_list().await;
        total += self.download_trade_cal().await;
        total += self.download_industry().await;
        total += self.download_daily_prices("20200101", true).await;
        total += self.download_income(true).await;
        total += self.download_balancesheet(true).await;
        total += self.download_cashflow(true).await;
        total += self.download_fina_indicator(true).await;
        total += self.download_index_daily("20200101").await;
        total += self.download_macro().await;
        total += self.download_commodity(true).await;
        info!("Tushare update_all total: {total}");
        total
    }
}

// ═══════════════════════════════════════════════════════════════════════
// HELPERS
// ═══════════════════════════════════════════════════════════════════════

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
