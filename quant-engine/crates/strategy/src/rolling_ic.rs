//! Rolling IC: dynamic factor direction and weight based on trailing IC.
//!
//! Each rebalance period:
//! 1. Compute forward return = price(now) / price(prev_date) - 1
//! 2. Spearman IC = corr(prev_factor_values, forward_return)
//! 3. Maintain rolling window of IC per factor
//! 4. Direction: avg_ic < -0.01 → -1, else +1
//! 5. Weight = direction × ICIR_tier_weight

use std::collections::HashMap;

use quant_core::types::{Date, TickerId};
use quant_data::cache::DataCache;
use rustc_hash::FxHashMap;

use crate::analysis::spearman_ic;

/// ICIR tier weights (from Python's factor analysis results).
fn icir_tier_weight(factor_name: &str) -> f64 {
    match factor_name {
        // T0 super-strong (boosted 2026-04-30):
        //   MOM_12M / INDUSTRY_MOM: #1 winner-discriminator (4/4 dates)
        //   SUE_PEAD / EARNINGS_SURPRISE: PEAD beat-and-raise (Bernard-Thomas)
        //   (Tested at T0 and rejected:
        //    - EV_TO_FCF v17: α 10.68→10.56 (-0.22%/yr)
        //    - PIOTROSKI v19: α regress -1.70%/yr (over-defensive)
        //    - AMIHUD_ILLIQ v20: no effect — universe min_dvol=$10M already
        //      filters out low-liquidity, AMIHUD has limited variation here.)
        "MOM_12M" | "TSMOM" | "INDUSTRY_MOM"
        | "SUE_PEAD" | "EARNINGS_SURPRISE" => 3.0,

        // T1 strong signal (|ICIR| >= 0.3)
        "FREE_FLOAT_PCT" | "TURN_20D" | "PIOTROSKI_F" | "EV_TO_FCF"
        | "AMIHUD_ILLIQ" | "COMPOSITE_EQUITY_ISSUANCE" | "ROE_TTM"
        // REVENUE_YOY boosted T2→T1 (2026-04-30): winner audit found Winners
        // 21% rev growth vs Losers 7%, all 4 sample dates.
        | "REVENUE_YOY" => 2.0,

        // T3 weak signal (0.05 <= |ICIR| < 0.15)
        "PRICE_52W_HIGH" | "VOLUME_RATIO" | "GROSS_MARGIN" | "SHAREHOLDER_YIELD"
        | "EARNINGS_PERSISTENCE" | "DAYS_SINCE_EARNINGS" | "OHLSON_O" | "REC_CHANGE"
        | "LOBBY_INTENSITY" | "EV_TO_EBIT" | "RESIDUAL_MOM_FF3" | "LOG_MARKET_CAP"
        | "ANALYST_DISPERSION" | "REV_CONCENTRATION" | "ASSET_GROWTH" | "RSI_14" => 0.5,

        // Direction-flip factors (|ICIR| < 0.05 but single-year > 0.5)
        "BP" | "INTANGIBLE_ADJ_BP" | "RD_INTENSITY" | "PV_TREND" | "EPS_REVISION"
        | "MOM_1M" | "BENEISH_M" | "VOLATILITY_21D" | "VOL_20D" | "ALTMAN_Z"
        | "CAPEX_GROWTH" | "MAX_RET" => 0.3,

        // True noise
        "GEO_CONCENTRATION" => 0.0,

        // T2 default (0.15 <= |ICIR| < 0.3)
        _ => 1.0,
    }
}

/// Factors with inherent negative direction (high = bad).
fn is_inherent_reverse(factor_name: &str) -> bool {
    matches!(factor_name,
        "TURN_20D" | "VOL_20D" | "IVOL" | "VOLATILITY_21D" | "VOLUME_RATIO"
        | "QMJ_LEVERAGE" | "QMJ_EARNINGS_VOL" | "QMJ_ROE_VOL"
        | "CASH_CONV_CYCLE" | "BENEISH_M" | "OHLSON_O" | "MAX_RET"
        | "DOWNSIDE_BETA" | "DARK_POOL_SHORT"
        | "ASSET_GROWTH" | "NET_OPERATING_ASSETS" | "COMPOSITE_EQUITY_ISSUANCE"
        // EV_TO_FCF / EV_TO_EBIT removed (2026-04-30): empirical ICIR=+0.875 /
        // +0.565, raw → positive return. Treating them as "value-low-good" was
        // structurally fighting the strongest signal in the system. Default
        // direction (+1) now aligns with empirical IC and winner signature
        // (Winners EV/FCF=113 vs Losers=7 — expensive stocks won 14y).
        // (QMJ_LEVERAGE / ANALYST_DISPERSION tested for flip in v15 — α regressed
        // 10.44→9.73, kept in reverse list. IC > 0 doesn't always mean flip helps
        // due to multi-factor portfolio interactions.)
        | "EV_TO_SALES" | "AMIHUD_ILLIQ"
        | "ANALYST_DISPERSION"
    )
}

