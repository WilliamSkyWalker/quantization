use std::path::PathBuf;

use clap::{Parser, Subcommand};
use tracing::info;
use tracing_subscriber::EnvFilter;

use qrs_core::config::Config;
use qrs_data::{builder, loader};

#[derive(Parser)]
#[command(name = "qrs", about = "Quantitative Research System (Rust Engine)")]
struct Cli {
    /// Config file path.
    #[arg(short, long, default_value = "config.toml")]
    config: PathBuf,

    /// Verbosity level (-v, -vv, -vvv).
    #[arg(short, long, action = clap::ArgAction::Count)]
    verbose: u8,

    #[command(subcommand)]
    command: Commands,
}

#[derive(Subcommand)]
enum Commands {
    /// Validate parquet cache files (schema + row counts).
    Validate {
        /// Path to cache directory.
        #[arg(long, default_value = "../cache")]
        cache_dir: PathBuf,
    },

    /// Load all cache files into DataCache and report stats.
    Load {
        /// Path to cache directory.
        #[arg(long, default_value = "../cache")]
        cache_dir: PathBuf,
    },

    /// Compute factor values for a single date.
    Factors {
        #[arg(long)]
        date: String,

        /// Output format: table, csv, json.
        #[arg(long, default_value = "table")]
        format: String,

        /// Filter by category.
        #[arg(long)]
        category: Option<String>,

        /// Path to cache directory.
        #[arg(long, default_value = "../cache")]
        cache_dir: PathBuf,
    },

    /// Compute composite scores for a single date.
    Score {
        #[arg(long)]
        date: String,

        /// Number of top stocks to show.
        #[arg(long, default_value = "30")]
        top: usize,
    },

    /// Run full backtest.
    Backtest {
        #[arg(long)]
        start: String,

        #[arg(long)]
        end: String,

        /// Output directory.
        #[arg(long, default_value = "../output/rust")]
        output: PathBuf,

        /// Disable MVO optimizer, use TopN + Softmax.
        #[arg(long)]
        no_optimizer: bool,

        /// Disable short leg.
        #[arg(long)]
        no_short: bool,
    },

    /// Run factor analysis (IC / Fama-MacBeth / Decay).
    Analyze {
        #[arg(long)]
        start: String,

        #[arg(long)]
        end: String,

        /// Number of worker threads.
        #[arg(long, default_value = "0")]
        workers: usize,

        /// Output directory.
        #[arg(long, default_value = "../output/factor_analysis")]
        output: PathBuf,
    },
}

fn main() {
    let cli = Cli::parse();

    // Setup tracing
    let filter = match cli.verbose {
        0 => "info",
        1 => "debug",
        _ => "trace",
    };
    tracing_subscriber::fmt()
        .with_env_filter(
            EnvFilter::try_from_default_env().unwrap_or_else(|_| EnvFilter::new(filter)),
        )
        .init();

    // Load config (use defaults if file doesn't exist)
    let _config = if cli.config.exists() {
        Config::load(&cli.config).unwrap_or_else(|e| {
            eprintln!("Warning: failed to load config: {e}, using defaults");
            Config::defaults()
        })
    } else {
        info!("Config file not found, using defaults");
        Config::defaults()
    };

    match cli.command {
        Commands::Validate { cache_dir } => {
            cmd_validate(&cache_dir);
        }
        Commands::Load { cache_dir } => {
            cmd_load(&cache_dir);
        }
        Commands::Factors {
            date,
            format: _,
            category: _,
            cache_dir,
        } => {
            cmd_factors(&cache_dir, &date);
        }
        Commands::Score { date, top } => {
            info!("TODO: score --date {date} --top {top}");
        }
        Commands::Backtest {
            start,
            end,
            output: _,
            no_optimizer: _,
            no_short: _,
        } => {
            info!("TODO: backtest --start {start} --end {end}");
        }
        Commands::Analyze {
            start,
            end,
            workers: _,
            output: _,
        } => {
            info!("TODO: analyze --start {start} --end {end}");
        }
    }
}

