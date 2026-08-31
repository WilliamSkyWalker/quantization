use std::path::PathBuf;

use clap::{Parser, Subcommand, ValueEnum};
use rayon::prelude::*;
use tracing::{debug, info, warn};
use tracing_subscriber::EnvFilter;

use quant_core::config::Config;
use quant_data::{builder, loader};

#[derive(Clone, Copy, Debug, ValueEnum)]
enum Market {
    Us,
    Cn,
}

#[derive(Parser)]
#[command(name = "quant", about = "Quantitative Research System (Rust Engine)")]
struct Cli {
    /// Config file path.
    #[arg(short, long, default_value = "config.toml")]
    config: PathBuf,

    /// Market: us (default) or cn (A-share).
    #[arg(long, value_enum, default_value_t = Market::Us)]
    market: Market,

    /// Verbosity level (-v, -vv, -vvv).
    #[arg(short, long, action = clap::ArgAction::Count)]
    verbose: u8,

    #[command(subcommand)]
    command: Commands,
}

#[derive(Subcommand)]
enum AlpacaAction {
    /// 显示账户 + 当前持仓
    Status,

    /// 计算 rebalance plan（不下单），从 signals JSON 读 target weights
    Plan {
        #[arg(long)]
        signals: PathBuf,
    },

    /// 计算 plan + 提交 market 订单（market day-order）
    Run {
        #[arg(long)]
        signals: PathBuf,

        /// 不真正下单，仅打印 plan
        #[arg(long)]
        dry_run: bool,
    },

    /// 组合 NAV vs benchmark（默认 SPY）对比
    Compare {
        /// Alpaca portfolio_history period: 1D / 1W / 1M / 3M / 1A / all
        #[arg(long, default_value = "1M")]
        period: String,

        /// Benchmark ticker
        #[arg(long, default_value = "SPY")]
        benchmark: String,

        /// 可选：导出 CSV 路径（列：time / portfolio_nav / benchmark_nav / excess）
        #[arg(long)]
        csv: Option<PathBuf>,
    },
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

        /// Show per-category score breakdown.
        #[arg(long)]
        detail: bool,
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

        /// Export the last-rebalance target weights to JSON (for paper trading).
        /// Path is written under the `output` dir as `signals_<last_date>.json`.
        #[arg(long)]
        export_signals: bool,
    },

    /// Show database table row counts, latest data date, last update time,
    /// and per-ticker import progress.
    DbStatus {
        /// Filter by market: us | cn | all (default: all)
        #[arg(long, default_value = "all")]
        market: String,
    },

    /// Download data from external APIs into MySQL.
    Download {
        /// Data source: tushare, fmp, quiver, fred.
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

        /// Only process this ticker (for testing).
        #[arg(long)]
        ticker: Option<String>,
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

        /// Which A-share factor set to analyze: `v1` (frozen financial-driven,
        /// default) or `v2` (sentiment-driven, LHB/margin-based). Ignored for
        /// US market.
        #[arg(long, default_value = "v1")]
        factor_set: String,
    },

    /// 美股 Alpaca paper / live 交易
    Alpaca {
        #[command(subcommand)]
        action: AlpacaAction,
    },

    /// 把 MySQL 表导出到 parquet 缓存
    ExportParquet {
        /// 输出目录（默认 ../cache）
        #[arg(long, default_value = "../cache")]
        output_dir: PathBuf,

        /// 仅导出指定 MySQL 表（多次指定）。不传时导出所有 v25 baseline 需要的表。
        #[arg(long)]
        table: Vec<String>,
    },

    /// Paper trade — apply target weights via PaperBroker.
    /// Currently A-share only; requires --market cn.
    Trade {
        /// Account id (creates if absent).
        #[arg(long, default_value = "default")]
        account: String,

        /// Target signal date (YYYY-MM-DD). Quotes for THIS date drive execution.
        #[arg(long)]
        date: String,

        /// JSON file with target weights: {"ts_code": weight, ...} (sums to ≤ 1.0).
        #[arg(long)]
        signals: PathBuf,

        /// Plan + risk-check only; no DB writes.
        #[arg(long)]
        dry_run: bool,

        /// Skip risk gate (debugging only).
        #[arg(long)]
        no_risk: bool,
    },
}

