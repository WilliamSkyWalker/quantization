//! Build DataCache from parquet files.
//! Converts Polars DataFrames into the HashMap-based DataCache.

use std::path::Path;
use std::sync::Mutex;

use chrono::NaiveDate;
use polars::prelude::*;
use rustc_hash::FxHashMap;
use tracing::{info, warn};

use quant_core::error::{QrsError, Result};
use quant_core::types::{Date, SectorInterner, TickerId, TickerInterner, YearMonth};

use crate::cache::*;
use crate::loader;
use crate::price_grid::PriceGrid;

/// Thread-safe ticker interner wrapper for parallel loading.
struct SharedInterner(Mutex<TickerInterner>);

impl SharedInterner {
    fn new() -> Self {
        Self(Mutex::new(TickerInterner::new()))
    }
    fn intern(&self, ticker: &str) -> TickerId {
        self.0.lock().unwrap().intern(ticker)
    }
    fn into_inner(self) -> TickerInterner {
        self.0.into_inner().unwrap()
    }
}

// Safety: SharedInterner uses Mutex internally
unsafe impl Sync for SharedInterner {}

/// Build a complete DataCache from the parquet cache directory.
/// If `start` and `end` are provided, only loads data needed for that range
/// (with lookback for factor computation).
pub fn build_cache(cache_dir: &Path) -> Result<DataCache> {
    build_cache_ranged(cache_dir, None, None)
}

/// Build DataCache with optional date range filtering.
/// Adds 2-year lookback before start for factor computation needs.
pub fn build_cache_ranged(cache_dir: &Path, start: Option<Date>, end: Option<Date>) -> Result<DataCache> {
    // Add lookback: factors need ~2 years of history before the start date
    let load_start = start.map(|s| s - chrono::Duration::days(800)); // ~2.2 years
    let load_end = end;

    if let (Some(s), Some(e)) = (load_start, load_end) {
        info!("Building DataCache from {} (date range: {} to {})", cache_dir.display(), s, e);
    } else {
        info!("Building DataCache from {} (full range)", cache_dir.display());
    }
    let t0 = std::time::Instant::now();

    let interner = SharedInterner::new();

    // Phase 1: Load daily prices + merge rolling stats into same HashMap
    let mut daily_prices = load_daily_prices(cache_dir, &interner, load_start, load_end)?;
    let index_prices = load_index_prices(cache_dir)?;
    let rolling_stats = load_rolling_stats_into(cache_dir, &interner, &mut daily_prices, load_start, load_end)?;
    let month_end_prices = compute_month_end_prices(&daily_prices);

    info!("Phase 1: {:.1}s — {} daily prices ({} with rolling stats)",
        t0.elapsed().as_secs_f64(), daily_prices.len(), rolling_stats);

    // Phase 2: Load remaining tables sequentially (small tables, fast)
    let t1 = std::time::Instant::now();
    let (financials, key_metrics) = load_financials(cache_dir, &interner)?;
    let enterprise_values = load_enterprise_values(cache_dir, &interner)?;
    let analyst_recs = load_analyst_recs(cache_dir, &interner)?;
    let earnings_surprises = load_earnings_surprises(cache_dir, &interner)?;
    let eps_estimates = load_eps_estimates(cache_dir, &interner)?;
    let dividends = load_dividends(cache_dir, &interner)?;
    let insider_trades = load_insider_trades(cache_dir, &interner)?;
    let shares_float = load_shares_float(cache_dir, &interner)?;
    let dark_pool = load_dark_pool(cache_dir, &interner)?;
    let institutional = load_institutional(cache_dir, &interner)?;
    let esg_ratings = load_esg_ratings(cache_dir, &interner)?;
    let employee_counts = load_employee_counts(cache_dir, &interner)?;
    let congress_trades = load_congress_trades(cache_dir, &interner)?;
    let gov_contracts = load_gov_contracts(cache_dir, &interner)?;
    let lobbying = load_lobbying(cache_dir, &interner)?;
    let revenue_segments = load_revenue_segments(cache_dir, &interner)?;
    let mut sector_interner = SectorInterner::new();
    let (sector_map, industry_map) = load_industry_class(cache_dir, &interner, &mut sector_interner)?;

    info!("Phase 2: {:.1}s", t1.elapsed().as_secs_f64());

    // Build trading calendar from index prices
    let mut trading_days: Vec<Date> = index_prices
        .keys()
        .filter(|(idx, _)| idx == "^GSPC")
        .map(|(_, d)| *d)
        .collect();
    trading_days.sort();

    let final_interner = interner.into_inner();

    info!(
        "DataCache built in {:.1}s: {} tickers, {} trading days, {} daily prices ({} with rolling stats)",
        t0.elapsed().as_secs_f64(),
        final_interner.len(),
        trading_days.len(),
        daily_prices.len(),
        rolling_stats,
    );

    Ok(DataCache {
        daily_prices,
        index_prices,
        month_end_prices,
        financials,
        key_metrics,
        enterprise_values,
        analyst_recs,
        earnings_surprises,
        eps_estimates,
        dividends,
        insider_trades,
        shares_float,
        dark_pool,
        institutional,
        esg_ratings,
        employee_counts,
        congress_trades,
        gov_contracts,
        lobbying,
        revenue_segments,
        sector_map,
        industry_map,
        ipo_dates: FxHashMap::default(),
        is_active: FxHashMap::default(),
        trading_days,
        ticker_interner: final_interner,
        sector_interner,
    })
}

// ===== Helper: extract column as typed iterator =====

