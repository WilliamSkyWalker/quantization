use std::path::PathBuf;

use clap::{Parser, Subcommand, ValueEnum};
use rayon::prelude::*;
use tracing::{info, warn};
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

        /// Export the last-rebalance target weights to JSON (for paper trading).
        /// Path is written under the `output` dir as `signals_<last_date>.json`.
        #[arg(long)]
        export_signals: bool,
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
    },

    /// 美股 Alpaca paper / live 交易
    Alpaca {
        #[command(subcommand)]
        action: AlpacaAction,
    },

    /// 把 PostgreSQL 表导出到 parquet 缓存（替代旧 Python pandas 脚本）
    ExportParquet {
        /// 输出目录（默认 ../cache）
        #[arg(long, default_value = "../cache")]
        output_dir: PathBuf,

        /// 仅导出指定 PG 表（多次指定）。不传时导出所有 v25 baseline 需要的表。
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

    /// One-time migration: policy_article + policy_analysis + scrape_log
    /// from legacy MySQL → PostgreSQL. Reads from MYSQL_* env vars unless
    /// overridden by flags.
    MigratePolicy {
        #[arg(long, env = "MYSQL_HOST", default_value = "127.0.0.1")]
        mysql_host: String,
        #[arg(long, env = "MYSQL_PORT", default_value = "3306")]
        mysql_port: u16,
        #[arg(long, env = "MYSQL_USER", default_value = "root")]
        mysql_user: String,
        #[arg(long, env = "MYSQL_PASSWORD", default_value = "")]
        mysql_password: String,
        #[arg(long, env = "MYSQL_DATABASE", default_value = "quant")]
        mysql_database: String,
        /// Rows per fetch batch.
        #[arg(long, default_value = "1000")]
        batch: usize,
        /// Read MySQL only; report counts but don't write to PG.
        #[arg(long)]
        dry_run: bool,
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
        Commands::Score { date, top } => {
            info!("TODO: score --date {date} --top {top}");
        }
        Commands::DbStatus => {
            cmd_db_status(&_config);
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
            cmd_backtest(&_config, &cache_dir, &start, &end, &output, no_short, no_optimizer, export_signals);
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
        Commands::MigratePolicy {
            mysql_host, mysql_port, mysql_user, mysql_password, mysql_database, batch, dry_run,
        } => {
            cmd_migrate_policy(&_config, &mysql_host, mysql_port, &mysql_user,
                               &mysql_password, &mysql_database, batch, dry_run);
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
    if db_url.contains("@:/") || db_url.contains("postgres://:@") {
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

async fn build_a_share_cache(
    pool: &sqlx::PgPool,
    start: chrono::NaiveDate,
    end: chrono::NaiveDate,
) -> quant_factors::a_share::cache::AShareCache {
    use quant_factors::a_share::cache::{AShareCache, ABar, AFinIndicator, AIndustry, AStockInfo};
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
            gross_margin: r.gross_margin.unwrap_or(f64::NAN),
            netprofit_margin: r.netprofit_margin.unwrap_or(f64::NAN),
            q_profit_yoy: r.q_profit_yoy.unwrap_or(f64::NAN),
            q_sales_yoy: r.q_sales_yoy.unwrap_or(f64::NAN),
            q_netprofit_yoy: r.q_netprofit_yoy.unwrap_or(f64::NAN),
            current_ratio: r.current_ratio.unwrap_or(f64::NAN),
            ocf_to_profit: r.ocf_to_profit.unwrap_or(f64::NAN),
        };
        financials.entry(r.ts_code).or_default().push(fin);
    }
    // get_latest_fin/get_fin_history scan front-to-back expecting most-recent first.
    for v in financials.values_mut() {
        v.sort_by(|a, b| b.end_date.cmp(&a.end_date));
    }

    info!("Loading a_industry_class (SW2021 L1)...");
    let inds = quant_db::queries::a_read::get_a_industry_class(pool).await
        .expect("Failed to load a_industry_class");
    info!("Loaded {} industry mappings", inds.len());
    let industry: FxHashMap<String, AIndustry> = inds.into_iter()
        .map(|i| (i.ts_code, AIndustry {
            index_code: i.index_code.unwrap_or_default(),
            industry_name: i.industry_name.unwrap_or_default(),
        }))
        .collect();

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
        }))
        .collect();

    let ts_codes: Vec<String> = daily.keys().cloned().collect();

    AShareCache {
        daily,
        financials,
        industry,
        basics,
        trading_days,
        index_prices: FxHashMap::default(),
        ts_codes,
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
    export_signals: bool,
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
                        "all" => { dl.download_all(&start).await; }
                        other => {
                            eprintln!("Unknown Tushare target: {other}");
                            eprintln!("Available: stock_list, trade_cal, daily_price, income, balance,");
                            eprintln!("  cashflow, indicator, industry, index, macro, commodity, all");
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
    if db_url.contains("@:/") || db_url.contains("postgres://:@") {
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
                &planned, &positions, cash, total_value, &q, &cache, &rc, &cost,
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

// ── MySQL → PG one-time migration: policy_article / policy_analysis ─────

fn cmd_migrate_policy(
    config: &Config,
    mysql_host: &str,
    mysql_port: u16,
    mysql_user: &str,
    mysql_password: &str,
    mysql_database: &str,
    batch: usize,
    dry_run: bool,
) {
    let pg_url = config.database.url();
    let schema = &config.database.schema;
    if pg_url.contains("@:/") || pg_url.contains("postgres://:@") {
        eprintln!("PG not configured. Set DB_HOST/USER/PASSWORD/DATABASE.");
        std::process::exit(1);
    }
    let mysql_url = quant_download::a_policy_migrate::mysql_url(
        mysql_host, mysql_port, mysql_user, mysql_password, mysql_database,
    );
    info!("MySQL: {}@{}:{}/{}", mysql_user, mysql_host, mysql_port, mysql_database);
    info!("PG: {}", pg_url.split('@').last().unwrap_or(""));
    info!("batch={} dry_run={}", batch, dry_run);

    let rt = tokio::runtime::Runtime::new().expect("tokio runtime");
    rt.block_on(async {
        let pg_pool = quant_db::pool::create_pool(&pg_url, schema, 8).await
            .expect("connect to PG");

        let stats = quant_download::a_policy_migrate::migrate(
            &mysql_url, &pg_pool, batch, dry_run,
        ).await.expect("migration failed");

        println!("\n=== Migration {}===", if dry_run { "(dry-run) " } else { "" });
        println!("policy_article  read={:>7} inserted={:>7} skipped={:>7}",
                 stats.articles_read, stats.articles_inserted, stats.articles_skipped);
        println!("policy_analysis read={:>7} inserted={:>7} skipped={:>7}",
                 stats.analyses_read, stats.analyses_inserted, stats.analyses_skipped);
        println!("scrape_log      read={:>7} inserted={:>7}",
                 stats.scrape_logs_read, stats.scrape_logs_inserted);

        pg_pool.close().await;
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
    match client.execute_plan(&plan).await {
        Ok(orders) => {
            println!("✓ Submitted {} orders.", orders.len());
            for o in orders.iter().take(5) {
                println!("  {} {} {} (id={}, status={})", o.side, o.qty, o.symbol, o.id, o.status);
            }
            if orders.len() > 5 {
                println!("  ... ({} more)", orders.len() - 5);
            }
        }
        Err(e) => {
            eprintln!("execute_plan failed: {e}");
            std::process::exit(1);
        }
    }
}

// ============================================================
// PostgreSQL → Parquet 导出（替代旧 Python pandas 脚本）
// ============================================================

struct ResolvedDb {
    host: String,
    port: u16,
    user: String,
    password: String,
    database: String,
    schema: String,
}

/// (PG 表名, parquet 文件名前缀, 时序列名 / 快照表用 None)
struct ExportSpec {
    pg_table: &'static str,
    parquet_basename: &'static str,
    /// 时序表的日期列。None = snapshot 表，文件名 `{name}_all.parquet`
    date_col: Option<&'static str>,
}

/// v25 baseline + A 股迁移后需要的所有表
const EXPORT_TABLES: &[ExportSpec] = &[
    // 美股时序
    ExportSpec { pg_table: "us_daily_price",            parquet_basename: "us_daily_price",            date_col: Some("trade_date") },
    ExportSpec { pg_table: "us_index_daily",            parquet_basename: "us_index_daily",            date_col: Some("trade_date") },
    ExportSpec { pg_table: "us_financial_data",         parquet_basename: "alpha_financial",           date_col: Some("filing_date") },
    ExportSpec { pg_table: "us_key_metric",             parquet_basename: "alpha_key_metric",          date_col: Some("date") },
    ExportSpec { pg_table: "us_enterprise_value",       parquet_basename: "alpha_enterprise_value",    date_col: Some("date") },
    ExportSpec { pg_table: "us_analyst_recommendation", parquet_basename: "us_analyst_recommendation", date_col: Some("date") },
    ExportSpec { pg_table: "us_earnings_surprise",      parquet_basename: "us_earnings_surprise",      date_col: Some("date") },
    ExportSpec { pg_table: "us_eps_estimate",           parquet_basename: "us_eps_estimate",           date_col: Some("date") },
    ExportSpec { pg_table: "us_corporate_action",       parquet_basename: "us_corporate_action_div",   date_col: Some("date") },
    ExportSpec { pg_table: "us_insider_trade",          parquet_basename: "us_insider_trade",          date_col: Some("transaction_date") },
    ExportSpec { pg_table: "us_employee_count",         parquet_basename: "us_employee_count",         date_col: Some("filing_date") },
    ExportSpec { pg_table: "us_revenue_segment",        parquet_basename: "us_revenue_segment",        date_col: Some("date") },
    ExportSpec { pg_table: "us_macro_indicator",        parquet_basename: "us_macro_indicator",        date_col: Some("report_date") },
    // 美股快照
    ExportSpec { pg_table: "us_stock_basic",            parquet_basename: "us_stock_basic",            date_col: None },
    ExportSpec { pg_table: "us_industry_class",         parquet_basename: "us_industry_class",         date_col: None },
    ExportSpec { pg_table: "us_shares_float",           parquet_basename: "us_shares_float",           date_col: None },
    ExportSpec { pg_table: "us_esg_rating",             parquet_basename: "us_esg_rating",             date_col: None },
    // A 股时序
    ExportSpec { pg_table: "a_daily_price",             parquet_basename: "a_daily_price",             date_col: Some("trade_date") },
    ExportSpec { pg_table: "a_financial_indicator",     parquet_basename: "a_financial_indicator",     date_col: Some("end_date") },
    ExportSpec { pg_table: "a_index_daily",             parquet_basename: "a_index_daily",             date_col: Some("trade_date") },
    ExportSpec { pg_table: "a_macro_indicator",         parquet_basename: "a_macro_indicator",         date_col: Some("date") },
    // A 股快照
    ExportSpec { pg_table: "a_stock_basic",             parquet_basename: "a_stock_basic",             date_col: None },
    ExportSpec { pg_table: "a_industry_class",          parquet_basename: "a_industry_class",          date_col: None },
    ExportSpec { pg_table: "a_trade_cal",               parquet_basename: "a_trade_cal",               date_col: None },
];

fn cmd_export_parquet(
    config: &quant_core::config::Config,
    output_dir: &PathBuf,
    filter_tables: &[String],
) {
    // 从 config 拿，空则从 env 读取（config.toml 里 host/user 等默认是空字符串）
    let host = if !config.database.host.is_empty() { config.database.host.clone() }
               else { std::env::var("DB_HOST").unwrap_or_default() };
    let port = if config.database.port != 0 { config.database.port }
               else { std::env::var("DB_PORT").ok().and_then(|s| s.parse().ok()).unwrap_or(5432) };
    let user = if !config.database.user.is_empty() { config.database.user.clone() }
               else { std::env::var("DB_USER").unwrap_or_default() };
    let password = if !config.database.password.is_empty() { config.database.password.clone() }
                   else { std::env::var("DB_PASSWORD").unwrap_or_default() };
    let database = if !config.database.database.is_empty() { config.database.database.clone() }
                   else { std::env::var("DB_DATABASE").unwrap_or_default() };
    let schema = if !config.database.schema.is_empty() { config.database.schema.clone() }
                 else { std::env::var("DB_SCHEMA").unwrap_or_else(|_| "quant".to_string()) };

    if host.is_empty() || user.is_empty() {
        eprintln!("Database not configured. Set DB_HOST/DB_USER/DB_PASSWORD/DB_DATABASE in .env");
        std::process::exit(1);
    }

    let db = ResolvedDb { host, port, user, password, database, schema };

    // 找 psql 二进制
    let psql = which_psql().unwrap_or_else(|| {
        eprintln!("psql binary not found. Install PostgreSQL client.");
        std::process::exit(1);
    });

    std::fs::create_dir_all(output_dir).expect("Failed to create output dir");

    let rt = tokio::runtime::Runtime::new().expect("Failed to create tokio runtime");
    let url = format!("postgres://{}:{}@{}:{}/{}", db.user, db.password, db.host, db.port, db.database);
    let pool = rt.block_on(async {
        quant_db::pool::create_pool(&url, &db.schema, 4).await
            .expect("Failed to connect to database")
    });

    let specs: Vec<&ExportSpec> = if filter_tables.is_empty() {
        EXPORT_TABLES.iter().collect()
    } else {
        EXPORT_TABLES.iter()
            .filter(|s| filter_tables.iter().any(|f| f == s.pg_table || f == s.parquet_basename))
            .collect()
    };

    println!("\n=== Export {} tables → {} ===", specs.len(), output_dir.display());
    let total_t0 = std::time::Instant::now();
    let mut total_rows = 0usize;
    let mut failures = Vec::new();

    for spec in &specs {
        let t0 = std::time::Instant::now();
        match rt.block_on(export_one_table(spec, &pool, &psql, &db, output_dir)) {
            Ok((path, rows)) => {
                total_rows += rows;
                println!(
                    "✓ {:<35} {:>12} rows → {} ({:.1}s)",
                    spec.pg_table, rows, path.file_name().unwrap().to_string_lossy(), t0.elapsed().as_secs_f64()
                );
            }
            Err(e) => {
                println!("✗ {:<35} FAILED: {}", spec.pg_table, e);
                failures.push(spec.pg_table.to_string());
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

fn which_psql() -> Option<std::path::PathBuf> {
    for candidate in ["psql", "/usr/bin/psql", "/usr/local/bin/psql", "/opt/homebrew/bin/psql"] {
        let path = std::path::PathBuf::from(candidate);
        if path.is_absolute() && path.exists() {
            return Some(path);
        }
        if let Ok(out) = std::process::Command::new("which").arg(candidate).output() {
            if out.status.success() {
                let s = String::from_utf8_lossy(&out.stdout).trim().to_string();
                if !s.is_empty() {
                    return Some(std::path::PathBuf::from(s));
                }
            }
        }
    }
    None
}

async fn export_one_table(
    spec: &ExportSpec,
    pool: &sqlx::PgPool,
    psql: &std::path::Path,
    db: &ResolvedDb,
    output_dir: &PathBuf,
) -> Result<(std::path::PathBuf, usize), String> {
    // Step 1: 决定文件名（snapshot vs 时序）
    let suffix = if let Some(date_col) = spec.date_col {
        // 取 min/max date 作日期范围
        let row: (Option<chrono::NaiveDate>, Option<chrono::NaiveDate>) =
            sqlx::query_as(&format!(
                "SELECT MIN({date_col}), MAX({date_col}) FROM {}.{}",
                db.schema, spec.pg_table
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

    // Step 2: psql \COPY → CSV
    let copy_sql = format!(
        "\\COPY (SELECT * FROM {}.{}) TO STDOUT WITH CSV HEADER",
        db.schema, spec.pg_table
    );
    let csv_file = std::fs::File::create(&csv_path)
        .map_err(|e| format!("create csv: {e}"))?;

    let status = std::process::Command::new(psql)
        .env("PGPASSWORD", &db.password)
        .args([
            "-h", &db.host,
            "-p", &db.port.to_string(),
            "-U", &db.user,
            "-d", &db.database,
            "-c", &copy_sql,
        ])
        .stdout(csv_file)
        .stderr(std::process::Stdio::null())
        .status()
        .map_err(|e| format!("spawn psql: {e}"))?;

    if !status.success() {
        let _ = std::fs::remove_file(&csv_path);
        return Err(format!("psql exit {}", status.code().unwrap_or(-1)));
    }

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

    let n_rows = df.height();
    Ok((parquet_path, n_rows))
}
