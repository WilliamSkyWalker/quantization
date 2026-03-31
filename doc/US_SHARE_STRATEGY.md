# 美股量化策略文档

本文档说明美股量化系统的完整算法逻辑。系统包含三套独立策略，共享数据层和回测引擎。

---

## 一、系统概述

美股量化系统提供 **三套策略**，用户可在 CLI / API / 前端切换：

| 策略 | 代码 | 核心理念 | 适用场景 |
|------|------|---------|---------|
| **Alpha（多因子多空）** | `us_multi_factor.py` | 25 因子 × 7 大类，两层类别打分，多空对冲 | 追求超额收益 |
| **Beta（Regime 控制）** | `us_beta_strategy.py` | 不做选股，Regime 择时 + 质量筛选等权，吃市场 beta | 追求稳健、低回撤 |
| **Baseline / Alpha v2** | `us_baseline_strategy.py` | 委托 Alpha 打分 + 月频调仓，开发迭代用 | 策略实验、A/B 对比 |

核心管道：
```
FMP/UW/Fiscal.ai/FRED → 数据层 → 因子处理 → Alpha / Beta / Baseline 策略
→ 风控调整（Baseline 禁用） → 回测引擎 / 模拟交易
```

### Alpha 策略绩效（2015-2025，$100 万，基准 Russell 1000，含幸存者偏差修正）

| 指标 | Alpha 多空策略 | Russell 1000 |
|------|---------------|-------------|
| 年化收益 | **12.79%** | 11.38% |
| 超额年化 | **+1.41%** | — |
| 夏普比率 | **0.68** | — |
| 最大回撤 | **-16.3%** | — |
| FF5 Alpha (年化) | **+6.69%** | — |
| FF5 Alpha t-stat | **2.26** (统计显著) | — |
| β_Mkt | 0.40 | — |
| 年化换手率 | 324% | — |

### Beta 策略绩效（2015-2025，$100 万，基准 Russell 1000）

| 指标 | Beta 策略 | Russell 1000 |
|------|----------|-------------|
| 年化收益 | **6.9%** | 11.38% |
| 最大回撤 | **-16.5%** | — |
| Calmar 比率 | **0.42** | — |
| Sharpe 比率 | **0.33** | — |
| 下行捕获率 | **37.3%** | — |
| 上行捕获率 | **46.3%** | — |
| 年化波动率 | **8.6%** | ~15% |
| 年化换手率 | 156% | — |

> **Alpha 策略：** 25 因子 × 7 大类（两层类别打分，纯线性）。历史回测（23 因子）leave-one-out 分析剪除 6 个有害因子后 FF5 alpha +6.73%（t=2.20）。新增 EARNINGS_SURPRISE + EPS_REVISION 两因子待回测验证。ML blend（LightGBM）代码已集成但 train() 未在回测流程中调用，当前全部结果为纯线性因子。
> **Beta 策略：** 不追求选股 alpha，通过四维 Regime 感知动态调仓（牛市高仓位吃 beta，熊市低仓位 + 现金保护）。
> **幸存者偏差修正：** 股票池含 227 只历史 S&P 500 成分股。
> **样本外验证：** IC 权重优化经样本外测试证伪（2015-2019 训练 → 2020-2025 alpha≈0），因此回归等权。

---

## 二、数据源

三家付费 API 为主数据源（旧源 yfinance/EDGAR/SimFin 保留，CLI `--old-source` 可回退）。

统一下载器：`backend/services/data/bulk_downloader.py`

### 2.1 FMP (Financial Modeling Prep) — 主力数据源

| 数据 | FMP 端点 | 方式 | 说明 |
|------|----------|------|------|
| 全市场股票列表 | stock-screener | per-ticker | NYSE+NASDAQ+AMEX 共 ~13,700 只 |
| SP500 + NASDAQ 100 成分 | sp500_constituent, nasdaq_constituent | per-ticker | 含历史变更（幸存者偏差修正）|
| 日线行情 | historical-price-full | per-ticker | OHLCV + adjClose，5 ticker/批 |
| 季度财报 | income-statement-bulk | **bulk 按年** | 全市场 1995+，含 filingDate |
| Key Metrics | key-metrics-bulk | **bulk 按年** | PE/PB/ROE/EV 等 60+ 指标 |
| Financial Ratios | ratios-bulk | **bulk 按年** | 60+ 比率 |
| Earnings Surprise | earnings-surprises-bulk | **bulk 按年** | actual vs estimated EPS, 1995+ |
| EPS Consensus | analyst-estimates-bulk | **bulk 按年** | 分析师共识预期 |
| Insider Trading | insider-trading (v4) | per-ticker 分页 | SEC Form 4，2003+ |
| GICS 行业 | profile | per-ticker 50/批 | sector / industry |
| 分红/拆股 | stock_dividend, stock_split | per-ticker | 全历史 |
| 指数日线 | historical-price-full | per-ticker | ^GSPC, ^IXIC, ^DJI, ^RUI |
| 商品日线 | historical-price-full | per-ticker | 黄金/原油/天然气等（GC=F→GCUSD） |
| 宏观经济 | economic (v4), treasury (v4) | per-ticker | GDP/CPI/失业率/国债收益率等 |

