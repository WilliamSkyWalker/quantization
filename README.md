# A股+美股多因子量化系统

覆盖数据采集、因子计算、组合构建、风控、回测、模拟交易、舆情爬取和报告生成。

## 系统架构

```
┌─────────────┐   ┌─────────────┐   ┌─────────────┐
│   CLI       │   │ Django API  │   │  Frontend   │
│ cli.py      │   │ api/views/  │   │ frontend/   │
└──────┬──────┘   └──────┬──────┘   └──────┬──────┘
       │                 │                 │
       └────────┬────────┘                 │
                ▼                          │
    services/  ◄───────────────────────────┘ (via HTTP)
    （唯一业务逻辑层）

A股管道:
  Tushare → data/{downloader,updater}.py → MySQL
  → data/cleaner.py → factors/*.py (30因子) → factors/processor.py
  → strategy/regime.py → strategy/multi_factor.py → risk/risk_manager.py
  → strategy/backtest.py | execution/paper_trader.py

美股管道（三策略：Alpha 多空对冲 + Beta Regime 控制 + Baseline VQM 验证）:
  FMP API → data/bulk_downloader.py → MySQL (us_* 表, bulk 按年/per-ticker)
    含: 行情/财报/key-metrics/earnings-surprise/EPS-consensus/insider/分红拆股
    SP500+NASDAQ100 成分股 + 历史变更（幸存者偏差修正）
  Unusual Whales → data/bulk_downloader.py → us_options_flow/us_dark_pool/us_congress_trade/us_news
  Fiscal.ai → data/bulk_downloader.py → us_daily_ratio (日频 PE/PB/EV)
  FRED → data/fred_downloader.py → us_macro_indicator 表
  FF5 → strategy/ff5.py → Fama-French 五因子回归分析
  (旧源 yfinance/EDGAR/SimFin 保留在 data/fmp_downloader.py，CLI --old-source 可回退)
  → data/us_cleaner.py → us_factors/*.py (31因子×7大类) → us_factors/processor.py
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

## 常用命令

```bash
# 安装后端依赖
pip install -r requirements.txt

# 启动服务（开发）
./start.sh                                        # 启动后端 + 前端 + 安装 cron

# 数据导入（新源 FMP/UW/Fiscal.ai）
python3 cli.py data bulk-import --source fmp --target all --clean --start-year 1995
python3 cli.py data bulk-import --source fmp --target financial-quarterly --clean  # 季度财报(IS+BS+CF)
python3 cli.py data bulk-import --source fmp --target analyst-grades --clean       # 分析师评级
python3 cli.py data bulk-import --source uw --target all --clean
python3 cli.py data bulk-import --source fiscal --target all
python3 cli.py data bulk-import --source quiver --target all                   # Quiver(游说/政府合同/WSB)
python3 cli.py data bulk-import --source av --target all                       # AlphaVantage(新闻情绪/期权)

# 增量更新
python3 cli.py data update --market us                 # 新源 (FMP+UW+Fiscal)
python3 cli.py data update --market us --old-source    # 旧源 (yfinance)

