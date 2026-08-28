# Project Memory

## Infrastructure
- **MySQL runs in Docker** (not system service). Start with `docker start <container>` not `sudo service mysql start`
- Database: MySQL, accessed via sqlx in Rust (not PostgreSQL as CLAUDE.md says — migrated to MySQL)
- Config: `quant-engine/config.toml` + env vars from `env.json`

## Project Structure
- **Production**: `quant-engine/` (Rust workspace, 9 crates)
- **Legacy**: `legacy_python/` (archived 2026-04-30, not maintained)
- **Documentation**: `doc/` (A_SHARE_STRATEGY.md, US_SHARE_STRATEGY.md, DATA_SOURCES.md)
- **Factor analysis output**: `output/factor_analysis/`
- **Data cache**: `cache/` (parquet for US, MySQL-only for A-share)

## Critical Rules (from CLAUDE.md)
1. **先读后写** — Read code before writing. Search all callers before modifying functions.
2. **不猜测** — Don't assume types/APIs/columns. Check definitions, docs, or run API.
3. **严禁凭记忆写 API** — Must check docs or run API. Tushare/AkShare docs may be outdated.
4. **每次修改后验证** — `cargo build --release` + smoke test + `SELECT COUNT(*)` to confirm DB.
5. **禁止静默失败** — Every `return/continue/break` must have logger.
6. **立即更新文档** — Update CLAUDE.md/README/doc/*.md after code changes.

## A-Share System
- **39 factors** across 7 categories (as of 2026-08-28)
- Cache built from MySQL on each run (no parquet for A-share)
- Universe filter requires trading data on the specific date
- Latest trading data date changes — always check before running score/backtest
- Score command implemented: `cmd_a_score` in `crates/cli/src/main.rs`

## Key Commands
```bash
# Stock selection (A-share)
cargo run --release -- --market cn score --date YYYY-MM-DD --top 30

# Factor analysis (A-share)
cargo run --release -- --market cn analyze --start 2021-01-01 --end YYYY-MM-DD

# Backtest (A-share)
cargo run --release -- --market cn backtest --start YYYY-MM-DD --end YYYY-MM-DD

# Download data
cargo run --release -- --market cn download --source tushare --target all

# Build
cd quant-engine && cargo build --release -p quant-cli
```

## Recent Work (2026-08-28)
- Added 10 new factors: PIOTROSKI_F, FREE_FLOAT_PCT, AMIHUD_ILLIQ, ACCRUALS, BAB_BETA, REVENUE_ACCELERATION, GROSS_MARGIN_CHG, RSI_14, MAX_RET, PRICE_52W_HIGH
- Implemented `score` command for A-share (cmd_a_score in main.rs)
- Added `detect_a_regime_public` wrapper in a_strategy.rs
- Factor analysis results: 4 factors significant at |t|>3.0 (REV_5D, VOL_PRICE_DIV, TURN_20D, SIZE), all price-based, no look-ahead bias
- Backtest improved: -28.40% → +13.41% total return
- Updated doc/A_SHARE_STRATEGY.md with new factors and analysis results

## File Locations
- CLI entry: `quant-engine/crates/cli/src/main.rs`
- A-share factors: `quant-engine/crates/factors/src/a_share/factors.rs`
- A-share strategy: `quant-engine/crates/strategy/src/a_strategy.rs`
- A-share analysis: `quant-engine/crates/strategy/src/analysis.rs`
- Config: `quant-engine/config.toml`