配置：`FMP_API_KEY`（Ultimate 套餐 $149/月，3000 req/min）

### 2.2 Unusual Whales — 替代数据

| 数据 | 端点 | 说明 |
|------|------|------|
| 期权异常活动 | /api/option-trades/flow-alerts | 异常权利金、volume spike |
| 暗池交易 | /api/darkpool/recent | 机构 off-exchange 交易 |
| 国会交易 | /api/congress/recent-trades | 参议员/众议员股票交易披露 |
| 新闻 | /api/news/headlines | 带 ticker 关联和情绪标签 |

配置：`UW_API_KEY`（$150/月，100+ 端点）

### 2.3 Fiscal.ai — 日频估值

| 数据 | 端点 | 说明 |
|------|------|------|
| 日频 PE/PB/EV | /v1/daily-ratios | 每日估值比率时序（比季报更及时）|

配置：`FISCAL_API_KEY`（$99/月）

### 2.4 Fama-French 五因子

下载器：`backend/services/strategy/ff5.py`

从 Kenneth French Data Library 自动下载 FF5 日度因子收益（Mkt-RF, SMB, HML, RMW, CMA, RF），本地 CSV 缓存 30 天。

### 2.5 FRED 宏观（补充）

下载器：`backend/services/data/fred_downloader.py`

20 项 FRED 宏观指标（GDP、CPI、PPI、VIX 等）。FMP 宏观端点已可覆盖大部分，FRED 作为补充/备用。

### 2.6 旧数据源（保留，`--old-source` 回退）

- yfinance → `backend/services/data/fmp_downloader.py`（类名保留兼容）
- SEC EDGAR → `backend/services/data/edgar_downloader.py`
- SimFin → `backend/services/data/simfin_downloader.py`

---

## 三、因子体系（25 因子 × 7 大类）

代码位置：`backend/services/us_factors/`

### 设计原则

- 两层打分：类内因子加权平均 → 类间大类加权求和
- 动态分母：缺失因子不补零，按有效因子等比缩减权重
- 防前视偏差：财务数据按 `filing_date <= date` 过滤（非 report_date），SimFin 异常的 filing_date 自动加 45 天缓冲

### 因子清单

| 大类 | 权重 | 因子 | 说明 |
|------|------|------|------|
| **value** | 1.0 | EP, BP, DIV_YIELD, BUYBACK_YIELD | 盈利/账面/股息/回购收益率 |
| **quality** | 1.0 | ROE_TTM, GROSS_MARGIN, PROFIT_STB, MARGIN_TREND, ACCRUALS | 盈利能力+稳定性+应计异常 |
| **growth** | 1.0 | NET_PROFIT_YOY, REVENUE_YOY, NET_PROFIT_CAGR_3Y | 成长性 |
| **momentum** | 1.0 | MOM_1M, MOM_3M, MOM_12M, REV_5D | 多频率动量+反转 |
| **technical** | 1.0 | TURN_20D, VOL_20D, IVOL, SIZE | 流动性+波动率+规模 |
| **analyst** | 1.0 | US_ANALYST_RATING, US_ANALYST_COVERAGE, EARNINGS_SURPRISE, EPS_REVISION | 分析师评级+覆盖度+盈利惊喜+预期修正 |
| **sentiment** | 1.0 | POLYMARKET_SENT | Polymarket 预测市场情绪 |

**大类等权（所有权重 1.0），因子内等权。** 不做 IC 引导权重优化。

### 因子剪枝记录（leave-one-out alpha 分析，2015-2023 样本内）

**方法**：对每个因子，去掉该因子后重跑全量回测，计算 FF5 alpha 变化。
**Δα 定义**：`Δα = full_alpha - leave_one_out_alpha`。**Δα > 0 = 因子有价值**（去掉后 alpha 下降），**Δα < 0 = 因子有害**（去掉后 alpha 上升）。
**剪枝规则**：仅剪除 Δα < 0 的因子（有害因子）。

