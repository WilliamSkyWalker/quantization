//! Configuration for the quantitative research system.
//! Ported from Python `services/config.py` — US-relevant parameters only.

use serde::Deserialize;
use std::collections::HashMap;
use std::path::{Path, PathBuf};

use crate::error::{QrsError, Result};

/// Top-level configuration.
#[derive(Debug, Deserialize)]
pub struct Config {
    /// Path to parquet cache directory (relative to config file or absolute).
    #[serde(default = "default_cache_dir")]
    pub cache_dir: PathBuf,

    /// Path to output directory.
    #[serde(default = "default_output_dir")]
    pub output_dir: PathBuf,

    #[serde(default)]
    pub universe: UniverseConfig,

    #[serde(default)]
    pub strategy: StrategyConfig,

    #[serde(default)]
    pub risk_controls: RiskControlsConfig,

    #[serde(default)]
    pub execution: ExecutionConfig,

    #[serde(default)]
    pub regime: RegimeConfig,

    #[serde(default)]
    pub short: ShortConfig,

    #[serde(default)]
    pub optimizer: OptimizerConfig,

    #[serde(default)]
    pub factor_processing: FactorProcessingConfig,

    #[serde(default)]
    pub category_weights: HashMap<String, f64>,
}

/// Universe filtering parameters.
#[derive(Debug, Deserialize)]
pub struct UniverseConfig {
    pub min_market_cap: f64,
    pub min_daily_volume: f64,
    pub min_listing_days: i32,
    pub benchmark_index: String,
}

/// Strategy parameters.
#[derive(Debug, Deserialize)]
pub struct StrategyConfig {
    pub max_holdings: usize,
    pub long_n: usize,
    pub min_select_score: f64,
    pub weight_temperature: f64,
    pub rebalance_interval: usize,
    pub rebalance_min_interval: usize,
    pub min_valid_categories: usize,
    pub missing_factor_threshold: f64,
    pub missing_factor_max_penalty: f64,
}

/// Risk control parameters.
#[derive(Debug, Deserialize)]
pub struct RiskControlsConfig {
    pub enabled: bool,
    pub use_vol_targeting: bool,
    pub target_vol: f64,
    pub vol_lookback_days: usize,
    pub vol_scale_min: f64,
    pub vol_scale_max: f64,
    pub dd_start_threshold: f64,
    pub dd_max_threshold: f64,
    pub dd_min_position: f64,
    pub strategy_mom_window: usize,
    pub strategy_mom_min_scale: f64,
}

/// Execution / cost parameters.
#[derive(Debug, Deserialize)]
pub struct ExecutionConfig {
    pub initial_capital: f64,
    pub buy_commission: f64,
    pub sell_commission: f64,
    pub stamp_tax: f64,
    pub slippage: f64,
}

/// Regime detection parameters.
#[derive(Debug, Deserialize)]
pub struct RegimeConfig {
    pub enabled: bool,
    pub index: String,
    pub ma_window: usize,
    pub bear_holdings_ratio: f64,
    pub bear_overrides: HashMap<String, f64>,
    pub bull_overrides: HashMap<String, f64>,
}

/// Short-side parameters.
#[derive(Debug, Deserialize)]
pub struct ShortConfig {
    pub enabled: bool,
    pub short_n: usize,
    pub net_exposure: f64,
    pub regime_gate: f64,
    pub min_mcap: f64,
    pub min_volume: f64,
    pub stop_loss: f64,
    pub eps_rev_pct: f64,
    pub score_pct: f64,
    pub borrow_fee: f64,
    pub borrow_fee_tiers: HashMap<String, f64>,
    pub factor_weights: HashMap<String, f64>,
}

/// MVO optimizer parameters.
#[derive(Debug, Deserialize)]
pub struct OptimizerConfig {
    pub enabled: bool,
    pub risk_aversion: f64,
    pub turnover_penalty: f64,
    pub max_long_weight: f64,
    pub max_short_weight: f64,
    pub max_sector_gross: f64,
    pub cov_lookback: usize,
    pub min_history_days: usize,
    pub gross_leverage: f64,
}

/// Factor processing (winsorize / neutralize / standardize).
#[derive(Debug, Deserialize)]
pub struct FactorProcessingConfig {
    pub neutralize_mode: String,
    pub standardize_mode: String,
    pub nonlinear_size: bool,
    pub category_neutralize_overrides: HashMap<String, String>,
}

// === Defaults ===

fn default_cache_dir() -> PathBuf {
    PathBuf::from("../cache")
}

fn default_output_dir() -> PathBuf {
    PathBuf::from("../output")
}

impl Default for UniverseConfig {
    fn default() -> Self {
        Self {
            min_market_cap: 5e8,
            min_daily_volume: 1e6,
            min_listing_days: 180,
            benchmark_index: "^GSPC".to_string(),
        }
    }
}

