# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目概述

A股多因子量化选股系统，覆盖数据采集、因子计算、组合构建、风控、回测、模拟交易、舆情爬取和报告生成的完整流程。

前后端分离架构：后端 `backend/`（Django + DRF），前端 `frontend/`（Vue 3 + Naive UI）。核心业务逻辑在 `backend/services/`。

## 常用命令

```bash
# 安装后端依赖
cd backend && pip install -r requirements.txt

# 启动服务（开发）
./start.sh                                        # 启动后端 + 前端 + 安装 cron

# API 调用（所有操作通过 backend API）
curl -X POST http://localhost:8000/api/data/update            # 增量更新
curl -X POST http://localhost:8000/api/data/backfill-income   # 补充利润表
curl -X POST http://localhost:8000/api/sentiment/download     # 舆情抓取
curl -X POST http://localhost:8000/api/data/download-reports   # 下载券商研报
curl http://localhost:8000/api/data/research-reports           # 查询券商研报
curl -X POST http://localhost:8000/api/paper/trade            # 执行 T+1 交易
curl -X POST http://localhost:8000/api/report/generate \
  -H 'Content-Type: application/json' \
  -d '{"start_date":"2020-01-01","end_date":"2024-12-31"}'    # 生成报告
```


## 系统架构

```
Tushare API → backend/services/data/{downloader,updater}.py → MySQL（11 张 ORM 表）
AKShare API → backend/services/data/akshare_downloader.py → research_report 表
  → services/data/cleaner.py（股票池过滤：退市、ST、上市天数、停牌、流动性、科创板）
  → services/factors/*.py（29 个因子，7 大类，均继承 FactorBase ABC）
  → services/factors/processor.py（MAD 去极值 → 按大类中性化 → 标准化(Z-Score/Rank) → 截断 ±3）
  → services/strategy/regime.py（CSI 300 60日MA ±5%渐进式切换 → 牛/熊大类权重插值）
  → services/strategy/multi_factor.py（两层打分 → Regime感知 → Top-N 选股 → Softmax 分配权重）
  → services/risk/risk_manager.py（个股/行业上限 → 线性回撤响应 / 波动率目标）
  → services/strategy/backtest.py 或 services/execution/paper_trader.py
  → services/monitor/{performance,report}.py

backend/api/views/ → Django REST Framework API 端点
backend/core/ → Django 配置（settings, urls, asgi）
frontend/ → Vue 3 + Naive UI 仪表盘
  数据管理页（/data）三个顶级 Tab：
    数据操作 — 快捷操作 + 10 类数据下载/更新表 + 舆情数据源概览（来源表+抓取/分析按钮）
    文章列表 — 舆情文章搜索/筛选/分页/详情抽屉
    数据表状态 — 全部 DB 表行数/最新日期
start.sh → 一键启动 + crontab 安装（cron 通过 curl 调用 API）
```

**舆情管道：** `services/sentiment/scrapers/` 下 11 个中国政府网站爬虫 + CCTV新闻联播（AKShare）+ 巨潮公告 + 3 个 Twitter/X 美国政策爬虫（Trump/Vance/Rubio）+ Polymarket 预测市场桥接，共 20 个爬虫，由 `services/sentiment/downloader.py` 调度，`HttpRateLimiter` 实现按域名限速。CCTV 爬虫通过 AKShare `news_cctv()` 获取新闻联播文字稿（Tier 1）。巨潮公告爬虫通过 cninfo POST API 获取上市公司公告标题（Tier 2）。Twitter 爬虫使用 twikit 库（免费，需 `TWITTER_USERNAME`/`TWITTER_EMAIL`/`TWITTER_PASSWORD`），独立限速器（90 req/min），缺少凭证或 twikit 未安装时优雅降级（跳过，不报错）。Polymarket 桥接爬虫从 `polymarket_alert` 表读取已有 LLM 分析的 alert，直接注入 `policy_article` + `policy_analysis`（Tier 8，`SKIP_ANALYSIS_SOURCES` 跳过重复分析）。

**券商研报管道：** `services/data/akshare_downloader.py` 通过 AKShare `stock_research_report_em()` 下载东方财富券商研报数据，存入 `research_report` 表。`services/factors/research.py` 提供 ANALYST_RATING（共识评级）和 ANALYST_COVERAGE（覆盖度）两个因子，直接按 ts_code 匹配（无需行业映射），归入 sentiment 大类。

