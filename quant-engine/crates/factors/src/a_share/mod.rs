//! A-share factor computation module.
//!
//! A-share factors use AShareCache (not US DataCache) because:
//! - Daily price includes valuation (pe_ttm, pb, turnover_rate) — US doesn't
//! - Financial data split into 4 tables (income/balance/cashflow/indicator) — US merges them
//! - Stock codes use ts_code format ("000001.SZ") — US uses ticker ("AAPL")
//!
//! Factors share the same category names as US for portfolio construction compatibility.

pub mod cache;
pub mod factors;
pub mod factors_v2;
pub mod universe;
