# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目概述

A股+美股多因子量化系统，覆盖数据采集、因子计算、组合构建、风控、回测、模拟交易、舆情爬取和报告生成。美股采用多空对冲策略（Long-Short Equity），10 年回测（2015-2025，含幸存者偏差修正）FF5 alpha 5.4%/年（t=1.69），年化 12.1% 跑赢 Russell 1000 0.8%/年。

三层架构：核心业务逻辑在 `backend/services/`，上层有三个入口调用同一套 service：
- **Django API** (`backend/api/views/`) — 前端 HTTP 调用
- **CLI** (`backend/cli.py`) — 命令行调试/运维
- **前端** (`frontend/`) — Vue 3 + Naive UI 仪表盘

## 常用命令

```bash
# 安装后端依赖
cd backend && pip install -r requirements.txt

# 启动服务（开发）
./start.sh                                        # 启动后端 + 前端 + 安装 cron

# CLI（推荐用于调试，直接调 service 层，无需启动服务器）
python3 backend/cli.py db status                               # 查看全表数据状态
python3 backend/cli.py data update --market us                 # 增量更新美股数据
python3 backend/cli.py select --market us --date 2025-01-15    # 美股选股
python3 backend/cli.py select --market cn --date 2025-01-15    # A股选股
python3 backend/cli.py backtest --market us --start 2020-01-01 # 美股回测
python3 backend/cli.py factor calc MOM_1M --market us          # 计算单因子
python3 backend/cli.py factor list --market us                 # 列出所有因子
python3 backend/cli.py score AAPL --date 2025-01-15            # 单只股票得分
python3 backend/cli.py paper status --market us                # 模拟账户状态
python3 backend/cli.py paper trade --market us                 # 执行模拟交易

# API 调用（通过 HTTP，需要服务器运行）
curl -X POST http://localhost:8000/api/data/update            # A股增量更新
curl -X POST http://localhost:8000/api/us/select              # 美股选股
curl -X POST http://localhost:8000/api/us/backtest/run \
  -H 'Content-Type: application/json' \
  -d '{"start_date":"2020-01-01","end_date":"2025-12-31"}'    # 美股回测
```


## 系统架构

```
┌─────────────┐   ┌─────────────┐   ┌─────────────┐
│   CLI       │   │ Django API  │   │  Frontend   │
│ backend/    │   │ backend/api/│   │ frontend/   │
│ cli.py      │   │ views/      │   │ src/        │
└──────┬──────┘   └──────┬──────┘   └──────┬──────┘
       │                 │                 │
       └────────┬────────┘                 │
                ▼                          │
    backend/services/  ◄───────────────────┘ (via HTTP)
    （唯一业务逻辑层）

A股管道:
  Tushare → data/{downloader,updater}.py → MySQL
  → data/cleaner.py → factors/*.py (30因子) → factors/processor.py
  → strategy/regime.py → strategy/multi_factor.py → risk/risk_manager.py
  → strategy/backtest.py | execution/paper_trader.py

美股管道（多空对冲，FF5 alpha 5.4%/年，t=1.69，含幸存者偏差修正）:
  yfinance → data/fmp_downloader.py → MySQL (us_* 表)
  SEC EDGAR → data/edgar_downloader.py → us_financial_data (2010起全量历史财报)
  SimFin → data/simfin_downloader.py → us_financial_data (补充)
  FRED → data/fred_downloader.py → us_macro_indicator 表
  FF5 → strategy/ff5.py → Fama-French 五因子回归分析
  → data/us_cleaner.py → us_factors/*.py (29因子) → us_factors/processor.py
  → strategy/us_regime.py (三维复合) → strategy/us_multi_factor.py (多空) → risk/us_risk_manager.py
  → strategy/us_backtest.py (T+0,借券费) | execution/us_paper_trader.py
```

**前端页面:**
- A股: 选股(`/select`)、回测(`/backtest`)、模拟交易(`/paper`)
- 美股: 选股(`/us/select`)、回测(`/us/backtest`)、模拟交易(`/us/paper`)
- 公共: 仪表盘(`/`)、数据管理(`/data`)、自选股(`/watchlist`)、设置(`/settings`)

