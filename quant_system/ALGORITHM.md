# A股多因子量化选股系统 — 算法文档

## 目录

1. [系统概览](#1-系统概览)
2. [股票池构建](#2-股票池构建)
3. [因子体系](#3-因子体系)
4. [因子处理流水线](#4-因子处理流水线)
5. [综合评分与选股](#5-综合评分与选股)
6. [行业因子权重配置](#6-行业因子权重配置)
7. [风控模块](#7-风控模块)
8. [回测引擎](#8-回测引擎)
9. [可配置参数汇总](#9-可配置参数汇总)

---

## 1. 系统概览

月频多因子打分选股策略，核心流程：

```
每月末交易日(T日)
  → 构建可交易股票池（含核心财务准入过滤）
  → 计算 19 个因子
  → 因子处理（去极值 → 行业市值中性化 → Z-Score → Clip ±3）
  → 大类合成评分（类内加权平均 → 类间固定分母合成）
  → 选取得分最高的 N 只，Score 比例分配权重
  → T+1 日开盘价执行交易
```

---

## 2. 股票池构建

> 文件: `data/cleaner.py`

按顺序过滤，通过所有条件的股票进入当期选股池：

| 序号 | 过滤规则 | 参数/阈值 |
|------|---------|----------|
| 1 | 剔除已退市 | `delist_date IS NULL` 或 > 当日 |
| 2 | 剔除 ST/\*ST | `is_st=0`，名称不含 ST/\*ST/SST |
| 3 | 剔除科创板 | 代码前缀 `68`（`EXCLUDE_STAR_MARKET=1` 时） |
| 4 | 剔除次新股 | 上市不足 `IPO_FILTER_DAYS`（默认 180 天） |
| 5 | 剔除停牌 | 当日 `volume > 0` |
| 6 | 流动性过滤 | 近 20 个交易日日均成交额 ≥ `MIN_DAILY_TURNOVER`（默认 5000 万元） |
| 7 | **核心财务准入** | EP/BP/ROE_TTM/GROSS_MARGIN 至少一项非空，否则剔除 |

涨停/跌停标记：主板 ±10%（阈值 9.9%），创业板/科创板 ±20%（阈值 19.9%）。涨停股不可买入但保留在池中。

---

## 3. 因子体系

共 19 个因子，分 5 大类。

### 3.1 价值因子（value）

| 因子 | 名称 | 公式 | 方向 |
|------|------|------|------|
| **EP** | 市盈率倒数 | TTM净利润 / (收盘价 × 总股本 × 10000) | 越高越好 |
| **BP** | 市净率倒数 | 每股净资产(BPS) / 收盘价 | 越高越好 |

- EP 使用滚动 4 季度 TTM 净利润，净利润 ≤ 0 返回 NaN
- BP 使用最新报告期 BPS，BPS ≤ 0 返回 NaN
- 数据防未来函数：仅使用 `ann_date ≤ 选股日` 的报告

### 3.2 质量因子（quality）

| 因子 | 名称 | 公式 | 方向 |
|------|------|------|------|
| **ROE_TTM** | 净资产收益率 | 直接读取 `financial_data.roe_ttm` | 越高越好 |
| **GROSS_MARGIN** | 毛利率 | 直接读取 `financial_data.gross_margin` | 越高越好 |
| **PROFIT_STB** | 盈利稳定性 | std(同比增长率) / \|mean(同比增长率)\| | **越低越好（反向）** |
| **MARGIN_TREND** | 毛利率趋势 | 当期毛利率 - 上期毛利率 | 越高越好 |

- PROFIT_STB 使用最近 4+ 个报告期的净利润同比增速的变异系数（CV），需 ≥ 3 组有效同比数据

### 3.3 成长因子（growth）

| 因子 | 名称 | 公式 | 方向 |
|------|------|------|------|
| **NET_PROFIT_YOY** | 净利润同比 | TTM净利润(当期) / TTM净利润(去年同期) - 1 | 越高越好 |
| **REVENUE_YOY** | 营收同比 | TTM营收(当期) / TTM营收(去年同期) - 1 | 越高越好 |

- 分母 ≤ 0 → NaN（避免负利润增速误导）
- revenue/net_profit 数据来自 `fina_indicator` + `income` 接口合并

### 3.4 动量因子（momentum）

| 因子 | 名称 | 公式 | 回溯期 | 方向 |
|------|------|------|--------|------|
| **MOM_1M** | 1 月动量 | Close(T) / Close(T-1M) - 1 | 1 个月 | 越高越好 |
| **MOM_3M** | 3 月动量 | Close(T) / Close(T-3M) - 1 | 3 个月 | 越高越好 |
| **MOM_12M** | 12-1 月动量 | Close(T-1M) / Close(T-12M) - 1 | 12 个月（跳过最近 1 月） | 越高越好 |
| **REV_5D** | 5 日短期反转 | -1 × 累计 5 日收益率 | 5 个交易日 | 越高越好（超跌反弹） |
| **IND_MOM** | 行业动量 | 行业内所有股票 20 日累计收益均值 | 20 交易日 | 越高越好 |
| **RESIDUAL_MOM** | 残差动量 | 个股 20 日累计收益 - 行业平均累计收益 | 20 交易日 | 越高越好 |

- MOM_12M 跳过最近 1 个月，避免短期反转污染
- RESIDUAL_MOM 剥离了行业 beta，捕捉个股 alpha

### 3.5 技术因子（technical）

| 因子 | 名称 | 公式 | 回溯期 | 方向 |
|------|------|------|--------|------|
| **TURN_20D** | 20 日平均换手率 | mean(turnover_rate, 20D) | 20 交易日 | **反向** |
| **VOL_20D** | 20 日波动率 | std(日收益率, 20D) | 20 交易日 | **反向** |
| **PRICE_DEV_60D** | 60 日均线偏离 | (Close - MA60) / MA60 | 60 交易日 | **反向** |
| **SIZE** | 市值 | ln(收盘价 × 流通股本 × 10000) | 当日 | 越高越好（偏大盘） |
| **VOL_PRICE_DIV** | 量价背离 | corr(pct_chg, volume_chg, 20D) | 20 交易日 | **反向** |

反向因子 = 值越低越好，权重为负数。

---

## 4. 因子处理流水线

> 文件: `factors/processor.py`

所有因子按统一流程做截面处理，顺序固定：

### 4.1 去极值（MAD 法）

```
median = 因子中位数
MAD = median(|x - median|)
上界 = median + 5 × 1.4826 × MAD
下界 = median - 5 × 1.4826 × MAD
超出边界的值截断到边界
```

- 系数 `1.4826` 为正态分布下 MAD → 标准差的换算常数
- `n=5.0`（较宽松，保留更多信息）

### 4.2 行业市值中性化

截面回归取残差，支持 3 种模式（`NEUTRALIZE_MODE` 配置）：

| 模式 | 回归矩阵 X | 说明 |
|------|-----------|------|
| `full` | 行业哑变量 + ln(市值) | 完整中性化（默认） |
| `size_only` | 仅 ln(市值) | 保留行业 Alpha |
| `none` | 跳过 | 不中性化 |

- 可选 `NONLINEAR_SIZE=1` 追加 ln(市值)² 非线性项
- OLS 使用 `numpy.linalg.pinv`（伪逆，数值稳定）
- 样本数 < 10 时跳过中性化

### 4.3 Z-Score 标准化

```
z = (x - mean) / std
```

输出：均值 0、标准差 1，使不同因子可比。

### 4.4 Z-Score Clip ±3

```
z = clip(z, -3.0, +3.0)
```

防止中性化后残差极端值（如行业层面因子经行业中性化后 std 极小导致 Z-score 爆炸）主导综合得分。

---

## 5. 综合评分与选股

> 文件: `strategy/multi_factor.py`

### 5.1 大类合成评分（固定分母）

19 个因子分为 5 个大类，评分分两层：

**第一层：类内加权平均（动态分母）**

同类因子衡量同一维度，缺失因子可互替：
```
cat_score = Σ(factor_zscore × factor_weight) / Σ|factor_weight|  （仅非 NaN 因子参与）
```

**第二层：类间固定分母合成**

大类间不可互替，缺失大类贡献 0，分母不缩小：
```
score = Σ(cat_score × cat_weight) / Σ|所有 cat_weight|
```

固定分母 = 1.0 + 1.0 + 1.0 + 1.0 + 0.5 = **4.5**

### 5.2 大类权重

| 大类 | 包含因子 | 大类权重 | 占比 |
|------|---------|---------|------|
| **value** | EP, BP | 1.0 | 22.2% |
| **quality** | ROE_TTM, GROSS_MARGIN, PROFIT_STB, MARGIN_TREND | 1.0 | 22.2% |
| **growth** | NET_PROFIT_YOY, REVENUE_YOY | 1.0 | 22.2% |
| **momentum** | MOM_1M, MOM_3M, MOM_12M, REV_5D, IND_MOM, RESIDUAL_MOM | 1.0 | 22.2% |
| **technical** | TURN_20D, VOL_20D, PRICE_DEV_60D, SIZE, VOL_PRICE_DIV | 0.5 | 11.1% |

设计目的：动量 6 个因子的总贡献被限制在 1 个大类权重内，与价值（2 因子）、质量（4 因子）等权。

### 5.3 因子级权重（类内）

| 因子 | 权重 | 说明 |
|------|------|------|
| EP, BP | 1.0 | 价值基准 |
| MOM_1M, MOM_3M, MOM_12M | 1.0 | 动量基准 |
| ROE_TTM, GROSS_MARGIN | 1.0 | 质量基准 |
| TURN_20D | **-1.0** | 反向，回避高换手 |
| VOL_20D | **-0.5** | 反向，防守 |
| PRICE_DEV_60D | **-0.3** | 反向，安全边际 |
| REV_5D | 0.4 | 短期反转信号 |
| PROFIT_STB | **-0.5** | 反向，偏好稳定 |
| MARGIN_TREND | 0.4 | 毛利趋势改善 |
| SIZE | 0.3 | 偏中大盘 |
| IND_MOM | 0.5 | 行业轮动 |
| NET_PROFIT_YOY | 0.8 | 成长性 |
| REVENUE_YOY | 0.6 | 营收增长 |
| RESIDUAL_MOM | 0.7 | 个股 alpha 动量 |
| VOL_PRICE_DIV | **-0.4** | 反向，量价背离信号 |

权重回退链：`DB行业配置 → __DEFAULT__ 配置 → 代码硬编码权重`

### 5.4 选股规则

1. **核心财务准入过滤**：EP/BP/ROE_TTM/GROSS_MARGIN 全部缺失的股票剔除
2. 按综合得分降序排列
3. 过滤 `score < MIN_SELECT_SCORE`（默认 0）
4. 取前 `MAX_HOLDINGS` 只（默认 10）
5. 排除涨停股（不可买入）
6. 允许空仓（无股票达标时持现金）

### 5.5 仓位分配 — Score 比例权重

选中股票按得分比例分配权重（替代旧的分档加权）：

```python
shifted = max(scores, 0)                    # 非负化
raw_w = shifted / sum(shifted)              # 得分比例
raw_w = max(raw_w, 1/(n_holdings*3))        # 最低权重下限
weight = raw_w / sum(raw_w)                 # 归一化
```

优势：消除分档阶梯效应，排名微变不会导致权重跳变。

### 5.6 换手惩罚（可选）

```
score += λ × is_in_portfolio
```

- `TURNOVER_PENALTY_LAMBDA` 默认 0.0（关闭）
- 已持仓股票加分，降低不必要的换手

---

## 6. 行业因子权重配置

> 文件: `data/database.py` (IndustryFactorConfig), `data/seed_config.py`

### 6.1 配置表结构

| 字段 | 类型 | 说明 |
|------|------|------|
| industry_name | String(50) | 行业名称，`__DEFAULT__` 为默认 |
| factor_name | String(30) | 因子名称 |
| weight | Float | 带符号权重（反向因子存负数） |

唯一键：`(industry_name, factor_name)`

### 6.2 向后兼容

- 表为空时行为与旧版代码完全一致（回退到硬编码权重）
- 表不存在时静默忽略（`_load_industry_weights` 捕获异常返回空字典）

---

## 7. 风控模块

> 文件: `risk/risk_manager.py`

### 7.1 权重调整流程

```
原始选股结果 → 流动性过滤 → 个股上限 → 行业上限 → 归一化 → 回撤缩仓/波动率目标
```

### 7.2 限制规则

| 规则 | 参数 | 说明 |
|------|------|------|
| 个股上限 | `MAX_SINGLE_WEIGHT = 5%` | 超限部分按比例分配给其他持仓，迭代至收敛（最多 10 轮） |
| 行业上限 | `MAX_INDUSTRY_WEIGHT = 30%` | 超限行业内所有股票等比例缩减 |
| 回撤缩仓 | `MAX_DRAWDOWN_THRESHOLD = 15%` | 当前回撤超 15% 时全仓位缩至 50%（`USE_VOL_TARGETING=0` 时） |
| 波动率目标 | `USE_VOL_TARGETING=1` | `scale = target_vol / realized_vol`，clipped [0.3, 1.0] |
| 流动性 | `MIN_DAILY_TURNOVER = 5000 万` | 近 20 日日均成交额不足则剔除 |

### 7.3 波动率目标管理（替代回撤缩仓）

```
realized_vol = std(日收益率, 最近 20 天) × √252
scale = target_vol / realized_vol
scale = clip(scale, VOL_SCALE_MIN, VOL_SCALE_MAX)
```

- `USE_VOL_TARGETING=0`（默认）→ 回撤缩仓逻辑
- `USE_VOL_TARGETING=1` → 波动率目标管理

---

## 8. 回测引擎

> 文件: `strategy/backtest.py`

### 8.1 执行模型

- **信号产生**: T 日收盘后
- **交易执行**: T+1 日开盘价
- **调仓频率**: 月频（每月最后一个交易日）
- **回溯**: 自动回溯 start_date 前 2 个月找最近调仓日，确保回测首日有持仓

### 8.2 交易成本

| 项目 | 买入 | 卖出 |
|------|------|------|
| 佣金 | max(5 元, 成交额 × 0.075%) | max(5 元, 成交额 × 0.075%) |
| 印花税 | — | 成交额 × 0.1% |
| 滑点 | 开盘价 × (1 + 0.1%) | 开盘价 × (1 - 0.1%) |

### 8.3 下单规则

- 最小交易单位: 100 股（1 手），向下取整
- 先卖后买（卖出释放现金后再买入）
- 涨停不可买入，跌停不可卖出
- **一字板处理**: `open == high == low == close` 时视为一字涨停/跌停
- **跌停卖单排队**: 跌停无法卖出时加入 `pending_sells`，下一个交易日自动重试

### 8.4 绩效指标

| 指标 | 公式 |
|------|------|
| 总收益 | NAV_end / NAV_start - 1 |
| 年化收益 | (1 + 总收益) ^ (1/年数) - 1 |
| 年化波动率 | std(日收益率) × √252 |
| Sharpe | (年化收益 - 2%) / 年化波动率 |
| 最大回撤 | min(NAV - cummax(NAV)) / cummax(NAV) |
| Calmar | 年化收益 / \|最大回撤\| |
| 日胜率 | 正收益天数 / 总交易天数 |

基准: 沪深 300 指数（000300.SH）

---

## 9. 可配置参数汇总

所有参数支持环境变量覆盖（`.env` 文件）。

### 数据

| 参数 | 默认值 | 环境变量 |
|------|--------|---------|
| DATA_START_DATE | 20150101 | DATA_START_DATE |
| IPO_FILTER_DAYS | 180 天 | — |
| EXCLUDE_STAR_MARKET | 1 | EXCLUDE_STAR_MARKET |

### 策略

| 参数 | 默认值 | 环境变量 |
|------|--------|---------|
| MAX_HOLDINGS | 10 | MAX_HOLDINGS |
| MIN_SELECT_SCORE | 0.0 | MIN_SELECT_SCORE |
| REBALANCE_FREQ | 月频 | — |
| TURNOVER_PENALTY_LAMBDA | 0.0（关闭） | TURNOVER_PENALTY_LAMBDA |
| NEUTRALIZE_MODE | full | NEUTRALIZE_MODE |
| NONLINEAR_SIZE | 0（关闭） | NONLINEAR_SIZE |

### 风控

| 参数 | 默认值 | 环境变量 |
|------|--------|---------|
| MAX_SINGLE_WEIGHT | 0.05 | — |
| MAX_INDUSTRY_WEIGHT | 0.30 | — |
| MAX_DRAWDOWN_THRESHOLD | 0.15 | — |
| DRAWDOWN_REDUCE_POSITION | 0.50 | — |
| MIN_DAILY_TURNOVER | 5000 万 | — |
| USE_VOL_TARGETING | 0（关闭） | USE_VOL_TARGETING |
| TARGET_VOL | 0.20 | TARGET_VOL |
| VOL_LOOKBACK_DAYS | 20 | VOL_LOOKBACK_DAYS |
| VOL_SCALE_MIN / MAX | 0.3 / 1.0 | VOL_SCALE_MIN / VOL_SCALE_MAX |

### 交易成本

| 参数 | 默认值 |
|------|--------|
| BUY_COMMISSION | 0.00075 (万7.5) |
| SELL_COMMISSION | 0.00075 (万7.5) |
| STAMP_TAX | 0.001 (千1) |
| SLIPPAGE | 0.001 (千1) |

### 模拟盘

| 参数 | 默认值 | 环境变量 |
|------|--------|---------|
| PAPER_INITIAL_CAPITAL | 1,000,000 元 | PAPER_INITIAL_CAPITAL |
| PAPER_ACCOUNT_NAME | default | PAPER_ACCOUNT_NAME |
| TRADER_TYPE | paper | TRADER_TYPE |

### 行业配置

| 参数 | 默认值 | 环境变量 |
|------|--------|---------|
| ALLOWED_INDUSTRIES | []（全市场） | ALLOWED_INDUSTRIES |
| INDUSTRY_INDEX_MAP | {} | INDUSTRY_INDEX_MAP |
