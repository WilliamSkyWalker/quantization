# A股+美股多因子量化系统（Rust）

覆盖数据采集、因子计算、组合构建、风控、回测、模拟交易、舆情爬取和报告生成。

> **2026-04-30 重大变更**：所有 Python 代码 + React 前端已归档到 [`legacy_python/`](legacy_python/)，不再维护。生产策略全部迁移到 Rust [`quant-engine/`](quant-engine/)。原因：Python 引擎多个 bug 未修，Rust v25 已达机构级 alpha (α=13.28%, t=3.40, Sharpe 0.99)。

## 系统架构（Rust 单栈，9 crates）

```
┌──────────────────────────────────────────────┐
│  quant-engine/  (Rust workspace)              │
│  ┌────────────────────────────────────────┐  │
│  │ quant-cli       CLI 入口 (`quant`)     │  │
│  │ quant-core      types / config         │  │
│  │ quant-data      parquet cache + 因子计算│  │
│  │ quant-factors   美股 + A 股 因子注册表 │  │
│  │ quant-strategy  scoring + MVO + regime │  │
│  │ quant-backtest  回测引擎 + FF5 回归    │  │
│  │ quant-download  FMP / FRED / Tushare   │  │
│  │ quant-trading   纸面 + 实盘交易（A 股）│  │
│  │ quant-db        PostgreSQL pool + ORM  │  │
│  └────────────────────────────────────────┘  │
└──────────────────────────────────────────────┘

文件命名规则（同名 + 前缀分流）:
  美股: us_xxx.rs / a 股: a_xxx.rs / 通用: xxx.rs

数据栈（仅 2 家）:
  FMP Ultimate (~$300/月)  — 财务/价格/EPS/insider/dividend
  FRED (永久免费)          — 12 个宏观指标
  (已废弃: Quiver / UW / Fiscal.ai / AlphaVantage)
```

## 常用命令（Rust CLI 统一入口 `quant`）

```bash
# === 编译 ===
cd quant-engine
cargo build --release -p quant-cli      # 输出 target/release/quant

# === 美股数据导入（FMP only，~$300/月 Ultimate plan） ===
quant download --source fmp --target all --start-year 1995    # 全量
quant download --source fmp --target daily_price              # 单端点
quant download --source fmp --target all --incremental        # 增量更新

# === 宏观数据（FRED 永久免费） ===
quant download --source fred --target all --start-year 2000

# === 美股因子分析 ===
quant analyze --start 2012-01-01 --end 2025-12-31 \
  --cache-dir ../cache --output ../output/factor_analysis

# === 美股回测（v25 baseline） ===
quant backtest --start 2012-01-01 --end 2025-12-31 \
  --cache-dir ../cache --output ../output/rust_v25
# 输出: α=13.28% t=3.40 / Sharpe 0.99 / Down Capture -8.62%

# === A 股 ===
quant --market cn factors --date 2025-12-31     # A 股因子
quant --market cn trade --date 2025-12-31 --signals signals.json   # A 股纸面交易

# === DB 状态 ===
quant db-status

```

## 配置

环境变量在 `.env`（项目根目录）。Rust CLI 通过 `dotenvy` 自动加载。

**最小配置（FMP + FRED + DB）**：
```
FMP_API_KEY=xxx              # FMP Ultimate plan
FRED_API_KEY=xxx             # FRED (永久免费)
DB_HOST=...                  # PostgreSQL 连接
DB_PORT=5432
DB_USER=...
DB_PASSWORD=...
DB_DATABASE=...
DB_SCHEMA=quant
```

**A 股额外**：`TUSHARE_TOKEN`

**Strategy 参数**（`quant-engine/config.toml`）：
- `[universe]`: min_market_cap = 1e10, min_daily_volume = 1e7
- `[execution]`: initial_capital = 1000000, slippage = 0.0005
- `[strategy]`: max_holdings, long_n, rebalance_interval
- `[optimizer]`: gross_leverage = 1.2, max_long_weight = 0.10, turnover_penalty = 0.01
- `[category_weights]`: value=0.7, quality=1.5, growth=1.5, momentum=2.5, analyst=1.2
- `[risk_controls]`: vol_targeting / dd_response 参数

