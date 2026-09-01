# 数据源说明（Rust v25, 2026-04-30 更新）

> **架构变更（2026-04-30）**：所有 Python 代码已归档至 `legacy_python/`。
> 生产 = Rust `quant-engine/`。本文档只反映**当前在用**的数据源。
> 已废弃的 UW / Fiscal.ai / Quiver / AlphaVantage 详见 [废弃章节](#已废弃数据源)。

---

## 一、生产数据源（仅 2 家）

| 数据源 | 费用 | 用途 | Rust crate |
|--------|------|------|------------|
| **FMP Ultimate** | 付费 (~$300/月) | 美股全部财务/价格/EPS/insider/dividend | `quant-download/src/us_fmp.rs` |
| **FRED** | **完全免费** | 美股 12 个宏观指标 | `quant-download/src/us_fred.rs` |
| **Tushare Pro** | 免费/付费 token | A 股全部数据 | `quant-download/src/a_tushare.rs` |

---

## 二、美股 — FMP Ultimate

### 端点（`quant download --source fmp --target X`）

| target | 表 | 说明 |
|--------|----|------|
| `stock_list` | `us_stock_basic` | 股票列表 + 上市/退市状态 |
| `profile` | `us_company_profile` | 公司基本信息 + 行业 + sector |
| `daily_price` | `us_daily_price` | 日线 OHLCV（split-adjusted close）|
| `financial` | `us_financial_data` | 财报（130 列全字段，含 NI/Rev/Equity/Assets 等）|
| `key_metric` | `us_key_metric` | 104 列衍生指标（ROE/ROIC/EV-FCF 等）|
| `growth` | `us_financial_growth` | 增长率指标 |
| `enterprise_value` | `us_enterprise_value` | EV + market cap（季度）|
| `earnings` | `us_earnings_surprise` | 财报暴击数据（PEAD 因子用）|
| `eps_estimate` | `us_eps_estimate` | 分析师 EPS 一致预测 |
| `insider` | `us_insider_trade` | 内部人交易 |
| `analyst` | `us_analyst_recommendation` | 分析师评级 |
| `dividend` | `us_corporate_action_div` | 派息记录 |
| `score` | `us_financial_score` | FMP 自算 Piotroski/Altman（对照用）|
| `float` | `us_shares_float` | 自由流通股本 |
| `employee` | `us_employee_count` | 员工数（增长率因子用）|
| `price_target` | `us_price_target` | 分析师目标价 |
| `esg` | `us_esg_rating` | ESG 评分 |
| `dcf` | `us_dcf_valuation` | FMP 自算 DCF |
| `peer` | `us_stock_peer` | 同业列表 |
| `index` | `us_index_daily` | S&P 500 / NASDAQ 指数日线 |
| `macro` | `us_macro_indicator` | 部分宏观（与 FRED 互补）|
| `congress` | `us_congress_trade` | 国会议员交易 |
| `press` | `us_press_release` | 财报新闻稿 |
| `revenue_segment` | `us_revenue_segment` | 产品+地理 收入分段 |

### 配置
- **依赖**：`FMP_API_KEY` 环境变量
- **限速**：默认 2500 req/min（Ultimate plan 上限 3000）；`FMP_RATE_LIMIT` 可调
- **并发**：30 worker (tokio Semaphore)，DB pool 40
- **起始年**：`--start-year 1995`（Ultimate 历史回到 1980s）
- **增量更新**：`--incremental` 自动按 ticker 取最新日期续拉

### 命令
```bash
cd quant-engine
./target/release/quant download --source fmp --target all --start-year 1995
./target/release/quant download --source fmp --target all --incremental   # 日常增量
./target/release/quant download --source fmp --target daily_price --incremental  # 单端点增量
```

---

## 三、美股 — FRED 宏观（免费）

12 个 FRED series（NFCI, HY OAS, IG OAS, 短端利率, 通胀预期, 失业率等）：

```bash
./target/release/quant download --source fred --target all --start-year 2000
```

- **依赖**：`FRED_API_KEY`（[免费注册](https://fred.stlouisfed.org/docs/api/api_key.html)）
- **存储表**：`us_macro_indicator`
- **当前 strategy 使用**：MACRO_CYCLE/MACRO_LIQD/MACRO_INFL/MACRO_EXTR（实际 IC 较弱，主要用于 regime 检测）

---

## 四、A 股 — Tushare Pro

### 端点（`quant --market cn download --source tushare --target X`）

| target | 表 | 用途 |
|--------|----|------|
| `stock_list` | `a_stock_basic` | 股票列表 + ST/退市标记 |
| `trade_cal` | `a_trade_cal` | 交易日历 |
| `daily_price` | `a_daily_price` | 日线 + 换手率/估值 |
| `income` | `a_financial_income` | 利润表（全字段）|
| `balance` | `a_financial_balance` | 资产负债表 |
| `cashflow` | `a_financial_cashflow` | 现金流表 |
| `indicator` | `a_financial_indicator` | 财务指标 |
| `industry` | `a_industry_class` | 申万行业分类 |
| `index` | `a_index_daily` | 沪深 300 / 中证 500 |
| `macro` | `a_macro_indicator` | SHIBOR/LPR/CPI/PPI/PMI/M2/GDP/美债 |

- **依赖**：`TUSHARE_TOKEN`
- **限速**：默认 200 req/min；`TUSHARE_RATE_LIMIT` 可调
- **并发**：10 worker（Tushare 限速更严）
- **起始年**：默认 2015

### 命令
```bash
./target/release/quant --market cn download --source tushare --target all --start-year 2015
./target/release/quant --market cn download --source tushare --target all --incremental
```

---

## 五、Fama-French 5 因子（免费）

用于 Rust backtest 末端的 strategy NAV 回归（α / β_HML / β_RMW 等）。

```bash
# 一次性下载（Ken French Data Library 永久免费）
cd /tmp && curl -fsSL -o ff5.zip \
  "https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/ftp/F-F_Research_Data_5_Factors_2x3_daily_CSV.zip"
unzip -o ff5.zip
awk 'NR==4 {print "date,Mkt-RF,SMB,HML,RMW,CMA,RF"; next} NR>4 && /^[0-9]{8},/ {print}' \
  F-F_Research_Data_5_Factors_2x3_daily.csv > /Users/daweilun/Documents/quantization/cache/ff5_daily.csv
```

格式：`date,Mkt-RF,SMB,HML,RMW,CMA,RF`（百分比，engine `pct=true` 自动 ÷100）。
backtest 自动检测 `cache/ff5_daily.csv` 存在即跑回归。

---

## 六、PostgreSQL 数据库

```
.env:
DB_HOST=...
DB_PORT=5432
DB_USER=...
DB_PASSWORD=...
DB_DATABASE=...
DB_SCHEMA=quant
```

- **连接管理**：sqlx PgPool，pool size 40（download）/ 8（query/CLI）
- **upsert 语义**：`INSERT ... ON CONFLICT (unique_keys) DO UPDATE`
- **慢 SQL 警告**：阈值 2s（sqlx `log_slow_statements`）

---

## 七、Parquet 缓存（本地数据加速）

回测/因子计算前需把 PostgreSQL 数据导出到 `cache/*.parquet`。

```
cache/
├── us_daily_price_<start>_<end>.parquet         33M+ 行
├── alpha_financial_<start>_<end>.parquet         29W+ 行
├── alpha_key_metric_<start>_<end>.parquet        27W+ 行
├── alpha_enterprise_value_<start>_<end>.parquet  23W+ 行
├── us_industry_class_all.parquet                 1.4W
├── us_index_daily_gspc_<start>_<end>.parquet     ~3700
├── ff5_daily.csv                                  Ken French
└── 其他 us_* / a_* 数据 parquet
```

DataCache 启动时一次性加载到内存（约 1-2GB），后续因子计算/回测全部从内存查询。

---

## 八、已废弃数据源

**2026-04-30 决定**：去除 4 家付费第三方数据源，节约 ~$600+/年。

| 数据源 | 月费 | 废弃理由 | 替代/影响 |
|--------|------|---------|----------|
| **Quiver** | ~$50 | 5 个相关因子 IC 全部 < \|0.35\|，部分方向反学术 | 5 因子禁用；alpha 仅退 0.25%/yr |
| **Unusual Whales** | ~$50-100 | 期权流/dark pool 已被 Quiver dark pool 替代后又一并废弃 | 无影响 |
| **Fiscal.ai** | 不详 | `us_daily_ratio` 表从未导入数据 | 无影响 |
| **AlphaVantage** | $0-250 | NEWS_SENTIMENT/IV_SKEW/PUT_CALL_RATIO 数据从未积累 | 无影响 |

废弃因子列表：
- `CONGRESS_NET_BUY` (ICIR=-0.183)
- `GOV_CONTRACT_FLOW` (ICIR=-0.346)
- `LOBBY_INTENSITY` (ICIR=-0.029)
- `DARK_POOL_SHORT` (ICIR=-0.168)
- `INST_OWNERSHIP_DELTA` (ICIR=-0.100)

代码中 `inventory::submit!` 注释保留，结构体仍编译通过，便于将来恢复。

---

## 九、舆情数据（暂缓，已归档）

中国政府网站爬虫 + Twitter 美国政策爬虫 + Polymarket 桥接（共 20 个）：

- 位置：`legacy_python/sentiment/scrapers/`
- 状态：暂未迁移到 Rust（POLYMARKET_SENT 因子 IC 接近零，性价比低）
- 如未来做 NLP sentiment 再决定迁移路径

---

## 十、A 股新闻/政策舆情抓取（Python 独立脚本，运行中）

与上面"已归档"的 20 爬虫体系不同，这是 A 股舆情/事件驱动转型（TODO.md P0.5）新增的三条独立管道，**不进 Rust workspace**，直接写 MySQL（`quant-engine/env.json` 的 `quant.{ENV}` 配置，非 PostgreSQL）：

| 脚本 | 数据源 | 目标表 | 说明 |
|------|--------|--------|------|
| `scripts/news_fetch_pipeline.py` | 东方财富新闻搜索接口 | `a_news_raw` / `a_news_fetch_state` | 个股新闻原文，按公司全称检索（代码检索误召回严重），单关键词硬上限~1000条，不支持日期区间参数。全市场 5212 只股票 backfill 已完成，150 万条入库 |
| `scripts/macro_news_fetch.py` | AkShare `news_cctv`（新闻联播）+ `macro_china_reserve_requirement_ratio`/`macro_china_lpr`（RRR/LPR） | `a_macro_news_raw` / `a_macro_rate_history` | 东财搜索对宏观关键词是模糊匹配不可靠，改用 AkShare 结构化/全文接口。**已知缺口**：`a_macro_news_raw` 存在 2021-10-25~2026-08-27 约 5 年数据缺失（backfill 曾被 WSL 重启中断），RRR/LPR 表完整 |
| `scripts/gov_policy_fetch.py` | 工信部 (MIIT) / 商务部 (MOFCOM) 官网公告列表页 | `a_gov_policy_raw` | 政策公告原文抓取，backfill 已完成 5390 条（MIIT 3075 + MOFCOM 2315） |

**运行模式**：三个脚本均支持 `--mode backfill`（全量补齐，自带去重）和 `--mode incremental`（增量抓取）。

**定时任务（crontab，2026-09-01 配置）**：

```
# 个股新闻: 盘前/盘中/盘后各抓一次（每天含周末）
0 9,13,16 * * *  cd /home/william/quantization && PATH=/home/william/.pyenv/shims:/usr/local/bin:/usr/bin:$PATH python3 scripts/news_fetch_pipeline.py --mode incremental >> logs/news_fetch_pipeline.log 2>&1
# 宏观新闻(新闻联播+RRR/LPR): 每日一次
30 7 * * *  cd /home/william/quantization && PATH=/home/william/.pyenv/shims:/usr/local/bin:/usr/bin:$PATH python3 scripts/macro_news_fetch.py --mode incremental >> logs/macro_news_fetch.log 2>&1
# 政策公告(工信部/商务部): 每日一次
0 8 * * *  cd /home/william/quantization && PATH=/home/william/.pyenv/shims:/usr/local/bin:/usr/bin:$PATH python3 scripts/gov_policy_fetch.py --mode incremental >> logs/gov_policy_fetch.log 2>&1
```

**已知教训（2026-09-01 修复）**：三个脚本的 `load_db_config()` 曾凭记忆假设 `env.json` 顶层有 `mysql`/`database` 键，实际结构是 `{"ENV": "test", "quant": {"test": {host,port,user,password,database}, "prod": {...}}}`（与 `quant_core::env::load()` 一致）。旧代码读不到配置会静默退化为 `root`+空密码连接失败，导致三条管道实际从未真正跑通增量抓取。已修复为正确解析 `env_cfg["ENV"]` → `quant_cfg[active_env]`。

---

## 十一、数据流（Rust 版）

```
FMP / FRED / Tushare API
     ↓ rate-limited HTTP (reqwest + tokio)
PostgreSQL (sqlx upsert)
     ↓ parquet export (定期)
cache/*.parquet
     ↓ DataCache::build (内存)
quant_factors (71 美股 + A 股因子)
     ↓
quant_strategy (scoring + MVO + regime)
     ↓
quant_backtest (T+0 引擎 + FF5 回归)
     ↓
output/*.csv (NAV / signals / FM / IC)
```

完成全部加载后，因子计算 / 回测 / 因子分析全部内存执行，**无 DB 查询**。
