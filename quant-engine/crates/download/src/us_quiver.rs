//! Quiver Quantitative data downloader.
//!
//! Endpoints: lobbying, gov_contracts, dark_pool, institutional_holders.
//! API: https://api.quiverquant.com/beta/

use std::collections::HashSet;

use serde_json::Value;
use sqlx::PgPool;
use tracing::{info, warn};

use crate::http::ApiClient;
use crate::progress::ticker_progress;

pub struct QuiverDownloader {
    pub api_key: String,
    pub client: ApiClient,
    pub pool: PgPool,
}

impl QuiverDownloader {
    pub fn new(api_key: String, pool: PgPool, rate_limit: u32) -> Self {
        Self {
            api_key,
            client: ApiClient::new(rate_limit, 5),
            pool,
        }
    }

    async fn quiver_get(&self, path: &str) -> Vec<Value> {
        let url = format!(
            "https://api.quiverquant.com/beta/{path}",
        );
        // Quiver uses Authorization header
        let full_url = format!("{url}?token={}", self.api_key);
        match self.client.get_json(&full_url).await {
            Ok(Value::Array(arr)) => arr,
            Ok(_) => vec![],
            Err(e) => { warn!("Quiver {path}: {e}"); vec![] }
        }
    }

    async fn get_stocks_only_tickers(&self) -> Vec<String> {
        sqlx::query_scalar::<_, String>(
            "SELECT ticker FROM us_stock_basic WHERE is_actively_trading = 1 AND is_etf = 0 AND is_fund = 0"
        ).fetch_all(&self.pool).await.unwrap_or_default()
    }

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

    async fn upsert_rows(&self, table: &str, rows: &[Value], unique_keys: &[&str]) -> usize {
        if rows.is_empty() { return 0; }
        let first = match rows[0].as_object() { Some(m) => m, None => return 0 };
        let columns: Vec<String> = first.keys().cloned().collect();
        if columns.is_empty() { return 0; }

        let chunk_size = 200;
        let mut total = 0usize;

        let col_list = columns.join(", ");
        let conflict_cols = unique_keys.join(", ");
        let update_set: String = columns.iter()
            .filter(|c| !unique_keys.contains(&c.as_str()))
            .map(|c| format!("{c} = EXCLUDED.{c}"))
            .collect::<Vec<_>>().join(", ");

        for chunk in rows.chunks(chunk_size) {
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
                format!("INSERT INTO {table} ({col_list}) VALUES {} ON CONFLICT ({conflict_cols}) DO NOTHING",
                    values_clauses.join(","))
            } else {
                format!("INSERT INTO {table} ({col_list}) VALUES {} ON CONFLICT ({conflict_cols}) DO UPDATE SET {update_set}",
                    values_clauses.join(","))
            };

            match sqlx::query(&sql).execute(&self.pool).await {
                Ok(r) => total += r.rows_affected() as usize,
                Err(e) => tracing::error!("Upsert {table} failed: {e}"),
            }
        }
        total
    }

    // ── Download methods ────────────────────────────────────────────────

    /// Lobbying records per ticker.
    pub async fn download_lobbying(&self) -> usize {
        let tickers = self.get_stocks_only_tickers().await;
        let done = self.get_done_tickers("us_lobbying").await;
        let pending: Vec<_> = tickers.into_iter().filter(|t| !done.contains(t)).collect();
        if pending.is_empty() { return 0; }

        let pb = ticker_progress(pending.len() as u64, "Quiver Lobbying");
        let mut total = 0;
        for ticker in &pending {
            let data = self.quiver_get(&format!("historical/lobbying/{ticker}")).await;
            if !data.is_empty() {
                let rows: Vec<Value> = data.into_iter().filter_map(|v| {
                    let obj = v.as_object()?;
                    let date = obj.get("Date").and_then(|v| v.as_str())?;
                    Some(serde_json::json!({
                        "ticker": ticker,
                        "date": date,
                        "amount": obj.get("Amount").and_then(|v| v.as_f64()).unwrap_or(0.0),
                        "registrant": obj.get("Registrant").and_then(|v| v.as_str()).unwrap_or(""),
                        "client": obj.get("Client").and_then(|v| v.as_str()).unwrap_or(""),
                        "issue": obj.get("Issue").and_then(|v| v.as_str()).unwrap_or(""),
                        "specific_issue": obj.get("Specific_Issue").and_then(|v| v.as_str()).unwrap_or(""),
                    }))
                }).collect();
                total += self.upsert_rows("us_lobbying", &rows, &["ticker", "date", "registrant", "client"]).await;
            }
            self.mark_done("us_lobbying", ticker).await;
            pb.inc(1);
        }
        pb.finish_with_message(format!("{total} rows"));
        total
    }

    /// Government contracts per ticker (quarterly).
    pub async fn download_gov_contracts(&self) -> usize {
        let tickers = self.get_stocks_only_tickers().await;
        let done = self.get_done_tickers("us_gov_contract").await;
        let pending: Vec<_> = tickers.into_iter().filter(|t| !done.contains(t)).collect();
        if pending.is_empty() { return 0; }

        let pb = ticker_progress(pending.len() as u64, "Quiver GovContracts");
        let mut total = 0;
        for ticker in &pending {
            let data = self.quiver_get(&format!("historical/govcontracts/{ticker}")).await;
            if !data.is_empty() {
                let rows: Vec<Value> = data.into_iter().filter_map(|v| {
                    let obj = v.as_object()?;
                    let year = obj.get("Year").and_then(|v| v.as_i64())?;
                    let quarter = obj.get("Qtr").and_then(|v| v.as_i64())?;
                    Some(serde_json::json!({
                        "ticker": ticker,
                        "year": year,
                        "quarter": quarter,
                        "amount": obj.get("Amount").and_then(|v| v.as_f64()).unwrap_or(0.0),
                    }))
                }).collect();
                total += self.upsert_rows("us_gov_contract", &rows, &["ticker", "year", "quarter"]).await;
            }
            self.mark_done("us_gov_contract", ticker).await;
            pb.inc(1);
        }
        pb.finish_with_message(format!("{total} rows"));
        total
    }

    /// Dark pool / off-exchange volume per ticker (daily).
    pub async fn download_dark_pool(&self) -> usize {
        let tickers = self.get_stocks_only_tickers().await;
        let done = self.get_done_tickers("us_dark_pool_volume").await;
        let pending: Vec<_> = tickers.into_iter().filter(|t| !done.contains(t)).collect();
        if pending.is_empty() { return 0; }

        let pb = ticker_progress(pending.len() as u64, "Quiver Dark Pool");
        let mut total = 0;
        for ticker in &pending {
            let data = self.quiver_get(&format!("historical/offexchange/{ticker}")).await;
            if !data.is_empty() {
                let rows: Vec<Value> = data.into_iter().filter_map(|v| {
                    let obj = v.as_object()?;
                    let date = obj.get("Date").and_then(|v| v.as_str())?;
                    Some(serde_json::json!({
                        "ticker": ticker,
                        "date": date,
                        "short_volume": obj.get("OTC_Short").and_then(|v| v.as_f64()).unwrap_or(0.0),
                        "total_volume": obj.get("OTC_Total").and_then(|v| v.as_f64()).unwrap_or(0.0),
                        "dpi": obj.get("DPI").and_then(|v| v.as_f64()).unwrap_or(0.0),
                    }))
                }).collect();
                total += self.upsert_rows("us_dark_pool_volume", &rows, &["ticker", "date"]).await;
            }
            self.mark_done("us_dark_pool_volume", ticker).await;
            pb.inc(1);
        }
        pb.finish_with_message(format!("{total} rows"));
        total
    }

    /// 13F institutional holdings per ticker (aggregated by quarter).
    pub async fn download_institutional_holders(&self) -> usize {
        let tickers = self.get_stocks_only_tickers().await;
        let done = self.get_done_tickers("us_institutional_holder").await;
        let pending: Vec<_> = tickers.into_iter().filter(|t| !done.contains(t)).collect();
        if pending.is_empty() { return 0; }

        let pb = ticker_progress(pending.len() as u64, "Quiver 13F");
        let mut total = 0;
        for ticker in &pending {
            let data = self.quiver_get(&format!("live/sec13fchanges?ticker={ticker}")).await;
            if !data.is_empty() {
                // Aggregate by ReportPeriod (quarter)
                let rows = aggregate_13f(ticker, &data);
                if !rows.is_empty() {
                    total += self.upsert_rows("us_institutional_holder", &rows, &["ticker", "date"]).await;
                }
            }
            self.mark_done("us_institutional_holder", ticker).await;
            pb.inc(1);
        }
        pb.finish_with_message(format!("{total} rows"));
        total
    }

    /// Download all Quiver data.
    pub async fn download_all(&self) -> usize {
        let mut total = 0;
        total += self.download_lobbying().await;
        total += self.download_gov_contracts().await;
        total += self.download_dark_pool().await;
        total += self.download_institutional_holders().await;
        info!("Quiver download_all total: {total}");
        total
    }
}

