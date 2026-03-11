# 美股回测策略文档

本文档说明美股事件驱动 P&L 回测系统的完整算法逻辑。

---

## 一、系统概述

美股回测基于 **Polymarket 预测市场告警** 驱动：当 Polymarket 事件价格发生显著变动时，LLM 分析其对美股的潜在影响，生成 affected_tickers（受影响股票 + 方向 + 置信度），回测引擎模拟在告警触发后建仓并持有 N 天，计算实际收益。

核心代码：`backend/services/polymarket/us_stock_backtester.py`

---

## 二、数据源

### 2.1 美股行情数据（yfinance）

下载器：`backend/services/data/fmp_downloader.py`（FMPDownloader，保留类名兼容 API）

| 数据 | 来源 | 说明 |
|------|------|------|
| 股票列表 | Wikipedia + yfinance | S&P 500 + NASDAQ 100 成分股，yfinance 补充市值和 IPO 日期 |
| 日线行情 | `yf.download()` | OHLCV + 复权收盘价，批量下载（50 ticker/批，8 线程） |
| 季度财务 | `Ticker.quarterly_*` | 利润表、资产负债表、现金流 |
| SEC 提交日期 | `Ticker.sec_filings` | 从 10-Q/10-K 匹配报告期末日获取 filing_date |
| GICS 行业 | `Ticker.info` | sector / industry |
| 分析师评级 | `Ticker.upgrades_downgrades` | 券商升降级 |
| 公司行动 | `Ticker.dividends` / `splits` | 分红、拆股 |

**指数**（`config.py: US_INDEX_SYMBOLS`）：`^GSPC`（S&P 500）、`^IXIC`（NASDAQ）、`^DJI`（Dow Jones）

**商品期货**（`config.py: US_COMMODITY_SYMBOLS`）：GC=F（黄金）、SI=F（白银）、CL=F（WTI 原油）、BZ=F（布伦特原油）、NG=F（天然气）、HG=F（铜）、ZC=F（玉米）、ZS=F（大豆）、ZW=F（小麦）

### 2.2 美国宏观数据（FRED）

下载器：`backend/services/data/fred_downloader.py`（FREDDownloader）

通过 `fredapi` 库访问 20 项宏观指标：

| 分类 | 指标 |
|------|------|
| 经济增长 | GDP、工业生产（INDPRO）、零售销售（RSAFS） |
| 通胀 | CPI（CPIAUCSL）、核心 CPI（CPILFESL）、PPI（PPIACO）、PCE（PCEPI） |
| 就业 | 失业率（UNRATE）、非农就业（PAYEMS）、初领失业金（ICSA）、制造业就业（MANEMP） |
| 利率 | 联邦基金利率（FEDFUNDS）、10Y 国债（DGS10）、2Y 国债（DGS2）、10Y-2Y 利差（T10Y2Y）、TED 利差（TEDRATE） |
| 货币 | M2 货币供应（M2SL） |
| 市场 | VIX 波动率（VIXCLS）、美元指数（DTWEXBGS） |
| 地产 | 新屋开工（HOUST） |

### 2.3 Polymarket 告警数据

存储在 `polymarket_alert` 表，包含：
- `condition_id`：市场 ID
- `alert_type`：告警类型（spike/dump/reversal 等）
- `price_before` / `price_after` / `price_change`：价格变动
- `affected_tickers`：JSON 格式，LLM 分析的受影响股票列表
- `llm_confidence` / `llm_sentiment` / `llm_summary`：LLM 分析结果

---

## 三、数据库表结构

10 张美股 ORM 表（`backend/services/data/database.py`）：

| 表 | 说明 | 唯一键 |
|----|------|--------|
| `us_stock_basic` | 股票基本信息 | ticker |
| `us_daily_price` | 日线行情 OHLCV | (ticker, trade_date) |
| `us_financial_data` | 季度财务（利润/资产/现金流） | (ticker, period) |
| `us_industry_class` | GICS 行业分类 | ticker |
| `us_index_daily` | 指数日线 | (index_code, trade_date) |
| `us_macro_indicator` | FRED 宏观指标 | (indicator_code, report_date) |
| `us_commodity_price` | 商品期货日线 | (symbol, trade_date) |
| `us_analyst_recommendation` | 分析师评级 | (ticker, date, analyst_company) |
| `us_sec_filing` | SEC 公告 | (ticker, filing_date, type) |
| `us_corporate_action` | 公司行动（分红/拆股） | (ticker, date, action_type) |

---

## 四、回测引擎（UsStockBacktester）

### 4.1 运行模式

| 模式 | 方法 | 数据来源 |
|------|------|----------|
| 模式 A | `run_from_alerts(alerts_json)` | 前端传入的 alerts JSON 列表 |
| 模式 B | `run_from_db()` | 直接从 `polymarket_alert` 表读取 |