## A股因子体系（Rust 实现 `quant-engine/crates/factors/src/a_share/`，下表为旧 Python 30 因子参考）

| 大类 | 权重 | 因子 |
|---|---|---|
| 价值 | 0.7 | EP, BP, DIV_YIELD |
| 质量 | 1.3 | ROE_TTM, GROSS_MARGIN, PROFIT_STB, MARGIN_TREND |
| 成长 | 1.0 | NET_PROFIT_YOY, REVENUE_YOY, NET_PROFIT_CAGR_3Y |
| 动量 | 0.9 | MOM_1M, MOM_3M, MOM_12M, REV_5D, IND_MOM, RESIDUAL_MOM, CMDTY_MOM |
| 技术 | 0.7 | TURN_20D, VOL_20D, PRICE_DEV_60D, SIZE, VOL_PRICE_DIV |
| 宏观 | 0.6 | MACRO_CYCLE, MACRO_LIQD, MACRO_INFL, MACRO_EXTR |
| 舆情 | 0.6 | POLICY_SENT, POLICY_INTENSITY, ANALYST_RATING, ANALYST_COVERAGE |

## 美股因子体系（Rust v25：71 因子 × 8 大类）

**实现位置**：`quant-engine/crates/factors/src/{value,quality,growth,momentum,defensive,analyst,alternative,...}/`

**v25 数据源**：仅 FMP + FRED（已禁用 Quiver 5 因子，alpha 仅退 0.25%/年）

**禁用的因子**（数据源付费 / IC 弱）：
- CONGRESS_NET_BUY / GOV_CONTRACT_FLOW / LOBBY_INTENSITY / DARK_POOL_SHORT / INST_OWNERSHIP_DELTA（Quiver 付费）
- NET_PROFIT_YOY (Winners/Losers 不区分)
- EPS_REVISION (FMP date 是 forecast period，misnamed)
- 4 对重复因子：SIZE/VOLATILITY_21D/TSMOM/QMJ_NET_PAYOUT 保留但与 LOG_MARKET_CAP/VOL_20D/MOM_12M/SHAREHOLDER_YIELD 等价

下面表格是旧 Python 31 因子参考，仅作历史对照：

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

**跨时代 FF5 Alpha 一致性验证（Python 旧引擎，待重新验证）：**

| 区间 | FF5 Alpha | t-stat | β_mkt | β_rmw | Sharpe | 超额年化 | 下行捕获 |
|------|-----------|--------|-------|-------|--------|---------|---------|
| 2000-2011（无 analyst 大类） | **6.18%** | **2.19** | 0.37 | -0.05 | 0.51 | +12.33% | 0.30 |
| 2012-2023（完整因子） | **6.58%** | **2.20** | 0.44 | -0.21 | 0.63 | +0.89% | 0.44 |

> ⚠️ **2026-04-30 重要更新**：发现 Python 引擎也有部分 bug（stop_cover 不限 cash + 候选集累积 + initial_capital=$100K），上面 Python 跑分**未用修复版引擎**重新核对，仅供历史参考。Rust 引擎修复后 2012-2025 真实表现见下方 **🚀 Rust 引擎 v21 baseline**。

---

## 🚀 Rust 引擎 v25 Baseline（2026-04-30，FMP+FRED only 机构级 alpha）

修完 10 个 engine + optimizer + scoring bug + 禁用 5 个 Quiver 付费因子后，Rust 引擎 14 年回测（**仅 FMP + FRED 免费/已订阅数据**）：