**舆情管道：** `services/sentiment/scrapers/` 下 11 个中国政府网站爬虫 + CCTV新闻联播（AKShare）+ 巨潮公告 + 3 个 Twitter/X 美国政策爬虫（Trump/Vance/Rubio）+ Polymarket 预测市场桥接，共 20 个爬虫，由 `services/sentiment/downloader.py` 调度，`HttpRateLimiter` 实现按域名限速。CCTV 爬虫通过 AKShare `news_cctv()` 获取新闻联播文字稿（Tier 1）。巨潮公告爬虫通过 cninfo POST API 获取上市公司公告标题（Tier 2）。Twitter 爬虫使用 twikit 库（免费，需 `TWITTER_USERNAME`/`TWITTER_EMAIL`/`TWITTER_PASSWORD`），独立限速器（90 req/min），缺少凭证或 twikit 未安装时优雅降级（跳过，不报错）。Polymarket 桥接爬虫从 `polymarket_alert` 表读取已有 LLM 分析的 alert，直接注入 `policy_article` + `policy_analysis`（Tier 8，`SKIP_ANALYSIS_SOURCES` 跳过重复分析）。

**券商研报管道：** `services/data/akshare_downloader.py` 通过 AKShare `stock_research_report_em()` 下载东方财富券商研报数据，存入 `research_report` 表。`services/factors/research.py` 提供 ANALYST_RATING（共识评级）和 ANALYST_COVERAGE（覆盖度）两个因子，直接按 ts_code 匹配（无需行业映射），归入 sentiment 大类。

**舆情因子化管道：** `services/sentiment/analyzer.py` 调度两层分析（`keyword_analyzer.py` 关键词底层 + `llm_analyzer.py` LLM 增强层），分析结果存入 `policy_analysis` 表。`services/factors/sentiment.py` 将行业级情感得分映射到个股（POLICY_SENT + POLICY_INTENSITY），注册到 sentiment 大类（权重 0.6）。两个舆情因子通过 `_get_sentiment_data()` 共享缓存避免重复 DB 查询。LLM 仅对 keyword intensity ≥ 0.5 的文章调用，无 API key 时优雅降级。

### 核心设计决策

- **无未来数据泄露：** 财务数据始终按 `ann_date <= date`（公告日）过滤，而非报告期。收盘价和市值取信号日当天数据。
- **两层因子打分：** 类内使用动态分母（缺失因子等比缩减权重）；类间使用动态分母（缺失大类权重按比例再分配给有值大类），`MIN_VALID_CATEGORIES=4` 限制最大膨胀。
- **Upsert 语义：** 所有数据库写入为幂等操作（唯一键冲突时 insert-or-update）。
- **可配置中性化：** `NEUTRALIZE_MODE = full | size_only | none` 控制 OLS 行业+市值残差中性化；`CATEGORY_NEUTRALIZE_OVERRIDES` 支持按大类覆盖（默认 momentum/macro/sentiment → size_only 保留行业 alpha）。
- **Regime 切换：** CSI 300 60 日 MA ±5% 渐进式切换（线性插值，避免 whipsaw），熊市时降低价值（0.7→0.6）/动量（0.9→0.6）、提高质量（1.3→1.5）/成长（1.0→0.8）/技术（0.7→1.0），可通过 `REGIME_ENABLED=0` 关闭。
- **价值陷阱惩罚：** value 大类得分 > 0 且 quality 大类得分 < -0.5 时，压缩 value 得分，避免买入低估值但基本面恶化的股票（如地产链）。
- **趋势门槛过滤：** MOM_12M < -1.0 的股票最终得分乘以衰减系数，防止买入持续下跌的股票。
- **关联行业组上限：** `INDUSTRY_GROUPS` 定义关联行业组（地产链/金融/TMT），`MAX_INDUSTRY_GROUP_WEIGHT=30%` 限制同一产业链合计权重。
- **自适应调仓：** 半月频基准 + 偏离度触发。每隔 `REBALANCE_CHECK_INTERVAL`（默认5日）检查 Top-N 持仓变化，新股占比 ≥ `REBALANCE_DEVIATION_THRESHOLD`（默认40%）时触发额外调仓，`REBALANCE_MIN_INTERVAL`（默认5日）防止过度交易。平静期可能月调一次，剧烈变化时几天就能响应。
- **T+1 执行模型：** 先卖后买。涨停股排除在买入之外；跌停股加入 `pending_sells` 队列下一交易日重试。
- **可插拔交易后端：** `BaseTrader` ABC + `main.py::_create_trader()` 工厂方法，目前仅实现 `PaperTrader`。
- **daily_basic 全量字段：** daily_price 表扩展 8 列（dv_ttm, pe_ttm, pb, ps_ttm, total_mv, circ_mv, turnover_rate_f, volume_ratio），Tushare daily_basic 全量下载并 backfill 历史数据。
- **财务数据时效性衰减：** 财务因子（EP/BP/ROE_TTM 等 9 个）按报告期距今时间衰减（≤3m: 100%, 3-6m: 50%, 6-9m: 25%, >9m: -1.0 负面信号）。延迟发布季报视为负面。
- **缺失因子惩罚：** 缺失因子 > 20% 时线性压缩最终得分，最大惩罚 50%，防止动态分母导致得分虚高。
- **回测预加载架构：** `FactorBase.preload_for_backtest()` 一次性加载 financial_data + daily_price + policy_analysis 到内存，因子计算全部从内存过滤（单日 ~2.1s，1 年 25 日 ~68s）。

