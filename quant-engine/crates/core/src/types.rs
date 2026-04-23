//! Core types: TickerId, SectorId, TickerInterner, FactorResult.

use rustc_hash::FxHashMap;
use std::fmt;

/// Interned ticker ID for O(1) comparison and fast HashMap performance.
/// String tickers are interned at load time; all internal ops use TickerId.
#[derive(Clone, Copy, PartialEq, Eq, Hash, PartialOrd, Ord)]
pub struct TickerId(pub u32);

impl fmt::Debug for TickerId {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "T({})", self.0)
    }
}

impl fmt::Display for TickerId {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{}", self.0)
    }
}

/// Interned sector/industry ID.
#[derive(Clone, Copy, PartialEq, Eq, Hash, PartialOrd, Ord)]
pub struct SectorId(pub u16);

impl fmt::Debug for SectorId {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "S({})", self.0)
    }
}

/// NaiveDate from chrono — trading dates have no timezone.
pub type Date = chrono::NaiveDate;

/// Year-month key for month-end price lookups.
#[derive(Clone, Copy, PartialEq, Eq, Hash, PartialOrd, Ord, Debug)]
pub struct YearMonth {
    pub year: i32,
    pub month: u32,
}

impl YearMonth {
    pub fn new(year: i32, month: u32) -> Self {
        Self { year, month }
    }

    pub fn from_date(date: Date) -> Self {
        Self {
            year: date.year(),
            month: date.month(),
        }
    }
}

impl fmt::Display for YearMonth {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{}-{:02}", self.year, self.month)
    }
}

use chrono::Datelike;

/// Cross-sectional factor result: ticker -> value.
/// Tickers with no valid value are simply absent from the map.
pub type FactorResult = FxHashMap<TickerId, f64>;

/// Bidirectional String <-> TickerId mapping.
/// Constructed once at parquet load time, shared via Arc.
pub struct TickerInterner {
    to_id: FxHashMap<String, TickerId>,
    to_str: Vec<String>,
}

impl TickerInterner {
    pub fn new() -> Self {
        Self {
            to_id: FxHashMap::default(),
            to_str: Vec::new(),
        }
    }

    /// Intern a ticker string, returning its TickerId.
    /// If already interned, returns existing ID.
    pub fn intern(&mut self, ticker: &str) -> TickerId {
        if let Some(&id) = self.to_id.get(ticker) {
            return id;
        }
        let id = TickerId(self.to_str.len() as u32);
        self.to_str.push(ticker.to_string());
        self.to_id.insert(ticker.to_string(), id);
        id
    }

    /// Look up TickerId by string. Returns None if not interned.
    pub fn get_id(&self, ticker: &str) -> Option<TickerId> {
        self.to_id.get(ticker).copied()
    }

    /// Resolve TickerId back to string.
    pub fn resolve(&self, id: TickerId) -> &str {
        &self.to_str[id.0 as usize]
    }

    /// Total number of interned tickers.
    pub fn len(&self) -> usize {
        self.to_str.len()
    }

    pub fn is_empty(&self) -> bool {
        self.to_str.is_empty()
    }
}

impl Default for TickerInterner {
    fn default() -> Self {
        Self::new()
    }
}

/// Bidirectional String <-> SectorId mapping.
pub struct SectorInterner {
    to_id: FxHashMap<String, SectorId>,
    to_str: Vec<String>,
}

impl SectorInterner {
    pub fn new() -> Self {
        Self {
            to_id: FxHashMap::default(),
            to_str: Vec::new(),
        }
    }

    pub fn intern(&mut self, sector: &str) -> SectorId {
        if let Some(&id) = self.to_id.get(sector) {
            return id;
        }
        let id = SectorId(self.to_str.len() as u16);
        self.to_str.push(sector.to_string());
        self.to_id.insert(sector.to_string(), id);
        id
    }

    pub fn get_id(&self, sector: &str) -> Option<SectorId> {
        self.to_id.get(sector).copied()
    }

    pub fn resolve(&self, id: SectorId) -> &str {
        &self.to_str[id.0 as usize]
    }

    pub fn len(&self) -> usize {
        self.to_str.len()
    }

    pub fn is_empty(&self) -> bool {
        self.to_str.is_empty()
    }
}

impl Default for SectorInterner {
    fn default() -> Self {
        Self::new()
    }
}
