//! FMP (Financial Modeling Prep) data downloader.
//!
//! Principle: API returns what we store — no field filtering.
//! Column names = camel_to_snake(API field), except symbol → ticker.

use std::collections::HashSet;

use chrono::NaiveDate;
use serde_json::Value;
use sqlx::PgPool;
use tracing::{debug, info, warn};

use crate::camel::snake_keys;
use crate::http::ApiClient;
use crate::progress::ticker_progress;

/// FMP downloader context.
pub struct FmpDownloader {
    pub api_key: String,
    pub client: ApiClient,
    pub pool: PgPool,
}

impl FmpDownloader {
    pub fn new(api_key: String, pool: PgPool, rate_limit: u32) -> Self {
        Self {
            api_key,
            client: ApiClient::new(rate_limit, 10),
            pool,
        }
    }

    // ── helpers ──────────────────────────────────────────────────────────

    async fn fmp_get(&self, path: &str, params: &[(&str, &str)]) -> Vec<Value> {
        let url = ApiClient::fmp_url(path, &self.api_key, params);
        match self.client.get_json(&url).await {
            Ok(Value::Array(arr)) => arr,
            Ok(other) => {
                // Some endpoints return a single object, wrap it
                if other.is_object() { vec![other] } else { vec![] }
            }
            Err(e) => {
                warn!("FMP {path}: {e}");
                vec![]
            }
        }
    }

    async fn fmp_get_v3(&self, path: &str, params: &[(&str, &str)]) -> Vec<Value> {
        let url = ApiClient::fmp_url_v3(path, &self.api_key, params);
        match self.client.get_json(&url).await {
            Ok(Value::Array(arr)) => arr,
            Ok(other) => {
                if other.is_object() { vec![other] } else { vec![] }
            }
            Err(e) => {
                warn!("FMP v3 {path}: {e}");
                vec![]
            }
        }
    }

    async fn get_active_tickers(&self) -> Vec<String> {
        let rows: Vec<(String,)> = sqlx::query_as(
            "SELECT ticker FROM us_stock_basic WHERE is_actively_trading = 1"
        )
        .fetch_all(&self.pool)
        .await
        .unwrap_or_default();
        rows.into_iter().map(|(t,)| t).collect()
    }

    async fn get_done_tickers(&self, table: &str) -> HashSet<String> {
        let rows: Vec<(String,)> = sqlx::query_as(
            "SELECT ticker FROM import_progress WHERE table_name = $1"
        )
        .bind(table)
        .fetch_all(&self.pool)
        .await
        .unwrap_or_default();
        rows.into_iter().map(|(t,)| t).collect()
    }

    async fn mark_done(&self, table: &str, ticker: &str) {
        sqlx::query(
            "INSERT INTO import_progress (table_name, ticker, completed_at) \
             VALUES ($1, $2, NOW()) \
             ON CONFLICT (table_name, ticker) DO UPDATE SET completed_at = NOW()"
        )
        .bind(table)
        .bind(ticker)
        .execute(&self.pool)
        .await
        .ok();
    }

    /// Generic upsert: INSERT rows from JSON into table, ON CONFLICT DO UPDATE.
    /// `unique_keys`: columns for the ON CONFLICT clause.
    /// `rows`: Vec of snake_case JSON objects.
    async fn upsert_rows(&self, table: &str, rows: &[Value], unique_keys: &[&str]) -> usize {
        if rows.is_empty() {
            return 0;
        }

        // Extract column names from first row
        let first = match rows[0].as_object() {
            Some(m) => m,
            None => return 0,
        };
        let columns: Vec<String> = first.keys().cloned().collect();
        if columns.is_empty() {
            return 0;
        }

        // Build batch INSERT ... ON CONFLICT DO UPDATE
        // Process in chunks of 500 to avoid parameter limits
        let chunk_size = 500;
        let mut total = 0usize;

        for chunk in rows.chunks(chunk_size) {
            let n_cols = columns.len();
            let mut param_idx = 1u32;
            let mut values_clauses = Vec::with_capacity(chunk.len());
            let mut params: Vec<String> = Vec::with_capacity(chunk.len() * n_cols);

            for row in chunk {
                let obj = match row.as_object() {
                    Some(m) => m,
                    None => continue,
                };
                let placeholders: Vec<String> = columns
                    .iter()
                    .map(|col| {
                        let p = format!("${param_idx}");
                        param_idx += 1;
                        let val = obj.get(col).cloned().unwrap_or(Value::Null);
                        params.push(json_to_sql_string(&val));
                        p
                    })
                    .collect();
                values_clauses.push(format!("({})", placeholders.join(", ")));
            }

            if values_clauses.is_empty() {
                continue;
            }

            let col_list = columns.join(", ");
            let conflict_cols = unique_keys.join(", ");
            let update_set: String = columns
                .iter()
                .filter(|c| !unique_keys.contains(&c.as_str()))
                .map(|c| format!("{c} = EXCLUDED.{c}"))
                .collect::<Vec<_>>()
                .join(", ");

            let sql = if update_set.is_empty() {
                format!(
                    "INSERT INTO {table} ({col_list}) VALUES {} ON CONFLICT ({conflict_cols}) DO NOTHING",
                    values_clauses.join(", ")
                )
            } else {
                format!(
                    "INSERT INTO {table} ({col_list}) VALUES {} ON CONFLICT ({conflict_cols}) DO UPDATE SET {update_set}",
                    values_clauses.join(", ")
                )
            };

            // Execute with text params
            let mut query = sqlx::query(&sql);
            for p in &params {
                query = query.bind(p);
            }

            match query.execute(&self.pool).await {
                Ok(result) => total += result.rows_affected() as usize,
                Err(e) => {
                    warn!("Upsert {table} failed: {e}");
                    debug!("SQL (first 500 chars): {}", &sql[..sql.len().min(500)]);
                }
            }
        }

        total
    }