| 剪除因子 | 原大类 | α_without | Δα | 理由 |
|---------|--------|-----------|-----|------|
| **VOL_PRICE_DIV** | technical | +7.91% | **-4.30%** | 去掉后 alpha 反而升到 +7.91%，纯噪音 |
| **RESIDUAL_MOM** | momentum | +7.08% | **-3.46%** | 去掉后 alpha 升到 +7.08%，与 MOM_1M/3M/12M 冗余 |
| **4×MACRO** | macro | +3.87% | **-0.25%** 各 | 截面同值（宏观指标不区分个股），整体移除 |

保留的高价值因子（Δα > 0，去掉后 alpha 下降最多）：

| 因子 | α_without | Δα | 说明 |
|------|-----------|-----|------|
| REV_5D | -7.96% | **+11.57%** | 去掉后 alpha 从 +3.6% 暴跌到 -8.0%，短期反转最关键 |
| ACCRUALS | -4.12% | **+7.74%** | 利润质量信号 |
| REVENUE_YOY | -2.97% | **+6.59%** | 营收增速 |
| IVOL | -1.68% | **+5.30%** | 低波动率异象 |

剪枝效果：FF5 alpha 从 +3.6%(t=0.71) → **+6.73%(t=2.20)**，Sharpe 从 0.31 → 0.72。

**样本外验证待完成**：以上分析在 2015-2023 样本内完成，存在数据窥探风险。t 从 0.71 跳到 2.20 的幅度在真正的样本外（2024-2025）验证之前，置信度有限。计划：用 2024-2025 数据做样本外确认。

### 因子处理流水线

`backend/services/us_factors/processor.py`

```
原始因子值 → MAD 去极值(5σ) → GICS Sector+Size 中性化(OLS) → Z-Score 标准化 → ±3 截断
```

中性化模式按大类配置：
- **full**（sector + size）：value, quality, growth, technical
- **size_only**（保留行业 alpha）：momentum, analyst
- **none**：sentiment

### 防前视偏差机制

- **财务数据：** 严格按 `filing_date <= date` 过滤（非 report_date）
- **SimFin Publish Date 修正：** 预加载时检测 `filing_date <= report_date` 的异常数据，强制加 45 天安全缓冲（修正 173 条记录）
- **财务时效衰减：** 报告期距今 ≤3月: 100%，3-6月: 50%，6-9月: 25%，>9月: 负面信号

---

## 四、多空选股策略

代码：`backend/services/strategy/us_multi_factor.py`（USMultiFactorStrategy）

### 4.1 打分模型（两层类别评分）

1. **类内评分**：每个大类内因子加权平均（动态分母处理缺失因子）
2. **类间评分**：7 个大类加权求和 / 有效大类权重之和
3. **最低类别数**：`MIN_VALID_CATEGORIES=4`，不足 4 个有效大类的股票排除

### 4.2 惩罚与过滤

- **核心财务准入**：缺少 GROSS_MARGIN 的股票直接排除
- **缺失因子惩罚**：缺失 > 20% 时线性压缩得分，最大惩罚 50%
- **财务时效衰减**：报告期距今 > 9 月的股票财务因子置负
- **价值陷阱**：value > 0 且 quality < -0.5 时压缩 value 得分
- **趋势过滤**：MOM_12M < -1.0 的股票最终得分打折
- **行业趋势过滤**：行业中位 MOM_12M < -0.5 时，该行业全体打折

### 4.3 多空选股（核心）

```
因子得分排序: [+3.0 ... +0.5 ... 0 ... -0.5 ... -3.0]
              ├── Top N (LONG) ──┤      ├── Bottom M (SHORT) ──┤
```

**多头（Long）：** 得分 ≥ `US_MIN_SELECT_SCORE`(0.0) 的 Top-N 股票。
- 默认 `US_LONG_N=15`，Regime 联动缩减（熊市更少多头）。
- Softmax 权重分配，`tau=1.5`。

**空头（Short）：** 得分 ≤ `US_SHORT_SCORE_THRESHOLD`(-0.8) 的 Bottom-M 股票。
- 默认 `US_SHORT_N=10`，熊市增加空头（+30%）。
- 反向 Softmax（越差的得分权重越高），取负。

