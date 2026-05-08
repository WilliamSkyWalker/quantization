//! FMP (Financial Modeling Prep) data downloader.
//!
//! Principle: API returns what we store — no field filtering.
//! Column names = camel_to_snake(API field), except symbol → ticker.

use std::collections::HashSet;
use std::sync::Arc;
use std::sync::atomic::{AtomicUsize, Ordering};

use serde_json::Value;
use sqlx::PgPool;
use tokio::sync::Semaphore;
use tracing::{error, info, warn};

use crate::camel::snake_keys;
use crate::http::ApiClient;
use crate::progress::ticker_progress;

const MAX_CONCURRENT: usize = 5;

/// FMP downloader context.
#[derive(Clone)]
pub struct FmpDownloader {
    pub api_key: String,
    pub client: ApiClient,
    pub pool: PgPool,
    /// If set, only process this single ticker (for testing).
    pub ticker_filter: Option<String>,
}

impl FmpDownloader {
    pub fn new(api_key: String, pool: PgPool, rate_limit: u32) -> Self {
        Self {
            api_key,
            client: ApiClient::new(rate_limit, 10),
            pool,
            ticker_filter: None,
        }
    }

    pub fn with_ticker(mut self, ticker: Option<&str>) -> Self {
        self.ticker_filter = ticker.map(|s| s.to_string());
        self
    }

    // ── HTTP helpers ────────────────────────────────────────────────────

    async fn fmp_get(&self, path: &str, params: &[(&str, &str)]) -> Vec<Value> {
        let url = ApiClient::fmp_url(path, &self.api_key, params);
        match self.client.get_json(&url).await {
            Ok(Value::Array(arr)) => arr,
            Ok(other) => if other.is_object() { vec![other] } else { vec![] },
            Err(e) => { warn!("FMP {path}: {e}"); vec![] }
        }
    }

    /// FMP versioned API: /api/v3/{path} (Python: _fmp_get_json with version="v3")
    async fn fmp_get_v3(&self, path: &str, params: &[(&str, &str)]) -> Vec<Value> {
        let url = ApiClient::fmp_url_v3(path, &self.api_key, params);
        match self.client.get_json(&url).await {
            Ok(Value::Array(arr)) => arr,
            Ok(other) => if other.is_object() { vec![other] } else { vec![] },
            Err(e) => { warn!("FMP v3 {path}: {e}"); vec![] }
        }
    }

    /// FMP v4 API: /api/v4/{path} (Python: _fmp_get_json with version="v4")
    async fn fmp_get_v4(&self, path: &str, params: &[(&str, &str)]) -> Vec<Value> {
        let url = ApiClient::fmp_url_v4(path, &self.api_key, params);
        match self.client.get_json(&url).await {
            Ok(Value::Array(arr)) => arr,
            Ok(other) => if other.is_object() { vec![other] } else { vec![] },
            Err(e) => { warn!("FMP v4 {path}: {e}"); vec![] }
        }
    }

    // ── DB helpers ──────────────────────────────────────────────────────

    async fn get_active_tickers(&self) -> Vec<String> {
        if let Some(ref t) = self.ticker_filter {
            return vec![t.clone()];
        }
        sqlx::query_scalar::<_, String>(
            "SELECT ticker FROM us_stock_basic WHERE is_actively_trading = 1"
        ).fetch_all(&self.pool).await.unwrap_or_default()
    }

    async fn get_stocks_only_tickers(&self) -> Vec<String> {
        if let Some(ref t) = self.ticker_filter {
            return vec![t.clone()];
        }
        sqlx::query_scalar::<_, String>(
            "SELECT ticker FROM us_stock_basic WHERE is_actively_trading = 1 AND is_etf = 0 AND is_fund = 0"
        ).fetch_all(&self.pool).await.unwrap_or_default()
    }

    async fn get_done_tickers(&self, table: &str) -> HashSet<String> {
        sqlx::query_scalar::<_, String>(
            "SELECT ticker FROM import_progress WHERE table_name = $1"
        ).bind(table).fetch_all(&self.pool).await.unwrap_or_default().into_iter().collect()
    }

    /// Get actual column names from DB for a table (cached).
    async fn get_table_columns(&self, table: &str) -> HashSet<String> {
        let sql = format!(
            "SELECT column_name FROM information_schema.columns WHERE table_schema = current_schema() AND table_name = '{table}'"
        );
        let rows: Vec<(String,)> = sqlx::query_as(&sql)
            .fetch_all(&self.pool).await.unwrap_or_default();
        rows.into_iter().map(|(c,)| c).collect()
    }

    /// Run a per-ticker async task concurrently (15 workers).
    /// `task_fn` takes (downloader, ticker) and returns row count.
    async fn run_concurrent<F, Fut>(
        &self,
        tickers: Vec<String>,
        label: &str,
        task_fn: F,
    ) -> usize
    where
        F: Fn(FmpDownloader, String) -> Fut + Send + Sync + 'static,
        Fut: std::future::Future<Output = usize> + Send + 'static,
    {
        if tickers.is_empty() { return 0; }
        info!("{label}: {} tickers ({MAX_CONCURRENT} concurrent)", tickers.len());
        let pb = Arc::new(ticker_progress(tickers.len() as u64, label));
        let total = Arc::new(AtomicUsize::new(0));
        let sem = Arc::new(Semaphore::new(MAX_CONCURRENT));
        let task_fn = Arc::new(task_fn);

        let mut handles = Vec::with_capacity(tickers.len());
        for ticker in tickers {
            let dl = self.clone();
            let sem = sem.clone();
            let total = total.clone();
            let pb = pb.clone();
            let task_fn = task_fn.clone();
            handles.push(tokio::spawn(async move {
                let _permit = sem.acquire().await.unwrap();
                let n = task_fn(dl, ticker).await;
                total.fetch_add(n, Ordering::Relaxed);
                pb.inc(1);
            }));
        }
        for h in handles { h.await.ok(); }
        let t = total.load(Ordering::Relaxed);
        pb.finish_with_message(format!("{t} rows"));
        t
    }

