# A股+美股多因子量化系统

覆盖数据采集、因子计算、组合构建、风控、回测、模拟交易、舆情爬取和报告生成。

## 系统架构（Django MVT，A 股/美股按 app 对齐）

```
┌─────────────┐   ┌─────────────┐   ┌─────────────┐
│   CLI       │   │ Django API  │   │  Frontend   │
│ manage.py   │   │ api/views/  │   │ frontend/   │
└──────┬──────┘   └──────┬──────┘   └──────┬──────┘
       │                 │                 │
       └────────┬────────┘                 │
                ▼                          │
    Django apps  ◄──────────────────────────┘ (via HTTP)
    (stocks / backtest / trading / sentiment)

文件命名规则（同名 app + 前缀分流）:
  A 股：a_xxx.py    美股：us_xxx.py    通用：xxx.py（无前缀）

A 股管道（已全面 Django ORM 化）:
  Tushare/AkShare → stocks/services/downloaders/a_tushare_*.py + a_akshare_*.py
    → AStockBasic / ADailyPrice / AFinancial{Income,Balance,Cashflow,Indicator}
    → stocks/services/a_cleaner.py
    → stocks/services/factors/a_*.py (30+ 因子)
    → backtest/services/a_regime.py + a_strategy.py + a_engine.py
    → trading/services/a_paper_trader.py + a_risk.py + a_gm_trader.py

美股管道（三策略：Alpha 多空 + Beta Regime + Baseline VQM）:
  FMP / UW / Fiscal / Quiver / AlphaVantage / FRED
    → stocks/services/downloaders/{fmp,bulk,fred,edgar,...}.py（待重命名为 us_*.py）
    → US{StockBasic, DailyPrice, FinancialData, ...} (40+ 表)
    → stocks/services/cleaner.py（待重命名为 us_cleaner.py）
    → stocks/services/factors/{value,quality,growth,...}.py（待重命名为 us_*.py）
    → backtest/services/{engine,strategy,regime,ff5,baseline,beta,ml_scorer,saver}.py（待重命名为 us_*.py）
    → trading/services/{paper_trader,alpaca_trader}.py（待重命名为 us_*.py）

通用层（无前缀）:
  stocks/services/upsert.py     — Django ORM 异步批写（UpsertManager）
  trading/services/base_trader.py — 交易执行抽象基类
  trading/services/monitor/       — 绩效 + HTML 报告
  backtest/models/result.py       — 回测结果持久化
```

**前端页面:**
- A股: 选股(`/select`)、回测(`/backtest`)、模拟交易(`/paper`)
- 美股: 选股(`/us/select`)、回测(`/us/backtest`)、模拟交易(`/us/paper`)
- 公共: 仪表盘(`/`)、数据管理(`/data`)、自选股(`/watchlist`)、设置(`/settings`)

## 常用命令（统一 management commands）