fn get_str_col<'a>(df: &'a DataFrame, col: &str) -> Result<&'a StringChunked> {
    let series = df.column(col).map_err(|_| QrsError::MissingColumn {
        table: String::new(),
        column: col.to_string(),
    })?;
    // Handle Null-typed columns (all values are null)
    if matches!(series.dtype(), DataType::Null) {
        // Return a reference to a Null-typed StringChunked won't work,
        // so we use a different approach — cast first
        // Actually, for Null columns, just error gracefully — caller should use get_str_col_opt
        return Err(QrsError::DataLoad(format!("Column {col} is Null type")));
    }
    series
        .str()
        .map_err(|e| QrsError::DataLoad(format!("Column {col} is not String: {e}")))
}

/// Get a string column, returning empty strings for all-null columns.
fn get_str_values(df: &DataFrame, col: &str) -> Vec<String> {
    match df.column(col) {
        Ok(series) => {
            if matches!(series.dtype(), DataType::Null) {
                return vec![String::new(); df.height()];
            }
            match series.str() {
                Ok(ca) => ca
                    .into_iter()
                    .map(|opt| opt.unwrap_or("").to_string())
                    .collect(),
                Err(_) => vec![String::new(); df.height()],
            }
        }
        Err(_) => vec![String::new(); df.height()],
    }
}

fn get_date_col(df: &DataFrame, col: &str) -> Result<Vec<Option<NaiveDate>>> {
    let series = df.column(col).map_err(|_| QrsError::MissingColumn {
        table: String::new(),
        column: col.to_string(),
    })?;

    // Handle both Date and Datetime types
    match series.dtype() {
        DataType::Date => {
            let ca = series
                .date()
                .map_err(|e| QrsError::DataLoad(format!("Column {col} date cast: {e}")))?;
            Ok(ca.into_iter()
                .map(|opt| opt.and_then(|days| {
                    // Polars Date = days since epoch (1970-01-01)
                    NaiveDate::from_num_days_from_ce_opt(days + 719_163)
                }))
                .collect())
        }
        DataType::Datetime(_, _) => {
            let ca = series
                .datetime()
                .map_err(|e| QrsError::DataLoad(format!("Column {col} datetime cast: {e}")))?;
            Ok(ca.into_iter()
                .map(|opt| opt.and_then(|ms| {
                    // Polars Datetime[ms] = milliseconds since epoch
                    let secs = ms / 1000;
                    let nsecs = ((ms % 1000) * 1_000_000) as u32;
                    chrono::DateTime::from_timestamp(secs, nsecs)
                        .map(|dt| dt.date_naive())
                }))
                .collect())
        }
        DataType::String => {
            // Parse date strings (YYYY-MM-DD)
            let ca = series
                .str()
                .map_err(|e| QrsError::DataLoad(format!("Column {col} str cast: {e}")))?;
            Ok(ca.into_iter()
                .map(|opt| opt.and_then(|s| NaiveDate::parse_from_str(s, "%Y-%m-%d").ok()))
                .collect())
        }
        _ => Err(QrsError::DataLoad(format!(
            "Column {col} has unsupported type {:?}",
            series.dtype()
        ))),
    }
}

fn get_f64_col(df: &DataFrame, col: &str) -> Vec<Option<f64>> {
    match df.column(col) {
        Ok(series) => {
            if matches!(series.dtype(), DataType::Null) {
                return vec![None; df.height()];
            }
            match series.f64() {
                Ok(ca) => ca.into_iter().collect(),
                Err(_) => {
                    // Try casting to f64
                    match series.cast(&DataType::Float64) {
                        Ok(casted) => match casted.f64() {
                            Ok(ca) => ca.into_iter().collect(),
                            Err(_) => vec![None; df.height()],
                        },
                        Err(_) => vec![None; df.height()],
                    }
                }
            }
        }
        Err(_) => vec![None; df.height()],
    }
}

fn opt_f64(v: Option<f64>) -> f64 {
    v.unwrap_or(f64::NAN)
}

/// Create a Polars Date literal from NaiveDate.
/// Polars Date = i32 days since 1970-01-01.
/// (NaiveDate::lit() wrongly produces Datetime, not Date.)
fn date_lit(d: Date) -> polars::prelude::Expr {
    
    let epoch = NaiveDate::from_ymd_opt(1970, 1, 1).unwrap();
    let days = (d - epoch).num_days() as i32;
    polars::prelude::Expr::Literal(polars::prelude::LiteralValue::Date(days))
}

/// Create a Polars Datetime literal from NaiveDateTime (for Datetime[ms] columns).
fn datetime_lit(dt: chrono::NaiveDateTime) -> polars::prelude::Expr {
    let ms = dt.and_utc().timestamp_millis();
    polars::prelude::Expr::Literal(polars::prelude::LiteralValue::DateTime(
        ms,
        polars::prelude::TimeUnit::Milliseconds,
        None,
    ))
}

// ===== Loaders =====

