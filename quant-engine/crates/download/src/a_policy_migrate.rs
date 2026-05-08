//! One-time migration: `policy_article` / `policy_analysis` / `scrape_log`
//! from legacy MySQL → PostgreSQL.
//!
//! Preserves original integer ids (so `policy_analysis.article_id` references
//! stay valid). Resets PG sequences after to next-available value.
//!
//! After full export/import is verified, the legacy MySQL DB can be retired.

use chrono::{NaiveDate, NaiveDateTime};
use sqlx::{MySqlPool, PgPool, QueryBuilder, Postgres};
use sqlx::FromRow;
use tracing::{info, warn};

#[derive(Debug, Default, Clone)]
pub struct PolicyMigrationStats {
    pub articles_read: usize,
    pub articles_inserted: usize,
    pub articles_skipped: usize,
    pub analyses_read: usize,
    pub analyses_inserted: usize,
    pub analyses_skipped: usize,
    pub scrape_logs_read: usize,
    pub scrape_logs_inserted: usize,
}

#[derive(Debug, FromRow)]
struct ArticleRow {
    id: i32,
    source: String,
    tier: i32,
    title: String,
    url: String,
    publish_date: NaiveDate,
    category: Option<String>,
    summary: Option<String>,
    content: Option<String>,
    content_hash: Option<String>,
    scraped_at: Option<NaiveDateTime>,
}

#[derive(Debug, FromRow)]
struct AnalysisRow {
    id: i32,
    article_id: i32,
    analysis_type: String,
    industries: Option<String>,
    sentiment: Option<f64>,
    intensity: Option<f64>,
    impact_type: Option<String>,
    keywords_hit: Option<String>,
    summary_text: Option<String>,
    affected_stocks: Option<String>,
    analyzed_at: Option<NaiveDateTime>,
}

#[derive(Debug, FromRow)]
struct ScrapeLogRow {
    id: i32,
    source: String,
    started_at: Option<NaiveDateTime>,
    finished_at: Option<NaiveDateTime>,
    articles_found: Option<i32>,
    articles_new: Option<i32>,
    status: Option<String>,
    error_message: Option<String>,
}

/// Build a `mysql://...` URL from individual params.
pub fn mysql_url(host: &str, port: u16, user: &str, password: &str, database: &str) -> String {
    format!("mysql://{user}:{password}@{host}:{port}/{database}")
}

