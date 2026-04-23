//! PostgreSQL connection pool setup.

use sqlx::postgres::{PgConnectOptions, PgPoolOptions};
use sqlx::PgPool;
use tracing::info;

/// Create a PgPool from a database URL string.
///
/// Sets `search_path` to the configured schema on each connection.
pub async fn create_pool(url: &str, schema: &str, max_connections: u32) -> Result<PgPool, sqlx::Error> {
    let options: PgConnectOptions = url.parse::<PgConnectOptions>()?
        .options([("search_path", schema)]);

    let schema_owned = schema.to_string();
    let pool = PgPoolOptions::new()
        .max_connections(max_connections)
        .after_connect(move |conn, _meta| {
            let schema = schema_owned.clone();
            Box::pin(async move {
                sqlx::query(&format!("SET search_path TO {schema}, public"))
                    .execute(&mut *conn)
                    .await?;
                Ok(())
            })
        })
        .connect_with(options)
        .await?;

    info!("PostgreSQL pool created: max_connections={max_connections}, schema={schema}");
    Ok(pool)
}

/// Create pool from individual config values.
pub async fn create_pool_from_config(
    host: &str,
    port: u16,
    user: &str,
    password: &str,
    database: &str,
    schema: &str,
    max_connections: u32,
) -> Result<PgPool, sqlx::Error> {
    let url = format!("postgres://{user}:{password}@{host}:{port}/{database}");
    create_pool(&url, schema, max_connections).await
}