fn load_daily_prices(
    cache_dir: &Path,
    interner: &SharedInterner,
    date_start: Option<Date>,
    date_end: Option<Date>,
) -> Result<PriceGrid> {
    let path = loader::find_any_cache_file(cache_dir, "us_daily_price")
        .ok_or_else(|| QrsError::ParquetNotFound("us_daily_price_*.parquet".into()))?;

    // Use Polars lazy scan with predicate pushdown for date filtering
    let df = if date_start.is_some() || date_end.is_some() {
        let mut lazy = polars::prelude::LazyFrame::scan_parquet(&path, Default::default())
            .map_err(|e| QrsError::DataLoad(format!("Scan parquet: {e}")))?;
        if let Some(s) = date_start {
            lazy = lazy.filter(polars::prelude::col("trade_date").gt_eq(date_lit(s)));
        }
        if let Some(e) = date_end {
            lazy = lazy.filter(polars::prelude::col("trade_date").lt_eq(date_lit(e)));
        }
        let result = lazy.collect()
            .map_err(|e| QrsError::DataLoad(format!("Collect filtered parquet: {e}")))?;
        info!("Loaded {} (filtered): {} rows x {} cols",
            path.file_name().unwrap_or_default().to_string_lossy(),
            result.height(), result.width());
        result
    } else {
        loader::load_parquet(&path)?
    };

    let tickers_col = get_str_col(&df, "ticker")?;
    let dates = get_date_col(&df, "trade_date")?;
    let open = get_f64_col(&df, "open");
    let high = get_f64_col(&df, "high");
    let low = get_f64_col(&df, "low");
    let close = get_f64_col(&df, "close");
    let adj_close = get_f64_col(&df, "adj_close");
    let volume = get_f64_col(&df, "volume");
    let change_pct = get_f64_col(&df, "change_percent");

    let n = df.height();

    // Phase A: Pre-intern all unique tickers
    {
        let mut seen = std::collections::HashSet::new();
        for i in 0..n {
            if let Some(t) = tickers_col.get(i) {
                if seen.insert(t.as_ptr()) {
                    interner.intern(t);
                }
            }
        }
    }

    // Phase B: Build TickerId array
    let ticker_ids: Vec<Option<TickerId>> = (0..n)
        .map(|i| tickers_col.get(i).and_then(|t| interner.0.lock().unwrap().get_id(t)))
        .collect();

    // Phase C: Collect unique dates for PriceGrid dimensions
    let max_ticker_id = ticker_ids.iter().filter_map(|t| t.map(|t| t.0)).max().unwrap_or(0) as usize + 1;
    let mut unique_dates: Vec<Date> = dates.iter().filter_map(|d| *d).collect();
    unique_dates.sort();
    unique_dates.dedup();

    // Phase D: Build PriceGrid (single contiguous allocation, then parallel fill)
    let mut grid = PriceGrid::new(max_ticker_id, &unique_dates);

    for i in 0..n {
        let tid = match ticker_ids[i] { Some(t) => t, None => continue };
        let date = match dates[i] { Some(d) => d, None => continue };
        let close_val = opt_f64(close[i]);
        let adj = adj_close[i].unwrap_or(close_val);
        grid.insert(tid, date, PriceBar {
            open: opt_f64(open[i]),
            high: opt_f64(high[i]),
            low: opt_f64(low[i]),
            close: close_val,
            adj_close: adj,
            volume: opt_f64(volume[i]),
            change_percent: opt_f64(change_pct[i]),
            cum_ret_5d: f64::NAN, cum_ret_20d: f64::NAN,
            dvol_20d: f64::NAN, vol_20d: f64::NAN,
            ma60_adj: f64::NAN, dollar_volume: f64::NAN,
        });
    }

    info!("Daily prices: {} entries in PriceGrid ({}x{}, {:.0}MB)",
        grid.len(), max_ticker_id, unique_dates.len(),
        grid.memory_bytes() as f64 / 1024.0 / 1024.0);
    Ok(grid)
}

fn load_index_prices(cache_dir: &Path) -> Result<FxHashMap<(String, Date), f64>> {
    let path = loader::find_any_cache_file(cache_dir, "us_index_daily_gspc")
    .ok_or_else(|| QrsError::ParquetNotFound("us_index_daily_gspc_*.parquet".into()))?;
    let df = loader::load_parquet(&path)?;

    let codes = get_str_col(&df, "index_code")?;
    let dates = get_date_col(&df, "trade_date")?;
    let close = get_f64_col(&df, "close");

    let mut map = FxHashMap::default();
    for i in 0..df.height() {
        let code = match codes.get(i) {
            Some(c) => c.to_string(),
            None => continue,
        };
        let date = match dates[i] {
            Some(d) => d,
            None => continue,
        };
        if let Some(c) = close[i] {
            map.insert((code, date), c);
        }
    }
    info!("Index prices: {} entries", map.len());
    Ok(map)
}

/// Load rolling stats from _rolling_indexed.parquet and merge into existing daily_prices.
/// Returns count of entries updated.
fn load_rolling_stats_into(
    cache_dir: &Path,
    interner: &SharedInterner,
    daily_prices: &mut PriceGrid,
    date_start: Option<Date>,
    date_end: Option<Date>,
) -> Result<usize> {
    let path = cache_dir.join("_rolling_indexed.parquet");
    if !path.exists() {
        warn!("_rolling_indexed.parquet not found, rolling stats will be empty");
        return Ok(0);
    }

    // Use lazy scan with date filtering (trade_date is Datetime[ms] here)
    let df = if date_start.is_some() || date_end.is_some() {
        let mut lazy = polars::prelude::LazyFrame::scan_parquet(&path, Default::default())
            .map_err(|e| QrsError::DataLoad(format!("Scan rolling parquet: {e}")))?;
        if let Some(s) = date_start {
            lazy = lazy.filter(polars::prelude::col("trade_date").gt_eq(
                datetime_lit(s.and_hms_opt(0, 0, 0).unwrap())
            ));
        }
        if let Some(e) = date_end {
            lazy = lazy.filter(polars::prelude::col("trade_date").lt_eq(
                datetime_lit(e.and_hms_opt(23, 59, 59).unwrap())
            ));
        }
        let result = lazy.collect()
            .map_err(|e| QrsError::DataLoad(format!("Collect filtered rolling: {e}")))?;
        info!("Loaded _rolling_indexed (filtered): {} rows", result.height());
        result
    } else {
        loader::load_parquet(&path)?
    };

    let tickers = get_str_col(&df, "ticker")?;
    let dates = get_date_col(&df, "trade_date")?;
    let rs_adj_close = get_f64_col(&df, "adj_close");
    let cum_ret_5d = get_f64_col(&df, "cum_ret_5d");
    let cum_ret_20d = get_f64_col(&df, "cum_ret_20d");
    let dvol_20d = get_f64_col(&df, "dvol_20d");
    let vol_20d = get_f64_col(&df, "vol_20d");
    let ma60_adj = get_f64_col(&df, "ma60_adj");
    let dollar_volume = get_f64_col(&df, "dollar_volume");

    let mut updated = 0usize;

    for i in 0..df.height() {
        let ticker_str = match tickers.get(i) {
            Some(t) => t,
            None => continue,
        };
        let date = match dates[i] {
            Some(d) => d,
            None => continue,
        };
        let tid = interner.intern(ticker_str);
        let _key = (tid, date);

        if let Some(bar) = daily_prices.get_mut(tid, date) {
            // Merge rolling stats into existing PriceBar
            bar.cum_ret_5d = opt_f64(cum_ret_5d[i]);
            bar.cum_ret_20d = opt_f64(cum_ret_20d[i]);
            bar.dvol_20d = opt_f64(dvol_20d[i]);
            bar.vol_20d = opt_f64(vol_20d[i]);
            bar.ma60_adj = opt_f64(ma60_adj[i]);
            bar.dollar_volume = opt_f64(dollar_volume[i]);
            // Also update adj_close from rolling stats (it's computed from Python, not NULL)
            let rs_adj = opt_f64(rs_adj_close[i]);
            if rs_adj.is_finite() && rs_adj > 0.0 {
                bar.adj_close = rs_adj;
            }
            updated += 1;
        }
        // If key not in daily_prices, skip (rolling_stats might have extra entries)
    }

    info!("Rolling stats merged: {} entries updated", updated);
    Ok(updated)
}