pub async fn migrate(
    mysql_url: &str,
    pg_pool: &PgPool,
    batch_size: usize,
    dry_run: bool,
) -> Result<PolicyMigrationStats, sqlx::Error> {
    info!("Connecting to MySQL...");
    let mysql_pool = MySqlPool::connect(mysql_url).await?;

    let mut stats = PolicyMigrationStats::default();

    // ── Stage 1: policy_article ──
    stats.articles_read = sqlx::query_scalar::<_, i64>("SELECT COUNT(*) FROM policy_article")
        .fetch_one(&mysql_pool).await? as usize;
    info!("MySQL policy_article rows: {}", stats.articles_read);

    let mut max_article_id = 0i32;
    let mut offset: i64 = 0;
    while (offset as usize) < stats.articles_read {
        let rows: Vec<ArticleRow> = sqlx::query_as(
            "SELECT id, source, tier, title, url, publish_date, category, summary, \
             content, content_hash, scraped_at \
             FROM policy_article ORDER BY id LIMIT ? OFFSET ?"
        )
        .bind(batch_size as i64).bind(offset)
        .fetch_all(&mysql_pool).await?;
        if rows.is_empty() { break; }

        for r in &rows { if r.id > max_article_id { max_article_id = r.id; } }
        if !dry_run {
            // Batch INSERT (single round-trip per chunk): ~100x faster than per-row.
            // updated_at 由 DB trigger 维护，应用层一律不写（即使是 migration 工具）
            let mut qb: QueryBuilder<Postgres> = QueryBuilder::new(
                "INSERT INTO policy_article \
                 (id, source, tier, title, url, publish_date, category, summary, \
                  content, content_hash, scraped_at) "
            );
            qb.push_values(&rows, |mut b, r| {
                b.push_bind(r.id)
                 .push_bind(&r.source)
                 .push_bind(r.tier)
                 .push_bind(&r.title)
                 .push_bind(&r.url)
                 .push_bind(r.publish_date)
                 .push_bind(&r.category)
                 .push_bind(&r.summary)
                 .push_bind(&r.content)
                 .push_bind(&r.content_hash)
                 .push_bind(r.scraped_at);
            });
            qb.push(" ON CONFLICT (id) DO NOTHING");
            let n = qb.build().execute(pg_pool).await?.rows_affected() as usize;
            stats.articles_inserted += n;
            stats.articles_skipped += rows.len() - n;
        }

        offset += rows.len() as i64;
        if offset % 5000 == 0 {
            info!("policy_article progress: {}/{}", offset, stats.articles_read);
        }
    }
    info!("policy_article: {} inserted, {} skipped (max id={})",
          stats.articles_inserted, stats.articles_skipped, max_article_id);

    // ── Stage 2: policy_analysis ──
    stats.analyses_read = sqlx::query_scalar::<_, i64>("SELECT COUNT(*) FROM policy_analysis")
        .fetch_one(&mysql_pool).await? as usize;
    info!("MySQL policy_analysis rows: {}", stats.analyses_read);

    let mut max_analysis_id = 0i32;
    offset = 0;
    while (offset as usize) < stats.analyses_read {
        let rows: Vec<AnalysisRow> = sqlx::query_as(
            "SELECT id, article_id, analysis_type, industries, sentiment, intensity, \
             impact_type, keywords_hit, summary_text, affected_stocks, analyzed_at \
             FROM policy_analysis ORDER BY id LIMIT ? OFFSET ?"
        )
        .bind(batch_size as i64).bind(offset)
        .fetch_all(&mysql_pool).await?;
        if rows.is_empty() { break; }

        for r in &rows { if r.id > max_analysis_id { max_analysis_id = r.id; } }
        if !dry_run {
            let mut qb: QueryBuilder<Postgres> = QueryBuilder::new(
                "INSERT INTO policy_analysis \
                 (id, article_id, analysis_type, industries, sentiment, intensity, \
                  impact_type, keywords_hit, summary_text, affected_stocks, analyzed_at) "
            );
            qb.push_values(&rows, |mut b, r| {
                b.push_bind(r.id)
                 .push_bind(r.article_id)
                 .push_bind(&r.analysis_type)
                 .push_bind(&r.industries)
                 .push_bind(r.sentiment)
                 .push_bind(r.intensity)
                 .push_bind(&r.impact_type)
                 .push_bind(&r.keywords_hit)
                 .push_bind(&r.summary_text)
                 .push_bind(&r.affected_stocks)
                 .push_bind(r.analyzed_at);
            });
            qb.push(" ON CONFLICT (article_id, analysis_type) DO NOTHING");
            let n = qb.build().execute(pg_pool).await?.rows_affected() as usize;
            stats.analyses_inserted += n;
            stats.analyses_skipped += rows.len() - n;
        }

        offset += rows.len() as i64;
        if offset % 5000 == 0 {
            info!("policy_analysis progress: {}/{}", offset, stats.analyses_read);
        }
    }
    info!("policy_analysis: {} inserted, {} skipped (max id={})",
          stats.analyses_inserted, stats.analyses_skipped, max_analysis_id);

    // ── Stage 3: scrape_log (best-effort, non-blocking) ──
    let log_count_res: Result<i64, _> = sqlx::query_scalar("SELECT COUNT(*) FROM scrape_log")
        .fetch_one(&mysql_pool).await;
    if let Ok(cnt) = log_count_res {
        stats.scrape_logs_read = cnt as usize;
        let mut max_log_id = 0i32;
        offset = 0;
        while (offset as usize) < stats.scrape_logs_read {
            let rows: Vec<ScrapeLogRow> = sqlx::query_as(
                "SELECT id, source, started_at, finished_at, articles_found, articles_new, \
                 status, error_message \
                 FROM scrape_log ORDER BY id LIMIT ? OFFSET ?"
            )
            .bind(batch_size as i64).bind(offset)
            .fetch_all(&mysql_pool).await?;
            if rows.is_empty() { break; }
            for r in &rows { if r.id > max_log_id { max_log_id = r.id; } }
            if !dry_run {
                let mut qb: QueryBuilder<Postgres> = QueryBuilder::new(
                    "INSERT INTO scrape_log \
                     (id, source, started_at, finished_at, articles_found, articles_new, status, error_message) "
                );
                qb.push_values(&rows, |mut b, r| {
                    b.push_bind(r.id)
                     .push_bind(&r.source)
                     .push_bind(r.started_at)
                     .push_bind(r.finished_at)
                     .push_bind(r.articles_found.unwrap_or(0))
                     .push_bind(r.articles_new.unwrap_or(0))
                     .push_bind(r.status.clone().unwrap_or_else(|| "running".to_string()))
                     .push_bind(&r.error_message);
                });
                qb.push(" ON CONFLICT (id) DO NOTHING");
                let n = qb.build().execute(pg_pool).await?.rows_affected() as usize;
                stats.scrape_logs_inserted += n;
            }
            offset += rows.len() as i64;
        }
        info!("scrape_log: {} inserted (max id={})", stats.scrape_logs_inserted, max_log_id);
    } else {
        warn!("scrape_log skipped (table may not exist in MySQL)");
    }

    // ── Stage 4: reset PG sequences past max(id) ──
    if !dry_run {
        for (table, max_id) in [
            ("policy_article", max_article_id),
            ("policy_analysis", max_analysis_id),
        ] {
            if max_id > 0 {
                let next = max_id as i64 + 1;
                let sql = format!(
                    "SELECT setval(pg_get_serial_sequence('{table}', 'id'), {next}, false)"
                );
                let _ = sqlx::query(&sql).execute(pg_pool).await?;
                info!("{table} sequence reset to {next}");
            }
        }
    }

    mysql_pool.close().await;
    Ok(stats)
}
