//! A-share sentiment-driven factor definitions (v2).
//!
//! These factors proxy market *sentiment* / crowd-money-flow behavior via
//! Dragon-Tiger-List (龙虎榜, `a_top_list`) and margin-trading (融资融券,
//! `a_margin_detail`) data — NOT news-text sentiment. Rationale: news
//! sentiment lags price by 30-50% of the move it's reacting to (see
//! `/tmp/qoder_task_v2_sentiment_factors.md`), whereas LHB / margin flows
//! are same-day or next-day proxies for informed / momentum-chasing money.
//!
//! Unit note: `a_top_list.net_rate` is Tushare's own `net_amount/amount*100`
//! ratio — used as-is, no unit conversion needed. `a_margin_detail.rzmre`
//! (今日融资买入额) is in **yuan (元)**, while `ABar::amount` (from
//! `a_daily_price.amount`) is in **thousand yuan (千元)** — verified
//! 2026-08-30 by cross-checking 000017.SZ 2026-08-28
//! (`a_top_list.amount` == 1000 × `a_daily_price.amount`, and
//! `a_margin_detail` values are of the same order-of-magnitude convention
//! as `a_top_list.amount`, i.e. yuan). `MARGIN_BUY_INTENSITY_5D` therefore
//! multiplies `bar.amount` by 1000 before dividing.
//!
//! Directions:
//! - `LHB_NET_RATE_5D` (direction=+1): established — net buying strength on
//!   the Dragon-Tiger-List should predict continuation.
//! - `LHB_APPEARANCE_FREQ_20D` (direction=0): sign unconfirmed — frequent
//!   LHB appearance could mean sustained institutional/hot-money interest
//!   (bullish) or could mean the stock is a retail-driven pump-and-dump
//!   target (bearish). Needs IC validation before assigning a direction.
//! - `MARGIN_BAL_CHG_20D` (direction=+1, tentative): rising margin balance
//!   suggests bullish leveraged conviction, but is also a crowding /
//!   forced-deleveraging risk factor (see finance-basics skill §10) — must
//!   re-validate IC periodically, not just once.
//! - `MARGIN_BUY_INTENSITY_5D` (direction=+1, tentative): margin buying as a
//!   fraction of turnover — high intensity suggests leveraged momentum
//!   chasing, same crowding caveat as above.
//!
//! Deliberately excluded: `a_moneyflow_hsgt` (沪深港通资金流向) has no
//! per-stock dimension (market-level only, northbound/southbound aggregate
//! flows) — it is a candidate for a future regime/macro overlay, not a
//! cross-sectional stock-selection factor. Fabricating a per-stock
//! cross-section from it would be incorrect.

use super::factors::AFactorDef;

type AFactorResult = super::factors::AFactorResult;

/// All 4 v2 sentiment-driven factor definitions.
pub fn all_factors_v2() -> Vec<AFactorDef> {
    vec![
        AFactorDef { name: "LHB_NET_RATE_5D", category: "sentiment", direction: 1,
            compute: |date, cache| {
                let mut r = AFactorResult::new();
                for code in cache.active_codes_on(date) {
                    let days = cache.lhb_days_before(code, date, 5);
                    if days.is_empty() { continue; }
                    let vals: Vec<f64> = days.iter()
                        .map(|(_, v)| v.net_rate)
                        .filter(|v| v.is_finite())
                        .collect();
                    if vals.is_empty() { continue; }
                    let mean = vals.iter().sum::<f64>() / vals.len() as f64;
                    r.insert(code.to_string(), mean);
                }
                r
            }},

        AFactorDef { name: "LHB_APPEARANCE_FREQ_20D", category: "sentiment", direction: 0,
            compute: |date, cache| {
                let mut r = AFactorResult::new();
                for code in cache.active_codes_on(date) {
                    let count = cache.lhb_appearance_count(code, date, 20);
                    // Non-sparse: report 0.0 for stocks with zero appearances too,
                    // since this is a frequency factor (absence is informative).
                    r.insert(code.to_string(), count as f64 / 20.0);
                }
                r
            }},

        AFactorDef { name: "MARGIN_BAL_CHG_20D", category: "sentiment", direction: 1,
            compute: |date, cache| {
                let mut r = AFactorResult::new();
                for code in cache.active_codes_on(date) {
                    let days = cache.margin_days_before(code, date, 21);
                    if days.len() < 21 { continue; }
                    let earliest = days.first().unwrap().1.rzye;
                    let latest = days.last().unwrap().1.rzye;
                    if !earliest.is_finite() || !latest.is_finite() || earliest.abs() < 1e-9 {
                        continue;
                    }
                    r.insert(code.to_string(), (latest - earliest) / earliest);
                }
                r
            }},

        AFactorDef { name: "MARGIN_BUY_INTENSITY_5D", category: "sentiment", direction: 1,
            compute: |date, cache| {
                let mut r = AFactorResult::new();
                for code in cache.active_codes_on(date) {
                    let days = cache.margin_days_before(code, date, 5);
                    if days.is_empty() { continue; }
                    let mut ratios: Vec<f64> = Vec::new();
                    for (d, m) in &days {
                        if !m.rzmre.is_finite() { continue; }
                        if let Some(bar) = cache.get_bar(code, *d) {
                            // bar.amount is in thousand yuan (千元); convert to yuan
                            // to match rzmre's yuan unit before dividing.
                            let amount_yuan = bar.amount * 1000.0;
                            if amount_yuan.abs() > 1e-9 && amount_yuan.is_finite() {
                                ratios.push(m.rzmre / amount_yuan);
                            }
                        }
                    }
                    if ratios.is_empty() { continue; }
                    let mean = ratios.iter().sum::<f64>() / ratios.len() as f64;
                    r.insert(code.to_string(), mean);
                }
                r
            }},
    ]
}