/// Compute month-end prices from daily prices (replacing pandas Period-based parquet).
fn compute_month_end_prices(
    daily: &PriceGrid,
) -> FxHashMap<(TickerId, YearMonth), f64> {
    use chrono::Datelike;

    let mut latest: FxHashMap<(TickerId, YearMonth), (Date, f64)> = FxHashMap::default();

    for (tid, date, bar) in daily.iter() {
        let ym = YearMonth::new(date.year(), date.month());
        let price = if bar.adj_close.is_finite() {
            bar.adj_close
        } else {
            bar.close
        };
        if !price.is_finite() {
            continue;
        }

        match latest.get(&(tid, ym)) {
            Some(&(existing_date, _)) if date > existing_date => {
                latest.insert((tid, ym), (date, price));
            }
            None => {
                latest.insert((tid, ym), (date, price));
            }
            _ => {}
        }
    }

    let result: FxHashMap<(TickerId, YearMonth), f64> =
        latest.into_iter().map(|(k, (_, p))| (k, p)).collect();
    info!("Month-end prices: {} entries", result.len());
    result
}

fn load_financials(
    cache_dir: &Path,
    interner: &SharedInterner,
) -> Result<(
    FxHashMap<TickerId, Vec<FinancialRecord>>,
    FxHashMap<TickerId, Vec<KeyMetricRecord>>,
)> {
    // Load alpha_financial (130 cols)
    let fin_path =
        loader::find_any_cache_file(cache_dir, "alpha_financial")
            .ok_or_else(|| QrsError::ParquetNotFound("alpha_financial_*.parquet".into()))?;
    let df = loader::load_parquet(&fin_path)?;

    let tickers = get_str_col(&df, "ticker")?;
    let dates = get_date_col(&df, "date")?;
    let filing_dates = get_date_col(&df, "filing_date")?;
    let periods = get_str_col(&df, "period")?;

    // Identify numeric columns (skip ticker, period, date, filing_date, and string cols)
    let skip_cols = [
        "ticker",
        "period",
        "date",
        "filing_date",
        "reported_currency",
        "cik",
        "accepted_date",
        "fiscal_year",
        "link",
        "final_link",
    ];
    let numeric_cols: Vec<String> = df
        .get_column_names()
        .iter()
        .filter(|c| !skip_cols.contains(&c.as_str()))
        .filter(|c| matches!(df.column(c.as_str()).map(|s| s.dtype().clone()), Ok(DataType::Float64 | DataType::Int64)))
        .map(|c| c.to_string())
        .collect();

    // Pre-extract all numeric columns
    let num_data: Vec<(&str, Vec<Option<f64>>)> = numeric_cols
        .iter()
        .map(|c| (c.as_str(), get_f64_col(&df, c)))
        .collect();

    let mut financials: FxHashMap<TickerId, Vec<FinancialRecord>> = FxHashMap::default();

    for i in 0..df.height() {
        let ticker_str = match tickers.get(i) {
            Some(t) => t,
            None => continue,
        };
        let date = match dates[i] {
            Some(d) => d,
            None => continue,
        };
        let filing_date = match filing_dates[i] {
            Some(d) => d,
            None => date, // fallback
        };
        let period = periods.get(i).unwrap_or("").to_string();
        let tid = interner.intern(ticker_str);

        let mut fields = FxHashMap::default();
        for (col_name, col_data) in &num_data {
            if let Some(v) = col_data[i] {
                if v.is_finite() {
                    fields.insert(col_name.to_string(), v);
                }
            }
        }

        financials.entry(tid).or_default().push(FinancialRecord {
            ticker_id: tid,
            date,
            filing_date,
            period,
            fields,
        });
    }

    // Sort each ticker's records by date desc (most recent first)
    for records in financials.values_mut() {
        records.sort_by(|a, b| b.date.cmp(&a.date));
    }

    info!(
        "Financials: {} tickers, {} total records",
        financials.len(),
        financials.values().map(|v| v.len()).sum::<usize>()
    );

    // Load key metrics
    let km_path =
        loader::find_any_cache_file(cache_dir, "alpha_key_metric")
            .ok_or_else(|| QrsError::ParquetNotFound("alpha_key_metric_*.parquet".into()))?;
    let km_df = loader::load_parquet(&km_path)?;

    let km_tickers = get_str_col(&km_df, "ticker")?;
    let km_dates = get_date_col(&km_df, "date")?;
    let km_periods = get_str_col(&km_df, "period")?;

    let km_skip = ["ticker", "period", "date", "fiscal_year", "calendar_year"];
    let km_numeric_cols: Vec<String> = km_df
        .get_column_names()
        .iter()
        .filter(|c| !km_skip.contains(&c.as_str()))
        .filter(|c| matches!(km_df.column(c.as_str()).map(|s| s.dtype().clone()), Ok(DataType::Float64 | DataType::Int64)))
        .map(|c| c.to_string())
        .collect();

    let km_num_data: Vec<(&str, Vec<Option<f64>>)> = km_numeric_cols
        .iter()
        .map(|c| (c.as_str(), get_f64_col(&km_df, c)))
        .collect();

    let mut key_metrics: FxHashMap<TickerId, Vec<KeyMetricRecord>> = FxHashMap::default();

    for i in 0..km_df.height() {
        let ticker_str = match km_tickers.get(i) {
            Some(t) => t,
            None => continue,
        };
        let date = match km_dates[i] {
            Some(d) => d,
            None => continue,
        };
        let period = km_periods.get(i).unwrap_or("").to_string();
        let tid = interner.intern(ticker_str);

        let mut fields = FxHashMap::default();
        for (col_name, col_data) in &km_num_data {
            if let Some(v) = col_data[i] {
                if v.is_finite() {
                    fields.insert(col_name.to_string(), v);
                }
            }
        }

        key_metrics.entry(tid).or_default().push(KeyMetricRecord {
            ticker_id: tid,
            date,
            period,
            fields,
        });
    }

    for records in key_metrics.values_mut() {
        records.sort_by(|a, b| b.date.cmp(&a.date));
    }

    info!(
        "Key metrics: {} tickers, {} total records",
        key_metrics.len(),
        key_metrics.values().map(|v| v.len()).sum::<usize>()
    );

    Ok((financials, key_metrics))
}

