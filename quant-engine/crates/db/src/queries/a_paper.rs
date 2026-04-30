//! Paper-trading DB queries — account / position / trade history.

use chrono::NaiveDate;
use sqlx::PgPool;

use crate::models::a_paper::*;

// ── Account ─────────────────────────────────────────────────────────────

pub async fn get_or_create_account(
    pool: &PgPool,
    account_id: &str,
    initial_capital: f64,
) -> Result<PaperAccountRow, sqlx::Error> {
    if let Some(a) = get_account(pool, account_id).await? {
        return Ok(a);
    }
    sqlx::query_as::<_, PaperAccountRow>(
        "INSERT INTO a_paper_account (account_id, initial_capital, cash) \
         VALUES ($1, $2, $2) RETURNING *"
    )
    .bind(account_id)
    .bind(initial_capital)
    .fetch_one(pool)
    .await
}

pub async fn get_account(
    pool: &PgPool,
    account_id: &str,
) -> Result<Option<PaperAccountRow>, sqlx::Error> {
    sqlx::query_as::<_, PaperAccountRow>(
        "SELECT * FROM a_paper_account WHERE account_id = $1"
    ).bind(account_id).fetch_optional(pool).await
}

// ── Positions ───────────────────────────────────────────────────────────

pub async fn get_positions(
    pool: &PgPool,
    account_id: &str,
) -> Result<Vec<PaperPositionRow>, sqlx::Error> {
    sqlx::query_as::<_, PaperPositionRow>(
        "SELECT * FROM a_paper_position WHERE account_id = $1 AND shares > 0"
    ).bind(account_id).fetch_all(pool).await
}

// ── Trades ──────────────────────────────────────────────────────────────
// PaperBroker writes positions/cash/trades together inside a transaction
// (see paper::PaperBroker::persist_fills) — so we don't expose pool-level
// upsert/update helpers that would risk callers split-writing without a tx.

pub async fn list_trades(
    pool: &PgPool,
    account_id: &str,
    start: NaiveDate,
    end: NaiveDate,
) -> Result<Vec<PaperTradeRow>, sqlx::Error> {
    sqlx::query_as::<_, PaperTradeRow>(
        "SELECT * FROM a_paper_trade \
         WHERE account_id = $1 AND trade_date >= $2 AND trade_date <= $3 \
         ORDER BY trade_date, id"
    ).bind(account_id).bind(start).bind(end).fetch_all(pool).await
}
