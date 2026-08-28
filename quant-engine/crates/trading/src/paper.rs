//! PaperBroker — DB-backed simulator that uses `a_exec` primitives so
//! results match the backtest engine exactly.
//!
//! State (cash, positions, trades) lives in `a_paper_*` tables. Quotes come
//! from a `QuoteSource` supplied by the caller (typically `CachedQuotes` over
//! `AShareCache` for replay or a real-time snapshot for live paper).

use async_trait::async_trait;
use chrono::{NaiveDate, Utc};
use rustc_hash::FxHashMap;
use sqlx::MySqlPool;
use tracing::{debug, info};

use quant_backtest::a_exec::{
    self, ACostConfig, Fill, OrderIntent, QuoteSource, Side,
};
use quant_db::queries::a_paper as q;

use crate::broker::{AccountSnapshot, Broker, BrokerError, Position};

/// PaperBroker — wraps a DB connection + a quote source.
pub struct PaperBroker<Q: QuoteSource + Send + Sync> {
    pool: MySqlPool,
    account_id: String,
    cost: ACostConfig,
    quotes: Q,
}

impl<Q: QuoteSource + Send + Sync> PaperBroker<Q> {
    pub fn new(pool: MySqlPool, account_id: String, cost: ACostConfig, quotes: Q) -> Self {
        Self { pool, account_id, cost, quotes }
    }

    /// Ensure account exists in DB; creates with `initial_capital` if absent.
    pub async fn init(&self, initial_capital: f64) -> Result<(), BrokerError> {
        q::get_or_create_account(&self.pool, &self.account_id, initial_capital).await?;
        Ok(())
    }

    async fn load_positions(&self) -> Result<(FxHashMap<String, i64>, FxHashMap<String, f64>), BrokerError> {
        let rows = q::get_positions(&self.pool, &self.account_id).await?;
        let mut shares = FxHashMap::default();
        let mut costs = FxHashMap::default();
        for r in rows {
            shares.insert(r.ts_code.clone(), r.shares);
            costs.insert(r.ts_code, r.avg_cost);
        }
        Ok((shares, costs))
    }

    async fn load_cash(&self) -> Result<f64, BrokerError> {
        let acct = q::get_account(&self.pool, &self.account_id).await?
            .ok_or_else(|| BrokerError::AccountNotFound(self.account_id.clone()))?;
        Ok(acct.cash)
    }

    /// Apply fills to DB: update cash, upsert positions, insert trade rows.
    async fn persist_fills(
        &self,
        date: NaiveDate,
        new_cash: f64,
        new_shares: &FxHashMap<String, i64>,
        new_costs: &FxHashMap<String, f64>,
        old_shares: &FxHashMap<String, i64>,
        fills: &[Fill],
    ) -> Result<(), BrokerError> {
        let mut tx = self.pool.begin().await?;

        // Cash (updated_at 由 DB trigger 自动维护)
        sqlx::query(
            "UPDATE a_paper_account SET cash = ? WHERE account_id = ?"
        ).bind(new_cash).bind(&self.account_id).execute(&mut *tx).await?;

        // Position changes — diff old vs new
        let mut keys: std::collections::BTreeSet<&String> = new_shares.keys().collect();
        keys.extend(old_shares.keys());
        for code in keys {
            let new_n = new_shares.get(code).copied().unwrap_or(0);
            let old_n = old_shares.get(code).copied().unwrap_or(0);
            if new_n == old_n { continue; }
            let cost = new_costs.get(code).copied().unwrap_or(0.0);
            if new_n <= 0 {
                sqlx::query(
                    "DELETE FROM a_paper_position WHERE account_id = ? AND ts_code = ?"
                ).bind(&self.account_id).bind(code).execute(&mut *tx).await?;
            } else {
                sqlx::query(
                    "INSERT INTO a_paper_position (account_id, ts_code, shares, avg_cost) \
                     VALUES (?, ?, ?, ?) \
                     ON DUPLICATE KEY UPDATE shares = VALUES(shares), avg_cost = VALUES(avg_cost)"
                )
                .bind(&self.account_id).bind(code).bind(new_n).bind(cost)
                .execute(&mut *tx).await?;
            }
        }

        // Trade history
        for f in fills {
            sqlx::query(
                "INSERT INTO a_paper_trade (account_id, trade_date, ts_code, side, shares, price, gross, fees) \
                 VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
            )
            .bind(&self.account_id).bind(date).bind(&f.ts_code).bind(f.side.as_str())
            .bind(f.shares).bind(f.price).bind(f.gross).bind(f.fees)
            .execute(&mut *tx).await?;
        }

        tx.commit().await?;
        Ok(())
    }
}