```bash
# 安装后端依赖
pip install -r requirements.txt

# 启动服务（开发）
./start.sh                                        # 后端 + 前端 + cron

# === 美股数据导入（FMP / Quiver） ===
python3 manage.py bulk_import --source fmp --target all --start-year 1995    # FMP 全量
python3 manage.py bulk_import --source fmp --target prices                    # 单端点
python3 manage.py bulk_import --source quiver --target all                    # Quiver 游说/政府合同

# 2026-04-15 数据补强批次（验证后正式列表）：
# 先建新表（dark_pool + 13f）
psql $PG_URL -f scripts/migrate_us_short_interest_13f.sql

# 真有历史的端点 — 一次性 bulk
python3 manage.py bulk_import --source fmp --target press-releases           # us_press_release（FMP 仅返最近 ~20 天，需 cron 每日积累）
python3 manage.py bulk_import --source fmp --target sec-filings              # us_sec_filing（per-ticker，按年切段，2010-至今）
python3 manage.py bulk_import --source fmp --target revenue-segments         # us_revenue_segment（产品+地理，2010-至今 15 年季度）
python3 manage.py bulk_import --source fmp --target 13f-holdings             # us_institutional_holder（FMP 必须循环 year/quarter，默认 2015-至今）
python3 manage.py bulk_import --source quiver --target dark-pool             # us_dark_pool_volume（替代 short interest，2010-至今每日）

# Snapshot-only 端点 — 当下立即跑 + 加 cron 周任务积累时序：
python3 manage.py bulk_import --source fmp --target dcf            --clean   # us_dcf_valuation（仅 1 条/票）
python3 manage.py bulk_import --source fmp --target scores         --clean   # us_financial_score（仅 1 条/票，对照自算 Piotroski/Altman 用）
python3 manage.py bulk_import --source fmp --target float          --clean   # us_shares_float（仅 1 条/票）
python3 manage.py bulk_import --source fmp --target peers          --clean   # us_stock_peer（同业列表，仅 1 条/票）

# C-3 FRED 宏观增强（NFCI / HY OAS / IG OAS / 短端利率 / 通胀预期 等 12 个新指标）：
python3 manage.py data_update --market us                                    # FRED 用 update 入口（全量自动加载新增 series）

# ⚠ 已移除：news（FMP 计划不含 /general-news 端点，404）+ short-interest（FMP 计划不含，改用 Quiver dark-pool）

# === A 股数据导入（Tushare / AkShare） ===
# 第一次：先备份 → drop+重建表 → 全量下载
psql $PG_URL -f scripts/migrate_ashare_schema.sql
python3 manage.py bulk_import --source tushare --target trade-cal             # 交易日历
python3 manage.py bulk_import --source tushare --target stock-list            # 股票列表
python3 manage.py bulk_import --source tushare --target all --start-date 20150101  # 全量
python3 manage.py bulk_import --source akshare --target all                    # 研报 + 高管持股

# 单端点（Tushare 全字段保留）
python3 manage.py bulk_import --source tushare --target {prices|income|balancesheet|cashflow|fina-indicator|industry|index|commodity|macro|trade-cal}

# === 增量更新（统一入口，--market 分流） ===
python3 manage.py data_update --market us                  # 美股 (FMP+UW+Fiscal+Quiver+AV+FRED)
python3 manage.py data_update --market us --old-source     # 美股旧源 (yfinance)
python3 manage.py data_update --market cn                  # A 股 (Tushare+AkShare 全部端点)

# === 回测 ===
python3 manage.py backtest --market us --start 2020-01-01 --end 2024-12-31
python3 manage.py backtest --market cn --start 2020-01-01 --end 2024-12-31

# === cli.py 残留命令（待迁移到 management commands） ===
python3 cli.py db status                                    # 查看全表数据状态
python3 cli.py select --market us --date 2025-01-15        # 选股
python3 cli.py paper trade --market us                      # 执行模拟交易
python3 cli.py polymarket history --min-volume 1000000      # Polymarket 历史
```

## 配置

环境变量在 `.env`（参考 `.env.example`）。所有配置在 `services/config.py` 中有默认值。

- **A股**: `TUSHARE_TOKEN`、`MAX_HOLDINGS`、`NEUTRALIZE_MODE`、`USE_VOL_TARGETING`、风控参数
- **美股**: `US_MAX_HOLDINGS`、`US_SLIPPAGE`、`US_REGIME_INDEX`、`US_CATEGORY_WEIGHTS` 等（均带 `US_` 前缀）
- **美股数据**: `FMP_API_KEY`（主数据源）、`UW_API_KEY`（Unusual Whales）、`FISCAL_API_KEY`（Fiscal.ai）、`QUIVER_API_KEY`（Quiver）、`ALPHAVANTAGE_API_KEY`（Alpha Vantage）、`FRED_API_KEY`（FRED 宏观）
- **舆情**: `TWITTER_USERNAME`/`TWITTER_EMAIL`/`TWITTER_PASSWORD`、`LLM_PROVIDER`/`LLM_API_KEY`/`LLM_MODEL`
- **数据库**: MySQL 连接信息

## A股因子体系（30 因子，`stocks/services/factors/a_*.py`）