**净敞口：** `US_NET_EXPOSURE=0.6`
- 多头总权重 = (1 + 0.6) / 2 = **80%**
- 空头总权重 = (1 - 0.6) / 2 = **-20%**
- Regime 动态调整：牛市 net=0.6，熊市 net→0.2

**ML Blend（未启用）：** LightGBM 代码已集成（`us_ml_scorer.py`），但 `train()` 未在回测/选股流程中被调用，`model` 始终为 None，`predict()` 不执行。当前全部回测结果为纯线性因子打分。需要实现滚动训练（expanding window）后才能安全启用。

### 4.4 复合 Regime 检测（四维 + Credit Veto）

`backend/services/strategy/us_regime.py`（USRegimeDetector）

四维复合指标投票决定牛/熊：

| 维度 | 数据源 | 牛市信号 | 熊市信号 | 权重 |
|------|--------|---------|---------|------|
| 趋势 | S&P 500 vs 60日MA | 偏离 ≥ +5% | 偏离 ≤ -5% | 35% |
| 波动率 | VIX 252日历史百分位 | < 20th percentile | > 80th percentile | 30% |
| 信用 | 10Y-2Y 国债利差 | 正利差 > 0.5% | 倒挂 < -0.5% | 25% |
| 拥挤度 | 动量因子截面分散度 | 高分散（因子有效） | 低分散（因子拥挤） | 10% |

**Regime Strength = 0.35 × trend + 0.30 × vol + 0.25 × credit + 0.10 × crowding**

**Credit Veto：** credit < 0.2 时 strength 封顶 0.5。
**因子拥挤惩罚：** crowding < 0.3 且 strength > 0.6 时，strength 额外压缩 20%。

Regime 影响：
- 大类权重：Regime 联动（`US_REGIME_BEAR_OVERRIDES` 配置）
- 多头数量：熊市缩减（`BEAR_HOLDINGS_RATIO=0.6`）
- 空头数量：熊市增加
- 净敞口：牛市 60% → 熊市 20%

### 4.5 纯月频调仓

- 固定频率：每 `US_REBALANCE_INTERVAL=20` 个交易日（约月频）
- **不使用偏离触发** — 回测验证偏离触发产生噪音交易，删除后收益提升、换手率下降
- 年化换手率：~320-400%

---

## 4B、Beta 策略（Regime 驱动仓位控制）

代码：`backend/services/strategy/us_beta_strategy.py`（USBetaStrategy）

### 设计理念

不做选股 alpha，收益来自市场 beta，价值来自少亏（熊市保护）。
评价标准：Calmar 比率、最大回撤、下行/上行捕获率（不追求 FF5 alpha）。

### 核心流程

```
Regime 检测（四维） → 目标仓位 (10%~90%) → 质量筛选等权持仓 → 现金管理
```

### 仓位决定

Regime strength [0, 1] 线性映射到 equity_pct [10%, 90%]：
- 强牛（strength ≥ 0.8）：equity ~75-90%
- 震荡（0.3~0.8）：equity ~34-75%
- 强熊（strength ≤ 0.2）：equity ~10-26%

### 选股逻辑（质量筛选，非 alpha 追求）

1. 获取清洗后的股票池（~550 只）
2. 计算简化版 Gross Profitability = revenue × gross_margin / total_assets
3. 保留 GP 前 50%（过滤掉最差的一半）
4. 从中等权选 30 只（`_N_HOLDINGS=30`）
5. 每只权重 = (1/30) × equity_pct

### 核心特征

| 特征 | 说明 |
|------|------|
| 下行保护 | 下行捕获率 37%（市场跌 10% 只跟跌 3.7%） |
| 低波动 | 年化 8.6%（市场 ~15%） |
| 低换手 | 156%/年（月频调仓 + 持仓稳定） |
| 无做空 | 纯多头 + 现金，无借券费 |

---

## 五、风控

代码：`backend/services/risk/us_risk_manager.py`（USRiskManager）

| 控制 | 参数 | 说明 |
|------|------|------|
| 流动性 | `US_MIN_DAILY_VOLUME=$1M` | 20 日平均美元成交额过滤 |
| 个股上限 | `US_MAX_SINGLE_WEIGHT=10%` | 多空对称：±10% |
| 行业上限 | `US_MAX_SECTOR_WEIGHT=25%` | GICS Sector，多空分别限制 |
| 总敞口 | `US_GROSS_EXPOSURE_CAP=1.5` | |weight| 总和上限 |
| 波动率目标 | `US_TARGET_VOL=16%` | 实现波动率 / 目标波动率缩放 |
| 回撤响应 | `5%~15%` → `1.0~0.4` | 线性降仓 |
| 策略动量 | `MA120` | NAV < 120 日均线且仍在下跌 → 降仓 |

