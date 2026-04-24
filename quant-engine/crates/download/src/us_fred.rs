//! FRED (Federal Reserve Economic Data) downloader.
//!
//! Downloads macro indicators via FRED API and stores in us_macro_indicator.
//! API: https://api.stlouisfed.org/fred/series/observations

use serde_json::Value;
use sqlx::PgPool;
use tracing::{error, info, warn};

use crate::http::ApiClient;

/// FRED series mapping: (indicator_code, fred_series_id).
const FRED_SERIES: &[(&str, &str)] = &[
    // Classic macro (20)
    ("US_GDP", "GDP"),
    ("US_CPI_YOY", "CPIAUCSL"),
    ("US_CORE_CPI", "CPILFESL"),
    ("US_PPI", "PPIACO"),
    ("US_UNEMP", "UNRATE"),
    ("US_NONFARM", "PAYEMS"),
    ("US_FED_RATE", "FEDFUNDS"),
    ("US_M2", "M2SL"),
    ("US_PMI_MFG", "MANEMP"),
    ("US_RETAIL", "RSAFS"),
    ("US_IND_PROD", "INDPRO"),
    ("US_HOUSING", "HOUST"),
    ("US_10Y", "DGS10"),
    ("US_2Y", "DGS2"),
    ("US_2Y10Y", "T10Y2Y"),
    ("US_TED", "TEDRATE"),
    ("US_VIX", "VIXCLS"),
    ("US_DXY", "DTWEXBGS"),
    ("US_INIT_CLAIMS", "ICSA"),
    ("US_PCE", "PCEPI"),
    // Enhanced macro (11)
    ("US_3M", "DGS3MO"),
    ("US_6M", "DGS6MO"),
    ("US_30Y", "DGS30"),
    ("US_HY_OAS", "BAMLH0A0HYM2"),
    ("US_IG_OAS", "BAMLC0A0CM"),
    ("US_BAA_AAA", "BAA10Y"),
    ("US_BREAKEVEN_5Y", "T5YIE"),
    ("US_BREAKEVEN_10Y", "T10YIE"),
    ("US_CAPACITY_UTIL", "TCU"),
    ("US_CONSUMER_SENT", "UMCSENT"),
    ("US_SAHM_RULE", "SAHMREALTIME"),
];

pub struct FredDownloader {
    pub api_key: String,
    pub client: ApiClient,
    pub pool: PgPool,
}

impl FredDownloader {
    pub fn new(api_key: String, pool: PgPool) -> Self {
        Self {
            api_key,
            client: ApiClient::new(120, 5), // FRED: 120 req/min
            pool,
        }
    }

    /// Download all FRED series.
    pub async fn download_all(&self, start_date: &str) -> usize {
        let mut total = 0;

        for &(indicator_code, fred_series) in FRED_SERIES {
            let count = self.download_series(indicator_code, fred_series, start_date).await;
            if count > 0 {
                info!("FRED {indicator_code}: {count} rows");
            }
            total += count;
        }

        info!("FRED total: {total} rows ({} indicators)", FRED_SERIES.len());
        total
    }

    async fn download_series(&self, indicator_code: &str, fred_series: &str, start_date: &str) -> usize {
        let url = format!(
            "https://api.stlouisfed.org/fred/series/observations\
             ?series_id={fred_series}&api_key={}&file_type=json\
             &observation_start={start_date}&sort_order=asc",
            self.api_key
        );

        let resp = match self.client.get_json(&url).await {
            Ok(v) => v,
            Err(e) => {
                error!("FRED {indicator_code} ({fred_series}): {e}");
                return 0;
            }
        };

        let observations = match resp.get("observations").and_then(|v| v.as_array()) {
            Some(arr) => arr,
            None => return 0,
        };

        let rows: Vec<Value> = observations.iter().filter_map(|obs| {
            let date = obs.get("date").and_then(|v| v.as_str())?;
            let value_str = obs.get("value").and_then(|v| v.as_str())?;
            let value: f64 = value_str.parse().ok()?;
            if !value.is_finite() { return None; }
            Some(serde_json::json!({
                "indicator_code": indicator_code,
                "report_date": date,
                "value": value,
            }))
        }).collect();

        if rows.is_empty() { return 0; }
        self.upsert_rows("us_macro_indicator", &rows, &["indicator_code", "report_date"]).await
    }

    async fn upsert_rows(&self, table: &str, rows: &[Value], unique_keys: &[&str]) -> usize {
        if rows.is_empty() { return 0; }
        let first = match rows[0].as_object() { Some(m) => m, None => return 0 };
        let columns: Vec<String> = first.keys().cloned().collect();
        if columns.is_empty() { return 0; }

        let mut param_idx = 1u32;
        let mut values_clauses = Vec::new();
        let mut params: Vec<String> = Vec::new();

        for row in rows {
            let obj = match row.as_object() { Some(m) => m, None => continue };
            let placeholders: Vec<String> = columns.iter().map(|col| {
                let p = format!("${param_idx}");
                param_idx += 1;
                let val = obj.get(col).unwrap_or(&Value::Null);
                params.push(match val {
                    Value::Null => String::new(),
                    Value::Number(n) => n.to_string(),
                    Value::String(s) => s.clone(),
                    other => other.to_string(),
                });
                p
            }).collect();
            values_clauses.push(format!("({})", placeholders.join(", ")));
        }

        let col_list = columns.join(", ");
        let conflict_cols = unique_keys.join(", ");
        let update_set: String = columns.iter()
            .filter(|c| !unique_keys.contains(&c.as_str()))
            .map(|c| format!("{c} = EXCLUDED.{c}"))
            .collect::<Vec<_>>().join(", ");

        let sql = format!(
            "INSERT INTO {table} ({col_list}) VALUES {} ON CONFLICT ({conflict_cols}) DO UPDATE SET {update_set}",
            values_clauses.join(", ")
        );

        let mut query = sqlx::query(&sql);
        for p in &params { query = query.bind(p); }

        match query.execute(&self.pool).await {
            Ok(r) => r.rows_affected() as usize,
            Err(e) => { error!("FRED upsert {table}: {e}"); 0 }
        }
    }
}
