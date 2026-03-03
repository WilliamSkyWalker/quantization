# A股多因子量化选股系统 — 算法文档

## 目录

1. [系统概览](#1-系统概览)
2. [股票池构建](#2-股票池构建)
3. [因子体系](#3-因子体系)
4. [因子处理流水线](#4-因子处理流水线)
5. [综合评分与选股](#5-综合评分与选股)
6. [行业因子权重配置](#6-行业因子权重配置)
7. [风控模块](#7-风控模块)
8. [Regime 切换机制](#8-regime-切换机制)
9. [回测与模拟盘引擎](#9-回测与模拟盘引擎)
10. [可配置参数汇总](#10-可配置参数汇总)
11. [舆情采集管道](#11-舆情采集管道)

---

## 1. 系统概览

月频多因子打分选股策略，核心流程：

```
每月末交易日(T日)
  → 构建可交易股票池（含核心财务准入过滤）
  → 计算 29 个因子（动量/偏离度使用前复权价格）
  → 因子处理（去极值 → 按大类中性化 → 二次去极值 → 标准化(Z-Score/Rank) → Clip ±3）
  → Regime 检测（CSI300 vs MA120 → 牛/熊大类权重切换）
  → 大类合成评分（类内加权平均 → 类间动态分母合成 → 最小有效大类数保护）
  → 选取得分最高的 N 只，Softmax 分配权重
  → T+1 日开盘价执行交易（含除权除息调整、跌停排队）
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

共 29 个因子，分 7 大类。

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
| **NET_PROFIT_CAGR_3Y** | 3年复合增长率 | (TTM净利润(当期) / TTM净利润(3年前))^(1/3) - 1 | 越高越好 |

- 分母 ≤ 0 → NaN（避免负利润增速误导）
- CAGR 要求当期和 3 年前 TTM 净利润均 > 0，IPO < 3 年的股票自动 NaN
- revenue/net_profit 数据来自 `fina_indicator` + `income` 接口合并

### 3.4 动量因子（momentum）

| 因子 | 名称 | 公式 | 回溯期 | 方向 |
|------|------|------|--------|------|
| **MOM_1M** | 1 月动量 | AdjClose(T) / AdjClose(T-1M) - 1 | 1 个月 | 越高越好 |
| **MOM_3M** | 3 月动量 | AdjClose(T) / AdjClose(T-3M) - 1 | 3 个月 | 越高越好 |
| **MOM_12M** | 12-1 月动量 | AdjClose(T-1M) / AdjClose(T-12M) - 1 | 12 个月（跳过最近 1 月） | 越高越好 |
| **REV_5D** | 5 日短期反转 | -1 × 累计 5 日收益率 | 5 个交易日 | 越高越好（超跌反弹） |
| **IND_MOM** | 行业动量 | 行业内所有股票 20 日累计收益均值 | 20 交易日 | 越高越好 |
| **RESIDUAL_MOM** | 残差动量 | 个股 20 日累计收益 - 行业平均累计收益 | 20 交易日 | 越高越好 |
| **CMDTY_MOM** | 商品轮动 | 对应商品期货 N 日收益率（OI 加权） | 20 交易日 | 越高越好 |

- **前复权价格**: MOM_1M/3M/12M 使用 `adj_close = close × adj_factor` 计算跨期收益率，避免除权除息产生虚假信号。`adj_factor` 为 NULL 时 fillna(1.0) 保持向后兼容。
- MOM_12M 跳过最近 1 个月，避免短期反转污染
- RESIDUAL_MOM 剥离了行业 beta，捕捉个股 alpha
- CMDTY_MOM 通过两层映射（L2 优先 → L1 回退）将商品价格动量传导到对应行业股票。无映射行业（如银行、计算机）返回 NaN，由动态分母机制正确处理。同行业多商品按 OI（持仓量）加权平均。数据来源：Tushare `fut_mapping` + `fut_daily`。

### 3.5 技术因子（technical）

| 因子 | 名称 | 公式 | 回溯期 | 方向 |
|------|------|------|--------|------|
| **TURN_20D** | 20 日平均换手率 | mean(turnover_rate, 20D) | 20 交易日 | **反向** |
| **VOL_20D** | 20 日波动率 | std(日收益率, 20D) | 20 交易日 | **反向** |
| **PRICE_DEV_60D** | 60 日均线偏离 | (AdjClose - MA60_adj) / MA60_adj | 60 交易日 | **反向** |
| **SIZE** | 市值 | ln(收盘价 × 流通股本 × 10000) | 当日 | 越高越好（偏大盘） |
| **VOL_PRICE_DIV** | 量价背离 | 趋势背离检测（见下） | 20 交易日 | 越高越好 |

反向因子 = 值越低越好，权重为负数。TURN_20D、VOL_20D、PRICE_DEV_60D、PROFIT_STB 为反向因子。

**PRICE_DEV_60D** 使用前复权价格 `adj_close = close × adj_factor` 计算 MA60 和偏离度，避免除权除息导致均线失真。

**VOL_PRICE_DIV 趋势背离公式**（正向因子，高值 = 背离 = 反转信号强）：
1. 20D 累计收益 → 价格趋势方向
2. 20D 成交量 OLS 斜率（标准化） → 量能趋势方向
3. 当价格方向与量能方向不一致时，divergence = |price_trend|；否则 = 0
4. 量增价跌 / 量缩价升 → 高值 → 反转信号

### 3.6 宏观因子（macro）

利用宏观经济指标的 trailing Z-score（24 月窗口），通过行业敏感度系数映射到个股。

| 因子 | 名称 | 信号公式 | 方向 |
|------|------|---------|------|
| **MACRO_CYCLE** | 经济周期 | 0.5×z(PMI-50) + 0.3×z(PPI_YOY) + 0.2×z(PMI_NEW_ORDER-50) | 越高越好 |
| **MACRO_LIQD** | 流动性 | 0.3×z(M1_M2_SPREAD) + 0.3×z(M2_YOY) + 0.2×(-z(Δ3M SHIBOR)) + 0.2×(-z(Δ3M LPR)) | 越高越好 |
| **MACRO_INFL** | 通胀结构 | 0.5×z(CPI-PPI) + 0.3×z(CPI) + 0.2×(-z(PPI)) | 越高越好 |
| **MACRO_EXTR** | 外部风险 | 0.6×(-z(UST_10Y)) + 0.4×z(UST_2Y10Y) | 越高越好 |

- **数据源**: 8 个 Tushare 宏观 API（shibor, shibor_lpr, cn_cpi, cn_ppi, cn_pmi, cn_m, cn_gdp, us_tycr）
- **防未来数据泄露**: 各指标按 `MACRO_PUBLICATION_LAG` 延迟取值（CPI/PPI/M2=16天, PMI=1天, GDP=20天, SHIBOR/LPR/UST=0天）
- **PMI 退化**: cn_pmi 需 2000 积分，不可用时退化为 PPI only 版本: 0.6×z(PPI_YOY) + 0.4×z(PPI_MP_YOY)
- **行业映射**: 每个因子有独立的行业敏感度字典（正=受益行业，负=防御行业），未映射行业 → NaN
- **数据库**: macro_indicator 表（通用 KV 结构，indicator_code + report_date 唯一键）

### 3.7 舆情因子（sentiment）

将政策文章分析结果（关键词 + LLM 两层）转化为行业级信号，再映射到个股。

| 因子 | 名称 | 信号公式 | 方向 |
|------|------|---------|------|
| **POLICY_SENT** | 政策情感 | 行业加权情感分 × 强度 | 越高越好 |
| **POLICY_INTENSITY** | 政策关注度 | 行业强度得分（不论正负） | 越高越好 |
| **ANALYST_RATING** | 分析师共识评级 | 近 90 天研报平均 rating_score (1~5) | 越高越好 |
| **ANALYST_COVERAGE** | 分析师覆盖度 | log(1 + 覆盖机构数) | 越高越好 |

- **券商研报因子**: 通过 AKShare `stock_research_report_em()` 获取东方财富券商研报，评级映射（买入=5, 增持=4, 中性=3, 减持=2, 卖出=1），直接按 ts_code 匹配（无需行业映射），无研报覆盖 → NaN
- **两层分析**: 关键词规则为底层（零成本），LLM 为增强层（仅对 keyword intensity ≥ 0.5 的文章调用）
- **行业映射**: 关键词词典覆盖 28 个申万一级行业 → 通过 industry_class 表传导到个股
- **时间衰减**: `weight = exp(-0.3 × days_ago)`，约 3 天半衰期
- **合并逻辑**: 同一文章 LLM 结果优先，否则用 keyword 结果
- **强度计算**: tier 权重 × min(命中数/3, 1.0)；标题命中 × 2.0，摘要 × 1.0
- **降级策略**: 无 LLM API key 时仅用 keyword 分析，不报错
- **政策影响类型** (`impact_type`): 每条分析记录标注影响类型，支持后续按类型差异化处理
  - `trade_tariff`: 贸易关税（进出口关税、贸易壁垒、贸易协定）
  - `tech_sanction`: 技术制裁（芯片禁令、实体清单、出口管制）
  - `monetary_policy`: 货币政策（利率、准备金率、汇率）
  - `fiscal_stimulus`: 财政刺激（减税降费、专项债、补贴）
  - `industry_regulation`: 行业监管（准入、反垄断、环保标准）
  - `general_policy`: 一般政策（不属于以上 5 类）
  - keyword 层基于规则分类，LLM 层由模型判断并校验
- **数据库**: policy_analysis 表（article_id + analysis_type 唯一键，upsert 语义）

---

## 4. 因子处理流水线

> 文件: `factors/processor.py`

所有因子按统一流程做截面处理，顺序固定：

```
去极值(MAD) → 中性化(OLS) → 二次去极值(MAD) → Z-Score → Clip ±3
```

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

**按大类覆盖中性化模式**（`CATEGORY_NEUTRALIZE_OVERRIDES`）：

宏观因子本质是行业 beta（通过 industry_sensitivity_map 映射），行业中性化会削弱行业轮动效果。舆情因子同理。因此默认按大类覆盖：

| 大类 | 默认中性化模式 | 原因 |
|------|--------------|------|
| macro | `size_only` | 保留行业 beta 信号 |
| sentiment | `size_only` | 保留行业级情感映射 |
| 其他 | 继承全局 `NEUTRALIZE_MODE` | — |

可通过环境变量 `CATEGORY_NEUTRALIZE_OVERRIDES` 覆盖（JSON 格式）。

### 4.3 二次去极值（中性化后）

仅在实际执行了中性化时（`neutralize_mode != "none"`）才做二次去极值，使用与 4.1 相同的 MAD 法（n=5.0）。

OLS 中性化残差可能出现极端值（行业样本少时尤为明显），二次去极值抑制这些残差极端值，防止后续 Z-score 标准化被污染。

### 4.4 标准化（Z-Score / Rank Percentile）

支持两种模式（`STANDARDIZE_MODE` 配置）：

**Z-Score（默认）**：
```
z = (x - mean) / std
```
输出：均值 0、标准差 1，使不同因子可比。

**Rank Percentile**（`STANDARDIZE_MODE=rank`）：
```
ranks = rank(x, method="average")
uniform = (ranks - 0.5) / n          # (0, 1) 均匀分布
result = (uniform - 0.5) × 6.0       # 映射到 [-3, +3]
```
对 A 股高度偏态的因子分布更稳健。

### 4.5 Z-Score Clip ±3

```
z = clip(z, -3.0, +3.0)
```

最终保护：防止经二次去极值后仍有的极端 Z-score 主导综合得分。

---

## 5. 综合评分与选股

> 文件: `strategy/multi_factor.py`

### 5.1 大类合成评分（固定分母）

28 个因子分为 7 个大类，评分分两层：

**第一层：类内加权平均（动态分母）**

同类因子衡量同一维度，缺失因子可互替：
```
cat_score = Σ(factor_zscore × factor_weight) / Σ|factor_weight|  （仅非 NaN 因子参与）
```

**第二层：类间动态分母合成（缺失大类权重再分配）**

缺失大类的权重自动按比例分配给有值大类：
```
score = Σ(cat_score × cat_weight) / Σ|有值大类的 cat_weight|
```

分母 = 有值大类的权重绝对值之和（而非固定 6.0）。当缺失 1 个大类（如 macro, 权重 0.6）时，分母从 6.0 变为 5.4，得分提升约 13%。

**最小有效大类数保护**：当某只股票的有效大类数（至少有 1 个非 NaN 因子的大类数）< `MIN_VALID_CATEGORIES`（默认 4）时，综合得分设为 NaN，该股票被自动剔除。防止 API 故障导致大面积因子缺失时产生不可靠信号，同时限制了缺失大类导致的最大得分膨胀。

### 5.2 大类权重

| 大类 | 包含因子 | 大类权重 | 占比 |
|------|---------|---------|------|
| **value** | EP, BP | 1.0 | 16.7% |
| **quality** | ROE_TTM, GROSS_MARGIN, PROFIT_STB, MARGIN_TREND | 1.0 | 16.7% |
| **growth** | NET_PROFIT_YOY, REVENUE_YOY, NET_PROFIT_CAGR_3Y | 1.2 | 20.0% |
| **momentum** | MOM_1M, MOM_3M, MOM_12M, REV_5D, IND_MOM, RESIDUAL_MOM, CMDTY_MOM | 1.3 | 21.7% |
| **technical** | TURN_20D, VOL_20D, PRICE_DEV_60D, SIZE, VOL_PRICE_DIV | 0.5 | 8.3% |
| **macro** | MACRO_CYCLE, MACRO_LIQD, MACRO_INFL, MACRO_EXTR | 0.6 | 10.0% |
| **sentiment** | POLICY_SENT, POLICY_INTENSITY, ANALYST_RATING, ANALYST_COVERAGE | 0.4 | 6.7% |

设计目的：成长和动量增强权重（1.2 / 1.3），更重视成长性和趋势信号；技术因子半权（0.5）降低噪声干扰；宏观因子权重 0.6（市场级信号，补充个股因子盲区）；舆情因子权重 0.4（初始偏低，待验证后调整）。券商研报因子归入 sentiment 大类，不新增大类，避免改变固定分母（6.0）。

### 5.3 因子级权重（类内）

| 因子 | 权重 | 说明 |
|------|------|------|
| EP, BP | 1.0 | 价值基准 |
| MOM_1M, MOM_3M, MOM_12M | 1.0 | 动量基准 |
| ROE_TTM, GROSS_MARGIN | 1.0 | 质量基准 |
| TURN_20D | **-1.0** | 反向，回避高换手 |
| VOL_20D | **-0.3** | 反向，防守（降低惩罚力度） |
| PRICE_DEV_60D | **-0.15** | 反向，安全边际（降低惩罚力度） |
| REV_5D | 0.4 | 短期反转信号 |
| PROFIT_STB | **-0.5** | 反向，偏好稳定 |
| MARGIN_TREND | 0.4 | 毛利趋势改善 |
| SIZE | 0.3 | 偏中大盘 |
| IND_MOM | 0.8 | 行业轮动（提高权重） |
| NET_PROFIT_YOY | 1.0 | 成长性（提高权重） |
| REVENUE_YOY | 0.8 | 营收增长（提高权重） |
| NET_PROFIT_CAGR_3Y | 0.8 | 3年复合增长率 |
| RESIDUAL_MOM | 0.7 | 个股 alpha 动量 |
| VOL_PRICE_DIV | 0.4 | 量价背离（正向，高值=背离强） |
| CMDTY_MOM | 0.6 | 商品轮动（信号间接） |
| MACRO_CYCLE | 0.8 | 经济周期（宏观核心信号） |
| MACRO_LIQD | 0.7 | 流动性 |
| MACRO_INFL | 0.5 | 通胀结构 |
| MACRO_EXTR | 0.4 | 外部风险（辅助信号） |
| POLICY_SENT | 0.6 | 政策情感（舆情核心信号） |
| POLICY_INTENSITY | 0.4 | 政策关注度（辅助信号） |
| ANALYST_RATING | 0.6 | 分析师共识评级 |
| ANALYST_COVERAGE | 0.3 | 分析师覆盖度（辅助信号） |

权重回退链：`DB行业配置 → __DEFAULT__ 配置 → 代码硬编码权重`

### 5.4 选股规则

依次执行（顺序与代码一致）：

1. **核心财务准入过滤**：EP/BP/ROE_TTM/GROSS_MARGIN 全部缺失的股票剔除
2. 剔除综合得分为 NaN 的股票（因子全缺失）
3. **排除涨停股**（不可买入）
4. 换手惩罚加分（若 `TURNOVER_PENALTY_LAMBDA > 0`，已持仓股 +λ）
5. 按综合得分降序排列
6. 过滤 `score < MIN_SELECT_SCORE`（默认 0）
7. 取前 `MAX_HOLDINGS` 只（默认 10）
8. 允许空仓（无股票达标时持现金）

### 5.5 仓位分配 — Softmax 权重

选中股票按 Softmax 分配权重，温度参数 τ 控制集中度（`WEIGHT_TEMPERATURE`，默认 2.0）：

```python
shifted = scores - max(scores)              # 数值稳定
exp_scores = exp(shifted / τ)               # Softmax
raw_w = exp_scores / sum(exp_scores)
raw_w = max(raw_w, 1/(n_holdings*3))        # 最低权重下限
weight = raw_w / sum(raw_w)                 # 归一化
```

- τ=2.0 时相邻 0.5 分差约 1.28x 权重差，分化温和
- τ=0 退化为等权
- 优势：比线性比例权重更平滑，头部集中可控

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
| 个股上限 | `MAX_SINGLE_WEIGHT = 12%` | 超限部分按比例分配给其他持仓，迭代至收敛（最多 10 轮） |
| 行业上限 | `MAX_INDUSTRY_WEIGHT = 30%` | 超限行业内所有股票等比例缩减 |
| 线性回撤响应 | `DD_START=10%, DD_MAX=25%` | 10%开始线性降仓，25%降至50%（`USE_VOL_TARGETING=0` 时） |
| 波动率目标 | `USE_VOL_TARGETING=1` | `scale = target_vol / realized_vol`，clipped [0.3, 1.0] |
| 流动性 | `MIN_DAILY_TURNOVER = 5000 万` | 近 20 日日均成交额不足则剔除 |

### 7.3 线性回撤响应

回撤在 `[DD_START, DD_MAX]` 区间内线性降仓（替代旧的二元触发）：

```
dd ≤ DD_START (10%) → 仓位 = 1.0（满仓）
dd ≥ DD_MAX  (25%) → 仓位 = DD_MIN_POSITION (50%)
中间                → 线性插值: 1.0 - (dd - start)/(max - start) × (1.0 - min_position)
```

优势：比二元触发（25% 才降仓到 70%）更及时更平滑，10% 就开始缓慢降仓。

### 7.4 波动率目标管理（替代回撤缩仓）

```
realized_vol = std(日收益率, 最近 60 天) × √252
scale = target_vol / realized_vol
scale = clip(scale, VOL_SCALE_MIN, VOL_SCALE_MAX)
```

- `USE_VOL_TARGETING=0`（默认）→ 线性回撤响应
- `USE_VOL_TARGETING=1` → 波动率目标管理

---

## 8. Regime 切换机制

> 文件: `strategy/regime.py`

基于 CSI 300 指数是否在 120 日均线上方判断市场状态（牛/熊），动态调整大类权重。

### 8.1 检测逻辑

```
close(CSI300) ≥ MA120(CSI300) → bull
close(CSI300) < MA120(CSI300) → bear
数据不足 → 回退到 bull
```

### 8.2 熊市大类权重覆盖

| 大类 | 牛市权重 | 熊市权重 | 说明 |
|------|---------|---------|------|
| momentum | 1.3 | 0.6 | 降低动量暴露（熊市动量反转） |
| quality | 1.0 | 1.5 | 提高质量防御 |
| 其他 | 不变 | 不变 | — |

- 可通过 `REGIME_BEAR_OVERRIDES` 环境变量自定义（JSON 格式）
- `REGIME_ENABLED=0` 完全关闭 regime 切换
- 120 日均线为慢速指标，不会频繁切换

---

## 9. 回测与模拟盘引擎

### 9.1 三条执行路径

选股、回测、模拟盘共享同一套选股逻辑，区别仅在执行层：

| 路径 | API | 选股 | 风控 | 执行器 |
|------|-----|------|------|--------|
| **选股展示** | `POST /api/strategy/select` | `score_all_stocks()` | — | 仅展示，不交易 |
| **回测** | `POST /api/strategy/backtest` | `generate_signals()` | `adjust_weights()` | `BacktestEngine` |
| **模拟盘日常** | `POST /api/paper/trade` | `select_stocks()` | `adjust_weights()` | `PaperTrader.sync_position()` |
| **模拟盘回放** | `POST /api/paper/replay` | `generate_signals()` | `adjust_weights()` | `PaperTrader.replay()` |

**选股逻辑一致性**：回测、模拟盘日常、模拟盘回放三者均调用 `MultiFactorStrategy.select_stocks()` 产生信号（`generate_signals()` 内部逐月调用 `select_stocks()`），因子计算、评分、权重分配完全一致。选股展示使用 `score_all_stocks(skip_industry_filter=True)` 展示全量评分（不过滤行业白名单），仅用于展示。

**风控管道**：回测和模拟盘均使用 `RiskManager.adjust_weights()`（流动性过滤 → 个股上限 → 行业上限 → 归一化）。选股展示不经风控。

### 9.2 共享执行模型

- **信号产生**: T 日收盘后（每月最后一个交易日）
- **交易执行**: T+1 日开盘价
- **调仓频率**: 月频
- **回溯**: `generate_signals()` 自动回溯 start_date 前 2 个月找最近调仓日，确保首日有持仓
- **T+1 日常模式**: 取 DB 最新两个交易日，`signal_date = T`（倒数第二日），`exec_date = T+1`（最新日）

### 9.3 交易成本（回测与模拟盘共享）

| 项目 | 买入 | 卖出 |
|------|------|------|
| 佣金 | max(5 元, 成交额 × 0.075%) | max(5 元, 成交额 × 0.075%) |
| 印花税 | — | 成交额 × 0.1% |
| 滑点 | 开盘价 × (1 + 0.1%) | 开盘价 × (1 - 0.1%) |

### 9.4 下单规则（共享）

- 最小交易单位: 100 股（1 手），向下取整
- 先卖后买（卖出释放现金后再买入）
- 买入按权重降序（优先买入权重大的股票）
- 涨停不可买入，跌停不可卖出
- 资金不足时部分成交或跳过

### 9.5 回测引擎特有逻辑

> 文件: `strategy/backtest.py`

- **一字板处理**: `open == high == low == close` 时视为一字涨停/跌停，增强涨跌停判断精度
- **跌停卖单排队**: 跌停（含一字跌停）无法卖出时加入 `pending_sells` 队列，下一个交易日开头自动重试
- **除权除息处理**: 每日循环开头检测 `adj_factor` 变化，自动调整持仓股数（`new_vol = round_to_lot(old_vol × adj_ratio)`），与 PaperTrader `_apply_corporate_actions` 逻辑一致
- **净值计算**: 内存中逐日追踪 `nav = (cash + market_value) / initial_capital`
- **基准**: 沪深 300 指数（000300.SH），支持行业指数对比

### 9.6 模拟盘引擎特有逻辑

> 文件: `execution/paper_trader.py`

- **持久化**: 账户状态（现金、持仓、交易记录、每日净值）写入 MySQL
- **除权除息**: 回放模式下检测 `adj_factor` 变化，自动调整持仓股数和成本
- **一字板处理**: `open == high == low == close` 时视为一字涨停/跌停（与回测引擎一致）
- **跌停卖单排队**: 回放模式下跌停（含一字跌停）无法卖出时加入 `pending_sells` 队列，下一个交易日开头自动重试（与回测引擎一致）
- **涨停买入阻断**: 涨停或一字涨停均不可买入

### 9.7 回测 vs 模拟盘执行差异

| 特性 | 回测 (`BacktestEngine`) | 模拟盘 (`PaperTrader`) |
|------|------------------------|----------------------|
| 一字板判断 | `open==high==low==close` | `open==high==low==close`（一致） |
| 跌停卖单 | `pending_sells` 队列，次日重试 | `pending_sells` 队列，次日重试（回放模式，一致） |
| 状态存储 | 内存（一次性） | MySQL 持久化 |
| 除权除息 | `adj_factor` 检测自动调整股数 | `adj_factor` 检测自动调整股数和成本（一致） |
| 净值追踪 | 内存 `pd.Series` | `paper_nav` 表 |

### 9.8 绩效指标（回测）

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

## 10. 可配置参数汇总

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
| MIN_VALID_CATEGORIES | 4 | MIN_VALID_CATEGORIES |
| CATEGORY_NEUTRALIZE_OVERRIDES | {"macro":"size_only","sentiment":"size_only"} | CATEGORY_NEUTRALIZE_OVERRIDES |
| STANDARDIZE_MODE | zscore | STANDARDIZE_MODE |
| WEIGHT_TEMPERATURE | 2.0 | WEIGHT_TEMPERATURE |
| REGIME_ENABLED | 1（开启） | REGIME_ENABLED |
| REGIME_MA_WINDOW | 120 | REGIME_MA_WINDOW |
| REGIME_INDEX_CODE | 000300.SH | REGIME_INDEX_CODE |
| REGIME_BEAR_OVERRIDES | {"momentum":0.6,"quality":1.5} | REGIME_BEAR_OVERRIDES |

### 风控

| 参数 | 默认值 | 环境变量 |
|------|--------|---------|
| MAX_SINGLE_WEIGHT | 0.12 | MAX_SINGLE_WEIGHT |
| MAX_INDUSTRY_WEIGHT | 0.30 | MAX_INDUSTRY_WEIGHT |
| DD_START_THRESHOLD | 0.10 | DD_START_THRESHOLD |
| DD_MAX_THRESHOLD | 0.25 | DD_MAX_THRESHOLD |
| DD_MIN_POSITION | 0.50 | DD_MIN_POSITION |
| MIN_DAILY_TURNOVER | 5000 万 | — |
| USE_VOL_TARGETING | 0（关闭） | USE_VOL_TARGETING |
| TARGET_VOL | 0.20 | TARGET_VOL |
| VOL_LOOKBACK_DAYS | 60 | VOL_LOOKBACK_DAYS |
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

### 宏观因子

| 参数 | 默认值 | 环境变量 |
|------|--------|---------|
| MACRO_ZSCORE_WINDOW | 24（月） | MACRO_ZSCORE_WINDOW |
| MACRO_PUBLICATION_LAG | {CPI:16, PPI:16, PMI:1, GDP:20, SHIBOR:0, LPR:0, UST:0} | — |
| MACRO_CYCLE_SENSITIVITY | {有色金属:1.0, 钢铁:1.0, 食品饮料:-0.3, ...} | — |
| MACRO_LIQD_SENSITIVITY | {房地产:1.0, 非银金融:0.9, 煤炭:-0.2, ...} | — |
| MACRO_INFL_SENSITIVITY | {食品饮料:0.8, 钢铁:-0.6, ...} | — |
| MACRO_EXTR_SENSITIVITY | {计算机:0.6, 电子:0.6, 银行:-0.2, ...} | — |

### 舆情抓取

| 参数 | 默认值 | 环境变量 |
|------|--------|---------|
| SENTIMENT_RATE_LIMIT | 20 req/min/domain | SENTIMENT_RATE_LIMIT |
| SENTIMENT_MAX_PAGES | 5 | SENTIMENT_MAX_PAGES |
| TWITTER_BEARER_TOKEN | （空） | TWITTER_BEARER_TOKEN |
| TWITTER_RATE_LIMIT | 90 req/min | TWITTER_RATE_LIMIT |
| TWITTER_MAX_TWEETS | 100 | TWITTER_MAX_TWEETS |

### 舆情因子

| 参数 | 默认值 | 环境变量 |
|------|--------|---------|
| SENTIMENT_LOOKBACK_DAYS | 7 | SENTIMENT_LOOKBACK_DAYS |
| SENTIMENT_DECAY | 0.3 | SENTIMENT_DECAY |
| SENTIMENT_LLM_THRESHOLD | 0.5 | SENTIMENT_LLM_THRESHOLD |
| LLM_PROVIDER | anthropic | LLM_PROVIDER |
| LLM_API_KEY | （空） | LLM_API_KEY |
| LLM_API_BASE | https://api.openai.com/v1 | LLM_API_BASE |
| LLM_MODEL | claude-haiku-4-5-20251001 | LLM_MODEL |

---

## 11. 舆情采集管道

### 11.1 中国政策层（Tier 1-4）

11 个政府网站爬虫 + CCTV新闻联播 + 巨潮公告，按政策影响力分 4 层级：

| 层级 | 来源 | 说明 |
|------|------|------|
| Tier 1 最高层 | gov_cn, xinhua, people, cctv | 国务院/新华社/人民日报/新闻联播 |
| Tier 2 产业层 | ndrc, miit, mofcom, cninfo | 发改委/工信部/商务部/巨潮公告 |
| Tier 3 金融监管 | csrc, pbc, nfra | 证监会/央行/金融监管总局 |
| Tier 4 专项行业 | nea, mohurd | 能源局/住建部 |

### 11.2 美国政策层（Tier 5）

通过 twikit 库（Twitter 内部 API，免费）采集美国关键政策人物推文，用于跟踪关税/贸易/外交政策动向：

| 来源 | 账号 | 类别 | Twitter User ID |
|------|------|------|----------------|
| twitter_trump | @realDonaldTrump | US Policy - President | 25073877 |
| twitter_vance | @JDVance | US Policy - Vice President | 1326229737551912960 |
| twitter_rubio | @marcorubio | US Policy - Secretary of State | 43201586 |

**架构设计：**
- `TwitterBaseScraper(BaseScraper)` 中间基类，重写 `__init__`/`scrape`/`parse_list_page`
- 使用 twikit `Client.get_user_tweets()` + `result.next()` 分页，纯 async，`scrape()` 中用 `asyncio.run()` 桥接
- 独立 `HttpRateLimiter`（90 req/min），3 个 Twitter 爬虫共享
- 转推过滤：`text.startswith("RT @")` 跳过
- Cookies 持久化到 `TWITTER_COOKIES_FILE`，避免重复登录
- 凭证为空或 twikit 未安装时 `scrape()` 返回空列表并打印警告，不影响其他来源

**推文→PolicyArticle 映射：**

| PolicyArticle 列 | 推文数据 |
|-------------------|----------|
| source | `twitter_trump` / `twitter_vance` / `twitter_rubio` |
| tier | 5 |
| title | 推文文本（≤500 字符，推文上限 280） |
| url | `https://x.com/{username}/status/{tweet_id}`（唯一键） |
| publish_date | twikit `created_at` 日期部分（`%a %b %d %H:%M:%S %z %Y` 格式） |
| category | `US Policy - President` 等 |
| summary | 推文全文 + 互动指标 `[RT:N, like:N]` |
| content_hash | SHA256(title\|date) |
