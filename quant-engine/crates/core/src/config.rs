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
    pub database: DatabaseConfig,

    #[serde(default)]
    pub optimizer: OptimizerConfig,

    #[serde(default)]
    pub factor_processing: FactorProcessingConfig,

    #[serde(default)]
    pub category_weights: HashMap<String, f64>,

    #[serde(default)]
    pub a_share: AShareConfig,
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

/// Database connection parameters.
#[derive(Debug, Deserialize)]
pub struct DatabaseConfig {
    pub host: String,
    pub port: u16,
    pub user: String,
    pub password: String,
    pub database: String,
    pub schema: String,
    pub max_connections: u32,
}

impl Default for DatabaseConfig {
    fn default() -> Self {
        Self {
            host: std::env::var("DB_HOST").unwrap_or_default(),
            port: std::env::var("DB_PORT").ok().and_then(|s| s.parse().ok()).unwrap_or(3306),
            user: std::env::var("DB_USER").unwrap_or_default(),
            password: std::env::var("DB_PASSWORD").unwrap_or_default(),
            database: std::env::var("DB_DATABASE").unwrap_or_default(),
            schema: std::env::var("DB_SCHEMA").unwrap_or_else(|_| "quant".to_string()),
            max_connections: 10,
        }
    }
}

impl DatabaseConfig {
    /// Build a MySQL connection URL.
    pub fn url(&self) -> String {
        let host = if self.host.is_empty() {
            std::env::var("DB_HOST").unwrap_or_else(|_| "localhost".to_string())
        } else {
            self.host.clone()
        };
        let user = if self.user.is_empty() {
            std::env::var("DB_USER").unwrap_or_default()
        } else {
            self.user.clone()
        };
        let password = if self.password.is_empty() {
            std::env::var("DB_PASSWORD").unwrap_or_default()
        } else {
            self.password.clone()
        };
        let database = if self.database.is_empty() {
            std::env::var("DB_DATABASE").unwrap_or_default()
        } else {
            self.database.clone()
        };
        format!("mysql://{user}:{password}@{host}:{}/{}",
            self.port, database)
    }
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

/// A-share specific parameters (used when CLI `--market cn`).
#[derive(Debug, Deserialize, Default)]
pub struct AShareConfig {
    #[serde(default)]
    pub universe: AShareUniverseConfig,
    #[serde(default)]
    pub execution: AShareExecutionConfig,
    #[serde(default)]
    pub market_rules: AShareMarketRulesConfig,
    #[serde(default)]
    pub strategy: AShareStrategyConfig,
    #[serde(default)]
    pub risk_controls: AShareRiskControlsConfig,
    #[serde(default)]
    pub regime: AShareRegimeConfig,
    #[serde(default)]
    pub factor_processing: FactorProcessingConfig,
}

#[derive(Debug, Deserialize)]
pub struct AShareUniverseConfig {
    pub min_market_cap: f64,
    pub min_daily_turnover: f64,
    pub min_listing_days: i32,
    pub benchmark_index: String,
    pub exclude_st: bool,
    pub exclude_star_market: bool,
    pub exclude_chinext: bool,
    pub exclude_bse: bool,
}

#[derive(Debug, Deserialize)]
pub struct AShareExecutionConfig {
    pub initial_capital: f64,
    pub buy_commission: f64,
    pub sell_commission: f64,
    pub stamp_tax: f64,
    pub slippage: f64,
    pub min_commission: f64,
    pub lot_size: i64,
}

/// Per-board limit-up/down rules (昨收 ± pct).
#[derive(Debug, Clone, Deserialize)]
pub struct AShareMarketRulesConfig {
    pub main_board_limit: f64,    // 沪市主板 60xxxx / 深市主板 000xxx,002xxx
    pub chinext_limit: f64,        // 创业板 300xxx
    pub star_market_limit: f64,    // 科创板 688xxx
    pub bse_limit: f64,            // 北交所 8/4xxxxx
    pub st_limit: f64,             // ST/*ST
    pub delisting_limit: f64,      // 退市整理期
    pub one_char_tolerance: f64,   // 一字板检测容忍度
}

#[derive(Debug, Deserialize)]
pub struct AShareStrategyConfig {
    pub max_holdings: usize,
    pub long_n: usize,
    pub min_select_score: f64,
    pub weight_temperature: f64,
    pub rebalance_interval: usize,
    pub rebalance_min_interval: usize,
    pub min_valid_categories: usize,
    pub missing_factor_threshold: f64,
    pub missing_factor_max_penalty: f64,
    pub turnover_penalty_lambda: f64,
    pub max_single_weight: f64,
    pub max_industry_weight: f64,
    pub max_industry_group_weight: f64,
}

#[derive(Debug, Deserialize)]
pub struct AShareRiskControlsConfig {
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

#[derive(Debug, Deserialize)]
pub struct AShareRegimeConfig {
    pub enabled: bool,
    pub index: String,
    pub ma_window: usize,
    pub bear_holdings_ratio: f64,
    pub bear_overrides: HashMap<String, f64>,
    pub bull_overrides: HashMap<String, f64>,
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

impl Default for AShareUniverseConfig {
    fn default() -> Self {
        Self {
            min_market_cap: 3e9,
            min_daily_turnover: 5e7,
            min_listing_days: 180,
            benchmark_index: "000300.SH".to_string(),
            exclude_st: true,
            exclude_star_market: false,
            exclude_chinext: false,
            exclude_bse: false,
        }
    }
}

impl Default for AShareExecutionConfig {
    fn default() -> Self {
        Self {
            initial_capital: 1_000_000.0,
            buy_commission: 0.00075,
            sell_commission: 0.00075,
            stamp_tax: 0.001,
            slippage: 0.001,
            min_commission: 5.0,
            lot_size: 100,
        }
    }
}

impl Default for AShareMarketRulesConfig {
    fn default() -> Self {
        Self {
            main_board_limit: 0.10,
            chinext_limit: 0.20,
            star_market_limit: 0.20,
            bse_limit: 0.30,
            st_limit: 0.05,
            delisting_limit: 0.10,
            one_char_tolerance: 0.005,
        }
    }
}

impl Default for AShareStrategyConfig {
    fn default() -> Self {
        Self {
            max_holdings: 20,
            long_n: 15,
            min_select_score: 0.0,
            weight_temperature: 2.0,
            rebalance_interval: 10,
            rebalance_min_interval: 5,
            min_valid_categories: 4,
            missing_factor_threshold: 0.20,
            missing_factor_max_penalty: 0.5,
            turnover_penalty_lambda: 0.15,
            max_single_weight: 0.12,
            max_industry_weight: 0.20,
            max_industry_group_weight: 0.30,
        }
    }
}

impl Default for AShareRiskControlsConfig {
    fn default() -> Self {
        Self {
            enabled: true,
            use_vol_targeting: false,
            target_vol: 0.20,
            vol_lookback_days: 60,
            vol_scale_min: 0.4,
            vol_scale_max: 1.5,
            dd_start_threshold: 0.05,
            dd_max_threshold: 0.15,
            dd_min_position: 0.40,
            strategy_mom_window: 120,
            strategy_mom_min_scale: 0.6,
        }
    }
}

impl Default for AShareRegimeConfig {
    fn default() -> Self {
        let mut bear = HashMap::new();
        bear.insert("momentum".to_string(), 1.0);
        bear.insert("quality".to_string(), 1.5);
        bear.insert("growth".to_string(), 0.8);
        bear.insert("value".to_string(), 0.6);
        bear.insert("technical".to_string(), 0.8);

        let mut bull = HashMap::new();
        bull.insert("momentum".to_string(), 1.2);
        bull.insert("quality".to_string(), 0.9);
        bull.insert("growth".to_string(), 1.2);
        bull.insert("value".to_string(), 0.5);
        bull.insert("technical".to_string(), 0.6);

        Self {
            enabled: false,
            index: "000300.SH".to_string(),
            ma_window: 40,
            bear_holdings_ratio: 0.75,
            bear_overrides: bear,
            bull_overrides: bull,
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
            database: DatabaseConfig::default(),
            optimizer: OptimizerConfig::default(),
            factor_processing: FactorProcessingConfig::default(),
            category_weights,
            a_share: AShareConfig::default(),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_share_defaults_match_spec() {
        let cfg = AShareConfig::default();
        assert_eq!(cfg.universe.min_market_cap, 3e9);
        assert_eq!(cfg.universe.benchmark_index, "000300.SH");
        assert!(cfg.universe.exclude_st);
        assert_eq!(cfg.execution.stamp_tax, 0.001);
        assert_eq!(cfg.execution.lot_size, 100);
        assert_eq!(cfg.market_rules.main_board_limit, 0.10);
        assert_eq!(cfg.market_rules.chinext_limit, 0.20);
        assert_eq!(cfg.market_rules.st_limit, 0.05);
        assert_eq!(cfg.regime.index, "000300.SH");
        assert_eq!(cfg.regime.bear_overrides.get("quality"), Some(&1.5));
    }

    #[test]
    fn a_share_loads_from_repo_config() {
        let path = Path::new("../../config.toml");
        if !path.exists() {
            return; // skip if running outside repo layout
        }
        let cfg = Config::load(path).expect("load config.toml");
        assert_eq!(cfg.a_share.universe.benchmark_index, "000300.SH");
        assert_eq!(cfg.a_share.execution.buy_commission, 0.00075);
        assert_eq!(cfg.a_share.market_rules.bse_limit, 0.30);
        // US side untouched
        assert_eq!(cfg.universe.benchmark_index, "^GSPC");
        assert_eq!(cfg.execution.stamp_tax, 0.0);
    }
}
