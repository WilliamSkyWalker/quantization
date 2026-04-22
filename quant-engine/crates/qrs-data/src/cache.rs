//! Central data cache loaded from parquet files.
//! Immutable after construction. Shared across threads via Arc<DataCache>.

use chrono::{Datelike, NaiveDate};
use rustc_hash::FxHashMap;

use qrs_core::types::{Date, SectorId, SectorInterner, TickerId, TickerInterner, YearMonth};

/// Daily OHLCV bar.
#[derive(Debug, Clone)]
pub struct PriceBar {
    pub open: f64,
    pub high: f64,
    pub low: f64,
    pub close: f64,
    pub adj_close: f64,
    pub volume: f64,
    pub change_percent: f64,
}

/// Precomputed rolling statistics for a (ticker, date).
#[derive(Debug, Clone)]
pub struct RollingStats {
    pub adj_close: f64,
    pub cum_ret_5d: f64,
    pub cum_ret_20d: f64,
    pub dvol_20d: f64,
    pub vol_20d: f64,
    pub ma60_adj: f64,
    pub volume: f64,
    pub dollar_volume: f64,
}

/// Financial statement record (one quarter).
#[derive(Debug, Clone)]
pub struct FinancialRecord {
    pub ticker_id: TickerId,
    pub date: Date,
    pub filing_date: Date,
    pub period: String,
    /// All numeric fields stored by name for flexibility.
    pub fields: FxHashMap<String, f64>,
}

/// Key metric record.
#[derive(Debug, Clone)]
pub struct KeyMetricRecord {
    pub ticker_id: TickerId,
    pub date: Date,
    pub period: String,
    pub fields: FxHashMap<String, f64>,
}

/// Analyst recommendation record.
#[derive(Debug, Clone)]
pub struct AnalystRec {
    pub date: Date,
    pub grading_company: String,
    pub new_grade: String,
    pub action: String,
}

/// Earnings surprise record.
#[derive(Debug, Clone)]
pub struct EarningsSurprise {
    pub date: Date,
    pub eps_actual: f64,
    pub eps_estimated: f64,
    pub surprise: f64,
    pub surprise_pct: f64,
}

/// EPS estimate record.
#[derive(Debug, Clone)]
pub struct EpsEstimate {
    pub date: Date,
    pub estimated_eps_avg: f64,
    pub estimated_eps_low: f64,
    pub estimated_eps_high: f64,
    pub num_analysts: f64,
}

/// Dividend record.
#[derive(Debug, Clone)]
pub struct DividendRecord {
    pub date: Date,
    pub dividend: f64,
}

/// Insider trade record.
#[derive(Debug, Clone)]
pub struct InsiderTrade {
    pub filing_date: Date,
    pub is_acquisition: bool,
    pub securities_transacted: f64,
    pub price: f64,
}

/// Enterprise value / market cap record.
#[derive(Debug, Clone)]
pub struct EvRecord {
    pub date: Date,
    pub market_cap: f64,
    pub enterprise_value: f64,
}

/// Central data cache — immutable after construction.
pub struct DataCache {
    // Price data
    pub daily_prices: FxHashMap<(TickerId, Date), PriceBar>,
    pub index_prices: FxHashMap<(String, Date), f64>,

    // Precomputed rolling statistics
    pub rolling_stats: FxHashMap<(TickerId, Date), RollingStats>,
    pub month_end_prices: FxHashMap<(TickerId, YearMonth), f64>,

    // Financial data (sorted by date desc per ticker)
    pub financials: FxHashMap<TickerId, Vec<FinancialRecord>>,
    pub key_metrics: FxHashMap<TickerId, Vec<KeyMetricRecord>>,
    pub enterprise_values: FxHashMap<TickerId, Vec<EvRecord>>,

    // Analyst / earnings / insider
    pub analyst_recs: FxHashMap<TickerId, Vec<AnalystRec>>,
    pub earnings_surprises: FxHashMap<TickerId, Vec<EarningsSurprise>>,
    pub eps_estimates: FxHashMap<TickerId, Vec<EpsEstimate>>,
    pub dividends: FxHashMap<TickerId, Vec<DividendRecord>>,
    pub insider_trades: FxHashMap<TickerId, Vec<InsiderTrade>>,

