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
        Commands::Backtest {
            start,
            end,
            output,
            cache_dir,
            no_optimizer: _,
            no_short,
        } => {
            cmd_backtest(&_config, &cache_dir, &start, &end, &output, no_short);
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
            let rolling_count = cache.daily_prices.values().filter(|b| b.cum_ret_5d.is_finite()).count();
            println!("  Rolling stats:     {} (merged into daily prices)", rolling_count);
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

    let _proc_config = qrs_factors::processor::ProcessConfig::default();

    println!("\n{:<20} {:>6} {:>10} {:>10} {:>10}   Top 5 / Bottom 5",
        "Factor", "N", "Mean", "Median", "Std");
    println!("{}", "-".repeat(100));

    for factor in &factors {
        let t0 = std::time::Instant::now();
        let raw = factor.compute(date, &cache);
        let _compute_ms = t0.elapsed().as_millis();

        // Process (winsorize + zscore, skip neutralize for now since sector_map is empty)
        let mut cfg = qrs_factors::processor::ProcessConfig::default();
        cfg.do_neutralize = false; // TODO: need sector_map populated
        let _processed = qrs_factors::processor::process_factor(
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

    let factors = qrs_factors::registry::all_factors();
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
    let proc_config = qrs_factors::processor::ProcessConfig {
        do_neutralize: false,
        ..Default::default()
    };

    let t0 = std::time::Instant::now();
    let mut factor_panel: std::collections::HashMap<
        chrono::NaiveDate,
        std::collections::HashMap<String, rustc_hash::FxHashMap<qrs_core::types::TickerId, f64>>,
    > = std::collections::HashMap::new();

    for (i, &date) in rebalance_dates.iter().enumerate() {
        let mut date_factors = std::collections::HashMap::new();
        for f in &factors {
            let raw = f.compute(date, &cache);
            if raw.is_empty() {
                continue;
            }
            let processed = qrs_factors::processor::process_factor(
                &raw,
                &cache.sector_map,
                &rustc_hash::FxHashMap::default(),
                &proc_config,
            );
            if !processed.is_empty() {
                date_factors.insert(f.name().to_string(), processed);
            }
        }
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
    let ic_summaries = qrs_strategy::analysis::compute_ic_panel(&factor_panel, &cache, 21);

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
    info!("IC summary saved to {}", csv_path.display());
}

fn cmd_backtest(
    config: &qrs_core::config::Config,
    cache_dir: &PathBuf,
    start_str: &str,
    end_str: &str,
    output_dir: &PathBuf,
    no_short: bool,
) {
    let start = chrono::NaiveDate::parse_from_str(start_str, "%Y-%m-%d")
        .expect("Invalid start date (expected YYYY-MM-DD)");
    let end = chrono::NaiveDate::parse_from_str(end_str, "%Y-%m-%d")
        .expect("Invalid end date (expected YYYY-MM-DD)");

    info!("Loading data...");
    let cache = builder::build_cache_ranged(cache_dir, Some(start), Some(end))
        .expect("Failed to build DataCache");

    // Get all registered factors
    let factors = qrs_factors::registry::all_factors();
    info!("{} factors registered", factors.len());

    // Build factor category map
    let mut factor_categories = std::collections::HashMap::new();
    let mut factor_weights_map = std::collections::HashMap::new();
    for f in &factors {
        factor_categories.insert(f.name().to_string(), f.category().to_string());
        let w = if f.inherent_direction() == -1 { -1.0 } else { 1.0 };
        factor_weights_map.insert(f.name().to_string(), w);
    }

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
    let proc_config = qrs_factors::processor::ProcessConfig {
        do_neutralize: false, // Simplified: skip neutralize (no sector_map)
        ..Default::default()
    };

    let t0 = std::time::Instant::now();
    for (i, &date) in rebalance_dates.iter().enumerate() {
        // Compute all factors
        let mut processed_factors = std::collections::HashMap::new();
        for f in &factors {
            let raw = f.compute(date, &cache);
            if raw.is_empty() {
                continue;
            }
            let processed = qrs_factors::processor::process_factor(
                &raw,
                &cache.sector_map,
                &rustc_hash::FxHashMap::default(),
                &proc_config,
            );
            if !processed.is_empty() {
                processed_factors.insert(f.name().to_string(), processed);
            }
        }

        // Score
        let scores = qrs_strategy::scoring::compute_scores(
            &processed_factors,
            &factor_weights_map,
            &factor_categories,
            &config.category_weights,
            config.strategy.min_valid_categories,
            config.strategy.missing_factor_threshold,
            config.strategy.missing_factor_max_penalty,
        );

        // Select portfolio
        let short_enabled = config.short.enabled && !no_short;
        let (long_w, short_w) = qrs_strategy::scoring::select_portfolio(
            &scores,
            config.strategy.long_n,
            if short_enabled { config.short.short_n } else { 0 },
            short_enabled,
            config.short.net_exposure,
            config.strategy.weight_temperature,
        );

        // Merge weights
        let mut combined = long_w;
        combined.extend(short_w);

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
    let engine = qrs_backtest::engine::BacktestEngine::from_config(config);
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

    // Yearly breakdown
    if result.nav.len() > 252 {
        println!("\nYearly Returns:");
        println!("{:>6} {:>10} {:>10} {:>10}", "Year", "Strategy", "S&P 500", "Excess");
        println!("{}", "-".repeat(42));

        let mut year_start_nav = result.nav[0].1;
        let mut year_start_bm = result.benchmark_nav.first().map(|(_, n)| *n).unwrap_or(1.0);
        let mut last_year = result.nav[0].0.year();

        for &(date, nav) in &result.nav {
            if date.year() != last_year {
                // Print previous year
                let strat_ret = nav / year_start_nav - 1.0;
                // Find benchmark nav at this point
                let bm_nav = result.benchmark_nav.iter()
                    .rev()
                    .find(|(d, _)| d.year() == last_year)
                    .map(|(_, n)| *n)
                    .unwrap_or(year_start_bm);
                let bm_ret = bm_nav / year_start_bm - 1.0;

                println!(
                    "{:>6} {:>9.2}% {:>9.2}% {:>9.2}%",
                    last_year,
                    strat_ret * 100.0,
                    bm_ret * 100.0,
                    (strat_ret - bm_ret) * 100.0,
                );

                year_start_nav = nav;
                year_start_bm = bm_nav;
                last_year = date.year();
            }
        }
        // Print last year
        if let Some(&(_, last_nav)) = result.nav.last() {
            let strat_ret = last_nav / year_start_nav - 1.0;
            let bm_nav = result.benchmark_nav.last().map(|(_, n)| *n).unwrap_or(year_start_bm);
            let bm_ret = bm_nav / year_start_bm - 1.0;
            println!(
                "{:>6} {:>9.2}% {:>9.2}% {:>9.2}%",
                last_year,
                strat_ret * 100.0,
                bm_ret * 100.0,
                (strat_ret - bm_ret) * 100.0,
            );
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

fn estimate_memory(cache: &qrs_data::cache::DataCache) -> f64 {
    let mut bytes = 0usize;

    // Daily prices: (TickerId + Date + PriceBar) per entry
    // PriceBar = 7 * 8 = 56 bytes, key = 4 + 4 = 8 bytes, overhead ~16
    bytes += cache.daily_prices.len() * 80;

    // Rolling stats merged into daily_prices — no extra allocation

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