fn main() {
    quant_core::env::load();

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
            match cli.market {
                Market::Us => cmd_factors(&cache_dir, &date),
                Market::Cn => cmd_a_factors(&_config, &date),
            }
        }
        Commands::Score { date, top, detail } => {
            match cli.market {
                Market::Us => info!("Score not implemented for US market"),
                Market::Cn => cmd_a_score(&_config, &date, top, detail),
            }
        }
        Commands::DbStatus { market } => {
            cmd_db_status(&_config, &market);
        }
        Commands::Download { source, target, start_year, incremental, ticker } => {
            cmd_download(&_config, &source, &target, start_year, incremental, ticker.as_deref());
        }
        Commands::Backtest {
            start,
            end,
            output,
            cache_dir,
            no_optimizer,
            no_short,
            export_signals,
        } => {
            cmd_backtest(&_config, cli.market, &cache_dir, &start, &end, &output, no_short, no_optimizer, export_signals);
        }
        Commands::Analyze {
            start,
            end,
            cache_dir,
            workers: _,
            output,
            factor_set,
        } => {
            match cli.market {
                Market::Cn => {
                    let rt = tokio::runtime::Runtime::new().expect("tokio runtime");
                    rt.block_on(cmd_analyze_cn(&_config, &start, &end, &output, &factor_set));
                }
                Market::Us => cmd_analyze(&cache_dir, &start, &end, &output),
            }
        }
        Commands::Trade { account, date, signals, dry_run, no_risk } => {
            match cli.market {
                Market::Us => {
                    eprintln!("trade: 美股请用 `quant alpaca`，不要用 trade");
                    std::process::exit(1);
                }
                Market::Cn => cmd_a_trade(&_config, &account, &date, &signals, dry_run, no_risk),
            }
        }
        Commands::Alpaca { action } => {
            cmd_alpaca(action);
        }
        Commands::ExportParquet { output_dir, table } => {
            cmd_export_parquet(&_config, &output_dir, &table);
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

fn cmd_a_factors(config: &Config, date_str: &str) {
    let date = match chrono::NaiveDate::parse_from_str(date_str, "%Y-%m-%d") {
        Ok(d) => d,
        Err(e) => {
            eprintln!("Invalid date format (expected YYYY-MM-DD): {e}");
            std::process::exit(1);
        }
    };

    let db_url = config.database.url();
    let schema = &config.database.schema;
    if db_url.contains("@:/") || db_url.contains("mysql://:@") {
        eprintln!("Database not configured. Set DB_HOST, DB_USER, DB_PASSWORD, DB_DATABASE env vars.");
        std::process::exit(1);
    }

    info!("Building A-share cache from DB...");
    let rt = tokio::runtime::Runtime::new().expect("Failed to create tokio runtime");
    let cache = rt.block_on(async {
        let pool = quant_db::pool::create_pool(&db_url, schema, 8).await
            .expect("Failed to connect to database");

        // Load 4 years back: needed for CAGR_3Y (~13 quarters) and momentum (240 days)
        let load_start = date - chrono::Duration::days(365 * 4);
        let load_end = date + chrono::Duration::days(1);

        let cache = build_a_share_cache(&pool, load_start, load_end).await;
        pool.close().await;
        cache
    });

    info!(
        "AShareCache: {} stocks, {} financials, {} industries, {} basics, {} trading days",
        cache.daily.len(),
        cache.financials.len(),
        cache.industry.len(),
        cache.basics.len(),
        cache.trading_days.len(),
    );

    // Build clean universe — factor outputs are intersected with this set.
    let clean_universe = {
        use quant_factors::a_share::universe::{AUniverseFilter, get_a_clean_universe};
        let filter = AUniverseFilter::from_config(&config.a_share.universe);
        let clean = get_a_clean_universe(date, &cache, &filter);
        info!("Clean universe on {date}: {}/{}", clean.len(), cache.ts_codes.len());
        clean
    };

    let factors = quant_factors::a_share::factors::all_factors();
    info!("{} A-share factors", factors.len());

    println!(
        "\n{:<22} {:<11} {:>5} {:>11} {:>11} {:>11}   Top 5 / Bottom 5",
        "Factor", "Category", "N", "Mean", "Median", "Std",
    );
    println!("{}", "-".repeat(120));

    for f in &factors {
        let raw_all = (f.compute)(date, &cache);
        // Intersect factor output with clean universe so stats reflect tradeable names only.
        let raw: rustc_hash::FxHashMap<String, f64> = raw_all.into_iter()
            .filter(|(code, _)| clean_universe.contains(code))
            .collect();
        let mut vals: Vec<f64> = raw.values().copied().collect();
        vals.sort_by(|a, b| a.partial_cmp(b).unwrap_or(std::cmp::Ordering::Equal));
        let n = vals.len();
        let mean = if n > 0 { vals.iter().sum::<f64>() / n as f64 } else { 0.0 };
        let median = if n > 0 {
            if n % 2 == 0 { (vals[n / 2 - 1] + vals[n / 2]) / 2.0 } else { vals[n / 2] }
        } else { 0.0 };
        let std_dev = if n > 1 {
            (vals.iter().map(|v| (v - mean).powi(2)).sum::<f64>() / (n as f64 - 1.0)).sqrt()
        } else { 0.0 };

        let mut sorted: Vec<(String, f64)> =
            raw.iter().map(|(k, &v)| (k.clone(), v)).collect();
        sorted.sort_by(|a, b| b.1.partial_cmp(&a.1).unwrap_or(std::cmp::Ordering::Equal));
        let top5: Vec<String> = sorted.iter().take(5)
            .map(|(c, v)| format!("{}({:.3})", c, v)).collect();
        let bottom5: Vec<String> = sorted.iter().rev().take(5)
            .map(|(c, v)| format!("{}({:.3})", c, v)).collect();

        println!(
            "{:<22} {:<11} {:>5} {:>11.4} {:>11.4} {:>11.4}   T: {} | B: {}",
            f.name, f.category, n, mean, median, std_dev,
            top5.join(", "),
            bottom5.join(", "),
        );
    }
}

fn cmd_a_score(config: &Config, date_str: &str, top_n: usize, detail: bool) {
    let date = match chrono::NaiveDate::parse_from_str(date_str, "%Y-%m-%d") {
        Ok(d) => d,
        Err(e) => {
            eprintln!("Invalid date format (expected YYYY-MM-DD): {e}");
            std::process::exit(1);
        }
    };

    let db_url = config.database.url();
    let schema = &config.database.schema;
    if db_url.contains("@:/") || db_url.contains("mysql://:@") {
        eprintln!("Database not configured. Set DB_HOST, DB_USER, DB_PASSWORD, DB_DATABASE env vars.");
        std::process::exit(1);
    }

    info!("Building A-share cache from DB...");
    let rt = tokio::runtime::Runtime::new().expect("Failed to create tokio runtime");
    let cache = rt.block_on(async {
        let pool = quant_db::pool::create_pool(&db_url, schema, 8).await
            .expect("Failed to connect to database");
        let load_start = date - chrono::Duration::days(365 * 4);
        let load_end = date + chrono::Duration::days(1);
        let cache = build_a_share_cache(&pool, load_start, load_end).await;
        pool.close().await;
        cache
    });

    info!(
        "AShareCache: {} stocks, {} trading days",
        cache.daily.len(),
        cache.trading_days.len(),
    );

    // Build clean universe
    let clean_universe = {
        use quant_factors::a_share::universe::{AUniverseFilter, get_a_clean_universe};
        let filter = AUniverseFilter::from_config(&config.a_share.universe);
        let clean = get_a_clean_universe(date, &cache, &filter);
        info!("Clean universe on {date}: {}/{}", clean.len(), cache.ts_codes.len());
        clean
    };

    // Regime detection
    let regime_cfg = &config.a_share.regime;
    let regime_overrides = if regime_cfg.enabled {
        let strength = quant_strategy::a_strategy::detect_a_regime_public(
            &cache, &regime_cfg.index, date, regime_cfg.ma_window,
        );
        let is_bear = strength < 0.3;
        info!("Regime strength: {strength:.2} ({})", if is_bear { "bear" } else { "bull/neutral" });
        if is_bear {
            Some(regime_cfg.bear_overrides.clone())
        } else {
            None
        }
    } else {
        None
    };

    if detail {
        // Compute scores with category breakdown
        let detailed = quant_strategy::a_strategy::compute_scores_detail(
            date,
            &cache,
            Some(&clean_universe),
            &config.a_share.strategy,
            regime_overrides.as_ref(),
        );

        info!("Scored {} stocks (detail mode)", detailed.len());

        // Select top-N by total score
        let mut sorted: Vec<(&str, f64, &rustc_hash::FxHashMap<String, f64>)> = detailed.iter()
            .filter(|(_, (score, _))| *score >= config.a_share.strategy.min_select_score && score.is_finite())
            .map(|(k, (score, cats))| (k.as_str(), *score, cats))
            .collect();
        sorted.sort_by(|a, b| b.1.total_cmp(&a.1));
        sorted.truncate(top_n);

        if sorted.is_empty() {
            println!("\nNo stocks passed the selection criteria on {date}");
            return;
        }

        let weight = 1.0 / sorted.len() as f64;

        println!("\n=== A股多因子选股 — {date} (详细模式) ===");
        println!("选股数: {} | 等权权重: {:.2}% | Regime: {}\n",
            sorted.len(),
            weight * 100.0,
            regime_overrides.as_ref().map_or("bull/neutral", |_| "bear"),
        );
        println!("{:<4} {:<12} {:<10} {:>8} {:>7} {:>7} {:>7} {:>7} {:>7} {:>7} {:>7}",
            "#", "代码", "名称", "总分", "value", "quality", "growth", "momentum", "tech", "macro", "sent");
        println!("{}", "-".repeat(100));

        for (i, (code, score, cats)) in sorted.iter().enumerate() {
            let name = cache.basics.get(*code)
                .map(|b| b.name.as_str()).unwrap_or("?");
            let v = cats.get("value").unwrap_or(&0.0);
            let q = cats.get("quality").unwrap_or(&0.0);
            let g = cats.get("growth").unwrap_or(&0.0);
            let m = cats.get("momentum").unwrap_or(&0.0);
            let t = cats.get("technical").unwrap_or(&0.0);
            let mc = cats.get("macro").unwrap_or(&0.0);
            let s = cats.get("sentiment").unwrap_or(&0.0);
            println!("{:<4} {:<12} {:<10} {:>+8.4} {:>+7.3} {:>+7.3} {:>+7.3} {:>+7.3} {:>+7.3} {:>+7.3} {:>+7.3}",
                i + 1, code, name, score, v, q, g, m, t, mc, s);
        }
        println!("{}", "-".repeat(100));
        println!("合计 {} 只股票", sorted.len());
    } else {
        // Original simple mode
        let scores = quant_strategy::a_strategy::compute_scores(
            date,
            &cache,
            Some(&clean_universe),
            &config.a_share.strategy,
            regime_overrides.as_ref(),
        );

        info!("Scored {} stocks", scores.len());

        let portfolio = quant_strategy::a_strategy::select_portfolio(
            &scores,
            top_n,
            config.a_share.strategy.min_select_score,
        );

        if portfolio.is_empty() {
            println!("\nNo stocks passed the selection criteria on {date}");
            return;
        }

        let mut sorted: Vec<(&str, f64)> = scores.iter()
            .filter(|(code, _)| portfolio.contains_key(*code))
            .map(|(k, &v)| (k.as_str(), v))
            .collect();
        sorted.sort_by(|a, b| b.1.total_cmp(&a.1));

        let weight = 1.0 / sorted.len() as f64;

        println!("\n=== A股多因子选股 — {date} ===");
        println!("选股数: {} | 等权权重: {:.2}% | Regime: {}\n",
            sorted.len(),
            weight * 100.0,
            regime_overrides.as_ref().map_or("bull/neutral", |_| "bear"),
        );
        println!("{:<4} {:<12} {:<10} {:>8} {:>12}", "#", "代码", "名称", "得分", "行业");
        println!("{}", "-".repeat(56));

        for (i, (code, score)) in sorted.iter().enumerate() {
            let name = cache.basics.get(*code)
                .map(|b| b.name.as_str()).unwrap_or("?");
            let industry = cache.industry_on(code, date)
                .map(|i| if i.industry_name.is_empty() { "?" } else { i.industry_name.as_str() })
                .unwrap_or("?");
            println!("{:<4} {:<12} {:<10} {:>+8.4} {:>12}", i + 1, code, name, score, industry);
        }
        println!("{}", "-".repeat(56));
        println!("合计 {} 只股票", sorted.len());
    }
}

async fn build_a_share_cache(
    pool: &sqlx::MySqlPool,
    start: chrono::NaiveDate,
    end: chrono::NaiveDate,
) -> quant_factors::a_share::cache::AShareCache {
    use quant_factors::a_share::cache::{AShareCache, ABar, AFinIndicator, AIndustry, AStockInfo, ALhbDay, AMarginDay};
    use rustc_hash::FxHashMap;

    info!("Loading a_daily_price [{}, {}]...", start, end);
    let prices = quant_db::queries::a_read::get_a_daily_prices(pool, start, end).await
        .expect("Failed to load a_daily_price");
    info!("Loaded {} price rows", prices.len());

    let mut daily: FxHashMap<String, Vec<(chrono::NaiveDate, ABar)>> = FxHashMap::default();
    for p in prices {
        let bar = ABar {
            open: p.open.unwrap_or(f64::NAN),
            high: p.high.unwrap_or(f64::NAN),
            low: p.low.unwrap_or(f64::NAN),
            close: p.close.unwrap_or(f64::NAN),
            pre_close: p.pre_close.unwrap_or(f64::NAN),
            pct_chg: p.pct_chg.unwrap_or(f64::NAN),
            vol: p.vol.unwrap_or(0.0),
            amount: p.amount.unwrap_or(0.0),
            adj_factor: p.adj_factor.unwrap_or(1.0),
            turnover_rate: p.turnover_rate.unwrap_or(f64::NAN),
            pe_ttm: p.pe_ttm.unwrap_or(f64::NAN),
            pb: p.pb.unwrap_or(f64::NAN),
            ps_ttm: p.ps_ttm.unwrap_or(f64::NAN),
            dv_ttm: p.dv_ttm.unwrap_or(f64::NAN),
            total_mv: p.total_mv.unwrap_or(f64::NAN),
            circ_mv: p.circ_mv.unwrap_or(f64::NAN),
        };
        daily.entry(p.ts_code).or_default().push((p.trade_date, bar));
    }
    for v in daily.values_mut() {
        v.sort_by_key(|(d, _)| *d);
    }

    info!("Loading a_financial_indicator [{}, {}]...", start, end);
    let fin_rows = quant_db::queries::a_read::get_a_financial_indicators(pool, start, end).await
        .expect("Failed to load a_financial_indicator");
    info!("Loaded {} financial rows", fin_rows.len());

    let mut financials: FxHashMap<String, Vec<AFinIndicator>> = FxHashMap::default();
    for r in fin_rows {
        let fin = AFinIndicator {
            end_date: r.end_date.format("%Y%m%d").to_string(),
            ann_date: r.ann_date.map(|d| d.format("%Y%m%d").to_string()).unwrap_or_default(),
            eps: r.eps.unwrap_or(f64::NAN),
            bps: r.bps.unwrap_or(f64::NAN),
            roe: r.roe.unwrap_or(f64::NAN),
            gross_margin: r.grossprofit_margin.unwrap_or(f64::NAN),
            netprofit_margin: r.netprofit_margin.unwrap_or(f64::NAN),
            q_profit_yoy: r.q_profit_yoy.unwrap_or(f64::NAN),
            q_sales_yoy: r.q_sales_yoy.unwrap_or(f64::NAN),
            q_netprofit_yoy: r.q_netprofit_yoy.unwrap_or(f64::NAN),
            netprofit_yoy: r.netprofit_yoy.unwrap_or(f64::NAN),
            current_ratio: r.current_ratio.unwrap_or(f64::NAN),
            ocf_to_profit: r.ocf_to_profit.unwrap_or(f64::NAN),
            roa: r.roa.unwrap_or(f64::NAN),
            quick_ratio: r.quick_ratio.unwrap_or(f64::NAN),
            assets_turn: r.assets_turn.unwrap_or(f64::NAN),
            debt_to_assets: r.debt_to_assets.unwrap_or(f64::NAN),
        };
        financials.entry(r.ts_code).or_default().push(fin);
    }
    // get_latest_fin/get_fin_history scan front-to-back expecting most-recent first.
    for v in financials.values_mut() {
        v.sort_by(|a, b| b.end_date.cmp(&a.end_date));
    }

    info!("Loading A-share industry membership intervals (SW2021 L1)...");
    let inds = quant_db::queries::a_read::get_a_industry_class(pool).await
        .expect("Failed to load a_industry_class");
    info!("Loaded {} industry membership intervals", inds.len());
    let mut industry: FxHashMap<String, Vec<AIndustry>> = FxHashMap::default();
    for row in inds {
        let in_date = match row.in_date.as_deref()
            .and_then(|value| parse_industry_date(value, "in_date", &row.ts_code))
        {
            Some(date) => date,
            None => {
                warn!("Skipping industry record for {} without a valid in_date", row.ts_code);
                continue;
            }
        };
        let out_date = match row.out_date.as_deref() {
            Some(value) => match parse_industry_date(value, "out_date", &row.ts_code) {
                Some(date) => Some(date),
                None => {
                    warn!("Skipping industry record for {} with an invalid out_date", row.ts_code);
                    continue;
                }
            },
            None => None,
        };
        industry.entry(row.ts_code).or_default().push(AIndustry {
            index_code: row.index_code.unwrap_or_default(),
            industry_name: row.industry_name.unwrap_or_default(),
            in_date: Some(in_date),
            out_date,
        });
    }
    for memberships in industry.values_mut() {
        memberships.sort_by_key(|membership| membership.in_date);
    }

    info!("Loading a_trade_cal (SSE) [{}, {}]...", start, end);
    let cal = quant_db::queries::a_read::get_a_trade_cal(pool, "SSE", start, end).await
        .expect("Failed to load a_trade_cal");
    let mut trading_days: Vec<chrono::NaiveDate> =
        cal.into_iter().map(|c| c.cal_date).collect();
    trading_days.sort();
    info!("Loaded {} trading days", trading_days.len());

    info!("Loading a_stock_basic (incl. delisted)...");
    let basic_rows = quant_db::queries::a_read::get_all_a_stocks(pool).await
        .expect("Failed to load a_stock_basic");
    info!("Loaded {} stock basics", basic_rows.len());
    let basics: FxHashMap<String, AStockInfo> = basic_rows.into_iter()
        .map(|r| (r.ts_code.clone(), AStockInfo {
            name: r.name.unwrap_or_default(),
            list_date: r.list_date,
            delist_date: r.delist_date,
            is_st: r.is_st != 0,
            board: r.board,
            total_share: r.total_share,
            free_share: r.free_share,
        }))
        .collect();

    let ts_codes: Vec<String> = daily.keys().cloned().collect();

    info!("Loading a_index_daily (000300.SH for BAB_BETA)...");
    let mut index_prices: FxHashMap<String, Vec<(chrono::NaiveDate, f64)>> = FxHashMap::default();
    match quant_db::queries::a_read::get_a_index_daily(pool, "000300.SH", start, end).await {
        Ok(rows) => {
            let mut pairs: Vec<(chrono::NaiveDate, f64)> = rows.into_iter()
                .filter_map(|r| r.close.map(|c| (r.trade_date, c)))
                .collect();
            pairs.sort_by_key(|(d, _)| *d);
            if !pairs.is_empty() {
                info!("Loaded {} CSI 300 index prices", pairs.len());
                index_prices.insert("000300.SH".to_string(), pairs);
            }
        }
        Err(e) => { warn!("Failed to load index prices: {e}"); }
    }

    info!("Loading a_top_list (龙虎榜每日交易明细) [{}, {}]...", start, end);
    let mut top_list: FxHashMap<String, Vec<(chrono::NaiveDate, ALhbDay)>> = FxHashMap::default();
    match quant_db::queries::a_read::get_a_top_list(pool, start, end).await {
        Ok(rows) => {
            info!("Loaded {} a_top_list rows", rows.len());
            // Aggregate same-day multi-reason rows: sum net_amount/l_buy/l_sell/amount,
            // amount-weighted mean of net_rate (net_rate is already a ratio; summing
            // ratios across reasons would double-count, so weight by each row's amount).
            let mut by_day: FxHashMap<(String, chrono::NaiveDate), (f64, f64, f64, f64, f64)> = FxHashMap::default();
            for r in rows {
                let key = (r.ts_code.clone(), r.trade_date);
                let net_amount = r.net_amount.unwrap_or(0.0);
                let l_buy = r.l_buy.unwrap_or(0.0);
                let l_sell = r.l_sell.unwrap_or(0.0);
                let amount = r.amount.unwrap_or(0.0);
                let net_rate = r.net_rate.unwrap_or(f64::NAN);
                let entry = by_day.entry(key).or_insert((0.0, 0.0, 0.0, 0.0, 0.0));
                entry.0 += net_amount;
                entry.1 += l_buy;
                entry.2 += l_sell;
                entry.3 += amount;
                // entry.4 accumulates the amount-weighted net_rate numerator.
                if net_rate.is_finite() {
                    entry.4 += net_rate * amount;
                }
            }
            for ((ts_code, date), (net_amount, l_buy, l_sell, amount, weighted_net_rate)) in by_day {
                let net_rate = if amount.abs() > 1e-9 {
                    weighted_net_rate / amount
                } else {
                    debug!("a_top_list {ts_code} {date}: zero total amount, falling back to unweighted net_rate");
                    f64::NAN
                };
                top_list.entry(ts_code).or_default().push((date, ALhbDay {
                    net_amount, l_buy, l_sell, amount, net_rate,
                }));
            }
            for v in top_list.values_mut() {
                v.sort_by_key(|(d, _)| *d);
            }
            info!("Aggregated a_top_list into {} tickers' daily entries", top_list.len());
        }
        Err(e) => { warn!("Failed to load a_top_list: {e}"); }
    }

    info!("Loading a_margin_detail (融资融券交易明细) [{}, {}]...", start, end);
    let mut margin_detail: FxHashMap<String, Vec<(chrono::NaiveDate, AMarginDay)>> = FxHashMap::default();
    match quant_db::queries::a_read::get_a_margin_detail(pool, start, end).await {
        Ok(rows) => {
            info!("Loaded {} a_margin_detail rows", rows.len());
            for r in rows {
                let entry = AMarginDay {
                    rzye: r.rzye.unwrap_or(f64::NAN),
                    rzmre: r.rzmre.unwrap_or(f64::NAN),
                };
                let bucket = margin_detail.entry(r.ts_code.clone()).or_default();
                if bucket.last().is_some_and(|(d, _)| *d == r.trade_date) {
                    warn!("Duplicate a_margin_detail row for {} {} — keeping last write", r.ts_code, r.trade_date);
                    bucket.pop();
                }
                bucket.push((r.trade_date, entry));
            }
            for v in margin_detail.values_mut() {
                v.sort_by_key(|(d, _)| *d);
            }
            info!("Aggregated a_margin_detail into {} tickers' daily entries", margin_detail.len());
        }
        Err(e) => { warn!("Failed to load a_margin_detail: {e}"); }
    }

    AShareCache {
        daily,
        financials,
        industry,
        basics,
        trading_days,
        index_prices,
        ts_codes,
        top_list,
        margin_detail,
    }
}

fn parse_industry_date(value: &str, field: &str, ts_code: &str) -> Option<chrono::NaiveDate> {
    match chrono::NaiveDate::parse_from_str(value, "%Y-%m-%d") {
        Ok(date) => Some(date),
        Err(error) => {
            warn!("Invalid {field} for industry membership {ts_code}: {value} ({error})");
            None
        }
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
        "Factor (FM)", "N", "Mean γ", "Std γ", "NW t-stat");
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
    let mut fm_csv = String::from("factor,n_months,mean_gamma,std_gamma,nw_t_stat\n");
    for s in &fm_summaries {
        fm_csv.push_str(&format!(
            "{},{},{:.8},{:.8},{:.4}\n",
            s.factor_name, s.n_months, s.mean_gamma, s.std_gamma, s.t_stat,
        ));
    }
    std::fs::write(&fm_path, fm_csv).ok();
    info!("Saved IC → {}, FM → {}", csv_path.display(), fm_path.display());
}

async fn cmd_analyze_cn(
    config: &quant_core::config::Config,
    start_str: &str,
    end_str: &str,
    output_dir: &PathBuf,
    factor_set: &str,
) {
    use chrono::Datelike;
    use quant_factors::a_share::factors::{all_factors, AFactorDef};
    use quant_factors::a_share::factors_v2::all_factors_v2;
    use quant_factors::a_share::universe::{AUniverseFilter, get_a_clean_universe};
    use quant_strategy::analysis::{compute_ic_panel_a, fama_macbeth_a};

    let start = chrono::NaiveDate::parse_from_str(start_str, "%Y-%m-%d")
        .expect("Invalid start date");
    let end = chrono::NaiveDate::parse_from_str(end_str, "%Y-%m-%d")
        .expect("Invalid end date");

    let db_url = config.database.url();
    let schema = &config.database.schema;
    let load_start = start - chrono::Duration::days(365 * 4);
    let load_end = end + chrono::Duration::days(30);

    info!("Loading A-share cache from DB [{}, {}]...", load_start, load_end);
    let pool = quant_db::pool::create_pool(&db_url, schema, 8).await
        .expect("connect to db");
    let cache = build_a_share_cache(&pool, load_start, load_end).await;
    pool.close().await;

    let factors: Vec<AFactorDef> = match factor_set {
        "v1" => all_factors(),
        "v2" => all_factors_v2(),
        other => panic!("Unknown --factor-set '{other}', expected 'v1' or 'v2'"),
    };
    info!("{} A-share factors registered (factor_set={factor_set})", factors.len());

    let rebalance_dates: Vec<chrono::NaiveDate> = {
        let mut dates = Vec::new();
        let mut last_ym = (0i32, 0u32);
        for &d in cache.trading_days.iter().rev() {
            let ym = (d.year(), d.month());
            if ym != last_ym {
                if d >= start && d <= end { dates.push(d); }
                last_ym = ym;
            }
        }
        dates.reverse();
        dates
    };
    info!("{} dates for analysis", rebalance_dates.len());

    let t0 = std::time::Instant::now();
    let mut factor_panel: std::collections::HashMap<
        chrono::NaiveDate,
        std::collections::HashMap<String, std::collections::HashMap<String, f64>>,
    > = std::collections::HashMap::new();
    let universe_filter = AUniverseFilter::from_config(&config.a_share.universe);

    for (i, &date) in rebalance_dates.iter().enumerate() {
        let clean_universe = get_a_clean_universe(date, &cache, &universe_filter);
        let mut date_factors = std::collections::HashMap::new();
        for f in &factors {
            let raw = (f.compute)(date, &cache);
            if raw.is_empty() { continue; }
            let raw: std::collections::HashMap<String, f64> = raw.into_iter()
                .filter(|(code, _)| clean_universe.contains(code))
                .collect();
            let processed = quant_strategy::a_strategy::winsorize_zscore_public(&raw);
            if !processed.is_empty() {
                date_factors.insert(f.name.to_string(), processed);
            }
        }
        factor_panel.insert(date, date_factors);

        if (i + 1) % 12 == 0 || i + 1 == rebalance_dates.len() {
            info!(
                "Panel {}/{}: universe={} ({:.1}s)",
                i + 1,
                rebalance_dates.len(),
                clean_universe.len(),
                t0.elapsed().as_secs_f64(),
            );
        }
    }
    info!("Factor panel: {:.1}s", t0.elapsed().as_secs_f64());

    info!("Computing IC (horizon=21d)...");
    let ic_summaries = compute_ic_panel_a(&factor_panel, &cache, 21);

    println!("\n{}", "=".repeat(90));
    println!("A-SHARE FACTOR ANALYSIS [{factor_set}]: {} to {} ({} months)", start, end, rebalance_dates.len());
    println!("{}", "=".repeat(90));

    println!(
        "\n{:<30} {:>5} {:>9} {:>9} {:>7} {:>7} {:>6}",
        "Factor", "N", "Mean IC", "Std IC", "ICIR", "t-stat", "%Pos"
    );
    println!("{}", "-".repeat(90));

    for s in &ic_summaries {
        let star = if s.t_stat.abs() > 3.0 { "***" }
            else if s.t_stat.abs() > 2.0 { "**" }
            else { "" };
        println!(
            "{:<30} {:>5} {:>9.4} {:>9.4} {:>+7.3} {:>+7.2} {:>5.0}% {}",
            s.factor_name, s.n_months, s.mean_ic, s.std_ic,
            s.icir, s.t_stat, s.pct_positive * 100.0, star,
        );
    }

    info!("Computing Fama-MacBeth (horizon=21d)...");
    let fm_summaries = fama_macbeth_a(&factor_panel, &cache, 21);

    println!("\n\n{:<30} {:>5} {:>12} {:>12} {:>8}",
        "Factor (FM)", "N", "Mean γ", "Std γ", "NW t-stat");
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

    std::fs::create_dir_all(output_dir).ok();
    let csv_path = output_dir.join(format!("a_ic_summary_{factor_set}_{start}_{end}.csv"));
    let mut csv = String::from("factor,n_months,mean_ic,std_ic,icir,t_stat,pct_positive\n");
    for s in &ic_summaries {
        csv.push_str(&format!(
            "{},{},{:.6},{:.6},{:.6},{:.6},{:.4}\n",
            s.factor_name, s.n_months, s.mean_ic, s.std_ic, s.icir, s.t_stat, s.pct_positive,
        ));
    }
    std::fs::write(&csv_path, csv).ok();

    let fm_path = output_dir.join(format!("a_fama_macbeth_{factor_set}_{start}_{end}.csv"));
    let mut fm_csv = String::from("factor,n_months,mean_gamma,std_gamma,nw_t_stat\n");
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
    market: Market,
    cache_dir: &PathBuf,
    start_str: &str,
    end_str: &str,
    output_dir: &PathBuf,
    no_short: bool,
    no_optimizer: bool,
    export_signals: bool,
) {
    let start = chrono::NaiveDate::parse_from_str(start_str, "%Y-%m-%d")
        .expect("Invalid start date (expected YYYY-MM-DD)");
    let end = chrono::NaiveDate::parse_from_str(end_str, "%Y-%m-%d")
        .expect("Invalid end date (expected YYYY-MM-DD)");
    if end < start {
        eprintln!("end ({end}) is before start ({start})");
        std::process::exit(1);
    }

    // ── A-share path: long-only equal-weight, T+1 engine. ──
    // Regime / MVO / short / rolling-IC NOT wired here (US-only today; tracked as TODOs).
    if matches!(market, Market::Cn) {
        use chrono::Datelike;
        use quant_backtest::a_engine::{run_backtest as a_run_backtest, ACostConfig};

        if no_short {
            warn!("--no-short: A 股策略本就是 long-only，flag 已忽略");
        }
        if no_optimizer {
            warn!("--no-optimizer: A 股 a_strategy 暂未接入 MVO，flag 已忽略");
        }

        let db_url = config.database.url();
        let schema = &config.database.schema;
        if db_url.contains("@:/") || db_url.contains("mysql://:@") {
            eprintln!("Database not configured. Set DB_HOST/USER/PASSWORD/DATABASE.");
            std::process::exit(1);
        }

        let benchmark_code = config.a_share.universe.benchmark_index.clone();
        // 4-year lookback: CAGR_3Y (~13 quarters) + momentum (240 days).
        let load_start = start - chrono::Duration::days(365 * 4);
        let load_end = end + chrono::Duration::days(1);

        info!("Loading A-share cache from DB [{}, {}]...", load_start, load_end);
        let rt = tokio::runtime::Runtime::new().expect("tokio runtime");
        let mut cache = rt.block_on(async {
            let pool = quant_db::pool::create_pool(&db_url, schema, 8).await
                .expect("connect to db");
            let mut cache = build_a_share_cache(&pool, load_start, load_end).await;

            // build_a_share_cache leaves index_prices empty; load benchmark series here.
            info!("Loading benchmark index {} [{}, {}]...", benchmark_code, load_start, load_end);
            let idx_rows = quant_db::queries::a_read::get_a_index_daily(
                &pool, &benchmark_code, load_start, load_end,
            ).await.expect("Failed to load a_index_daily");
            let mut series: Vec<(chrono::NaiveDate, f64)> = idx_rows.into_iter()
                .filter_map(|r| r.close.map(|c| (r.trade_date, c)))
                .collect();
            series.sort_by_key(|(d, _)| *d);
            info!("Loaded {} benchmark index rows", series.len());
            cache.index_prices.insert(benchmark_code.clone(), series);

            pool.close().await;
            cache
        });

        if cache.daily.is_empty() {
            eprintln!("No A-share price data in DB for [{}, {}].", load_start, load_end);
            std::process::exit(1);
        }
        let bm_rows = cache.index_prices.get(&benchmark_code).map(|v| v.len()).unwrap_or(0);
        if bm_rows == 0 {
            warn!("Benchmark {} has 0 rows — benchmark NAV will stay flat.", benchmark_code);
        }
        info!(
            "AShareCache: {} stocks, {} financials, {} industries, {} basics, {} trading days, {} benchmark rows",
            cache.daily.len(),
            cache.financials.len(),
            cache.industry.len(),
            cache.basics.len(),
            cache.trading_days.len(),
            bm_rows,
        );

        // Trim trading_days + benchmark to backtest window. Per-stock daily and
        // financials are date-keyed independently and stay intact for lookback.
        cache.trading_days.retain(|d| *d >= start && *d <= end);
        if let Some(s) = cache.index_prices.get_mut(&benchmark_code) {
            s.retain(|(d, _)| *d >= start && *d <= end);
        }
        if cache.trading_days.is_empty() {
            eprintln!("No trading days in [{}, {}] — check a_trade_cal SSE coverage.", start, end);
            std::process::exit(1);
        }
        info!("Backtest window: {} trading days [{}, {}]",
            cache.trading_days.len(),
            cache.trading_days.first().unwrap(),
            cache.trading_days.last().unwrap(),
        );

        // === Generate monthly signals ===
        // NOTE: switched from the archived v1 (a_strategy::generate_signals,
        // financial-driven) to the v2 stub (a_strategy_v2::generate_signals_v2,
        // sentiment-driven, currently empty). See a_strategy_v2.rs doc comment.
        let t0 = std::time::Instant::now();
        let signals = quant_strategy::a_strategy_v2::generate_signals_v2(
            &cache,
            config.a_share.strategy.max_holdings,
            config.a_share.strategy.min_select_score,
            Some(&config.a_share.universe),
            &config.a_share.strategy,
            Some(&config.a_share.regime),
        );
        info!(
            "Signal generation: {:.1}s ({} monthly signals)",
            t0.elapsed().as_secs_f64(),
            signals.len(),
        );

        if signals.is_empty() {
            eprintln!("No signals generated — check universe filter / factor coverage.");
            return;
        }

        // === Dump holdings @ first/middle/last rebalance ===
        {
            let dates: Vec<chrono::NaiveDate> = signals.keys().copied().collect();
            let n = dates.len();
            let dump_dates: Vec<chrono::NaiveDate> = if n >= 3 {
                vec![dates[0], dates[n / 2], dates[n - 1]]
            } else {
                dates.clone()
            };

            for d in dump_dates {
                let w = match signals.get(&d) {
                    Some(x) => x,
                    None => continue,
                };
                let mut sorted: Vec<(&String, f64)> =
                    w.iter().map(|(k, &v)| (k, v)).collect();
                sorted.sort_by(|a, b| b.1.partial_cmp(&a.1).unwrap_or(std::cmp::Ordering::Equal));
                let gross: f64 = w.values().map(|v| v.abs()).sum();
                let net: f64 = w.values().sum();
                println!(
                    "\n=== Holdings @ {d} (n={}, gross={:.2}, net={:+.2}) ===",
                    w.len(), gross, net
                );
                println!("  ts_code      weight   name         industry");
                for (code, weight) in sorted.iter().take(15) {
                    let name = cache.basics.get(*code)
                        .map(|b| b.name.as_str()).unwrap_or("?");
                    let ind = cache.industry_on(code, d)
                        .map(|i| i.industry_name.as_str()).unwrap_or("?");
                    println!("  {code:<12} {weight:>+7.4}   {name:<12} {ind}");
                }
            }
        }

        // === Run backtest ===
        let cost = ACostConfig::from_a_share(&config.a_share);
        let result = a_run_backtest(
            &signals,
            &cache,
            &cost,
            &benchmark_code,
            Some(&config.a_share.universe),
        );

        // === Print results ===
        let bm_label = if benchmark_code == "000300.SH" { "CSI 300" } else { benchmark_code.as_str() };
        println!("\n{}", "=".repeat(70));
        println!("A-SHARE BACKTEST: {} to {}", start, end);
        println!("Benchmark: {} ({})", benchmark_code, bm_label);
        println!("{}", "=".repeat(70));
        println!("Total Return:       {:>10.2}%", result.total_return * 100.0);
        println!("Annual Return:      {:>10.2}%", result.annual_return * 100.0);
        println!("Annual Volatility:  {:>10.2}%", result.annual_volatility * 100.0);
        println!("Sharpe Ratio:       {:>10.2}", result.sharpe_ratio);
        println!("Max Drawdown:       {:>10.2}%", result.max_drawdown * 100.0);
        println!("Calmar Ratio:       {:>10.2}", result.calmar_ratio);
        println!("Win Rate:           {:>10.2}%", result.win_rate * 100.0);
        println!("Total Trades:       {:>10}", result.total_trades);
        println!("Annual Turnover:    {:>10.2}x", result.annual_turnover);

        // Yearly breakdown (only meaningful when window spans >100 trading days).
        if result.nav.len() > 100 {
            println!("\nYearly Returns:");
            println!("{:>6} {:>10} {:>10} {:>10}", "Year", "Strategy", bm_label, "Excess");
            println!("{}", "-".repeat(42));

            let mut year_ends: std::collections::BTreeMap<i32, f64> = std::collections::BTreeMap::new();
            let mut bm_year_ends: std::collections::BTreeMap<i32, f64> = std::collections::BTreeMap::new();
            for &(date, nav) in &result.nav {
                year_ends.insert(date.year(), nav);
            }
            for &(date, nav) in &result.benchmark_nav {
                bm_year_ends.insert(date.year(), nav);
            }

            let mut prev_nav = 1.0;
            let mut prev_bm = 1.0;
            for (year, &nav) in &year_ends {
                let bm = bm_year_ends.get(year).copied().unwrap_or(prev_bm);
                let strat_ret = nav / prev_nav - 1.0;
                let bm_ret = bm / prev_bm - 1.0;
                let excess = strat_ret - bm_ret;
                println!(
                    "{:>6} {:>9.2}% {:>9.2}% {:>+9.2}%",
                    year, strat_ret * 100.0, bm_ret * 100.0, excess * 100.0
                );
                prev_nav = nav;
                prev_bm = bm;
            }
        }

        // === Export last-rebalance signal ===
        if export_signals {
            if let Some((last_date, last_weights)) = signals.iter().next_back() {
                let weights_map: std::collections::BTreeMap<String, f64> = last_weights.iter()
                    .map(|(k, &v)| (k.clone(), v))
                    .collect();
                let gross: f64 = last_weights.values().map(|w| w.abs()).sum();
                let net: f64 = last_weights.values().sum();
                let n_long = last_weights.values().filter(|&&w| w > 0.0).count();
                let n_short = last_weights.values().filter(|&&w| w < 0.0).count();

                let payload = serde_json::json!({
                    "date": last_date.to_string(),
                    "weights": weights_map,
                    "metadata": {
                        "n_long": n_long,
                        "n_short": n_short,
                        "n_total": last_weights.len(),
                        "gross": gross,
                        "net": net,
                        "market": "cn",
                        "benchmark": benchmark_code,
                        "backtest_start": start_str,
                        "backtest_end": end_str,
                        "generated_at": chrono::Utc::now().to_rfc3339(),
                    }
                });

                std::fs::create_dir_all(output_dir).ok();
                let path = output_dir.join(format!("signals_{last_date}.json"));
                std::fs::write(&path, serde_json::to_string_pretty(&payload).unwrap()).ok();
                info!(
                    "Exported signals → {} ({} positions, gross={:.2})",
                    path.display(), last_weights.len(), gross
                );
            } else {
                warn!("--export-signals: no signals generated, skipping export");
            }
        }
        return;
    }

    // ── US path (default) ──
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
    let mut scores_history: std::collections::BTreeMap<
        chrono::NaiveDate,
        rustc_hash::FxHashMap<quant_core::types::TickerId, f64>,
    > = std::collections::BTreeMap::new();
    let mut prev_holdings: rustc_hash::FxHashSet<quant_core::types::TickerId> = Default::default();

    // Category-specific neutralize modes (from Python config)
    let cat_neutralize_overrides = &config.factor_processing.category_neutralize_overrides;

    let universe_filter = quant_data::universe::UniverseFilter {
        min_market_cap: config.universe.min_market_cap,
        min_daily_volume: config.universe.min_daily_volume,
        min_volume_days: 20,
    };

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
                    do_neutralize: true, // Enabled: force selection within each sector to avoid value-trap concentration in beaten-down sectors
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
        scores_history.insert(date, scores.clone());

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
            // Carry over prev holdings only if still in top-K by score. Without
            // this cap, the candidate set grows monotonically (~10 new each
            // month over 14 years → 1800+ candidates), and the optimizer's
            // post-hoc gross-leverage rescale crushes net exposure to ~0%
            // (target 0.6 × scale 1/60 = 0.01), turning the L/S strategy into
            // unintended pure market-neutral noise.
            let prev_keep_top = (long_n + short_n) as usize;
            let mut prev_scored: Vec<(quant_core::types::TickerId, f64)> = prev_weights
                .keys()
                .filter_map(|&tid| scores.get(&tid).map(|&s| (tid, s.abs())))
                .collect();
            prev_scored.sort_by(|a, b| b.1.partial_cmp(&a.1).unwrap_or(std::cmp::Ordering::Equal));
            for (tid, _) in prev_scored.iter().take(prev_keep_top) {
                candidate_set.insert(*tid);
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

    // === Export last-rebalance target weights to JSON (for paper trading) ===
    if export_signals {
        if let Some((&last_date, last_weights)) = signals.iter().next_back() {
            let mut weights_str: std::collections::BTreeMap<String, f64> =
                std::collections::BTreeMap::new();
            for (&tid, &w) in last_weights {
                weights_str.insert(cache.ticker_interner.resolve(tid).to_string(), w);
            }
            let n_long = last_weights.values().filter(|&&w| w > 0.0).count();
            let n_short = last_weights.values().filter(|&&w| w < 0.0).count();
            let gross: f64 = last_weights.values().map(|w| w.abs()).sum();
            let net: f64 = last_weights.values().sum();

            let payload = serde_json::json!({
                "date": last_date.to_string(),
                "weights": weights_str,
                "metadata": {
                    "n_long": n_long,
                    "n_short": n_short,
                    "n_total": last_weights.len(),
                    "gross": gross,
                    "net": net,
                    "backtest_start": start_str,
                    "backtest_end": end_str,
                    "generated_at": chrono::Utc::now().to_rfc3339(),
                }
            });

            std::fs::create_dir_all(output_dir).ok();
            let path = output_dir.join(format!("signals_{last_date}.json"));
            std::fs::write(&path, serde_json::to_string_pretty(&payload).unwrap()).ok();
            info!(
                "Exported signals → {} ({}L/{}S, gross={:.2}, net={:+.2})",
                path.display(), n_long, n_short, gross, net
            );
        } else {
            warn!("--export-signals: no signals generated, skipping export");
        }
    }

    // === Dump portfolio holdings at first/middle/last rebalance ===
    {
        let signal_dates: Vec<chrono::NaiveDate> = signals.keys().copied().collect();
        let n = signal_dates.len();
        let dump_dates: Vec<chrono::NaiveDate> = if n >= 3 {
            vec![signal_dates[0], signal_dates[n / 2], signal_dates[n - 1]]
        } else {
            signal_dates.clone()
        };

        for d in dump_dates {
            let weights = match signals.get(&d) {
                Some(w) => w,
                None => continue,
            };
            let scores_at = scores_history.get(&d);
            let mut sorted: Vec<(quant_core::types::TickerId, f64)> =
                weights.iter().map(|(&t, &w)| (t, w)).collect();
            sorted.sort_by(|a, b| b.1.partial_cmp(&a.1).unwrap_or(std::cmp::Ordering::Equal));

            let gross: f64 = weights.values().map(|v| v.abs()).sum();
            let net: f64 = weights.values().sum();
            println!(
                "\n=== Holdings @ {d} (n={}, gross={:.2}, net={:+.2}) ===",
                weights.len(), gross, net
            );

            let longs: Vec<_> = sorted.iter().filter(|(_, w)| *w > 0.0).collect();
            let shorts: Vec<_> = sorted.iter().rev().filter(|(_, w)| *w < 0.0).collect();

            println!("Top {} longs:  ticker  weight   score   sector", longs.len().min(15));
            for (tid, w) in longs.iter().take(15) {
                let tk = cache.ticker_interner.resolve(*tid);
                let sc = scores_at.and_then(|s| s.get(tid)).copied().unwrap_or(f64::NAN);
                let sec = cache.sector_map.get(tid)
                    .map(|sid| cache.sector_interner.resolve(*sid))
                    .unwrap_or("?");
                println!("  {tk:<8} {w:>+7.4} {sc:>+7.3}   {sec}");
            }
            if !shorts.is_empty() {
                println!("Bottom {} shorts:", shorts.len().min(15));
                for (tid, w) in shorts.iter().take(15) {
                    let tk = cache.ticker_interner.resolve(*tid);
                    let sc = scores_at.and_then(|s| s.get(tid)).copied().unwrap_or(f64::NAN);
                    let sec = cache.sector_map.get(tid)
                        .map(|sid| cache.sector_interner.resolve(*sid))
                        .unwrap_or("?");
                    println!("  {tk:<8} {w:>+7.4} {sc:>+7.3}   {sec}");
                }
            }
        }
    }

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
    ticker: Option<&str>,
) {
    let db_url = config.database.url();
    let schema = &config.database.schema;
    if db_url.contains("@:/") || db_url.contains("mysql://:@") {
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
        let pool = quant_db::pool::create_pool(&db_url, schema, 40).await
            .expect("Failed to connect to database");

        match source {
            "fmp" => {
                // Default 2500/min matches FMP Ultimate plan (3000/min cap, 500 headroom)
                let rate_limit: u32 = std::env::var("FMP_RATE_LIMIT")
                    .ok().and_then(|s| s.parse().ok()).unwrap_or(2500);
                let dl = quant_download::us_fmp::FmpDownloader::new(
                    fmp_key, pool.clone(), rate_limit,
                ).with_ticker(ticker);

                if incremental && target == "all" {
                    dl.update_all().await;
                } else {
                    match target {
                        "stock_list" => { dl.download_stock_list().await; }
                        "profile" => { dl.download_company_profiles().await; }
                        "daily_price" => { dl.download_daily_prices(start_year, incremental).await; }
                        "financial" => {
                            if incremental { dl.download_financials_incremental().await; }
                            else { dl.download_financials().await; }
                        }
                        "key_metric" => {
                            if incremental { dl.download_key_metrics_incremental().await; }
                            else { dl.download_key_metrics().await; }
                        }
                        "growth" => {
                            if incremental { dl.download_financial_growth_incremental().await; }
                            else { dl.download_financial_growth().await; }
                        }
                        "enterprise_value" => {
                            if incremental { dl.download_enterprise_values_incremental().await; }
                            else { dl.download_enterprise_values().await; }
                        }
                        "owner_earnings" => { dl.download_owner_earnings().await; }
                        "earnings" => {
                            if incremental { dl.download_earnings_surprises_incremental().await; }
                            else { dl.download_earnings_surprises().await; }
                        }
                        "eps_estimate" => { dl.download_eps_estimates().await; }
                        "insider" => { dl.download_insider_trading().await; }
                        "analyst" => { dl.download_analyst_grades().await; }
                        "dividend" => { dl.download_dividends().await; }
                        "score" => { dl.download_financial_scores().await; }
                        "float" => { dl.download_shares_float().await; }
                        "insider_stat" => { dl.download_insider_statistics().await; }
                        "employee" => { dl.download_employee_count().await; }
                        "price_target" => { dl.download_price_targets().await; }
                        "esg" => { dl.download_esg_ratings().await; }
                        "dcf" => { dl.download_dcf_valuations().await; }
                        "peer" => { dl.download_stock_peers().await; }
                        "index" => { dl.download_index_daily(start_year).await; }
                        "macro" => { dl.download_macro().await; }
                        "congress" => { dl.download_congress_trading().await; }
                        "press" => { dl.download_press_releases().await; }
                        "revenue_segment" => { dl.download_revenue_segments().await; }
                        "delisted" => { dl.download_delisted().await; }
                        "symbol_change" => { dl.download_symbol_changes().await; }
                        "all" => { dl.download_all(start_year).await; }
                        other => {
                            eprintln!("Unknown FMP target: {other}");
                            eprintln!("Available: stock_list, profile, daily_price, financial, key_metric,");
                            eprintln!("  growth, enterprise_value, owner_earnings, earnings, eps_estimate,");
                            eprintln!("  insider, analyst, dividend, score, float, insider_stat, employee,");
                            eprintln!("  price_target, esg, dcf, peer, index, macro, congress, press,");
                            eprintln!("  revenue_segment, delisted, symbol_change, all");
                            std::process::exit(1);
                        }
                    }
                }
            }
            "quiver" => {
                let quiver_key = std::env::var("QUIVER_API_KEY").unwrap_or_default();
                if quiver_key.is_empty() {
                    eprintln!("QUIVER_API_KEY not set.");
                    std::process::exit(1);
                }
                let rate_limit: u32 = std::env::var("QUIVER_RATE_LIMIT")
                    .ok().and_then(|s| s.parse().ok()).unwrap_or(60);
                let dl = quant_download::us_quiver::QuiverDownloader::new(
                    quiver_key, pool.clone(), rate_limit,
                );
                match target {
                    "lobbying" => { dl.download_lobbying().await; }
                    "gov_contract" => { dl.download_gov_contracts().await; }
                    "dark_pool" => { dl.download_dark_pool().await; }
                    "institutional" => { dl.download_institutional_holders().await; }
                    "all" => { dl.download_all().await; }
                    other => {
                        eprintln!("Unknown Quiver target: {other}");
                        eprintln!("Available: lobbying, gov_contract, dark_pool, institutional, all");
                        std::process::exit(1);
                    }
                }
            }
            "tushare" => {
                let ts_token = std::env::var("TUSHARE_TOKEN").unwrap_or_default();
                if ts_token.is_empty() {
                    eprintln!("TUSHARE_TOKEN not set.");
                    std::process::exit(1);
                }
                let rate_limit: u32 = std::env::var("TUSHARE_RATE_LIMIT")
                    .ok().and_then(|s| s.parse().ok()).unwrap_or(200);
                let dl = quant_download::a_tushare::TushareDownloader::new(
                    ts_token, pool.clone(), rate_limit,
                ).with_ticker(ticker.as_deref());
                let start = format!("{start_year}0101");
                if incremental && target == "all" {
                    dl.update_all().await;
                } else {
                    match target {
                        "stock_list" => { dl.download_stock_list().await; }
                        "trade_cal" => { dl.download_trade_cal().await; }
                        "daily_price" => { dl.download_daily_prices(&start, incremental).await; }
                        "income" => { dl.download_income(incremental).await; }
                        "balance" => { dl.download_balancesheet(incremental).await; }
                        "cashflow" => { dl.download_cashflow(incremental).await; }
                        "indicator" => { dl.download_fina_indicator(incremental).await; }
                        "industry" => { dl.download_industry().await; }
                        "index" => { dl.download_index_daily(&start).await; }
                        "macro" => { dl.download_macro().await; }
                        "commodity" => { dl.download_commodity(incremental).await; }
                        "top_list" => { dl.download_top_list(&start, incremental).await; }
                        "top_inst" => { dl.download_top_inst(&start, incremental).await; }
                        "margin" => { dl.download_margin(&start, incremental).await; }
                        "margin_detail" => { dl.download_margin_detail(&start, incremental).await; }
                        "moneyflow_hsgt" => { dl.download_moneyflow_hsgt(&start, incremental).await; }
                        "forecast" => { dl.download_forecast(incremental).await; }
                        "express" => { dl.download_express(incremental).await; }
                        "stk_holdertrade" => { dl.download_stk_holdertrade(incremental).await; }
                        "repurchase" => { dl.download_repurchase(incremental).await; }
                        "share_float" => { dl.download_share_float(incremental).await; }
                        "all" => { dl.download_all(&start).await; }
                        other => {
                            eprintln!("Unknown Tushare target: {other}");
                            eprintln!("Available: stock_list, trade_cal, daily_price, income, balance,");
                            eprintln!("  cashflow, indicator, industry, index, macro, commodity,");
                            eprintln!("  top_list, top_inst, margin, margin_detail, moneyflow_hsgt,");
                            eprintln!("  forecast, express, stk_holdertrade, repurchase, share_float, all");
                            std::process::exit(1);
                        }
                    }
                }
            }
            "fred" => {
                let fred_key = std::env::var("FRED_API_KEY").unwrap_or_default();
                if fred_key.is_empty() {
                    eprintln!("FRED_API_KEY not set.");
                    std::process::exit(1);
                }
                let dl = quant_download::us_fred::FredDownloader::new(
                    fred_key, pool.clone(),
                );
                let start = format!("{start_year}-01-01");
                dl.download_all(&start).await;
            }
            other => {
                eprintln!("Unknown source: {other}. Available: fmp, quiver, fred, tushare");
                std::process::exit(1);
            }
        }

        pool.close().await;
    });
}

/// Per-table metadata for db-status. `date_col` = MAX() target for "latest data
/// date" (None → snapshot table, only updated_at meaningful). Column names were
/// verified against live PG `information_schema.columns` — do not edit blindly.
struct DbStatusTable {
    name: &'static str,
    market: &'static str,           // "us" | "cn" | "shared"
    date_col: Option<&'static str>,
}

const DB_STATUS_TABLES: &[DbStatusTable] = &[
    // ── US ───────────────────────────────────────────────────────────────
    // Snapshot
    DbStatusTable { name: "us_stock_basic",            market: "us", date_col: None },
    DbStatusTable { name: "us_industry_class",         market: "us", date_col: None },
    DbStatusTable { name: "us_company_profile",        market: "us", date_col: None },
    DbStatusTable { name: "us_esg_rating",             market: "us", date_col: None },
    DbStatusTable { name: "us_financial_score",        market: "us", date_col: None },
    DbStatusTable { name: "us_insider_statistic",      market: "us", date_col: None },
    DbStatusTable { name: "us_stock_peer",             market: "us", date_col: None },
    DbStatusTable { name: "us_press_release",          market: "us", date_col: None },
    DbStatusTable { name: "us_delisted",               market: "us", date_col: None },
    DbStatusTable { name: "us_gov_contract",           market: "us", date_col: None },
    DbStatusTable { name: "us_price_target",           market: "us", date_col: None },
    // Time-series
    DbStatusTable { name: "us_daily_price",            market: "us", date_col: Some("trade_date") },
    DbStatusTable { name: "us_index_daily",            market: "us", date_col: Some("trade_date") },
    DbStatusTable { name: "us_commodity_price",        market: "us", date_col: Some("trade_date") },
    DbStatusTable { name: "us_macro_indicator",        market: "us", date_col: Some("report_date") },
    DbStatusTable { name: "us_financial_data",         market: "us", date_col: Some("filing_date") },
    DbStatusTable { name: "us_key_metric",             market: "us", date_col: Some("date") },
    DbStatusTable { name: "us_enterprise_value",       market: "us", date_col: Some("date") },
    DbStatusTable { name: "us_financial_growth",       market: "us", date_col: Some("date") },
    DbStatusTable { name: "us_owner_earnings",         market: "us", date_col: Some("date") },
    DbStatusTable { name: "us_analyst_recommendation", market: "us", date_col: Some("date") },
    DbStatusTable { name: "us_earnings_surprise",      market: "us", date_col: Some("date") },
    DbStatusTable { name: "us_eps_estimate",           market: "us", date_col: Some("date") },
    DbStatusTable { name: "us_price_target_detail",    market: "us", date_col: Some("published_date") },
    DbStatusTable { name: "us_corporate_action",       market: "us", date_col: Some("date") },
    DbStatusTable { name: "us_insider_trade",          market: "us", date_col: Some("transaction_date") },
    DbStatusTable { name: "us_congress_trade",         market: "us", date_col: Some("transaction_date") },
    DbStatusTable { name: "us_dark_pool_volume",       market: "us", date_col: Some("date") },
    DbStatusTable { name: "us_institutional_holder",   market: "us", date_col: Some("date") },
    DbStatusTable { name: "us_employee_count",         market: "us", date_col: Some("filing_date") },
    DbStatusTable { name: "us_revenue_segment",        market: "us", date_col: Some("date") },
    DbStatusTable { name: "us_sec_filing",             market: "us", date_col: Some("filing_date") },
    DbStatusTable { name: "us_lobbying",               market: "us", date_col: Some("date") },
    DbStatusTable { name: "us_dcf_valuation",          market: "us", date_col: Some("date") },
    DbStatusTable { name: "us_index_constituent",      market: "us", date_col: Some("date") },
    DbStatusTable { name: "us_news",                   market: "us", date_col: Some("published_at") },
    DbStatusTable { name: "us_symbol_change",          market: "us", date_col: Some("date") },
    DbStatusTable { name: "us_shares_float",           market: "us", date_col: Some("date") },
    // ── CN ───────────────────────────────────────────────────────────────
    DbStatusTable { name: "a_stock_basic",             market: "cn", date_col: None },
    DbStatusTable { name: "a_trade_cal",               market: "cn", date_col: None },
    DbStatusTable { name: "a_industry_class",          market: "cn", date_col: None },
    DbStatusTable { name: "a_daily_price",             market: "cn", date_col: Some("trade_date") },
    DbStatusTable { name: "a_index_daily",             market: "cn", date_col: Some("trade_date") },
    DbStatusTable { name: "a_commodity_price",         market: "cn", date_col: Some("trade_date") },
    DbStatusTable { name: "a_macro_indicator",         market: "cn", date_col: Some("report_date") },
    // f_ann_date = 实际公告日 (PIT-correct), 不是 end_date (报告期 forward-looking)
    DbStatusTable { name: "a_financial_income",        market: "cn", date_col: Some("f_ann_date") },
    DbStatusTable { name: "a_financial_balance",       market: "cn", date_col: Some("f_ann_date") },
    DbStatusTable { name: "a_financial_cashflow",      market: "cn", date_col: Some("f_ann_date") },
    DbStatusTable { name: "a_financial_indicator",     market: "cn", date_col: Some("ann_date") }, // no f_ann_date
    DbStatusTable { name: "a_insider_transaction",     market: "cn", date_col: Some("ann_date") },
    DbStatusTable { name: "a_research_report",         market: "cn", date_col: Some("publish_date") },
    DbStatusTable { name: "a_top_list",                market: "cn", date_col: Some("trade_date") },
    DbStatusTable { name: "a_top_inst",                market: "cn", date_col: Some("trade_date") },
    DbStatusTable { name: "a_margin",                  market: "cn", date_col: Some("trade_date") },
    DbStatusTable { name: "a_margin_detail",           market: "cn", date_col: Some("trade_date") },
    DbStatusTable { name: "a_moneyflow_hsgt",          market: "cn", date_col: Some("trade_date") },
    // ── shared ──────────────────────────────────────────────────────────
    DbStatusTable { name: "import_progress",           market: "shared", date_col: None },
];

fn cmd_db_status(config: &quant_core::config::Config, market_filter: &str) {
    let db_url = config.database.url();
    let schema = &config.database.schema;
    if db_url.contains("@:/") || db_url.contains("mysql://:@") {
        eprintln!("Database not configured. Set DB_HOST, DB_USER, DB_PASSWORD, DB_DATABASE env vars.");
        std::process::exit(1);
    }

    let market_filter = market_filter.to_lowercase();
    if !["us", "cn", "all"].contains(&market_filter.as_str()) {
        eprintln!("Invalid --market: {market_filter}. Use us | cn | all");
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

        // ── Pre-fetch import_progress map: table_name -> done ticker count ──
        let progress_rows: Vec<(String, i64)> = sqlx::query_as(
            "SELECT table_name, COUNT(DISTINCT ticker) FROM import_progress GROUP BY table_name"
        ).fetch_all(&pool).await.unwrap_or_default();
        let progress_map: std::collections::HashMap<String, i64> = progress_rows.into_iter().collect();

        // ── Active ticker totals ────────────────────────────────────────────
        let us_total: i64 = sqlx::query_scalar(
            "SELECT COUNT(*) FROM us_stock_basic WHERE is_actively_trading = 1"
        ).fetch_one(&pool).await.unwrap_or(0);
        let cn_total: i64 = sqlx::query_scalar(
            "SELECT COUNT(*) FROM a_stock_basic WHERE list_status = 'L'"
        ).fetch_one(&pool).await.unwrap_or(0);

        // ── Header ──────────────────────────────────────────────────────────
        let sep = "─".repeat(105);
        println!();
        println!("{:<32} {:>12} {:>13} {:>19} {:>15}", "TABLE", "ROWS", "LATEST DATA", "LAST UPDATE", "PROGRESS");
        println!("{sep}");

        let mut current_market = "";
        let mut total_rows = 0i64;
        for spec in DB_STATUS_TABLES {
            if market_filter != "all" && spec.market != market_filter && spec.market != "shared" {
                continue;
            }
            // Group header
            if spec.market != current_market {
                let label = match spec.market {
                    "us" => "[US]",
                    "cn" => "[CN]",
                    _ => "[shared]",
                };
                println!("{label}");
                current_market = spec.market;
            }

            let denom = match spec.market {
                "us" => us_total,
                "cn" => cn_total,
                _ => 0,
            };
            print_db_status_row(&pool, spec, &progress_map, denom, &mut total_rows).await;
        }

        println!("{sep}");
        println!("{:<32} {:>12}", "TOTAL", total_rows);

        pool.close().await;
    });
}

