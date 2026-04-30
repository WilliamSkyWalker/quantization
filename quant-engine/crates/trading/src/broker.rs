//! Broker abstraction — paper, live, and dry-run all implement the same trait.

use async_trait::async_trait;
use chrono::{DateTime, NaiveDate, Utc};
use rustc_hash::FxHashMap;
use serde::{Deserialize, Serialize};
use thiserror::Error;

pub use quant_backtest::a_exec::{Fill, OrderIntent, Side};

/// A position record for one ticker in an account.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Position {
    pub ts_code: String,
    pub shares: i64,
    /// Average cost basis per share (CNY).
    pub avg_cost: f64,
}

/// Account snapshot — what the broker reports for the cash account.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct AccountSnapshot {
    pub account_id: String,
    pub cash: f64,
    pub nav: f64,
    pub total_market_value: f64,
    pub positions: Vec<Position>,
    pub as_of: DateTime<Utc>,
}

#[derive(Debug, Error)]
pub enum BrokerError {
    #[error("database error: {0}")]
    Db(#[from] sqlx::Error),
    #[error("account {0} not found")]
    AccountNotFound(String),
    #[error("missing quote for {0} on {1}")]
    MissingQuote(String, NaiveDate),
    #[error("internal: {0}")]
    Internal(String),
}

/// Generic broker contract. Both `PaperBroker` and any future live
/// implementation must conform — that's the only way to keep the CLI and
/// risk gate broker-agnostic.
#[async_trait]
pub trait Broker: Send + Sync {
    /// Read-only snapshot of the current account.
    async fn snapshot(&self) -> Result<AccountSnapshot, BrokerError>;

    /// Submit a batch of orders for execution at the next opportunity.
    /// Returns the fills that succeeded (subset of input).
    async fn submit(
        &self,
        date: NaiveDate,
        orders: &[OrderIntent],
    ) -> Result<Vec<Fill>, BrokerError>;

    /// Plan + submit from a target weight signal. Convenience wrapper that
    /// loads positions, calls `a_exec::plan_orders`, and submits.
    async fn rebalance(
        &self,
        date: NaiveDate,
        target_weights: &FxHashMap<String, f64>,
    ) -> Result<Vec<Fill>, BrokerError>;
}
