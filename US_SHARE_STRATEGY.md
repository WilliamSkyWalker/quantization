# 美股量化策略文档

本文档说明美股量化系统的完整算法逻辑。系统包含两套独立策略，共享数据层和回测引擎。

---

## 一、系统概述

美股量化系统提供 **两套策略**，用户可在 CLI / API / 前端切换：

| 策略 | 代码 | 核心理念 | 适用场景 |
|------|------|---------|---------|
| **Alpha（多因子多空）** | `us_multi_factor.py` | 4 因子选股 + 多空对冲，追求 FF5 alpha | 追求超额收益 |
| **Beta（Regime 控制）** | `us_beta_strategy.py` | 不做选股，Regime 择时 + 质量筛选等权，吃市场 beta | 追求稳健、低回撤 |

核心管道：
```
yfinance/SimFin/FRED → 数据层 → 因子处理 → Alpha 或 Beta 策略
→ 风控调整 → 回测引擎 / 模拟交易
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

> **Alpha 策略：** 4 个美股专属因子等权，不做 IC 引导权重优化。删除 25 个从 A 股移植的噪音因子。
> **Beta 策略：** 不追求选股 alpha，通过四维 Regime 感知动态调仓（牛市高仓位吃 beta，熊市低仓位 + 现金保护）。
> **幸存者偏差修正：** 股票池含 227 只历史 S&P 500 成分股。
> **样本外验证：** IC 权重优化经样本外测试证伪（2015-2019 训练 → 2020-2025 alpha≈0），因此回归等权。

---

## 二、数据源

### 2.1 行情数据（yfinance）

下载器：`backend/services/data/fmp_downloader.py`


| 数据 | 来源 | 说明 |
|------|------|------|
| 股票列表 | Wikipedia + yfinance | S&P 500 + NASDAQ 100 成分股 |
| 日线行情 | `yf.download()` | OHLCV + adj_close，50 ticker/批，8 线程 |
| 季度财务 | `Ticker.quarterly_*` | 利润表/资产负债表/现金流（最近 4-8 季度）|
| GICS 行业 | `Ticker.info` | sector / industry |
| 分析师评级 | `Ticker.upgrades_downgrades` | 券商升降级 |
| 公司行动 | `Ticker.dividends` / `splits` | 分红、拆股 |

### 2.2 历史财报（SEC EDGAR + SimFin）

**SEC EDGAR**（主力）：`backend/services/data/edgar_downloader.py`

从 SEC XBRL API 下载全量历史季报（2010 年起），每家公司一个 JSON。完全免费，无 API 限制（bulk download），包含关键的 `filed` 日期（SEC 提交日 = 防前视偏差）。XBRL 标签自动映射到标准化字段（Revenue、NetIncomeLoss、Assets、StockholdersEquity 等）。

当前数据：**35,295 条季报**，609 只股票（含历史成分股），2010-2026。

**SimFin**（补充）：`backend/services/data/simfin_downloader.py`

SimFin 免费版提供 5 年历史季报（2020 起），字段标准化程度高，作为 EDGAR 的交叉验证源。

配置：`SIMFIN_API_KEY`（免费注册 https://www.simfin.com）

### 2.3 宏观数据（FRED）

下载器：`backend/services/data/fred_downloader.py`

20 项 FRED 宏观指标：GDP、CPI、PPI、FEDFUNDS、M2、VIX、USD 指数等。

### 2.4 Fama-French 五因子

下载器：`backend/services/strategy/ff5.py`

从 Kenneth French Data Library 自动下载 FF5 日度因子收益（Mkt-RF, SMB, HML, RMW, CMA, RF），本地 CSV 缓存 30 天。用于回测后的 alpha 回归分析。

### 2.5 历史成分股（幸存者偏差修正）

代码：`backend/services/data/historical_universe.py`

从 Wikipedia S&P 500 变更记录提取 2015 年以来被移除的 227 只股票，加入股票池（标记 `is_active=0`）。下载它们的行情（yfinance）、财报（SEC EDGAR）、行业分类（yfinance info）。回测时按当日是否有交易数据决定是否纳入股票池，实现 Point-in-Time 宇宙。

修正效果：幸存者偏差贡献了约 2-3%/年的虚假 alpha（t-stat 从 2.21 降到 1.69）。

### 2.6 指数与商品

- 指数：^GSPC（S&P 500）、^IXIC（NASDAQ）、^DJI（Dow Jones）、**^RUI（Russell 1000，回测基准）**
- 商品：GC=F（黄金）、CL=F（原油）、NG=F（天然气）等 9 种

---

## 三、因子体系（4 个核心因子，美股专属设计）

代码位置：`backend/services/us_factors/`

### 设计原则

A股因子不能直接用于美股（机构主导 vs 散户主导、信息效率不同、因子有效性不同）。因子选择标准：
1. **学术验证在美股市场有效**（非 A 股移植）
2. **因子间相关性低**（IC 分析确认）
3. **数据在现有体系可获取**

### 核心因子

| 因子 | 类别 | 学术来源 | 10年ICIR | 说明 |
|------|------|---------|---------|------|
| **MOM_12_1** | 动量 | Jegadeesh 1993 | **+0.229** | 12-1 月动量（跳过最近 1 月避免反转噪音） |
| **Shareholder Yield** | 估值 | 综合 | **+0.209** | DIV_YIELD + BUYBACK_YIELD 合并 |
| **IVOL** | 技术 | Ang 2006 | **+0.172** | 特质波动率取反（低 IVOL 溢价） |
| **Gross Profitability** | 质量 | Novy-Marx 2013 | +0.114 | Gross Profit / Total Assets |

**等权合成，不做 IC 引导权重优化。**

### 删除因子及理由

从 A 股移植的 29 个因子中，25 个被删除：

| 删除因子 | 理由 |
|---------|------|
| EP, BP | 美股价值因子自 2017 年后 IC 持续下滑 |
| ROE_TTM, PROFIT_STB, MARGIN_TREND | 与 Gross Profitability 高相关，且 IC 更低 |
| MOM_1M/3M, REV_5D, RESIDUAL_MOM | 与 MOM_12_1 高相关，稀释信号 |
| TURN/VOL_20D, SIZE, VOL_PRICE_DIV | IC 接近零或不稳定 |
| 4 个宏观因子 | 截面 IC 为零（同值无法区分个股） |
| 2 个分析师因子 | IC 低且滞后 |
| ACCRUALS | 10 年 ICIR=0.036，接近零（在大盘股+低覆盖池中无效） |
| NET_PROFIT_YOY 等 3 个成长 | IC 不稳定 |

### 因子处理流水线

`backend/services/us_factors/processor.py`

```
原始因子值 → MAD 去极值(5σ) → GICS Sector 中性化(OLS) → Z-Score 标准化 → ±3 截断
```

### 逐年 IC 稳定性（2015-2025 季度截面）

```
Factor        2015  2016  2017  2018  2019  2020  2021  2022  2023  2024  2025  ICIR
MOM_12_1       N/A +0.08 -0.04 -0.21 +0.19 +0.36 -0.17 +0.10 +0.11 +0.04 +0.13 +0.229
ShrYield     +0.18 +0.08 -0.15 +0.21 +0.03 -0.02 +0.07 +0.06 -0.03 -0.04 +0.03 +0.209
IVOL         +0.17 +0.05 -0.23 +0.02 +0.10 +0.23 +0.06 +0.11 -0.01 -0.21 +0.08 +0.172
GrossProfit  -0.01 -0.08 +0.01 +0.02 +0.21 +0.18 +0.01 -0.00 +0.06 -0.00 -0.08 +0.114
```

### 防前视偏差机制

- **财务数据：** 严格按 `filing_date <= date` 过滤（非 report_date）
- **SimFin Publish Date 修正：** 预加载时检测 `filing_date <= report_date` 的异常数据，强制加 45 天安全缓冲（修正 881 条记录）
- **SimFin 和 yfinance 数据合并时，SimFin 的 Publish Date 优先**

---

## 四、多空选股策略

代码：`backend/services/strategy/us_multi_factor.py`（USMultiFactorStrategy）

### 4.1 打分模型

4 个因子等权合成：`score = mean(MOM_12_1_z, ShrYield_z, IVOL_z, GrossProfit_z)`

每个因子经过 MAD 去极值 + Z-Score 标准化后取平均。动态分母处理缺失因子（如某只股票无财报数据则 GrossProfit 缺失，由 3 个有值因子平均）。

### 4.2 惩罚与过滤

- **财务时效衰减：** 报告期距今 ≤3月: 100%，3-6月: 50%，6-9月: 25%，>9月: -1.0
- **缺失因子惩罚：** 缺失 > 20% 时线性压缩得分，最大惩罚 50%
- **价值陷阱：** value > 0 且 quality < -0.5 时压缩 value 得分
- **趋势过滤：** MOM_12M < -1.0 的股票最终得分打折

### 4.3 多空选股（核心）

```
因子得分排序: [+3.0 ... +0.5 ... 0 ... -0.5 ... -3.0]
              ├── Top N (LONG) ──┤      ├── Bottom M (SHORT) ──┤
