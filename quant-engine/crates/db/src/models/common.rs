//! Shared model types.

use chrono::NaiveDateTime;
use sqlx::FromRow;

/// Import progress tracking (equivalent to Python's ImportProgress).
#[derive(Debug, Clone, FromRow)]
pub struct ImportProgress {
    pub id: i32,
    pub table_name: String,
    pub ticker: String,
    pub completed_at: Option<NaiveDateTime>,
}