fn load_enterprise_values(
    cache_dir: &Path,
    interner: &SharedInterner,
) -> Result<FxHashMap<TickerId, Vec<EvRecord>>> {
    let path = loader::find_any_cache_file(cache_dir, "alpha_enterprise_value")
    .ok_or_else(|| QrsError::ParquetNotFound("alpha_enterprise_value_*.parquet".into()))?;
    let df = loader::load_parquet(&path)?;

    let tickers = get_str_col(&df, "ticker")?;
    let dates = get_date_col(&df, "date")?;
    let mktcap = get_f64_col(&df, "market_capitalization");
    let ev = get_f64_col(&df, "enterprise_value");

    let mut map: FxHashMap<TickerId, Vec<EvRecord>> = FxHashMap::default();

    for i in 0..df.height() {
        let ticker_str = match tickers.get(i) {
            Some(t) => t,
            None => continue,
        };
        let date = match dates[i] {
            Some(d) => d,
            None => continue,
        };
        let tid = interner.intern(ticker_str);

        map.entry(tid).or_default().push(EvRecord {
            date,
            market_cap: opt_f64(mktcap[i]),
            enterprise_value: opt_f64(ev[i]),
        });
    }

    // Sort by date desc
    for records in map.values_mut() {
        records.sort_by(|a, b| b.date.cmp(&a.date));
    }

    info!("Enterprise values: {} tickers", map.len());
    Ok(map)
}

// Simpler loaders for smaller tables

fn load_analyst_recs(
    cache_dir: &Path,
    interner: &SharedInterner,
) -> Result<FxHashMap<TickerId, Vec<AnalystRec>>> {
    let path = loader::find_any_cache_file(cache_dir, "us_analyst_recommendation");
    let Some(path) = path else {
        warn!("us_analyst_recommendation not found");
        return Ok(FxHashMap::default());
    };
    let df = loader::load_parquet(&path)?;

    let tickers = get_str_col(&df, "ticker")?;
    let dates = get_date_col(&df, "date")?;
    let grading = get_str_values(&df, "grading_company");
    let grade = get_str_values(&df, "new_grade");
    let action = get_str_values(&df, "action");

    let mut map: FxHashMap<TickerId, Vec<AnalystRec>> = FxHashMap::default();
    for i in 0..df.height() {
        let tid = interner.intern(tickers.get(i).unwrap_or(""));
        let date = match dates[i] {
            Some(d) => d,
            None => continue,
        };
        map.entry(tid).or_default().push(AnalystRec {
            date,
            grading_company: grading[i].clone(),
            new_grade: grade[i].clone(),
            action: action[i].clone(),
        });
    }
    for records in map.values_mut() {
        records.sort_by(|a, b| b.date.cmp(&a.date));
    }
    info!("Analyst recs: {} tickers", map.len());
    Ok(map)
}

fn load_earnings_surprises(
    cache_dir: &Path,
    interner: &SharedInterner,
) -> Result<FxHashMap<TickerId, Vec<EarningsSurprise>>> {
    let path = loader::find_any_cache_file(cache_dir, "us_earnings_surprise");
    let Some(path) = path else {
        return Ok(FxHashMap::default());
    };
    let df = loader::load_parquet(&path)?;

    let tickers = get_str_col(&df, "ticker")?;
    let dates = get_date_col(&df, "date")?;
    let actual = get_f64_col(&df, "eps_actual");
    let estimated = get_f64_col(&df, "eps_estimated");
    let surprise = get_f64_col(&df, "surprise");
    let surprise_pct = get_f64_col(&df, "surprise_pct");

    let mut map: FxHashMap<TickerId, Vec<EarningsSurprise>> = FxHashMap::default();
    for i in 0..df.height() {
        let tid = interner.intern(tickers.get(i).unwrap_or(""));
        let date = match dates[i] {
            Some(d) => d,
            None => continue,
        };
        map.entry(tid).or_default().push(EarningsSurprise {
            date,
            eps_actual: opt_f64(actual[i]),
            eps_estimated: opt_f64(estimated[i]),
            surprise: opt_f64(surprise[i]),
            surprise_pct: opt_f64(surprise_pct[i]),
        });
    }
    for records in map.values_mut() {
        records.sort_by(|a, b| b.date.cmp(&a.date));
    }
    Ok(map)
}

