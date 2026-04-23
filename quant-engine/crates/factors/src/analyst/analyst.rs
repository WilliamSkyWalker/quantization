//! Analyst factors: US_ANALYST_RATING, US_ANALYST_COVERAGE

use std::collections::HashSet;

use quant_core::types::{Date, FactorResult};
use quant_data::cache::DataCache;

use crate::registry::Factor;

const LOOKBACK_DAYS: i64 = 120;

fn rating_score(grade: &str) -> Option<f64> {
    match grade.trim() {
        "Strong Buy" => Some(5.0),
        "Buy" | "Outperform" | "Overweight" => Some(4.0),
        "Market Perform" | "Hold" | "Neutral" | "Equal-Weight"
        | "Sector Perform" | "In-Line" | "Peer Perform" => Some(3.0),
        "Underperform" | "Underweight" | "Reduce" => Some(2.0),
        "Sell" | "Strong Sell" => Some(1.0),
        _ => None,
    }
}

/// Analyst Rating: mean rating score over trailing window
pub struct UsAnalystRating;
inventory::submit! { &UsAnalystRating as &dyn Factor }

impl Factor for UsAnalystRating {
    fn name(&self) -> &'static str { "US_ANALYST_RATING" }
    fn category(&self) -> &'static str { "analyst" }
    fn inherent_direction(&self) -> i8 { 0 }
    fn ic_window_months(&self) -> u32 { 12 }

    fn compute(&self, date: Date, cache: &DataCache) -> FactorResult {
        let start = date - chrono::Duration::days(LOOKBACK_DAYS);
        let mut result = FactorResult::default();

        for (&tid, recs) in &cache.analyst_recs {
            let scores: Vec<f64> = recs
                .iter()
                .filter(|r| r.date >= start && r.date <= date)
                .filter_map(|r| rating_score(&r.new_grade))
                .collect();
            if !scores.is_empty() {
                let mean = scores.iter().sum::<f64>() / scores.len() as f64;
                result.insert(tid, mean);
            }
        }
        result
    }
}

/// Analyst Coverage: log(1 + distinct analyst firms) in trailing window
pub struct UsAnalystCoverage;
inventory::submit! { &UsAnalystCoverage as &dyn Factor }

impl Factor for UsAnalystCoverage {
    fn name(&self) -> &'static str { "US_ANALYST_COVERAGE" }
    fn category(&self) -> &'static str { "analyst" }
    fn inherent_direction(&self) -> i8 { 0 }
    fn ic_window_months(&self) -> u32 { 12 }

    fn compute(&self, date: Date, cache: &DataCache) -> FactorResult {
        let start = date - chrono::Duration::days(LOOKBACK_DAYS);
        let mut result = FactorResult::default();

        for (&tid, recs) in &cache.analyst_recs {
            let firms: HashSet<&str> = recs
                .iter()
                .filter(|r| r.date >= start && r.date <= date)
                .map(|r| r.grading_company.as_str())
                .filter(|s| !s.is_empty())
                .collect();
            if !firms.is_empty() {
                result.insert(tid, (1.0 + firms.len() as f64).ln());
            }
        }
        result
    }
}

/// Insider Net Buy: net insider purchases / market cap over trailing 90 days
pub struct InsiderNetBuy;
inventory::submit! { &InsiderNetBuy as &dyn Factor }

impl Factor for InsiderNetBuy {
    fn name(&self) -> &'static str { "INSIDER_NET_BUY" }
    fn category(&self) -> &'static str { "analyst" }
    fn inherent_direction(&self) -> i8 { 0 }
    fn ic_window_months(&self) -> u32 { 12 }

    fn compute(&self, date: Date, cache: &DataCache) -> FactorResult {
        let start = date - chrono::Duration::days(90);
        let mut result = FactorResult::default();

        for (&tid, trades) in &cache.insider_trades {
            let net_value: f64 = trades
                .iter()
                .filter(|t| t.filing_date >= start && t.filing_date <= date)
                .map(|t| {
                    let val = t.securities_transacted * t.price;
                    if t.is_acquisition { val } else { -val }
                })
                .filter(|v| v.is_finite())
                .sum();

            if net_value.abs() < 1e-6 {
                continue;
            }

            let mktcap = match cache.get_market_cap(tid, date) {
                Some(m) if m > 0.0 => m,
                _ => continue,
            };

            let ratio = net_value / mktcap;
            if ratio.is_finite() {
                result.insert(tid, ratio);
            }
        }
        result
    }
}