fn cmd_validate(cache_dir: &PathBuf) {
    info!("Validating cache directory: {}", cache_dir.display());

    match loader::validate_cache(cache_dir) {
        Ok(results) => {
            println!("\n{:<60} {:>10} {:>6}", "File", "Rows", "Cols");
            println!("{}", "-".repeat(78));

            let mut total_rows = 0usize;
            let mut total_files = 0usize;

            for (name, rows, cols) in &results {
                if *rows > 0 {
                    println!("{:<60} {:>10} {:>6}", name, rows, cols);
                    total_rows += rows;
                    total_files += 1;
                } else {
                    println!("{:<60} {:>10}", name, "FAILED");
                }
            }

            println!("{}", "-".repeat(78));
            println!("Total: {} files, {} rows", total_files, total_rows);
        }
        Err(e) => {
            eprintln!("Validation failed: {e}");
            std::process::exit(1);
        }
    }
}

fn cmd_load(cache_dir: &PathBuf) {
    info!("Loading all cache files into DataCache...");

    match builder::build_cache(cache_dir) {
        Ok(cache) => {
            println!("\nDataCache Summary:");
            println!("  Tickers:           {}", cache.ticker_interner.len());
            println!("  Trading days:      {}", cache.trading_days.len());
            println!("  Daily prices:      {}", cache.daily_prices.len());
            println!("  Rolling stats:     {}", cache.rolling_stats.len());
            println!("  Month-end prices:  {}", cache.month_end_prices.len());
            println!(
                "  Financials:        {} tickers, {} records",
                cache.financials.len(),
                cache.financials.values().map(|v| v.len()).sum::<usize>()
            );
            println!(
                "  Key metrics:       {} tickers, {} records",
                cache.key_metrics.len(),
                cache.key_metrics.values().map(|v| v.len()).sum::<usize>()
            );
            println!(
                "  Enterprise values: {} tickers",
                cache.enterprise_values.len()
            );
            println!("  Analyst recs:      {} tickers", cache.analyst_recs.len());
            println!(
                "  Earnings surp:     {} tickers",
                cache.earnings_surprises.len()
            );
            println!("  EPS estimates:     {} tickers", cache.eps_estimates.len());
            println!("  Dividends:         {} tickers", cache.dividends.len());
            println!("  Insider trades:    {} tickers", cache.insider_trades.len());

            // Show sample lookups
            if let Some(&first_day) = cache.trading_days.first() {
                println!("\n  First trading day:  {first_day}");
            }
            if let Some(&last_day) = cache.trading_days.last() {
                println!("  Last trading day:   {last_day}");
            }

            // Memory estimate
            let mem_mb = estimate_memory(&cache);
            println!("\n  Estimated memory:  ~{mem_mb:.0} MB");
        }
        Err(e) => {
            eprintln!("Failed to build DataCache: {e}");
            std::process::exit(1);
        }
    }
}

