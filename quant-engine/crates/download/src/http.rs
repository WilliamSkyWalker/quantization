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
        Self::with_dns_override(calls_per_minute, max_concurrent, None)
    }

    pub fn with_dns_override(
        calls_per_minute: u32,
        max_concurrent: usize,
        dns_override: Option<(&str, std::net::IpAddr)>,
    ) -> Self {
        let interval_ms = if calls_per_minute > 0 {
            60_000u64.div_ceil(calls_per_minute as u64)
        } else {
            0
        };
        let mut builder = Client::builder().timeout(Duration::from_secs(60));
        if let Some((host, ip)) = dns_override {
            // Override only DNS; retain the original hostname for TLS and HTTP.
            builder = builder.resolve(host, std::net::SocketAddr::new(ip, 0));
        }
        Self {
            client: builder.build().expect("Failed to build HTTP client"),
            semaphore: Arc::new(Semaphore::new(max_concurrent)),
            interval_ms,
            last_request: Arc::new(tokio::sync::Mutex::new(std::time::Instant::now())),
        }
    }

    /// Shared by clones. Every network attempt, including retries, uses this gate.
    async fn wait_for_slot(&self) {
        loop {
            let wait = {
                let mut last = self.last_request.lock().await;
                let now = std::time::Instant::now();
                let next = *last + Duration::from_millis(self.interval_ms);
                if now >= next {
                    *last = now;
                    return;
                }
                next.duration_since(now)
            };
            // Do not hold the mutex while sleeping: throttle responses must be
            // able to move the shared deadline while requests are queued.
            sleep(wait).await;
        }
    }

    /// Delay all clones after a provider throttle; queued requests re-check the gate.
    pub async fn cooldown(&self, duration: Duration) {
        let mut last = self.last_request.lock().await;
        *last = (*last).max(std::time::Instant::now() + duration);
    }

    /// GET JSON with rate limiting and retry (429 / 5xx).
    pub async fn get_json(&self, url: &str) -> Result<Value, String> {
        let _permit = self.semaphore.acquire().await.map_err(|e| e.to_string())?;

        let backoff_waits = [5, 10, 20, 30, 60];
        let max_retries = 5;

        for attempt in 0..max_retries {
            self.wait_for_slot().await;
            let resp = match self.client.get(url).send().await {
                Ok(r) => r,
                Err(e) => {
                    let detail = request_error_detail(e);
                    warn!("HTTP error (attempt {}/{}): {detail}", attempt + 1, max_retries);
                    if attempt + 1 == max_retries {
                        return Err(format!("HTTP failed after {max_retries} attempts: {detail}"));
                    }
                    sleep(Duration::from_secs(2u64.pow(attempt as u32))).await;
                    continue;
                }
            };

            let status = resp.status().as_u16();
            if status == 429 {
                let wait = backoff_waits[attempt.min(backoff_waits.len() - 1)];
                warn!("Rate limited (429), waiting {wait}s (attempt {}/{})", attempt + 1, max_retries);
                self.cooldown(Duration::from_secs(wait)).await;
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

    /// Build a FMP stable URL: https://financialmodelingprep.com/stable/{endpoint}
    /// (Python: _fmp_get_stable)
    pub fn fmp_url(path: &str, api_key: &str, params: &[(&str, &str)]) -> String {
        let mut url = format!("https://financialmodelingprep.com/stable/{path}?apikey={api_key}");
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

        let backoff_waits = [5, 10, 20, 30, 60];
        let max_retries = 5;

        for attempt in 0..max_retries {
            self.wait_for_slot().await;
            let resp = match self.client.post(url).json(body).send().await {
                Ok(r) => r,
                Err(e) => {
                    let detail = request_error_detail(e);
                    warn!("HTTP POST error (attempt {}/{}): {detail}", attempt + 1, max_retries);
                    if attempt + 1 == max_retries {
                        return Err(format!("POST failed after {max_retries} attempts: {detail}"));
                    }
                    sleep(Duration::from_secs(2u64.pow(attempt as u32))).await;
                    continue;
                }
            };

            let status = resp.status().as_u16();
            if status == 429 {
                let wait = backoff_waits[attempt.min(backoff_waits.len() - 1)];
                warn!("Rate limited (429), waiting {wait}s");
                self.cooldown(Duration::from_secs(wait)).await;
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

    /// Build a FMP versioned URL: https://financialmodelingprep.com/api/{version}/{path}
    /// (Python: _fmp_get_json with version param)
    pub fn fmp_url_v3(path: &str, api_key: &str, params: &[(&str, &str)]) -> String {
        Self::fmp_url_versioned(path, api_key, "v3", params)
    }

    pub fn fmp_url_v4(path: &str, api_key: &str, params: &[(&str, &str)]) -> String {
        Self::fmp_url_versioned(path, api_key, "v4", params)
    }

    fn fmp_url_versioned(path: &str, api_key: &str, version: &str, params: &[(&str, &str)]) -> String {
        let mut url = format!("https://financialmodelingprep.com/api/{version}/{path}?apikey={api_key}");
        for (k, v) in params {
            url.push('&');
            url.push_str(k);
            url.push('=');
            url.push_str(v);
        }
        url
    }
}

fn request_error_detail(error: reqwest::Error) -> String {
    // URLs may carry API keys. Debug includes the source chain without the URL.
    let timeout = error.is_timeout();
    let connect = error.is_connect();
    format!("timeout={timeout}, connect={connect}, {:?}", error.without_url())
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

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn request_spacing_rounds_up_to_respect_provider_quota() {
        assert_eq!(ApiClient::new(119, 4).interval_ms, 505);
    }

    #[tokio::test]
    async fn cloned_clients_share_spacing_and_cooldown() {
        let client = ApiClient::new(3000, 2); // 20ms per request
        let other = client.clone();
        client.wait_for_slot().await;
        let start = std::time::Instant::now();
        other.wait_for_slot().await;
        assert!(start.elapsed() >= Duration::from_millis(20));
        client.cooldown(Duration::from_millis(100)).await;
        let start = std::time::Instant::now();
        other.wait_for_slot().await;
        assert!(start.elapsed() >= Duration::from_millis(100));
    }
}
