//! Tushare Pro API downloader for A-share data.
//!
//! Tushare API: POST https://api.tushare.pro with JSON body.
//! Rate limit: configurable (default 900 points/min).
//! Convention: no `fields=` parameter — keep all fields returned by API.

use std::collections::HashSet;

use serde_json::{json, Value};
use sqlx::PgPool;
use tracing::{debug, info, warn};

use crate::http::ApiClient;
use crate::progress::ticker_progress;

pub struct TushareDownloader {
    pub token: String,
    pub client: ApiClient,
    pub pool: PgPool,
}

impl TushareDownloader {
    pub fn new(token: String, pool: PgPool, rate_limit: u32) -> Self {
        Self {
            token,
            client: ApiClient::new(rate_limit, 5),
            pool,
        }
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

        // Rate limit
        let resp = match self.client.post_json(url, &body).await {
            Ok(v) => v,
            Err(e) => {
                warn!("Tushare {api_name}: {e}");
                return vec![];
            }
        };

        // Parse Tushare response format: { "data": { "fields": [...], "items": [[...], ...] } }
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

        // Convert rows to JSON objects
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
        sqlx::query_scalar::<_, String>(
            "SELECT ts_code FROM a_stock_basic WHERE list_status = 'L'"
        ).fetch_all(&self.pool).await.unwrap_or_default()
    }

    async fn upsert_rows(&self, table: &str, rows: &[Value], unique_keys: &[&str]) -> usize {
        if rows.is_empty() { return 0; }
        let first = match rows[0].as_object() { Some(m) => m, None => return 0 };
        let columns: Vec<String> = first.keys().cloned().collect();
        if columns.is_empty() { return 0; }

        let chunk_size = 500;
        let mut total = 0usize;

        for chunk in rows.chunks(chunk_size) {
            let mut param_idx = 1u32;
            let mut values_clauses = Vec::new();
            let mut params: Vec<String> = Vec::new();

            for row in chunk {
                let obj = match row.as_object() { Some(m) => m, None => continue };
                let placeholders: Vec<String> = columns.iter().map(|col| {
                    let p = format!("${param_idx}");
                    param_idx += 1;
                    let val = obj.get(col).unwrap_or(&Value::Null);
                    params.push(json_to_sql(val));
                    p
                }).collect();
                values_clauses.push(format!("({})", placeholders.join(", ")));
            }
            if values_clauses.is_empty() { continue; }

            let col_list = columns.join(", ");
            let conflict_cols = unique_keys.join(", ");
            let update_set: String = columns.iter()
                .filter(|c| !unique_keys.contains(&c.as_str()))
                .map(|c| format!("{c} = EXCLUDED.{c}"))
                .collect::<Vec<_>>().join(", ");

            let sql = if update_set.is_empty() {
                format!("INSERT INTO {table} ({col_list}) VALUES {} ON CONFLICT ({conflict_cols}) DO NOTHING",
                    values_clauses.join(", "))
            } else {
                format!("INSERT INTO {table} ({col_list}) VALUES {} ON CONFLICT ({conflict_cols}) DO UPDATE SET {update_set}",
                    values_clauses.join(", "))
            };

            let mut query = sqlx::query(&sql);
            for p in &params { query = query.bind(p); }
            match query.execute(&self.pool).await {
                Ok(r) => total += r.rows_affected() as usize,
                Err(e) => warn!("Upsert {table} failed: {e}"),
            }
        }
        total
    }

    // ═══════════════════════════════════════════════════════════════════
    // DOWNLOAD METHODS
    // ═══════════════════════════════════════════════════════════════════

    /// Download A-share stock list.
    pub async fn download_stock_list(&self) -> usize {
        info!("Downloading Tushare stock list...");
        let mut total = 0;
        for status in ["L", "D", "P"] {
            let data = self.tushare_call("stock_basic", &json!({
                "list_status": status,
            })).await;
            if !data.is_empty() {
                total += self.upsert_rows("a_stock_basic", &data, &["ts_code"]).await;
            }
        }
        info!("Stock list: {total} rows");
        total
    }

    /// Download trading calendar.
    pub async fn download_trade_cal(&self) -> usize {
        let mut total = 0;
        for exchange in ["SSE", "SZSE"] {
            let data = self.tushare_call("trade_cal", &json!({
                "exchange": exchange,
                "start_date": "20000101",
                "end_date": "20261231",
            })).await;
            if !data.is_empty() {
                total += self.upsert_rows("a_trade_cal", &data, &["exchange", "cal_date"]).await;
            }
        }
        info!("Trade calendar: {total} rows");
        total
    }

