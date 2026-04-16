# 数据源说明

本文档说明系统所有外部数据的来源、获取方式和用途。

> **架构注解（A 股 P1 迁移已完成）：**
> - A 股下载器：`stocks/services/downloaders/a_tushare_*.py + a_akshare_*.py`（8 文件）
> - A 股全字段保留：Tushare 端点不指定 `fields=`，财报拆 4 表（income/balance/cashflow/indicator）
> - 美股下载器：`stocks/services/downloaders/{fmp,bulk,fred,edgar,...}.py`（待重命名为 `us_*.py` 对齐 A 股）
> - DDL：`scripts/migrate_ashare_schema.sql`（drop+recreate 20 张 A 股表）

---

## 一、A股市场数据

### Tushare Pro (`stocks/services/downloaders/a_tushare_*.py`)

| 数据 | API 调用 | 频率 | 用途 |
|------|---------|------|------|
| 沪深 A 股列表 | `pro.stock_basic()` | 按需 | 股票池、退市/ST 过滤 |
| 日线行情 (OHLCV) | `pro.daily()` | 日 | 行情、因子计算 |
| 每日指标 (换手率/估值/市值等) | `pro.daily_basic()` | 日 | TURN_20D 技术因子、DIV_YIELD 股息率因子、pe_ttm/pb/ps_ttm 估值、total_mv/circ_mv 市值、turnover_rate_f 自由流通换手率、volume_ratio 量比 |
| 复权因子 | `pro.adj_factor()` | 日 | 前复权价格计算 |
| 交易日历 | `pro.trade_cal()` | 按需 | 交易日判断 |
| 指数日线 | `pro.index_daily()` | 日 | 基准指数 |
| 申万行业指数 | `pro.sw_daily()` | 日 | IND_MOM 行业动量因子 |

- **依赖**: `TUSHARE_TOKEN` 环境变量
- **限速**: 180 req/min（可配置 `TUSHARE_RATE_LIMIT`）
- **起始日期**: `DATA_START_DATE`，默认 `20150101`

### Tushare 财务数据 (`stocks/services/downloaders/a_tushare_financials.py`)

| 数据 | API 调用 | 用途 |
|------|---------|------|
| 利润表 | `pro.income()` | EP、ROE_TTM、GROSS_MARGIN 等 |
| 资产负债表 | `pro.balancesheet()` | BP、SIZE |
| 现金流量表 | `pro.cashflow()` | 质量因子 |
| 财务审计 | `pro.fina_audit()` | 公告日（ann_date）防前视偏差 |

---

## 二、A股宏观数据

### Tushare 宏观指标 (`stocks/services/downloaders/a_tushare_macro.py`)

| 指标 | API 调用 | 频率 |
|------|---------|------|
| SHIBOR (隔夜/3M) | `pro.shibor()` | 日 |
| LPR (1年期) | `pro.shibor_lpr()` | 日 |
| CPI (同比) | `pro.cn_cpi()` | 月 |
| PPI (同比) | `pro.cn_ppi()` | 月 |
| PMI (制造业/新订单) | `pro.cn_pmi()` | 月 |
| M2/M1 货币供应 (同比) | `pro.cn_m()` | 月 |
| GDP (同比) | `pro.cn_gdp()` | 季 |
| 美债收益率 (10Y/2Y-10Y 利差) | `pro.us_tycr()` | 日 |

- **存储表**: `macro_indicator`
- **用途**: MACRO_CYCLE、MACRO_LIQD、MACRO_INFL、MACRO_EXTR 四个宏观因子

---

## 三、A股商品期货

### Tushare 期货 (`stocks/services/downloaders/a_tushare_commodity.py`)

| API 调用 | 说明 |
|---------|------|
| `pro.fut_mapping()` | 主力合约映射 |
| `pro.fut_daily()` | 期货日线 (OHLC/结算价/持仓量) |

**15 个品种**:

| 分类 | 品种 | 交易所 |
|------|------|--------|
| 贵金属 | AU (黄金)、AG (白银) | 上期所 |
| 工业金属 | CU (铜)、AL (铝)、ZN (锌)、PB (铅)、NI (镍)、SN (锡) | 上期所 |
| 黑色系 | RB (螺纹钢)、I (铁矿石)、J (焦炭)、JM (焦煤) | 上期所/大商所 |
| 能源 | SC (原油) | 上海国际能源交易中心 |
| 化工 | SA (纯碱)、MA (甲醇) | 郑商所 |

- **存储表**: `commodity_price`
- **用途**: CMDTY_MOM 商品动量因子

---

## 四、美股市场数据

统一下载器：`stocks/services/downloaders/bulk.py`（美股 FMP 全源）（六源：FMP/UW/Fiscal.ai/Quiver/AlphaVantage/FRED）