# CLI 调试
python3 cli.py db status                               # 查看全表数据状态
python3 cli.py select --market us --date 2025-01-15    # 美股选股
python3 cli.py backtest --market us --start 2020-01-01 # 美股回测
python3 cli.py factor list --market us                 # 列出所有因子
python3 cli.py score AAPL --date 2025-01-15            # 单只股票得分
python3 cli.py paper status --market us                # 模拟账户状态
python3 cli.py paper trade --market us                 # 执行模拟交易
```

## 配置

环境变量在 `.env`（参考 `.env.example`）。所有配置在 `services/config.py` 中有默认值。

- **A股**: `TUSHARE_TOKEN`、`MAX_HOLDINGS`、`NEUTRALIZE_MODE`、`USE_VOL_TARGETING`、风控参数
- **美股**: `US_MAX_HOLDINGS`、`US_SLIPPAGE`、`US_REGIME_INDEX`、`US_CATEGORY_WEIGHTS` 等（均带 `US_` 前缀）
- **美股数据**: `FMP_API_KEY`（主数据源）、`UW_API_KEY`（Unusual Whales）、`FISCAL_API_KEY`（Fiscal.ai）、`FRED_API_KEY`（FRED 宏观）
- **美股旧源（保留）**: `SIMFIN_API_KEY`（SimFin）、SEC EDGAR（免费），CLI `--old-source` 可回退
- **舆情**: `TWITTER_USERNAME`/`TWITTER_EMAIL`/`TWITTER_PASSWORD`、`LLM_PROVIDER`/`LLM_API_KEY`/`LLM_MODEL`
- **数据库**: MySQL 连接信息

## A股因子体系（30 因子，`services/factors/`）

| 大类 | 权重 | 因子 |
|---|---|---|
| 价值 | 0.7 | EP, BP, DIV_YIELD |
| 质量 | 1.3 | ROE_TTM, GROSS_MARGIN, PROFIT_STB, MARGIN_TREND |
| 成长 | 1.0 | NET_PROFIT_YOY, REVENUE_YOY, NET_PROFIT_CAGR_3Y |
| 动量 | 0.9 | MOM_1M, MOM_3M, MOM_12M, REV_5D, IND_MOM, RESIDUAL_MOM, CMDTY_MOM |
| 技术 | 0.7 | TURN_20D, VOL_20D, PRICE_DEV_60D, SIZE, VOL_PRICE_DIV |
| 宏观 | 0.6 | MACRO_CYCLE, MACRO_LIQD, MACRO_INFL, MACRO_EXTR |
| 舆情 | 0.6 | POLICY_SENT, POLICY_INTENSITY, ANALYST_RATING, ANALYST_COVERAGE |

## 美股因子体系（31 因子 × 7 大类，`services/us_factors/`）

| 大类 | 权重 | 因子 |
|------|------|------|
| value | 1.0 | EP, BP, DIV_YIELD, BUYBACK_YIELD |
| quality | 1.0 | ROE_TTM, GROSS_MARGIN, PROFIT_STB, MARGIN_TREND, ACCRUALS |
| growth | 1.0 | NET_PROFIT_YOY, REVENUE_YOY, NET_PROFIT_CAGR_3Y |
| momentum | 1.0 | MOM_1M, MOM_3M, MOM_12M, REV_5D |
| technical | 1.0 | TURN_20D, VOL_20D, IVOL, SIZE, IV_SKEW, PUT_CALL_RATIO |
| analyst | 1.0 | US_ANALYST_RATING, US_ANALYST_COVERAGE, EARNINGS_SURPRISE, EPS_REVISION, INSIDER_NET_BUY |
| sentiment | 1.0 | POLYMARKET_SENT, LOBBY_INTENSITY, GOV_CONTRACT, NEWS_SENTIMENT |

等权合成，两层类别打分（类内动态分母 + 类间加权）。因子方向由 trailing 36M 滚动 IC 自动决定（3 个固有反转 + 5 个质量保护锁定 + 其余动态）。

## 美股回测绩效（含幸存者偏差修正，基准 Russell 1000）

**Alpha 策略逐年收益（31 因子多空对冲，净敞口 ~60%，基准 S&P 500）：**

| Year | 策略 | S&P 500 | 超额 | 区间 |
|------|------|---------|------|------|
| 2012 | 5.89% | 11.68% | -5.79% | IS |
| 2013 | 23.50% | 26.39% | -2.89% | IS |
| 2014 | 3.76% | 12.39% | -8.63% | IS |
| 2015 | 11.11% | -0.69% | **+11.80%** | IS |
| 2016 | 12.16% | 11.24% | +0.92% | IS |
| 2017 | 24.40% | 18.42% | **+5.98%** | IS |
| 2018 | 15.48% | -7.01% | **+22.49%** | IS |
| 2019 | 0.33% | 28.71% | -28.38% | IS |
| 2020 | 16.04% | 15.29% | +0.75% | IS |
| 2021 | 7.01% | 28.79% | -21.78% | IS |
| 2022 | 7.78% | -19.95% | **+27.73%** | OOS |
| 2023 | 3.36% | 24.73% | -21.37% | OOS |
| 2024 | 48.73% | 24.01% | **+24.72%** | OOS |
| 2025 | 108.25% | 16.65% | **+91.61%** ⚠️ | OOS |
| 2026 | -2.33% | -5.97% | +3.64% | OOS |

| 指标 | IS (2012-2021) | OOS (2022-2026) |
|------|---------------|-----------------|
| FF5 Alpha | +6.66% (t=2.26**) | 待确认 |
| Sharpe | 0.64 | ~0.88 |
| β_mkt | 0.44 | ~0.40 |
| β_rmw | -0.21 | ~-0.60 ⚠️ |
| 下行捕获 | 0.40 | ~0.35 |

> **策略特征：** FF5 Alpha 跨时代一致（2000-2011: 6.18% t=2.19, 2012-2023: 6.58% t=2.20）。熊市保护极强（2002 +36%, 2008 +32%, 2022 +17%），牛市跟不上（半仓 L/S 天然代价）。2025 +108% 是 AI 泡沫风格红利，不可持续。

## 核心设计决策

- **无前视偏差（全 31 因子已审计）：** 财务数据按 `filing_date <= date`（公告日）过滤，价格按 `trade_date <= date`，宏观 30 天 lag，情绪/另类数据用 trailing lookback 窗口
- **两层因子打分：** 类内动态分母 + 类间动态分母，`MIN_VALID_CATEGORIES=4`
- **Upsert 语义：** 所有数据库写入为幂等操作（`INSERT ... ON DUPLICATE KEY UPDATE`）
- **Regime 切换：** 四维复合（趋势+VIX+利差+拥挤度）+ Credit Veto
- **回测预加载：** `preload_for_backtest()` 一次性加载到内存，因子计算全部从内存过滤

## 舆情管道

`services/sentiment/scrapers/` 下 11 个中国政府网站爬虫 + CCTV新闻联播（AKShare）+ 巨潮公告 + 3 个 Twitter/X 美国政策爬虫 + Polymarket 预测市场桥接，共 20 个爬虫。两层分析：关键词底层 + LLM 增强层。

## 开发历史

| 阶段 | 模块 | 状态 |
|------|------|------|
| Phase 1-6 | 数据层→因子层→策略层→风控层→执行层→监控层 | ✅ |
| Phase 7-14 | 因子增强 + 舆情 + Polymarket + 美股接入 | ✅ |
| Phase 15-24 | 性能优化 + 自适应调仓 + 因子质量增强 | ✅ |
| 美股 Alpha v1 | 29 因子多空对冲 + Regime + FF5 回归 | ✅ |
| 美股 Alpha v2 | 阶梯式重构 → 23 因子剪枝 | ✅ |
| 美股 Alpha v3 | 31 因子 + 滚动 IC → IS α=6.66%(t=2.26), OOS 熊市保护强(2022 +27.7%) | ✅ |
| 数据迁移 | FMP+UW+Fiscal.ai+Quiver+AlphaVantage 六源数据接入 | ✅ |
| 全项目日志覆盖 | 所有 return/continue/break/except 分支加 logger | ✅ |
| 待办 | 行业内选股验证 / β_rmw 约束 / 换手率控制 / 滑点测试 / 实盘接入 | 📋 |

**当前待办（按优先级）：**

P0 — 因子优化（最高优先级）：
1. EPS_REVISION v2 四维复合（方向×幅度×广度×加速度）
2. ACCRUALS v2 拆分、BP v2 R&D 调整、REV_5D v2 条件反转、ROE_TTM v2 趋势

P1 — 空头端重建：独立负向催化剂选股 + 20-25 只分散 + 市值下限 $5B

P2 — 噪音清理：移除 IV_SKEW/PUT_CALL_RATIO/NEWS_SENTIMENT/POLYMARKET_SENT

P3 — 权重分级：ICIR>0.3→2.0, 0.15-0.3→1.0, <0.15→0.5

P4 — 回测鲁棒性：换手率控制 + 滑点敏感性 + Regime 参数扰动

P5 — 工程与实盘：增量采集自动化 + 券商 API + 模拟 3-6 月


## 详细文档

- [A股策略算法](doc/A_SHARE_STRATEGY.md)
- [美股策略算法](doc/US_SHARE_STRATEGY.md)
- [数据源详情](doc/DATA_SOURCES.md)
- [Polymarket 策略](doc/old/PollyMarket_STRATEGY.md)
- [Polymarket P&L 分析](doc/old/POLYMARKET_PNL_ANALYSIS.md)