    /// Download daily prices (per trade_date, full market).
    /// Merges: daily + daily_basic + adj_factor.
    pub async fn download_daily_prices(&self, start_date: &str) -> usize {
        info!("Downloading A-share daily prices from {start_date}...");

        // Get trading days
        let cal_data = self.tushare_call("trade_cal", &json!({
            "exchange": "SSE",
            "start_date": start_date,
            "end_date": "20261231",
            "is_open": 1,
        })).await;

        let trade_dates: Vec<String> = cal_data.iter().filter_map(|v| {
            v.get("cal_date").and_then(|d| d.as_str()).map(|s| s.to_string())
        }).collect();

        let done = self.get_done_tickers("a_daily_price").await;
        let pending: Vec<_> = trade_dates.iter()
            .filter(|d| !done.contains(d.as_str()))
            .cloned().collect();

        if pending.is_empty() {
            info!("All trade dates done for a_daily_price");
            return 0;
        }

        info!("Daily prices: {} trade dates to process", pending.len());
        let pb = ticker_progress(pending.len() as u64, "A-Share Daily");
        let mut total = 0;

        for date in &pending {
            // Fetch daily OHLC
            let daily = self.tushare_call("daily", &json!({"trade_date": date})).await;
            // Fetch daily basic (valuation)
            let basic = self.tushare_call("daily_basic", &json!({"trade_date": date})).await;
            // Fetch adj factor
            let adj = self.tushare_call("adj_factor", &json!({"trade_date": date})).await;

            // Merge by ts_code
            let merged = merge_daily(&daily, &basic, &adj);
            if !merged.is_empty() {
                total += self.upsert_rows("a_daily_price", &merged, &["ts_code", "trade_date"]).await;
            }
            self.mark_done("a_daily_price", date).await;
            pb.inc(1);
        }

        pb.finish_with_message(format!("{total} rows"));
        total
    }

    /// Download income statements (per ts_code).
    pub async fn download_income(&self) -> usize {
        self.download_financial_table("income", "a_financial_income",
            &["ts_code", "end_date", "report_type"]).await
    }

    /// Download balance sheets.
    pub async fn download_balancesheet(&self) -> usize {
        self.download_financial_table("balancesheet", "a_financial_balance",
            &["ts_code", "end_date", "report_type"]).await
    }

    /// Download cash flow statements.
    pub async fn download_cashflow(&self) -> usize {
        self.download_financial_table("cashflow", "a_financial_cashflow",
            &["ts_code", "end_date", "report_type"]).await
    }

    /// Download financial indicators.
    pub async fn download_fina_indicator(&self) -> usize {
        self.download_financial_table("fina_indicator", "a_financial_indicator",
            &["ts_code", "end_date"]).await
    }

    /// Generic per-ticker financial table download.
    async fn download_financial_table(&self, api_name: &str, table: &str, unique_keys: &[&str]) -> usize {
        let ts_codes = self.get_all_ts_codes().await;
        let done = self.get_done_tickers(table).await;
        let pending: Vec<_> = ts_codes.into_iter().filter(|t| !done.contains(t)).collect();
        if pending.is_empty() { info!("{table}: all done"); return 0; }

        info!("{table}: {} ts_codes to process", pending.len());
        let pb = ticker_progress(pending.len() as u64, &format!("Tushare {api_name}"));
        let mut total = 0;

        for ts_code in &pending {
            let data = self.tushare_call(api_name, &json!({"ts_code": ts_code})).await;
            if !data.is_empty() {
                total += self.upsert_rows(table, &data, unique_keys).await;
            }
            self.mark_done(table, ts_code).await;
            pb.inc(1);
        }

        pb.finish_with_message(format!("{total} rows"));
        total
    }