```
Total Return    +1293.65%      (Annual 20.76%)
Sharpe Ratio    0.99           ⭐ 机构级
FF5 α           13.28%         ⭐⭐⭐ Harvey-Liu-Zhu |t|>3 真 alpha
FF5 t-stat      3.40
Down Capture    -8.62%         ⭐⭐ S&P 跌月策略反赚
Capture Ratio   -8.51
Excess vs S&P   +7.98%/年
Max Drawdown    -29.29%
Calmar          0.71

数据成本：FMP Ultimate (~$300/月) + FRED (免费)
不需要：Quiver / Unusual Whales / Fiscal.ai / Alpha Vantage
(节约 ~$600+/年, alpha 仅退 0.25%/年 vs 含 Quiver 版)
```

**演进路径**（详见 [memory project_winner_signature_baseline_2026_04_30.md](.claude/projects/memory/)）：
1. 修 8 个 engine bug（floor() / sign-flip / cover starvation / margin call）
2. 修 optimizer 致命 bug（post-hoc gross scaling 把 net 0.6 压成 0.01）
3. 修 wiring bug（universe filter 不读 config / sector neutralize 默认关）
4. Layer 1 类内归总 weighted-avg → weighted-sum
5. Winner-signature 调权（MOM_12M/SUE_PEAD/EARNINGS_SURPRISE 进 T0）
6. EV_TO_FCF/EV_TO_EBIT 方向修正（empirical IC 与原 reverse 列表矛盾）
7. gross_leverage 1.0 → 1.2（轻杠杆放大 alpha）

**Rust 引擎复现命令：**
```bash
cd quant-engine
cargo run --release -p quant-cli -- backtest \
  --start 2012-01-01 --end 2025-12-31 \
  --cache-dir ../cache --output ../output/rust_v21
```

**Winner Signature 核心（14 年 30 大赢家中位数）**：
- MOM_12M: 0.37（vs Losers ~0）— 4/4 期持续，#1 信号
- REV_YoY: 21%（vs Losers 7%）
- RD_INTENSITY: 13%（vs Losers 2%，6× 差距）
- EV_TO_FCF: 113（贵！）vs Losers 7（便宜）— **传统 value 方向反了**
- NI_YoY: 32% ≈ Losers 33% — **不区分赢家/输家**（已删除）

**14 年大赢家 Top 10**（剔除杠杆 ETF）：
NVDA 530× / CELH 472× / TSLA 239× / AVGO 119× / AXON 106× / NFLX 90× / MPWR 59× / BLDR 49× / LRCX 46× / FICO 45× — 33% 是半导体（穿越 14 年最稳行业主题）。

## 核心设计决策

- **无前视偏差（全 31 因子已审计）：** 财务数据按 `filing_date <= date`（公告日）过滤，价格按 `trade_date <= date`，宏观 30 天 lag，情绪/另类数据用 trailing lookback 窗口
- **两层因子打分：** 类内动态分母 + 类间动态分母，`MIN_VALID_CATEGORIES=4`
- **MVO 优化器（v4）：** cvxpy + OSQP 替换 Top-N + Softmax。目标函数 `max μ̂'w − λ·w'Σw − γ·||w − w_prev||₁`，约束净敞口/总杠杆/单股上下限/行业 gross。Ledoit-Wolf 252D 协方差矩阵，求解失败自动降级 Top-N
- **Upsert 语义：** 所有数据库写入为幂等操作（`INSERT ... ON DUPLICATE KEY UPDATE`）
- **Regime 切换：** 四维复合（趋势+VIX+利差+拥挤度）+ Credit Veto
- **回测预加载：** `preload_for_backtest()` 一次性加载到内存，因子计算全部从内存过滤

## 舆情管道（Legacy Python，已归档）