| 大类 | 权重 | 因子 |
|---|---|---|
| 价值 | 0.7 | EP, BP, DIV_YIELD |
| 质量 | 1.3 | ROE_TTM, GROSS_MARGIN, PROFIT_STB, MARGIN_TREND |
| 成长 | 1.0 | NET_PROFIT_YOY, REVENUE_YOY, NET_PROFIT_CAGR_3Y |
| 动量 | 0.9 | MOM_1M, MOM_3M, MOM_12M, REV_5D, IND_MOM, RESIDUAL_MOM, CMDTY_MOM |
| 技术 | 0.7 | TURN_20D, VOL_20D, PRICE_DEV_60D, SIZE, VOL_PRICE_DIV |
| 宏观 | 0.6 | MACRO_CYCLE, MACRO_LIQD, MACRO_INFL, MACRO_EXTR |
| 舆情 | 0.6 | POLICY_SENT, POLICY_INTENSITY, ANALYST_RATING, ANALYST_COVERAGE |

## 美股因子体系（43 因子 × 7 大类）

**已迁移到 AlphaSignal 架构**（`stocks/services/factors/signals/`，元数据化 + `@register` 自动注册）的批次：
- **Quality**（15 因子，2026-04-15）— Batch 1
- **Momentum**（10 因子，6 个新增 + 4 个 legacy 待迁移，2026-04-15）— Batch 2

未迁移的类别仍用旧架构，按 T1 backlog 逐批迁移（Value / Defensive / Liquidity / Analyst / Short-side）。

文件命名：`stocks/services/factors/signals/{category}/us_{factor}.py`（A 股未来按 `a_{factor}.py` 加入）。

| 大类 | 因子 | 架构 |
|------|------|------|
| value | EP, BP, DIV_YIELD, BUYBACK_YIELD | legacy |
| **quality** (15) | ROE_TTM, GROSS_MARGIN, PROFIT_STB, MARGIN_TREND, ACCRUALS（legacy 5 已迁移）+ **PIOTROSKI_F, ALTMAN_Z, OHLSON_O, BENEISH_M, QMJ_LEVERAGE, QMJ_EARNINGS_VOL, QMJ_ROE_VOL, QMJ_NET_PAYOUT, CASH_CONV_CYCLE, EARNINGS_PERSISTENCE**（新增 10）| **AlphaSignal** |
| **momentum** (10) | MOM_1M, MOM_3M, MOM_12M, REV_5D（legacy 4）+ **PRICE_52W_HIGH, RESIDUAL_MOM_FF3, SUE_PEAD, INDUSTRY_MOM, FROG_IN_PAN, TSMOM**（新增 6）| **AlphaSignal**（新增）+ legacy |
| growth | NET_PROFIT_YOY, REVENUE_YOY, NET_PROFIT_CAGR_3Y | legacy |
| technical | TURN_20D, VOL_20D, IVOL, SIZE, IV_SKEW, PUT_CALL_RATIO | legacy |
| analyst | US_ANALYST_RATING, US_ANALYST_COVERAGE, EARNINGS_SURPRISE, EPS_REVISION, INSIDER_NET_BUY | legacy |
| sentiment | POLYMARKET_SENT, LOBBY_INTENSITY, GOV_CONTRACT, NEWS_SENTIMENT | legacy |

**Quality 15 因子构成：**
- 体检类：PIOTROSKI_F（9 分财务体检，Piotroski 2000）
- 造假检测：BENEISH_M（8 ratios，Beneish 1999，反向）
- 破产预警：ALTMAN_Z（5 ratios，Altman 1968）+ OHLSON_O（9 输入 logit，Ohlson 1980，反向）
- QMJ Safety：QMJ_LEVERAGE / QMJ_EARNINGS_VOL / QMJ_ROE_VOL（全反向，Asness-Frazzini-Pedersen 2019）
- QMJ Payout：QMJ_NET_PAYOUT（股东净收益 / 市值）
- 运营效率：CASH_CONV_CYCLE（反向）+ EARNINGS_PERSISTENCE（EPS 8Q AR(1)）
- 原有 5 个：ROE_TTM / GROSS_MARGIN / PROFIT_STB / MARGIN_TREND / ACCRUALS