/// Aggregate 13F per-fund detail into per-quarter summary.
fn aggregate_13f(ticker: &str, data: &[Value]) -> Vec<Value> {
    use std::collections::BTreeMap;

    let mut quarters: BTreeMap<String, (usize, f64, f64, usize, usize, usize)> = BTreeMap::new();

    for item in data {
        let obj = match item.as_object() { Some(m) => m, None => continue };
        let period = match obj.get("ReportPeriod").and_then(|v| v.as_str()) {
            Some(p) => p.to_string(),
            None => continue,
        };
        let held = obj.get("Held").and_then(|v| v.as_f64()).unwrap_or(0.0);
        let close = obj.get("Close").and_then(|v| v.as_f64()).unwrap_or(0.0);
        let change = obj.get("Change_Share").and_then(|v| v.as_f64()).unwrap_or(0.0);

        let entry = quarters.entry(period).or_insert((0, 0.0, 0.0, 0, 0, 0));
        entry.0 += 1; // n_funds
        entry.1 += held; // total_shares
        entry.2 += held * close; // total_value
        if change > 0.0 { entry.3 += 1; } // increased
        if change < 0.0 { entry.4 += 1; } // reduced
        if held == 0.0 { entry.5 += 1; } // closed
    }

    quarters.into_iter().map(|(date, (n_funds, shares, value, inc, red, closed))| {
        serde_json::json!({
            "ticker": ticker,
            "date": date,
            "investors_holding": n_funds,
            "number_of_13f_shares": shares,
            "total_invested": value,
            "increased_positions": inc,
            "reduced_positions": red,
            "closed_positions": closed,
        })
    }).collect()
}

fn to_sql_literal(val: &Value) -> String {
    match val {
        Value::Null => "NULL".to_string(),
        Value::Bool(b) => if *b { "1".to_string() } else { "0".to_string() },
        Value::Number(n) => n.to_string(),
        Value::String(s) => format!("'{}'", s.replace('\'', "''")),
        _ => format!("'{}'", val.to_string().replace('\'', "''")),
    }
}