`legacy_python/sentiment/scrapers/` 下 11 个中国政府网站爬虫 + CCTV新闻联播（AKShare）+ 巨潮公告 + 3 个 Twitter/X 美国政策爬虫 + Polymarket 预测市场桥接，共 20 个爬虫。当前 Rust 策略（v25）不依赖 sentiment 因子（POLYMARKET_SENT 全 0、IC 接近噪声）。后续如需 NLP sentiment 再决定是否迁移。

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
| Rust 引擎 v0.1 | 美股因子 + 回测 + MVO 优化器 (qrs-* 6 crates) | ✅ |
| **Rust 引擎 8 bug 修复** (2026-04-30) | floor()/sign-flip/cover-starvation/margin-call/post-hoc-gross-scaling | ✅ |
| **Layer 1 sum scoring** (2026-04-30) | 类内归总 weighted-avg → weighted-sum，FF5 α=8.24% (t=2.47) | ✅ |
| **Winner signature 审计** (2026-04-30) | 14 年 30 大赢家 vs 30 大输家因子签名，6 项调参 | ✅ |
| v21 baseline | α=13.53% t=3.47 / Sharpe 1.01（含 Quiver 5 因子） | ✅ |
| **🏆 v25 baseline** (2026-04-30) | **仅 FMP+FRED, α=13.28% t=3.40 / Sharpe 0.99 / Down Capture -8.62%** | ✅ |
| P1 空头 v5 | 独立因子模型 + Regime 渐进 + 融券约束 + 止损，待回测 | 🔄 |
| 待办 | Python 引擎同步修复 / 实盘验证 / 多周期 momentum 探索 | 📋 |

**当前待办（按优先级）：**

**P0.5 — 工业级架构补强 ✅ 已完成（Rust 实现）：**
- 风险模型：`quant-engine/crates/strategy/src/optimizer.rs`（Ledoit-Wolf 252D 协方差 Σ）
- MVO 优化器：Clarabel QP 求解器，目标 `max μ̂'w − λ·w'Σw − γ·||w − w_prev||₁`
- 约束（v25 production）：净敞口 0.6 / 总杠杆 1.2 / 单股 [-0.05, +0.10] / Σ|wᵢ| ≤ gross_leverage（QP 内联立约束，非 post-hoc scaling）

**因子分析结果（2026-04-21，79 因子 × 168 月）：**

| 级别 | ICIR 阈值 | 数量 | 代表因子 |
|------|----------|------|---------|
| T1 强信号 | ≥ 0.3 | 9 | FREE_FLOAT_PCT(+0.46), TURN_20D(+0.45), PIOTROSKI_F(+0.40), SUE_PEAD(+0.38), EV_TO_FCF(+0.37) |
| T2 有信号 | 0.15-0.3 | 21 | ESG_RISK, MOM_12M, DIV_YIELD, PROFIT_STB, TSMOM, INDUSTRY_MOM 等 |
| T3 弱信号 | 0.05-0.15 | 20 | PRICE_52W_HIGH, GROSS_MARGIN, OHLSON_O 等 |
| 方向翻转 | <0.05 但单年 >0.5 | 12 | EPS_REVISION, BP, MOM_1M, VOL_20D, ALTMAN_Z, BENEISH_M（滚动 IC 处理） |
| 真噪音 | <0.05 且无信号 | 1 | GEO_CONCENTRATION |

**当前待办（基于 v21 Rust baseline，2026-04-30）：**

**P0 — 验证 v21 鲁棒性：**
- 用 Walk-forward 切样本（2012-2018 IS，2019-2025 OOS）验证 winner-signature 不是过拟合
- 跑 1995-2011 数据看 alpha 是否穿越早期年份
- Python 引擎同步 8 个 bug 修复，做 Python ↔ Rust 交叉验证

**P1 — 突破 v21 局部最优（结构性改造）：**
- 多周期 momentum 组合（3M/6M/12M/24M）
- 行业 sleeve（Tech 单独配额，避免 sector neutralize 把 NVDA 拉平）
- Layer 2 也改 sum，加 quality × momentum 交互项
- 季频 vs 月频 rebalance

**P2 — 回测鲁棒性 / 风控：**
- 换手率控制（v21 年化 ~400%，工业级 ~150%）
- 滑点敏感性 / 交易成本压力测试
- Regime 参数扰动（Credit Veto / 拥挤度阈值）

**P3 — 数据修复**：INSIDER_NET_BUY 数据缺失 / INST_OWNERSHIP_DELTA 覆盖不足 / EPS_REVISION PIT 快照表

