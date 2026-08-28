//! Paper-trading DB queries — account / position / trade history.

use chrono::NaiveDate;
use sqlx::MySqlPool;

use crate::models::a_paper::*;

// ── Account ─────────────────────────────────────────────────────────────

pub async fn get_or_create_account(
    pool: &MySqlPool,
    account_id: &str,
    initial_capital: f64,
) -> Result<PaperAccountRow, sqlx::Error> {
    if let Some(a) = get_account(pool, account_id).await? {
        return Ok(a);
    }
    sqlx::query(
        "INSERT INTO a_paper_account (account_id, initial_capital, cash) \
         VALUES (?, ?, ?)"
    )
    .bind(account_id)
    .bind(initial_capital)
    .bind(initial_capital)
    .execute(pool)
    .await?;

    get_account(pool, account_id).await?
        .ok_or(sqlx::Error::RowNotFound)
}

pub async fn get_account(
    pool: &MySqlPool,
    account_id: &str,
) -> Result<Option<PaperAccountRow>, sqlx::Error> {
    sqlx::query_as::<_, PaperAccountRow>(
        "SELECT * FROM a_paper_account WHERE account_id = ?"
    ).bind(account_id).fetch_optional(pool).await
}

// ── Positions ───────────────────────────────────────────────────────────

pub async fn get_positions(
    pool: &MySqlPool,
    account_id: &str,
) -> Result<Vec<PaperPositionRow>, sqlx::Error> {
    sqlx::query_as::<_, PaperPositionRow>(
        "SELECT * FROM a_paper_position WHERE account_id = ? AND shares > 0"
    ).bind(account_id).fetch_all(pool).await
}

// ── Trades ──────────────────────────────────────────────────────────────

pub async fn list_trades(
    pool: &MySqlPool,
    account_id: &str,
    start: NaiveDate,
    end: NaiveDate,
) -> Result<Vec<PaperTradeRow>, sqlx::Error> {
    sqlx::query_as::<_, PaperTradeRow>(
        "SELECT * FROM a_paper_trade \
         WHERE account_id = ? AND trade_date >= ? AND trade_date <= ? \
         ORDER BY trade_date, id"
    ).bind(account_id).bind(start).bind(end).fetch_all(pool).await
}