    // ── download methods ────────────────────────────────────────────────

    /// Download stock list from FMP and upsert into us_stock_basic.
    pub async fn download_stock_list(&self) -> usize {
        info!("Downloading FMP stock list...");
        let data = self.fmp_get_v3("stock/list", &[]).await;
        if data.is_empty() {
            warn!("FMP stock list returned empty");
            return 0;
        }

        let rows: Vec<Value> = data.into_iter().map(|v| snake_keys(&v)).collect();
        let count = self.upsert_rows("us_stock_basic", &rows, &["ticker"]).await;
        info!("Stock list: {count} rows upserted");
        count
    }

    /// Download daily prices for all active tickers.
    ///
    /// Full mode: skip tickers already in import_progress.
    /// Incremental: fetch from last known date to today.
    pub async fn download_daily_prices(&self, start_year: i32, incremental: bool) -> usize {
        let tickers = self.get_active_tickers().await;
        if tickers.is_empty() {
            warn!("No active tickers for daily prices");
            return 0;
        }

        let today = chrono::Local::now().format("%Y-%m-%d").to_string();
        let done = self.get_done_tickers("us_daily_price").await;

        let pending: Vec<String> = if incremental {
            tickers // all tickers in incremental mode
        } else {
            tickers.into_iter().filter(|t| !done.contains(t)).collect()
        };

        if pending.is_empty() {
            info!("All tickers done for us_daily_price");
            return 0;
        }

        info!("Daily prices: {} tickers to process", pending.len());
        let pb = ticker_progress(pending.len() as u64, "FMP Daily Prices");
        let mut total = 0usize;

        for ticker in &pending {
            let count = self.download_daily_price_one(ticker, start_year, &today).await;
            total += count;
            self.mark_done("us_daily_price", ticker).await;
            pb.inc(1);
        }

        pb.finish_with_message(format!("{total} rows"));
        info!("FMP daily prices total: {total}");
        total
    }

    async fn download_daily_price_one(&self, ticker: &str, start_year: i32, today: &str) -> usize {
        let end_year: i32 = today[..4].parse().unwrap_or(2025);
        let mut count = 0;

        // Download in 10-year segments
        let mut yr = start_year;
        while yr <= end_year {
            let seg_end = (yr + 9).min(end_year);
            let from = format!("{yr}-01-01");
            let to = format!("{seg_end}-12-31");

            let data = self.fmp_get(
                "historical-price-eod/full",
                &[("symbol", ticker), ("from", &from), ("to", &to)],
            ).await;

            if !data.is_empty() {
                let rows: Vec<Value> = data.into_iter()
                    .map(|v| {
                        let mut obj = snake_keys(&v);
                        // Rename date → trade_date for unique key
                        if let Some(map) = obj.as_object_mut() {
                            if let Some(date_val) = map.remove("date") {
                                map.insert("trade_date".to_string(), date_val);
                            }
                        }
                        obj
                    })
                    .collect();

                count += self.upsert_rows("us_daily_price", &rows, &["ticker", "trade_date"]).await;
            }

            yr += 10;
        }

        count
    }

    /// Download financial statements (IS+BS+CF merged) for all tickers.
    pub async fn download_financials(&self) -> usize {
        let tickers = self.get_active_tickers().await;
        let done = self.get_done_tickers("us_financial_data").await;
        let pending: Vec<String> = tickers.into_iter().filter(|t| !done.contains(t)).collect();

        if pending.is_empty() {
            info!("All tickers done for us_financial_data");
            return 0;
        }

        info!("Financials: {} tickers to process", pending.len());
        let pb = ticker_progress(pending.len() as u64, "FMP Financials");
        let mut total = 0usize;

        for ticker in &pending {
            // Income Statement
            let is_data = self.fmp_get(
                "income-statement",
                &[("symbol", ticker), ("period", "quarter"), ("limit", "400")],
            ).await;
            // Balance Sheet
            let bs_data = self.fmp_get(
                "balance-sheet-statement",
                &[("symbol", ticker), ("period", "quarter"), ("limit", "400")],
            ).await;
            // Cash Flow
            let cf_data = self.fmp_get(
                "cash-flow-statement",
                &[("symbol", ticker), ("period", "quarter"), ("limit", "400")],
            ).await;

            // Merge IS+BS+CF by (ticker, date, period) — same approach as Python
            let merged = merge_financial_statements(&is_data, &bs_data, &cf_data);
            if !merged.is_empty() {
                total += self.upsert_rows("us_financial_data", &merged, &["ticker", "date", "period"]).await;
            }

            self.mark_done("us_financial_data", ticker).await;
            pb.inc(1);
        }

        pb.finish_with_message(format!("{total} rows"));
        info!("FMP financials total: {total}");
        total
    }