async fn print_db_status_row(
    pool: &sqlx::MySqlPool,
    spec: &DbStatusTable,
    progress_map: &std::collections::HashMap<String, i64>,
    denom: i64,
    total_rows: &mut i64,
) {
    // Single scan for COUNT, MAX(date_col), MAX(updated_col).
    // import_progress uses `completed_at` instead of `updated_at`.
    let updated_col = if spec.name == "import_progress" { "completed_at" } else { "updated_at" };
    let select_cols = match spec.date_col {
        Some(c) => format!("COUNT(*), CAST(MAX({c}) AS CHAR), CAST(MAX({updated_col}) AS CHAR)"),
        None    => format!("COUNT(*), CAST(NULL AS CHAR), CAST(MAX({updated_col}) AS CHAR)"),
    };
    let sql = format!("SELECT {select_cols} FROM {}", spec.name);
    let row: Result<(i64, Option<String>, Option<String>), _> =
        sqlx::query_as(&sql).fetch_one(pool).await;

    let (count, latest, last_update) = match row {
        Ok(r) => r,
        Err(e) => {
            println!("{:<32} {:>12} {:>13} {:>19} {:>15}", spec.name, "ERR", "-", "-", "-");
            warn!("db-status query failed for {}: {e}", spec.name);
            return;
        }
    };
    *total_rows += count;

    // YYYY-MM-DD (10) for date, YYYY-MM-DD HH:MM (16) for timestamp
    let latest_str = latest.as_deref().map(|s| s.get(..10).unwrap_or(s).to_string()).unwrap_or_else(|| "-".to_string());
    let last_str = last_update.as_deref().map(|s| s.get(..16).unwrap_or(s).to_string()).unwrap_or_else(|| "-".to_string());

    let progress_str = match progress_map.get(spec.name) {
        Some(&done) if denom > 0 => format!("{done}/{denom}"),
        Some(&done) => format!("{done}/-"),
        None => "-".to_string(),
    };

    println!(
        "{:<32} {:>12} {:>13} {:>19} {:>15}",
        spec.name, count, latest_str, last_str, progress_str
    );
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

// ── A-share paper trade ─────────────────────────────────────────────────

fn cmd_a_trade(
    config: &Config,
    account_id: &str,
    date_str: &str,
    signals_path: &PathBuf,
    dry_run: bool,
    no_risk: bool,
) {
    use quant_backtest::a_exec::{self, ACostConfig, CachedQuotes};
    use quant_factors::a_share::universe::{AUniverseFilter, get_a_clean_universe};
    use quant_trading::broker::Broker;
    use quant_trading::paper::PaperBroker;
    use quant_trading::risk::{self, RiskConfig};

    let date = match chrono::NaiveDate::parse_from_str(date_str, "%Y-%m-%d") {
        Ok(d) => d,
        Err(e) => {
            eprintln!("Invalid date: {e}");
            std::process::exit(1);
        }
    };

    // Load signals JSON: {"600519.SH": 0.05, ...}
    let raw = match std::fs::read_to_string(signals_path) {
        Ok(s) => s,
        Err(e) => {
            eprintln!("Failed to read signals {}: {e}", signals_path.display());
            std::process::exit(1);
        }
    };
    let raw_weights: rustc_hash::FxHashMap<String, f64> = match serde_json::from_str(&raw) {
        Ok(w) => w,
        Err(e) => {
            eprintln!("Failed to parse signals JSON: {e}");
            std::process::exit(1);
        }
    };
    let total_w: f64 = raw_weights.values().sum();
    info!("Signals: {} tickers, total weight {:.4}", raw_weights.len(), total_w);
    if total_w > 1.001 {
        eprintln!("Warning: total weight {} > 1.0", total_w);
    }

    let db_url = config.database.url();
    let schema = &config.database.schema;
    if db_url.contains("@:/") || db_url.contains("mysql://:@") {
        eprintln!("Database not configured. Set DB_HOST/USER/PASSWORD/DATABASE.");
        std::process::exit(1);
    }

    let rt = tokio::runtime::Runtime::new().expect("tokio runtime");
    rt.block_on(async {
        let pool = quant_db::pool::create_pool(&db_url, schema, 8).await
            .expect("connect to db");

        // Load cache for the trading day. We need ~60 days of price for liquidity calc
        // and basics for ST detection.
        let load_start = date - chrono::Duration::days(120);
        let load_end = date + chrono::Duration::days(1);
        info!("Loading A-share cache [{}, {}]...", load_start, load_end);
        let cache = build_a_share_cache(&pool, load_start, load_end).await;
        info!("Cache: {} stocks, {} basics, {} industries",
              cache.daily.len(), cache.basics.len(), cache.industry.len());

        let cost = ACostConfig::from_a_share(&config.a_share);

        // Defensive: enforce clean universe even if upstream signals didn't filter.
        let filter = AUniverseFilter::from_config(&config.a_share.universe);
        let universe = get_a_clean_universe(date, &cache, &filter);
        let dropped: Vec<String> = raw_weights.keys()
            .filter(|c| !universe.contains(c.as_str())).cloned().collect();
        if !dropped.is_empty() {
            info!("Dropped {} signal(s) outside clean universe: {:?}",
                  dropped.len(), &dropped[..dropped.len().min(10)]);
        }
        let target_weights: rustc_hash::FxHashMap<String, f64> = raw_weights.into_iter()
            .filter(|(c, _)| universe.contains(c.as_str())).collect();
        info!("Clean universe: {}/{}; signals retained: {}",
              universe.len(), cache.ts_codes.len(), target_weights.len());

        let quotes = CachedQuotes { cache: &cache, date };

        // ── Plan ──
        let initial_capital = config.a_share.execution.initial_capital;
        let broker = PaperBroker::new(pool.clone(), account_id.to_string(), cost.clone(), quotes);

        if !dry_run {
            broker.init(initial_capital).await
                .expect("init account");
        }

        // Load current state (or fresh if dry-run)
        let positions = if dry_run {
            rustc_hash::FxHashMap::default()
        } else {
            let snap = broker.snapshot().await.expect("snapshot");
            snap.positions.iter().map(|p| (p.ts_code.clone(), p.shares)).collect()
        };
        let cash = if dry_run { initial_capital }
                   else { broker.snapshot().await.unwrap().cash };

        let q = CachedQuotes { cache: &cache, date };
        let total_value = a_exec::portfolio_value(&positions, &q, cash);
        let planned = a_exec::plan_orders(&positions, &target_weights, total_value, &q, &cost);
        info!("Planned {} orders (total NAV est. {:.0})", planned.len(), total_value);

        // ── Risk gate ──
        let final_orders = if no_risk {
            planned
        } else {
            let rc = RiskConfig::from_strategy(&config.a_share.strategy);
            let (filtered, report) = risk::apply(
                &planned, &positions, cash, total_value, date, &q, &cache, &rc, &cost,
            );
            info!("Risk: {} skipped, {} downscaled",
                  report.skipped_count(), report.downscaled_count());
            filtered
        };

        println!("\n=== Order list ({} orders) ===", final_orders.len());
        println!("{:<6} {:<12} {:>8} {:>12}", "SIDE", "TS_CODE", "SHARES", "PRICE_EST");
        for o in &final_orders {
            let bar = cache.get_bar(&o.ts_code, date);
            let price = bar.map(|b| b.open).unwrap_or(f64::NAN);
            println!("{:<6} {:<12} {:>8} {:>12.4}", o.side.as_str(), o.ts_code, o.shares, price);
        }

        if dry_run {
            println!("\n[dry-run] no DB writes performed");
            pool.close().await;
            return;
        }

        // ── Submit ──
        let fills = broker.submit(date, &final_orders).await.expect("submit");
        let snap = broker.snapshot().await.expect("snapshot");
        println!("\n=== Fills ({}) ===", fills.len());
        for f in &fills {
            println!("{:<6} {:<12} {:>8} @ {:>10.4}  fees={:.2}  gross={:.0}",
                     f.side.as_str(), f.ts_code, f.shares, f.price, f.fees, f.gross);
        }
        println!("\n=== Account snapshot ===");
        println!("cash         = {:.2}", snap.cash);
        println!("market value = {:.2}", snap.total_market_value);
        println!("nav          = {:.4}x", snap.nav);
        println!("positions    = {}", snap.positions.len());

        pool.close().await;
    });
}

// ============================================================
// Alpaca paper / live trading (US)
// ============================================================

fn cmd_alpaca(action: AlpacaAction) {
    let rt = tokio::runtime::Runtime::new().expect("Failed to create tokio runtime");
    rt.block_on(async {
        let client = match quant_trading::us_alpaca::AlpacaClient::from_env() {
            Ok(c) => c,
            Err(e) => {
                eprintln!("Alpaca client init failed: {e}");
                eprintln!("Env vars required: ALPACA_API_KEY, ALPACA_SECRET_KEY (ALPACA_PAPER=true default)");
                std::process::exit(1);
            }
        };

        match action {
            AlpacaAction::Status => alpaca_status(&client).await,
            AlpacaAction::Plan { signals } => alpaca_plan(&client, &signals, true).await,
            AlpacaAction::Run { signals, dry_run } => alpaca_plan(&client, &signals, dry_run).await,
            AlpacaAction::Compare { period, benchmark, csv } => {
                alpaca_compare(&client, &period, &benchmark, csv.as_deref()).await
            }
        }
    });
}

async fn alpaca_status(client: &quant_trading::us_alpaca::AlpacaClient) {
    let state = match client.portfolio_state().await {
        Ok(s) => s,
        Err(e) => {
            eprintln!("portfolio_state failed: {e}");
            std::process::exit(1);
        }
    };

    println!("\n=== Alpaca Account ===");
    println!("ID:                 {}", state.account.id);
    println!("Status:             {}", state.account.status);
    println!("Cash:               ${:>14.2}", state.account.cash);
    println!("Equity:             ${:>14.2}", state.account.equity);
    println!("Portfolio Value:    ${:>14.2}", state.account.portfolio_value);
    println!("Buying Power:       ${:>14.2}", state.account.buying_power);
    println!("Long Market Value:  ${:>14.2}", state.account.long_market_value);
    println!("Short Market Value: ${:>14.2}", state.account.short_market_value);
    println!("PDT:                {}", state.account.pattern_day_trader);

    println!("\n=== Positions ({}) ===", state.positions.len());
    if state.positions.is_empty() {
        println!("(空)");
    } else {
        println!("{:<8} {:>9} {:>10} {:>10} {:>12} {:>10} {:<6}",
                 "Symbol", "Qty", "Avg Cost", "Current", "MktValue", "PnL", "Side");
        let mut sorted: Vec<_> = state.positions.values().collect();
        sorted.sort_by(|a, b| {
            b.market_value.abs().partial_cmp(&a.market_value.abs()).unwrap_or(std::cmp::Ordering::Equal)
        });
        for p in sorted {
            println!(
                "{:<8} {:>9.0} {:>10.2} {:>10.2} ${:>10.2} {:>+10.2} {:<6}",
                p.symbol, p.qty, p.avg_entry_price, p.current_price,
                p.market_value, p.unrealized_pl, p.side
            );
        }
    }

    match client.open_orders().await {
        Ok(orders) => {
            println!("\n=== Open Orders ({}) ===", orders.len());
            if orders.is_empty() {
                println!("(无挂单)");
            } else {
                println!("{:<8} {:>6} {:<6} {:<12} {}",
                         "Symbol", "Qty", "Side", "Status", "Created");
                for o in &orders {
                    println!("{:<8} {:>6} {:<6} {:<12} {}",
                             o.symbol, o.qty, o.side, o.status, o.created_at);
                }
            }
        }
        Err(e) => eprintln!("open_orders 查询失败: {e}"),
    }
}

async fn alpaca_plan(
    client: &quant_trading::us_alpaca::AlpacaClient,
    signals_path: &std::path::Path,
    dry_run: bool,
) {
    let weights = match quant_trading::us_alpaca::load_signals_json(signals_path) {
        Ok(w) => w,
        Err(e) => {
            eprintln!("Load signals failed: {e}");
            std::process::exit(1);
        }
    };
    info!("Loaded {} target weights from {}", weights.len(), signals_path.display());

    let plan = match client.plan_rebalance(&weights, None).await {
        Ok(p) => p,
        Err(e) => {
            eprintln!("plan_rebalance failed: {e}");
            std::process::exit(1);
        }
    };

    println!("\n=== Rebalance Plan ===");
    println!("Equity: ${:.2} / Cash: ${:.2}", plan.equity, plan.cash);
    println!("\n{:<8} {:>10} {:>10} {:>10} {:>12} {:<6}",
             "Symbol", "Current", "Target", "Δ", "$Target", "Side");
    println!("{}", "-".repeat(72));
    for o in &plan.orders {
        println!(
            "{:<8} {:>10.0} {:>10.0} {:>+10.0} ${:>10.2} {:<6}",
            o.symbol, o.current_shares, o.target_shares, o.delta_shares,
            o.target_dollar, o.side
        );
    }
    let total_buy: f64 = plan.orders.iter()
        .filter(|o| o.side == "buy")
        .map(|o| o.delta_shares.abs() * o.current_price)
        .sum();
    let total_sell: f64 = plan.orders.iter()
        .filter(|o| o.side == "sell")
        .map(|o| o.delta_shares.abs() * o.current_price)
        .sum();
    println!("\nTotal: {} orders, ~${:.0} buy + ~${:.0} sell",
             plan.orders.len(), total_buy, total_sell);

    if dry_run {
        println!("\n[DRY RUN] 不下单。要真正提交请不带 --dry-run 跑 `alpaca run`。");
        return;
    }

    println!("\n提交订单...");
    let report = client.execute_plan(&plan).await;

    println!("✓ Submitted {} orders.", report.submitted.len());
    for o in report.submitted.iter().take(5) {
        println!("  {} {} {} (id={}, status={})", o.side, o.qty, o.symbol, o.id, o.status);
    }
    if report.submitted.len() > 5 {
        println!("  ... ({} more)", report.submitted.len() - 5);
    }

    if !report.failed.is_empty() {
        eprintln!("\n✗ {} order(s) FAILED — 实际持仓将偏离目标组合:", report.failed.len());
        for f in &report.failed {
            eprintln!("  {} {} {}: {}", f.side, f.shares, f.symbol, f.error);
        }
        eprintln!("\n处理建议: 手动补单 / 换标的 / 下次 rebalance 重试。退出码 = 2。");
        std::process::exit(2);
    }
}

/// 组合 NAV vs benchmark 对比（默认 SPY）
///
/// Alpaca portfolio_history 端点返回 unix-second 时间戳 + equity 序列。
/// Benchmark 走 data.alpaca.markets 历史日线（adjustment=all 含 splits/dividends）。
/// 按日期对齐两条序列、归一化到起点 = 1.0，输出 NAV + 累计 ret 表。
async fn alpaca_compare(
    client: &quant_trading::us_alpaca::AlpacaClient,
    period: &str,
    benchmark: &str,
    csv_out: Option<&std::path::Path>,
) {
    use chrono::TimeZone;

    // period=1D 必须用 intraday timeframe，否则 EOD 快照前永远是 0 样本。
    // 其余 period 用日线最实用。
    let intraday = period == "1D";
    let timeframe = if intraday { "5Min" } else { "1D" };
    let bar_timeframe = if intraday { "5Min" } else { "1Day" };

    let history = match client.portfolio_history(period, timeframe).await {
        Ok(h) => h,
        Err(e) => {
            eprintln!("portfolio_history 查询失败: {e}");
            eprintln!("提示: period 必须是 1D/1W/1M/3M/1A/all 之一");
            std::process::exit(1);
        }
    };

    if history.timestamp.is_empty() {
        eprintln!("portfolio_history 返回空 — 账户太新或 period 无样本");
        std::process::exit(1);
    }

    // unix-sec → NaiveDateTime (UTC)。daily 模式会按日期 dedup（一日一点），
    // intraday 保留每 5 分钟样本。
    let mut port_series: Vec<(chrono::NaiveDateTime, f64)> = history.timestamp.iter()
        .zip(history.equity.iter())
        .filter_map(|(&ts, &eq)| {
            if eq <= 0.0 { return None; }
            let dt = chrono::Utc.timestamp_opt(ts, 0).single()?;
            Some((dt.naive_utc(), eq))
        })
        .collect();
    port_series.sort_by_key(|(t, _)| *t);
    if !intraday {
        port_series.dedup_by_key(|(t, _)| t.date());
    }

    if port_series.len() < 2 {
        eprintln!(
            "组合只有 {} 个样本（period={period}），至少需要 2 个日点才能算累计 ret。\
             \n账户刚开仓时正常 — 等积累几天后再跑。",
            port_series.len()
        );
        if let Some(&(_, eq)) = port_series.first() {
            println!("当前 equity: ${:.2}（base_value=${:.2}）", eq, history.base_value);
        }
        return;
    }

    let start_dt = port_series.first().unwrap().0;
    let end_dt = port_series.last().unwrap().0;

    // 拉 benchmark bars。intraday 用 5Min，daily 用 1Day；起点回看 1 天/5 天保证对齐。
    let lookback_days = if intraday { 1 } else { 5 };
    let bm_start = (start_dt.date() - chrono::Duration::days(lookback_days))
        .format("%Y-%m-%d").to_string();
    let bm_end = (end_dt.date() + chrono::Duration::days(1))
        .format("%Y-%m-%d").to_string();
    let bars = match client.historical_bars(benchmark, &bm_start, &bm_end, bar_timeframe).await {
        Ok(b) => b,
        Err(e) => {
            eprintln!("{benchmark} 历史 K 线查询失败: {e}");
            std::process::exit(1);
        }
    };
    if bars.is_empty() {
        eprintln!("{benchmark} 在 [{bm_start}, {bm_end}] 无 bar — 检查 ticker 拼写");
        std::process::exit(1);
    }

    // Bar.t = "2026-05-12T04:00:00Z" — parse to NaiveDateTime
    let mut bm_by_time: std::collections::BTreeMap<chrono::NaiveDateTime, f64> =
        std::collections::BTreeMap::new();
    for b in &bars {
        if let Ok(dt) = chrono::DateTime::parse_from_rfc3339(&b.t) {
            bm_by_time.insert(dt.naive_utc(), b.c);
        }
    }

    // 起点对齐：找组合首点 ≤ 的最近 bm bar
    let bm_base = bm_by_time.range(..=start_dt).next_back().map(|(_, &c)| c)
        .or_else(|| bm_by_time.values().next().copied());
    let bm_base = match bm_base {
        Some(c) if c > 0.0 => c,
        _ => {
            eprintln!("{benchmark} 起点价格无效");
            std::process::exit(1);
        }
    };

    let port_base = port_series.first().unwrap().1;

    println!("\n=== NAV 对比: 组合 vs {} ===", benchmark);
    let stamp_fmt = if intraday { "%Y-%m-%d %H:%M" } else { "%Y-%m-%d" };
    let stamp_w = if intraday { 17 } else { 12 };
    println!("Period: {period} ({} 个样本, {} → {})",
        port_series.len(),
        start_dt.format(stamp_fmt),
        end_dt.format(stamp_fmt),
    );
    println!("起点 equity = ${:.2}, 起点 {} 价 = ${:.2}\n", port_base, benchmark, bm_base);
    println!("{:<width$} {:>12} {:>10} {:>12} {:>10} {:>10}",
        "Time", "Equity", "Port NAV", &format!("{} Close", benchmark), "BM NAV", "Excess",
        width = stamp_w);
    println!("{}", "-".repeat(stamp_w + 58));

    let mut last_bm_price = bm_base;
    let mut last_port_nav = 1.0;
    let mut last_bm_nav = 1.0;
    let mut csv_rows: Vec<(String, f64, f64)> = Vec::new();
    for (t, eq) in &port_series {
        // 取最近 ≤t 的 bm bar
        let bm_close = bm_by_time.range(..=*t).next_back().map(|(_, &c)| c)
            .unwrap_or(last_bm_price);
        last_bm_price = bm_close;

        let port_nav = eq / port_base;
        let bm_nav = bm_close / bm_base;
        let excess = port_nav - bm_nav;
        println!("{:<width$} ${:>11.2} {:>10.4} ${:>11.2} {:>10.4} {:>+10.4}",
            t.format(stamp_fmt).to_string(), eq, port_nav, bm_close, bm_nav, excess,
            width = stamp_w);
        last_port_nav = port_nav;
        last_bm_nav = bm_nav;
        csv_rows.push((t.format(stamp_fmt).to_string(), port_nav, bm_nav));
    }

    let port_total_ret = (last_port_nav - 1.0) * 100.0;
    let bm_total_ret = (last_bm_nav - 1.0) * 100.0;
    let excess_total = port_total_ret - bm_total_ret;
    println!("\n=== 累计 ===");
    println!("组合:     {:>+8.2}%", port_total_ret);
    println!("{}:     {:>+8.2}%", benchmark, bm_total_ret);
    println!("超额:     {:>+8.2}% ({})",
        excess_total,
        if excess_total >= 0.0 { "组合跑赢" } else { "组合跑输" });

    // 注意事项
    if port_series.len() < 10 {
        println!("\n⚠ 样本只有 {} 个日点，统计噪声很大，仅供参考。", port_series.len());
    }

    if let Some(out) = csv_out {
        let mut csv = String::from("time,portfolio_nav,benchmark_nav,excess\n");
        for (t, pn, bn) in &csv_rows {
            csv.push_str(&format!("{},{},{},{}\n", t, pn, bn, pn - bn));
        }
        if let Err(e) = std::fs::write(out, csv) {
            eprintln!("写 CSV {} 失败: {e}", out.display());
        } else {
            println!("\nCSV 导出 → {}", out.display());
        }
    }
}

// ============================================================
// MySQL → Parquet 导出（替代旧 Python pandas 脚本）
// ============================================================

/// (MySQL 表名, parquet 文件名前缀, 时序列名 / 快照表用 None)
struct ExportSpec {
    table: &'static str,
    parquet_basename: &'static str,
    /// 时序表的日期列。None = snapshot 表，文件名 `{name}_all.parquet`
    date_col: Option<&'static str>,
}

/// v25 baseline + A 股迁移后需要的所有表
const EXPORT_TABLES: &[ExportSpec] = &[
    // 美股时序
    ExportSpec { table: "us_daily_price",            parquet_basename: "us_daily_price",            date_col: Some("trade_date") },
    ExportSpec { table: "us_index_daily",            parquet_basename: "us_index_daily",            date_col: Some("trade_date") },
    ExportSpec { table: "us_financial_data",         parquet_basename: "alpha_financial",           date_col: Some("filing_date") },
    ExportSpec { table: "us_key_metric",             parquet_basename: "alpha_key_metric",          date_col: Some("date") },
    ExportSpec { table: "us_enterprise_value",       parquet_basename: "alpha_enterprise_value",    date_col: Some("date") },
    ExportSpec { table: "us_analyst_recommendation", parquet_basename: "us_analyst_recommendation", date_col: Some("date") },
    ExportSpec { table: "us_earnings_surprise",      parquet_basename: "us_earnings_surprise",      date_col: Some("date") },
    ExportSpec { table: "us_eps_estimate",           parquet_basename: "us_eps_estimate",           date_col: Some("date") },
    ExportSpec { table: "us_corporate_action",       parquet_basename: "us_corporate_action_div",   date_col: Some("date") },
    ExportSpec { table: "us_insider_trade",          parquet_basename: "us_insider_trade",          date_col: Some("transaction_date") },
    ExportSpec { table: "us_employee_count",         parquet_basename: "us_employee_count",         date_col: Some("filing_date") },
    ExportSpec { table: "us_revenue_segment",        parquet_basename: "us_revenue_segment",        date_col: Some("date") },
    ExportSpec { table: "us_macro_indicator",        parquet_basename: "us_macro_indicator",        date_col: Some("report_date") },
    // 美股快照
    ExportSpec { table: "us_stock_basic",            parquet_basename: "us_stock_basic",            date_col: None },
    ExportSpec { table: "us_industry_class",         parquet_basename: "us_industry_class",         date_col: None },
    ExportSpec { table: "us_shares_float",           parquet_basename: "us_shares_float",           date_col: None },
    ExportSpec { table: "us_esg_rating",             parquet_basename: "us_esg_rating",             date_col: None },
    // A 股时序
    ExportSpec { table: "a_daily_price",             parquet_basename: "a_daily_price",             date_col: Some("trade_date") },
    ExportSpec { table: "a_financial_indicator",     parquet_basename: "a_financial_indicator",     date_col: Some("end_date") },
    ExportSpec { table: "a_index_daily",             parquet_basename: "a_index_daily",             date_col: Some("trade_date") },
    ExportSpec { table: "a_macro_indicator",         parquet_basename: "a_macro_indicator",         date_col: Some("report_date") },
    // A 股快照
    ExportSpec { table: "a_stock_basic",             parquet_basename: "a_stock_basic",             date_col: None },
    ExportSpec { table: "a_industry_class",          parquet_basename: "a_industry_class",          date_col: None },
    ExportSpec { table: "a_trade_cal",               parquet_basename: "a_trade_cal",               date_col: None },
];

fn cmd_export_parquet(
    config: &quant_core::config::Config,
    output_dir: &PathBuf,
    filter_tables: &[String],
) {
    let db_url = config.database.url();
    if db_url.contains("@:/") || db_url.contains("mysql://:@") {
        eprintln!("Database not configured. Set DB_HOST/DB_USER/DB_PASSWORD/DB_DATABASE.");
        std::process::exit(1);
    }

    std::fs::create_dir_all(output_dir).expect("Failed to create output dir");

    let rt = tokio::runtime::Runtime::new().expect("Failed to create tokio runtime");
    let pool = rt.block_on(async {
        quant_db::pool::create_pool(&db_url, &config.database.schema, 4).await
            .expect("Failed to connect to database")
    });

    let specs: Vec<&ExportSpec> = if filter_tables.is_empty() {
        EXPORT_TABLES.iter().collect()
    } else {
        EXPORT_TABLES.iter()
            .filter(|s| filter_tables.iter().any(|f| f == s.table || f == s.parquet_basename))
            .collect()
    };

    println!("\n=== Export {} tables → {} ===", specs.len(), output_dir.display());
    let total_t0 = std::time::Instant::now();
    let mut total_rows = 0usize;
    let mut failures = Vec::new();

    for spec in &specs {
        let t0 = std::time::Instant::now();
        match rt.block_on(export_one_table(spec, &pool, output_dir)) {
            Ok((path, rows)) => {
                total_rows += rows;
                println!(
                    "✓ {:<35} {:>12} rows → {} ({:.1}s)",
                    spec.table, rows, path.file_name().unwrap().to_string_lossy(), t0.elapsed().as_secs_f64()
                );
            }
            Err(e) => {
                println!("✗ {:<35} FAILED: {}", spec.table, e);
                failures.push(spec.table.to_string());
            }
        }
    }

    println!(
        "\nTotal: {} tables, {} rows, {:.1}s",
        specs.len() - failures.len(), total_rows, total_t0.elapsed().as_secs_f64()
    );
    if !failures.is_empty() {
        eprintln!("Failed: {}", failures.join(", "));
        std::process::exit(1);
    }

    rt.block_on(async { pool.close().await });
}

async fn export_one_table(
    spec: &ExportSpec,
    pool: &sqlx::MySqlPool,
    output_dir: &PathBuf,
) -> Result<(std::path::PathBuf, usize), String> {
    use sqlx::Row;

    // Step 1: 决定文件名（snapshot vs 时序）
    let suffix = if let Some(date_col) = spec.date_col {
        // 取 min/max date 作日期范围
        let row: (Option<chrono::NaiveDate>, Option<chrono::NaiveDate>) =
            sqlx::query_as(&format!(
                "SELECT MIN(`{date_col}`), MAX(`{date_col}`) FROM `{}`",
                spec.table
            ))
            .fetch_one(pool)
            .await
            .map_err(|e| format!("min/max query: {e}"))?;
        match (row.0, row.1) {
            (Some(min), Some(max)) => format!("{min}_{max}"),
            _ => return Err("table is empty".to_string()),
        }
    } else {
        "all".to_string()
    };

    let parquet_name = format!("{}_{}.parquet", spec.parquet_basename, suffix);
    let parquet_path = output_dir.join(&parquet_name);
    let csv_path = output_dir.join(format!(".{}_{}_tmp.csv", spec.parquet_basename, suffix));

    // Step 2: query MySQL with textual casts, then write an RFC 4180 CSV.
    // Casting lets one generic exporter preserve every source column without
    // relying on database-client-specific CSV protocols.
    let columns: Vec<String> = sqlx::query_scalar(&format!("SHOW COLUMNS FROM `{}`", spec.table))
        .fetch_all(pool)
        .await
        .map_err(|e| format!("inspect columns: {e}"))?;
    if columns.is_empty() {
        return Err("table has no columns".to_string());
    }
    let select_exprs = columns.iter()
        .map(|column| format!("CAST(`{column}` AS CHAR) AS `{column}`"))
        .collect::<Vec<_>>()
        .join(", ");
    let rows = sqlx::query(&format!("SELECT {select_exprs} FROM `{}`", spec.table))
        .fetch_all(pool)
        .await
        .map_err(|e| format!("select rows: {e}"))?;

    let mut csv_file = std::io::BufWriter::new(
        std::fs::File::create(&csv_path).map_err(|e| format!("create csv: {e}"))?,
    );
    write_csv_record(&mut csv_file, columns.iter().map(String::as_str))
        .map_err(|e| format!("write CSV header: {e}"))?;
    for row in &rows {
        let values: Result<Vec<Option<String>>, _> = (0..columns.len())
            .map(|index| row.try_get(index))
            .collect();
        let values = values.map_err(|e| format!("decode row: {e}"))?;
        write_csv_record(
            &mut csv_file,
            values.iter().map(|value| value.as_deref().unwrap_or("")),
        ).map_err(|e| format!("write CSV row: {e}"))?;
    }
    use std::io::Write;
    csv_file.flush().map_err(|e| format!("flush CSV: {e}"))?;

    // Step 3: polars CSV → parquet
    use polars::prelude::*;
    let mut df = LazyCsvReader::new(&csv_path)
        .with_has_header(true)
        .with_try_parse_dates(true)
        .finish()
        .map_err(|e| format!("read csv: {e}"))?
        .collect()
        .map_err(|e| format!("collect csv: {e}"))?;

    let mut parquet_file = std::fs::File::create(&parquet_path)
        .map_err(|e| format!("create parquet: {e}"))?;
    ParquetWriter::new(&mut parquet_file)
        .finish(&mut df)
        .map_err(|e| format!("write parquet: {e}"))?;

    // Step 4: 删 CSV temp
    let _ = std::fs::remove_file(&csv_path);

    let n_rows = rows.len();
    Ok((parquet_path, n_rows))
}

fn write_csv_record<'a>(
    writer: &mut impl std::io::Write,
    values: impl IntoIterator<Item = &'a str>,
) -> std::io::Result<()> {
    for (index, value) in values.into_iter().enumerate() {
        if index > 0 {
            writer.write_all(b",")?;
        }
        if value.contains([',', '"', '\n', '\r']) {
            writer.write_all(b"\"")?;
            writer.write_all(value.replace('"', "\"\"").as_bytes())?;
            writer.write_all(b"\"")?;
        } else {
            writer.write_all(value.as_bytes())?;
        }
    }
    writer.write_all(b"\n")
}
