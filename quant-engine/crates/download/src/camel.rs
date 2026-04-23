//! camelCase → snake_case conversion for FMP API field names.
//!
//! Handles abbreviations (EBITDA, EPS, ROE, etc.) and renames (symbol → ticker).

use std::collections::HashMap;
use std::sync::LazyLock;

static ABBREVIATIONS: LazyLock<Vec<(&str, &str)>> = LazyLock::new(|| {
    let mut v = vec![
        ("EBITDA", "ebitda"), ("EBIT", "ebit"), ("EPS", "eps"),
        ("EBT", "ebt"), ("ROE", "roe"), ("ROA", "roa"),
        ("ROIC", "roic"), ("ROCE", "roce"), ("SGA", "sga"),
        ("OCF", "ocf"), ("FCF", "fcf"), ("DCF", "dcf"),
        ("TTM", "ttm"), ("IPO", "ipo"), ("CEO", "ceo"),
        ("CFO", "cfo"), ("CIK", "cik"), ("SIC", "sic"),
        ("ESG", "esg"), ("ETF", "etf"), ("WACC", "wacc"),
        ("USD", "usd"), ("PE", "pe"), ("PB", "pb"),
    ];
    // Sort by length descending so longer abbreviations match first
    v.sort_by(|a, b| b.0.len().cmp(&a.0.len()));
    v
});

static RENAMES: LazyLock<HashMap<&str, &str>> = LazyLock::new(|| {
    let mut m = HashMap::new();
    m.insert("symbol", "ticker");
    m
});

/// Convert a camelCase FMP field name to snake_case.
pub fn camel_to_snake(name: &str) -> String {
    if let Some(&renamed) = RENAMES.get(name) {
        return renamed.to_string();
    }

    let mut result = name.to_string();

    // Replace known abbreviations
    for &(abbr, replacement) in ABBREVIATIONS.iter() {
        while let Some(idx) = result.find(abbr) {
            let before = if idx > 0 { result.as_bytes()[idx - 1] } else { 0 };
            let after_idx = idx + abbr.len();
            let after = if after_idx < result.len() { result.as_bytes()[after_idx] } else { 0 };

            let prefix = if before != 0 && (before as char).is_ascii_lowercase() { "_" } else { "" };
            let suffix = if after != 0 && (after as char).is_ascii_lowercase() { "_" } else { "" };

            result = format!(
                "{}{}{}{}{}",
                &result[..idx],
                prefix,
                replacement,
                suffix,
                &result[after_idx..]
            );
        }
    }

    // Standard camelCase → snake_case
    let mut out = String::with_capacity(result.len() + 4);
    let bytes = result.as_bytes();
    for i in 0..bytes.len() {
        let c = bytes[i] as char;
        if c.is_ascii_uppercase() {
            if i > 0 {
                let prev = bytes[i - 1] as char;
                if prev.is_ascii_lowercase() || prev.is_ascii_digit() {
                    out.push('_');
                } else if i + 1 < bytes.len() && (bytes[i + 1] as char).is_ascii_lowercase() {
                    out.push('_');
                }
            }
            out.push(c.to_ascii_lowercase());
        } else {
            out.push(c);
        }
    }

    // Clean up double underscores
    while out.contains("__") {
        out = out.replace("__", "_");
    }
    out.trim_matches('_').to_string()
}

/// Convert a JSON object's keys from camelCase to snake_case.
pub fn snake_keys(obj: &serde_json::Value) -> serde_json::Value {
    match obj {
        serde_json::Value::Object(map) => {
            let new_map: serde_json::Map<String, serde_json::Value> = map
                .iter()
                .map(|(k, v)| (camel_to_snake(k), snake_keys(v)))
                .collect();
            serde_json::Value::Object(new_map)
        }
        serde_json::Value::Array(arr) => {
            serde_json::Value::Array(arr.iter().map(snake_keys).collect())
        }
        other => other.clone(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_basics() {
        assert_eq!(camel_to_snake("symbol"), "ticker");
        assert_eq!(camel_to_snake("adjClose"), "adj_close");
        assert_eq!(camel_to_snake("changePercent"), "change_percent");
        assert_eq!(camel_to_snake("marketCap"), "market_cap");
    }

    #[test]
    fn test_abbreviations() {
        assert_eq!(camel_to_snake("netIncomeRatio"), "net_income_ratio");
        assert_eq!(camel_to_snake("epsDiluted"), "eps_diluted");
        assert_eq!(camel_to_snake("isETF"), "is_etf");
    }
}
