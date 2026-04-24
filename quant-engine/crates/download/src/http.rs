//! HTTP client with rate limiting and retry logic.

use std::sync::Arc;
use std::time::Duration;

use reqwest::Client;
use serde_json::Value;
use tokio::sync::Semaphore;
use tokio::time::sleep;
use tracing::warn;

/// Rate-limited HTTP client for financial data APIs.
pub struct ApiClient {
    client: Client,
    /// Semaphore to limit concurrent requests.
    semaphore: Arc<Semaphore>,
    /// Minimum interval between requests (milliseconds).
    interval_ms: u64,
    /// Last request timestamp tracking (per-source).
    last_request: Arc<tokio::sync::Mutex<std::time::Instant>>,
}

impl ApiClient {
    /// Create a new rate-limited client.
    ///
    /// `calls_per_minute`: max requests per minute (converted to interval).
    /// `max_concurrent`: max simultaneous in-flight requests.
    pub fn new(calls_per_minute: u32, max_concurrent: usize) -> Self {
        let interval_ms = if calls_per_minute > 0 {
            60_000 / calls_per_minute as u64
        } else {
            0
        };
        Self {
            client: Client::builder()
                .timeout(Duration::from_secs(60))
                .build()
                .expect("Failed to build HTTP client"),
            semaphore: Arc::new(Semaphore::new(max_concurrent)),
            interval_ms,
            last_request: Arc::new(tokio::sync::Mutex::new(std::time::Instant::now())),
        }
    }

    /// GET JSON with rate limiting and retry (429 / 5xx).
    pub async fn get_json(&self, url: &str) -> Result<Value, String> {
        let _permit = self.semaphore.acquire().await.map_err(|e| e.to_string())?;

        // Rate limit: wait if too soon since last request
        {
            let mut last = self.last_request.lock().await;
            let elapsed = last.elapsed().as_millis() as u64;
            if elapsed < self.interval_ms {
                sleep(Duration::from_millis(self.interval_ms - elapsed)).await;
            }
            *last = std::time::Instant::now();
        }

        let backoff_waits = [5, 10, 20, 30, 60];
        let max_retries = 5;

        for attempt in 0..max_retries {
            let resp = match self.client.get(url).send().await {
                Ok(r) => r,
                Err(e) => {
                    warn!("HTTP error (attempt {}/{}): {e}", attempt + 1, max_retries);
                    if attempt + 1 == max_retries {
                        return Err(format!("HTTP failed after {max_retries} retries: {e}"));
                    }
                    sleep(Duration::from_secs(2u64.pow(attempt as u32))).await;
                    continue;
                }
            };

            let status = resp.status().as_u16();
            if status == 429 {
                let wait = backoff_waits[attempt.min(backoff_waits.len() - 1)];
                warn!("Rate limited (429), waiting {wait}s (attempt {}/{})", attempt + 1, max_retries);
                sleep(Duration::from_secs(wait)).await;
                continue;
            }
            if status >= 500 {
                warn!("Server error ({status}), retrying...");
                sleep(Duration::from_secs(2u64.pow(attempt as u32))).await;
                continue;
            }
            if status != 200 {
                let body = resp.text().await.unwrap_or_default();
                return Err(format!("HTTP {status}: {body}"));
            }

            let body = resp.json::<Value>().await.map_err(|e| format!("JSON parse error: {e}"))?;
            return Ok(body);
        }

        Err("Max retries exceeded".to_string())
    }

    /// Build a FMP API URL.
    pub fn fmp_url(path: &str, api_key: &str, params: &[(&str, &str)]) -> String {
        let mut url = format!("https://financialmodelingprep.com/api/stable/{path}?apikey={api_key}");
        for (k, v) in params {
            url.push('&');
            url.push_str(k);
            url.push('=');
            url.push_str(v);
        }
        url
    }

    /// POST JSON with rate limiting and retry.
    pub async fn post_json(&self, url: &str, body: &serde_json::Value) -> Result<serde_json::Value, String> {
        let _permit = self.semaphore.acquire().await.map_err(|e| e.to_string())?;

        {
            let mut last = self.last_request.lock().await;
            let elapsed = last.elapsed().as_millis() as u64;
            if elapsed < self.interval_ms {
                sleep(Duration::from_millis(self.interval_ms - elapsed)).await;
            }
            *last = std::time::Instant::now();
        }

        let backoff_waits = [5, 10, 20, 30, 60];
        let max_retries = 5;

        for attempt in 0..max_retries {
            let resp = match self.client.post(url).json(body).send().await {
                Ok(r) => r,
                Err(e) => {
                    warn!("HTTP POST error (attempt {}/{}): {e}", attempt + 1, max_retries);
                    if attempt + 1 == max_retries {
                        return Err(format!("POST failed after {max_retries} retries: {e}"));
                    }
                    sleep(Duration::from_secs(2u64.pow(attempt as u32))).await;
                    continue;
                }
            };

            let status = resp.status().as_u16();
            if status == 429 {
                let wait = backoff_waits[attempt.min(backoff_waits.len() - 1)];
                warn!("Rate limited (429), waiting {wait}s");
                sleep(Duration::from_secs(wait)).await;
                continue;
            }
            if status >= 500 {
                warn!("Server error ({status}), retrying...");
                sleep(Duration::from_secs(2u64.pow(attempt as u32))).await;
                continue;
            }
            if status != 200 {
                let body_text = resp.text().await.unwrap_or_default();
                return Err(format!("HTTP {status}: {body_text}"));
            }

            return resp.json::<serde_json::Value>().await.map_err(|e| format!("JSON parse: {e}"));
        }

        Err("Max retries exceeded".to_string())
    }

    /// Build a FMP v3 API URL.
    pub fn fmp_url_v3(path: &str, api_key: &str, params: &[(&str, &str)]) -> String {
        let mut url = format!("https://financialmodelingprep.com/api/v3/{path}?apikey={api_key}");
        for (k, v) in params {
            url.push('&');
            url.push_str(k);
            url.push('=');
            url.push_str(v);
        }
        url
    }
}

impl Clone for ApiClient {
    fn clone(&self) -> Self {
        Self {
            client: self.client.clone(),
            semaphore: self.semaphore.clone(),
            interval_ms: self.interval_ms,
            last_request: self.last_request.clone(),
        }
    }
}