**Momentum 6 个新增：**
- PRICE_52W_HIGH（George-Hwang 2004，当前价 / 过去 52 周最高）
- RESIDUAL_MOM_FF3（Blitz-Huij-Martens 2011，剔 FF3 残差累加，比纯动量更稳）
- SUE_PEAD（Foster-Olsen-Shevlin 1984，标准化盈利惊喜 + 60 天事件窗口）
- INDUSTRY_MOM（Moskowitz-Grinblatt 1999，个股 12M − 行业中位数 12M）
- FROG_IN_PAN（Da-Gurun-Warachka 2014，动量"连续小涨" vs "一次性大涨"）
- TSMOM（Moskowitz-Ooi-Pedersen 2012，时序动量，方向由滚动 IC 决定）

等权合成，两层类别打分（类内动态分母 + 类间加权）。因子方向由 AlphaSignal.inherent_direction 元数据 + 滚动 IC 动态决定：基本面 24-36M、动量 6-12M、情绪 6M。

## 美股回测绩效（含幸存者偏差修正，基准 S&P 500）

**Alpha 策略逐年收益（31 因子多空对冲，净敞口 ~60%）：**

| Year | 策略 | S&P 500 | 超额 | 区间 |
|------|------|---------|------|------|
| 2000 | -7.06% | -9.27% | **+2.21%** | 扩展 |
| 2001 | 7.53% | -10.53% | **+18.06%** | 扩展 |
| 2002 | 11.85% | -23.80% | **+35.65%** | 扩展 |
| 2003 | 48.42% | 22.32% | **+26.10%** | 扩展 |
| 2004 | 14.84% | 9.33% | **+5.51%** | 扩展 |
| 2005 | 19.45% | 3.84% | **+15.61%** | 扩展 |
| 2006 | 11.58% | 11.78% | -0.20% | 扩展 |
| 2007 | 1.57% | 3.65% | -2.08% | 扩展 |
| 2008 | -5.79% | -37.58% | **+31.80%** | 扩展 |
| 2009 | 21.87% | 19.67% | +2.20% | 扩展 |
| 2010 | 17.50% | 11.00% | **+6.50%** | 扩展 |
| 2011 | -3.78% | -1.12% | -2.65% | 扩展 |
| 2012 | 5.72% | 11.68% | -5.96% | IS |
| 2013 | 27.27% | 26.39% | +0.88% | IS |
| 2014 | 28.95% | 12.39% | **+16.56%** | IS |
| 2015 | 1.71% | -0.69% | +2.40% | IS |
| 2016 | 2.97% | 11.24% | -8.27% | IS |
| 2017 | 22.57% | 18.42% | **+4.15%** | IS |
| 2018 | -13.68% | -7.01% | -6.67% | IS |
| 2019 | 10.88% | 28.71% | -17.84% | IS |
| 2020 | 70.20% | 15.29% | **+54.90%** | IS |
| 2021 | 4.53% | 28.79% | -24.27% | IS |
| 2022 | -3.29% | -19.95% | **+16.66%** | OOS |
| 2023 | 11.80% | 24.73% | -12.92% | OOS |
| 2024 | 48.73% | 24.01% | **+24.72%** | OOS |
| 2025 | 108.25% | 16.65% | **+91.61%** ⚠️ | OOS |
| 2026 | -2.33% | -5.97% | +3.64% | OOS |

**跨时代 FF5 Alpha 一致性验证：**

| 区间 | FF5 Alpha | t-stat | β_mkt | β_rmw | Sharpe | 超额年化 | 下行捕获 |
|------|-----------|--------|-------|-------|--------|---------|---------|
| 2000-2011（无 analyst 大类） | **6.18%** | **2.19** | 0.37 | -0.05 | 0.51 | +12.33% | 0.30 |
| 2012-2023（完整因子） | **6.58%** | **2.20** | 0.44 | -0.21 | 0.63 | +0.89% | 0.44 |