模式 B 自动去重：同日同 `condition_id` 同 `alert_type` 只保留最早一条。

### 4.2 交易信号提取

从每条告警的 `affected_tickers` 提取：

```
告警 → affected_tickers[i] → {
    ticker: "AAPL",
    direction: "bullish" | "bearish",
    confidence: 0.0~1.0
}
```

- 过滤条件：`confidence >= min_confidence`（默认 0.0，即不过滤）
- 每个 ticker 独立成一笔交易信号

### 4.3 入场逻辑（Entry）

根据告警时间确定入场日期（美东时间 ET）：

```
if alert_time.hour < 16:00 ET:
    入场日 = 当天（如果是交易日）
else:
    入场日 = 下一个交易日
```

- 最多向前搜索 10 个自然日
- 入场价 = 入场日收盘价

### 4.4 出场逻辑（Exit）

```
出场日 = 入场日后第 holding_days 个交易日
```

- 默认 `holding_days = 5`（一周）
- 出场价 = 出场日收盘价
- **Mark-to-Market（MTM）**：如果持仓期尚未结束（数据不足），使用最新可用收盘价，标记 `is_mark_to_market = true`

### 4.5 收益计算

```python
# 做多（bullish）
return_pct = (exit_price - entry_price) / entry_price × 100

# 做空（bearish）
return_pct = (entry_price - exit_price) / entry_price × 100

# 基准收益：同期买入持有（纯多）
benchmark_pct = (exit_price - entry_price) / entry_price × 100

# 超额收益
alpha_pct = return_pct - benchmark_pct
```

### 4.6 股价加载

- 从 `us_daily_price` 表批量加载
- 时间范围：`min(alert_time) - 5天` 到 `max(alert_time) + holding_days + 10天`
- 分批查询（50 ticker/批）

---

## 五、汇总统计

### 5.1 基础指标

| 指标 | 说明 |
|------|------|
| total_trades | 总交易笔数 |
| settled_trades | 已结算（非 MTM）笔数 |
| mtm_trades | Mark-to-Market 笔数 |
| win_rate | 胜率 = win_count / total_trades |
| avg_return_pct | 平均收益率 |
| median_return_pct | 中位数收益率 |
| total_return_pct | 累计收益率（加总） |
| sharpe_ratio | 夏普比率 = mean/std × √252 |
| profit_factor | 盈利因子 = 总盈利 / 总亏损 |
| max_single_win_pct | 单笔最大盈利 |
| max_single_loss_pct | 单笔最大亏损 |

### 5.2 分组统计

- **按方向**（bullish / bearish）：胜率、平均收益
- **按告警类型**（spike / dump / reversal 等）：胜率、平均收益
- **按置信度**（high ≥0.7 / medium 0.4-0.7 / low <0.4）：胜率、平均收益
- **按 ticker**：交易次数、胜率、平均收益（Top 20）
- **Top Winners / Losers**：各 10 笔

### 5.3 基准对比

- `benchmark_avg_pct`：所有交易同期买入持有的平均收益
- `benchmark_win_rate`：买入持有的胜率
- `alpha_avg_pct`：策略超额收益 = avg_return - benchmark_avg

---

## 六、API 端点

| 端点 | 方法 | 说明 |
|------|------|------|
| `/api/polymarket/us-pnl/run` | POST | 从 DB 运行 P&L 回测 |
| `/api/polymarket/us-pnl/run-from-alerts` | POST | 从 alerts JSON 运行 |

请求参数：
- `holding_days`：持有天数（默认 5）
- `min_confidence`：最低置信度过滤（默认 0.0）
- `limit`：最大告警数量（0=不限）

---

## 七、与 A 股系统的区别

| 维度 | A 股系统 | 美股系统 |
|------|---------|---------|
| 驱动方式 | 多因子打分 + 定期调仓 | 事件驱动（Polymarket 告警） |
| 选股逻辑 | 30 个因子加权排序 Top-N | LLM 分析受影响 ticker + 方向 |
| 调仓频率 | 半月频 + 自适应偏离度触发 | 每笔告警独立建仓 |
| 持仓期 | 持续持有直到调出 | 固定 N 天（默认 5 天） |
| 做空 | 不支持（A 股限制） | 支持（bearish 方向） |
| 风控 | 行业上限 + 回撤响应 + 波动率目标 | 单笔独立，无组合级风控 |
| 数据源 | Tushare Pro | yfinance + FRED + Polymarket |

---

## 八、配置参数

| 环境变量 | 默认值 | 说明 |
|----------|--------|------|
| `FRED_API_KEY` | — | FRED API 密钥（必需） |
| `US_DATA_START_DATE` | `20150101` | 美股数据起始日期 |
| `FMP_API_KEY` | — | 历史遗留，当前未使用 |