### A股因子体系（30 个因子，`services/factors/`）

| 大类 | 权重 | 因子 |
|---|---|---|
| 价值 | 0.7 | EP, BP, DIV_YIELD |
| 质量 | 1.3 | ROE_TTM, GROSS_MARGIN, PROFIT_STB, MARGIN_TREND |
| 成长 | 1.0 | NET_PROFIT_YOY, REVENUE_YOY, NET_PROFIT_CAGR_3Y |
| 动量 | 0.9 | MOM_1M, MOM_3M, MOM_12M, REV_5D, IND_MOM, RESIDUAL_MOM, CMDTY_MOM |
| 技术 | 0.7 | TURN_20D, VOL_20D, PRICE_DEV_60D, SIZE, VOL_PRICE_DIV |
| 宏观 | 0.6 | MACRO_CYCLE, MACRO_LIQD, MACRO_INFL, MACRO_EXTR |
| 舆情 | 0.6 | POLICY_SENT, POLICY_INTENSITY, ANALYST_RATING, ANALYST_COVERAGE |

### 美股因子体系（29 个因子，`services/us_factors/`）

| 大类 | 权重 | 因子 |
|---|---|---|
| 价值 | 0.8 | EP, BP, DIV_YIELD, BUYBACK_YIELD |
| 质量 | 1.3 | ROE_TTM, GROSS_MARGIN, PROFIT_STB, MARGIN_TREND, ACCRUALS |
| 成长 | 1.1 | NET_PROFIT_YOY, REVENUE_YOY, NET_PROFIT_CAGR_3Y |
| 动量 | 1.0 | MOM_1M, MOM_3M, MOM_12M, REV_5D, RESIDUAL_MOM |
| 技术 | 0.7 | TURN_20D, VOL_20D, IVOL, SIZE, VOL_PRICE_DIV |
| 宏观 | 0.6 | US_MACRO_CYCLE, US_MACRO_LIQD, US_MACRO_INFL, US_MACRO_EXTR |
| 分析师 | 0.5 | US_ANALYST_RATING, US_ANALYST_COVERAGE |
| 情感 | 0.4 | POLYMARKET_SENT |

### 美股回测绩效（2015-2025，含幸存者偏差修正，基准 Russell 1000）

| 指标 | 策略 | Russell 1000 |
|------|------|-------------|
| 总收益 | +252% | +226% |
| 年化收益 | 12.1% | 11.4% |
| 超额年化 | +0.77% | — |
| 夏普比率 | 0.58 | — |
| 最大回撤 | -22.7% | — |
| FF5 Alpha (年化) | +5.42% | — |
| FF5 t-stat | 1.69 (接近显著) | — |
| β_Mkt | 0.46 | — |
| 年化换手率 | 426% | — |