**P4 — 空头 v5**：独立因子模型 + Regime 渐进 + 融券约束 + 15% 止损

**P5 — 工程与实盘**：Alpaca 模拟盘已接入 + 实盘验证 3-6 月

**P7 — 长期架构升级**：
- PCA 统计风险因子（Barra 开源替代，前 20-30 主成分作风格因子）
- 多周期信号合成（日/周/月频分层）
- Alpha Bayesian Shrinkage（极值持仓抑制）
- 交易成本/市场冲击模型（写入优化器目标）
- Alpha Capture System（因子版本化 + 独立 IC 监控 + 灰度上线）
- Barra-style P&L 归因（每日拆解 factor return × β + specific return）


## Rust 计算引擎 (`quant-engine/`)

因子计算和回测引擎从 Python 迁移到 Rust，解决 macOS multiprocessing fork crash 并提升性能。**2026-04-30：完整迁移完成**，Python 全部归档（`legacy_python/`）。

**架构（Rust 单栈）：**
- 数据下载、DB 写入、因子计算、回测、A 股 trading 全部 Rust
- 美股 paper trading + Web API 待 Rust 实现

**技术栈：** Polars (parquet I/O) + rayon (并行) + nalgebra (线代) + clap (CLI)

```bash
cd quant-engine && cargo build --release

cargo run --release -- validate --cache-dir ../cache/           # 验证缓存文件
cargo run --release -- factors --date 2024-12-31 --cache-dir ../cache/  # 单日因子
cargo run --release -- backtest --start 2012-01-01 --end 2025-12-31 --cache-dir ../cache/  # 回测
cargo run --release -- analyze --start 2020-01-01 --end 2024-12-31 --cache-dir ../cache/   # IC + FM
```

**Workspace 结构：**
```
quant-engine/
├── crates/
│   ├── core/       核心类型 + 配置（TickerId, Config, Date）
│   ├── data/       Parquet 加载 + PriceGrid + DataCache + Universe filter
│   ├── factors/    71 个因子（v25 已禁用 5 个 Quiver 付费 + EPS_REVISION + NET_PROFIT_YOY）
│   ├── strategy/   scoring + MVO (Clarabel QP) + 滚动IC + Regime
│   ├── backtest/   T+0 回测引擎 + FF5 回归 + margin call
│   ├── download/   FMP / FRED / Tushare 下载器
│   ├── trading/    A 股纸面 + 掘金实盘
│   ├── db/         PostgreSQL pool + sqlx
│   └── cli/        CLI 入口 `quant`
```

### Rust 引擎 v25 baseline（2012-2025 14 年完整回测）

| 指标 | 值 |
|------|-----|
| Annual Return | **20.76%** |
| S&P 500 | 12.77% |
| **Excess vs S&P** | **+7.98%** |
| Sharpe | **0.99** ⭐ 机构级 |
| **FF5 α** | **13.28% (t=3.40)** ⭐⭐⭐ HLZ \|t\|>3 |
| Max Drawdown | -29.29% |
| Calmar | 0.71 |
| **Down Capture** | **-8.62%** ⭐⭐ 反向防守 |
| Annual Turnover | ~400% |
| 持仓数 | ~12（8 长 + 4 短） |
| 运行时间 | ~70s（14 年完整回测）|

### 策略架构

**分层组合（60% + 25% + 15%）：**
- **Tier 1 大盘核心**（$50B+, 15 只）— 质量 + 动量 + 分析师 + REVENUE_ACCELERATION
- **Tier 2 优质新股**（IPO < 2 年, 科技/医疗, 营收增速 > 20%, 7 只）
- **Tier 3 小盘动量**（$500M-$5B, 盈利, 动量 Top 10%, 4 只）
- **空头叠加**（熊市才开启, 4 维 Regime 检测, 20% 止损）

**持仓粘性：** Top 20% 才保留，跌出即换。换手率 249%（vs 无粘性 433%），excess 反而提升。

### 策略探索与结论

