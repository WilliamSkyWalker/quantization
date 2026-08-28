//! MySQL connection pool setup.

use std::time::Duration;

use sqlx::mysql::{MySqlConnectOptions, MySqlPoolOptions};
use sqlx::ConnectOptions;
use sqlx::MySqlPool;
use tracing::info;
use tracing::log::LevelFilter;

/// Create a MySqlPool from a database URL string.
pub async fn create_pool(url: &str, _schema: &str, max_connections: u32) -> Result<MySqlPool, sqlx::Error> {
    let mut options: MySqlConnectOptions = url.parse::<MySqlConnectOptions>()?
        .log_slow_statements(LevelFilter::Warn, Duration::from_secs(2));

    // Disable SSL for local connections and set connect timeout
    options = options.ssl_mode(sqlx::mysql::MySqlSslMode::Disabled);

    let pool = MySqlPoolOptions::new()
        .max_connections(max_connections)
        .acquire_timeout(Duration::from_secs(10))
        .connect_with(options)
        .await?;

    info!("MySQL pool created: max_connections={max_connections}");
    Ok(pool)
}

/// Create pool from individual config values.
pub async fn create_pool_from_config(
    host: &str,
    port: u16,
    user: &str,
    password: &str,
    database: &str,
    _schema: &str,
    max_connections: u32,
) -> Result<MySqlPool, sqlx::Error> {
    let url = format!("mysql://{user}:{password}@{host}:{port}/{database}");
    create_pool(&url, _schema, max_connections).await
}
