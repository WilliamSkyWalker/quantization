//! env.json loader (mercury-style).
//!
//! Reads env.json (search order: ./env.json, ../env.json, ../../env.json),
//! picks the section matching `ENV`, and flattens it into process env vars
//! that downstream `std::env::var(...)` calls already expect.
//!
//! Skipped during Docker build when `QUANT_BUILDING=1`.

use serde_json::Value;
use std::path::PathBuf;
use tracing::{info, warn};

/// Walk up from cwd until we find env.json (or quant-engine/env.json).
fn find_env_json() -> Option<PathBuf> {
    let mut dir = std::env::current_dir().ok()?;
    loop {
        for candidate in [dir.join("env.json"), dir.join("quant-engine").join("env.json")] {
            if candidate.is_file() {
                return Some(candidate);
            }
        }
        if !dir.pop() { return None; }
    }
}

/// Load env.json and flatten into process env vars.
///
/// Returns the resolved file path (for logging) or `None` if skipped/missing.
pub fn load() -> Option<PathBuf> {
    if std::env::var("QUANT_BUILDING").is_ok() {
        info!("QUANT_BUILDING set, skipping env.json load");
        return None;
    }

    let path = match find_env_json() {
        Some(p) => p,
        None => {
            warn!("env.json not found walking up from cwd; relying on process env");
            return None;
        }
    };

    let raw = match std::fs::read_to_string(&path) {
        Ok(s) => s,
        Err(e) => { warn!("env.json read failed at {}: {e}", path.display()); return None; }
    };
    let cfg: Value = match serde_json::from_str(&raw) {
        Ok(v) => v,
        Err(e) => { warn!("env.json parse failed at {}: {e}", path.display()); return None; }
    };

    let env_name = cfg.get("ENV").and_then(|v| v.as_str()).unwrap_or("test").to_string();

    // Multi-env services: pick by ENV
    set_from_obj(&cfg, &["quant", &env_name], &[
        ("host", "DB_HOST"), ("port", "DB_PORT"), ("user", "DB_USER"),
        ("password", "DB_PASSWORD"), ("database", "DB_DATABASE"), ("schema", "DB_SCHEMA"),
    ]);
    set_from_obj(&cfg, &["alpaca", &env_name], &[
        ("api_key", "ALPACA_API_KEY"), ("secret_key", "ALPACA_SECRET_KEY"), ("paper", "ALPACA_PAPER"),
    ]);

    // Single-env services: flat
    set_from_obj(&cfg, &["fmp"], &[
        ("api_key", "FMP_API_KEY"), ("rate_limit", "FMP_RATE_LIMIT"),
    ]);
    set_from_obj(&cfg, &["tushare"], &[
        ("token", "TUSHARE_TOKEN"), ("rate_limit", "TUSHARE_RATE_LIMIT"),
    ]);
    set_from_obj(&cfg, &["fred"], &[
        ("api_key", "FRED_API_KEY"),
    ]);
    set_from_obj(&cfg, &["quiver"], &[
        ("api_key", "QUIVER_API_KEY"), ("rate_limit", "QUIVER_RATE_LIMIT"),
    ]);

    info!("Loaded env.json (ENV={env_name}) from {}", path.display());
    Some(path)
}

/// Walk to `path` inside `root`, then for each (json_key, env_var) pair set the
/// env var if (a) the json key exists, (b) the env var isn't already set
/// (process env wins, allows k8s `-e VAR=…` override).
fn set_from_obj(root: &Value, path: &[&str], mapping: &[(&str, &str)]) {
    let mut cur = root;
    for k in path {
        cur = match cur.get(*k) {
            Some(v) => v,
            None => { return; }
        };
    }
    let obj = match cur.as_object() {
        Some(o) => o,
        None => return,
    };
    for (json_key, env_var) in mapping {
        if std::env::var_os(env_var).is_some() { continue; }
        if let Some(v) = obj.get(*json_key) {
            let s = match v {
                Value::String(s) => s.clone(),
                Value::Number(n) => n.to_string(),
                Value::Bool(b) => b.to_string(),
                Value::Null => continue,
                other => other.to_string(),
            };
            // SAFETY: single-threaded process startup before any tokio runtime.
            unsafe { std::env::set_var(env_var, s); }
        }
    }
}