---

## 4C、Baseline / Alpha v2 实验策略

代码：`backend/services/strategy/us_baseline_strategy.py`（USBaselineStrategy）

**用途**：Alpha v2 开发迭代框架。委托 `USMultiFactorStrategy` 进行 25 因子打分+选股，月频调仓。也用于 VQM 基线验证（历史，已完成）。

**当前配置**（Alpha v2 Step 3.5）：
- 因子打分：委托 USMultiFactorStrategy（25 因子 × 7 大类，两层类别评分）
- 选股：USMultiFactorStrategy._select_from_scores（Top-15 long + Bottom-10 short, Softmax）
- 调仓：月频（每月最后交易日）
- 风控：引擎层 risk_controls=False（不使用 vol targeting/drawdown response）

### VQM 基线验证（历史记录）

早期用 3 因子（EP+ROE+MOM_12M）dollar-neutral 验证回测引擎，alpha=-14%，确认引擎正确但 VQM 因子在 2016-2023 不可用（详见第十四节 Step 1）。

### 数据清洗记录

回测过程中发现并修复的数据问题：
- **eps 字段**：28 条值异常（EDGAR 部分时期返回 net_income 而非 per-share EPS），置 NULL
- **roe 字段**：156 条值异常（>500%），置 NULL 后从 net_income/total_equity 重算
- **单位不一致**：21 条记录（12 tickers）total_assets/total_equity 单位混乱（EDGAR 用 USD vs SimFin 用百万），按 ticker 中位数为锚自动缩放修复
- **EPS 符号**：69 条 eps 与 net_income 正负不一致，置 NULL
- **ROE 全表重算**：23183 条统一用 `net_income / total_equity * 100` 重算，clip [-500, 500]

### CLI 用法

```bash
python3 backend/cli.py backtest --market us --strategy-type baseline --start 2015-01-01 --end 2023-12-31
```

---

## 六、回测引擎

代码：`backend/services/strategy/us_backtest.py`（USBacktestEngine）

### 执行规则

| 维度 | 美股 | A股对比 |
|------|------|---------|
| 结算 | T+0 当日收盘执行 | T+1 次日开盘 |
| Lot | 1 股 | 100 股 |
| 佣金 | 0（零佣金） | 万 7.5 |
| 印花税 | 0 | 0.1% 卖出 |
| 滑点 | 5bps | 10bps |
| 涨跌停 | 无 | ±10% |
| 做空 | 支持（负权重） | 不支持 |
| 调仓 | 纯月频（20 交易日） | 半月频 + 偏离触发 |
| 基准 | **Russell 1000 (^RUI)** | CSI 300 |
| 借券费 | **1.5% 年化** | — |

### 订单执行流程

```
1. 平仓阶段（释放资金）：
   - 卖出多头减仓 → cash += 卖出金额
   - 买入空头平仓 → cash -= 买回金额

2. 建仓阶段：
   - 买入多头建仓 → cash -= 买入金额
   - 卖出空头建仓 → cash += 卖空收入
```

### NAV 计算

```
NAV = (cash + Σ(shares × price)) / initial_capital
```
- 多头：shares > 0，price 上涨 → NAV 增加
- 空头：shares < 0，price 下跌 → NAV 增加

---

## 七、模拟交易

代码：`backend/services/execution/us_paper_trader.py`（USPaperTrader）

4 张 DB 表：`us_paper_account`, `us_paper_position`, `us_paper_transaction`, `us_paper_nav`

- T+0 结算，支持做空（负 volume）
- 默认初始资金 $100,000
- `sync_position(target_weights)` 自动调仓到目标权重

---

## 八、数据库表

14 张美股相关表（`backend/services/data/database.py`）：

| 表 | 说明 |
|----|------|
| `us_stock_basic` | 股票基本信息（ticker, market_cap, ipo_date） |
| `us_daily_price` | 日线行情（OHLCV + adj_close） |
| `us_financial_data` | 季度财报（FMP bulk 1995+） |
| `us_industry_class` | GICS 行业分类 |
| `us_index_daily` | 指数日线 |
| `us_macro_indicator` | FRED 宏观指标 |
| `us_commodity_price` | 商品期货日线 |
| `us_analyst_recommendation` | 分析师评级 |
| `us_sec_filing` | SEC 公告 |
| `us_corporate_action` | 公司行动（分红/拆股）|
| `us_paper_account` | 模拟账户 |
| `us_paper_position` | 模拟持仓 |
| `us_paper_transaction` | 模拟交易记录 |
| `us_paper_nav` | 模拟 NAV 历史 |