    /// Download index daily prices (S&P 500, etc).
    pub async fn download_index_daily(&self, start_year: i32) -> usize {
        let indices = ["^GSPC", "^DJI", "^IXIC", "^RUT"];
        let today = chrono::Local::now().format("%Y-%m-%d").to_string();
        let mut total = 0;

        for index in &indices {
            let end_year: i32 = today[..4].parse().unwrap_or(2025);
            let mut yr = start_year;
            while yr <= end_year {
                let seg_end = (yr + 9).min(end_year);
                let from = format!("{yr}-01-01");
                let to = format!("{seg_end}-12-31");

                let data = self.fmp_get(
                    "historical-price-eod/full",
                    &[("symbol", index), ("from", &from), ("to", &to)],
                ).await;

                if !data.is_empty() {
                    let rows: Vec<Value> = data.into_iter()
                        .map(|v| {
                            let mut obj = snake_keys(&v);
                            if let Some(map) = obj.as_object_mut() {
                                if let Some(date_val) = map.remove("date") {
                                    map.insert("trade_date".to_string(), date_val);
                                }
                                // symbol → index_code for this table
                                if let Some(sym) = map.remove("ticker") {
                                    map.insert("index_code".to_string(), sym);
                                }
                            }
                            obj
                        })
                        .collect();

                    total += self.upsert_rows("us_index_daily", &rows, &["index_code", "trade_date"]).await;
                }
                yr += 10;
            }
            info!("Index {index}: done");
        }

        info!("Index daily total: {total}");
        total
    }
}

// ── Financial statement merge helper ────────────────────────────────────

fn merge_financial_statements(
    is_data: &[Value],
    bs_data: &[Value],
    cf_data: &[Value],
) -> Vec<Value> {
    use std::collections::HashMap;

    // Index BS and CF by (date, period)
    let mut bs_map: HashMap<(String, String), &Value> = HashMap::new();
    for row in bs_data {
        if let (Some(d), Some(p)) = (
            row.get("date").and_then(|v| v.as_str()),
            row.get("period").and_then(|v| v.as_str()),
        ) {
            bs_map.insert((d.to_string(), p.to_string()), row);
        }
    }
    let mut cf_map: HashMap<(String, String), &Value> = HashMap::new();
    for row in cf_data {
        if let (Some(d), Some(p)) = (
            row.get("date").and_then(|v| v.as_str()),
            row.get("period").and_then(|v| v.as_str()),
        ) {
            cf_map.insert((d.to_string(), p.to_string()), row);
        }
    }

    // Merge: IS as base, overlay BS and CF fields
    let mut result = Vec::new();
    for is_row in is_data {
        let date = is_row.get("date").and_then(|v| v.as_str()).unwrap_or("");
        let period = is_row.get("period").and_then(|v| v.as_str()).unwrap_or("");
        if date.is_empty() || period.is_empty() {
            continue;
        }
        let key = (date.to_string(), period.to_string());

        let mut merged = snake_keys(is_row);
        if let Some(map) = merged.as_object_mut() {
            // Merge BS fields
            if let Some(bs) = bs_map.get(&key) {
                let bs_snake = snake_keys(bs);
                if let Some(bs_obj) = bs_snake.as_object() {
                    for (k, v) in bs_obj {
                        if !map.contains_key(k) {
                            map.insert(k.clone(), v.clone());
                        }
                    }
                }
            }
            // Merge CF fields
            if let Some(cf) = cf_map.get(&key) {
                let cf_snake = snake_keys(cf);
                if let Some(cf_obj) = cf_snake.as_object() {
                    for (k, v) in cf_obj {
                        if !map.contains_key(k) {
                            map.insert(k.clone(), v.clone());
                        }
                    }
                }
            }
        }

        result.push(merged);
    }

    result
}

/// Convert a JSON value to a SQL-safe string for parameterized queries.
fn json_to_sql_string(val: &Value) -> String {
    match val {
        Value::Null => String::new(), // will bind as empty string
        Value::Bool(b) => b.to_string(),
        Value::Number(n) => n.to_string(),
        Value::String(s) => s.clone(),
        _ => val.to_string(),
    }
}
