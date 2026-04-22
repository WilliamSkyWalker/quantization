//! Alternative factors: ESG_RISK, EMPLOYEE_GROWTH, CONGRESS_NET_BUY,
//! GOV_CONTRACT_FLOW, LOBBY_INTENSITY, REV_CONCENTRATION, GEO_CONCENTRATION, SEGMENT_GROWTH_DISP

use qrs_core::types::{Date, FactorResult};
use qrs_data::cache::DataCache;
use crate::registry::Factor;

pub struct EsgRisk;
inventory::submit! { &EsgRisk as &dyn Factor }
impl Factor for EsgRisk {
    fn name(&self) -> &'static str { "ESG_RISK" }
    fn category(&self) -> &'static str { "alternative" }
    fn inherent_direction(&self) -> i8 { 0 }
    fn ic_window_months(&self) -> u32 { 36 }
    fn compute(&self, _date: Date, cache: &DataCache) -> FactorResult {
        let mut result = FactorResult::default();
        for (&tid, &rating) in &cache.esg_ratings {
            if rating.is_finite() { result.insert(tid, rating); }
        }
        result
    }
}

pub struct EmployeeGrowth;
inventory::submit! { &EmployeeGrowth as &dyn Factor }
impl Factor for EmployeeGrowth {
    fn name(&self) -> &'static str { "EMPLOYEE_GROWTH" }
    fn category(&self) -> &'static str { "alternative" }
    fn inherent_direction(&self) -> i8 { 0 }
    fn ic_window_months(&self) -> u32 { 24 }
    fn compute(&self, date: Date, cache: &DataCache) -> FactorResult {
        let mut result = FactorResult::default();
        for (&tid, records) in &cache.employee_counts {
            let recent: Vec<_> = records.iter().filter(|r| r.filing_date <= date).take(2).collect();
            if recent.len() < 2 { continue; }
            let latest = recent[0].employee_count;
            let prev = recent[1].employee_count;
            if prev > 0.0 && latest.is_finite() && prev.is_finite() {
                let growth = (latest - prev) / prev;
                if growth.is_finite() { result.insert(tid, growth); }
            }
        }
        result
    }
}

pub struct CongressNetBuy;
inventory::submit! { &CongressNetBuy as &dyn Factor }
impl Factor for CongressNetBuy {
    fn name(&self) -> &'static str { "CONGRESS_NET_BUY" }
    fn category(&self) -> &'static str { "alternative" }
    fn inherent_direction(&self) -> i8 { 1 }
    fn ic_window_months(&self) -> u32 { 12 }
    fn compute(&self, date: Date, cache: &DataCache) -> FactorResult {
        let start = date - chrono::Duration::days(90);
        let mut result = FactorResult::default();
        for (&tid, records) in &cache.congress_trades {
            let recent: Vec<_> = records.iter().filter(|r| r.date >= start && r.date <= date).collect();
            if recent.is_empty() { continue; }
            let buys = recent.iter().filter(|r| r.is_purchase).count() as f64;
            let total = recent.len() as f64;
            let ratio = (2.0 * buys - total) / total; // -1 (all sell) to +1 (all buy)
            if ratio.is_finite() { result.insert(tid, ratio); }
        }
        result
    }
}

pub struct GovContractFlow;
inventory::submit! { &GovContractFlow as &dyn Factor }
impl Factor for GovContractFlow {
    fn name(&self) -> &'static str { "GOV_CONTRACT_FLOW" }
    fn category(&self) -> &'static str { "alternative" }
    fn inherent_direction(&self) -> i8 { 1 }
    fn ic_window_months(&self) -> u32 { 12 }
    fn compute(&self, date: Date, cache: &DataCache) -> FactorResult {
        use chrono::Datelike;
        let cur_year = date.year();
        let mut result = FactorResult::default();
        for (&tid, records) in &cache.gov_contracts {
            // Sum last 4 quarters
            let total: f64 = records.iter()
                .filter(|r| r.year >= cur_year - 1)
                .take(4)
                .map(|r| r.amount)
                .filter(|a| a.is_finite())
                .sum();
            if total <= 0.0 { continue; }
            let mc = match cache.get_market_cap(tid, date) { Some(m) if m > 0.0 => m, _ => continue };
            let ratio = total / mc;
            if ratio.is_finite() { result.insert(tid, ratio); }
        }
        result
    }
}

pub struct LobbyIntensity;
inventory::submit! { &LobbyIntensity as &dyn Factor }
impl Factor for LobbyIntensity {
    fn name(&self) -> &'static str { "LOBBY_INTENSITY" }
    fn category(&self) -> &'static str { "alternative" }
    fn inherent_direction(&self) -> i8 { 1 }
    fn ic_window_months(&self) -> u32 { 12 }
    fn compute(&self, date: Date, cache: &DataCache) -> FactorResult {
        let start = date - chrono::Duration::days(365);
        let mut result = FactorResult::default();
        for (&tid, records) in &cache.lobbying {
            let total: f64 = records.iter()
                .filter(|r| r.date >= start && r.date <= date)
                .map(|r| r.amount)
                .filter(|a| a.is_finite())
                .sum();
            if total <= 0.0 { continue; }
            let mc = match cache.get_market_cap(tid, date) { Some(m) if m > 0.0 => m, _ => continue };
            let ratio = total / mc;
            if ratio.is_finite() { result.insert(tid, ratio); }
        }
        result
    }
}