    // Static metadata
    pub sector_map: FxHashMap<TickerId, SectorId>,
    pub industry_map: FxHashMap<TickerId, SectorId>,
    pub ipo_dates: FxHashMap<TickerId, Date>,
    pub is_active: FxHashMap<TickerId, bool>,

    // Trading calendar (sorted)
    pub trading_days: Vec<Date>,

    // Interners
    pub ticker_interner: TickerInterner,
    pub sector_interner: SectorInterner,
}

impl DataCache {
    /// Get adjusted close price for a ticker on a date.
    pub fn get_close(&self, ticker: TickerId, date: Date) -> Option<f64> {
        self.daily_prices
            .get(&(ticker, date))
            .map(|bar| bar.adj_close)
    }

    /// Get the most recent financial record for a ticker on or before date.
    pub fn get_latest_financial(&self, ticker: TickerId, date: Date) -> Option<&FinancialRecord> {
        self.financials.get(&ticker).and_then(|records| {
            records.iter().find(|r| r.filing_date <= date)
        })
    }

    /// Get financial history (n most recent quarters before date).
    pub fn get_financial_history(
        &self,
        ticker: TickerId,
        date: Date,
        n_quarters: usize,
    ) -> Vec<&FinancialRecord> {
        match self.financials.get(&ticker) {
            Some(records) => records
                .iter()
                .filter(|r| r.filing_date <= date)
                .take(n_quarters)
                .collect(),
            None => vec![],
        }
    }

    /// Get the latest market cap for a ticker on or before date.
    pub fn get_market_cap(&self, ticker: TickerId, date: Date) -> Option<f64> {
        self.enterprise_values.get(&ticker).and_then(|records| {
            records
                .iter()
                .find(|r| r.date <= date)
                .map(|r| r.market_cap)
        })
    }

    /// Get TTM (trailing twelve months) sum for a financial field.
    pub fn get_ttm_value(&self, ticker: TickerId, date: Date, field: &str) -> Option<f64> {
        let history = self.get_financial_history(ticker, date, 4);
        if history.len() < 3 {
            return None;
        }
        let mut sum = 0.0;
        let mut count = 0;
        for record in &history {
            if let Some(&value) = record.fields.get(field) {
                if value.is_finite() {
                    sum += value;
                    count += 1;
                }
            }
        }
        if count >= 3 { Some(sum) } else { None }
    }

    /// Get month-end price N months ago from date.
    pub fn get_month_end_price(
        &self,
        ticker: TickerId,
        date: Date,
        months_ago: u32,
    ) -> Option<f64> {
        let target = subtract_months(date, months_ago)?;
        let ym = YearMonth::from_date(target);
        self.month_end_prices.get(&(ticker, ym)).copied()
    }

    /// Get rolling stats for a ticker on a date.
    pub fn get_rolling_stats(&self, ticker: TickerId, date: Date) -> Option<&RollingStats> {
        self.rolling_stats.get(&(ticker, date))
    }

    /// Get dividends in trailing period.
    pub fn get_trailing_dividends(
        &self,
        ticker: TickerId,
        date: Date,
        lookback_days: i64,
    ) -> f64 {
        let start = date - chrono::Duration::days(lookback_days);
        match self.dividends.get(&ticker) {
            Some(records) => records
                .iter()
                .filter(|r| r.date > start && r.date <= date)
                .map(|r| r.dividend)
                .sum(),
            None => 0.0,
        }
    }

    /// Get index close price on a date.
    pub fn get_index_close(&self, index: &str, date: Date) -> Option<f64> {
        self.index_prices.get(&(index.to_string(), date)).copied()
    }

    /// Get all active tickers for universe filtering.
    pub fn active_tickers(&self) -> Vec<TickerId> {
        self.is_active
            .iter()
            .filter(|&(_, active)| *active)
            .map(|(&id, _)| id)
            .collect()
    }
}

/// Subtract N months from a date (approximate: use last day of target month).
fn subtract_months(date: Date, months: u32) -> Option<Date> {
    let total_months = date.year() * 12 + date.month() as i32 - 1 - months as i32;
    let year = total_months.div_euclid(12);
    let month = (total_months.rem_euclid(12) + 1) as u32;
    // Use last day of target month
    let next_month = if month == 12 {
        NaiveDate::from_ymd_opt(year + 1, 1, 1)?
    } else {
        NaiveDate::from_ymd_opt(year, month + 1, 1)?
    };
    Some(next_month - chrono::Duration::days(1))
}