/// Factors that should never flip direction.
fn is_never_reverse(factor_name: &str) -> bool {
    matches!(factor_name,
        "ROE_TTM" | "GROSS_MARGIN" | "PROFIT_STB" | "ACCRUALS" | "MARGIN_TREND"
        | "PIOTROSKI_F" | "ALTMAN_Z" | "EARNINGS_PERSISTENCE"
        | "QMJ_NET_PAYOUT" | "SHAREHOLDER_YIELD"
    )
}

/// Rolling IC state maintained across rebalance dates.
pub struct RollingIcState {
    /// Per-factor rolling IC values.
    ic_history: HashMap<String, Vec<f64>>,
    /// Previous period's factor snapshot: factor_name -> (ticker -> value).
    prev_snapshot: Option<HashMap<String, FxHashMap<TickerId, f64>>>,
    /// Previous rebalance date.
    prev_date: Option<Date>,
}

impl RollingIcState {
    pub fn new() -> Self {
        Self {
            ic_history: HashMap::new(),
            prev_snapshot: None,
            prev_date: None,
        }
    }

    /// Update rolling IC and return new factor weights.
    /// Call once per rebalance date AFTER computing factor values.
    pub fn update(
        &mut self,
        date: Date,
        current_factors: &HashMap<String, FxHashMap<TickerId, f64>>,
        cache: &DataCache,
    ) -> HashMap<String, f64> {
        // Step 1: Compute IC using previous snapshot + current period returns
        if let (Some(prev_snap), Some(prev_date)) = (&self.prev_snapshot, self.prev_date) {
            // Compute forward returns: price(date) / price(prev_date) - 1
            let fwd_returns = compute_period_returns(prev_date, date, cache);

            if fwd_returns.len() >= 30 {
                for (fname, prev_values) in prev_snap {
                    if is_inherent_reverse(fname) || is_never_reverse(fname) {
                        continue; // Direction fixed, don't compute IC
                    }

                    if let Some(ic) = spearman_ic(prev_values, &fwd_returns) {
                        let history = self.ic_history.entry(fname.clone()).or_default();
                        history.push(ic);

                        // Trim to max window
                        let max_window = ic_window_months(fname);
                        if history.len() > max_window {
                            let excess = history.len() - max_window;
                            history.drain(..excess);
                        }
                    }
                }
            }
        }

        // Step 2: Save current snapshot for next period
        self.prev_snapshot = Some(current_factors.clone());
        self.prev_date = Some(date);

        // Step 3: Compute weights = direction × ICIR tier
        // Direction: inherent_reverse → -1, never_reverse → +1,
        //            others → rolling IC mean < -0.01 → -1, else +1
        // Tier: always applied (from factor analysis, not dependent on IC history)
        let mut weights = HashMap::new();

        for fname in current_factors.keys() {
            let direction = if is_inherent_reverse(fname) {
                -1.0
            } else if is_never_reverse(fname) {
                1.0
            } else {
                // Direction flip disabled: IC-based direction is too noisy
                // after neutralization strips sector alpha.
                // Keep positive direction, let ICIR tier handle weighting.
                1.0
            };

            let tier = icir_tier_weight(fname);
            weights.insert(fname.clone(), direction * tier);
        }

        weights
    }
}

/// IC window size per factor (months).
fn ic_window_months(factor_name: &str) -> usize {
    match factor_name {
        // Fundamentals: slow style rotation, 24-36M
        "EP" | "BP" | "DIV_YIELD" | "BUYBACK_YIELD" => 30,
        "NET_PROFIT_CAGR_3Y" => 36,
        "NET_PROFIT_YOY" | "REVENUE_YOY" => 24,
        // Momentum/technical: fast decay, 6-12M
        "MOM_1M" => 6, "MOM_3M" => 9, "MOM_12M" => 12, "REV_5D" => 6,
        "SIZE" => 24,
        // Analyst/earnings: medium, 12-18M
        "US_ANALYST_RATING" | "US_ANALYST_COVERAGE" => 18,
        "EARNINGS_SURPRISE" => 18, "EPS_REVISION" => 12, "INSIDER_NET_BUY" => 12,
        // Sentiment/alternative: short, 6M
        "LOBBY_INTENSITY" | "GOV_CONTRACT_FLOW" => 12,
        // Default
        _ => 18,
    }
}

/// Compute period returns: price(end_date) / price(start_date) - 1 for each ticker.
fn compute_period_returns(
    start_date: Date,
    end_date: Date,
    cache: &DataCache,
) -> FxHashMap<TickerId, f64> {
    let mut result = FxHashMap::default();

    // Get prices on both dates
    for (tid, bar_end) in cache.daily_prices.iter_date(end_date) {
        if !bar_end.adj_close.is_finite() || bar_end.adj_close <= 0.0 {
            continue;
        }
        if let Some(bar_start) = cache.daily_prices.get(tid, start_date) {
            if bar_start.adj_close.is_finite() && bar_start.adj_close > 0.0 {
                let ret = bar_end.adj_close / bar_start.adj_close - 1.0;
                if ret.is_finite() {
                    result.insert(tid, ret);
                }
            }
        }
    }

    result
}
