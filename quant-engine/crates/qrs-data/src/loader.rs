//! Parquet file loading using Polars.
//! Reads the existing cache/ directory created by the Python system.

use std::path::Path;

use polars::prelude::*;
use tracing::{info, warn};

use qrs_core::error::{QrsError, Result};

/// Load a parquet file into a Polars DataFrame.
pub fn load_parquet(path: &Path) -> Result<DataFrame> {
    let file = std::fs::File::open(path).map_err(|e| {
        QrsError::ParquetNotFound(format!("{}: {}", path.display(), e))
    })?;
    let df = ParquetReader::new(file)
        .finish()
        .map_err(|e| QrsError::DataLoad(format!("{}: {}", path.display(), e)))?;
    info!(
        "Loaded {}: {} rows x {} cols",
        path.file_name().unwrap_or_default().to_string_lossy(),
        df.height(),
        df.width(),
    );
    Ok(df)
}

/// Find a cache file matching the pattern `{table}_{start}_{end}.parquet`.
/// If start/end are provided, checks that cached range covers the requested range.
/// If start/end are empty, returns the first matching file.
pub fn find_cache_file(cache_dir: &Path, table: &str, start: &str, end: &str) -> Option<std::path::PathBuf> {
    let pattern = format!("{table}_");
    let entries = std::fs::read_dir(cache_dir).ok()?;

    for entry in entries.flatten() {
        let name = entry.file_name().to_string_lossy().to_string();
        if !name.starts_with(&pattern) || !name.ends_with(".parquet") {
            continue;
        }
        // Extract dates from filename: {table}_{cached_start}_{cached_end}.parquet
        let stem = name.trim_end_matches(".parquet");
        let date_part = &stem[pattern.len()..];
        let parts: Vec<&str> = date_part.splitn(2, '_').collect();
        if parts.len() == 2 {
            // If no range check needed, return first match
            if start.is_empty() || end.is_empty() {
                return Some(entry.path());
            }
            let cached_start = parts[0];
            let cached_end = parts[1];
            if cached_start <= start && cached_end >= end {
                return Some(entry.path());
            }
        }
    }
    None
}

/// Find any cache file matching `{table}_*.parquet` (ignoring date range).
pub fn find_any_cache_file(cache_dir: &Path, table: &str) -> Option<std::path::PathBuf> {
    find_cache_file(cache_dir, table, "", "")
}

/// Find a cache file matching `{table}_all.parquet` (snapshot tables).
pub fn find_snapshot_cache(cache_dir: &Path, table: &str) -> Option<std::path::PathBuf> {
    let name = format!("{table}_all.parquet");
    let path = cache_dir.join(&name);
    if path.exists() {
        Some(path)
    } else {
        None
    }
}

/// Validate all expected cache files exist and are readable.
/// Returns a list of (filename, rows, cols) for each found file.
pub fn validate_cache(cache_dir: &Path) -> Result<Vec<(String, usize, usize)>> {
    if !cache_dir.exists() {
        return Err(QrsError::DataLoad(format!(
            "Cache directory not found: {}",
            cache_dir.display()
        )));
    }

    let mut results = Vec::new();
    let mut entries: Vec<_> = std::fs::read_dir(cache_dir)?
        .filter_map(|e| e.ok())
        .filter(|e| {
            e.path()
                .extension()
                .map_or(false, |ext| ext == "parquet")
        })
        .collect();
    entries.sort_by_key(|e| e.file_name());

    for entry in entries {
        let path = entry.path();
        let name = path.file_name().unwrap_or_default().to_string_lossy().to_string();

        match load_parquet(&path) {
            Ok(df) => {
                results.push((name, df.height(), df.width()));
            }
            Err(e) => {
                warn!("Failed to load {}: {}", name, e);
                results.push((name, 0, 0));
            }
        }
    }

    Ok(results)
}
