//! A-share trading layer — paper broker + risk gate, sharing execution
//! primitives with `quant-backtest::a_exec` so paper results match backtest.
//!
//! Live broker integrations (e.g. 掘金 GM) are out of scope here; expected
//! to land in a separate crate or via process-bridge later.

pub mod broker;
pub mod paper;
pub mod risk;
pub mod us_alpaca;
