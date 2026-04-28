---
name: finance-basics
description: 量化投研金融常识 checklist。当任务涉及因子构建、策略回测、组合优化、风险模型、空头端、Alpha 归因、universe 筛选、财务数据使用时，必须对照本 skill 的常识条款逐项检查，避免犯低级错误。触发关键词：因子 / 回测 / alpha / beta / IC / ICIR / 选股 / 优化器 / MVO / universe / 行业中性 / 风险模型 / 空头 / 换手率 / 财报。
---

# 金融基础常识

本项目已发生过的"低级金融常识"错误的纠错手册。每次涉及量化研究/回测/组合构建时，**逐条对照检查**，不能跳过。

---

## 目录

**A. 数据层** — 数据正确性、PIT、survivorship、对齐
1. Universe / 候选池
2. Alpha vs Beta：选股 vs 风格暴露
3. 因子：截面 vs 时序
4. 财务数据：Point-in-Time
5. Survivorship Bias / 幸存者偏差
6. Look-ahead Bias / 前瞻偏差
7. 数据频率与对齐

**B. 因子 / 策略层** — 信号有效性、空头特殊性、归因
8. 空头端 / Short Side
9. IC / ICIR / t-stat 解读
10. 因子拥挤度 (Factor Crowding)
11. 业绩归因 (Performance Attribution)
12. 多空中性化层级 (Long-Short Neutralization)

**C. 组合 / 风险层** — 成本、协方差、约束
13. 换手率与交易成本
14. 风险模型 / 协方差估计
15. 组合约束体系

**D. 验证层** — 回测真实性、OOS 设计
16. 回测真实性 Checklist
17. 训练 / 验证 / 测试与 Walk-forward

**E. 区域附录**
18. 常见数据陷阱（A 股）

**速查索引**：见末尾「使用方式」节

---

## 1. Universe / 候选池

**铁律：候选池 ≠ 数据库里全部股票。回测 universe 必须是 point-in-time 可投资集合，不是今天的指数成分。**

### 标准过滤层