> **策略特征：** FF5 Alpha 跨时代一致（~6.2%, t~2.2），行业内选股 alpha 确认存在（EPS_REVISION 行业内 ICIR=0.43）。熊市保护极强（2002 +36%, 2008 +32%, 2022 +17%），牛市跟不上（半仓 L/S 天然代价）。2025 +108% 是 AI 泡沫风格红利（β_rmw=-1.01），不可持续。

## 核心设计决策

- **无前视偏差（全 31 因子已审计）：** 财务数据按 `filing_date <= date`（公告日）过滤，价格按 `trade_date <= date`，宏观 30 天 lag，情绪/另类数据用 trailing lookback 窗口
- **两层因子打分：** 类内动态分母 + 类间动态分母，`MIN_VALID_CATEGORIES=4`
- **MVO 优化器（v4）：** cvxpy + OSQP 替换 Top-N + Softmax。目标函数 `max μ̂'w − λ·w'Σw − γ·||w − w_prev||₁`，约束净敞口/总杠杆/单股上下限/行业 gross。Ledoit-Wolf 252D 协方差矩阵，求解失败自动降级 Top-N
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
| 美股 Alpha v3 | 31 因子 + 分因子滚动 IC + ML blend → 跨时代 α~6.2%(t~2.2) | ✅ |
| 数据迁移 | FMP+UW+Fiscal.ai+Quiver+AlphaVantage 六源数据接入 | ✅ |
| 全项目日志覆盖 | 106 个 Python 文件所有 return/continue/break/except 分支加 logger | ✅ |
| P0 行业内验证 | EPS_REVISION 行业内 ICIR=0.43，确认截面选股 alpha 存在 | ✅ |
| P0.5 MVO 优化器 | Ledoit-Wolf 风险模型 + cvxpy/OSQP MVO 替换 Top-N + Softmax | ✅ |
| 性能优化 (2026-04-21) | 并行预加载 + parquet 缓存 + multiprocessing spawn + 因子向量化 | ✅ |
| 因子扩展 (2026-04-21) | 81 因子（59 AlphaSignal + 22 legacy）+ 10 张新表预加载 + 20 因子缓存改造 | ✅ |
| 因子分析框架 | 逐年 IC/ICIR + Fama-MacBeth + 因子衰减 + 慢因子 profile | ✅ |
| PRICE_TARGET_RATIO v2 | 前瞻偏差修复：<2021 Forward EP / ≥2021 per-analyst PT detail | ✅ |
| 滚动 IC v2 | 连续权重（EMA + ICIR 缩放 + 置信度）替代二值 +1/-1 | ✅ |
| CLI 统一 | cli.py 删除，全部迁移到 Django management commands | ✅ |
| P1 空头 v5 | 独立因子模型 + Regime 渐进 + 融券约束 + 止损，待回测 | 🔄 |
| 待办 | 回测验证 / 因子权重分级 / 回测鲁棒性 / 实盘 | 📋 |

**当前待办（按优先级）：**

**P0.5 — 工业级架构补强 ✅ 已完成：**
- 风险模型：`backtest/services/us_risk_model.py`（Ledoit-Wolf 252D 协方差 Σ，parquet 缓存）
- MVO 优化器：`backtest/services/us_optimizer.py`（cvxpy + OSQP，目标 `max μ̂'w − λ·w'Σw − γ·||w − w_prev||₁`）
- 约束：净敞口 0.6 / 总杠杆 ≤ 1.0 / 单股 [-5%, +15%] / 行业 gross ≤ 25%
- `US_USE_OPTIMIZER=0` 一键回退 Top-N + Softmax

**因子分析结果（2026-04-21，79 因子 × 168 月）：**