```

**多头（Long）：** 得分 ≥ `US_MIN_SELECT_SCORE`(0.0) 的 Top-N 股票。
- 默认 `US_LONG_N=15`，Regime 联动缩减（熊市更少多头）。
- Softmax 权重分配，`tau=1.5`。

**空头（Short）：** 得分 ≤ `US_SHORT_SCORE_THRESHOLD`(-0.3) 的 Bottom-M 股票。
- 默认 `US_SHORT_N=10`，熊市增加空头（反向 Regime 联动）。
- 反向 Softmax（越差的得分权重越高），取负。

**净敞口：** `US_NET_EXPOSURE=0.6`
- 多头总权重 = (1 + 0.6) / 2 = **80%**
- 空头总权重 = (1 - 0.6) / 2 = **-20%**
- 总敞口上限：`US_GROSS_EXPOSURE_CAP=1.5`

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

**Credit Veto：** 当 credit < 0.2（利差深度倒挂）时，strength 封顶 0.5，防止熊市反弹陷阱（如 2022 年 8 月 S&P 反弹但利差仍倒挂）。

**因子拥挤惩罚：** 当 crowding < 0.3 且 strength > 0.6 时，strength 额外压缩 20%。

Regime 对 Alpha 策略的影响：
- 大类权重：熊市提高质量(1.5)、降低价值(0.6)/成长(0.8)
- 多头数量：熊市缩减（`BEAR_HOLDINGS_RATIO=0.6`）
- 空头数量：熊市增加（+30%）
- **净敞口动态调整：** 牛市 net=0.6（80%多/20%空），熊市 net→0.2（60%多/40%空）

Regime 对 Beta 策略的影响：
- **直接决定仓位：** strength [0, 1] → equity_pct [10%, 90%] 线性映射
- 剩余资金自动变现金（无做空）

### 4.5 纯月频调仓

- 固定频率：每 `US_REBALANCE_INTERVAL=20` 个交易日（约月频）
- **不使用偏离触发** — 经回测验证，偏离触发机制产生的全部是噪音交易，删除后年化收益提升 2%+，换手率下降 40%
- 年化换手率：~450%（月频合理范围）

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
| `us_financial_data` | 季度财报（SimFin + yfinance） |
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
python3 backend/cli.py data download --market us --target simfin   # 下载 SimFin 财报
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
| `SIMFIN_API_KEY` | — | SimFin API Key |
| `FRED_API_KEY` | — | FRED API Key |

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
| 因子数 | 30（含舆情） | **4**（美股专属：MOM_12_1+ShrYield+IVOL+GrossProfit）|
| 财报来源 | Tushare（ann_date 过滤） | **SEC EDGAR** + SimFin + yfinance（filing_date 过滤 + 45 天缓冲）|
| 宏观指标 | 中国 PMI/SHIBOR/PPI/M2 | 美国 ISM/FEDFUNDS/CPI/VIX |
| Regime | CSI 300 均线偏离（单维） | **四维复合**（趋势+VIX+利差+拥挤度）+ Credit Veto |
| 调仓 | 半月频 + 偏离触发 | **纯月频**（偏离触发已删除） |
| 基准 | CSI 300 | **Russell 1000** |
| 幸存者偏差 | 未修正 | **已修正**（含 227 只历史成分股） |
| FF5 回归 | 无 | **有**（Alpha 策略: alpha 6.69%，t=2.26） |
| ML 增强 | 无 | **LightGBM 因子合成**（可选） |
| 配置前缀 | 无前缀 | `US_` 前缀 |