    async fn mark_done(&self, table: &str, ticker: &str) {
        sqlx::query(
            "INSERT INTO import_progress (table_name, ticker, completed_at) \
             VALUES ($1, $2, NOW()) ON CONFLICT (table_name, ticker) DO UPDATE SET completed_at = NOW()"
        ).bind(table).bind(ticker).execute(&self.pool).await.ok();
    }

    /// Get the latest date per ticker in a table (for incremental updates).
    /// Returns {ticker: "YYYY-MM-DD"}.
    async fn get_ticker_latest(&self, table: &str, date_field: &str) -> std::collections::HashMap<String, String> {
        let sql = format!(
            "SELECT ticker, MAX({date_field})::text as latest FROM {table} GROUP BY ticker"
        );
        let rows: Vec<(String, Option<String>)> = sqlx::query_as(&sql)
            .fetch_all(&self.pool).await.unwrap_or_default();
        rows.into_iter()
            .filter_map(|(t, d)| d.map(|d| (t, d[..10].to_string())))
            .collect()
    }

    /// Generic batch upsert: INSERT with inline SQL literals for correct type inference.
    /// Filters out columns not present in the DB table (API may return extra fields).
    async fn upsert_rows(&self, table: &str, rows: &[Value], unique_keys: &[&str]) -> usize {
        if rows.is_empty() { return 0; }

        let first = match rows[0].as_object() {
            Some(m) => m,
            None => return 0,
        };

        // Filter columns: skip id (serial PK) + created_at/updated_at (DB trigger
        // quant.set_updated_at 自动维护) + 任何 DB 不认识的字段
        let db_columns = self.get_table_columns(table).await;
        let columns: Vec<String> = first.keys()
            .filter(|k| {
                let k = k.as_str();
                k != "id" && k != "updated_at" && k != "created_at"
                    && (db_columns.is_empty() || db_columns.contains(k))
            })
            .cloned()
            .collect();
        if columns.is_empty() { return 0; }

        let col_list = columns.join(", ");
        let conflict_cols = unique_keys.join(", ");
        let update_set: String = columns.iter()
            .filter(|c| !unique_keys.contains(&c.as_str()))
            .map(|c| format!("{c} = EXCLUDED.{c}"))
            .collect::<Vec<_>>().join(", ");

        let chunk_size = 200;
        let mut total = 0usize;
        let mut error_count = 0usize;

        // Deduplicate rows by unique key to avoid "cannot affect row a second time"
        let mut seen = HashSet::new();
        let deduped: Vec<&Value> = rows.iter().filter(|row| {
            if let Some(obj) = row.as_object() {
                let key: Vec<String> = unique_keys.iter()
                    .map(|k| obj.get(*k).map(|v| v.to_string()).unwrap_or_default())
                    .collect();
                seen.insert(key)
            } else { false }
        }).collect();

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
                    if error_count <= 3 {
                        error!("Upsert {table} failed: {e}");
                    }
                    if error_count == 3 {
                        error!("Upsert {table}: suppressing further errors");
                    }
                }
            }
        }
        total
    }

    // ═══════════════════════════════════════════════════════════════════
    // DOWNLOAD METHODS
    // ═══════════════════════════════════════════════════════════════════

    // ── 1. Stock List ───────────────────────────────────────────────────

    pub async fn download_stock_list(&self) -> usize {
        info!("Downloading FMP stock list (stock-screener)...");
        // Python uses v3/stock-screener per exchange, NOT v3/stock/list
        let mut all_data = Vec::new();
        for exchange in &["NYSE", "NASDAQ", "AMEX"] {
            let data = self.fmp_get_v3("stock-screener", &[
                ("exchange", exchange), ("limit", "20000"), ("isActivelyTrading", "true"),
            ]).await;
            info!("FMP stock-screener {exchange}: {} rows", data.len());
            all_data.extend(data);
        }
        if all_data.is_empty() { return 0; }
        let rows: Vec<Value> = all_data.into_iter().map(|v| snake_keys(&v)).collect();

        // Also extract industry classification
        let ind_rows: Vec<Value> = rows.iter().filter_map(|v| {
            let obj = v.as_object()?;
            let ticker = obj.get("ticker")?.as_str()?;
            let sector = obj.get("sector").and_then(|v| v.as_str()).unwrap_or("");
            let industry = obj.get("industry").and_then(|v| v.as_str()).unwrap_or("");
            if sector.is_empty() && industry.is_empty() { return None; }
            Some(serde_json::json!({
                "ticker": ticker, "sector": sector, "industry": industry
            }))
        }).collect();

        let count = self.upsert_rows("us_stock_basic", &rows, &["ticker"]).await;
        if !ind_rows.is_empty() {
            self.upsert_rows("us_industry_class", &ind_rows, &["ticker"]).await;
        }
        info!("Stock list: {count} rows");
        count
    }

    // ── 2. Company Profiles ─────────────────────────────────────────────

    pub async fn download_company_profiles(&self) -> usize {
        let tickers = self.get_active_tickers().await;
        let done = self.get_done_tickers("us_company_profile").await;
        let pending: Vec<_> = tickers.into_iter().filter(|t| !done.contains(t)).collect();
        self.run_concurrent(pending, "Profiles", |dl, ticker| async move {
            let data = dl.fmp_get("profile", &[("symbol", &ticker)]).await;
            let mut n = 0;
            if !data.is_empty() {
                let rows: Vec<Value> = data.into_iter().map(|v| snake_keys(&v)).collect();
                n = dl.upsert_rows("us_company_profile", &rows, &["ticker"]).await;
            }
            dl.mark_done("us_company_profile", &ticker).await;
            n
        }).await
    }

    // ── 3. Daily Prices ─────────────────────────────────────────────────

    pub async fn download_daily_prices(&self, start_year: i32, incremental: bool) -> usize {
        let tickers = self.get_active_tickers().await;
        let today = chrono::Local::now().format("%Y-%m-%d").to_string();
        let done = self.get_done_tickers("us_daily_price").await;
        let pending: Vec<String> = if incremental { tickers } else {
            tickers.into_iter().filter(|t| !done.contains(t)).collect()
        };
        if pending.is_empty() { return 0; }

        info!("Daily prices: {} tickers ({MAX_CONCURRENT} concurrent)", pending.len());
        let pb = Arc::new(ticker_progress(pending.len() as u64, "Daily Prices"));
        let total = Arc::new(AtomicUsize::new(0));
        let sem = Arc::new(Semaphore::new(MAX_CONCURRENT));

        let mut handles = Vec::with_capacity(pending.len());
        for ticker in pending {
            let dl = self.clone();
            let today = today.clone();
            let sem = sem.clone();
            let total = total.clone();
            let pb = pb.clone();
            handles.push(tokio::spawn(async move {
                let _permit = sem.acquire().await.unwrap();
                let n = dl.download_daily_price_one(&ticker, start_year, &today).await;
                dl.mark_done("us_daily_price", &ticker).await;
                total.fetch_add(n, Ordering::Relaxed);
                pb.inc(1);
            }));
        }
        for h in handles { h.await.ok(); }
        let t = total.load(Ordering::Relaxed);
        pb.finish_with_message(format!("{t} rows"));
        t
    }

    pub async fn download_daily_price_one(&self, ticker: &str, start_year: i32, today: &str) -> usize {
        let end_year: i32 = today[..4].parse().unwrap_or(2025);
        let mut count = 0;
        let mut yr = start_year;
        while yr <= end_year {
            let seg_end = (yr + 9).min(end_year);
            let data = self.fmp_get("historical-price-eod/full", &[
                ("symbol", ticker), ("from", &format!("{yr}-01-01")), ("to", &format!("{seg_end}-12-31")),
            ]).await;
            if !data.is_empty() {
                let rows: Vec<Value> = data.into_iter().map(|v| {
                    let mut obj = snake_keys(&v);
                    rename_key(obj.as_object_mut().unwrap(), "date", "trade_date");
                    obj
                }).collect();
                count += self.upsert_rows("us_daily_price", &rows, &["ticker", "trade_date"]).await;
            }
            yr += 10;
        }
        count
    }

    // ── 4. Financial Quarterly (IS+BS+CF merged) ────────────────────────

    pub async fn download_financials(&self) -> usize {
        let tickers = self.get_stocks_only_tickers().await;
        let done = self.get_done_tickers("us_financial_data").await;
        let pending: Vec<_> = tickers.into_iter().filter(|t| !done.contains(t)).collect();
        self.run_concurrent(pending, "Financials", |dl, ticker| async move {
            let is_data = dl.fmp_get("income-statement", &[("symbol", &ticker), ("period", "quarter"), ("limit", "400")]).await;
            let bs_data = dl.fmp_get("balance-sheet-statement", &[("symbol", &ticker), ("period", "quarter"), ("limit", "400")]).await;
            let cf_data = dl.fmp_get("cash-flow-statement", &[("symbol", &ticker), ("period", "quarter"), ("limit", "400")]).await;
            let merged = merge_three_statements(&is_data, &bs_data, &cf_data);
            let mut n = 0;
            if !merged.is_empty() {
                n = dl.upsert_rows("us_financial_data", &merged, &["ticker", "period"]).await;
            }
            dl.mark_done("us_financial_data", &ticker).await;
            n
        }).await
    }

    /// Incremental financials: only fetch tickers whose latest filing_date is stale.
    pub async fn download_financials_incremental(&self) -> usize {
        let tickers = self.get_stocks_only_tickers().await;
        let latest = Arc::new(self.get_ticker_latest("us_financial_data", "date").await);
        let today = chrono::Local::now().format("%Y-%m-%d").to_string();
        info!("Financials incremental: {} tickers, {} with existing data", tickers.len(), latest.len());

        let latest_c = latest.clone();
        let today_c = today.clone();
        self.run_concurrent(tickers, "Financials (incr)", move |dl, ticker| {
            let latest = latest_c.clone();
            let today = today_c.clone();
            async move {
                if let Some(latest_date) = latest.get(&ticker) {
                    if let (Ok(ld), Ok(td)) = (
                        chrono::NaiveDate::parse_from_str(latest_date, "%Y-%m-%d"),
                        chrono::NaiveDate::parse_from_str(&today, "%Y-%m-%d"),
                    ) {
                        if (td - ld).num_days() < 30 { return 0; }
                    }
                }
                let is_data = dl.fmp_get("income-statement", &[("symbol", &ticker), ("period", "quarter"), ("limit", "10")]).await;
                let bs_data = dl.fmp_get("balance-sheet-statement", &[("symbol", &ticker), ("period", "quarter"), ("limit", "10")]).await;
                let cf_data = dl.fmp_get("cash-flow-statement", &[("symbol", &ticker), ("period", "quarter"), ("limit", "10")]).await;
                let merged = merge_three_statements(&is_data, &bs_data, &cf_data);
                if !merged.is_empty() {
                    dl.upsert_rows("us_financial_data", &merged, &["ticker", "period"]).await
                } else { 0 }
            }
        }).await
    }

    // ── 5. Key Metrics ──────────────────────────────────────────────────

    pub async fn download_key_metrics(&self) -> usize {
        self.simple_per_ticker("us_key_metric", &["ticker", "date"], "Key Metrics",
            "key-metrics", &[("period", "quarter"), ("limit", "400")]).await
    }
    pub async fn download_key_metrics_incremental(&self) -> usize {
        self.simple_per_ticker_incremental("us_key_metric", &["ticker", "date"], "Key Metrics",
            "key-metrics", &[("period", "quarter"), ("limit", "400")], "date").await
    }

    // ── 6. Financial Growth ─────────────────────────────────────────────

    pub async fn download_financial_growth(&self) -> usize {
        self.simple_per_ticker("us_financial_growth", &["ticker", "date"], "Financial Growth",
            "financial-growth", &[("period", "quarter"), ("limit", "400")]).await
    }
    pub async fn download_financial_growth_incremental(&self) -> usize {
        self.simple_per_ticker_incremental("us_financial_growth", &["ticker", "date"], "Financial Growth",
            "financial-growth", &[("period", "quarter"), ("limit", "400")], "date").await
    }

    // ── 7. Enterprise Values ────────────────────────────────────────────

    pub async fn download_enterprise_values(&self) -> usize {
        self.simple_per_ticker("us_enterprise_value", &["ticker", "date"], "Enterprise Values",
            "enterprise-values", &[("period", "quarter"), ("limit", "400")]).await
    }
    pub async fn download_enterprise_values_incremental(&self) -> usize {
        self.simple_per_ticker_incremental("us_enterprise_value", &["ticker", "date"], "Enterprise Values",
            "enterprise-values", &[("period", "quarter"), ("limit", "400")], "date").await
    }

    // ── 8. Owner Earnings ───────────────────────────────────────────────

    pub async fn download_owner_earnings(&self) -> usize {
        self.simple_per_ticker("us_owner_earnings", &["ticker", "date"], "Owner Earnings",
            "owner-earnings", &[]).await
    }

    // ── 9. Earnings Surprises ───────────────────────────────────────────

    pub async fn download_earnings_surprises(&self) -> usize {
        self.simple_per_ticker("us_earnings_surprise", &["ticker", "date"], "Earnings Surprises",
            "earnings", &[("limit", "400")]).await
    }
    pub async fn download_earnings_surprises_incremental(&self) -> usize {
        self.simple_per_ticker_incremental("us_earnings_surprise", &["ticker", "date"], "Earnings Surprises",
            "earnings", &[("limit", "400")], "date").await
    }

    // ── 10. EPS Estimates ───────────────────────────────────────────────

    pub async fn download_eps_estimates(&self) -> usize {
        let tickers = self.get_stocks_only_tickers().await;
        let done = self.get_done_tickers("us_eps_estimate").await;
        let pending: Vec<_> = tickers.into_iter().filter(|t| !done.contains(t)).collect();
        self.run_concurrent(pending, "EPS Estimates", |dl, ticker| async move {
            let data = dl.fmp_get_v3(&format!("analyst-estimates/{ticker}"),
                &[("period", "quarter"), ("limit", "200")]).await;
            let mut n = 0;
            if !data.is_empty() {
                let rows: Vec<Value> = data.into_iter().map(|v| snake_keys(&v)).collect();
                n = dl.upsert_rows("us_eps_estimate", &rows, &["ticker", "date"]).await;
            }
            dl.mark_done("us_eps_estimate", &ticker).await;
            n
        }).await
    }

    // ── 11. Insider Trading ─────────────────────────────────────────────

    pub async fn download_insider_trading(&self) -> usize {
        let tickers = self.get_stocks_only_tickers().await;
        let done = self.get_done_tickers("us_insider_trade").await;
        let pending: Vec<_> = tickers.into_iter().filter(|t| !done.contains(t)).collect();
        self.run_concurrent(pending, "Insider Trading", |dl, ticker| async move {
            let mut n = 0;
            for page in 0..50 {
                let data = dl.fmp_get_v4("insider-trading",
                    &[("symbol", &ticker), ("page", &page.to_string()), ("limit", "100")]).await;
                if data.is_empty() { break; }
                let len = data.len();
                let rows: Vec<Value> = data.into_iter().map(|v| snake_keys(&v)).collect();
                n += dl.upsert_rows("us_insider_trade", &rows,
                    &["ticker", "transaction_date", "reporting_name", "transaction_type"]).await;
                if len < 100 { break; }
            }
            dl.mark_done("us_insider_trade", &ticker).await;
            n
        }).await
    }

    // ── 12. Analyst Grades ──────────────────────────────────────────────

    pub async fn download_analyst_grades(&self) -> usize {
        let tickers = self.get_stocks_only_tickers().await;
        let done = self.get_done_tickers("us_analyst_recommendation").await;
        let pending: Vec<_> = tickers.into_iter().filter(|t| !done.contains(t)).collect();
        self.run_concurrent(pending, "Analyst Grades", |dl, ticker| async move {
            let data = dl.fmp_get_v3(&format!("grade/{ticker}"), &[]).await;
            let mut n = 0;
            if !data.is_empty() {
                let rows: Vec<Value> = data.into_iter().map(|v| snake_keys(&v)).collect();
                n = dl.upsert_rows("us_analyst_recommendation", &rows,
                    &["ticker", "date", "grading_company"]).await;
            }
            dl.mark_done("us_analyst_recommendation", &ticker).await;
            n
        }).await
    }

    // ── 13. Dividends & Splits ──────────────────────────────────────────

    pub async fn download_dividends(&self) -> usize {
        self.simple_per_ticker("us_corporate_action", &["ticker", "date", "action_type"], "Dividends",
            "dividends", &[]).await
    }

    // ── 14. Financial Scores ────────────────────────────────────────────

    pub async fn download_financial_scores(&self) -> usize {
        self.simple_per_ticker("us_financial_score", &["ticker"], "Financial Scores",
            "financial-scores", &[]).await
    }

    // ── 15. Shares Float ────────────────────────────────────────────────

    pub async fn download_shares_float(&self) -> usize {
        self.simple_per_ticker("us_shares_float", &["ticker", "date"], "Shares Float",
            "shares-float", &[]).await
    }

    // ── 16. Insider Statistics ───────────────────────────────────────────

    pub async fn download_insider_statistics(&self) -> usize {
        self.simple_per_ticker("us_insider_statistic", &["ticker", "year", "quarter"], "Insider Stats",
            "insider-trading/statistics", &[]).await
    }

    // ── 17. Employee Count ──────────────────────────────────────────────

    pub async fn download_employee_count(&self) -> usize {
        self.simple_per_ticker("us_employee_count", &["ticker", "period_of_report"], "Employee Count",
            "employee-count", &[]).await
    }

    // ── 18. Price Targets ───────────────────────────────────────────────

    pub async fn download_price_targets(&self) -> usize {
        self.simple_per_ticker("us_price_target", &["ticker"], "Price Targets",
            "price-target-consensus", &[]).await
    }

    // ── 19. ESG Ratings ─────────────────────────────────────────────────

    pub async fn download_esg_ratings(&self) -> usize {
        self.simple_per_ticker("us_esg_rating", &["ticker", "fiscal_year"], "ESG Ratings",
            "esg-ratings", &[]).await
    }

    // ── 20. DCF Valuations ──────────────────────────────────────────────

    pub async fn download_dcf_valuations(&self) -> usize {
        self.simple_per_ticker("us_dcf_valuation", &["ticker", "date", "dcf_type"], "DCF Valuations",
            "discounted-cash-flow", &[]).await
    }

    // ── 21. Stock Peers ─────────────────────────────────────────────────

    pub async fn download_stock_peers(&self) -> usize {
        let tickers = self.get_stocks_only_tickers().await;
        let done = self.get_done_tickers("us_stock_peer").await;
        let pending: Vec<_> = tickers.into_iter().filter(|t| !done.contains(t)).collect();
        if pending.is_empty() { return 0; }

        self.run_concurrent(pending, "Stock Peers", |dl, ticker| async move {
            let data = dl.fmp_get("stock-peers", &[("symbol", &ticker)]).await;
            let mut n = 0;
            for item in &data {
                if let Some(peers) = item.get("peersList").and_then(|v| v.as_array()) {
                    for peer in peers {
                        if let Some(p) = peer.as_str() {
                            let row = serde_json::json!({"ticker": &ticker, "peer_ticker": p});
                            n += dl.upsert_rows("us_stock_peer", &[row], &["ticker", "peer_ticker"]).await;
                        }
                    }
                }
            }
            dl.mark_done("us_stock_peer", &ticker).await;
            n
        }).await
    }

    // ── 22. Index Daily ─────────────────────────────────────────────────

    pub async fn download_index_daily(&self, start_year: i32) -> usize {
        let indices = ["^GSPC", "^DJI", "^IXIC", "^RUT"];
        let today = chrono::Local::now().format("%Y-%m-%d").to_string();
        let end_year: i32 = today[..4].parse().unwrap_or(2025);
        let mut total = 0;

        for index in &indices {
            let mut yr = start_year;
            while yr <= end_year {
                let seg_end = (yr + 9).min(end_year);
                let data = self.fmp_get("historical-price-eod/full", &[
                    ("symbol", index), ("from", &format!("{yr}-01-01")), ("to", &format!("{seg_end}-12-31")),
                ]).await;
                if !data.is_empty() {
                    let rows: Vec<Value> = data.into_iter().map(|v| {
                        let mut obj = snake_keys(&v);
                        if let Some(map) = obj.as_object_mut() {
                            rename_key(map, "date", "trade_date");
                            if let Some(sym) = map.remove("ticker") {
                                map.insert("index_code".to_string(), sym);
                            }
                        }
                        obj
                    }).collect();
                    total += self.upsert_rows("us_index_daily", &rows, &["index_code", "trade_date"]).await;
                }
                yr += 10;
            }
            info!("Index {index}: done");
        }
        total
    }

    // ── 23. Macro Indicators ────────────────────────────────────────────

    pub async fn download_macro(&self) -> usize {
        let indicators = [
            ("treasury", "US_10Y", "year10"),
            ("treasury", "US_2Y", "year2"),
            ("treasury", "US_5Y", "year5"),
            ("treasury", "US_30Y", "year30"),
            ("treasury", "US_1M", "month1"),
            ("treasury", "US_3M", "month3"),
            ("economic", "US_GDP", "GDP"),
            ("economic", "US_CPI", "CPI"),
            ("economic", "US_UNEMPLOYMENT", "unemployment-rate"),
        ];
        let mut total = 0;

        for (endpoint_type, code, param) in &indicators {
            let data = if *endpoint_type == "treasury" {
                self.fmp_get_v4("treasury", &[("from", "2000-01-01"), ("to", "2026-12-31")]).await
            } else {
                self.fmp_get_v4(&format!("economic?name={param}"), &[]).await
            };

            if data.is_empty() { continue; }

            let rows: Vec<Value> = data.into_iter().filter_map(|v| {
                let obj = v.as_object()?;
                let date = obj.get("date").and_then(|v| v.as_str())?;
                let value = if *endpoint_type == "treasury" {
                    obj.get(*param).and_then(|v| v.as_f64())
                } else {
                    obj.get("value").and_then(|v| v.as_f64())
                }?;
                Some(serde_json::json!({
                    "indicator_code": code,
                    "report_date": date,
                    "value": value,
                }))
            }).collect();

            total += self.upsert_rows("us_macro_indicator", &rows, &["indicator_code", "report_date"]).await;
            info!("Macro {code}: {} rows", rows.len());
        }
        total
    }

    // ── 24. Congress Trading ────────────────────────────────────────────

    pub async fn download_congress_trading(&self) -> usize {
        let tickers = self.get_stocks_only_tickers().await;
        let done = self.get_done_tickers("us_congress_trade").await;
        let pending: Vec<_> = tickers.into_iter().filter(|t| !done.contains(t)).collect();
        self.run_concurrent(pending, "Congress Trading", |dl, ticker| async move {
            let mut n = 0;
            for endpoint in &["senate-trades", "house-trades"] {
                let data = dl.fmp_get(endpoint, &[("symbol", &ticker)]).await;
                if !data.is_empty() {
                    let rows: Vec<Value> = data.into_iter().map(|v| snake_keys(&v)).collect();
                    n += dl.upsert_rows("us_congress_trade", &rows,
                        &["ticker", "transaction_date", "first_name", "last_name", "type"]).await;
                }
            }
            dl.mark_done("us_congress_trade", &ticker).await;
            n
        }).await
    }

    // ── 25. Press Releases ──────────────────────────────────────────────

    pub async fn download_press_releases(&self) -> usize {
        let tickers = self.get_stocks_only_tickers().await;
        let done = self.get_done_tickers("us_press_release").await;
        let pending: Vec<_> = tickers.into_iter().filter(|t| !done.contains(t)).collect();
        self.run_concurrent(pending, "Press Releases", |dl, ticker| async move {
            let data = dl.fmp_get("news/press-releases", &[("symbols", &ticker), ("limit", "500")]).await;
            let mut n = 0;
            if !data.is_empty() {
                let rows: Vec<Value> = data.into_iter().map(|v| snake_keys(&v)).collect();
                n = dl.upsert_rows("us_press_release", &rows, &["ticker", "url"]).await;
            }
            dl.mark_done("us_press_release", &ticker).await;
            n
        }).await
    }

    // ── 26. Revenue Segments ────────────────────────────────────────────

    pub async fn download_revenue_segments(&self) -> usize {
        let tickers = self.get_stocks_only_tickers().await;
        let done = self.get_done_tickers("us_revenue_segment").await;
        let pending: Vec<_> = tickers.into_iter().filter(|t| !done.contains(t)).collect();
        self.run_concurrent(pending, "Revenue Segments", |dl, ticker| async move {
            let mut n = 0;
            for seg_type in &["product", "geographic"] {
                let data = dl.fmp_get(&format!("revenue-{seg_type}-segmentation"), &[
                    ("symbol", &ticker), ("structure", "flat"),
                ]).await;
                if !data.is_empty() {
                    let rows: Vec<Value> = data.into_iter().map(|v| {
                        let mut obj = snake_keys(&v);
                        if let Some(map) = obj.as_object_mut() {
                            map.insert("segment_type".to_string(), Value::String(seg_type.to_string()));
                        }
                        obj
                    }).collect();
                    n += dl.upsert_rows("us_revenue_segment", &rows,
                        &["ticker", "date", "segment_type", "segment_name"]).await;
                }
            }
            dl.mark_done("us_revenue_segment", &ticker).await;
            n
        }).await
    }

    // ── 27. Delisted Companies ──────────────────────────────────────────

    pub async fn download_delisted(&self) -> usize {
        // Python: _fmp_get_stable("delisted-companies")
        let data = self.fmp_get("delisted-companies", &[]).await;
        if data.is_empty() { return 0; }
        let rows: Vec<Value> = data.into_iter().map(|v| snake_keys(&v)).collect();
        self.upsert_rows("us_delisted", &rows, &["ticker"]).await
    }

    // ── 28. Symbol Changes ──────────────────────────────────────────────

    pub async fn download_symbol_changes(&self) -> usize {
        // Python: _fmp_get_stable("symbol-change")
        let data = self.fmp_get("symbol-change", &[]).await;
        if data.is_empty() { return 0; }
        let rows: Vec<Value> = data.into_iter().map(|v| snake_keys(&v)).collect();
        self.upsert_rows("us_symbol_change", &rows, &["old_symbol", "new_symbol", "date"]).await
    }

    // ── download_all ────────────────────────────────────────────────────

    pub async fn download_all(&self, start_year: i32) -> usize {
        let mut total = 0;
        total += self.download_stock_list().await;
        total += self.download_company_profiles().await;
        total += self.download_index_daily(start_year).await;
        total += self.download_macro().await;
        total += self.download_daily_prices(start_year, false).await;
        total += self.download_financials().await;
        total += self.download_key_metrics().await;
        total += self.download_financial_growth().await;
        total += self.download_enterprise_values().await;
        total += self.download_owner_earnings().await;
        total += self.download_earnings_surprises().await;
        total += self.download_eps_estimates().await;
        total += self.download_insider_trading().await;
        total += self.download_analyst_grades().await;
        total += self.download_dividends().await;
        total += self.download_financial_scores().await;
        total += self.download_shares_float().await;
        total += self.download_insider_statistics().await;
        total += self.download_employee_count().await;
        total += self.download_price_targets().await;
        total += self.download_esg_ratings().await;
        total += self.download_dcf_valuations().await;
        total += self.download_stock_peers().await;
        total += self.download_congress_trading().await;
        total += self.download_press_releases().await;
        total += self.download_revenue_segments().await;
        total += self.download_delisted().await;
        total += self.download_symbol_changes().await;
        info!("FMP download_all total: {total}");
        total
    }

    /// Incremental update: only fetch new data since last known date per ticker.
    pub async fn update_all(&self) -> usize {
        let mut total = 0;
        total += self.download_stock_list().await;
        total += self.download_daily_prices(2020, true).await;
        total += self.download_financials_incremental().await;
        total += self.download_key_metrics_incremental().await;
        total += self.download_financial_growth_incremental().await;
        total += self.download_enterprise_values_incremental().await;
        total += self.download_earnings_surprises_incremental().await;
        total += self.download_index_daily(2020).await;
        total += self.download_macro().await;
        info!("FMP update_all total: {total}");
        total
    }

    // ── Generic simple per-ticker helper ────────────────────────────────

    async fn simple_per_ticker(
        &self,
        table: &str,
        unique_keys: &[&str],
        label: &str,
        endpoint: &str,
        extra_params: &[(&str, &str)],
    ) -> usize {
        self.simple_per_ticker_ex(table, unique_keys, label, endpoint, extra_params, false, "date").await
    }

    /// Incremental variant: queries DB for latest date per ticker, only fetches newer data.
    async fn simple_per_ticker_incremental(
        &self,
        table: &str,
        unique_keys: &[&str],
        label: &str,
        endpoint: &str,
        extra_params: &[(&str, &str)],
        date_field: &str,
    ) -> usize {
        self.simple_per_ticker_ex(table, unique_keys, label, endpoint, extra_params, true, date_field).await
    }

    async fn simple_per_ticker_ex(
        &self,
        table: &str,
        unique_keys: &[&str],
        label: &str,
        endpoint: &str,
        extra_params: &[(&str, &str)],
        incremental: bool,
        date_field: &str,
    ) -> usize {
        let tickers = self.get_stocks_only_tickers().await;
        let today = chrono::Local::now().format("%Y-%m-%d").to_string();

        let latest_map = if incremental {
            let latest = self.get_ticker_latest(table, date_field).await;
            info!("{label} incremental: {} tickers, {} with existing data", tickers.len(), latest.len());
            Some(latest)
        } else {
            None
        };

        let pending: Vec<String> = if incremental {
            tickers
        } else {
            let done = self.get_done_tickers(table).await;
            tickers.into_iter().filter(|t| !done.contains(t)).collect()
        };

        if pending.is_empty() { info!("{label}: all done"); return 0; }

        info!("{label}: {} tickers ({MAX_CONCURRENT} concurrent)", pending.len());
        let pb = Arc::new(ticker_progress(pending.len() as u64, label));
        let total = Arc::new(AtomicUsize::new(0));
        let skipped = Arc::new(AtomicUsize::new(0));
        let sem = Arc::new(Semaphore::new(MAX_CONCURRENT));

        // Convert to owned for spawn
        let table = table.to_string();
        let unique_keys: Vec<String> = unique_keys.iter().map(|s| s.to_string()).collect();
        let endpoint = endpoint.to_string();
        let extra_params: Vec<(String, String)> = extra_params.iter().map(|(k, v)| (k.to_string(), v.to_string())).collect();
        let latest_map = latest_map.map(Arc::new);

        let mut handles = Vec::with_capacity(pending.len());
        for ticker in pending {
            let dl = self.clone();
            let sem = sem.clone();
            let total = total.clone();
            let skipped = skipped.clone();
            let pb = pb.clone();
            let today = today.clone();
            let table = table.clone();
            let unique_keys = unique_keys.clone();
            let endpoint = endpoint.clone();
            let extra_params = extra_params.clone();
            let latest_map = latest_map.clone();

            handles.push(tokio::spawn(async move {
                let _permit = sem.acquire().await.unwrap();

                // Incremental: check if ticker needs update
                let from_date = if let Some(ref latest) = latest_map {
                    match latest.get(&ticker) {
                        Some(latest_date) => {
                            if latest_date.as_str() >= &today[..10] {
                                skipped.fetch_add(1, Ordering::Relaxed);
                                pb.inc(1);
                                return;
                            }
                            Some(next_day(latest_date))
                        }
                        None => None,
                    }
                } else {
                    None
                };

                let mut params: Vec<(&str, &str)> = vec![("symbol", &ticker)];
                for (k, v) in &extra_params {
                    params.push((k.as_str(), v.as_str()));
                }
                let from_str;
                if let Some(ref fd) = from_date {
                    from_str = fd.clone();
                    params.push(("from", &from_str));
                }

                let data = dl.fmp_get(&endpoint, &params).await;
                if !data.is_empty() {
                    let rows: Vec<Value> = data.into_iter().map(|v| snake_keys(&v)).collect();
                    let uk_refs: Vec<&str> = unique_keys.iter().map(|s| s.as_str()).collect();
                    let n = dl.upsert_rows(&table, &rows, &uk_refs).await;
                    total.fetch_add(n, Ordering::Relaxed);
                }
                if latest_map.is_none() {
                    dl.mark_done(&table, &ticker).await;
                }
                pb.inc(1);
            }));
        }
        for h in handles { h.await.ok(); }
        let t = total.load(Ordering::Relaxed);
        let s = skipped.load(Ordering::Relaxed);
        pb.finish_with_message(format!("{t} rows ({s} skipped)"));
        t
    }
}