fn load_eps_estimates(
    cache_dir: &Path,
    interner: &SharedInterner,
) -> Result<FxHashMap<TickerId, Vec<EpsEstimate>>> {
    let path =
        loader::find_any_cache_file(cache_dir, "us_eps_estimate");
    let Some(path) = path else {
        return Ok(FxHashMap::default());
    };
    let df = loader::load_parquet(&path)?;

    let tickers = get_str_col(&df, "ticker")?;
    let dates = get_date_col(&df, "date")?;
    let avg = get_f64_col(&df, "estimated_eps_avg");
    let low = get_f64_col(&df, "estimated_eps_low");
    let high = get_f64_col(&df, "estimated_eps_high");
    let n_analysts = get_f64_col(&df, "number_analysts_estimated_eps");

    let mut map: FxHashMap<TickerId, Vec<EpsEstimate>> = FxHashMap::default();
    for i in 0..df.height() {
        let tid = interner.intern(tickers.get(i).unwrap_or(""));
        let date = match dates[i] {
            Some(d) => d,
            None => continue,
        };
        map.entry(tid).or_default().push(EpsEstimate {
            date,
            estimated_eps_avg: opt_f64(avg[i]),
            estimated_eps_low: opt_f64(low[i]),
            estimated_eps_high: opt_f64(high[i]),
            num_analysts: opt_f64(n_analysts[i]),
        });
    }
    for records in map.values_mut() {
        records.sort_by(|a, b| b.date.cmp(&a.date));
    }
    Ok(map)
}

fn load_dividends(
    cache_dir: &Path,
    interner: &SharedInterner,
) -> Result<FxHashMap<TickerId, Vec<DividendRecord>>> {
    let path = loader::find_any_cache_file(cache_dir, "us_corporate_action_div");
    let Some(path) = path else {
        return Ok(FxHashMap::default());
    };
    let df = loader::load_parquet(&path)?;

    let tickers = get_str_col(&df, "ticker")?;
    let dates = get_date_col(&df, "date")?;
    let dividends = get_f64_col(&df, "dividend");

    let mut map: FxHashMap<TickerId, Vec<DividendRecord>> = FxHashMap::default();
    for i in 0..df.height() {
        let tid = interner.intern(tickers.get(i).unwrap_or(""));
        let date = match dates[i] {
            Some(d) => d,
            None => continue,
        };
        if let Some(div) = dividends[i] {
            if div.is_finite() && div > 0.0 {
                map.entry(tid).or_default().push(DividendRecord {
                    date,
                    dividend: div,
                });
            }
        }
    }
    for records in map.values_mut() {
        records.sort_by(|a, b| b.date.cmp(&a.date));
    }
    Ok(map)
}

fn load_insider_trades(
    cache_dir: &Path,
    interner: &SharedInterner,
) -> Result<FxHashMap<TickerId, Vec<InsiderTrade>>> {
    let path =
        loader::find_any_cache_file(cache_dir, "us_insider_trade");
    let Some(path) = path else {
        return Ok(FxHashMap::default());
    };
    let df = loader::load_parquet(&path)?;

    let tickers = get_str_col(&df, "ticker")?;
    let dates = get_date_col(&df, "filing_date")?;
    let acq_disp = get_str_values(&df, "acquisition_or_disposition");
    let securities = get_f64_col(&df, "securities_transacted");
    let price = get_f64_col(&df, "price");

    let mut map: FxHashMap<TickerId, Vec<InsiderTrade>> = FxHashMap::default();
    for i in 0..df.height() {
        let tid = interner.intern(tickers.get(i).unwrap_or(""));
        let date = match dates[i] {
            Some(d) => d,
            None => continue,
        };
        let is_acq = acq_disp[i] == "A";
        map.entry(tid).or_default().push(InsiderTrade {
            filing_date: date,
            is_acquisition: is_acq,
            securities_transacted: opt_f64(securities[i]),
            price: opt_f64(price[i]),
        });
    }
    for records in map.values_mut() {
        records.sort_by(|a, b| b.filing_date.cmp(&a.filing_date));
    }
    Ok(map)
}

// ===== Alternative data loaders =====

fn load_shares_float(
    cache_dir: &Path,
    interner: &SharedInterner,
) -> Result<FxHashMap<TickerId, SharesFloat>> {
    let path = match loader::find_snapshot_cache(cache_dir, "us_shares_float") {
        Some(p) => p,
        None => { warn!("us_shares_float_all.parquet not found"); return Ok(FxHashMap::default()); }
    };
    let df = loader::load_parquet(&path)?;
    let tickers = get_str_col(&df, "ticker")?;
    let ff = get_f64_col(&df, "free_float");
    let fs = get_f64_col(&df, "float_shares");
    let os = get_f64_col(&df, "outstanding_shares");
    let mut map = FxHashMap::default();
    for i in 0..df.height() {
        let tid = interner.intern(tickers.get(i).unwrap_or(""));
        map.insert(tid, SharesFloat {
            free_float: opt_f64(ff[i]),
            float_shares: opt_f64(fs[i]),
            outstanding_shares: opt_f64(os[i]),
        });
    }
    info!("Shares float: {} tickers", map.len());
    Ok(map)
}