impl Default for StrategyConfig {
    fn default() -> Self {
        Self {
            max_holdings: 25,
            long_n: 15,
            min_select_score: 0.0,
            weight_temperature: 1.5,
            rebalance_interval: 20,
            rebalance_min_interval: 20,
            min_valid_categories: 4,
            missing_factor_threshold: 0.20,
            missing_factor_max_penalty: 0.5,
        }
    }
}

impl Default for RiskControlsConfig {
    fn default() -> Self {
        Self {
            enabled: true,
            use_vol_targeting: true,
            target_vol: 0.16,
            vol_lookback_days: 60,
            vol_scale_min: 0.5,
            vol_scale_max: 1.5,
            dd_start_threshold: 0.10,
            dd_max_threshold: 0.25,
            dd_min_position: 0.50,
            strategy_mom_window: 120,
            strategy_mom_min_scale: 0.70,
        }
    }
}

impl Default for ExecutionConfig {
    fn default() -> Self {
        Self {
            initial_capital: 100_000.0,
            buy_commission: 0.0,
            sell_commission: 0.0,
            stamp_tax: 0.0,
            slippage: 0.0005,
        }
    }
}

impl Default for RegimeConfig {
    fn default() -> Self {
        Self {
            enabled: true,
            index: "^GSPC".to_string(),
            ma_window: 60,
            bear_holdings_ratio: 0.6,
            bear_overrides: HashMap::new(),
            bull_overrides: HashMap::new(),
        }
    }
}

impl Default for ShortConfig {
    fn default() -> Self {
        let mut factor_weights = HashMap::new();
        factor_weights.insert("EPS_REVISION".to_string(), 0.40);
        factor_weights.insert("ACCRUALS".to_string(), 0.25);
        factor_weights.insert("EARNINGS_SURPRISE".to_string(), 0.20);
        factor_weights.insert("INSIDER_NET_BUY".to_string(), 0.15);
        factor_weights.insert("BORROW_COST".to_string(), -0.10);

        let mut borrow_fee_tiers = HashMap::new();
        borrow_fee_tiers.insert("50e9".to_string(), 0.003);
        borrow_fee_tiers.insert("10e9".to_string(), 0.015);

        Self {
            enabled: true,
            short_n: 10,
            net_exposure: 0.6,
            regime_gate: 0.55,
            min_mcap: 1e10,
            min_volume: 5e7,
            stop_loss: 0.15,
            eps_rev_pct: 0.20,
            score_pct: 0.30,
            borrow_fee: 0.015,
            borrow_fee_tiers,
            factor_weights,
        }
    }
}

impl Default for OptimizerConfig {
    fn default() -> Self {
        Self {
            enabled: true,
            risk_aversion: 1.0,
            turnover_penalty: 0.005,
            max_long_weight: 0.15,
            max_short_weight: 0.05,
            max_sector_gross: 0.25,
            cov_lookback: 252,
            min_history_days: 120,
            gross_leverage: 1.0,
        }
    }
}

impl Default for FactorProcessingConfig {
    fn default() -> Self {
        let mut overrides = HashMap::new();
        overrides.insert("momentum".to_string(), "size_only".to_string());
        overrides.insert("macro".to_string(), "size_only".to_string());
        overrides.insert("analyst".to_string(), "size_only".to_string());
        overrides.insert("sentiment".to_string(), "none".to_string());

        Self {
            neutralize_mode: "full".to_string(),
            standardize_mode: "zscore".to_string(),
            nonlinear_size: false,
            category_neutralize_overrides: overrides,
        }
    }
}

impl Config {
    /// Load configuration from a TOML file.
    pub fn load(path: &Path) -> Result<Self> {
        let content = std::fs::read_to_string(path).map_err(|e| {
            QrsError::Config(format!("Failed to read config file {}: {}", path.display(), e))
        })?;
        let mut config: Config = toml::from_str(&content)
            .map_err(|e| QrsError::Config(format!("Failed to parse config: {e}")))?;

        // Apply default category weights if empty
        if config.category_weights.is_empty() {
            for cat in &[
                "value", "quality", "growth", "momentum", "technical", "macro", "analyst",
                "sentiment",
            ] {
                config.category_weights.insert(cat.to_string(), 1.0);
            }
        }

        Ok(config)
    }

    /// Create a Config with all defaults (no file needed).
    pub fn defaults() -> Self {
        let mut category_weights = HashMap::new();
        for cat in &[
            "value", "quality", "growth", "momentum", "technical", "macro", "analyst", "sentiment",
        ] {
            category_weights.insert(cat.to_string(), 1.0);
        }

        Self {
            cache_dir: default_cache_dir(),
            output_dir: default_output_dir(),
            universe: UniverseConfig::default(),
            strategy: StrategyConfig::default(),
            risk_controls: RiskControlsConfig::default(),
            execution: ExecutionConfig::default(),
            regime: RegimeConfig::default(),
            short: ShortConfig::default(),
            optimizer: OptimizerConfig::default(),
            factor_processing: FactorProcessingConfig::default(),
            category_weights,
        }
    }
}