---

## 九、API 端点

| 端点 | 方法 | 说明 |
|------|------|------|
| `/api/us/universe` | GET | 可交易股票池 |
| `/api/us/select` | POST | 运行多空选股 |
| `/api/us/backtest/run` | POST | 运行回测（参数 `strategy`: `alpha`\|`beta`） |
| `/api/us/paper/account` | GET | 模拟账户信息 |
| `/api/us/paper/positions` | GET | 模拟持仓 |
| `/api/us/paper/nav` | GET | NAV 历史 |
| `/api/us/paper/trade` | POST | 执行模拟交易 |
| `/api/us/paper/reset` | POST | 重置模拟账户 |

---

## 十、CLI 命令

```bash
python3 backend/cli.py backtest --market us --strategy-type alpha  # Alpha 策略回测（默认）
python3 backend/cli.py backtest --market us --strategy-type beta   # Beta 策略回测
python3 backend/cli.py backtest --market us --start 2015-01-01     # 指定起始日期
python3 backend/cli.py select --market us --date 2025-01-15        # 多空选股
python3 backend/cli.py factor calc EP --market us                  # 单因子计算
python3 backend/cli.py factor list --market us                     # 因子列表
python3 backend/cli.py score AAPL --date 2025-01-15                # 单股得分
python3 backend/cli.py paper status --market us                    # 模拟账户
python3 backend/cli.py paper trade --market us                     # 执行交易
python3 backend/cli.py data bulk-import --source fmp --target all --start-year 1995  # FMP 全量导入
python3 backend/cli.py data bulk-import --source uw --target all   # Unusual Whales 全量
python3 backend/cli.py data download --market us --target simfin --old-source  # SimFin 旧源
```

---

## 十一、配置参数

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `US_SHORT_ENABLED` | `1` | 做空开关 |
| `US_LONG_N` | `15` | 多头持仓数 |
| `US_SHORT_N` | `10` | 空头持仓数（实际受门槛限制，通常 5-8 只） |
| `US_SHORT_SCORE_THRESHOLD` | `-0.8` | 做空得分门槛（收紧后减少空头换手） |
| `US_NET_EXPOSURE` | `0.6` | 净敞口（牛市 80%多/20%空，熊市动态→0.2）|
| `US_GROSS_EXPOSURE_CAP` | `1.5` | 总敞口上限 |
| `US_REBALANCE_INTERVAL` | `20` | 调仓间隔（交易日，纯月频） |
| `US_SLIPPAGE` | `0.0005` | 滑点 5bps |
| `US_SHORT_BORROW_FEE` | `0.015` | 借券费 1.5% 年化 |
| `US_MAX_SINGLE_WEIGHT` | `0.10` | 个股权重上限 ±10% |
| `US_MAX_SECTOR_WEIGHT` | `0.25` | 行业权重上限 ±25% |
| `US_BENCHMARK_INDEX` | `^RUI` | 回测基准（Russell 1000） |
| `US_REGIME_INDEX` | `^GSPC` | Regime 检测基准（S&P 500） |
| `US_CATEGORY_WEIGHTS` | 全等权 1.0 | 大类权重（等权，不做 IC 引导优化） |
| `FMP_API_KEY` | — | FMP API Key（主数据源） |
| `UW_API_KEY` | — | Unusual Whales API Key |
| `FISCAL_API_KEY` | — | Fiscal.ai API Key |
| `FRED_API_KEY` | — | FRED API Key（宏观补充） |
| `SIMFIN_API_KEY` | — | SimFin API Key（旧源，`--old-source` 用） |

---

## 十二、Polymarket P&L 分析器（独立模块）

代码：`backend/services/polymarket/polymarket_pnl_analyzer.py`

这是一个独立的事件驱动 P&L 分析工具（非多因子策略），基于 Polymarket 预测市场告警触发美股建仓，计算持有 N 天的收益。与上述多因子系统独立运行。

---

## 十三、与 A 股系统的核心差异

