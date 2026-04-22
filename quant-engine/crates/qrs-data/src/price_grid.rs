//! PriceGrid: flat 2D array replacing HashMap<(TickerId, Date), PriceBar>.
//!
//! Layout: data[ticker_id * n_dates + date_idx]
//! Empty slots: close == 0.0 sentinel.
//! Eliminates 18M hash computations, single contiguous allocation.

use qrs_core::types::{Date, TickerId};
use rustc_hash::FxHashMap;

use crate::cache::PriceBar;

/// Sentinel PriceBar (represents empty slot).
const EMPTY: PriceBar = PriceBar {
    open: 0.0, high: 0.0, low: 0.0, close: 0.0, adj_close: 0.0,
    volume: 0.0, change_percent: f64::NAN,
    cum_ret_5d: f64::NAN, cum_ret_20d: f64::NAN,
    dvol_20d: f64::NAN, vol_20d: f64::NAN,
    ma60_adj: f64::NAN, dollar_volume: f64::NAN,
};

/// Dense 2D price storage: O(1) lookup by (TickerId, Date).
pub struct PriceGrid {
    data: Vec<PriceBar>,
    n_dates: usize,
    n_tickers: usize,
    date_to_idx: FxHashMap<Date, usize>,
    idx_to_date: Vec<Date>,
    filled: usize, // count of non-empty slots
}

impl PriceGrid {
    /// Create a new PriceGrid with given dimensions.
    pub fn new(n_tickers: usize, dates: &[Date]) -> Self {
        let n_dates = dates.len();
        let mut date_to_idx = FxHashMap::default();
        date_to_idx.reserve(n_dates);
        for (i, &d) in dates.iter().enumerate() {
            date_to_idx.insert(d, i);
        }

        let total = n_tickers * n_dates;
        let data = vec![EMPTY; total];

        Self {
            data,
            n_dates,
            n_tickers,
            date_to_idx,
            idx_to_date: dates.to_vec(),
            filled: 0,
        }
    }

    #[inline]
    fn index(&self, ticker: TickerId, date_idx: usize) -> usize {
        ticker.0 as usize * self.n_dates + date_idx
    }

    /// Insert a PriceBar. Returns true if slot was empty.
    #[inline]
    pub fn insert(&mut self, ticker: TickerId, date: Date, bar: PriceBar) -> bool {
        if let Some(&di) = self.date_to_idx.get(&date) {
            let idx = self.index(ticker, di);
            if idx < self.data.len() {
                let was_empty = self.data[idx].close == 0.0;
                self.data[idx] = bar;
                if was_empty {
                    self.filled += 1;
                }
                return was_empty;
            }
        }
        false
    }

    /// Get a PriceBar by (TickerId, Date). Returns None if empty.
    #[inline]
    pub fn get(&self, ticker: TickerId, date: Date) -> Option<&PriceBar> {
        let di = *self.date_to_idx.get(&date)?;
        let idx = self.index(ticker, di);
        let bar = self.data.get(idx)?;
        if bar.close != 0.0 { Some(bar) } else { None }
    }

    /// Get a mutable PriceBar.
    #[inline]
    pub fn get_mut(&mut self, ticker: TickerId, date: Date) -> Option<&mut PriceBar> {
        let di = *self.date_to_idx.get(&date)?;
        let idx = self.index(ticker, di);
        let bar = self.data.get_mut(idx)?;
        if bar.close != 0.0 { Some(bar) } else { None }
    }

    /// Number of non-empty entries.
    pub fn len(&self) -> usize {
        self.filled
    }

    pub fn is_empty(&self) -> bool {
        self.filled == 0
    }

    /// Iterate over all non-empty (TickerId, Date, &PriceBar) entries.
    pub fn iter(&self) -> impl Iterator<Item = (TickerId, Date, &PriceBar)> + '_ {
        (0..self.n_tickers).flat_map(move |tid| {
            let ticker = TickerId(tid as u32);
            (0..self.n_dates).filter_map(move |di| {
                let idx = tid * self.n_dates + di;
                let bar = &self.data[idx];
                if bar.close != 0.0 {
                    Some((ticker, self.idx_to_date[di], bar))
                } else {
                    None
                }
            })
        })
    }

    /// Iterate entries for a specific date.
    pub fn iter_date(&self, date: Date) -> impl Iterator<Item = (TickerId, &PriceBar)> + '_ {
        let di = self.date_to_idx.get(&date).copied();
        (0..self.n_tickers).filter_map(move |tid| {
            let di = di?;
            let idx = tid * self.n_dates + di;
            let bar = &self.data[idx];
            if bar.close != 0.0 {
                Some((TickerId(tid as u32), bar))
            } else {
                None
            }
        })
    }

    /// Iterate entries for a specific ticker.
    pub fn iter_ticker(&self, ticker: TickerId) -> impl Iterator<Item = (Date, &PriceBar)> + '_ {
        let base = ticker.0 as usize * self.n_dates;
        (0..self.n_dates).filter_map(move |di| {
            let bar = &self.data[base + di];
            if bar.close != 0.0 {
                Some((self.idx_to_date[di], bar))
            } else {
                None
            }
        })
    }

    /// Iterate entries within a date range [start, end] for all tickers.
    /// Much faster than iter() + filter for windowed factors (35-day lookback etc).
    pub fn iter_date_range(&self, start: Date, end: Date) -> impl Iterator<Item = (TickerId, Date, &PriceBar)> + '_ {
        // Find the index range for dates
        let di_start = self.idx_to_date.partition_point(|d| *d < start);
        let di_end = self.idx_to_date.partition_point(|d| *d <= end);

        (0..self.n_tickers).flat_map(move |tid| {
            let ticker = TickerId(tid as u32);
            (di_start..di_end).filter_map(move |di| {
                let idx = tid * self.n_dates + di;
                let bar = &self.data[idx];
                if bar.close != 0.0 {
                    Some((ticker, self.idx_to_date[di], bar))
                } else {
                    None
                }
            })
        })
    }

    /// Total allocated capacity.
    pub fn capacity(&self) -> usize {
        self.data.len()
    }

    /// Memory usage in bytes.
    pub fn memory_bytes(&self) -> usize {
        self.data.len() * std::mem::size_of::<PriceBar>()
            + self.idx_to_date.len() * std::mem::size_of::<Date>()
    }
}