### 4.1 FMP (Financial Modeling Prep) — 主力数据源

配置：`FMP_API_KEY`（Ultimate $149/月，3000 req/min，bulk 按年独立限流）

| 数据 | 端点 | 方式 | 表 |
|------|------|------|-----|
| 全市场股票列表 | stock-screener | per-ticker | us_stock_basic |
| 公司详情（快照） | **stable/profile** | per-ticker | us_company_profile |
| 日线行情 | historical-price-full | per-ticker 5/批 | us_daily_price |
| 季度财报 (IS+BS+CF) | stable/income-statement + balance-sheet + cash-flow | per-ticker 季度 | us_financial_data |
| Key Metrics | stable/key-metrics | per-ticker 季度 | us_key_metric |
| Financial Ratios | stable/ratios | per-ticker 季度 | us_key_metric（共享表，COALESCE upsert） |
| Enterprise Values（含历史市值） | stable/enterprise-values | per-ticker 季度 | us_enterprise_value |
| Financial Growth | stable/financial-growth | per-ticker 季度 | us_financial_growth |
| Owner Earnings | stable/owner-earnings | per-ticker | us_owner_earnings |
| Earnings Surprise | **stable/earnings** | per-ticker | us_earnings_surprise |
| EPS Consensus | **v3/analyst-estimates** | per-ticker | us_eps_estimate |
| Insider Trading (Form 4) | insider-trading (v4) | per-ticker 分页 2003+ | us_insider_trade |
| Insider Statistics | stable/insider-trade-statistics | per-ticker | us_insider_statistic |
| GICS 行业 | stock-screener | per-ticker | us_industry_class |
| 分红/拆股 | stock_dividend, stock_split | per-ticker | us_corporate_action |
| 分析师评级变更 | grade (v3) | per-ticker | us_analyst_recommendation |
| ESG 评级 | stable/esg-environmental-social-governance-data | per-ticker | us_esg_rating |
| 员工数 | stable/employee-count | per-ticker | us_employee_count |
| 国会交易 | senate-trading (v4) + house-disclosure (v4) | per-ticker | us_congress_trade |
| 指数日线 | historical-price-full | per-ticker | us_index_daily |
| 指数成分历史 | sp500_constituent + nasdaq_constituent + historical | per-ticker | us_index_constituent |
| 商品日线 | historical-price-full | per-ticker (GC=F→GCUSD) | us_commodity_price |
| 宏观经济 | economic (v4), treasury (v4) | per-ticker | us_macro_indicator |
| 退市公司 | delisted-companies (v3) | bulk | us_delisted |
| 代码变更 | symbol_change (v4) | bulk | us_symbol_change |

> **已废弃端点**:
> - ~~historical-market-capitalization~~: Ultimate plan 只有 ~90 天，`from`/`to` 需 Enterprise plan（402）。历史市值改用 `us_enterprise_value.market_capitalization`（季度精度，1983-至今）。
> - ~~v3/earnings-surprises/{ticker}~~: 字段名不匹配 DB（`actualEarningResult` vs `epsActual`），已切换到 `stable/earnings`。
> - ~~v3/profile/{ticker} batch~~: 字段名不匹配 DB（`mktCap` vs `marketCap`），已切换到 `stable/profile`。
>
> **COALESCE upsert**: Key Metrics 和 Ratios 共享 `us_key_metric` 表。`database.py:upsert()` 使用 `COALESCE(EXCLUDED.col, table.col)` 确保后写入端点不覆盖前者字段为 NULL。

### 4.2 Unusual Whales — 替代数据

配置：`UW_API_KEY`（$150/月，100+ 端点）

| 数据 | 端点 | 表 |
|------|------|-----|
| 期权异常活动 | /api/option-trades/flow-alerts | us_options_flow |
| 暗池交易 | /api/darkpool/recent | us_dark_pool |
| 国会交易 | /api/congress/recent-trades | us_congress_trade |
| 新闻 | /api/news/headlines | us_news |

### 4.3 Fiscal.ai — 日频估值

配置：`FISCAL_API_KEY`（$99/月）

| 数据 | 端点 | 表 |
|------|------|-----|
| 日频 PE/PB/EV | /v1/daily-ratios | us_daily_ratio |

### 4.4 Quiver Quantitative — 政治/另类数据

配置：`QUIVER_API_KEY`（Hobbyist $10/月）

| 数据 | 端点 | 表 |
|------|------|-----|
| 游说活动 | historical/lobbying/{ticker} | us_lobbying | ✅ 已导入（223k 行, 1546 ticker, 1999-2026）|
| 政府合同 | historical/govcontracts/{ticker} | us_gov_contract | ✅ 已导入（36k 行, 1415 ticker, 2008-2026）|
| ~~WSB 情绪~~ | ~~historical/wallstreetbets/{ticker}~~ | ~~us_wsb_sentiment~~ | 已废弃（只有 3 个 ticker，无截面区分力）|