| 维度 | A 股 | 美股 |
|------|------|------|
| 标识符 | `ts_code` (600000.SH) | `ticker` (AAPL) |
| 结算 | T+1 | T+0 |
| 做空 | 不支持 | **多空对冲** |
| 行业分类 | 申万（31 类） | GICS（11 Sector） |
| 涨跌停 | ±10% | 无 |
| 佣金 | 万 7.5 + 印花税 0.1% | 零佣金 |
| 复权 | `close × adj_factor` | `adj_close` 直接使用 |
| 因子数 | 30（含舆情） | **25**（7 大类：value/quality/growth/momentum/technical/analyst/sentiment）|
| 财报来源 | Tushare（ann_date 过滤） | **FMP** bulk 1995+（filing_date 过滤 + 45 天缓冲）|
| 宏观指标 | 中国 PMI/SHIBOR/PPI/M2 | 美国 ISM/FEDFUNDS/CPI/VIX |
| Regime | CSI 300 均线偏离（单维） | **四维复合**（趋势+VIX+利差+拥挤度）+ Credit Veto |
| 调仓 | 半月频 + 偏离触发 | **纯月频**（偏离触发已删除） |
| 基准 | CSI 300 | **Russell 1000** |
| 幸存者偏差 | 未修正 | **已修正**（含 227 只历史成分股） |
| FF5 回归 | 无 | **有**（Alpha v2: alpha 6.73%，t=2.20） |
| ML 增强 | 无 | LightGBM 代码已集成但未启用（train 未调用） |
| 配置前缀 | 无前缀 | `US_` 前缀 |

---

## 十四、Alpha v2 开发路线（当前）

### 背景

Baseline 验证（4C 节）证实引擎和数据管道正确，但经典 VQM 因子在 2016-2023 不可用。Alpha v1（29 因子，20 日调仓）FF5 alpha +6.69%（t=2.26）。Alpha v2 在此基础上剪枝到 23 因子 + 月频调仓，alpha +6.73%（t=2.20）。注：ML blend（LightGBM）虽然代码已集成但 `train()` 从未在回测流程中被调用，当前结果为**纯线性因子**，无 ML look-ahead bias。

### 开发阶段

| 阶段 | 目标 | 结果 |
|------|------|------|
| **Step 1** | 4 因子 dollar-neutral baseline | ❌ alpha=-15%，dollar-neutral 在 2016-2023 不可行（价值因子历史最差十年） |
| **Step 2** | +Regime 动态净敞口（4因子） | ⚠️ alpha +1%（t=0.31），4 因子选股力不足 |
| **Step 3** | 29 因子（纯线性）+ v1 选股 | ✅ alpha=+3.62%（t=0.71），年化 9.5% |
| **Step 3.5** | Leave-one-out 因子剪枝（29→23） | ✅ **alpha=+6.73%（t=2.20 显著），年化 17.2%，Sharpe 0.72** |
| **Step 4** | 截面风控（行业软约束15%），替代时序风控 | ✅ DD -29.8%→-24.7%，alpha +6.73%→+2.56%（行业配置贡献 ~4%） |
| **Step 4.5** | 2024-2026 样本外验证 | ✅ **alpha 消失**（-1.61%, t=-0.23），下行捕获 99%，详见下方 |
| **Step 5** | 盈利预期修正因子（EPS revision）+ LLM 舆情增强 | 📋 验证增量 alpha |

### Step 3→3.5 详细结果（当前最佳版本）

代码：`us_baseline_strategy.py`（USBaselineStrategy）委托 `USMultiFactorStrategy` 打分+选股，月频调仓。

Step 3 使用全部 29 因子（含 6 个后来被剪掉的有害因子），alpha=+3.62%（t=0.71，不显著）。
Step 3.5 通过 leave-one-out 分析剪除 6 个因子（VOL_PRICE_DIV、RESIDUAL_MOM、4×MACRO），alpha 跃升至 +6.73%。

| 指标 | 剪枝后 (23因子) | 剪枝前 (29因子) | Alpha v1 (对照) |
|------|----------------|----------------|----------------|
| 年化收益 | **17.20%** | 9.46% | 12.79% |
| 最大回撤 | -29.75% | -30.85% | **-16.32%** |
| Sharpe | **0.72** | 0.31 | 0.68 |
| FF5 Alpha | **+6.73% (t=2.20)** | +3.62% (t=0.71) | +6.69% (t=2.26) |
| β_mkt | 0.82 | 0.47 | 0.40 |
| 超额年化 | **+7.53%** | -0.22% | +1.41% |
| 交易笔数 | 2196 | 4652 | — |

**注意**：虽然配置 `US_ML_SCORING_ENABLED=True`，但 LightGBM 的 `train()` 从未在回测流程中被调用（model=None），`predict()` 不执行。所有回测结果均为**纯线性因子打分**，无 ML look-ahead bias。

