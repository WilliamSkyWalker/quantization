pub mod a_strategy;
pub mod a_strategy_v2;
pub mod analysis;
pub mod optimizer;
pub mod rolling_ic;
pub mod scoring;
pub mod us_regime;
pub mod us_short;
pub mod us_tiered;

// Backwards-compat aliases (CLI 等外部代码仍引用旧路径)。
pub use us_regime as regime;
pub use us_short as short;
pub use us_tiered as tiered;