/// Revenue Concentration (HHI): product segments
pub struct RevConcentration;
inventory::submit! { &RevConcentration as &dyn Factor }
impl Factor for RevConcentration {
    fn name(&self) -> &'static str { "REV_CONCENTRATION" }
    fn category(&self) -> &'static str { "alternative" }
    fn inherent_direction(&self) -> i8 { 0 }
    fn ic_window_months(&self) -> u32 { 24 }
    fn compute(&self, date: Date, cache: &DataCache) -> FactorResult {
        compute_hhi(date, cache, "product")
    }
}

/// Geographic Concentration (HHI): geographic segments
pub struct GeoConcentration;
inventory::submit! { &GeoConcentration as &dyn Factor }
impl Factor for GeoConcentration {
    fn name(&self) -> &'static str { "GEO_CONCENTRATION" }
    fn category(&self) -> &'static str { "alternative" }
    fn inherent_direction(&self) -> i8 { 0 }
    fn ic_window_months(&self) -> u32 { 24 }
    fn compute(&self, date: Date, cache: &DataCache) -> FactorResult {
        compute_hhi(date, cache, "geographic")
    }
}

fn compute_hhi(date: Date, cache: &DataCache, seg_type: &str) -> FactorResult {
    let mut result = FactorResult::default();
    for (&tid, records) in &cache.revenue_segments {
        // Get latest period's segments of the right type
        let latest_date = records.iter()
            .filter(|r| r.date <= date && r.segment_type.to_lowercase().contains(seg_type))
            .map(|r| r.date)
            .max();
        let Some(ld) = latest_date else { continue };

        let segments: Vec<f64> = records.iter()
            .filter(|r| r.date == ld && r.segment_type.to_lowercase().contains(seg_type))
            .map(|r| r.revenue)
            .filter(|r| r.is_finite() && *r > 0.0)
            .collect();
        if segments.len() < 2 { continue; }

        let total: f64 = segments.iter().sum();
        if total <= 0.0 { continue; }
        let hhi: f64 = segments.iter().map(|s| (s / total).powi(2)).sum();
        if hhi.is_finite() { result.insert(tid, hhi); }
    }
    result
}

pub struct SegmentGrowthDisp;
inventory::submit! { &SegmentGrowthDisp as &dyn Factor }
impl Factor for SegmentGrowthDisp {
    fn name(&self) -> &'static str { "SEGMENT_GROWTH_DISP" }
    fn category(&self) -> &'static str { "alternative" }
    fn inherent_direction(&self) -> i8 { 0 }
    fn ic_window_months(&self) -> u32 { 24 }
    fn compute(&self, date: Date, cache: &DataCache) -> FactorResult {
        use chrono::Datelike;
        let mut result = FactorResult::default();
        for (&tid, records) in &cache.revenue_segments {
            let products: Vec<_> = records.iter()
                .filter(|r| r.date <= date && r.segment_type.to_lowercase().contains("product"))
                .collect();
            if products.is_empty() { continue; }

            // Get unique dates (latest 2 years)
            let mut dates: Vec<_> = products.iter().map(|r| r.date).collect();
            dates.sort(); dates.dedup();
            if dates.len() < 2 { continue; }

            let latest = *dates.last().unwrap();
            let prior = dates.iter().rev().find(|&&d| d.year() < latest.year());
            let Some(&prior_date) = prior else { continue };

            // Compute YoY growth per segment
            let mut growths = Vec::new();
            let latest_segs: std::collections::HashMap<&str, f64> = products.iter()
                .filter(|r| r.date == latest)
                .map(|r| (r.segment_name.as_str(), r.revenue))
                .collect();
            let prior_segs: std::collections::HashMap<&str, f64> = products.iter()
                .filter(|r| r.date == prior_date)
                .map(|r| (r.segment_name.as_str(), r.revenue))
                .collect();

            for (name, &rev_now) in &latest_segs {
                if let Some(&rev_prev) = prior_segs.get(name) {
                    if rev_prev.abs() > 1e-6 {
                        growths.push((rev_now - rev_prev) / rev_prev.abs());
                    }
                }
            }

            if growths.len() < 2 { continue; }
            let n = growths.len() as f64;
            let mean = growths.iter().sum::<f64>() / n;
            let std = (growths.iter().map(|g| (g - mean).powi(2)).sum::<f64>() / (n - 1.0)).sqrt();
            if std.is_finite() { result.insert(tid, std); }
        }
        result
    }
}
