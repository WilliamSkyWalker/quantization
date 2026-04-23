//! Error types for the quantitative research system.

use thiserror::Error;

#[derive(Error, Debug)]
pub enum QrsError {
    #[error("Config error: {0}")]
    Config(String),

    #[error("Data loading error: {0}")]
    DataLoad(String),

    #[error("Parquet file not found: {0}")]
    ParquetNotFound(String),

    #[error("Missing column '{column}' in table '{table}'")]
    MissingColumn { table: String, column: String },

    #[error("Factor computation error in {factor}: {message}")]
    FactorCompute { factor: String, message: String },

    #[error("Optimization failed: {0}")]
    Optimization(String),

    #[error("Backtest error: {0}")]
    Backtest(String),

    #[error("IO error: {0}")]
    Io(#[from] std::io::Error),
}

pub type Result<T> = std::result::Result<T, QrsError>;