    /// Download industry classification (Shenwan).
    pub async fn download_industry(&self) -> usize {
        info!("Downloading Shenwan industry classification...");
        let mut total = 0;

        for src in ["SW2021", "SW2014"] {
            for level in ["L1", "L2"] {
                let indices = self.tushare_call("index_classify", &json!({
                    "level": level, "src": src,
                })).await;

                for idx in &indices {
                    let index_code = match idx.get("index_code").and_then(|v| v.as_str()) {
                        Some(c) => c.to_string(),
                        None => continue,
                    };
                    let index_name = idx.get("index_name").and_then(|v| v.as_str()).unwrap_or("");
                    let industry_name = idx.get("industry_name").and_then(|v| v.as_str()).unwrap_or(index_name);

                    let members = self.tushare_call("index_member", &json!({
                        "index_code": &index_code,
                    })).await;

                    let rows: Vec<Value> = members.iter().filter_map(|m| {
                        let ts_code = m.get("con_code").or(m.get("ts_code")).and_then(|v| v.as_str())?;
                        let in_date = m.get("in_date").and_then(|v| v.as_str()).unwrap_or("");
                        Some(json!({
                            "ts_code": ts_code,
                            "index_code": &index_code,
                            "index_name": index_name,
                            "industry_name": industry_name,
                            "src": src,
                            "level": level,
                            "in_date": in_date,
                            "out_date": m.get("out_date").and_then(|v| v.as_str()).unwrap_or(""),
                        }))
                    }).collect();

                    if !rows.is_empty() {
                        total += self.upsert_rows("a_industry_class", &rows,
                            &["ts_code", "src", "level", "index_code", "in_date"]).await;
                    }
                }
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
            let data = self.tushare_call("index_daily", &json!({
                "ts_code": code,
                "start_date": start_date,
            })).await;
            if !data.is_empty() {
                total += self.upsert_rows("a_index_daily", &data, &["ts_code", "trade_date"]).await;
            }
            info!("Index {name} ({code}): done");
        }
        total
    }

    /// Download macro indicators.
    pub async fn download_macro(&self) -> usize {
        info!("Downloading A-share macro indicators...");
        let mut total = 0;

        // SHIBOR
        let data = self.tushare_call("shibor", &json!({"start_date": "20060101"})).await;
        for row in &data {
            let date = match row.get("date").and_then(|v| v.as_str()) { Some(d) => d, None => continue };
            for (field, code) in [("on", "SHIBOR_ON"), ("1w", "SHIBOR_1W"), ("1m", "SHIBOR_1M"), ("3m", "SHIBOR_3M")] {
                if let Some(val) = row.get(field).and_then(|v| v.as_f64()) {
                    let r = json!({"indicator": code, "report_date": date, "freq": "D", "value": val});
                    total += self.upsert_rows("a_macro_indicator", &[r], &["indicator", "report_date", "freq"]).await;
                }
            }
        }

        // CPI
        let data = self.tushare_call("cn_cpi", &json!({"start_m": "200001"})).await;
        for row in &data {
            let month = match row.get("month").and_then(|v| v.as_str()) { Some(m) => m, None => continue };
            let date = format!("{}-01", month.replace('.', "-"));
            if let Some(val) = row.get("nt_yoy").and_then(|v| v.as_f64()) {
                let r = json!({"indicator": "CPI_YOY", "report_date": date, "freq": "M", "value": val});
                total += self.upsert_rows("a_macro_indicator", &[r], &["indicator", "report_date", "freq"]).await;
            }
        }

        // PPI
        let data = self.tushare_call("cn_ppi", &json!({"start_m": "200001"})).await;
        for row in &data {
            let month = match row.get("month").and_then(|v| v.as_str()) { Some(m) => m, None => continue };
            let date = format!("{}-01", month.replace('.', "-"));
            if let Some(val) = row.get("ppi_yoy").and_then(|v| v.as_f64()) {
                let r = json!({"indicator": "PPI_YOY", "report_date": date, "freq": "M", "value": val});
                total += self.upsert_rows("a_macro_indicator", &[r], &["indicator", "report_date", "freq"]).await;
            }
        }

        info!("Macro indicators: {total} rows");
        total
    }

    /// Download all A-share data.
    pub async fn download_all(&self, start_date: &str) -> usize {
        let mut total = 0;
        total += self.download_stock_list().await;
        total += self.download_trade_cal().await;
        total += self.download_industry().await;
        total += self.download_index_daily(start_date).await;
        total += self.download_macro().await;
        total += self.download_daily_prices(start_date).await;
        total += self.download_income().await;
        total += self.download_balancesheet().await;
        total += self.download_cashflow().await;
        total += self.download_fina_indicator().await;
        info!("Tushare download_all total: {total}");
        total
    }
}

/// Merge daily + daily_basic + adj_factor by ts_code.
fn merge_daily(daily: &[Value], basic: &[Value], adj: &[Value]) -> Vec<Value> {
    use std::collections::HashMap;

    let mut basic_map: HashMap<&str, &Value> = HashMap::new();
    for row in basic {
        if let Some(code) = row.get("ts_code").and_then(|v| v.as_str()) {
            basic_map.insert(code, row);
        }
    }
    let mut adj_map: HashMap<&str, &Value> = HashMap::new();
    for row in adj {
        if let Some(code) = row.get("ts_code").and_then(|v| v.as_str()) {
            adj_map.insert(code, row);
        }
    }

    daily.iter().filter_map(|row| {
        let code = row.get("ts_code").and_then(|v| v.as_str())?;
        let mut merged = row.as_object()?.clone();

        // Overlay basic fields (skip ts_code/trade_date duplicates)
        if let Some(b) = basic_map.get(code) {
            if let Some(b_obj) = b.as_object() {
                for (k, v) in b_obj {
                    if k != "ts_code" && k != "trade_date" && !merged.contains_key(k) {
                        merged.insert(k.clone(), v.clone());
                    }
                }
            }
        }

        // Overlay adj_factor
        if let Some(a) = adj_map.get(code) {
            if let Some(a_obj) = a.as_object() {
                for (k, v) in a_obj {
                    if k != "ts_code" && k != "trade_date" && !merged.contains_key(k) {
                        merged.insert(k.clone(), v.clone());
                    }
                }
            }
        }

        Some(Value::Object(merged))
    }).collect()
}

fn json_to_sql(val: &Value) -> String {
    match val {
        Value::Null => String::new(),
        Value::Bool(b) => b.to_string(),
        Value::Number(n) => n.to_string(),
        Value::String(s) => s.clone(),
        _ => val.to_string(),
    }
}
