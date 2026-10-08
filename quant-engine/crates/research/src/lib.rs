//! A-share whole-market money-flow research, separate from portfolio execution.
pub mod a_execution_price;
pub mod a_low_price_cases;
pub mod a_minutes;
pub mod a_pair_validation;
pub mod a_universal;
pub mod analysis;
pub mod data;

use chrono::NaiveDate;
use quant_core::config::Config;
use serde_json::json;
use std::path::Path;

pub type Error = Box<dyn std::error::Error + Send + Sync>;

pub async fn run(
    config: &Config,
    stage: &str,
    cache: &Path,
    output: &Path,
    start: NaiveDate,
    end: NaiveDate,
    workers: usize,
    entry_price: a_execution_price::EntryPriceModel,
) -> Result<(), Error> {
    if start > end {
        return Err("start must not be after end".into());
    }
    if !["fetch", "analyze", "all"].contains(&stage) {
        return Err("stage must be fetch, analyze or all".into());
    }
    let began = std::time::Instant::now();
    let threads = if workers == 0 {
        std::thread::available_parallelism()?.get().min(8)
    } else {
        workers
    };
    rayon::ThreadPoolBuilder::new()
        .num_threads(threads)
        .build_global()?;
    let pool =
        quant_db::pool::create_pool(&config.database.url(), &config.database.schema, 4).await?;
    if stage != "analyze" {
        data::fetch(&pool, cache, start, end).await?;
    }
    if stage != "fetch" {
        if start < NaiveDate::from_ymd_opt(2023, 10, 1).unwrap()
            || end > NaiveDate::from_ymd_opt(2026, 9, 30).unwrap()
        {
            return Err(
                "Analysis must stay inside the frozen 2023-10-01..2026-09-30 study window".into(),
            );
        }
        std::fs::create_dir_all(output)?;
        let lock = std::fs::OpenOptions::new()
            .create(true)
            .truncate(false)
            .write(true)
            .open(output.join(".analysis.lock"))?;
        lock.try_lock()
            .map_err(|e| format!("Research output is locked: {e}"))?;
        freeze_protocol(output, entry_price)?;
        let actual = json!({"start":start,"end":end,"raw_cache":std::fs::canonicalize(cache)?});
        let run_config = output.join("run_config.json");
        if run_config.exists() {
            let saved: serde_json::Value = serde_json::from_slice(&std::fs::read(&run_config)?)?;
            if actual != saved {
                return Err("Run range or raw cache changed; use a new output directory".into());
            }
        } else {
            atomic_json(&run_config, &actual)?;
        }
        atomic_json(
            &output.join("run_state.json"),
            &json!({"status":"running","started_at":chrono::Utc::now()}),
        )?;
        let dataset = data::load(&pool, cache, start, end).await?;
        tracing::info!(
            stocks = dataset.stocks.len(),
            dates = dataset.dates.len(),
            seconds = began.elapsed().as_secs_f64(),
            "Research data loaded"
        );
        if entry_price.uses_full_day_range() {
            tracing::warn!(
                "Entry fill uses full-day low + (high-low)*2/3: an ex-post price assumption, not a minute-level execution backtest"
            );
        }
        analysis::analyze(&dataset, output, entry_price)?;
        let manifest = json!({"implementation":"Rust quant-research", "start":start,"end":end,
            "workers":threads,"elapsed_seconds":began.elapsed().as_secs_f64(),
            "entry_price":entry_price.as_str(),"entry_assumption":entry_price.description(),
            "industry_minimum_other_peers":10,"bootstrap":"2000 monthly draws, deterministic seed 42; unadjusted for multiple testing"});
        atomic_json(&output.join("run_manifest.json"), &manifest)?;
        atomic_json(
            &output.join("run_state.json"),
            &json!({"status":"complete","completed_at":chrono::Utc::now()}),
        )?;
    }
    tracing::info!(
        seconds = began.elapsed().as_secs_f64(),
        stage,
        "Money-flow research completed"
    );
    pool.close().await;
    Ok(())
}

fn atomic_json(path: &Path, value: &serde_json::Value) -> Result<(), Error> {
    let temporary = path.with_extension("json.tmp");
    std::fs::write(&temporary, serde_json::to_vec_pretty(value)?)?;
    std::fs::rename(temporary, path)?;
    Ok(())
}

fn freeze_protocol(
    output: &Path,
    entry_price: a_execution_price::EntryPriceModel,
) -> Result<(), Error> {
    std::fs::create_dir_all(output)?;
    let mut protocol = json!({
        "research":"2024", "validation":"2025", "final_review":"2026-01-01..2026-09-30",
        "warmup_start":"2023-10-01", "primary_horizon":5, "diagnostic_horizons":[1,5,10,20],
        "entry":"observation date + 2 SSE trading days, open",
        "exit":"entry + horizon SSE trading days, open", "costs":0.0045,
        "eligibility":"SH/SZ, 60 prior daily observations, positive traded price; no LHB/margin gate",
        "capacity":"report >=5m CNY trailing20 median amount separately; no market-cap floor",
        "missing":"NA, never interpreted as zero; record coverage and executable-label coverage",
        "metrics":"daily rank IC; quintile returns; industry-relative returns; monthly block CI",
        "limitations":["research event returns, not a portfolio backtest", "no historical ST screen",
            "fixed proportional costs, no market impact", "missing/delisted coverage may bias results",
            "2026 has already been viewed in earlier strategy research; not pristine out-of-sample"]
    });
    if entry_price.uses_full_day_range() {
        protocol["entry"] = json!(entry_price.description());
        protocol["entry_price_model"] = json!(entry_price.as_str());
        protocol["limitations"].as_array_mut().unwrap().push(json!("Entry uses ex-post full-day OHLC range, not an observable intraday signal or guaranteed fill; this is a daily price-assumption scenario, not a minute-level backtest"));
    }
    let path = output.join("protocol.json");
    if path.exists() {
        let existing: serde_json::Value = serde_json::from_slice(&std::fs::read(&path)?)?;
        if existing != protocol {
            return Err("Protocol changed; use a new output directory".into());
        }
    } else {
        std::fs::write(&path, serde_json::to_vec_pretty(&protocol)?)?;
    }
    Ok(())
}

#[cfg(test)]
mod protocol_tests {
    use super::*;
    use a_execution_price::EntryPriceModel;
    #[test]
    fn changed_entry_assumption_cannot_overwrite_existing_protocol() {
        let output = std::env::temp_dir().join(format!(
            "quant-price-protocol-{}-{}",
            std::process::id(),
            chrono::Utc::now().timestamp_nanos_opt().unwrap()
        ));
        freeze_protocol(&output, EntryPriceModel::Open).unwrap();
        let original = std::fs::read(output.join("protocol.json")).unwrap();
        assert!(freeze_protocol(&output, EntryPriceModel::DailyRangeTwoThirds).is_err());
        assert_eq!(
            std::fs::read(output.join("protocol.json")).unwrap(),
            original
        );
        std::fs::remove_dir_all(output).unwrap();
    }
}