### 4.5 Alpha Vantage — 新闻情绪/期权数据

配置：`ALPHAVANTAGE_API_KEY`（Premium $99-150/月）

| 数据 | 端点 | 表 |
|------|------|-----|
| AI 新闻情绪 | NEWS_SENTIMENT | us_news_sentiment |
| 期权快照（IV/Greeks 聚合） | HISTORICAL_OPTIONS | us_options_snapshot |

---

## 五、美国宏观数据

### FMP 宏观端点（主力）

FMP `/api/v4/economic` 和 `/api/v4/treasury` 提供 GDP、CPI、失业率、国债收益率等主要宏观指标。

### FRED（补充）

`stocks/services/downloaders/fred.py`（待重命名为 us_fred.py），通过 `fredapi` 库补充 FMP 未覆盖的指标（VIX、TED 利差、DXY 等）。

| 指标代码 | FRED Series | 说明 | 频率 |
|----------|-------------|------|------|
| US_GDP | GDP | 美国 GDP | 季 |
| US_CPI_YOY | CPIAUCSL | CPI | 月 |
| US_CORE_CPI | CPILFESL | 核心 CPI | 月 |
| US_PPI | PPIACO | PPI | 月 |
| US_UNEMP | UNRATE | 失业率 | 月 |
| US_NONFARM | PAYEMS | 非农就业人数 | 月 |
| US_FED_RATE | FEDFUNDS | 联邦基金利率 | 日 |
| US_M2 | M2SL | M2 货币供应量 | 月 |
| US_10Y | DGS10 | 10 年期美债收益率 | 日 |
| US_2Y | DGS2 | 2 年期美债收益率 | 日 |
| US_2Y10Y | T10Y2Y | 10Y-2Y 利差 | 日 |
| US_VIX | VIXCLS | VIX 波动率指数 | 日 |
| US_DXY | DTWEXBGS | 美元指数 | 日 |
| US_INIT_CLAIMS | ICSA | 首次申领失业金 | 周 |
| US_PCE | PCEPI | PCE 价格指数 | 月 |

- **依赖**: `FRED_API_KEY` 环境变量
- **存储表**: `us_macro_indicator`

---

## 六、券商研报

### AKShare / 东方财富 (`stocks/services/downloaders/a_akshare_reports.py`)

| 数据 | API 调用 | 说明 |
|------|---------|------|
| 券商研报 | `akshare.stock_research_report_em()` | 东方财富研报数据 |

- **字段**: 报告日期、股票代码、分析师、机构、评级（买入 5.0 → 卖出 1.0）
- **直接 API**: `https://reportapi.eastmoney.com/report/list`
- **存储表**: `research_report`
- **因子用途**: ANALYST_RATING（共识评级）、ANALYST_COVERAGE（覆盖度）

---

## 七、舆情数据

### 政策新闻爬虫 (`sentiment/services/scrapers/`)

16 个爬虫分 5 个层级，由 `SentimentDownloader` 统一调度。

**Tier 1 — 最高层（国家级）**

| 来源 | 文件 | 获取方式 |
|------|------|---------|
| 中国政府网 (gov.cn) | `gov_cn.py` | JSON API `gov.cn/zhengce/zuixin/ZUIXINZHENGCE.json` |
| 新华社 | `xinhua.py` | HTML 爬取 |
| 人民网 | `people.py` | HTML 爬取 |
| CCTV 新闻联播 | `cctv.py` | AKShare `ak.news_cctv(date)` 获取文字稿 |

**Tier 2 — 产业层**

| 来源 | 文件 | 获取方式 |
|------|------|---------|
| 国家发改委 (NDRC) | `ndrc.py` | HTML 爬取 |
| 工信部 (MIIT) | `miit.py` | HTML 爬取 |
| 商务部 (MOFCOM) | `mofcom.py` | HTML 爬取 |
| 巨潮公告 (cninfo) | `cninfo.py` | POST API `cninfo.com.cn/new/hisAnnouncement/query` |

**Tier 3 — 金融监管**

| 来源 | 文件 | 获取方式 |
|------|------|---------|
| 证监会 (CSRC) | `csrc.py` | HTML 爬取 |
| 央行 (PBC) | `pbc.py` | HTML 爬取 |
| 国家金融监督管理总局 (NFRA) | `nfra.py` | HTML 爬取 |

**Tier 4 — 专项行业**

| 来源 | 文件 | 获取方式 |
|------|------|---------|
| 国家能源局 (NEA) | `nea.py` | HTML 爬取 |
| 住建部 (MOHURD) | `mohurd.py` | HTML 爬取 |