**leave-one-out 分析说明**：在同一回测期（2015-2023）内，对每个因子单独执行"去掉该因子→重算分数→跑回测→测 FF5 alpha"的 leave-one-out 实验。单因子 Δα 最大 +11.57%（REV_5D），存在过拟合风险，剪枝决策仅基于 Δα 方向（正/负），不依赖具体幅度。

**关键发现**：
- 剪枝后交易笔数减半（4652→2196），信噪比显著改善
- 回撤 -29.8% 集中在 COVID（2020-03-23）

**β_mkt 暴露变化的影响**：v2 的 β_mkt=0.82 远高于 v1 的 0.40，策略性质从"低 beta 对冲"变为"接近主动多头"。超额年化 +7.53% 中有相当部分来自 beta 暴露增加（2015-2023 是牛市），而非纯选股能力提升。Beta 调整后的选股 alpha 需要通过 FF5 回归的截距项（已扣除 β_mkt 影响）来评估：v2 的 FF5 alpha=+6.73% 与 v1 的 +6.69% 基本持平，说明**纯选股能力相当，超额收益差异主要来自 beta 暴露**。

**风控悖论**：已验证加入现有时序风控（vol targeting + drawdown response）后 t-stat 从 2.20 跌到 ~1.1，alpha 显著性消失。原因：多空策略的回撤通常是风格切换（如价值股被抛售），此时降仓等于在风格极值点割肉，错过后续均值回归。

### Step 4 结果：截面风控替代时序风控

**放弃**：vol targeting / drawdown response 参数调优（时序风控在 L/S 策略中适得其反：风格极值点降仓 → 错过均值回归）。

**实施方案**：软行业约束 `MAX_SECTOR_NET_WEIGHT=15%`（单行业净敞口上限），空头阈值从 -0.8 放宽到 -0.3（增加空头数量）。

| 指标 | 软约束 15% | 强制行业中性 | 无约束 (Step 3.5) |
|------|-----------|-------------|-------------------|
| 年化收益 | **10.87%** | 7.70% | 17.20% |
| 最大回撤 | **-24.7%** | -24.6% | -29.8% |
| FF5 Alpha | **+2.56% (t=0.94)** | -0.09% (t=-0.03) | +6.73% (t=2.20) |
| β_mkt | 0.64 | 0.63 | 0.82 |

**关键发现**：Step 3.5 的 alpha=+6.73% 中约 4% 来自行业配置（超配 Tech），~2.5% 来自行业内选股。强制行业中性后 alpha 归零，说明因子的截面选股能力有限，真正的信号在行业间。

### Step 4.5 结果：样本外验证（2024-01 ~ 2026-03）

| 指标 | 样本外 (2024-2026) | 样本内 (2015-2023) |
|------|-------------------|-------------------|
| 年化收益 | **12.16%** | 10.87% |
| 最大回撤 | **-20.62%** | -24.7% |
| Sharpe | **0.51** | 0.46 |
| FF5 Alpha | **-1.61% (t=-0.23)** | +2.56% (t=0.94) |
| β_mkt | 0.65 | 0.64 |
| 下行捕获 | **99.4%** | 58.0% |

逐年表现：2024 +16.5%，2025 +5.9%，2026 YTD +4.3%。

**样本外结论**：
- **收益维持**（年化 12.16%），但完全由 beta 驱动（β_mkt=0.65 × 市场年化 ~15%）
- **FF5 alpha 消失**（-1.61%，t=-0.23）：样本内的行业配置 alpha 在样本外不持续
- **下行保护失效**：下行捕获 99.4%（几乎跟跌），空头对冲未起作用
- **策略实质**：β≈0.65 的被动指数跟踪器，没有稳健的选股 alpha

**诚实评估**：原 23 因子体系在行业内缺乏选股能力（已扩展至 25 因子，新增 EARNINGS_SURPRISE + EPS_REVISION 待验证），样本内 alpha 主要来自行业配置（超配 Tech），此优势在样本外行业轮动模式改变后消失。需要引入真正具有截面区分力的因子（如盈利预期修正 EPS revision）才可能产生可持续的选股 alpha。

### 设计原则

- **每步独立可测**：每个阶段前后跑 FF5 回归对比，alpha 增量不显著则回滚
- **因子选择基于样本外验证**：不做 IC 引导权重优化（v1 已证明是数据窥探）
- **三层同步**：每步完成后同步 CLI / API / 前端 / 文档