fn cmd_factors(cache_dir: &PathBuf, date_str: &str) {
    info!("Computing factors for {date_str}...");

    let cache = match builder::build_cache(cache_dir) {
        Ok(c) => c,
        Err(e) => {
            eprintln!("Failed to build DataCache: {e}");
            std::process::exit(1);
        }
    };

    let date = match chrono::NaiveDate::parse_from_str(date_str, "%Y-%m-%d") {
        Ok(d) => d,
        Err(e) => {
            eprintln!("Invalid date format (expected YYYY-MM-DD): {e}");
            std::process::exit(1);
        }
    };

    // Get all registered factors and compute
    let factors = qrs_factors::registry::all_factors();
    info!("{} factors registered", factors.len());

    let proc_config = qrs_factors::processor::ProcessConfig::default();

    println!("\n{:<20} {:>6} {:>10} {:>10} {:>10}   Top 5 / Bottom 5",
        "Factor", "N", "Mean", "Median", "Std");
    println!("{}", "-".repeat(100));

    for factor in &factors {
        let t0 = std::time::Instant::now();
        let raw = factor.compute(date, &cache);
        let compute_ms = t0.elapsed().as_millis();

        // Process (winsorize + zscore, skip neutralize for now since sector_map is empty)
        let mut cfg = qrs_factors::processor::ProcessConfig::default();
        cfg.do_neutralize = false; // TODO: need sector_map populated
        let processed = qrs_factors::processor::process_factor(
            &raw,
            &cache.sector_map,
            &rustc_hash::FxHashMap::default(), // mktcap map not needed without neutralize
            &cfg,
        );

        // Statistics on raw values
        let mut vals: Vec<f64> = raw.values().copied().collect();
        vals.sort_by(|a, b| a.partial_cmp(b).unwrap_or(std::cmp::Ordering::Equal));
        let n = vals.len();
        let mean = if n > 0 { vals.iter().sum::<f64>() / n as f64 } else { 0.0 };
        let median = if n > 0 {
            if n % 2 == 0 { (vals[n/2-1] + vals[n/2]) / 2.0 } else { vals[n/2] }
        } else { 0.0 };
        let std_dev = if n > 1 {
            (vals.iter().map(|v| (v - mean).powi(2)).sum::<f64>() / (n as f64 - 1.0)).sqrt()
        } else { 0.0 };

        // Top 5 / Bottom 5
        let mut sorted_tickers: Vec<(qrs_core::types::TickerId, f64)> =
            raw.iter().map(|(&t, &v)| (t, v)).collect();
        sorted_tickers.sort_by(|a, b| b.1.partial_cmp(&a.1).unwrap_or(std::cmp::Ordering::Equal));

        let top5: Vec<String> = sorted_tickers.iter().take(5)
            .map(|(tid, v)| format!("{}({:.3})", cache.ticker_interner.resolve(*tid), v))
            .collect();
        let bottom5: Vec<String> = sorted_tickers.iter().rev().take(5)
            .map(|(tid, v)| format!("{}({:.3})", cache.ticker_interner.resolve(*tid), v))
            .collect();

        println!(
            "{:<20} {:>6} {:>10.4} {:>10.4} {:>10.4}   T: {} | B: {}",
            factor.name(),
            n,
            mean,
            median,
            std_dev,
            top5.join(", "),
            bottom5.join(", "),
        );
    }
}

fn estimate_memory(cache: &qrs_data::cache::DataCache) -> f64 {
    let mut bytes = 0usize;

    // Daily prices: (TickerId + Date + PriceBar) per entry
    // PriceBar = 7 * 8 = 56 bytes, key = 4 + 4 = 8 bytes, overhead ~16
    bytes += cache.daily_prices.len() * 80;

    // Rolling stats: similar
    bytes += cache.rolling_stats.len() * 96;

    // Month-end prices: (TickerId + YearMonth + f64)
    bytes += cache.month_end_prices.len() * 24;

    // Financials: FinancialRecord has FxHashMap<String, f64>
    // Rough: 130 cols * 16 bytes per entry = ~2KB per record
    let fin_records: usize = cache.financials.values().map(|v| v.len()).sum();
    bytes += fin_records * 2000;

    // Key metrics: similar
    let km_records: usize = cache.key_metrics.values().map(|v| v.len()).sum();
    bytes += km_records * 1600;

    // Smaller tables
    bytes += cache.enterprise_values.values().map(|v| v.len()).sum::<usize>() * 32;
    bytes += cache.analyst_recs.values().map(|v| v.len()).sum::<usize>() * 80;
    bytes += cache.earnings_surprises.values().map(|v| v.len()).sum::<usize>() * 48;
    bytes += cache.eps_estimates.values().map(|v| v.len()).sum::<usize>() * 48;
    bytes += cache.dividends.values().map(|v| v.len()).sum::<usize>() * 16;
    bytes += cache.insider_trades.values().map(|v| v.len()).sum::<usize>() * 32;

    bytes as f64 / 1024.0 / 1024.0
}