**Tier 5 — 美国政策 (Twitter/X)**

| 来源 | 文件 | 获取方式 |
|------|------|---------|
| @realDonaldTrump | `twitter_trump.py` | twikit 库（免费登录方式） |
| JD Vance | `twitter_vance.py` | twikit 库 |
| Marco Rubio | `twitter_rubio.py` | twikit 库 |

- **依赖**: `TWITTER_USERNAME` / `TWITTER_EMAIL` / `TWITTER_PASSWORD`（可选，缺失时跳过）
- **限速**: Twitter 90 req/min，其他网站 600 req/min/域名
- **存储表**: `policy_article`

### 舆情分析 (`sentiment/services/scrapers/analyzer.py`)

两层分析管道：

1. **关键词分析** (`keyword_analyzer.py`) — 基于 `INDUSTRY_KEYWORDS` 字典匹配行业关键词，计算初始 intensity
2. **LLM 增强** (`llm_analyzer.py`) — 对 intensity >= 0.5 的文章调用 LLM 做深度分析

LLM 支持两种后端:
- **Anthropic Claude**: `LLM_PROVIDER="anthropic"`，默认模型 `claude-haiku-4-5-20251001`
- **OpenAI 兼容** (DeepSeek/通义千问等): `LLM_PROVIDER="openai"`，可配置 `LLM_API_BASE`

- **存储表**: `policy_analysis`
- **因子用途**: POLICY_SENT（政策情感）、POLICY_INTENSITY（政策力度）

---

## 八、环境变量汇总

| 变量 | 必需 | 说明 |
|------|------|------|
| `TUSHARE_TOKEN` | A 股必需 | Tushare Pro API token |
| `FMP_API_KEY` | 美股必需 | FMP Ultimate ($149/月) |
| `UW_API_KEY` | 美股可选 | Unusual Whales ($150/月) |
| `FISCAL_API_KEY` | 美股可选 | Fiscal.ai ($99/月) |
| `QUIVER_API_KEY` | 美股可选 | Quiver Quantitative ($10/月) |
| `ALPHAVANTAGE_API_KEY` | 美股可选 | Alpha Vantage Premium ($99-150/月) |
| `FRED_API_KEY` | 美股宏观补充 | FRED API key |
| `TWITTER_USERNAME` / `TWITTER_EMAIL` / `TWITTER_PASSWORD` | 可选 | Twitter 爬虫登录凭证 |
| `LLM_PROVIDER` | 可选 | `anthropic` 或 `openai` |
| `LLM_API_KEY` | 可选 | LLM API key |
| `LLM_MODEL` | 可选 | LLM 模型名 |
| `LLM_API_BASE` | 可选 | OpenAI 兼容 API 地址 |
| MySQL 连接 | 必需 | `MYSQL_HOST` / `MYSQL_PORT` / `MYSQL_USER` / `MYSQL_PASSWORD` / `MYSQL_DATABASE` |

---

## 九、数据流总览

```
┌─ A股 ───────────────────────────────────────────────────┐
│  Tushare Pro ──→ 股票列表/日线/财务/指数/行业 ──→ MySQL │
│  Tushare Pro ──→ 宏观指标 (SHIBOR/CPI/PPI/M2...) ──→ MySQL │
│  Tushare Pro ──→ 商品期货 (15品种主力合约) ──→ MySQL    │
│  AKShare ────→ 券商研报 (东方财富) ──→ MySQL            │
├─ 美股 ──────────────────────────────────────────────────┤
│  FMP bulk ───→ metrics/earnings/estimates (按年 1995+)     │
│  FMP ticker ─→ 季度财报(IS+BS+CF)/行情/行业/insider/分析师 │
│  FMP ticker ─→ 分红拆股/指数/商品/宏观/SP500+NQ100成分    │
│  UW ─────────→ 期权flow/暗池/国会交易/新闻 ──→ MySQL    │
│  Fiscal.ai ──→ 日频 PE/PB/EV ──→ MySQL                 │
│  Quiver ─────→ 游说活动/政府合同/WSB情绪 ──→ MySQL      │
│  FRED ───────→ 宏观指标补充 ──→ MySQL                   │
├─ 舆情 ──────────────────────────────────────────────────┤
│  16 个爬虫 ──→ 政策文章 ──→ MySQL                       │
│  关键词+LLM ──→ 行业情感分析 ──→ MySQL                  │
└─────────────────────────────────────────────────────────┘
          ↓
   A股: 因子计算 → 选股 → 回测/模拟交易（详见 A_SHARE_STRATEGY.md）
   美股: 31因子多空对冲 + FF5 回归（详见 US_SHARE_STRATEGY.md）
```
