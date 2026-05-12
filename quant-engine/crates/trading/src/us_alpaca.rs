//! Alpaca Markets REST 客户端 — 美股 paper / live trading.
//!
//! 与 A 股 PaperBroker 的区别：
//! - A 股：本地 PG `a_paper_*` 表存仓位/交易/NAV，自己模拟执行
//! - 美股：状态全部委托 Alpaca cloud（账户/仓位/订单），本地只发指令
//!
//! 用法：
//! ```no_run
//! use quant_trading::us_alpaca::AlpacaClient;
//! # async fn run() -> anyhow::Result<()> {
//! let client = AlpacaClient::from_env()?;
//! let acct = client.account().await?;
//! println!("cash={}, equity={}", acct.cash, acct.equity);
//! # Ok(()) }
//! ```

use std::collections::HashMap;

use chrono::{DateTime, Utc};
use rustc_hash::FxHashMap;
use serde::{Deserialize, Serialize};
use thiserror::Error;
use tracing::{info, warn};

const PAPER_BASE: &str = "https://paper-api.alpaca.markets";
const LIVE_BASE: &str = "https://api.alpaca.markets";
const DATA_BASE: &str = "https://data.alpaca.markets";

#[derive(Debug, Error)]
pub enum AlpacaError {
    #[error("env var {0} not set")]
    MissingEnv(&'static str),
    #[error("HTTP error: {0}")]
    Http(#[from] reqwest::Error),
    #[error("Alpaca API error {status}: {body}")]
    Api { status: u16, body: String },
    #[error("parse error: {0}")]
    Parse(String),
}

/// 账户快照（GET /v2/account）
#[derive(Debug, Clone, Deserialize)]
pub struct Account {
    pub id: String,
    pub status: String,
    #[serde(deserialize_with = "deserialize_str_f64")]
    pub cash: f64,
    #[serde(deserialize_with = "deserialize_str_f64")]
    pub equity: f64,
    #[serde(deserialize_with = "deserialize_str_f64")]
    pub buying_power: f64,
    #[serde(deserialize_with = "deserialize_str_f64")]
    pub portfolio_value: f64,
    #[serde(deserialize_with = "deserialize_str_f64", default)]
    pub long_market_value: f64,
    #[serde(deserialize_with = "deserialize_str_f64", default)]
    pub short_market_value: f64,
    pub pattern_day_trader: bool,
}

/// 持仓（GET /v2/positions）
#[derive(Debug, Clone, Deserialize)]
pub struct Position {
    pub symbol: String,
    pub asset_id: String,
    /// 多头正、空头负（单位：股）
    #[serde(deserialize_with = "deserialize_str_f64")]
    pub qty: f64,
    #[serde(deserialize_with = "deserialize_str_f64")]
    pub avg_entry_price: f64,
    #[serde(deserialize_with = "deserialize_str_f64")]
    pub market_value: f64,
    #[serde(deserialize_with = "deserialize_str_f64")]
    pub current_price: f64,
    #[serde(deserialize_with = "deserialize_str_f64")]
    pub unrealized_pl: f64,
    /// "long" or "short"
    pub side: String,
}

/// 提交订单的 payload（POST /v2/orders）
#[derive(Debug, Serialize)]
pub struct OrderRequest {
    pub symbol: String,
    pub qty: String,           // Alpaca 接受字符串
    pub side: String,          // "buy" | "sell"
    #[serde(rename = "type")]
    pub order_type: String,    // "market" | "limit"
    pub time_in_force: String, // "day" | "gtc" | "ioc" | "fok"
}

/// GET /v2/account/portfolio/history response
#[derive(Debug, Clone, Deserialize)]
pub struct PortfolioHistory {
    /// Unix epoch seconds for each sample
    pub timestamp: Vec<i64>,
    pub equity: Vec<f64>,
    pub profit_loss: Vec<f64>,
    pub profit_loss_pct: Vec<f64>,
    pub base_value: f64,
    pub timeframe: String,
}

/// 单根 K 线（data.alpaca.markets bars）
#[derive(Debug, Clone, Deserialize)]
pub struct Bar {
    /// RFC3339 timestamp，例如 "2026-05-12T00:00:00Z"
    pub t: String,
    pub o: f64,
    pub h: f64,
    pub l: f64,
    pub c: f64,
    pub v: f64,
}

#[derive(Debug, Clone, Deserialize)]
pub struct Order {
    pub id: String,
    pub symbol: String,
    pub qty: String,
    pub side: String,
    pub status: String,
    pub created_at: String,
}

/// 账户简要 + 当前仓位 — Rebalance 计算需要的状态
#[derive(Debug, Clone)]
pub struct PortfolioState {
    pub account: Account,
    pub positions: FxHashMap<String, Position>,
}

/// Diff 计算结果：要从当前仓位变到 target，每只票应该买/卖多少股
#[derive(Debug, Clone)]
pub struct RebalancePlan {
    /// symbol → (current_shares, target_shares, delta_shares)
    pub orders: Vec<RebalanceOrder>,
    pub equity: f64,
    pub cash: f64,
}

#[derive(Debug, Clone)]
pub struct RebalanceOrder {
    pub symbol: String,
    pub current_shares: f64,
    pub target_shares: f64,
    pub delta_shares: f64,
    pub current_price: f64,
    pub target_dollar: f64,
    pub side: &'static str, // "buy" / "sell"
}

pub struct AlpacaClient {
    base: String,
    api_key: String,
    secret_key: String,
    http: reqwest::Client,
}

impl AlpacaClient {
    /// 从环境变量构造：ALPACA_API_KEY / ALPACA_SECRET_KEY / ALPACA_PAPER (default true)
    pub fn from_env() -> Result<Self, AlpacaError> {
        let api_key = std::env::var("ALPACA_API_KEY")
            .map_err(|_| AlpacaError::MissingEnv("ALPACA_API_KEY"))?;
        let secret_key = std::env::var("ALPACA_SECRET_KEY")
            .map_err(|_| AlpacaError::MissingEnv("ALPACA_SECRET_KEY"))?;
        let paper = std::env::var("ALPACA_PAPER")
            .map(|v| !matches!(v.as_str(), "0" | "false" | "FALSE" | "no"))
            .unwrap_or(true);
        let base = if paper { PAPER_BASE } else { LIVE_BASE }.to_string();
        Ok(Self {
            base,
            api_key,
            secret_key,
            http: reqwest::Client::builder()
                .timeout(std::time::Duration::from_secs(30))
                .build()?,
        })
    }

    fn auth_headers(&self) -> Vec<(&'static str, String)> {
        vec![
            ("APCA-API-KEY-ID", self.api_key.clone()),
            ("APCA-API-SECRET-KEY", self.secret_key.clone()),
        ]
    }

    async fn get<T: for<'de> Deserialize<'de>>(&self, path: &str) -> Result<T, AlpacaError> {
        let url = format!("{}{path}", self.base);
        let mut req = self.http.get(&url);
        for (k, v) in self.auth_headers() {
            req = req.header(k, v);
        }
        let resp = req.send().await?;
        let status = resp.status();
        if !status.is_success() {
            let body = resp.text().await.unwrap_or_default();
            return Err(AlpacaError::Api {
                status: status.as_u16(),
                body,
            });
        }
        let json: T = resp.json().await?;
        Ok(json)
    }

    async fn post<B: Serialize, T: for<'de> Deserialize<'de>>(
        &self,
        path: &str,
        body: &B,
    ) -> Result<T, AlpacaError> {
        let url = format!("{}{path}", self.base);
        let mut req = self.http.post(&url).json(body);
        for (k, v) in self.auth_headers() {
            req = req.header(k, v);
        }
        let resp = req.send().await?;
        let status = resp.status();
        if !status.is_success() {
            let body = resp.text().await.unwrap_or_default();
            return Err(AlpacaError::Api {
                status: status.as_u16(),
                body,
            });
        }
        let json: T = resp.json().await?;
        Ok(json)
    }

    /// GET /v2/account
    pub async fn account(&self) -> Result<Account, AlpacaError> {
        self.get("/v2/account").await
    }

    /// GET /v2/positions
    pub async fn positions(&self) -> Result<Vec<Position>, AlpacaError> {
        self.get("/v2/positions").await
    }

    /// GET /v2/positions/{symbol} — 单只仓位（404 时返回 None）
    pub async fn position(&self, symbol: &str) -> Result<Option<Position>, AlpacaError> {
        let url = format!("{}/v2/positions/{symbol}", self.base);
        let mut req = self.http.get(&url);
        for (k, v) in self.auth_headers() {
            req = req.header(k, v);
        }
        let resp = req.send().await?;
        if resp.status().as_u16() == 404 {
            return Ok(None);
        }
        if !resp.status().is_success() {
            let status = resp.status().as_u16();
            let body = resp.text().await.unwrap_or_default();
            return Err(AlpacaError::Api { status, body });
        }
        Ok(Some(resp.json().await?))
    }

    /// 取最近的成交价（GET /v2/stocks/{symbol}/trades/latest 用 data 子域名）
    pub async fn latest_price(&self, symbol: &str) -> Result<f64, AlpacaError> {
        let url = format!("{DATA_BASE}/v2/stocks/{symbol}/trades/latest");
        let mut req = self.http.get(&url);
        for (k, v) in self.auth_headers() {
            req = req.header(k, v);
        }
        let resp = req.send().await?;
        let status = resp.status();
        if !status.is_success() {
            let body = resp.text().await.unwrap_or_default();
            return Err(AlpacaError::Api {
                status: status.as_u16(),
                body,
            });
        }
        let json: serde_json::Value = resp.json().await?;
        let price = json
            .get("trade")
            .and_then(|t| t.get("p"))
            .and_then(|p| p.as_f64())
            .ok_or_else(|| AlpacaError::Parse("trade.p missing".to_string()))?;
        Ok(price)
    }

    /// GET /v2/account/portfolio/history — 组合 equity 时间序列
    ///
    /// period: "1D" / "1W" / "1M" / "3M" / "1A" / "all"
    /// timeframe: "1Min" / "5Min" / "15Min" / "1H" / "1D"
    pub async fn portfolio_history(
        &self,
        period: &str,
        timeframe: &str,
    ) -> Result<PortfolioHistory, AlpacaError> {
        let path = format!(
            "/v2/account/portfolio/history?period={period}&timeframe={timeframe}"
        );
        self.get(&path).await
    }

    /// GET data.alpaca.markets /v2/stocks/{symbol}/bars — 历史日线
    ///
    /// start/end: ISO date "YYYY-MM-DD"
    /// timeframe: "1Day" / "1Hour" / "1Min" 等
    pub async fn historical_bars(
        &self,
        symbol: &str,
        start: &str,
        end: &str,
        timeframe: &str,
    ) -> Result<Vec<Bar>, AlpacaError> {
        // feed=iex — free / 入门 data plan 不允许查 SIP feed 最近 15 分钟数据。
        // IEX 单交易所覆盖足够做 SPY 之类宽基对比，正式付费用户可加 feed=sip。
        let url = format!(
            "{DATA_BASE}/v2/stocks/{symbol}/bars?timeframe={timeframe}&start={start}&end={end}&adjustment=all&feed=iex&limit=10000"
        );
        let mut req = self.http.get(&url);
        for (k, v) in self.auth_headers() {
            req = req.header(k, v);
        }
        let resp = req.send().await?;
        let status = resp.status();
        if !status.is_success() {
            let body = resp.text().await.unwrap_or_default();
            return Err(AlpacaError::Api {
                status: status.as_u16(),
                body,
            });
        }
        let json: serde_json::Value = resp.json().await?;
        let bars = json
            .get("bars")
            .and_then(|b| b.as_array())
            .cloned()
            .unwrap_or_default();
        let parsed: Vec<Bar> = bars
            .into_iter()
            .filter_map(|v| serde_json::from_value::<Bar>(v).ok())
            .collect();
        Ok(parsed)
    }

    /// 当前组合状态：账户 + 所有持仓
    pub async fn portfolio_state(&self) -> Result<PortfolioState, AlpacaError> {
        let account = self.account().await?;
        let positions_vec = self.positions().await?;
        let positions: FxHashMap<String, Position> = positions_vec
            .into_iter()
            .map(|p| (p.symbol.clone(), p))
            .collect();
        Ok(PortfolioState { account, positions })
    }

    /// GET /v2/orders?status=open — 当前所有未成交挂单（含 accepted/new/pending）
    pub async fn open_orders(&self) -> Result<Vec<Order>, AlpacaError> {
        self.get("/v2/orders?status=open&limit=500").await
    }

    /// POST /v2/orders — market 单
    pub async fn submit_market_order(
        &self,
        symbol: &str,
        shares: i64,
        side_buy: bool,
    ) -> Result<Order, AlpacaError> {
        let body = OrderRequest {
            symbol: symbol.to_string(),
            qty: shares.abs().to_string(),
            side: if side_buy { "buy" } else { "sell" }.to_string(),
            order_type: "market".to_string(),
            time_in_force: "day".to_string(),
        };
        self.post("/v2/orders", &body).await
    }

    /// 计算 rebalance plan（不真正下单）
    ///
    /// Args:
    ///     target_weights: ticker → 权重（正多头/负空头），权重和应 ≤ gross_leverage
    ///     prices: 可选 price override；None 时从 Alpaca latest_price 拉
    ///
    /// 返回每只票的 buy/sell 股数（已经按整股 floor）
    pub async fn plan_rebalance(
        &self,
        target_weights: &HashMap<String, f64>,
        prices_override: Option<&HashMap<String, f64>>,
    ) -> Result<RebalancePlan, AlpacaError> {
        let state = self.portfolio_state().await?;
        let equity = state.account.equity;
        let cash = state.account.cash;

        // 收集所有涉及的 symbol
        let mut all_symbols: std::collections::HashSet<String> = state.positions.keys().cloned().collect();
        all_symbols.extend(target_weights.keys().cloned());

        let mut orders = Vec::new();
        for symbol in all_symbols {
            let current_shares = state
                .positions
                .get(&symbol)
                .map(|p| p.qty)
                .unwrap_or(0.0);
            let target_weight = target_weights.get(&symbol).copied().unwrap_or(0.0);
            let target_dollar = target_weight * equity;

            // 价格：override 优先，否则现仓位的 current_price，再否则 Alpaca latest
            let price = if let Some(pmap) = prices_override {
                if let Some(&p) = pmap.get(&symbol) {
                    p
                } else if let Some(p) = state.positions.get(&symbol).map(|x| x.current_price) {
                    p
                } else {
                    self.latest_price(&symbol).await.unwrap_or(0.0)
                }
            } else if let Some(p) = state.positions.get(&symbol).map(|x| x.current_price) {
                p
            } else {
                self.latest_price(&symbol).await.unwrap_or(0.0)
            };

            if price <= 0.0 {
                warn!("plan_rebalance: skip {symbol} (no price)");
                continue;
            }

            let target_shares = (target_dollar / price).trunc();
            let delta = target_shares - current_shares;
            if delta.abs() < 1.0 {
                continue;
            }
            let side = if delta > 0.0 { "buy" } else { "sell" };
            orders.push(RebalanceOrder {
                symbol,
                current_shares,
                target_shares,
                delta_shares: delta,
                current_price: price,
                target_dollar,
                side,
            });
        }

        // 排序：sells 优先（释放资金），buys 后置（按权重降序）
        orders.sort_by(|a, b| {
            let a_priority = if a.side == "sell" { 0 } else { 1 };
            let b_priority = if b.side == "sell" { 0 } else { 1 };
            a_priority
                .cmp(&b_priority)
                .then_with(|| b.target_dollar.abs().partial_cmp(&a.target_dollar.abs()).unwrap())
        });

        Ok(RebalancePlan {
            orders,
            equity,
            cash,
        })
    }

    /// 执行 plan：依次提交所有订单（market day-order）。
    /// 返回 (已提交订单, 失败订单)。失败 = ticker/side/shares/错误原因，
    /// 调用方需自行决定是否补单/放弃/abort（不在此处静默吞错）。
    pub async fn execute_plan(&self, plan: &RebalancePlan) -> ExecutionReport {
        let mut submitted = Vec::new();
        let mut failed = Vec::new();
        for o in &plan.orders {
            let shares = o.delta_shares.abs() as i64;
            if shares < 1 {
                continue;
            }
            let side_buy = o.side == "buy";
            match self.submit_market_order(&o.symbol, shares, side_buy).await {
                Ok(order) => {
                    info!(
                        "submitted {} {} {} (id={})",
                        o.side, shares, o.symbol, order.id
                    );
                    submitted.push(order);
                }
                Err(e) => {
                    let msg = e.to_string();
                    tracing::error!("submit failed {} {} {}: {msg}", o.side, shares, o.symbol);
                    failed.push(FailedOrder {
                        symbol: o.symbol.clone(),
                        side: o.side.to_string(),
                        shares,
                        error: msg,
                    });
                }
            }
        }
        ExecutionReport { submitted, failed }
    }
}

/// 单笔订单提交失败的明细
#[derive(Debug, Clone)]
pub struct FailedOrder {
    pub symbol: String,
    pub side: String,
    pub shares: i64,
    pub error: String,
}

/// `execute_plan` 的返回值：分开记录已提交 / 失败
#[derive(Debug, Clone)]
pub struct ExecutionReport {
    pub submitted: Vec<Order>,
    pub failed: Vec<FailedOrder>,
}

/// serde 反序列化辅助：Alpaca 返回字符串数字，要转 f64
fn deserialize_str_f64<'de, D>(deserializer: D) -> Result<f64, D::Error>
where
    D: serde::Deserializer<'de>,
{
    use serde::de::Error;
    let s = String::deserialize(deserializer)?;
    s.parse::<f64>().map_err(D::Error::custom)
}

/// 从 signals_*.json 解析 target weights
pub fn load_signals_json(path: &std::path::Path) -> Result<HashMap<String, f64>, AlpacaError> {
    let content = std::fs::read_to_string(path)
        .map_err(|e| AlpacaError::Parse(format!("read {}: {e}", path.display())))?;
    let json: serde_json::Value = serde_json::from_str(&content)
        .map_err(|e| AlpacaError::Parse(format!("parse json: {e}")))?;
    let weights = json
        .get("weights")
        .ok_or_else(|| AlpacaError::Parse("missing 'weights' field".to_string()))?;
    let map: HashMap<String, f64> = serde_json::from_value(weights.clone())
        .map_err(|e| AlpacaError::Parse(format!("weights parse: {e}")))?;
    Ok(map)
}

/// 简单 epoch helper（PaperPnL 等用）
pub fn now_utc() -> DateTime<Utc> {
    Utc::now()
}