> 注：股票池含 S&P 500 历史成分股（227 只已移除公司），消除幸存者偏差。
> 数据源：yfinance 行情 + SEC EDGAR 财报 + SimFin + FRED 宏观。

## 配置

环境变量在 `backend/.env`（参考 `backend/.env.example`）。所有配置在 `backend/services/config.py` 中有默认值。

- **A股**: `TUSHARE_TOKEN`、`MAX_HOLDINGS`、`NEUTRALIZE_MODE`、`USE_VOL_TARGETING`、风控参数
- **美股**: `US_MAX_HOLDINGS`、`US_SLIPPAGE`、`US_REGIME_INDEX`、`US_CATEGORY_WEIGHTS` 等（均带 `US_` 前缀）
- **美股数据**: `SIMFIN_API_KEY`（SimFin 历史财报）、`FRED_API_KEY`（FRED 宏观）、SEC EDGAR（免费，无需 key）
- **舆情**: `TWITTER_USERNAME`/`TWITTER_EMAIL`/`TWITTER_PASSWORD`、`LLM_PROVIDER`/`LLM_API_KEY`/`LLM_MODEL`
- **数据库**: MySQL 连接信息

## 编码规范

### 三层同步规则（关键）

系统有三个入口层调用同一套 service：**CLI** (`backend/cli.py`)、**API** (`backend/api/views/`)、**前端** (`frontend/src/`)。修改时必须保持三层同步：

- **新增/修改 service 方法** → 同步更新 CLI 命令 + API view + 前端页面（如涉及）
- **新增 API 端点** → 同步在 CLI 中添加对应命令、在前端 `api/index.ts` 中添加函数
- **新增 CLI 命令** → 确认对应的 API 端点是否也需要（通常需要）
- **业务逻辑只写在 `backend/services/`** — CLI 和 API views 都是薄壳，只做参数解析 + 调用 service + 格式化输出，禁止在 CLI 或 API view 中编写业务逻辑
- **前端修改后必须 `pnpm build`** — Django 只提供 dist 静态文件
- **代码变动后同步更新文档** — 修改策略/因子/架构后，必须同步更新以下文档：
  - `CLAUDE.md`（本文件）— 项目概述、因子表、编码规范
  - `A_SHARE_STRATEGY.md` — A股策略算法文档
  - `US_SHARE_STRATEGY.md` — 美股策略算法文档

### 通用规范

- 所有模块使用 `logging.getLogger(__name__)`，日志级别取 `config.settings.LOG_LEVEL`
- Matplotlib 必须在导入 `pyplot` 前调用 `matplotlib.use("Agg")`
- 因子计算始终为截面（同一日期，全部股票）
- MySQL 列名 `open` 是保留字，原生 SQL 需用反引号转义
- 数据库层使用 SQLAlchemy ORM（`DeclarativeBase`）

### 市场差异

- **A股**: 申万行业分类、涨跌停处理、T+1 规则、`ts_code` 标识、`adj_factor` 复权
- **美股**: GICS 行业分类、无涨跌停、T+0 规则、`ticker` 标识、`adj_close` 复权、零佣金
- A股配置无前缀（`MAX_HOLDINGS`），美股配置带 `US_` 前缀（`US_MAX_HOLDINGS`）
- A股因子在 `services/factors/`，美股因子在 `services/us_factors/`（独立，不共享基类）

### 文档

- 在[A_SHARE_STRATEGY.md](A_SHARE_STRATEGY.md) 和[CONTINUE_PROMPT.md](CONTINUE_PROMPT.md)中记录变动
- 美股回测算法文档：[US_SHARE_STRATEGY.md](US_SHARE_STRATEGY.md)
- Polymarket 策略文档：[PollyMarket_STRATEGY.md](PollyMarket_STRATEGY.md)
- Polymarket P&L 分析结论：[POLYMARKET_PNL_ANALYSIS.md](POLYMARKET_PNL_ANALYSIS.md)