#[async_trait]
impl<Q: QuoteSource + Send + Sync> Broker for PaperBroker<Q> {
    async fn snapshot(&self) -> Result<AccountSnapshot, BrokerError> {
        let acct = q::get_account(&self.pool, &self.account_id).await?
            .ok_or_else(|| BrokerError::AccountNotFound(self.account_id.clone()))?;
        let rows = q::get_positions(&self.pool, &self.account_id).await?;
        let mut total_mv = 0.0;
        let mut positions: Vec<Position> = Vec::with_capacity(rows.len());
        for r in &rows {
            let mv = self.quotes.bar(&r.ts_code)
                .map(|b| b.close * r.shares as f64)
                .unwrap_or(r.avg_cost * r.shares as f64);
            total_mv += mv;
            positions.push(Position {
                ts_code: r.ts_code.clone(),
                shares: r.shares,
                avg_cost: r.avg_cost,
            });
        }
        Ok(AccountSnapshot {
            account_id: acct.account_id,
            cash: acct.cash,
            nav: (acct.cash + total_mv) / acct.initial_capital,
            total_market_value: total_mv,
            positions,
            as_of: Utc::now(),
        })
    }

    async fn submit(
        &self,
        date: NaiveDate,
        orders: &[OrderIntent],
    ) -> Result<Vec<Fill>, BrokerError> {
        let (mut shares, mut costs) = self.load_positions().await?;
        let old_shares = shares.clone();
        let mut cash = self.load_cash().await?;

        let fills = a_exec::execute_orders(orders, &mut shares, &mut cash, &self.quotes, &self.cost);

        // Update avg_cost using fills (weighted average for buys, unchanged for sells).
        for f in &fills {
            match f.side {
                Side::Buy => {
                    let prev_shares = old_shares.get(&f.ts_code).copied().unwrap_or(0);
                    let prev_cost = costs.get(&f.ts_code).copied().unwrap_or(0.0);
                    let total_shares = prev_shares + f.shares;
                    let new_cost = if total_shares > 0 {
                        (prev_cost * prev_shares as f64 + f.gross + f.fees) / total_shares as f64
                    } else { 0.0 };
                    costs.insert(f.ts_code.clone(), new_cost);
                }
                Side::Sell => {
                    if shares.get(&f.ts_code).copied().unwrap_or(0) <= 0 {
                        costs.remove(&f.ts_code);
                    }
                    // partial sells keep prior avg_cost (FIFO/avg unaffected)
                }
            }
        }

        self.persist_fills(date, cash, &shares, &costs, &old_shares, &fills).await?;
        info!("Submitted {} orders → {} fills (account {})",
              orders.len(), fills.len(), self.account_id);
        for f in &fills {
            debug!("  {} {} {} @ {:.4} (fees {:.2})",
                   f.side.as_str(), f.shares, f.ts_code, f.price, f.fees);
        }
        Ok(fills)
    }

    async fn rebalance(
        &self,
        date: NaiveDate,
        target_weights: &FxHashMap<String, f64>,
    ) -> Result<Vec<Fill>, BrokerError> {
        let (shares, _) = self.load_positions().await?;
        let cash = self.load_cash().await?;
        let total_value = a_exec::portfolio_value(&shares, &self.quotes, cash);
        let orders = a_exec::plan_orders(&shares, target_weights, total_value, &self.quotes, &self.cost);
        debug!("Rebalance plan: {} orders, total_value={:.2}", orders.len(), total_value);
        self.submit(date, &orders).await
    }
}

