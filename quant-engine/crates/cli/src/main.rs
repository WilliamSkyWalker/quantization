use std::path::PathBuf;

use clap::{Parser, Subcommand};
use rayon::prelude::*;
use tracing::info;
use tracing_subscriber::EnvFilter;

use quant_core::config::Config;
use quant_data::{builder, loader};

#[derive(Parser)]
#[command(name = "quant", about = "Quantitative Research System (Rust Engine)")]
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

        /// Path to cache directory.
        #[arg(long, default_value = "../cache")]
        cache_dir: PathBuf,

        /// Disable MVO optimizer, use TopN + Softmax.
        #[arg(long)]
        no_optimizer: bool,

        /// Disable short leg.
        #[arg(long)]
        no_short: bool,
    },

    /// Show database table row counts and connection status.
    DbStatus,

    /// Download data from external APIs into PostgreSQL.
    Download {
        /// Data source: fmp, quiver, fred.
        #[arg(long)]
        source: String,

        /// Target table: all, stock_list, daily_price, financial, index, etc.
        #[arg(long, default_value = "all")]
        target: String,

        /// Start year for historical data.
        #[arg(long, default_value = "1995")]
        start_year: i32,

        /// Incremental update (only fetch new data).
        #[arg(long)]
        incremental: bool,
    },

    /// Run factor analysis (IC / Fama-MacBeth / Decay).
    Analyze {
        #[arg(long)]
        start: String,

        #[arg(long)]
        end: String,

        /// Path to cache directory.
        #[arg(long, default_value = "../cache")]
        cache_dir: PathBuf,

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
        Commands::DbStatus => {
            cmd_db_status(&_config);
        }
        Commands::Download { source, target, start_year, incremental } => {
            cmd_download(&_config, &source, &target, start_year, incremental);
        }
        Commands::Backtest {
            start,
            end,
            output,
            cache_dir,
            no_optimizer,
            no_short,
        } => {
            cmd_backtest(&_config, &cache_dir, &start, &end, &output, no_short, no_optimizer);
        }
        Commands::Analyze {
            start,
            end,
            cache_dir,
            workers: _,
            output,
        } => {
            cmd_analyze(&cache_dir, &start, &end, &output);
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
            println!("  Rolling stats:     (merged into daily prices)");
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
    let factors = quant_factors::registry::all_factors();
    info!("{} factors registered", factors.len());

    let _proc_config = quant_factors::processor::ProcessConfig::default();

    println!("\n{:<20} {:>6} {:>10} {:>10} {:>10}   Top 5 / Bottom 5",
        "Factor", "N", "Mean", "Median", "Std");
    println!("{}", "-".repeat(100));

    for factor in &factors {
        let t0 = std::time::Instant::now();
        let raw = factor.compute(date, &cache);
        let _compute_ms = t0.elapsed().as_millis();

        // Process (winsorize + zscore, skip neutralize for now since sector_map is empty)
        let mut cfg = quant_factors::processor::ProcessConfig::default();
        cfg.do_neutralize = false; // TODO: need sector_map populated
        let _processed = quant_factors::processor::process_factor(
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
        let mut sorted_tickers: Vec<(quant_core::types::TickerId, f64)> =
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

fn cmd_analyze(
    cache_dir: &PathBuf,
    start_str: &str,
    end_str: &str,
    output_dir: &PathBuf,
) {
    let start = chrono::NaiveDate::parse_from_str(start_str, "%Y-%m-%d")
        .expect("Invalid start date");
    let end = chrono::NaiveDate::parse_from_str(end_str, "%Y-%m-%d")
        .expect("Invalid end date");

    info!("Loading data...");
    let cache = builder::build_cache_ranged(cache_dir, Some(start), Some(end))
        .expect("Failed to build DataCache");

    let factors = quant_factors::registry::all_factors();
    info!("{} factors registered", factors.len());

    // Determine monthly rebalance dates
    use chrono::Datelike;
    let rebalance_dates: Vec<chrono::NaiveDate> = {
        let mut dates = Vec::new();
        let mut last_ym = (0i32, 0u32);
        let trade_dates: Vec<_> = cache.trading_days.iter()
            .filter(|&&d| d >= start && d <= end)
            .copied()
            .collect();
        for &d in trade_dates.iter().rev() {
            let ym = (d.year(), d.month());
            if ym != last_ym {
                dates.push(d);
                last_ym = ym;
            }
        }
        dates.reverse();
        dates
    };
    info!("{} dates for analysis", rebalance_dates.len());

    // Build factor panel: date -> {factor_name -> {ticker -> value}}
    let proc_config = quant_factors::processor::ProcessConfig {
        do_neutralize: false, // IC analysis uses raw (non-neutralized) factors
        ..Default::default()
    };

    let t0 = std::time::Instant::now();
    let mut factor_panel: std::collections::HashMap<
        chrono::NaiveDate,
        std::collections::HashMap<String, rustc_hash::FxHashMap<quant_core::types::TickerId, f64>>,
    > = std::collections::HashMap::new();

    for (i, &date) in rebalance_dates.iter().enumerate() {
        // Parallel factor computation for each date
        let date_factors: std::collections::HashMap<String, rustc_hash::FxHashMap<quant_core::types::TickerId, f64>> = factors
            .par_iter()
            .filter_map(|f| {
                let raw = f.compute(date, &cache);
                if raw.is_empty() { return None; }
                let processed = quant_factors::processor::process_factor(
                    &raw,
                    &cache.sector_map,
                    &rustc_hash::FxHashMap::default(),
                    &proc_config,
                );
                if processed.is_empty() { None } else { Some((f.name().to_string(), processed)) }
            })
            .collect();
        factor_panel.insert(date, date_factors);

        if (i + 1) % 12 == 0 || i + 1 == rebalance_dates.len() {
            info!(
                "Panel {}/{} ({:.1}s)",
                i + 1,
                rebalance_dates.len(),
                t0.elapsed().as_secs_f64(),
            );
        }
    }

    let panel_time = t0.elapsed().as_secs_f64();
    info!("Factor panel: {:.1}s", panel_time);

    // Compute IC (1-month horizon = 21 trading days)
    info!("Computing IC (horizon=21d)...");
    let ic_summaries = quant_strategy::analysis::compute_ic_panel(&factor_panel, &cache, 21);

    // Print results
    println!("\n{}", "=".repeat(90));
    println!("FACTOR ANALYSIS: {} to {} ({} months)", start, end, rebalance_dates.len());
    println!("{}", "=".repeat(90));

    println!(
        "\n{:<30} {:>5} {:>9} {:>9} {:>7} {:>7} {:>6}",
        "Factor", "N", "Mean IC", "Std IC", "ICIR", "t-stat", "%Pos"
    );
    println!("{}", "-".repeat(90));

    for s in &ic_summaries {
        let star = if s.t_stat.abs() > 3.0 {
            "***"
        } else if s.t_stat.abs() > 2.0 {
            "**"
        } else {
            ""
        };
        println!(
            "{:<30} {:>5} {:>9.4} {:>9.4} {:>+7.3} {:>+7.2} {:>5.0}% {}",
            s.factor_name,
            s.n_months,
            s.mean_ic,
            s.std_ic,
            s.icir,
            s.t_stat,
            s.pct_positive * 100.0,
            star,
        );
    }

    // Fama-MacBeth regression
    info!("Computing Fama-MacBeth (horizon=21d)...");
    let fm_summaries = quant_strategy::analysis::fama_macbeth(&factor_panel, &cache, 21);

    println!("\n\n{:<30} {:>5} {:>12} {:>12} {:>8}",
        "Factor (FM)", "N", "Mean γ", "Std γ", "t-stat");
    println!("{}", "-".repeat(75));

    for s in &fm_summaries {
        let star = if s.t_stat.abs() > 3.0 { "***" }
            else if s.t_stat.abs() > 2.0 { "**" }
            else { "" };
        println!(
            "{:<30} {:>5} {:>12.6} {:>12.6} {:>+7.2} {}",
            s.factor_name, s.n_months, s.mean_gamma, s.std_gamma, s.t_stat, star,
        );
    }

    println!("\n> Harvey-Liu-Zhu (2016): |t| > 3.0 for statistical significance.");

    // Save to CSV
    std::fs::create_dir_all(output_dir).ok();
    let csv_path = output_dir.join(format!("ic_summary_{start}_{end}.csv"));
    let mut csv = String::from("factor,n_months,mean_ic,std_ic,icir,t_stat,pct_positive\n");
    for s in &ic_summaries {
        csv.push_str(&format!(
            "{},{},{:.6},{:.6},{:.6},{:.6},{:.4}\n",
            s.factor_name, s.n_months, s.mean_ic, s.std_ic, s.icir, s.t_stat, s.pct_positive,
        ));
    }
    std::fs::write(&csv_path, csv).ok();

    let fm_path = output_dir.join(format!("fama_macbeth_{start}_{end}.csv"));
    let mut fm_csv = String::from("factor,n_months,mean_gamma,std_gamma,t_stat\n");
    for s in &fm_summaries {
        fm_csv.push_str(&format!(
            "{},{},{:.8},{:.8},{:.4}\n",
            s.factor_name, s.n_months, s.mean_gamma, s.std_gamma, s.t_stat,
        ));
    }
    std::fs::write(&fm_path, fm_csv).ok();
    info!("Saved IC → {}, FM → {}", csv_path.display(), fm_path.display());
}

fn cmd_backtest(
    config: &quant_core::config::Config,
    cache_dir: &PathBuf,
    start_str: &str,
    end_str: &str,
    output_dir: &PathBuf,
    no_short: bool,
    no_optimizer: bool,
) {
    let start = chrono::NaiveDate::parse_from_str(start_str, "%Y-%m-%d")
        .expect("Invalid start date (expected YYYY-MM-DD)");
    let end = chrono::NaiveDate::parse_from_str(end_str, "%Y-%m-%d")
        .expect("Invalid end date (expected YYYY-MM-DD)");

    info!("Loading data...");
    let cache = builder::build_cache_ranged(cache_dir, Some(start), Some(end))
        .expect("Failed to build DataCache");

    // Get all registered factors
    let factors = quant_factors::registry::all_factors();
    info!("{} factors registered", factors.len());

    // Build factor category map
    let mut factor_categories = std::collections::HashMap::new();
    for f in &factors {
        factor_categories.insert(f.name().to_string(), f.category().to_string());
    }

    // Rolling IC state for dynamic factor weighting
    let mut rolling_ic = quant_strategy::rolling_ic::RollingIcState::new();

    // Determine rebalance dates (monthly: last trading day of each month)
    use chrono::Datelike;
    let rebalance_dates: Vec<chrono::NaiveDate> = {
        let mut dates = Vec::new();
        let mut last_ym = (0i32, 0u32);
        let trade_dates: Vec<_> = cache.trading_days.iter()
            .filter(|&&d| d >= start && d <= end)
            .copied()
            .collect();
        for &d in trade_dates.iter().rev() {
            let ym = (d.year(), d.month());
            if ym != last_ym {
                dates.push(d);
                last_ym = ym;
            }
        }
        dates.reverse();
        dates
    };
    info!("{} rebalance dates (monthly)", rebalance_dates.len());

    // Generate signals for each rebalance date
    let mut signals = std::collections::BTreeMap::new();
    let mut prev_holdings: rustc_hash::FxHashSet<quant_core::types::TickerId> = Default::default();

    // Category-specific neutralize modes (from Python config)
    let cat_neutralize_overrides = &config.factor_processing.category_neutralize_overrides;

    let universe_filter = quant_data::universe::UniverseFilter::default();

    let t0 = std::time::Instant::now();
    for (i, &date) in rebalance_dates.iter().enumerate() {
        // Get clean universe for this date
        let universe = quant_data::universe::get_clean_universe(date, &cache, &universe_filter);
        if universe.is_empty() {
            continue;
        }

        // Build market_cap map for universe tickers (needed for neutralization)
        let mktcap_map: rustc_hash::FxHashMap<quant_core::types::TickerId, f64> = universe.iter()
            .filter_map(|&tid| cache.get_market_cap(tid, date).map(|m| (tid, m)))
            .collect();

        // Compute all factors in parallel (rayon), filtered to universe
        let processed_factors: std::collections::HashMap<String, rustc_hash::FxHashMap<quant_core::types::TickerId, f64>> = factors
            .par_iter()
            .filter_map(|f| {
                let raw = f.compute(date, &cache);
                if raw.is_empty() { return None; }
                let filtered: rustc_hash::FxHashMap<quant_core::types::TickerId, f64> = raw
                    .into_iter()
                    .filter(|(tid, _)| universe.contains(tid))
                    .collect();
                if filtered.is_empty() { return None; }

                // Per-category neutralize mode
                let cat = f.category();
                let neut_mode = cat_neutralize_overrides
                    .get(cat)
                    .map(|s| s.as_str())
                    .unwrap_or(&config.factor_processing.neutralize_mode);

                let proc_cfg = quant_factors::processor::ProcessConfig {
                    do_winsorize: true,
                    do_neutralize: false, // Disabled: alpha comes from sector allocation, not stock selection
                    do_standardize: true,
                    mad_n: 5.0,
                    neutralize_mode: neut_mode.to_string(),
                    nonlinear_size: config.factor_processing.nonlinear_size,
                    standardize_mode: config.factor_processing.standardize_mode.clone(),
                };

                let processed = quant_factors::processor::process_factor(
                    &filtered,
                    &cache.sector_map,
                    &mktcap_map,
                    &proc_cfg,
                );
                if processed.is_empty() { None } else { Some((f.name().to_string(), processed)) }
            })
            .collect();

        // Update rolling IC weights (uses previous period's snapshot + current returns)
        let factor_weights_map = rolling_ic.update(date, &processed_factors, &cache);

        // Score
        let scores = quant_strategy::scoring::compute_scores(
            &processed_factors,
            &factor_weights_map,
            &factor_categories,
            &config.category_weights,
            config.strategy.min_valid_categories,
            config.strategy.missing_factor_threshold,
            config.strategy.missing_factor_max_penalty,
        );

        // === Regime detection (4-dimensional composite) ===
        let regime_state = quant_strategy::regime::detect(&cache, &config.regime, date);
        let regime_ratio = quant_strategy::regime::holdings_ratio(
            regime_state.strength, config.regime.bear_holdings_ratio,
        );

        // === Portfolio construction: MVO or Tiered ===
        let use_optimizer = config.optimizer.enabled && !no_optimizer;
        let mut combined: rustc_hash::FxHashMap<quant_core::types::TickerId, f64>;

        if use_optimizer {
            // MVO path: select top candidates first, then optimize
            // Python selects top 50 long + bottom 30 short + prev holdings ≈ 80 stocks
            let long_n = 50usize;
            let short_n = 30usize;

            // Sort by score descending
            let mut score_vec: Vec<(quant_core::types::TickerId, f64)> = scores
                .iter().map(|(&t, &s)| (t, s)).collect();
            score_vec.sort_by(|a, b| b.1.partial_cmp(&a.1).unwrap_or(std::cmp::Ordering::Equal));

            // Top long_n candidates + bottom short_n candidates + prev holdings
            let prev_weights: rustc_hash::FxHashMap<quant_core::types::TickerId, f64> = signals
                .values().next_back()
                .cloned()
                .unwrap_or_default();

            let mut candidate_set = rustc_hash::FxHashSet::default();
            for (tid, _) in score_vec.iter().take(long_n) {
                candidate_set.insert(*tid);
            }
            if !no_short {
                for (tid, _) in score_vec.iter().rev().take(short_n) {
                    candidate_set.insert(*tid);
                }
            }
            for &tid in prev_weights.keys() {
                candidate_set.insert(tid);
            }
            let candidate_tickers: Vec<quant_core::types::TickerId> =
                candidate_set.into_iter().collect();

            let (returns_opt, cov_tickers) = quant_strategy::optimizer::build_returns_matrix(
                &cache, date, &candidate_tickers,
                config.optimizer.cov_lookback, config.optimizer.min_history_days,
            );

            if let Some(returns) = returns_opt {
                let (cov, _shrinkage) = quant_strategy::optimizer::ledoit_wolf(&returns);

                // Only pass candidate scores (not full universe)
                let candidate_scores: rustc_hash::FxHashMap<quant_core::types::TickerId, f64> =
                    cov_tickers.iter()
                        .filter_map(|&tid| scores.get(&tid).map(|&s| (tid, s)))
                        .collect();

                let net_exp = if no_short { 1.0 } else { config.short.net_exposure };
                let mvo_result = quant_strategy::optimizer::optimize(
                    &candidate_scores, &cov, &cov_tickers, &prev_weights,
                    &cache.sector_map, &config.optimizer,
                    net_exp, !no_short,
                );
                combined = mvo_result.weights;
            } else {
                // Fallback to tiered if covariance estimation fails
                let tiered_config = quant_strategy::tiered::TieredConfig::default();
                combined = quant_strategy::tiered::select_tiered_portfolio(
                    date, &processed_factors, &factor_weights_map, &cache, &tiered_config,
                    &prev_holdings,
                );
            }
        } else {
            // Tiered portfolio: 60% large cap + 25% IPO + 15% small cap
            let tiered_config = quant_strategy::tiered::TieredConfig::default();
            combined = quant_strategy::tiered::select_tiered_portfolio(
                date, &processed_factors, &factor_weights_map, &cache, &tiered_config,
                &prev_holdings,
            );
        }

        // Update prev_holdings for next period's stickiness
        prev_holdings = combined.keys()
            .filter(|t| combined.get(t).map(|&w| w > 0.0).unwrap_or(false))
            .copied()
            .collect();

        // === Regime-adaptive position scaling ===
        // In bear markets, scale down all positions (both long and short)
        if regime_ratio < 0.99 {
            for v in combined.values_mut() {
                *v *= regime_ratio;
            }
        }

        // === Regime-adaptive short overlay (non-MVO path) ===
        if !no_short && !use_optimizer {
            let short_config = quant_strategy::short::ShortConfig::default();
            // Use regime strength directly instead of the old simple MA check
            let short_exposure = short_config.base_short_exposure * regime_state.strength.min(1.0);

            if short_exposure > 0.01 {
                // Scale long weights down to make room for shorts
                let long_total: f64 = combined.values().filter(|v| **v > 0.0).sum();
                let target_long = 1.0 - short_exposure;
                if long_total > 0.0 {
                    let scale = target_long / long_total;
                    for v in combined.values_mut() {
                        if *v > 0.0 { *v *= scale; }
                    }
                }

                let short_scores = quant_strategy::short::compute_short_scores(
                    &processed_factors, &scores, &cache, date, &short_config,
                );
                let short_weights = quant_strategy::short::select_short_portfolio(
                    &short_scores, &short_config, short_exposure,
                );
                combined.extend(short_weights);
            }
        }

        if !combined.is_empty() {
            signals.insert(date, combined);
        }

        if (i + 1) % 12 == 0 || i + 1 == rebalance_dates.len() {
            let elapsed = t0.elapsed().as_secs_f64();
            info!(
                "Signal {}/{}: {} scored, {} selected ({:.1}s total)",
                i + 1,
                rebalance_dates.len(),
                scores.len(),
                signals.get(&date).map(|s| s.len()).unwrap_or(0),
                elapsed,
            );
        }
    }

    let signal_time = t0.elapsed().as_secs_f64();
    info!("Signal generation: {:.1}s ({} signals)", signal_time, signals.len());

    // Run backtest
    let engine = quant_backtest::engine::BacktestEngine::from_config(config);
    let result = engine.run(&signals, &cache, start, end);

    // Print results
    println!("\n{}", "=".repeat(70));
    println!("BACKTEST RESULTS: {} to {}", start, end);
    println!("{}", "=".repeat(70));

    let s = &result.stats;
    println!("Total Return:       {:>10.2}%", s.total_return * 100.0);
    println!("Annual Return:      {:>10.2}%", s.annual_return * 100.0);
    println!("Annual Volatility:  {:>10.2}%", s.annual_volatility * 100.0);
    println!("Sharpe Ratio:       {:>10.2}", s.sharpe_ratio);
    println!("Max Drawdown:       {:>10.2}%", s.max_drawdown * 100.0);
    println!("Calmar Ratio:       {:>10.2}", s.calmar_ratio);
    println!("Win Rate:           {:>10.2}%", s.win_rate * 100.0);
    println!("Total Trades:       {:>10}", s.total_trades);
    println!("Annual Turnover:    {:>10.2}%", s.annual_turnover * 100.0);
    println!("Benchmark Return:   {:>10.2}%", s.benchmark_annual_return * 100.0);
    println!("Excess Return:      {:>10.2}%", s.excess_annual_return * 100.0);

    // Yearly breakdown: group NAV by year, compute year-end / year-start - 1
    if result.nav.len() > 100 {
        println!("\nYearly Returns:");
        println!("{:>6} {:>10} {:>10} {:>10}", "Year", "Strategy", "S&P 500", "Excess");
        println!("{}", "-".repeat(42));

        use chrono::Datelike;
        // Collect last NAV of each year
        let mut year_ends: std::collections::BTreeMap<i32, f64> = std::collections::BTreeMap::new();
        let mut bm_year_ends: std::collections::BTreeMap<i32, f64> = std::collections::BTreeMap::new();

        for &(date, nav) in &result.nav {
            year_ends.insert(date.year(), nav);
        }
        for &(date, nav) in &result.benchmark_nav {
            bm_year_ends.insert(date.year(), nav);
        }

        let years: Vec<i32> = year_ends.keys().copied().collect();
        for (i, &year) in years.iter().enumerate() {
            let nav_end = year_ends[&year];
            let nav_start = if i == 0 {
                result.nav[0].1
            } else {
                year_ends[&years[i - 1]]
            };

            let bm_end = bm_year_ends.get(&year).copied().unwrap_or(1.0);
            let bm_start = if i == 0 {
                result.benchmark_nav.first().map(|(_, n)| *n).unwrap_or(1.0)
            } else {
                bm_year_ends.get(&years[i - 1]).copied().unwrap_or(1.0)
            };

            let strat_ret = if nav_start > 0.0 { nav_end / nav_start - 1.0 } else { 0.0 };
            let bm_ret = if bm_start > 0.0 { bm_end / bm_start - 1.0 } else { 0.0 };

            println!(
                "{:>6} {:>9.2}% {:>9.2}% {:>9.2}%",
                year,
                strat_ret * 100.0,
                bm_ret * 100.0,
                (strat_ret - bm_ret) * 100.0,
            );
        }
    }

    // Capture ratios
    if result.nav.len() > 60 && result.benchmark_nav.len() > 60 {
        let cap = quant_backtest::ff5::capture_ratios(&result.nav, &result.benchmark_nav);
        println!("\nCapture Ratios (monthly):");
        println!("  Up Capture:       {:>10.2}%", cap.up_capture * 100.0);
        println!("  Down Capture:     {:>10.2}%", cap.down_capture * 100.0);
        println!("  Capture Ratio:    {:>10.2}", cap.capture_ratio);
        println!("  ({} up months, {} down months)", cap.n_up_months, cap.n_down_months);
    }

    // FF5 regression (if data file exists)
    let ff5_path = cache_dir.join("ff5_daily.csv");
    if ff5_path.exists() && result.nav.len() > 60 {
        let ff5_data = quant_backtest::ff5::load_ff5_csv(&ff5_path, true);
        if !ff5_data.is_empty() {
            let ff5_results = quant_backtest::ff5::analyze(&result.nav, &ff5_data, false);
            if let Some(full) = ff5_results.first() {
                println!("\nFama-French 5-Factor Regression:");
                println!("  Alpha (ann):      {:>10.2}% (t={:.2})", full.alpha_annualized * 100.0, full.alpha_t_stat);
                println!("  R²:               {:>10.3}", full.r_squared);
                for name in quant_backtest::ff5::FACTOR_NAMES {
                    if let Some(&b) = full.betas.get(*name) {
                        println!("  β_{:<12}    {:>10.3}", name, b);
                    }
                }
                println!("  Observations:     {:>10}", full.n_obs);
            }
        }
    }

    // Save NAV to CSV
    std::fs::create_dir_all(output_dir).ok();
    let nav_path = output_dir.join("nav.csv");
    let mut csv = String::from("date,nav,benchmark\n");
    for &(date, nav) in &result.nav {
        let bm = result.benchmark_nav.iter()
            .find(|(d, _)| *d == date)
            .map(|(_, n)| *n)
            .unwrap_or(f64::NAN);
        csv.push_str(&format!("{},{:.6},{:.6}\n", date, nav, bm));
    }
    std::fs::write(&nav_path, csv).ok();
    info!("NAV saved to {}", nav_path.display());
}

fn cmd_download(
    config: &quant_core::config::Config,
    source: &str,
    target: &str,
    start_year: i32,
    incremental: bool,
) {
    let db_url = config.database.url();
    let schema = &config.database.schema;
    if db_url.contains("@:/") || db_url.contains("postgres://:@") {
        eprintln!("Database not configured. Set DB_HOST, DB_USER, DB_PASSWORD, DB_DATABASE env vars.");
        std::process::exit(1);
    }

    let fmp_key = std::env::var("FMP_API_KEY").unwrap_or_default();
    if fmp_key.is_empty() && source == "fmp" {
        eprintln!("FMP_API_KEY not set.");
        std::process::exit(1);
    }

    let rt = tokio::runtime::Runtime::new().expect("Failed to create tokio runtime");
    rt.block_on(async {
        let pool = quant_db::pool::create_pool(&db_url, schema, 5).await
            .expect("Failed to connect to database");

        match source {
            "fmp" => {
                let rate_limit: u32 = std::env::var("FMP_RATE_LIMIT")
                    .ok().and_then(|s| s.parse().ok()).unwrap_or(300);
                let dl = quant_download::us_fmp::FmpDownloader::new(
                    fmp_key, pool.clone(), rate_limit,
                );

                match target {
                    "stock_list" => { dl.download_stock_list().await; }
                    "daily_price" => { dl.download_daily_prices(start_year, incremental).await; }
                    "financial" => { dl.download_financials().await; }
                    "index" => { dl.download_index_daily(start_year).await; }
                    "all" => {
                        dl.download_stock_list().await;
                        dl.download_index_daily(start_year).await;
                        dl.download_daily_prices(start_year, incremental).await;
                        dl.download_financials().await;
                    }
                    other => {
                        eprintln!("Unknown FMP target: {other}");
                        eprintln!("Available: stock_list, daily_price, financial, index, all");
                        std::process::exit(1);
                    }
                }
            }
            other => {
                eprintln!("Unknown source: {other}. Available: fmp");
                std::process::exit(1);
            }
        }

        pool.close().await;
    });
}

fn cmd_db_status(config: &quant_core::config::Config) {
    let db_url = config.database.url();
    let schema = &config.database.schema;
    if db_url.contains("@:/") || db_url.contains("postgres://:@") {
        eprintln!("Database not configured. Set DB_HOST, DB_USER, DB_PASSWORD, DB_DATABASE env vars.");
        std::process::exit(1);
    }

    info!("Connecting to database...");
    let rt = tokio::runtime::Runtime::new().expect("Failed to create tokio runtime");
    rt.block_on(async {
        let pool = match quant_db::pool::create_pool(&db_url, schema, 2).await {
            Ok(p) => p,
            Err(e) => {
                eprintln!("Failed to connect: {e}");
                std::process::exit(1);
            }
        };

        let tables = [
            "us_stock_basic", "us_daily_price", "us_financial_data", "us_key_metric",
            "us_index_daily", "us_industry_class", "us_enterprise_value",
            "us_analyst_recommendation", "us_earnings_surprise", "us_eps_estimate",
            "us_corporate_action", "us_insider_trade", "us_macro_indicator",
            "us_shares_float", "us_dark_pool_volume", "us_institutional_holder",
            "us_employee_count", "us_congress_trade", "us_gov_contract", "us_lobbying",
            "us_revenue_segment", "us_esg_rating", "us_company_profile",
            "us_sec_filing", "us_press_release", "us_news",
            "import_progress",
        ];

        println!("\n{:<35} {:>12}", "Table", "Rows");
        println!("{}", "-".repeat(49));

        let mut total = 0i64;
        for table in &tables {
            match quant_db::queries::us_read::count_rows(&pool, table).await {
                Ok(count) => {
                    println!("{:<35} {:>12}", table, count);
                    total += count;
                }
                Err(_) => {
                    println!("{:<35} {:>12}", table, "N/A");
                }
            }
        }
        println!("{}", "-".repeat(49));
        println!("{:<35} {:>12}", "TOTAL", total);

        pool.close().await;
    });
}

fn estimate_memory(cache: &quant_data::cache::DataCache) -> f64 {
    let mut bytes = 0usize;

    // Daily prices: PriceGrid flat Vec
    bytes += cache.daily_prices.memory_bytes();

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