fn load_dark_pool(
    cache_dir: &Path,
    interner: &SharedInterner,
) -> Result<FxHashMap<TickerId, Vec<DarkPoolRecord>>> {
    let path = match loader::find_any_cache_file(cache_dir, "us_dark_pool_volume") {
        Some(p) => p,
        None => { warn!("us_dark_pool_volume not found"); return Ok(FxHashMap::default()); }
    };
    let df = loader::load_parquet(&path)?;
    let tickers = get_str_col(&df, "ticker")?;
    let dates = get_date_col(&df, "date")?;
    let dpi = get_f64_col(&df, "dpi");
    let mut map: FxHashMap<TickerId, Vec<DarkPoolRecord>> = FxHashMap::default();
    for i in 0..df.height() {
        let tid = interner.intern(tickers.get(i).unwrap_or(""));
        let date = match dates[i] { Some(d) => d, None => continue };
        if let Some(d) = dpi[i] {
            if d.is_finite() {
                map.entry(tid).or_default().push(DarkPoolRecord { date, dpi: d });
            }
        }
    }
    for v in map.values_mut() { v.sort_by(|a, b| b.date.cmp(&a.date)); }
    info!("Dark pool: {} tickers", map.len());
    Ok(map)
}

fn load_institutional(
    cache_dir: &Path,
    interner: &SharedInterner,
) -> Result<FxHashMap<TickerId, Vec<InstitutionalRecord>>> {
    let path = match loader::find_any_cache_file(cache_dir, "us_institutional_holder") {
        Some(p) => p,
        None => { return Ok(FxHashMap::default()); }
    };
    let df = loader::load_parquet(&path)?;
    let tickers = get_str_col(&df, "ticker")?;
    let dates = get_date_col(&df, "date")?;
    let shares = get_f64_col(&df, "number_of_13f_shares");
    let mut map: FxHashMap<TickerId, Vec<InstitutionalRecord>> = FxHashMap::default();
    for i in 0..df.height() {
        let tid = interner.intern(tickers.get(i).unwrap_or(""));
        let date = match dates[i] { Some(d) => d, None => continue };
        map.entry(tid).or_default().push(InstitutionalRecord {
            date,
            number_of_13f_shares: opt_f64(shares[i]),
        });
    }
    for v in map.values_mut() { v.sort_by(|a, b| b.date.cmp(&a.date)); }
    info!("Institutional: {} tickers", map.len());
    Ok(map)
}

fn load_esg_ratings(
    cache_dir: &Path,
    interner: &SharedInterner,
) -> Result<FxHashMap<TickerId, f64>> {
    let path = match loader::find_snapshot_cache(cache_dir, "us_esg_rating") {
        Some(p) => p,
        None => { return Ok(FxHashMap::default()); }
    };
    let df = loader::load_parquet(&path)?;
    let tickers = get_str_col(&df, "ticker")?;
    let ratings = get_str_values(&df, "esg_risk_rating");
    // Map letter ratings to numeric: A+=10, A=9, ... D=1
    fn rating_to_num(s: &str) -> Option<f64> {
        match s.trim() {
            "AAA" => Some(10.0), "AA" => Some(9.0), "A" => Some(8.0),
            "BBB" => Some(7.0), "BB" => Some(6.0), "B" => Some(5.0),
            "CCC" => Some(4.0), "CC" => Some(3.0), "C" => Some(2.0),
            "D" => Some(1.0),
            _ => s.parse::<f64>().ok(), // Some sources store numeric directly
        }
    }
    // Keep latest per ticker (last row wins since parquet may have multiple fiscal years)
    let mut map = FxHashMap::default();
    for i in 0..df.height() {
        let tid = interner.intern(tickers.get(i).unwrap_or(""));
        if let Some(v) = rating_to_num(&ratings[i]) {
            map.insert(tid, v);
        }
    }
    info!("ESG ratings: {} tickers", map.len());
    Ok(map)
}

fn load_employee_counts(
    cache_dir: &Path,
    interner: &SharedInterner,
) -> Result<FxHashMap<TickerId, Vec<EmployeeRecord>>> {
    let path = match loader::find_any_cache_file(cache_dir, "us_employee_count") {
        Some(p) => p,
        None => { return Ok(FxHashMap::default()); }
    };
    let df = loader::load_parquet(&path)?;
    let tickers = get_str_col(&df, "ticker")?;
    let dates = get_date_col(&df, "filing_date")?;
    let counts = get_f64_col(&df, "employee_count");
    let mut map: FxHashMap<TickerId, Vec<EmployeeRecord>> = FxHashMap::default();
    for i in 0..df.height() {
        let tid = interner.intern(tickers.get(i).unwrap_or(""));
        let date = match dates[i] { Some(d) => d, None => continue };
        if let Some(c) = counts[i] {
            if c.is_finite() && c > 0.0 {
                map.entry(tid).or_default().push(EmployeeRecord { filing_date: date, employee_count: c });
            }
        }
    }
    for v in map.values_mut() { v.sort_by(|a, b| b.filing_date.cmp(&a.filing_date)); }
    info!("Employee counts: {} tickers", map.len());
    Ok(map)
}

fn load_congress_trades(
    cache_dir: &Path,
    interner: &SharedInterner,
) -> Result<FxHashMap<TickerId, Vec<CongressRecord>>> {
    let path = match loader::find_any_cache_file(cache_dir, "us_congress_trade") {
        Some(p) => p,
        None => { return Ok(FxHashMap::default()); }
    };
    let df = loader::load_parquet(&path)?;
    let tickers = get_str_col(&df, "ticker")?;
    let dates = get_date_col(&df, "transaction_date")?;
    let types = get_str_values(&df, "type");
    let mut map: FxHashMap<TickerId, Vec<CongressRecord>> = FxHashMap::default();
    for i in 0..df.height() {
        let tid = interner.intern(tickers.get(i).unwrap_or(""));
        let date = match dates[i] { Some(d) => d, None => continue };
        let is_purchase = types[i].to_lowercase().contains("purchase");
        map.entry(tid).or_default().push(CongressRecord { date, is_purchase });
    }
    for v in map.values_mut() { v.sort_by(|a, b| b.date.cmp(&a.date)); }
    info!("Congress trades: {} tickers", map.len());
    Ok(map)
}

