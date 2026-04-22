//! Build DataCache from parquet files.
//! Converts Polars DataFrames into the HashMap-based DataCache.

use std::path::Path;

use chrono::NaiveDate;
use polars::prelude::*;
use rustc_hash::FxHashMap;
use tracing::{info, warn};

use qrs_core::error::{QrsError, Result};
use qrs_core::types::{Date, SectorInterner, TickerId, TickerInterner, YearMonth};

use crate::cache::*;
use crate::loader;

/// Build a complete DataCache from the parquet cache directory.
pub fn build_cache(cache_dir: &Path) -> Result<DataCache> {
    info!("Building DataCache from {}", cache_dir.display());
    let t0 = std::time::Instant::now();

    let mut interner = TickerInterner::new();
    let mut sector_interner = SectorInterner::new();

    // Load core tables
    let daily_prices = load_daily_prices(cache_dir, &mut interner)?;
    let index_prices = load_index_prices(cache_dir)?;
    let rolling_stats = load_rolling_stats(cache_dir, &mut interner)?;
    let month_end_prices = compute_month_end_prices(&daily_prices);
    let (financials, key_metrics) = load_financials(cache_dir, &mut interner)?;
    let enterprise_values = load_enterprise_values(cache_dir, &mut interner)?;
    let analyst_recs = load_analyst_recs(cache_dir, &mut interner)?;
    let earnings_surprises = load_earnings_surprises(cache_dir, &mut interner)?;
    let eps_estimates = load_eps_estimates(cache_dir, &mut interner)?;
    let dividends = load_dividends(cache_dir, &mut interner)?;
    let insider_trades = load_insider_trades(cache_dir, &mut interner)?;

    // Build trading calendar from index prices
    let mut trading_days: Vec<Date> = index_prices
        .keys()
        .filter(|(idx, _)| idx == "^GSPC")
        .map(|(_, d)| *d)
        .collect();
    trading_days.sort();

    info!(
        "DataCache built in {:.1}s: {} tickers, {} trading days, {} daily prices, {} rolling stats",
        t0.elapsed().as_secs_f64(),
        interner.len(),
        trading_days.len(),
        daily_prices.len(),
        rolling_stats.len(),
    );

    Ok(DataCache {
        daily_prices,
        index_prices,
        rolling_stats,
        month_end_prices,
        financials,
        key_metrics,
        enterprise_values,
        analyst_recs,
        earnings_surprises,
        eps_estimates,
        dividends,
        insider_trades,
        sector_map: FxHashMap::default(),  // TODO: load from industry class
        industry_map: FxHashMap::default(),
        ipo_dates: FxHashMap::default(),   // TODO: load from stock basic
        is_active: FxHashMap::default(),   // TODO: load from stock basic
        trading_days,
        ticker_interner: interner,
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

// ===== Loaders =====

fn load_daily_prices(
    cache_dir: &Path,
    interner: &mut TickerInterner,
) -> Result<FxHashMap<(TickerId, Date), PriceBar>> {
    let path = loader::find_any_cache_file(cache_dir, "us_daily_price")
        .ok_or_else(|| QrsError::ParquetNotFound("us_daily_price_*.parquet".into()))?;
    let df = loader::load_parquet(&path)?;

    let tickers = get_str_col(&df, "ticker")?;
    let dates = get_date_col(&df, "trade_date")?;
    let open = get_f64_col(&df, "open");
    let high = get_f64_col(&df, "high");
    let low = get_f64_col(&df, "low");
    let close = get_f64_col(&df, "close");
    let adj_close = get_f64_col(&df, "adj_close");
    let volume = get_f64_col(&df, "volume");
    let change_pct = get_f64_col(&df, "change_percent");

    let mut map = FxHashMap::default();
    map.reserve(df.height());

    for i in 0..df.height() {
        let ticker_str = match tickers.get(i) {
            Some(t) => t,
            None => continue,
        };
        let date = match dates[i] {
            Some(d) => d,
            None => continue,
        };
        let close_val = opt_f64(close[i]);
        // adj_close is all NULL in this parquet, fall back to close
        let adj = adj_close[i].unwrap_or(close_val);

        let tid = interner.intern(ticker_str);
        map.insert(
            (tid, date),
            PriceBar {
                open: opt_f64(open[i]),
                high: opt_f64(high[i]),
                low: opt_f64(low[i]),
                close: close_val,
                adj_close: adj,
                volume: opt_f64(volume[i]),
                change_percent: opt_f64(change_pct[i]),
            },
        );
    }

    info!("Daily prices: {} entries", map.len());
    Ok(map)
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

fn load_rolling_stats(
    cache_dir: &Path,
    interner: &mut TickerInterner,
) -> Result<FxHashMap<(TickerId, Date), RollingStats>> {
    let path = cache_dir.join("_rolling_indexed.parquet");
    if !path.exists() {
        warn!("_rolling_indexed.parquet not found, rolling stats will be empty");
        return Ok(FxHashMap::default());
    }
    let df = loader::load_parquet(&path)?;

    let tickers = get_str_col(&df, "ticker")?;
    let dates = get_date_col(&df, "trade_date")?; // Datetime[ms] handled by get_date_col
    let adj_close = get_f64_col(&df, "adj_close");
    let cum_ret_5d = get_f64_col(&df, "cum_ret_5d");
    let cum_ret_20d = get_f64_col(&df, "cum_ret_20d");
    let dvol_20d = get_f64_col(&df, "dvol_20d");
    let vol_20d = get_f64_col(&df, "vol_20d");
    let ma60_adj = get_f64_col(&df, "ma60_adj");
    let volume = get_f64_col(&df, "volume");
    let dollar_volume = get_f64_col(&df, "dollar_volume");

    let mut map = FxHashMap::default();
    map.reserve(df.height());

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
        map.insert(
            (tid, date),
            RollingStats {
                adj_close: opt_f64(adj_close[i]),
                cum_ret_5d: opt_f64(cum_ret_5d[i]),
                cum_ret_20d: opt_f64(cum_ret_20d[i]),
                dvol_20d: opt_f64(dvol_20d[i]),
                vol_20d: opt_f64(vol_20d[i]),
                ma60_adj: opt_f64(ma60_adj[i]),
                volume: opt_f64(volume[i]),
                dollar_volume: opt_f64(dollar_volume[i]),
            },
        );
    }

    info!("Rolling stats: {} entries", map.len());
    Ok(map)
}

/// Compute month-end prices from daily prices (replacing pandas Period-based parquet).
fn compute_month_end_prices(
    daily: &FxHashMap<(TickerId, Date), PriceBar>,
) -> FxHashMap<(TickerId, YearMonth), f64> {
    use chrono::Datelike;

    // Group by (ticker, year-month), keep the latest date's adj_close
    let mut latest: FxHashMap<(TickerId, YearMonth), (Date, f64)> = FxHashMap::default();

    for (&(tid, date), bar) in daily {
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
    interner: &mut TickerInterner,
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
    interner: &mut TickerInterner,
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
    interner: &mut TickerInterner,
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
    interner: &mut TickerInterner,
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
    interner: &mut TickerInterner,
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
    interner: &mut TickerInterner,
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
    interner: &mut TickerInterner,
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
