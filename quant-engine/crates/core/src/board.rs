//! A-share board classification — drives limit-up/down thresholds.

use crate::config::AShareMarketRulesConfig;

/// Trading board classification (drives limit-up/down threshold).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Board {
    /// 沪市主板 (60xxxx.SH) / 深市主板·中小板 (000/001/002/003xxx.SZ)
    Main,
    /// 创业板 300xxx.SZ
    ChiNext,
    /// 科创板 688xxx.SH
    StarMarket,
    /// 北交所 4/8xxxxx.BJ
    Bse,
}

/// Identify board from Tushare ts_code (e.g. "000001.SZ", "688001.SH", "830799.BJ").
pub fn board_from_ts_code(ts_code: &str) -> Board {
    if ts_code.ends_with(".BJ") {
        return Board::Bse;
    }
    let code = ts_code.split('.').next().unwrap_or("");
    let prefix3 = code.get(0..3).unwrap_or("");
    match prefix3 {
        "688" => Board::StarMarket,
        "300" | "301" => Board::ChiNext,
        // 沪市主板 600/601/603/605, 深市主板·中小 000/001/002/003
        _ => Board::Main,
    }
}

/// Daily limit-up/down percentage (decimal) for the given board / ST status.
pub fn limit_pct_for(board: Board, is_st: bool, rules: &AShareMarketRulesConfig) -> f64 {
    if is_st {
        return rules.st_limit;
    }
    match board {
        Board::Main => rules.main_board_limit,
        Board::ChiNext => rules.chinext_limit,
        Board::StarMarket => rules.star_market_limit,
        Board::Bse => rules.bse_limit,
    }
}