fn load_gov_contracts(
    cache_dir: &Path,
    interner: &SharedInterner,
) -> Result<FxHashMap<TickerId, Vec<GovContractRecord>>> {
    let path = match loader::find_snapshot_cache(cache_dir, "us_gov_contract") {
        Some(p) => p,
        None => { return Ok(FxHashMap::default()); }
    };
    let df = loader::load_parquet(&path)?;
    let tickers = get_str_col(&df, "ticker")?;
    let years = get_f64_col(&df, "year");
    let quarters = get_f64_col(&df, "quarter");
    let amounts = get_f64_col(&df, "amount");
    let mut map: FxHashMap<TickerId, Vec<GovContractRecord>> = FxHashMap::default();
    for i in 0..df.height() {
        let tid = interner.intern(tickers.get(i).unwrap_or(""));
        if let (Some(y), Some(q), Some(a)) = (years[i], quarters[i], amounts[i]) {
            if a.is_finite() {
                map.entry(tid).or_default().push(GovContractRecord {
                    year: y as i32, quarter: q as i32, amount: a,
                });
            }
        }
    }
    for v in map.values_mut() { v.sort_by(|a, b| (b.year, b.quarter).cmp(&(a.year, a.quarter))); }
    info!("Gov contracts: {} tickers", map.len());
    Ok(map)
}

fn load_lobbying(
    cache_dir: &Path,
    interner: &SharedInterner,
) -> Result<FxHashMap<TickerId, Vec<LobbyingRecord>>> {
    let path = match loader::find_any_cache_file(cache_dir, "us_lobbying") {
        Some(p) => p,
        None => { return Ok(FxHashMap::default()); }
    };
    let df = loader::load_parquet(&path)?;
    let tickers = get_str_col(&df, "ticker")?;
    let dates = get_date_col(&df, "date")?;
    let amounts = get_f64_col(&df, "amount");
    let mut map: FxHashMap<TickerId, Vec<LobbyingRecord>> = FxHashMap::default();
    for i in 0..df.height() {
        let tid = interner.intern(tickers.get(i).unwrap_or(""));
        let date = match dates[i] { Some(d) => d, None => continue };
        if let Some(a) = amounts[i] {
            if a.is_finite() && a > 0.0 {
                map.entry(tid).or_default().push(LobbyingRecord { date, amount: a });
            }
        }
    }
    for v in map.values_mut() { v.sort_by(|a, b| b.date.cmp(&a.date)); }
    info!("Lobbying: {} tickers", map.len());
    Ok(map)
}

fn load_revenue_segments(
    cache_dir: &Path,
    interner: &SharedInterner,
) -> Result<FxHashMap<TickerId, Vec<RevenueSegmentRecord>>> {
    let path = match loader::find_any_cache_file(cache_dir, "us_revenue_segment") {
        Some(p) => p,
        None => { return Ok(FxHashMap::default()); }
    };
    let df = loader::load_parquet(&path)?;
    let tickers = get_str_col(&df, "ticker")?;
    let dates = get_date_col(&df, "date")?;
    let names = get_str_values(&df, "segment_name");
    let revenues = get_f64_col(&df, "revenue");
    let types = get_str_values(&df, "segment_type");
    let mut map: FxHashMap<TickerId, Vec<RevenueSegmentRecord>> = FxHashMap::default();
    for i in 0..df.height() {
        let tid = interner.intern(tickers.get(i).unwrap_or(""));
        let date = match dates[i] { Some(d) => d, None => continue };
        if let Some(r) = revenues[i] {
            if r.is_finite() {
                map.entry(tid).or_default().push(RevenueSegmentRecord {
                    date,
                    segment_name: names[i].clone(),
                    revenue: r,
                    segment_type: types[i].clone(),
                });
            }
        }
    }
    for v in map.values_mut() { v.sort_by(|a, b| b.date.cmp(&a.date)); }
    info!("Revenue segments: {} tickers", map.len());
    Ok(map)
}

fn load_industry_class(
    cache_dir: &Path,
    interner: &SharedInterner,
    sector_interner: &mut SectorInterner,
) -> Result<(FxHashMap<TickerId, quant_core::types::SectorId>, FxHashMap<TickerId, quant_core::types::SectorId>)> {
    let path = match loader::find_snapshot_cache(cache_dir, "us_industry_class") {
        Some(p) => p,
        None => {
            warn!("us_industry_class_all.parquet not found, sector_map will be empty");
            return Ok((FxHashMap::default(), FxHashMap::default()));
        }
    };
    let df = loader::load_parquet(&path)?;
    let tickers = get_str_col(&df, "ticker")?;
    let sectors = get_str_values(&df, "sector");
    let industries = get_str_values(&df, "industry");

    let mut sector_map = FxHashMap::default();
    let mut industry_map = FxHashMap::default();

    for i in 0..df.height() {
        let tid = interner.intern(tickers.get(i).unwrap_or(""));
        if !sectors[i].is_empty() {
            sector_map.insert(tid, sector_interner.intern(&sectors[i]));
        }
        if !industries[i].is_empty() {
            industry_map.insert(tid, sector_interner.intern(&industries[i]));
        }
    }

    info!("Industry class: {} sector mappings, {} industry mappings, {} unique sectors",
        sector_map.len(), industry_map.len(), sector_interner.len());
    Ok((sector_map, industry_map))
}