#[cfg(test)]
mod tests {
    use super::*;
    use chrono::NaiveDate;
    use crate::a_share::cache::{ABar, AShareCache, ALhbDay, AMarginDay};
    use rustc_hash::FxHashMap;

    fn empty_bar() -> ABar {
        ABar {
            open: 10.0, high: 10.0, low: 10.0, close: 10.0, pre_close: 10.0,
            vol: 1000.0, amount: 100.0, // thousand yuan
            adj_factor: 1.0, pct_chg: 0.0,
            turnover_rate: 1.0, pe_ttm: 10.0, pb: 1.0, ps_ttm: 1.0, dv_ttm: 1.0,
            total_mv: 1e6, circ_mv: 1e6,
        }
    }

    fn d(day: u32) -> NaiveDate { NaiveDate::from_ymd_opt(2024, 1, day).unwrap() }

    fn make_cache(top_list: FxHashMap<String, Vec<(NaiveDate, ALhbDay)>>,
                  margin_detail: FxHashMap<String, Vec<(NaiveDate, AMarginDay)>>,
                  daily: FxHashMap<String, Vec<(NaiveDate, ABar)>>,
                  trading_days: Vec<NaiveDate>) -> AShareCache {
        let ts_codes: Vec<String> = daily.keys().cloned().collect();
        AShareCache {
            daily,
            financials: FxHashMap::default(),
            industry: FxHashMap::default(),
            basics: FxHashMap::default(),
            trading_days,
            index_prices: FxHashMap::default(),
            ts_codes,
            top_list,
            margin_detail,
        }
    }

    #[test]
    fn lhb_net_rate_5d_averages_existing_entries_only() {
        let mut top_list = FxHashMap::default();
        top_list.insert("000001.SZ".to_string(), vec![
            (d(1), ALhbDay { net_amount: 1.0, l_buy: 2.0, l_sell: 1.0, amount: 3.0, net_rate: 10.0 }),
            (d(3), ALhbDay { net_amount: 1.0, l_buy: 2.0, l_sell: 1.0, amount: 3.0, net_rate: 20.0 }),
        ]);
        let mut daily = FxHashMap::default();
        daily.insert("000001.SZ".to_string(), vec![(d(5), empty_bar())]);
        let cache = make_cache(top_list, FxHashMap::default(), daily, vec![d(1), d(2), d(3), d(4), d(5)]);

        let result = all_factors_v2();
        let f = result.iter().find(|f| f.name == "LHB_NET_RATE_5D").unwrap();
        let scores = (f.compute)(d(5), &cache);
        assert!((scores["000001.SZ"] - 15.0).abs() < 1e-9);
    }

    #[test]
    fn lhb_appearance_freq_20d_reports_zero_for_no_appearance() {
        let mut daily = FxHashMap::default();
        daily.insert("000001.SZ".to_string(), vec![(d(5), empty_bar())]);
        let cache = make_cache(FxHashMap::default(), FxHashMap::default(), daily, vec![d(1), d(2), d(3), d(4), d(5)]);

        let result = all_factors_v2();
        let f = result.iter().find(|f| f.name == "LHB_APPEARANCE_FREQ_20D").unwrap();
        let scores = (f.compute)(d(5), &cache);
        assert_eq!(scores["000001.SZ"], 0.0);
    }

    #[test]
    fn margin_bal_chg_20d_requires_21_entries() {
        let mut margin_detail = FxHashMap::default();
        // Only 3 entries — should be skipped (< 21 required).
        margin_detail.insert("000001.SZ".to_string(), vec![
            (d(1), AMarginDay { rzye: 100.0, rzmre: 10.0 }),
            (d(2), AMarginDay { rzye: 110.0, rzmre: 10.0 }),
            (d(3), AMarginDay { rzye: 120.0, rzmre: 10.0 }),
        ]);
        let mut daily = FxHashMap::default();
        daily.insert("000001.SZ".to_string(), vec![(d(3), empty_bar())]);
        let cache = make_cache(FxHashMap::default(), margin_detail, daily, vec![d(1), d(2), d(3)]);

        let result = all_factors_v2();
        let f = result.iter().find(|f| f.name == "MARGIN_BAL_CHG_20D").unwrap();
        let scores = (f.compute)(d(3), &cache);
        assert!(scores.get("000001.SZ").is_none());
    }

    #[test]
    fn margin_buy_intensity_5d_converts_units_correctly() {
        let mut margin_detail = FxHashMap::default();
        margin_detail.insert("000001.SZ".to_string(), vec![
            (d(1), AMarginDay { rzye: 100.0, rzmre: 50_000.0 }), // yuan
        ]);
        let mut daily = FxHashMap::default();
        // amount = 100 thousand yuan = 100,000 yuan
        daily.insert("000001.SZ".to_string(), vec![(d(1), empty_bar())]);
        let cache = make_cache(FxHashMap::default(), margin_detail, daily, vec![d(1)]);

        let result = all_factors_v2();
        let f = result.iter().find(|f| f.name == "MARGIN_BUY_INTENSITY_5D").unwrap();
        let scores = (f.compute)(d(1), &cache);
        // 50,000 / 100,000 = 0.5
        assert!((scores["000001.SZ"] - 0.5).abs() < 1e-9);
    }
}