// ═══════════════════════════════════════════════════════════════════════
// HELPERS
// ═══════════════════════════════════════════════════════════════════════

/// Compute next day string from "YYYY-MM-DD".
fn next_day(date_str: &str) -> String {
    chrono::NaiveDate::parse_from_str(date_str, "%Y-%m-%d")
        .map(|d| (d + chrono::Duration::days(1)).format("%Y-%m-%d").to_string())
        .unwrap_or_else(|_| date_str.to_string())
}

fn rename_key(map: &mut serde_json::Map<String, Value>, from: &str, to: &str) {
    if let Some(val) = map.remove(from) {
        map.insert(to.to_string(), val);
    }
}

fn merge_three_statements(is_data: &[Value], bs_data: &[Value], cf_data: &[Value]) -> Vec<Value> {
    use std::collections::HashMap;

    let mut bs_map: HashMap<(String, String), &Value> = HashMap::new();
    for row in bs_data {
        if let (Some(d), Some(p)) = (
            row.get("date").and_then(|v| v.as_str()),
            row.get("period").and_then(|v| v.as_str()),
        ) { bs_map.insert((d.to_string(), p.to_string()), row); }
    }
    let mut cf_map: HashMap<(String, String), &Value> = HashMap::new();
    for row in cf_data {
        if let (Some(d), Some(p)) = (
            row.get("date").and_then(|v| v.as_str()),
            row.get("period").and_then(|v| v.as_str()),
        ) { cf_map.insert((d.to_string(), p.to_string()), row); }
    }

    let mut result = Vec::new();
    for is_row in is_data {
        let date = is_row.get("date").and_then(|v| v.as_str()).unwrap_or("");
        let period = is_row.get("period").and_then(|v| v.as_str()).unwrap_or("");
        if date.is_empty() || period.is_empty() { continue; }
        let key = (date.to_string(), period.to_string());

        let mut merged = snake_keys(is_row);
        if let Some(map) = merged.as_object_mut() {
            if let Some(bs) = bs_map.get(&key) {
                if let Some(bs_obj) = snake_keys(bs).as_object().cloned() {
                    for (k, v) in bs_obj { if !map.contains_key(&k) { map.insert(k, v); } }
                }
            }
            if let Some(cf) = cf_map.get(&key) {
                if let Some(cf_obj) = snake_keys(cf).as_object().cloned() {
                    for (k, v) in cf_obj { if !map.contains_key(&k) { map.insert(k, v); } }
                }
            }
        }
        result.push(merged);
    }
    result
}

/// Convert a JSON value to a SQL literal string for inline INSERT.
/// Numbers: inline. Strings: single-quoted + escaped. Bools: 0/1. Null: NULL.
fn to_sql_literal(val: &Value) -> String {
    match val {
        Value::Null => "NULL".to_string(),
        Value::Bool(b) => if *b { "1".to_string() } else { "0".to_string() },
        Value::Number(n) => n.to_string(),
        Value::String(s) => {
            // Escape single quotes for SQL
            let escaped = s.replace('\'', "''");
            format!("'{escaped}'")
        }
        Value::Array(_) | Value::Object(_) => {
            let s = val.to_string().replace('\'', "''");
            format!("'{s}'")
        }
    }
}