- 全市场 ~5000 只美股 / ~5000 只 A 股，但**实际可投资 universe** 通常 1000–3000 只
- 必须过滤：
  - **流动性**：日均成交额 > X（美股常用 $5M ADV，A 股常用 5000 万）
  - **市值**：> Y（避免 micro-cap，挂单冲击巨大）
  - **价格**：股价 > $5（美股 penny stock 摩擦多）/ A 股剔除 ST/*ST/退市风险
  - **上市天数**：> 252 个交易日（避免新股波动）
  - **停牌 / 退市**：剔除当日停牌、已退市

### Point-in-Time Universe

- **每个截面日用当时的 universe**，不是今天的指数成分股
- **指数成分股是好起点**：S&P 1500 / Russell 1000+2000 / 中证 800 / 沪深 300 + 中证 500 + 中证 1000
- **指数重组日效应**：Russell 1000/2000 每年 6 月底 Reconstitution Day，被剔除/纳入的股票交易量异常 5-10x，回测要么避开要么显式建模
- **指数成分历史**：付费源（Compustat / CRSP / Bloomberg）有 PIT 成分历史；免费源（Yahoo / Wikipedia）只有当前快照——**这是免费数据回测的最大陷阱之一**

### GICS / 行业分类的 PIT 问题

- **GICS schema 会重分类**：最著名 2018-09-28 Telecommunication Services → Communication Services（FB / GOOG / NFLX 从 IT 划入新板块）；2016-09 Real Estate 从 Financials 拆出
- 用今天的 GICS 跑 2017 回测：行业中性会污染（同一公司在历史和今天属不同行业）
- **必须用 PIT GICS**：每个截面日用当时的 sector 分类
- **A 股**：申万 2014 / 2021 重大调整，中信也变过，同样需 PIT

### ADR / 双重上市处理

- **中概股**：阿里 (BABA US + 9988 HK) / 京东 (JD US + 9618 HK) / 拼多多 等，A 股 + ADR + H 股最多三处定价
- **双重上市去重**：universe 仅保留主上市地（最高 ADV）；跨市场组合可保留多个但权重合并
- **公司行动**：双重上市股票的 split / dividend / 私有化在不同 listing 时点不同，PIT 处理要小心

### Universe 构造的两种范式

- **Rules-based**：自定义流动性 / 市值 / 价格 / 行业过滤
  - 优点：完全可控、可定制风格
  - 缺点：边界跳变（今日合规明日不合规），换手成本高
- **Index-based**：直接跟踪某指数成分（S&P 500 / Russell 3000 / 中证 800）
  - 优点：稳定、行业基准明确、与 ETF 对接顺畅
  - 缺点：被指数定义绑架（权重 = 大市值，有 size bias）

> 🚨 **本项目踩过的坑**：MVO 优化器把全 universe 2700 只股票全部作为候选池，没做流动性筛选，回测出 1373% 虚假收益（2026-04-23）

---

## 2. Alpha vs Beta：选股 vs 风格暴露

**铁律：组合收益 = β·风格因子 + ε（α）。报告 α 前必须先剥离 β。**

- **总收益 ≠ Alpha**。一个赚钱的组合可能只是：
  - 超配某行业（科技 β 高 → 上涨年份赚钱）
  - 超配某 size/value/momentum 风格
  - 杠杆放大市场 β
- **Alpha 是在控制风险暴露后的剩余**：用 Fama-French 5 因子 / Barra 模型回归，看截距 α 是否显著
- **行业中性化是 alpha 检验的黄金标准**：
  - 在每个 GICS Sector 内部做横截面 ranking
  - 如果行业内排名失效，说明全市场的 alpha 来自行业配置（β），不是选股
- **风格中性化**：beta / size / momentum / value / quality 都要剥离

> 🚨 **本项目踩过的坑**：Strategy v3 IS α=6.66% (t=2.26)，但行业中性后 alpha 归零；2025 +108% 是 AI 泡沫风格红利（β_rmw=-1.01），不是 skill

---

## 3. 因子：截面 vs 时序

**铁律：因子定义必须明确"截面"还是"时序"，混淆会让整个 IC 分析失真。**

### 截面 vs 时序

- **截面因子 (cross-sectional)**：同一日期，所有股票按因子值 ranking → 选 top / bottom
  - 本项目所有因子默认截面（CLAUDE.md 已规定）
  - IC = 当期因子值 与 下期收益的截面相关系数
- **时序因子 (time-series)**：同一股票在时间维度的信号（如 momentum t-1 → t 的方向）
- **混淆案例**：把"全市场 RSI < 30"当截面信号是错的，正确是"RSI 在所有股票里排名 bottom 10%"

### 分组测试 (Quantile Portfolios)

最常用的因子有效性可视化：

- **5 分组 (quintile)**：每个截面按因子排序分 5 组，看 Q1 vs Q5 收益差
  - 优点：每组 ~20% universe，样本量足够
  - 缺点：分辨率低，无法区分 top 5% 与 top 20%
- **10 分组 (decile)**：每组 ~10%，分辨率更高
  - 优点：能看到极端组的"长尾 alpha"
  - 缺点：每组样本少（universe < 500 时单组 < 50 只），噪声大
- **报告内容**：
  - 各组累计净值曲线（Q1 应低于 Q5）
  - Top minus Bottom (TMB) 多空组合的 Sharpe / IR
  - 分组单调性 (Monotonicity Test)：Q1 < Q2 < Q3 < Q4 < Q5 是理想，反单调说明因子方向反了

### Fama-MacBeth vs Pooled Regression

跑因子收益时两种方法选哪个：

| 方法 | 步骤 | 标准误 | 适用 |
|------|------|--------|------|
| **Fama-MacBeth** | 每个截面跑回归 → 系数时序均值 | 时序 SD / √T | 截面因子（**推荐**） |
| **Pooled (panel)** | 所有 (t, i) 数据堆叠回归 | 简单 OLS | 时序因子 |
| **Pooled + Cluster SE** | Panel 带聚类标准误（按 firm 或 time） | Cluster-robust | Panel data，需双重 cluster |

- **Fama-MacBeth 是截面因子检验黄金标准**：与 IC 分析在概念上等价（都是逐截面计算）
- **Pooled 容易低估标准误**：忽略截面相关性 → 假阳性多
- **Newey-West 修正**：FM 系数有自相关时必须用 NW 校正（→ 第 9 节）

### 因子正交化

多因子模型里因子间相关性高会导致权重不稳定 / 多重共线，两种正交化思路：

- **Sequential orthogonalization**：按优先级逐个回归剥离

  ```
  Factor_2_clean = residual( Factor_2 ~ Factor_1 )
  Factor_3_clean = residual( Factor_3 ~ Factor_1 + Factor_2 )
  ```

  - 优点：直观、可解释
  - 缺点：依赖剥离顺序，结果不唯一
- **Simultaneous (PCA-based)**：对相关矩阵做 PCA，取主成分
  - 优点：无主观顺序
  - 缺点：主成分缺乏经济意义

### 因子合成方法

把多个原始因子合成单一信号：

- **Z-score 加权**：每个因子 z-score normalize 后加权求和
  - 受极端值影响（需先 winsorize）
- **Rank 加权**：每个因子转 rank 再加权
  - 对极端值稳健（A 股推荐，因涨跌停 truncation）
- **IC 加权**：权重 ∝ 因子滚动 IC（动态调整）
  - 类似本项目滚动 IC 框架
- **机器学习集成**：Lasso / Ridge / Random Forest 学因子组合
  - 注意 walk-forward 训练（→ 第 17 节）

### 实施 checklist

- [ ] 因子定义明确截面 / 时序
- [ ] 跑 5 / 10 分组测试 + 单调性检验
- [ ] Fama-MacBeth 系数 + Newey-West 标准误
- [ ] 多因子模型先做相关矩阵检查（\|ρ\| > 0.7 触发正交化）
- [ ] 因子合成方法（Z-score / Rank / IC 加权）按数据特性选

---

## 4. 财务数据：Point-in-Time

**铁律：永远用"当时可知"的数据回测，不用"今天才知道"的数据。**

### 披露日 vs 报告期

- **报告期（period_end）≠ 披露日（announcement_date / filing_date）**
  - Q4 2024 财报：报告期 2024-12-31，披露可能 2025-03-15
  - 回测以**披露日**为可用起点
- **A 股**有业绩预告 / 业绩快报机制（→ 第 15 节），可比正式披露早 15-30 天
- **美股**：earnings call 日期 = 实际披露，10-K / 10-Q filing 通常稍晚（5-30 天）

### 财报修订（Restatements）

- **10-K / 10-Q 会被修订**：原始披露 → 后续季报修订 → 年报最终版
- **回测必须用"当时披露的版本" (first-print)**，不是最终修订版
- **大多数免费数据源只存最终版**（Yahoo / AkShare），有 look-ahead bias
- **付费源** Compustat / Wharton 有 unrestated 历史版本
- **业内做法**：标记每次披露的 vintage，回测用 first-print

### TTM 滚动窗口

- TTM (Trailing Twelve Months) = 过去 4 个季度累计或平均
- **断档处理**：
  - 公司缺一季度 → 该截面日 TTM 不可用（不要插值）
  - IPO 不足 4 季度 → universe 排除直到累积满 4 季度
  - 重大重组 / 分拆 → 子公司 TTM 不可继承

### 会计准则切换

- **ASC 606 (2018-01) 收入确认新准则**：SaaS 公司收入提前确认，跨准则 ROE/ROA 不可比
- **IFRS 16 / ASC 842 (2019-01) 租赁准则**：租赁负债上表，资产负债率突变
- **跨准则 = 不可比**：因子计算遇到准则切换日要么剔除该季度要么用调整版本（数据源通常提供 as-reported vs as-amended 两版）

### 数据 Vendor 更新延迟

- **披露日 ≠ 入库日**：Compustat quarterly 数据 vendor 入库通常 +30 天
- **回测用的是"vendor 当时已收录"的数据**，不是"SEC 已披露"
- A 股 Tushare / AkShare 入库通常披露后 1-3 天内
- **保守做法**：可用日 = max(披露日, vendor 入库日) + buffer 5 天

### 基本面语义陷阱

财务数据"看到"和"理解对了"是两码事：

- **一次性损益 (non-recurring items)**
  - 资产处置 / 重组费用 / 减值损失 / 法律和解金
  - 不剥离会让 ROE / EPS 信号失真
  - 数据源通常提供 **GAAP vs adjusted** 两版（adjusted 已剥离）
- **商誉 (goodwill)**
  - 并购溢价的"幽灵资产"，无现金流贡献
  - 减值时一次性 hit P&L（巨额非现金费用），扭曲季度 ROE
  - 高商誉公司（goodwill / total assets > 30%）需特别关注减值风险
- **研发费用化 vs 资本化**
  - GAAP 美股 R&D 全部费用化，IFRS 部分可资本化
  - 跨准则 ROE / Margin 不可比
  - 修正方法：把 R&D 加回 EBITDA，计算 R&D-adjusted ROE
- **季节性**
  - **Q4 偏差**：年末调整、奖金、坏账计提集中在 Q4，单季 EPS 有结构性偏低
  - **Q1 业绩预告**：A 股 Q1 业绩预告窗口集中，预告本身是信号
  - 同比 (YoY) > 环比 (QoQ)：季节性强行业（零售、能源）只看 YoY
- **合并范围变化**
  - 子公司收购 / 剥离会导致同比数据"假增长"
  - 须查 same-store / organic growth 口径（管理层 MD&A 会披露）
- **关联交易 / 表外项目**
  - SPV / VIE 等表外负债不在合并报表，但有实质风险
  - 美股 KMI / GE / 中概股阿里 VIE 都曾踩过坑
- **审计意见**
  - 标准无保留 / 保留 / 否定 / 无法表示意见
  - 非标意见显著负 alpha 但披露日才可知（PIT）

### 分析师预期 / 估值数据

- **EPS 修正 / 分析师预期 / DCF / target price / rating**：必须用 PIT 快照（每天存一次）
- 否则**前瞻偏差**严重：今天看到的预期值已包含未来修正

### 价格调整

- **前复权**做回测（after-adjustment）
- **不复权**仅展示用，不可作因子输入
- **A 股复权口径**必须明确（→ 第 15 节）

### ST / 退市

- 剔除**当时**已经 ST 或退市的股票，不是今天的状态
- A 股 ST 标记日是 PIT 信息

> 🚨 **MEMORY 里待办**：EPS point-in-time 快照积累（解决前瞻偏差）

---

## 5. Survivorship Bias / 幸存者偏差

**铁律：只用"今天还在的股票"做历史回测会严重高估收益（典型 +2-5% 年化虚假 alpha）。**

### 必须包含的事件

- **退市股票**：财务退市 / 重大违法退市 / 私有化 / 破产
- **被并购方**：announcement 当日 jump，必须建模
- **分拆 (spinoff)**：母公司 + 子公司双向跟踪
- **借壳上市**：原代码新主营业务，要么全段剔除要么标记重组日

### M&A 处理协议

并购事件是 survivorship 处理最关键的环节：

- **announcement 日**：被收购方股价跳跃式上涨（典型 +20-40% to deal premium）
- **deal close 日**：被收购方退出 universe
- **回测处理**：
  - 持有被收购方至 deal close → final return = 现金对价 + 收购方股票兑换价值
  - announcement 后 spread = (deal price − market price) / market price ≈ 1-5%（merger arbitrage 收益区）
  - **不可在 announcement 后剔除该股票**（会丢失价格 jump）
- **失败的并购**：announcement 后撤回，股价回吐 jump（可能 -30%）—— 这部分 P&L 必须计入

### 检查方法

- universe 数量在历史上**应有进有出**，不能始终是同一批
- 抽查 10 只 2010 年还在的股票今天的状态（应有 ~10-20% 已退市）
- 长样本（10 年+）的 historical universe vs current universe 重叠率应 < 70%

### 数据源

- **付费源** CRSP（美股）、Wind / 同花顺（A 股）有完整退市历史
- **免费源** Yahoo Finance / AkShare 只有当前 ticker 列表，**严重 survivorship bias**
- **本项目数据源** FMP 提供 `delistedCompanies` endpoint，可用于补全退市清单

---

## 6. Look-ahead Bias / 前瞻偏差

**铁律：回测每个截面只能用 t 日及之前 vendor 已入库的数据做信号 / 模型 fit。**

### 常见来源

- **未来数据泄漏**：用 t+1 收盘价计算 t 日因子（最低级错误）
- **指数成分股**：用今天的成分股回测历史（→ 第 1 节）
- **行业分类**：GICS / 申万会变，公司换行业，要用 PIT 分类（→ 第 1 节）
- **财务数据**：报告期 vs 披露日 vs vendor 入库日（→ 第 4 节）
- **In-sample fit**：用全样本 fit 的 regime / 因子权重模型回测

### Regime Detection 的前瞻陷阱

- **错误做法**：用全样本 fit HMM / 聚类 / kmeans，再切片用于历史回测
- **正确做法（expanding window）**：
  - 每个截面日 t，仅用 [start, t] 数据 fit regime 模型
  - predict t+1 日所属 regime
  - 滚动前进，每天重新 fit
- **rolling vs expanding 选择**：
  - **expanding**（推荐）：保留所有历史信息，适合 regime 持续性强的市场
  - **rolling**（如 5 年窗口）：适合假设老数据失效，但样本量小时不稳定
- **In-sample fit 是严重偷看未来**：常见自欺欺人的"regime 策略"都是这样跑出来的

### 机器学习模型的前瞻陷阱

- **特征 normalization 用全样本 mean / std** → look-ahead，必须 expanding 计算
- **Hyperparameter tuning 在测试集上做** → 信息泄漏
- **Cross-validation 随机分割** → 时序数据不可随机，必须 walk-forward 或 purged CV
- **训练集包含 test 时段对应的财报** → 看似分割了但财报披露日跨界

### 检查方法

- **Permutation test**：把因子值打乱时间顺序，IC 应趋近 0；不为 0 说明有泄漏
- **Single-day 前瞻测试**：用 t+1 数据替换 t，IC 应大幅上升（> 2x），不上升说明 t 已经包含未来信息
- **审 code**：所有 `.shift(-1)` / `.rolling(closed='right')` / 涉及未来的窗口操作逐一确认

---

## 7. 数据频率与对齐

**铁律：因子定义不可跨频率直接转换；跨市场组合必须明确时区与日历对齐基准。**

### 数据频率层级

| 频率 | 典型来源 | 适用 |
|------|---------|------|
| **Tick** | exchange direct feed / consolidated tape | HFT、microstructure 研究 |
| **Minute (1m / 5m / 15m)** | databento / polygon / TickData | 日内策略、VWAP 执行 |
| **Hourly** | 自行聚合 | 跨时区组合、跨资产关联 |
| **Daily (OHLCV)** | yfinance / FMP / Tushare | 中低频量化主流 |
| **Weekly / Monthly** | 各源聚合 | 长期策略、宏观因子 |

### 跨频率转换的陷阱

- **不可直接平均**：日频 momentum (20D) ≠ 月频 momentum (1M)，因为日内波动结构不同
- **重采样 anchor 选择**：
  - 月频 = 月底最后一个交易日（business month-end）
  - 周频 = 周五（A 股）/ 周一（部分美股）
  - anchor 不一致会导致 IC 不可比
- **HF → LF 聚合**：sum / mean / last / first 不同，需明确（成交量 sum，价格 last，volatility √(sum of variance)）
- **LF → HF 插值**：财务数据（季频）插值到日频时，**不能线性插值**（泄漏未来），必须 step function（披露日跳变）

### 时区与交易日历

- **A 股**：UTC+8，9:30-11:30 / 13:00-15:00（含集合竞价 9:15-9:25 / 14:57-15:00）
- **港股**：UTC+8，9:30-12:00 / 13:00-16:00
- **美股**：ET（UTC-4 夏令时 / -5 冬令时），9:30-16:00 + 盘前 4:00-9:30 + 盘后 16:00-20:00
- **跨市场组合的对齐基准**：
  - 各市场**当地收盘价** + 各自交易日历（推荐）
  - 用 UTC 时间戳统一（适合分钟级跨资产）
  - 用某市场为锚（如美股收盘 = T 日 16:00 ET，其他市场截取最近收盘）

### 节假日与日历对齐

- **A 股节假日**：春节（7-10 天）/ 国庆（7 天）/ 五一 / 元旦 / 清明 / 端午 / 中秋
- **美股节假日**：MLK / Presidents Day / Good Friday / Memorial Day / Independence Day / Labor Day / Thanksgiving / Christmas（含 9 个完整休市 + 3 个半日休市）
- **半日休市**：美股感恩节后 / 圣诞前夕通常 13:00 ET 收市
- **节假日错配**：A 股开市但港股 / 美股休市（反之亦然），跨市场组合当日标记为单边交易日
- **临时停牌 / 熔断**：2015 中国熔断 / 2020 美股熔断 / 2022 LME 镍停盘 — 历史回测要含这些异常日

### 实施建议

- **本项目场景**：日频 OHLCV 为主，因子计算用 daily anchor
- **跨市场对齐**：A 股 / 港股按 UTC+8、美股按 ET 各自维护交易日历
- **交易日历包**：Python 用 `exchange_calendars` / `pandas_market_calendars`，Rust 用自维护 JSON
- **时间戳统一**：DB 存 UTC，展示按市场本地时间转换

### 常见错误

- **A/H 价差因子算错**：A 股 15:00 收盘 vs 港股 16:00 收盘，直接做差有 1 小时滞后
- **国庆 7 天后的"上涨" / "下跌"**：跨假期收益 ≠ 7 个交易日收益（信息累积），单独建模
- **日内 momentum 转日频**：开盘 vs 收盘 momentum 不同，需明确 anchor
- **TTM 跨财年错位**：日频 TTM 时，财年末"刷新"日要对齐

---

## 8. 空头端 / Short Side

**铁律：空头不是简单地"对 bottom N 取负权重"。空头有一组多头不存在的法律 / 操作 / 税务约束，不建模会假设性地高估收益。**

### 成本结构

- **借券费率 (borrow fee)**：普通 GC (general collateral) 0.3-3%/年；hard-to-borrow 可达 50%/年；meme 股可瞬间冲到 100%+
- **借券费率每日变动**：回测必须用 daily 借券快照，不能用静态平均（券池流动性日内变化）
- **股息赔付义务**：空头持有期间必须把分红"赔付"给借出方（cash-in-lieu）
  - 进一步：long 端股息可享 qualified dividend 15-20% 税率，short 端的 cash-in-lieu 是普通收入 37%，**税务上空头还有 ~15-20% 额外侵蚀**
- **rebate**：现金抵押品的利息回报，正常情况下 rebate ≈ Fed funds − fee；hard-to-borrow 时 rebate 转负
- **市场冲击**：空头平仓往往比多头平仓难（轧空风险），冲击成本要 ×1.5

### 监管 / 操作约束

- **Reg SHO Locate Rule**：美股做空必须在 T+1 前 locate 借券来源，否则是 naked short 违规
- **强制 buy-in**：券商在借不到券时可以单方强制平仓你的空头（任意价格成交）
- **Threshold Security List**：连续 5 个交易日 fail-to-deliver 的标的进入限制名单，新增空头被禁
- **Uptick Rule (SSR, Reg SHO Rule 201)**：单日 -10%+ 触发，次日及当日剩余时间空头只能在 above-bid 报价（不能 hit bid）
- **Pattern Day Trader**：retail 账户 < $25k 限制 5 天 4 次 day trade，机构无此限制

### 信号性质

- **空头 ≠ 多头反向**：多空因子的 IC 通常 **多头侧 > 空头侧**（"alpha decays on the short side"）
- **空头集中度**：10 只空头太集中，单只 squeeze 毁组合；推荐 ≥ 50 只
- **下行保护幻觉**：半仓做空 + 半仓做多 在熊市的"保护"主要来自半仓 cash 效应，不是空头对冲
- **Short Interest Ratio (SIR / days-to-cover)** = SI / ADV，> 5 days 的标的轧空风险高
- **Crowded short 高度危险**：见第 9 节，2021 GME 是教科书

### 实施 checklist

- [ ] 借券成本日度建模（不是静态 bps）
- [ ] 股息日赔付 + 税务调整
- [ ] Reg SHO locate 模拟（约 5-10% 标的某些日不可借）
- [ ] SSR 触发后次日不开新空头
- [ ] 单空头 < 2% AUM、≥ 50 只
- [ ] SIR > 5 days 的标的列入观察名单（不一定剔除但要监控）

> 🚨 **本项目结论**：空头端形同虚设；下行保护来自半仓效应而非空头对冲。归因 (→ 第 10 节) 显示 Short IR ≈ 0，应考虑改 long-only + index hedge

---

## 9. IC / ICIR / t-stat 解读

### 基础指标

| 指标 | 含义 | 显著性阈值 |
|------|------|-----------|
| **IC（截面）** | 因子值 与 下期收益 的截面 Spearman/Pearson 相关 | \|IC\| > 0.03 可用，> 0.05 强 |
| **ICIR** | mean(IC) / std(IC)，类似 IC 的 Sharpe | > 0.5 可用，> 1.0 强 |
| **t-stat** | α / SE(α) | > 2.0 边缘显著，> 3.0 显著 |
| **Sharpe** | (年化收益 − rf) / 年化波动 | > 1.0 可投资，> 1.5 优秀，> 2.0 稀有 |

### Spearman vs Pearson IC

- **Pearson**：线性相关，对极端值敏感
- **Spearman**：rank 相关，对极端值稳健
- **A 股强烈推荐 Spearman**：涨跌停 truncation 严重，Pearson 会被极端 ±10% 收益污染
- **美股大盘股 Pearson 可用**，但因子值和收益必须先 winsorize（1%/99%）
- 报告时**两个都报**，差距大说明分布偏态严重

### IC Half-life（衰减速度）

- 定义：因子从 t 日预测能力衰减到一半的天数
- **决定调仓频率**：half-life = 5 天的因子月频调仓浪费 80% alpha；half-life = 60 天的因子日频调仓徒增成本
- **估计方法**：autocorrelation of factor returns，或 lagged IC(t, t+k) vs k 的指数拟合
- **典型值**：短期反转 1-3 天 / 动量 60-120 天 / 价值 250+ 天 / 质量 250+ 天

### 多重检验校正

跑 100 个因子总有几个 t > 2，必须校正：

| 方法 | 控制目标 | 阈值变化 | 适用 |
|------|---------|---------|------|
| **Bonferroni** | Family-Wise Error Rate (FWER) | t > 2.0 → t > √(2log N) | 太保守，仅少量假设时用 |
| **Benjamini-Hochberg (BH)** | False Discovery Rate (FDR) | 按 p-value 排序，p_(i) < (i/N)·α | **批量因子测试推荐** |
| **Deflated Sharpe Ratio** | 多次试错下的 Sharpe 显著性 | DSR = (SR − E[max SR_N]) / std(SR) | 策略层（不是因子层） |
| **Probabilistic Sharpe Ratio** | 非正态分布下的 SR 置信度 | PSR(SR*) = Pr(SR > SR*) | 偏态 / 厚尾收益 |
| **Newey-West** | 自相关误差校正 | 修正 SE(α) 的 lag 偏差 | 时序回归 |

> 🚨 Strategy v3 t = 2.26：**单因子检验显著，多次试错后大概率不显著**。Lopez de Prado 的经验法则：N=100 个策略尝试时，需要 raw Sharpe > 2.5 才能等同于单次 Sharpe > 1

### Deflated Sharpe Ratio 公式

```
DSR = Φ((SR − E[max SR_N]) / σ(SR))
E[max SR_N] ≈ √(2 log N) − γ / √(2 log N)        (γ = Euler-Mascheroni ≈ 0.577)
σ(SR) ≈ √((1 − γ_3·SR + (γ_4 − 1)/4 · SR²) / (T − 1))   (γ_3 偏度, γ_4 峰度)
```

- N = 你尝试过的策略 / 因子组合数
- T = 样本天数
- DSR < 0.95 则不显著

### 实操要点

- **IS / OOS 必须分开**：IS Sharpe > OOS Sharpe 是常态，OOS 跌一半很常见
- **t = 2.26 是边缘显著**，不是"很强的 alpha"
- **IC 月度化报告**：单日 IC 噪声大，月度均值 + IR 更稳定
- **IC 分行业看**：全市场 IC = 0.05 但某些行业 IC = -0.05 是常见现象（→ 第 10 节归因）

---

## 10. 因子拥挤度 (Factor Crowding)

**铁律：alpha 不是因子值给的，是"少数人发现的因子值"给的。因子被广泛持有时收益模式发生质变——顺风加速、逆风单日暴跌。**

### Crowding 三阶段

- **发现期**：少数研究者识别因子，IC 稳定但温和（~0.03），alpha 是信息溢价
- **拥挤期**：资金涌入，rolling Sharpe 创历史新高，short-term IC 暴正（>0.08），但 alpha 来源已变质——不再是信息溢价，是资金流推动
- **去拥挤期**：持有人同步减仓 → 流动性枯竭 → 因子组合单日 -8σ（一天 -10% 不罕见）

### Crowding 度量方法

| 方法 | 数据源 | 命中阈值 |
|------|--------|---------|
| **13F holdings overlap** | SEC 13F（本项目已有 `us_13f_holdings`） | top 50 基金共持比例 > 30% |
| **Short Interest 集中度** | FINRA SI / Quiver dark-pool | 多空因子顶部 SI 占比 > 历史 2σ |
| **Thematic ETF AUM 增速** | ETF prospectus（ARKK/SOXX/KWEB/QUAL） | 6M AUM 增速 > 50% |
| **因子 Sharpe 历史分位** | 因子日收益 | rolling 12M Sharpe > 历史 95% 分位 |
| **因子收益 autocorrelation** | 因子日收益 | 1-day autocorr 从 ~0 转 +0.1 |
| **因子收益 kurtosis** | 因子日收益 | 60D kurtosis > 10（尖峰厚尾爆表） |
| **同行 beta** | 公开 quant fund 净值回归 | β 上升 = 持仓趋同 |

### 历史 quant crash 必看

- **2007 quant quake (Aug 7-9)**：momentum / value / 低波动同步回撤 30%+，3 天反弹但当年永久退出 30% 资金。导火索：LTCM 风格爆仓 → 同行强制平仓 → 多因子模型同步去杠杆
- **2020 Q1 momentum crash (Mar)**：Renaissance Institutional Equities 当年 -19%，momentum 因子 Q1 -25%
- **2021 Jan GME / meme squeeze**：crowded short 触发 systematic multi-strat 多空双爆
- **共同模式**：因子 IC 长期为正 → 被发现 → 资金涌入 → IC 极正且 kurtosis 暴增 → 单日反向 -10%+ → 因子永久衰减或半衰

### 应对策略

- **拥挤因子降权**：3+ 个 crowding 指标命中 → 多因子模型该因子权重砍半
- **流动性 buffer**：单仓位 < 5 ADV days，确保去拥挤时 5 天内能撤
- **反转 overlay**：拥挤期叠加 short-term reversal 信号抵消顺风加速
- **regime overlay**：crowding 高时切到 low-beta / quality 防御组合
- **绝不在拥挤顶部加杠杆**：2007 quant quake 重灾区都是高杠杆 quant fund

### 本项目的 crowding 证据

> 🚨 **2025 +108% 收益的 crowding 拆解**：
> - β_rmw = -1.01 → 组合极端 short-quality = long glamour / AI / momentum
> - 2024-2025 AI 泡沫期 ARKK / SOXX / KWEB / SMH 规模膨胀
> - 顶部持仓集中 NVDA / META / TSLA 等 13F 高重叠标的
> - 同期 RMW 负侧 Sharpe 创历史新高（>2σ）
> - **结论**：收益是 crowding-driven beta，不是 idiosyncratic alpha；策略本质是 thematic momentum ETF 的杠杆放大版

> 🚨 **空头端形同虚设的根因之一**：高 SI 名单上的股票（meme-like）容易被轧空，空头组合在 risk-on 日单日大跌；alpha 衰减叠加 crowding squeeze 双重失效

### 操作 checklist（建议每月跑一次）

- [ ] 因子日收益的 60D rolling kurtosis / autocorr / Sharpe 分位
- [ ] 13F top 50 基金顶部持仓重叠度（用 `us_13f_holdings` 表）
- [ ] thematic ETF AUM 6M 增速（ARKK/SOXX/KWEB/QUAL/SMH）
- [ ] 任 3 项命中 → 触发减仓 alert + 归因报告
- [ ] 历史压力测试：组合在 2007-08-07 / 2020-03-09 / 2021-01-27 三日的模拟收益

---

## 11. 业绩归因 (Performance Attribution)

**铁律：没归因不能改策略。看到 +20% 收益不知道哪 5% 是 alpha 哪 15% 是 beta，任何调权决策都是盲目的。**

### 三种归因方法

| 方法 | 拆分维度 | 适用场景 |
|------|---------|---------|
| **Brinson-Fachler** | 行业配置 vs 个股选择 | 多头主动管理（vs benchmark） |
| **Factor-based (Barra)** | Country / Industry / Style 因子 + Specific | 多因子量化策略 |
| **多空分别归因** | Long book IR vs Short book IR | 多空策略 |

### Brinson-Fachler 公式

把组合相对基准的超额收益拆为三部分：

- **配置效应 (Allocation)** = Σᵢ (wᵢ_p − wᵢ_b) × (Rᵢ_b − R_b)
  - 行业相对基准超配 / 低配带来的收益
- **选股效应 (Selection)** = Σᵢ wᵢ_b × (Rᵢ_p − Rᵢ_b)
  - 行业内选股相对行业基准的超额
- **交叉项 (Interaction)** = Σᵢ (wᵢ_p − wᵢ_b) × (Rᵢ_p − Rᵢ_b)
  - 配置 × 选股的交互（实操中并入选股或单独报告）

> 🚨 **本项目核心矛盾的诊断工具**：Strategy v3 IS α=6.66% 是 allocation 还是 selection？MEMORY 已结论"行业中性后 alpha 归零"——本质就是 Selection ≈ 0、全部来自 Allocation（超配科技）。Brinson 是把这个口头结论变成数字表的标准方法

### Factor-based (Barra-style) 归因

更细的拆分，按风险模型因子分解 P&L：

```
R_portfolio = R_market + Σ β_style · F_style + Σ β_industry · F_industry + α_specific
```

- **β_style**：组合在 size / value / momentum / quality(RMW) / volatility / liquidity 等风格因子的暴露
- **F_style**：当期风格因子收益
- **α_specific**：剩余的 idiosyncratic alpha（真正的选股能力）

> 🚨 **2025 +108% 的 Barra 拆解**：β_rmw = −1.01 × 当期 RMW 因子收益（强负） = 大正贡献。这部分是 **style timing 的 β，不是 α**。把它包装成 alpha 是常见的归因误用（→ 第 2 节）

### 多空分别归因

多空组合的 IR 不能只看总体，必须分开诊断：

| 项 | 公式 | 解读 |
|---|------|------|
| **Long book IR** | mean(R_long − R_long_bench) / TE_long | 多头侧 alpha |
| **Short book IR** | mean(R_short_bench − R_short) / TE_short | 空头侧 alpha（注意符号） |
| **L/S total** | (R_long − R_short) / σ_LS | 整体多空 IR |
| **L vs S 贡献拆分** | Long_bps vs Short_bps 各占多少 | 哪边在真正赚钱 |

> 🚨 **本项目结论**：Long IR > 0，Short IR ≈ 0 或负 → "alpha decays on the short side" 的典型症状；下行保护实际来自半仓 cash equivalent 效应，不是空头对冲（→ 第 7 节）

### 归因结果如何驱动决策

| 归因结果 | 决策 |
|---------|------|
| Selection ≈ 0、Allocation 主导 | 不是选股策略：要么承认是 sector rotation，要么加行业中性约束 |
| Specific α ≈ 0、Style β 主导 | 检查是否 crowded style（→ 第 9 节）；考虑 style 中性 |
| Long IR 强、Short IR 弱 | 改 long-only + index hedge，废弃 single-name short |
| Style 暴露 vs benchmark 偏离过大 | 加风格中性约束（\|β_style\| < 0.3） |
| 单日 P&L 由 1-2 只股票主导 | 集中度过高，加 single-name cap |
| 牛市归因好、熊市归因差 | regime 不稳健，重新审视 OOS |

### 归因实施 checklist（每月跑）

- [ ] Brinson 三项分解（用 point-in-time GICS Sector + benchmark）
- [ ] Barra 风格暴露 + 风格收益贡献分解
- [ ] Long book / Short book / Net 三本账分别归因
- [ ] 归因与第 12 节 backtest checklist 交叉比对
- [ ] 如果 Specific α / Total Return < 30%，重新审视策略本质
- [ ] 多 regime 分别归因（牛 / 熊 / 横盘）

### 常见误用

- **不要把"行业择时"包装成 alpha**：超配科技 2024-2025 赚钱不是选股能力
- **不要只报 IS 归因**：必须 OOS 独立跑一份
- **不要按今天的 GICS 跑历史归因**：用 point-in-time GICS（→ 第 1 节）
- **基准选错会扭曲所有结论**：long-short 策略的基准是 0 或 rf，不是 SPY
- **不要省略交叉项**：低配且选股错的行业 interaction 是负值，省略会高估 selection

---

## 12. 多空中性化层级 (Long-Short Neutralization)

**铁律："中性"不是一个概念，是多个独立层级。dollar-neutral ≠ market-neutral ≠ sector-neutral。混用术语会让组合在你以为安全的维度上爆雷。**

### 五种中性化层级

| 层级 | 约束公式 | 防御什么 | 实施成本 |
|------|---------|---------|---------|
| **Dollar-neutral** | Σ w_long = Σ \|w_short\| | 总美元敞口 = 0 | 低（按权重缩放） |
| **Beta-neutral** | Σ w_long·β_long = Σ \|w_short\|·β_short | 市场 β 敞口 = 0 | 中（需估计 β） |
| **Sector-neutral** | 每个 GICS Sector 内 Σw_long = Σ\|w_short\| | 行业 β 敞口 = 0 | 中（行业分类要 PIT） |
| **Style-neutral** | \|β_size / value / mom / rmw\| < ε | 风格因子敞口 = 0 | 高（需 Barra 风险模型） |
| **Country / Currency-neutral** | 各国 / 各币种敞口 = 0 | 国家 / 汇率风险 = 0 | 高（多区域组合用） |

### 关键认知：层级是相互独立的

满足一个不代表满足其他。举例：

- Long $1M NVDA (β=2.0) + Short $1M XLU 公用事业 ETF (β=0.5)
- ✅ Dollar-neutral（多空各 $1M）
- ❌ Beta-neutral（净 β = 2.0 − 0.5 = 1.5）
- ❌ Sector-neutral（科技 +$1M / 公用事业 −$1M）

这种组合在牛市暴赚（β 敞口 1.5），熊市暴亏。**很多自称 "market-neutral" 的策略实际只是 dollar-neutral**。

### Dollar-and-Beta-Neutral（工业标准最低要求）

```
Σ w_long = Σ |w_short|                          (dollar)
Σ w_long · β_long = Σ |w_short| · β_short       (beta)
```

实施方式：
- 优化器加两个等式约束（cvxpy / OSQP 都支持）
- β 用过去 252 天对 SPY 的回归估计（OLS / Ledoit-Wolf 缩减）
- β 必须 PIT、winsorize（高 SI 名单 β 经常是异常值）
- 周频或月频再平衡，不每日调整

### Sector-Neutral 的两种做法

- **硬约束（行业内 L/S）**：每个 GICS Sector 内独立做 long-short
  - 优点：彻底剥离行业 β
  - 缺点：每个行业仅 50-200 只股票，分散度低
- **软约束**：组合层面 \|sector_exposure\| < 5%
  - 优点：保留行业内深选股 + 跨行业横截面排序
  - 缺点：仍有少量行业敞口

> 🚨 **本项目 P0 待办**："行业内 long-short 测试" = 严格硬 sector-neutral 验证。MEMORY 已有"行业中性后 alpha 归零"是初步结论，硬 sector-neutral 跑完才能定论

### Style-Neutral 的两种做法

- **事前 (ex-ante)**：优化器加 \|β_style\| < 0.3 约束
- **事后 (ex-post)**：跑完组合后回归剥离 style 收益，剩余视为 specific alpha

> 🚨 Strategy v3 在 ex-post 剥离 style 后 alpha 归零；V6 若要保留 specific alpha，必须 ex-ante 加 style-neutral 约束（→ 第 12 节风险模型 + 优化器实施）

### 选择层级的决策树

```
你的 alpha 来自哪？
├─ 跨行业横截面排序  → dollar + beta-neutral
├─ 行业内选股        → + sector-neutral（硬或软）
├─ 多因子模型        → + style-neutral（防 crowding 红利）
└─ 多区域组合        → + country / currency-neutral
```

**叠加是常态**。高质量量化基金通常 dollar + beta + sector + style 四重中性。每加一层中性：
- 减少 R²（解释方差） → 留给 specific alpha 的空间更小但更纯
- 增加约束 → 优化解可能不可行 → 用 soft constraint 或松弛
- 提高换手 → 约束随 β / sector / style 时变每日扰动 → 成本上升

### 常见错误

- **把 dollar-neutral 等同于 market-neutral** → 实际净 β 可能 ±1
- **用今天的 β 跑历史回测** → β 时变，必须 PIT
- **Beta 估计窗口选错** → 短窗噪声大，长窗跟不上 regime（推荐 252 天 + 半衰期 63 天）
- **忽视短端股票的 β 偏差** → 高 SI 名单 β 异常，需 winsorize
- **Sector schema 混用** → A 股用申万 / 中信，美股用 GICS，混用污染中性约束
- **以为 sector-neutral 自动 style-neutral** → 行业内仍可能有 size / value / momentum 偏离

### 中性化实施 checklist

- [ ] 明确 alpha 来源 → 选择对应中性化层级
- [ ] β 估计：PIT、winsorize、shrinkage
- [ ] Sector 分类：PIT、与 universe 一致
- [ ] 优化器约束：硬 vs 软 vs 惩罚项（按可行性选）
- [ ] 中性化前后 R² 对比，确认风格暴露有效降低
- [ ] 月度报告残余敞口（不应 > 阈值）
- [ ] 中性化后 OOS Sharpe 仍 > 阈值才算合格策略

---

## 13. 换手率与交易成本

**铁律：交易成本会侵蚀 alpha。年化换手率 > 4x 时，成本可能超过 alpha。**

### 换手率定义

- **单边换手率** = 总成交额 / 2 / 平均 AUM（除以 2 因为买卖各算一次）
- **双边换手率** = 单边 × 2
- **年化换手** = 月度 × 12 或日度 × 252

### 交易成本结构

成本 = **佣金 + 价差 (spread) + 冲击 (impact)**：

- **美股大盘股**：单边 5-10 bps
- **美股小盘股**：单边 20-50 bps
- **A 股**：单边 10-20 bps（佣金 + 印花税卖出 0.05% + 冲击）
- **ETF**：单边 1-3 bps（流动性最好）

### A 股印花税不对称

- **买入 0、卖出 0.05%**（2023-08 减半，原 0.1%）
- **影响**：
  - 多空策略空头侧不交印花税（融券平仓视为买入），多头平仓必交
  - 对**短期反转 / 高换手策略**影响显著
  - **降换手对 A 股价值更大**（vs 美股双向均匀）

### 冲击成本（非线性）

- **常数 bps 模型**（最简单）：cost = const × notional，回测早期可用
- **sqrt-law（Almgren-Chriss 简化）**：

```
impact_bps = α · σ_daily · √(order_size / ADV)
```

- α 经验常数 ~10-20 bps
- σ_daily = 股票日波动率
- order_size / ADV = 成交占比
- **直觉**：成交 1% ADV 冲击 ~10 bps；成交 10% ADV 冲击 ~30 bps（不是 100 bps）
- **何时切换**：单股 order > 5% ADV 必须用 sqrt-law；本项目 < $50M AUM 时常数 bps 够用

### 借券费率时间结构

见第 7 节。hard-to-borrow 列表每日变化，回测要 daily 借券快照，不能用静态平均。

### 降换手手段

- **Buffer zone**：top 30% 加入，跌出 top 50% 才剔除
- **换手惩罚**：优化器目标函数加 − λ·turnover
- **Persistence weight**：因子值用 N 日滚动均值代替日值
- **Bootstrap rebalance**：限制每周再平衡上限 N%
- **No-trade zone**：单股票 weight 变化 < threshold 时不调

### 例：年化换手率成本影响

- 年化 8x 换手 + 单边 10 bps = **1.6% 年化成本**，吃掉一半 alpha
- 年化 12x 换手 + 单边 15 bps = 3.6%，alpha 必须 > 3.6% 才有正收益

> 🚨 **MEMORY 里待办**：换手率控制（年化 ~8x 太高）

---

## 14. 风险模型 / 协方差估计

**铁律：MVO 对输入扰动极度敏感。Σ 估计错 1% → 权重可能错 50%（"Markowitz Optimization Enigma", Michaud 1989）。**

### 协方差矩阵估计

- **样本协方差问题**：N 股票需 N(N+1)/2 个参数；T < N 时矩阵不可逆
- **缩减估计 (shrinkage)**：
  - **Ledoit-Wolf (2004)**：Σ̂ = (1−δ)·Σ_sample + δ·F，F = 常数相关矩阵；δ 解析最优
  - **Factor model**：Σ = B·F·B' + Ω
    - **Barra**：行业 + 风格因子 + 国家因子
    - **PCA**：前 k 个主成分作为统计因子
    - **Fama-French**：市场 + size + value + 等
  - **DCC (Dynamic Conditional Correlation)**：相关系数时变

### Risk Attribution 公式

组合方差 = w'·Σ·w，单股票贡献：

```
RC_i = w_i · (Σw)_i / σ_p          (Risk Contribution)
σ_p = √(w'·Σ·w)                    (组合波动率)
Σ RC_i = σ_p                        (加和等于总风险)
```

- **Risk Parity**：所有 RC_i 相等
- **Marginal Risk Contribution**：MC_i = (Σw)_i / σ_p，加仓 1 单位 i 的边际风险

### MVO 不稳定性问题

**Michaud (1989) "Markowitz Optimization Enigma"**：
- μ 输入扰动 1% → 最优权重可能反转
- 对噪声估计的"过度拟合"——历史 Sharpe 高的股票被极端 overweight
- **诊断**：μ 加 ±5% 扰动，看权重重叠度，应 > 80%

### 稳健化方法

- **Resampled Efficient Frontier (Michaud)**：bootstrap μ/Σ 多次跑 MVO，权重平均
- **Robust Optimization**：在 μ/Σ 的不确定集上做 worst-case 优化
- **Black-Litterman (1992)**：工业标准
  - 先验：市场均衡隐含收益（CAPM 反推）
  - 观点：分析师 / 因子模型给的预测 + 置信度
  - 后验：Bayesian 融合 → 更稳定的 μ̂
  - **优势**：长期看比朴素 MVO 稳定 20-30%
- **Hierarchical Risk Parity (Lopez de Prado 2016)**：用聚类 + 递归二分配权重，绕过 Σ 求逆

### MVO 输入要求

- **μ**：预期超额收益
  - 必须 winsorize（极端值会主导）
  - 单位是年化（与 Σ 时间尺度对齐）
  - 量纲一致（同 universe）
- **Σ**：协方差
  - 必须正定（缩减保证）
  - 与 μ 同时间窗口
- **约束**：
  - long-only / 多空 / dollar-neutral
  - 行业暴露 \|β_sector\| < 阈值（→ 第 11 节）
  - 单股票上限 \|w_i\| < 5%（避免集中）
  - turnover ≤ Y% / 期（→ 第 12 节）
  - tracking error 上限（vs benchmark）

### 实施 checklist

- [ ] Σ 估计用 Ledoit-Wolf 或 Factor model（不裸用样本协方差）
- [ ] μ 经过 winsorize、shrinkage 处理
- [ ] MVO 输出做扰动测试（μ ±5% 扰动权重重叠 > 80%）
- [ ] 权重 sanity check：top 10 holdings < 30% AUM
- [ ] 历史回测验证 ex-ante vol vs ex-post realized vol（应 ±20% 内吻合）

---

## 15. 组合约束体系

**铁律：约束不是事后加的，是策略设计的一部分。无约束的优化器输出永远是"集中持有 1-3 只股票"的退化解。**

### 约束的层级

| 层级 | 约束类型 | 典型阈值 |
|------|---------|---------|
| **单股票** | abs weight cap | \|w_i\| < 5% AUM |
| **单股票** | ADV 占比 cap | order < 5-10% × ADV |
| **行业** | sector exposure cap | \|w_sector − w_bench_sector\| < 5% |
| **国家 / 地区** | country exposure cap | \|w_country − w_bench\| < 10% |
| **风格** | style β cap | \|β_size / value / mom\| < 0.3 |
| **市场** | market β cap | \|β_market\| < 0.1（中性策略） |
| **波动** | vol target | 年化 σ_p ∈ [10%, 15%] |
| **杠杆** | gross leverage | (Σ\|w_i\|) ≤ 2.0 |
| **集中度** | concentration | top 10 holdings < 30% |
| **换手** | turnover budget | 月度单边 < 30% |
| **回撤** | DD limit | trailing 6M DD > 10% 触发减仓 |

### 硬约束 vs 软约束 vs 惩罚项

| 类型 | 实施 | 优点 | 缺点 |
|------|------|------|------|
| **硬约束** | optimizer 等式 / 不等式约束 | 严格满足 | 可能 infeasible |
| **软约束** | violation 大于阈值才触发 | 可行性强 | 边界附近不稳定 |
| **惩罚项** | objective 加 λ·violation² | 永远可行、平滑 | 阈值由 λ 隐式决定，难调 |

**实操推荐**：核心约束（单股 cap、行业 cap）用硬约束；turnover、tracking error 用惩罚项。

### 单股票 ADV 占比 cap（最关键且最易遗漏）

- AUM = $50M，单股 weight = 3% → 持仓 $1.5M
- 若 ADV = $5M，持仓占 30% ADV → 进出场需 6 个交易日
- **典型规则**：单股持仓 < 5 ADV days（即 holding / ADV < 5）
- **回测扩容陷阱**：小 AUM 回测能跑通的策略，扩到 $500M 时约束全部触发，权重全部 cap → alpha 严重衰减
- **解决**：回测显式建模 ADV cap，画出 capacity curve（AUM vs Sharpe）

### Tracking Error 上限（vs benchmark）

- TE = √Var(R_p − R_b)
- **典型策略**：
  - **Enhanced Index**：TE 1-3%（轻度 active）
  - **Active**：TE 3-6%（主流主动管理）
  - **Long-Short**：TE 8-15%（无 benchmark 时算 absolute risk）
- **TE 与 IR 关系**：α = IR × TE，给定 alpha 目标 → 反推 TE 预算

### Drawdown 软停损规则

- **trailing 6M DD > X% → 减仓 50%**
- **trailing 12M DD > Y% → 减仓 100%**
- **何时恢复**：DD 修复 50% 或新高 → 重新加仓
- **争议**：是否会"在最低点止损"？历史回测必须分别测试启用与不启用

### 约束设计的决策流

```
1. 明确策略类型（long-only / long-short / market-neutral）
2. 明确 alpha 来源（→ 第 12 节中性化层级）
3. 明确 capacity 目标（AUM 上限）
4. 设定 risk budget（vol target / TE / DD limit）
5. 反推单股 / 行业 / 风格约束阈值
6. optimizer 实施（硬 / 软 / 惩罚）
7. 历史回测验证 binding 频率（< 30% 时段触发约束算合理）
```

### 实施 checklist

- [ ] 单股 ADV 占比 cap（最关键）
- [ ] 单股 weight cap
- [ ] 行业 / 国家暴露 cap
- [ ] 风格 β cap（→ 第 12 节）
- [ ] Vol target 或 leverage cap
- [ ] Turnover budget（→ 第 13 节）
- [ ] DD 软停损规则
- [ ] 约束 binding 频率监控

### 常见错误

- **忘记 ADV 约束** → 回测 alpha 强但扩容崩溃
- **硬约束设过严** → optimizer infeasible，每次返回退化解
- **软约束阈值与 λ 不一致** → 实际行为不符合预期
- **不监控 binding 频率** → 不知道哪些约束实际起作用
- **回测时未模拟约束** → 上线后优化器输出与回测差异巨大

---

## 16. 回测真实性 Checklist

每次发回测结果前，强制自检：

- [ ] Universe 是 point-in-time，不是今天的成分股
- [ ] Universe 经过流动性 / 市值 / ST 筛选
- [ ] 财务数据用披露日，不用报告期
- [ ] 分析师预期用 point-in-time 快照
- [ ] 价格用前复权
- [ ] 包含已退市股票（无 survivorship bias）
- [ ] 信号 t 日生成，t+1 日开盘 / VWAP 执行（不是 t 日收盘）
- [ ] 扣除交易成本（至少单边 5-10 bps）
- [ ] 扣除借券成本（如有空头）
- [ ] 行业中性 / 风格中性后 alpha 是否还在
- [ ] IS / OOS 分割，OOS 不能 fit
- [ ] 多重检验调整

> 🚨 任何回测出现 IS Sharpe > 3 / 年化收益 > 50%，第一反应是"哪里漏了"，不是"找到圣杯"

---

## 17. 训练 / 验证 / 测试与 Walk-forward

**铁律：测试集只能跑一次。在测试集上调参就把它变成了验证集，必须重新切分。**

### 三段式分割

| 集 | 用途 | 看几次 |
|---|------|-------|
| **训练集 (Train)** | fit 模型 / 估计因子权重 | 任意次 |
| **验证集 (Validation)** | 超参选择 / 模型对比 | 多次（每次都污染） |
| **测试集 (Test)** | 最终 OOS 评估 | **只能 1 次** |

- **典型分割**：60% Train / 20% Val / 20% Test（按时间顺序）
- **本项目当前** IS / OOS 2021 切分 → 实质是 Train+Val / Test 两段，验证集与训练集混合，调参时无独立验证

### Walk-forward 框架

时序数据不能随机分割，必须按时间滚动：

```
[Train_1     ] → Val_1 → Test_1
   [Train_2     ] → Val_2 → Test_2
      [Train_3     ] → Val_3 → Test_3
         ...
```

- **Expanding window**：训练集只增不减（推荐，保留所有历史信息）
- **Rolling window**：训练集固定长度（适合假设老数据失效）
- **Step size**：滚动步长（月度 / 季度 / 年度），决定重训练频率

### Purged Cross-Validation (Lopez de Prado)

经典 K-fold CV 在金融时序数据上有 lookahead 风险：

- **Purge**：剔除训练集中跨越验证集边界的样本（避免标签信息泄漏）
- **Embargo**：验证集后留 buffer 期再放回训练集（避免 serial correlation 泄漏）
- **Combinatorial Purged CV (CPCV)**：所有 train/test 组合枚举，估计策略 PnL 分布
- **本项目实操**：CPCV 在工业界落地少，**优先 walk-forward + bootstrap**，CPCV 作为 reference

### Bootstrap 估计 Sharpe 置信区间

单点 Sharpe 估计不够，需置信区间：

- **Block bootstrap**：按时间块（如 21 天）重采样，保留 serial correlation
- **Stationary bootstrap**：随机块长（指数分布）
- **典型流程**：
  - 1000 次 bootstrap → 1000 个 Sharpe → 取 5% / 95% 分位
  - 报告：`Sharpe = 1.2, 95% CI [0.8, 1.6]`
  - **CI 下界 < 0** = 不显著

### 调参的反模式

- **在测试集上调参** → 测试集失效
- **多次跑测试集挑最好的** → multi-comparison 偏差（→ 第 9 节多重检验）
- **超参与训练集大小耦合** → 扩样本时超参失效
- **用未来 fold 的统计量做 normalization** → 经典前瞻泄漏（→ 第 6 节）

### 实施 checklist

- [ ] 严格三段式分割，测试集只跑 1 次
- [ ] Walk-forward 而非随机 CV
- [ ] 训练 / 验证之间 purge + embargo
- [ ] Bootstrap 报告 Sharpe 置信区间
- [ ] 超参选择记录次数（→ 第 9 节 Deflated Sharpe）
- [ ] 测试集结果与验证集结果对比，差距 > 50% 触发警报

### 本项目应用

> 🚨 **当前 IS/OOS 2021 切分**：
> - IS = 2015-2020，OOS = 2021-2025
> - 验证集与训练集混合，调参时实际在 IS 上 fit
> - **改进方案**：IS 拆分为 Train (2015-2018) + Val (2019-2020)，OOS (2021-2023) 调超参，Test (2024-2025) 只跑 1 次
> - **当前 OOS 2021-2025** 已经被多次"看过"（每次因子调整都在 OOS 上验证），实质上是验证集

---

## 18. 常见数据陷阱（A 股）

### 交易制度

- **T+1 限制**：当日买入次日才能卖出。信号 t 日生成 → t+1 买入 → 最早 t+2 卖出。日内策略不可行
- **涨跌停**：主板 ±10%、ST/*ST ±5%、创业板/科创板/北交所 ±20%、新股上市前 5 日不限
  - **触板封单不能成交**：回测必须剔除当日成交记录（不是按收盘价成交）
  - **一字板**：开盘即触板，全天不可买入；连板股票流动性极差
  - **跌停换手**：跌停封单内的成交可作为退出窗口（按比例）
  - **集合竞价**：9:15-9:25 开盘竞价、14:57-15:00 收盘竞价，撮合规则与连续竞价不同
- **印花税不对称**：买入 0、卖出 0.05%（2023-08 减半，原 0.1%）
- **过户费 + 经手费 + 证管费**：合计 ~1bp，对冲基金可忽略，retail 显著
- **板块涨停限制**：极端市况下交易所可临时全板块停牌（如 2015 熔断 / 2020 疫情）

### 融券 / 做空

- **融券标的池受限**：仅特定股票可融券，沪深合计约 1500-2000 只（动态调整），费率 8-12%/年（远高于美股）
- **券池流动性差**：申报当日可能无券，机构客户排队
- **转融通**：公募基金借出股票给券商再借给空头，2024 年起监管收紧
- **裸卖空禁止**：A 股严禁 naked short
- **空头策略 universe 受严重限制**：导致行业内多空在某些行业不可行（券池覆盖不足）

### 上市制度

- **注册制（科创板 2019 / 创业板 2020 / 主板 2023）vs 核准制**：
  - 注册制新股发行价市场化，新股价值发现期长（30-50 个交易日波动剧烈）
  - 核准制时代新股恒涨停（"打新无风险"），注册制下破发常见
  - 历史回测分段：2019 前用核准制规则，2019/2020 后逐步切换
- **次新股**：上市 < 252 个交易日波动巨大，因子失效，建议剔除
- **北交所**（2021-）：流动性极差，单股日成交可 < 100 万，量化策略基本不可用，universe 应剔除
- **退市新规（2020/2024）**：财务/交易/规范/重大违法四类强制退市，每年数十家，必须包含在历史 universe 防 survivorship

### 跨市场数据

- **沪深港通**：北向额度 520 亿/日 + 港股通 420 亿/日；节假日错配（A 股开市港股休市需对齐）
- **AH 股价差**：同公司 A/H 双重上市，A 股普遍溢价 30-100%（折溢价指数 AH Premium）
- **中概股 / ADR**：阿里 / 京东 / 拼多多在美 + 港双重上市，universe 去重规则要明确

### 财务数据特殊性

- **披露窗口**：年报 4-30 截止 / 半年报 8-30 / 季报 4-30、8-30、10-30；披露日**不均匀分布**，最后一周扎堆
- **业绩预告（强制）**：净利润预计同比 ±50% 或亏损必须发预告，比正式披露早 15-30 天，是另一个 PIT 数据源（Tushare `forecast_vip`）
- **业绩快报**：年报前 1-2 个月发，比正式披露早；可以信号化但偶有修正
- **审计意见**：标准无保留 / 保留 / 否定 / 无法表示意见，非标意见显著负 alpha 但披露日才可知
- **行业分类 schema**：申万一级（28 个）/ 中信一级（30 个）/ 证监会（19 个），不同 schema 同一公司可能落不同行业
  - **申万 PIT 问题**：申万指数公司 2014/2021 重大调整，历史回测要用 PIT 申万分类
- **股本变动**：增发 / 配股 / 转增 / 回购，前复权价已处理，但筹码层面信号要单独跟踪

### 复权

- **前复权（默认）**：用今天的股本/股价回算历史，回测推荐
- **后复权**：以上市日为基准，便于看真实涨幅
- **不复权**：原始价格，仅展示用，**不可用于因子计算**

### 风险提示

- **政策事件驱动**：A 股政策敏感性远高于美股（货币、监管、产业），系统性 regime shift 频繁
- **机构占比低**：散户占比 60%+（美股 < 10%），微观结构 / 行为偏差因子在 A 股更有效

---

## 使用方式

涉及以下任务时，**回到本文档逐条检查**：
- 写新因子 → 第 3、4、6、7、9、10 节
- 跑回测 → 第 1、4、5、6、7、10、13、16、17 节
- 解读结果 → 第 2、8、9、10、11 节
- 写优化器 → 第 1、12、13、14、15 节
- 设计 universe → 第 1、5、7、18 节
- 加空头端 → 第 8、10、11、12 节
- 处理跨市场 / 跨频率数据 → 第 7 节
- 检查 crowding → 第 10 节
- 拆 P&L 来源 → 第 11 节
- 设计多空对冲 → 第 12 节
- 设计组合约束 → 第 15 节
- 设计 OOS 验证 / walk-forward → 第 17 节

**不要凭"金融直觉"写代码。每次都对照 checklist。**
