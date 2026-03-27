# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目概述

A股+美股多因子量化系统，覆盖数据采集、因子计算、组合构建、风控、回测、模拟交易、舆情爬取和报告生成。美股采用多空对冲策略（Long-Short Equity，IC 引导因子权重 + 低覆盖度股票池 450 只），10 年回测（2015-2025，含幸存者偏差修正）FF5 alpha 10.5%/年（t=3.05，p<0.01），年化 17.6% 跑赢 Russell 1000 6.2%/年。

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
python3 backend/cli.py backtest --market us --start 2020-01-01 # 美股回测 (Alpha, 默认)
python3 backend/cli.py backtest --market us --strategy-type beta # 美股回测 (Beta)
python3 backend/cli.py backtest --market us --strategy-type baseline --start 2015-01-01 # 美股回测 (Baseline VQM L/S)
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

美股管道（三策略：Alpha 多空对冲 + Beta Regime 控制 + Baseline VQM 验证）:
  yfinance → data/fmp_downloader.py → MySQL (us_* 表)
  SEC EDGAR → data/edgar_downloader.py → us_financial_data (2010起全量历史财报)
  SimFin → data/simfin_downloader.py → us_financial_data (补充)
  FRED → data/fred_downloader.py → us_macro_indicator 表
  FF5 → strategy/ff5.py → Fama-French 五因子回归分析
  → data/us_cleaner.py → us_factors/*.py (23因子×7大类) → us_factors/processor.py
  → strategy/us_regime.py (四维复合 + credit veto)
    → Alpha:    strategy/us_multi_factor.py (多空) → risk/us_risk_manager.py
    → Beta:     strategy/us_beta_strategy.py (Regime→仓位, 质量筛选等权)
    → Baseline: strategy/us_baseline_strategy.py (VQM 3因子, 纯静态 dollar-neutral)
  → strategy/us_backtest.py (T+0,借券费,risk_controls开关) | execution/us_paper_trader.py
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

### 美股因子体系（23 因子 × 7 大类，`services/us_factors/`）

| 大类 | 权重 | 因子 |
|------|------|------|
| value | 1.0 | EP, BP, DIV_YIELD, BUYBACK_YIELD |
| quality | 1.0 | ROE_TTM, GROSS_MARGIN, PROFIT_STB, MARGIN_TREND, ACCRUALS |
| growth | 1.0 | NET_PROFIT_YOY, REVENUE_YOY, NET_PROFIT_CAGR_3Y |
| momentum | 1.0 | MOM_1M, MOM_3M, MOM_12M, REV_5D |
| technical | 1.0 | TURN_20D, VOL_20D, IVOL, SIZE |
| analyst | 1.0 | US_ANALYST_RATING, US_ANALYST_COVERAGE |
| sentiment | 1.0 | POLYMARKET_SENT |

等权合成，两层类别打分（类内动态分母 + 类间加权），不做 IC 引导权重优化。

**剪枝记录**（leave-one-out alpha 分析，2015-2023）：
- ~~RESIDUAL_MOM~~: Δα=-3.46%（与 MOM_1M/3M/12M 冗余，信号更嘈杂）
- ~~VOL_PRICE_DIV~~: Δα=-4.30%（美股大盘股无量价背离信号）
- ~~4×MACRO~~: Δα=-0.25%（截面同值，无个股区分力，macro 大类整体移除）

### 美股回测绩效（2015-2025，含幸存者偏差修正，基准 Russell 1000）

| 指标 | Alpha v2 (23因子) | Alpha v1 (29因子) | Beta | Russell 1000 |
|------|-------------------|-------------------|------|-------------|
| 年化收益 | **17.2%** | 12.8% | 6.9% | 11.4% |
| 最大回撤 | -29.8% | **-16.3%** | -16.5% | — |
| Sharpe | **0.72** | 0.68 | 0.33 | — |
| FF5 Alpha | **+6.73%** (t=2.20) | +6.69% (t=2.26) | +0.88% | — |
| Market Beta | 0.82 | 0.40 | — | — |
| 超额年化 | **+7.53%** | +1.41% | -4.5% | — |

> **Alpha v2**: 23 因子（纯线性，剪掉 6 个有害因子）+ 月频调仓。α=6.73%(t=2.20 显著)，回撤 -29.8% 待优化。ML(LightGBM) 代码已集成但 train() 未调用，当前结果为纯线性。
> **Alpha v1**: 29 因子多空对冲（net exposure 60%），20 日调仓，风控完善（剪枝前版本）。
> **Beta**: Regime 择时 + 质量筛选，追求稳健。
> **Baseline**: EP+ROE+MOM_12_1 三因子 dollar-neutral（top/bottom 10%），纯静态无风控覆盖。2016-2023 价值因子历史最差十年，baseline 年化 -12% 符合 AQR 公开因子数据，已验证引擎和数据管道正确。
> 含幸存者偏差修正（227 只历史 S&P 500 成分股）。

### Alpha v2 开发路线（当前）

阶梯式重构：每一步对比 FF5 alpha 增量，确保改进可量化。

| 阶段 | 内容 | 结果 |
|------|------|------|
| Step 1 | 4 因子 dollar-neutral baseline | ✅ 失败：alpha=-15%，dollar-neutral 在 2016-2023 不可行 |
| Step 2 | +Regime 动态净敞口（4因子） | ✅ alpha 转正但仅 +1%（t=0.31），4 因子选股力不足 |
| Step 3 | 29 因子 + v1 选股逻辑 | ✅ alpha=+3.62%（t=0.71） |
| Step 3.5 | Leave-one-out 因子剪枝（29→23因子） | ✅ **alpha=+6.73%（t=2.20 显著），年化 17.2%** |
| Step 4 | 截面风控（GICS 行业中性 + beta 约束），替代时序风控 | 📋 |
| Step 4.5 | 2024-2025 样本外验证 | 📋 |
| Step 5 | 盈利预期修正因子 + LLM 舆情增强 | 📋 |

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

- A股策略算法：[A_SHARE_STRATEGY.md](A_SHARE_STRATEGY.md)
- 美股策略算法：[US_SHARE_STRATEGY.md](US_SHARE_STRATEGY.md)
- Polymarket 策略：[PollyMarket_STRATEGY.md](PollyMarket_STRATEGY.md)
- Polymarket P&L 分析：[POLYMARKET_PNL_ANALYSIS.md](POLYMARKET_PNL_ANALYSIS.md)

### A股开发历史

| 阶段 | 模块 | 状态 |
|------|------|------|
| Phase 1 | 数据层（配置/ORM/下载/清洗/更新） | ✅ |
| Phase 2 | 因子层（30 因子 + 处理流水线 + IC 评估） | ✅ |
| Phase 3 | 策略层（分类复合评分 + 回测引擎） | ✅ |
| Phase 4 | 风控层（个股/行业上限 + 回撤/波动率） | ✅ |
| Phase 5 | 执行层（PaperTrader + BaseTrader） | ✅ |
| Phase 6 | 监控层（绩效追踪 + HTML 报告） | ✅ |
| Phase 7-8 | 因子增强 + 算法升级（4 新因子 + Z-score clip） | ✅ |
| Phase 8 | 舆情层（16 源爬虫 + LLM 分析 + 舆情因子化） | ✅ |
| Phase 9-12 | 参数调优 + 商品/宏观因子 + Regime 切换 + Softmax | ✅ |
| Phase 13-14 | Polymarket + 美股数据接入 | ✅ |
| Phase 15-20 | 性能优化 + 信号增强 + 预加载架构 | ✅ |
| Phase 21-24 | 回测优化 + 自适应调仓 + 因子质量增强 | ✅ |
| 美股量化 | 29 因子多空对冲 + FF5 回归 + 幸存者偏差修正 | ✅ |
| 美股 Alpha v1 | 29 因子多空对冲 + Regime 动态净敞口（剪枝前） | ✅ |
| 美股 Baseline | VQM 3 因子 dollar-neutral 验证（引擎+数据管道已验证） | ✅ |
| 数据清洗 | eps/roe 脏数据修复 + EDGAR/SimFin 单位统一 + ROE 重算 | ✅ |
| **美股 Alpha v2** | **阶梯式重构：clean baseline → Regime → 风控 → LLM 因子** | **🔨** |
| 待办 | 券商实盘对接 (QMT/Ptrade) / Insider 因子 | 📋 |
