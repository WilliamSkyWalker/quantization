# Polymarket 事件驱动美股预警系统 — 算法文档

## 目录

1. [系统概览](#1-系统概览)
2. [数据源与接入](#2-数据源与接入)
3. [市场发现算法](#3-市场发现算法)
4. [实时赔率流与价格历史](#4-实时赔率流与价格历史)
5. [Spike 检测算法](#5-spike-检测算法)
6. [LLM 事件分析器](#6-llm-事件分析器)
7. [告警管理与去重](#7-告警管理与去重)
8. [数据模型](#8-数据模型)
9. [实时推送架构](#9-实时推送架构)
10. [历史回测引擎](#10-历史回测引擎)
11. [美股 P&L 回测引擎](#11-美股-pl-回测引擎)
12. [美股数据接入](#12-美股数据接入)
13. [可配置参数汇总](#13-可配置参数汇总)

---

## 1. 系统概览

基于 Polymarket 预测市场的事件驱动美股预警系统。核心逻辑：**预测市场赔率的剧烈变动是现实世界事件的先行信号**，通过监控赔率 Spike 并用 LLM 分析事件对美股的影响，生成实时交易预警。

与 A 股多因子系统的半月频+自适应调仓不同，本系统是**事件驱动、实时响应**的模式。

```
Polymarket Gamma API ──→ 市场发现（高交易量政治/经济事件，每小时刷新）
         ↓
Polymarket CLOB WebSocket ──→ 实时赔率流（tick 级推送）
         ↓
    PriceHistory 滚动缓存（24h deque）
         ↓
    Spike 检测（5min / 1h / 24h 三档阈值）
         ↓
    AlertManager（去重 + LLM 冷却）
         ↓
    EventAnalyzer → LLM 分析（事件→受影响股票 + 方向 + 置信度）
         ↓
    告警持久化（MySQL）+ WebSocket 推送（Django Channels → 前端）
```

---

## 2. 数据源与接入

### 2.1 Gamma API（市场发现）

> 端点: `https://gamma-api.polymarket.com/events`

Polymarket 的公开 REST API，提供所有活跃预测市场的元数据：

- **question**: 市场问题（如 "Will the US impose tariffs on China by June 2026?"）
- **category**: 分类（politics, economics, crypto, sports 等）
- **outcomePrices**: YES/NO 赔率（0-1 概率，如 `[0.72, 0.28]`）
- **volume**: 总交易量（USD）
- **liquidity**: 流动性深度（USD）
- **conditionId / clobTokenIds**: 用于 CLOB WebSocket 订阅的标识

调用频率：每 `POLYMARKET_DISCOVERY_INTERVAL`（默认 3600s）一次。

### 2.2 CLOB WebSocket（实时赔率流）

> 端点: `wss://ws-subscriptions-clob.polymarket.com/ws/market`

Polymarket 的 Central Limit Order Book WebSocket，提供 tick 级实时赔率推送：

- 订阅格式: `{"type": "market", "assets_ids": ["token_id_1", "token_id_2", ...]}`
- 消息格式: `{"asset_id": "...", "price": 0.72, ...}`
- 自动重连：断线后 5 秒自动重连

### 2.3 数据源特性

| 特性 | Gamma API | CLOB WebSocket |
|------|-----------|----------------|
| 延迟 | ~1s（HTTP 请求） | ~100ms（流式推送） |
| 频率 | 按需调用（默认 1h） | 实时 tick |
| 认证 | 无需 | 无需 |
| 费率限制 | 宽松 | 无显式限制 |
| 数据内容 | 市场元数据 + 快照赔率 | 实时赔率变动 |

---

## 3. 市场发现算法

> 文件: `services/polymarket/monitor.py :: _discover_markets()`

每小时从 Gamma API 发现值得监控的预测市场。

### 3.1 筛选流程

```
GET /events?active=true&closed=false&order=volume24hr&ascending=false&limit=50
  → 遍历每个 event 下的 markets
  → 过滤: volume >= POLYMARKET_MIN_VOLUME (默认 $50,000)
  → 取 conditionId 作为唯一标识
  → 解析 outcomePrices 获取 YES/NO 赔率
  → Upsert 到 polymarket_event 表
  → 加入内存 _markets 字典
  → 初始化 PriceHistory（首个数据点 = Gamma API 返回的当前赔率）
```

### 3.2 市场上限

最多监控 `POLYMARKET_MAX_MARKETS`（默认 50）个市场。按 24h 交易量降序排列，高交易量市场优先。

### 3.3 Upsert 语义

与 A 股数据下载一致，所有写入均为幂等操作：
- 已存在 → 更新赔率、交易量、流动性、标记 `is_active=True`
- 不存在 → 插入新记录

---

## 4. 实时赔率流与价格历史

> 文件: `services/polymarket/monitor.py :: PriceHistory`

### 4.1 PriceHistory 滚动缓存

每个市场维护一个内存中的 `PriceHistory` 实例：

- **数据结构**: `deque[(timestamp, price)]`，按时间正序
- **保留窗口**: 24 小时（`max_age_seconds=86400`）
- **自动清理**: 每次 `add()` 时删除超过 24h 的旧数据
- **查询方式**: `get_price_at(seconds_ago)` — 最近邻匹配

### 4.2 最近邻匹配与数据充分性

```python
def get_price_at(seconds_ago):
    target_time = now - seconds_ago
    找到 deque 中距离 target_time 最近的数据点
    if 最近邻距离 > seconds_ago * 20%:
        return None  # 数据不足，不做判断
    return 该数据点的 price
```

**20% 容差规则**: 如果距目标时间点最近的数据点偏差超过时间窗口的 20%，则认为数据不足以判断该时间窗口的变动，返回 None 跳过检测。

例如：查询 5 分钟前的价格（300s），如果最近的数据点距离 300s 前差了 60s 以上，就放弃。

### 4.3 快照持久化

每 `POLYMARKET_SNAPSHOT_INTERVAL`（默认 60s）将所有监控中市场的当前价格写入 `polymarket_price_snapshot` 表，用于：
- 历史赔率回溯
- 告警事后分析
- 前端赔率走势图

---

## 5. Spike 检测算法

> 文件: `services/polymarket/monitor.py :: _check_spikes()`

Spike 检测是系统的核心算法——判断赔率是否发生了**异常大幅变动**。

### 5.1 三档阈值

| 告警类型 | 时间窗口 | 阈值 | 含义 | 配置项 |
|---------|---------|------|------|-------|
| `spike_5m` | 5 分钟 | 5% | 极短期剧烈波动（突发事件） | `POLYMARKET_SPIKE_5M` |
| `spike_1h` | 1 小时 | 15% | 中短期趋势确认 | `POLYMARKET_SPIKE_1H` |
| `spike_24h` | 24 小时 | 25% | 日级别重大转向 | `POLYMARKET_SPIKE_24H` |

### 5.2 检测逻辑

```
每收到一条 WebSocket 赔率消息:
    current_price = 消息中的 YES 赔率

    for (alert_type, lookback_seconds, threshold) in SPIKE_RULES:
        old_price = PriceHistory.get_price_at(lookback_seconds)
        if old_price is None:
            continue  # 数据不足，跳过
        change = current_price - old_price
        if abs(change) >= threshold:
            trigger_alert(...)
```

### 5.3 阈值说明

阈值为**绝对价格变动**（不是百分比变动），因为 Polymarket 赔率本身就是 0-1 的概率值：
- 赔率从 0.50 → 0.55 = 变动 0.05 = 触发 5min Spike
- 赔率从 0.30 → 0.55 = 变动 0.25 = 触发 24h Spike
- 赔率从 0.80 → 0.65 = 变动 -0.15 = 触发 1h Spike（绝对值）

### 5.4 设计考量

- **三档互不排斥**: 同一市场可以同时触发多个档位的 Spike（如 5min Spike + 1h Spike）
- **双向检测**: 赔率上升和下降都会触发（使用 `abs(change)`）
- **去重靠 AlertManager**: Spike 检测本身不做去重，由 AlertManager 的冷却机制控制

---

## 6. LLM 事件分析器

> 文件: `services/polymarket/event_analyzer.py`

### 6.1 分析流程

当 Spike 触发后，EventAnalyzer 将事件信息发送给 LLM，获取对美股的影响分析：

```
输入:
    - question: 市场问题
    - description: 市场描述（截断到 3000 字符）
    - category: 分类
    - price_before / price_after / price_change: 赔率变动详情
    - alert_type: Spike 类型
    - timeframe: 人类可读时间描述

LLM 分析 →

输出 (JSON):
    - affected_tickers: [{ticker, direction, confidence, reasoning}]
    - affected_sectors: [GICS 行业]
    - summary: 2-3 句分析
    - overall_sentiment: -1.0 ~ +1.0
    - confidence: 0.0 ~ 1.0
```

### 6.2 双提供商模式

复用 A 股舆情系统的 LLM 集成模式：

| 提供商 | SDK | 配置 |
|-------|-----|------|
| Anthropic Claude | `anthropic` | `LLM_PROVIDER=anthropic` + `LLM_API_KEY` |
| OpenAI-compatible | `openai` | `LLM_PROVIDER=openai` + `LLM_API_KEY` + `LLM_API_BASE` |

- `temperature=0.1`（低创造性，偏向一致性）
- `max_tokens=800`
- 90 秒超时
- 无 API key 或 SDK 未安装时**优雅降级**（告警仍然生成，但无 LLM 分析字段）

### 6.3 Ticker 交叉验证

LLM 返回的 ticker 会与 S&P 500 + NASDAQ 100 成分股列表交叉验证：

```python
valid_tickers = set(US_FALLBACK_TICKERS)  # S&P 500 + NASDAQ 100 成分股

for ticker in llm_output["affected_tickers"]:
    if ticker not in valid_tickers:
        过滤掉  # LLM 幻觉的非成分股 ticker
```

这确保只有实际可交易的成分股才会出现在告警中。验证列表来自 `config.py::US_FALLBACK_TICKERS`（可通过 `.env` 覆盖）。成分股列表通过 `fmp_downloader.py` 从 Wikipedia 抓取 S&P 500 和 NASDAQ 100 页面获取，合并去重后约 530 只。

### 6.4 Prompt 设计

System Prompt 要求 LLM 扮演**专注于预测市场和事件驱动交易的美股分析师**，输出要求：
- `affected_tickers`: 仅包含有**明确直接敞口**的股票，上限 10 只
- `direction`: 必须为 `bullish` 或 `bearish`
- `confidence`: 0-1 置信度
- `reasoning`: 每只股票的简要理由

### 6.5 响应解析与容错

- 支持 markdown 代码块包裹的 JSON（提取 `{...}` 部分）
- 字段范围校验（sentiment 夹到 [-1, 1]，confidence 夹到 [0, 1]）
- direction 缺失时根据 overall_sentiment 符号推断
- 解析失败返回 None，不影响告警生成

---

## 7. 告警管理与去重

> 文件: `services/polymarket/alert_manager.py`

### 7.1 去重机制（冷却期）

同一事件的同一类型 Spike 在冷却期内不重复触发：

```python
cache_key = (condition_id, alert_type)
if now - last_trigger[cache_key] < POLYMARKET_LLM_COOLDOWN:
    跳过  # 冷却中
else:
    last_trigger[cache_key] = now
    继续处理
```

- **冷却期**: `POLYMARKET_LLM_COOLDOWN`（默认 300s = 5 分钟）
- **粒度**: 每个 (condition_id, alert_type) 独立冷却
- 同一事件的不同类型 Spike 互不干扰（如 5min Spike 和 1h Spike 可以同时触发）

### 7.2 告警处理流程

```
1. 去重检查 → 冷却中则跳过
2. 调用 EventAnalyzer.analyze() → 获取 LLM 分析（可选）
3. 构建 PolymarketAlert ORM 对象
4. 写入 polymarket_alert 表
5. 通过 Django Channels group_send("polymarket", ...) 推送到前端
```

### 7.3 告警数据结构

每条告警包含：

| 字段 | 说明 |
|------|------|
| `condition_id` | 关联的市场 |
| `alert_type` | spike_5m / spike_1h / spike_24h |
| `price_before` / `price_after` / `price_change` | 赔率变动详情 |
| `timeframe_seconds` | 时间窗口 |
| `question` | 事件问题（冗余存储） |
| `affected_tickers` | JSON: LLM 分析的受影响股票 |
| `affected_sectors` | JSON: 受影响行业 |
| `llm_summary` | LLM 分析摘要 |
| `llm_sentiment` | 整体市场情感 -1.0 ~ +1.0 |
| `llm_confidence` | 分析置信度 0.0 ~ 1.0 |
| `is_read` | 用户是否已读 |

---

## 8. 数据模型

> 文件: `services/polymarket/models.py`

### 8.1 polymarket_event

监控的预测市场事件，由 Gamma API 发现并持续更新。

| 列 | 类型 | 说明 |
|----|------|------|
| `condition_id` | VARCHAR(100), UNIQUE | Polymarket 唯一标识 |
| `token_id` | VARCHAR(100) | CLOB YES 合约 token ID |
| `question` | VARCHAR(1000) | 市场问题 |
| `description` | TEXT | 市场描述 |
| `category` | VARCHAR(100) | 分类 |
| `outcome_yes_price` | FLOAT | 最新 YES 赔率 |
| `outcome_no_price` | FLOAT | 最新 NO 赔率 |
| `volume` | FLOAT | 总交易量 (USD) |
| `liquidity` | FLOAT | 流动性 (USD) |
| `end_date` | DATETIME | 市场结束日期 |
| `is_active` | BOOLEAN | 是否在监控中 |
| `slug` | VARCHAR(500) | Polymarket URL slug |
| `gamma_market_id` | VARCHAR(100) | Gamma API 内部 ID |

### 8.2 polymarket_price_snapshot

赔率时间序列，每 60 秒一条快照。

| 列 | 类型 | 说明 |
|----|------|------|
| `condition_id` | VARCHAR(100) | 关联市场 |
| `timestamp` | DATETIME | 快照时间 |
| `yes_price` | FLOAT | YES 赔率 |
| `no_price` | FLOAT | NO 赔率 |
| `spread` | FLOAT | 买卖价差 |
| `volume_24h` | FLOAT | 24h 交易量 |
| `source` | VARCHAR(20) | 数据来源 (websocket / gamma) |

索引: `(condition_id, timestamp)` 联合索引。

### 8.3 polymarket_alert

生成的告警记录，含 LLM 分析结果。

| 列 | 类型 | 说明 |
|----|------|------|
| `condition_id` | VARCHAR(100) | 关联市场 |
| `alert_type` | VARCHAR(20) | spike_5m / spike_1h / spike_24h |
| `price_before` / `price_after` / `price_change` | FLOAT | 赔率变动 |
| `timeframe_seconds` | INTEGER | 时间窗口 |
| `question` | VARCHAR(1000) | 事件问题 |
| `affected_tickers` | TEXT (JSON) | 受影响股票 |
| `affected_sectors` | TEXT (JSON) | 受影响行业 |
| `llm_summary` | TEXT | 分析摘要 |
| `llm_sentiment` | FLOAT | 情感 -1.0 ~ +1.0 |
| `llm_confidence` | FLOAT | 置信度 0.0 ~ 1.0 |
| `is_read` | BOOLEAN | 是否已读 |

---

## 9. 实时推送架构

### 9.1 后端推送链路

```
Polymarket CLOB WebSocket
  → PolymarketMonitor (asyncio 线程)
  → _handle_ws_message()
    → 更新 PriceHistory (内存 deque)
    → _push_price_update() → Django Channels group_send("polymarket")  [实时赔率]
    → _check_spikes() → AlertManager.trigger_alert()
      → EventAnalyzer.analyze() → LLM API
      → DB 写入 polymarket_alert
      → Django Channels group_send("polymarket")  [新告警]
```

### 9.2 WebSocket Consumer

> 文件: `tasks/consumers.py :: PolymarketConsumer`

```
ws/polymarket/ → PolymarketConsumer
    connect → 加入 "polymarket" 组
    disconnect → 离开 "polymarket" 组

消息类型:
    price_update → {type, data: {condition_id, yes_price, no_price, timestamp}}
    alert        → {type, data: {id, condition_id, alert_type, price_change, affected_tickers, ...}}
```

### 9.3 前端 WebSocket

前端页面 `Polymarket.vue` 在 `onMounted` 时连接 `ws/polymarket/`，监控运行中时保持连接：

- `price_update` → 就地更新市场列表中对应行的 YES 赔率
- `alert` → 插入告警列表头部 + 全局提示
- 断线自动重连（3 秒间隔）

---

## 10. 历史回测引擎

> 文件: `services/polymarket/backtester.py`, `services/polymarket/history.py`

### 10.1 历史数据下载

`PolymarketHistoryDownloader` 从已结算市场下载历史赔率时间序列：

```
Gamma API → 发现已结算高交易量市场（active=false, closed=true）
  → CLOB API /prices-history → 分钟级赔率序列
  → 存入 polymarket_price_snapshot 表
```

- **CLOB API**: `https://clob.polymarket.com/prices-history?market={token_id}&interval=1m&fidelity=1`
- **请求间隔**: 50ms（保守限速，CLOB API 限制 9000 req/10s）
- **数据结构**: `{timestamp, price}` 序列，存为 `polymarket_price_snapshot` 记录

### 10.2 回测流程

`PolymarketBacktester` 回放已结算市场的历史价格，模拟实时 Spike 检测：

```
读取 DB 中已结算市场 + 历史价格序列
  → 按时间顺序逐条回放价格
  → 使用与实时监控相同的三档 Spike 检测逻辑
  → 触发 LLM 分析（可选）
  → 汇总：总告警数、按类型/市场分布、LLM 分析结果
```

- **Spike 逻辑复用**: 与实时监控使用相同的阈值和检测算法
- **LLM 冷却**: 同一 (condition_id, alert_type) 遵守 `POLYMARKET_LLM_COOLDOWN`
- **可选跳过 LLM**: `use_llm=False` 仅做 Spike 检测，节省 API 调用
- **自定义阈值**: 支持覆盖 `spike_5m/1h/24h` 参数

---

## 11. 美股 P&L 回测引擎

> 文件: `services/polymarket/us_stock_backtester.py`

将 Polymarket 告警（含 LLM 分析的受影响 ticker + 方向）与实际美股价格联动，计算事件驱动策略的真实收益。

### 11.1 两种运行模式

| 模式 | 输入源 | API 端点 |
|------|-------|---------|
| **模式 A** | Polymarket 回测结果的 alerts JSON | `POST /api/polymarket/backtest/us-stock-pnl` |
| **模式 B** | DB `polymarket_alert` 表 | `POST /api/polymarket/backtest/us-stock-pnl-from-db` |

### 11.2 核心流程

```
告警列表 → 提取交易信号 (ticker, direction, confidence, alert_time)
  → 过滤: confidence >= min_confidence
  → 批量下载股价 (yfinance, batch_size=20)
  → 逐笔计算:
    1. 入场日期: 告警时间 < 16:00 ET → 当天; ≥ 16:00 ET → 下一交易日
    2. 出场日期: 入场后第 N 个交易日（默认 5 天）
    3. 收益率: bullish → (exit-entry)/entry; bearish → (entry-exit)/entry
    4. 未到期: 使用最新收盘价做 mark-to-market
  → 汇总统计
```

### 11.3 入场时间逻辑

基于美国东部时间 (ET)，以 16:00（美股收盘）为界：

| 告警时间 | 入场规则 |
|---------|---------|
| 交易日 16:00 ET 前 | 当天收盘价入场 |
| 交易日 16:00 ET 后 | 下一个交易日收盘价入场 |
| 非交易日（周末/假日） | 下一个交易日收盘价入场 |

### 11.4 汇总统计

| 指标 | 说明 |
|------|------|
| 胜率 (win_rate) | 正收益交易数 / 总交易数 |
| 平均收益率 | 所有交易收益率均值 |
| 中位数收益率 | 所有交易收益率中位数 |
| Sharpe Ratio | (avg_return / std_return) × √252 |
| Profit Factor | 总盈利 / |总亏损| |
| 最大单笔盈利/亏损 | 单笔收益率极值 |

**分组统计**:
- 按方向 (bullish/bearish)
- 按告警类型 (spike_5m/1h/24h)
- 按置信度 (high ≥0.7 / medium 0.4-0.7 / low <0.4)
- 按 ticker（前 20 高频 ticker）
- Top 10 盈利/亏损交易

### 11.5 配置参数

| 参数 | 默认值 | 说明 |
|------|-------|------|
| `holding_days` | 5 | 持仓交易日数 |
| `min_confidence` | 0.0 | 最低 LLM 置信度过滤 |
| `limit` | 200 | 模式 B 从 DB 读取的告警数上限 |

---

## 12. 美股数据接入

> 文件: `services/data/fmp_downloader.py`

### 12.1 股票池

**S&P 500 + NASDAQ 100** 合并去重，约 530 只美股：

```
Wikipedia "List of S&P 500 companies" → ~503 只（NYSE）
Wikipedia "Nasdaq-100" → 合并新增 ~30 只（NASDAQ）
去重: S&P 500 已有的 ticker 不重复添加
兜底: 两页面均失败时使用 US_FALLBACK_TICKERS
→ Upsert 到 us_stock_basic 表
→ 不在列表中的旧股票标记为 is_active=0
```

### 12.2 日线行情

通过 yfinance 批量下载日线数据（OHLCV + 复权价），存入 `us_daily_price` 表。

### 12.3 指数与商品

| 类别 | 代码 |
|------|------|
| 指数 | ^GSPC (S&P 500), ^IXIC (NASDAQ), ^DJI (Dow Jones) |
| 商品 | GC=F (金), SI=F (银), CL=F (WTI油), BZ=F (布油), NG=F (天然气), HG=F (铜), ZC=F (玉米), ZS=F (大豆), ZW=F (小麦) |

---

## 13. 可配置参数汇总

> 文件: `services/config.py`

所有参数均可通过 `.env` 文件覆盖。

### 13.1 Polymarket 专属配置

| 参数 | 默认值 | 说明 |
|------|-------|------|
| `POLYMARKET_GAMMA_API` | `https://gamma-api.polymarket.com` | Gamma REST API 地址 |
| `POLYMARKET_CLOB_WS` | `wss://ws-subscriptions-clob.polymarket.com/ws/market` | CLOB WebSocket 地址 |
| `POLYMARKET_SPIKE_5M` | 0.05 | 5 分钟 Spike 阈值 |
| `POLYMARKET_SPIKE_1H` | 0.15 | 1 小时 Spike 阈值 |
| `POLYMARKET_SPIKE_24H` | 0.25 | 24 小时 Spike 阈值 |
| `POLYMARKET_MIN_VOLUME` | 50000 | 最低交易量过滤 (USD) |
| `POLYMARKET_MAX_MARKETS` | 50 | 最大监控市场数 |
| `POLYMARKET_SNAPSHOT_INTERVAL` | 60 | 快照保存间隔（秒） |
| `POLYMARKET_DISCOVERY_INTERVAL` | 3600 | 市场发现间隔（秒） |
| `POLYMARKET_LLM_COOLDOWN` | 300 | 同一事件 LLM 分析冷却期（秒） |

### 13.2 复用的全局配置

| 参数 | 用途 |
|------|------|
| `LLM_PROVIDER` | LLM 提供商（anthropic / openai） |
| `LLM_API_KEY` | LLM API 密钥（无则禁用 LLM 分析） |
| `LLM_API_BASE` | OpenAI-compatible API 基础 URL |
| `LLM_MODEL` | LLM 模型名称 |
| `US_FALLBACK_TICKERS` | S&P 500 + NASDAQ 100 成分股兜底列表（用于交叉验证） |

### 13.3 API 端点

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/polymarket/monitor/start` | 启动监控（后台任务） |
| POST | `/api/polymarket/monitor/stop` | 停止监控 |
| GET | `/api/polymarket/status` | 监控状态 + 市场列表 |
| GET | `/api/polymarket/alerts` | 告警列表（分页，?is_read=true/false） |
| POST | `/api/polymarket/alerts/{id}/read` | 标记告警已读 |
| POST | `/api/polymarket/backtest/discover` | 发现已结算市场 |
| GET | `/api/polymarket/backtest/markets` | 已下载的回测市场列表 |
| GET | `/api/polymarket/backtest/price-series/<condition_id>` | 市场赔率时间序列 |
| POST | `/api/polymarket/backtest/run` | 运行 Polymarket 回测 |
| POST | `/api/polymarket/backtest/us-stock-pnl` | 美股 P&L 回测（从 alerts JSON） |
| POST | `/api/polymarket/backtest/us-stock-pnl-from-db` | 美股 P&L 回测（从 DB 告警） |

### 13.4 依赖

| 包 | 版本 | 用途 |
|----|------|------|
| `websockets` | >=12.0 | CLOB WebSocket 连接 |
| `requests` | >=2.31.0 | Gamma API HTTP 调用（已有） |
| `channels` | >=4.0 | Django WebSocket 推送（已有） |
| `yfinance` | >=0.2.31 | 美股价格数据下载（US Stock P&L 回测） |