| 级别 | ICIR 阈值 | 数量 | 代表因子 |
|------|----------|------|---------|
| T1 强信号 | ≥ 0.3 | 9 | FREE_FLOAT_PCT(+0.46), TURN_20D(+0.45), PIOTROSKI_F(+0.40), SUE_PEAD(+0.38), EV_TO_FCF(+0.37) |
| T2 有信号 | 0.15-0.3 | 21 | ESG_RISK, MOM_12M, DIV_YIELD, PROFIT_STB, TSMOM, INDUSTRY_MOM 等 |
| T3 弱信号 | 0.05-0.15 | 20 | PRICE_52W_HIGH, GROSS_MARGIN, OHLSON_O 等 |
| 方向翻转 | <0.05 但单年 >0.5 | 12 | EPS_REVISION, BP, MOM_1M, VOL_20D, ALTMAN_Z, BENEISH_M（滚动 IC 处理） |
| 真噪音 | <0.05 且无信号 | 1 | GEO_CONCENTRATION |

**当前待办（按优先级）：**

**P0 — 回测验证（滚动 IC v2 + MVO + 81 因子）：**
- 跑 2012-2025 完整回测，对比 v3 基线
- 验证滚动 IC v2 连续权重的效果

**P1 — 因子权重分级**：基于 ICIR 分析结果，T1 因子权重 2.0，T2 因子 1.0，T3 因子 0.5

**P2 — 回测鲁棒性**：换手率控制 + 滑点敏感性 + Regime 参数扰动

**P3 — 数据修复**：INSIDER_NET_BUY 数据缺失 / INST_OWNERSHIP_DELTA 覆盖不足

**P4 — 空头 v5**：独立因子模型 + Regime 渐进 + 融券约束 + 15% 止损

**P5 — 工程与实盘**：增量采集自动化 + Alpaca 模拟盘已接入 + 实盘验证 3-6 月

**P7 — 长期架构升级**：
- PCA 统计风险因子（Barra 开源替代，前 20-30 主成分作风格因子）
- 多周期信号合成（日/周/月频分层）
- Alpha Bayesian Shrinkage（极值持仓抑制）
- 交易成本/市场冲击模型（写入优化器目标）
- Alpha Capture System（因子版本化 + 独立 IC 监控 + 灰度上线）
- Barra-style P&L 归因（每日拆解 factor return × β + specific return）


## Rust 计算引擎 (`quant-engine/`)

因子计算和回测引擎正在从 Python 迁移到 Rust，解决 macOS multiprocessing fork crash 并提升性能。

**架构分工：**
- **Python (Django)** — 数据下载、DB 写入、Web API、前端
- **Rust (`quant-engine/`)** — 因子计算、回测模拟、因子分析（读 parquet 缓存）

**技术栈：** Polars (parquet I/O) + rayon (并行) + nalgebra (线代) + OSQP (MVO 优化) + clap (CLI)

```bash
# 构建
cd quant-engine && cargo build --release

# 验证缓存文件
cargo run --release -- validate --cache-dir ../cache/

# 单日因子计算
cargo run --release -- factors --date 2024-12-31

# 单日评分
cargo run --release -- score --date 2024-12-31 --top 30

# 完整回测
cargo run --release -- backtest --start 2012-01-01 --end 2025-12-31 --output ../output/rust/

# 因子分析（IC / Fama-MacBeth / Decay）
cargo run --release -- analyze --start 2012-01-01 --end 2025-12-31
```

**Rust Workspace 结构：**
```
quant-engine/
├── crates/
│   ├── qrs-core/       核心类型 + 配置（TickerId, Config, Date）
│   ├── qrs-data/       Parquet 加载 + DataCache + Universe 过滤
│   ├── qrs-factors/    ~80 个因子（value/quality/momentum/... 8 大类）
│   ├── qrs-strategy/   两层评分 + 滚动IC + Regime + MVO优化器
│   ├── qrs-backtest/   仓位制 T+0 回测引擎 + 风控
│   └── qrs-cli/        CLI 入口（clap derive）
```

## 详细文档

- [A股策略算法](doc/A_SHARE_STRATEGY.md)
- [美股策略算法](doc/US_SHARE_STRATEGY.md)
- [数据源详情](doc/DATA_SOURCES.md)
- [Polymarket 策略](doc/old/PollyMarket_STRATEGY.md)
- [Polymarket P&L 分析](doc/old/POLYMARKET_PNL_ANALYSIS.md)