**舆情因子化管道：** `services/sentiment/analyzer.py` 调度两层分析（`keyword_analyzer.py` 关键词底层 + `llm_analyzer.py` LLM 增强层），分析结果存入 `policy_analysis` 表。`services/factors/sentiment.py` 将行业级情感得分映射到个股（POLICY_SENT + POLICY_INTENSITY），注册到 sentiment 大类（权重 0.4）。LLM 仅对 keyword intensity ≥ 0.5 的文章调用，无 API key 时优雅降级。

### 核心设计决策

- **无未来数据泄露：** 财务数据始终按 `ann_date <= date`（公告日）过滤，而非报告期。收盘价和市值取信号日当天数据。
- **两层因子打分：** 类内使用动态分母（缺失因子等比缩减权重）；类间使用动态分母（缺失大类权重按比例再分配给有值大类），`MIN_VALID_CATEGORIES=4` 限制最大膨胀。
- **Upsert 语义：** 所有数据库写入为幂等操作（唯一键冲突时 insert-or-update）。
- **可配置中性化：** `NEUTRALIZE_MODE = full | size_only | none` 控制 OLS 行业+市值残差中性化；`CATEGORY_NEUTRALIZE_OVERRIDES` 支持按大类覆盖（默认 momentum/macro/sentiment → size_only 保留行业 alpha）。
- **Regime 切换：** CSI 300 60 日 MA ±5% 渐进式切换（线性插值，避免 whipsaw），熊市时降低动量（0.8→0.3）/成长（1.0→0.6）、提高质量（1.2→1.5）/价值（1.0→1.3）/技术（0.7→1.0），可通过 `REGIME_ENABLED=0` 关闭。
- **T+1 执行模型：** 先卖后买。涨停股排除在买入之外；跌停股加入 `pending_sells` 队列下一交易日重试。
- **可插拔交易后端：** `BaseTrader` ABC + `main.py::_create_trader()` 工厂方法，目前仅实现 `PaperTrader`。

### 因子体系（29 个因子）

| 大类 | 权重 | 因子 |
|---|---|---|
| 价值 | 1.0 | EP, BP |
| 质量 | 1.2 | ROE_TTM, GROSS_MARGIN, PROFIT_STB, MARGIN_TREND |
| 成长 | 1.0 | NET_PROFIT_YOY, REVENUE_YOY, NET_PROFIT_CAGR_3Y |
| 动量 | 0.8 | MOM_1M, MOM_3M, MOM_12M, REV_5D, IND_MOM, RESIDUAL_MOM, CMDTY_MOM |
| 技术 | 0.7 | TURN_20D, VOL_20D, PRICE_DEV_60D, SIZE, VOL_PRICE_DIV |
| 宏观 | 0.6 | MACRO_CYCLE, MACRO_LIQD, MACRO_INFL, MACRO_EXTR |
| 舆情 | 0.6 | POLICY_SENT, POLICY_INTENSITY, ANALYST_RATING, ANALYST_COVERAGE |

## 配置

环境变量在 `backend/.env`（参考 `backend/.env.example`）。关键配置：`TUSHARE_TOKEN`、`TWITTER_USERNAME`/`TWITTER_EMAIL`/`TWITTER_PASSWORD`（twikit 免费方案）、`LLM_PROVIDER`/`LLM_API_KEY`/`LLM_MODEL`（舆情 LLM 增强层，支持 Anthropic Claude + OpenAI，可选）、MySQL 连接信息、`MAX_HOLDINGS`、`NEUTRALIZE_MODE`、`USE_VOL_TARGETING`、风控参数。所有配置在 `backend/services/config/settings.py` 中有默认值。

## 编码规范

- 所有模块使用 `logging.getLogger(__name__)`，日志级别取 `config.settings.LOG_LEVEL`
- Matplotlib 必须在导入 `pyplot` 前调用 `matplotlib.use("Agg")`
- 因子计算始终为截面（同一日期，全部股票）
- MySQL 列名 `open` 是保留字，原生 SQL 需用反引号转义
- 数据库层使用 SQLAlchemy ORM（`DeclarativeBase`）
- 面向 A 股市场（申万行业分类、涨跌停处理、T+1 规则）
- 在[A_SHARE_STRATEGY.md](A_SHARE_STRATEGY.md) 和[CONTINUE_PROMPT.md](CONTINUE_PROMPT.md)中记录变动
