//! Paper trading row models — match `migrate_paper_tables.sql` exactly.

use chrono::{DateTime, NaiveDate, Utc};
use sqlx::FromRow;

#[derive(Debug, Clone, FromRow)]
pub struct PaperAccountRow {
    pub id: i64,
    pub account_id: String,
    pub initial_capital: f64,
    pub cash: f64,
    pub created_at: DateTime<Utc>,
    pub updated_at: DateTime<Utc>,
}

#[derive(Debug, Clone, FromRow)]
pub struct PaperPositionRow {
    pub id: i64,
    pub account_id: String,
    pub ts_code: String,
    pub shares: i64,
    pub avg_cost: f64,
    pub updated_at: DateTime<Utc>,
}

#[derive(Debug, Clone, FromRow)]
pub struct PaperTradeRow {
    pub id: i64,
    pub account_id: String,
    pub trade_date: NaiveDate,
    pub ts_code: String,
    pub side: String,
    pub shares: i64,
    pub price: f64,
    pub gross: f64,
    pub fees: f64,
    pub created_at: DateTime<Utc>,
}