| 实验 | 结果 | 结论 |
|------|------|------|
| 行业中性化 | Sharpe 0.22→0.13 | Alpha 来自行业配置，中性化后选股 alpha 归零。**关闭** |
| 滚动 IC 方向翻转 | Sharpe 降到 -0.05 | IC 太噪，方向判断不可靠。**只用 ICIR 分级权重，不翻方向** |
| 空头（简单 Regime） | Annual -3.93% | 牛市做空被轧。**改为 4 维 Regime，牛市关闭空头** |
| 周频调仓 | Sharpe 0.57（更低） | 换手翻倍但收益不增，因子信号是月频的。**保持月频** |
| 持仓粘性 top 50% | Sharpe 0.37 | 太宽松，错过好股。**收紧到 top 20%** |
| 持仓粘性 top 20% | **Sharpe 0.58, Turnover 249%** | 换手减半，excess 翻倍。**最优配置** |
| 行业优先选股 | Sharpe 0.44 | 行业内选最大市值太无聊。**个股优先更好** |
| REVENUE_ACCELERATION | Sharpe 0.56→0.60 | 营收加速度（二阶导）是最有价值的新因子 |
| 纯多头 vs 多空 | 多头 Sharpe 0.22 | 15 只等权中盘股无法跟上 500 只市值加权大盘。**分层解决** |

### 因子分析关键发现（IC + Fama-MacBeth）

**IC 排名 Top 10（2020-2024, 60 月）：**

| Factor | ICIR | t-stat | 含义 |
|--------|------|--------|------|
| EV_TO_FCF | +0.93 | 7.07*** | 自由现金流估值 |
| EARNINGS_SURPRISE | +0.93 | 7.06*** | 盈利惊喜（PEAD） |
| PIOTROSKI_F | +0.78 | 5.93*** | 财务健康度 |
| QMJ_NET_PAYOUT | +0.74 | 5.67*** | 股东回报 |
| BUYBACK_YIELD | +0.74 | 5.64*** | 回购收益率 |
| PROFIT_STB | +0.72 | 5.49*** | 利润稳定性 |
| COMPOSITE_ISSUANCE | -0.69 | -5.24*** | 发股稀释（反向） |
| EV_TO_EBIT | +0.67 | 5.12*** | EBIT 估值 |
| AMIHUD_ILLIQ | -0.65 | -4.94*** | 流动性（反向） |
| SUE_PEAD | +0.60 | 4.58*** | 标准化盈利惊喜 |

**Fama-MacBeth 结论：** 76 因子多因子截面回归，最高 MOM_12M t=2.49，**无因子达到 HLZ |t|>3.0 显著性**。因子间共线性高，独立边际贡献弱。

**核心发现：** 因子体系的 alpha 主要来自行业配置 beta（超配科技/成长），不是截面选股。分层组合通过 Tier 1 大盘核心跟住基准，Tier 2/3 捕捉 IPO 和小盘动量提供额外 alpha。

### 性能优化路径

| 版本 | 总时间 | 优化 |
|------|--------|------|
| 初版 | 3:44 | 全量 33M 行 HashMap |
| +date 过滤 | 2:06 | Polars predicate pushdown |
| +rayon | 1:19 | 72 因子并行计算 |
| +PriceGrid | 31s | flat Vec O(1) 替代 HashMap |
| +iter_date_range | **47s (14年)** | 窗口因子不遍历全量 |

## 详细文档

- [A股策略算法](doc/A_SHARE_STRATEGY.md) — 算法描述（Rust 路径已注解）
- [美股策略算法](doc/US_SHARE_STRATEGY.md) — v25 baseline + 因子 tier 表
- [数据源详情](doc/DATA_SOURCES.md) — FMP + FRED only
- [部署指南](doc/DEPLOYMENT.md) — ECS + 台式机 Rust 部署
- [Legacy Python](legacy_python/README.md) — 归档说明 + 已知 bug
- [废弃文档](doc/deprecated/2026-04-30-archive/) — Polymarket / Vectorize 等历史
