# 美股量化策略文档

本文档说明美股量化系统的完整算法逻辑。系统包含 **Python (Django MVT)** 和 **Rust (quant-engine)** 双栈，共享数据层。

---

## 一、当前 Production Baseline：Rust 引擎 v25（2026-04-30）

**仅 FMP + FRED 数据，14 年回测（2012-2025）：**

```
Total Return    +1293.65%      (Annual 20.76%)
Sharpe Ratio    0.99           ⭐ 机构级
Calmar Ratio    0.71
Max Drawdown    -29.29%
Annual Turnover ~400%

FF5 α           13.28%         ⭐⭐⭐ Harvey-Liu-Zhu |t|>3 真 alpha
FF5 t-stat      3.40
FF5 R²          0.260          (74% 收益不被 FF5 解释)

β_Mkt-RF        0.41
β_HML           -0.33          (anti-value)
β_RMW           -0.22          (anti-quality-profit)
β_CMA           +0.16

Up Capture      73.35%
Down Capture    -8.62%         ⭐⭐ S&P 跌月策略反赚 8.6%（市场中性 alpha）
Capture Ratio   -8.51
Excess vs S&P   +7.98%/年
```

**数据源（仅 2 家）：**
- **FMP Ultimate**（~$300/月）— 主力数据：财务/价格/EPS/insider/dividend
- **FRED**（永久免费）— 12 个宏观指标

不需要：Quiver / Unusual Whales / Fiscal.ai / AlphaVantage（已禁用 5 个对应因子，alpha 仅退 0.25%/年）

**v25 复现命令：**
```bash
cd quant-engine
cargo run --release -p quant-cli -- backtest \
  --start 2012-01-01 --end 2025-12-31 \
  --cache-dir ../cache --output ../output/rust_v25
```

需要 `cache/ff5_daily.csv`（Ken French 5-factor daily, 免费）才能跑 FF5 回归。

### v25 vs v21 对比（去 Quiver 影响）

| 指标 | v21 (76 因子) | **v25 (71 因子)** | Δ |
|------|--------------|------------------|----|
| Sharpe | 1.01 | 0.99 | -0.02 |
| FF5 α | 13.53% (t=3.47) | 13.28% (t=3.40) | -0.25% |
| Excess vs S&P | +8.30% | +7.98% | -0.32% |
| Max DD | -30.65% | **-29.29%** | -1.36 ✓ |
| Calmar | 0.69 | **0.71** | +0.02 ✓ |

**禁用因子（IC 都很弱）：**
- CONGRESS_NET_BUY (ICIR=-0.183)
- GOV_CONTRACT_FLOW (ICIR=-0.346)
- LOBBY_INTENSITY (ICIR=-0.029)
- DARK_POOL_SHORT (ICIR=-0.168)
- INST_OWNERSHIP_DELTA (ICIR=-0.100)

---

## 二、Python 旧引擎绩效（2026-04-30 待重新核对）

| 区间 | FF5 Alpha | t-stat | Sharpe | β_mkt | β_rmw | 超额年化 | 下行捕获 |
|------|-----------|--------|--------|-------|-------|---------|---------|
| 2000-2011（无 analyst 大类） | 6.18% | 2.19 | 0.51 | 0.37 | -0.05 | +12.33% | 0.30 |
| 2012-2023（完整 31 因子） | 6.58% | 2.20 | 0.63 | 0.44 | -0.21 | +0.89% | 0.44 |

> ⚠️ **Python 引擎也含 3 个未修 bug**：stop_cover 不限 cash / 候选集累积 / initial_capital=$100K。需用修复版 Python 引擎重跑核对。Rust 引擎已修完 10 个 bug，是当前可信 baseline。

---

## 三、系统概述（Rust 单栈）

> **2026-04-30 重大变更**：Python (Django) + 前端 (React) 全部归档至 `legacy_python/`。
> 生产策略 100% 在 Rust `quant-engine/` 中。

```
FMP / FRED → quant-download → PostgreSQL → parquet cache
                                              ↓
                            quant-data (DataCache 内存加载)
                                              ↓
                            quant-factors (71 因子)
                                              ↓
                            quant-strategy (scoring + MVO + regime)
                                              ↓
                            quant-backtest (T+0 引擎 + FF5 回归)
                                              ↓
                            output/*.csv (NAV / signals / α)
```

历史 Python 三套策略（Alpha/Beta/Baseline）已合并为 Rust 单一 v25 实现。Rust v25 ≈ Python Alpha 修了 10 bug + winner-signature 调权 + Layer 1 sum scoring + 杠杆 1.2。

---

## 四、数据源

> **2026-04-30 精简**：仅 FMP + FRED 两家，去掉 Quiver / UW / Fiscal.ai / AlphaVantage（节约 ~$600+/年，alpha 仅退 0.25%/yr）。详见 [DATA_SOURCES.md](DATA_SOURCES.md)。

### 4.1 FMP Ultimate — 主力（付费 ~$300/月）

涵盖：股票列表 / 日线 OHLCV / 季度财报全字段 / key metrics / 财务增长 / 分析师评级 / EPS estimates / earnings surprise / insider trading / 分红 / GICS 行业 / 指数日线 / 宏观（部分）/ ESG / DCF / shares float / employee count / revenue segments。

CLI：`quant download --source fmp --target {stock_list|daily_price|financial|...|all}`，30 并发，2500 req/min。

### 4.2 FRED — 宏观（永久免费）

12 个 series：NFCI, HY OAS, IG OAS, 短端利率, 通胀预期, 失业率等。

CLI：`quant download --source fred --target all --start-year 2000`

### 4.3 Fama-French 5 因子（永久免费）

Ken French Data Library。一次性 wget + 清洗到 `cache/ff5_daily.csv`。
backtest 自动检测此文件存在则跑 α / β_HML / β_RMW / β_CMA 回归。
详见 [DATA_SOURCES.md](DATA_SOURCES.md) §五。

### 4.4 已废弃数据源（v25 不依赖）

| 数据源 | 月费 | 废弃因子 |
|--------|------|---------|
| Quiver | ~$50 | CONGRESS_NET_BUY / GOV_CONTRACT_FLOW / LOBBY_INTENSITY / DARK_POOL_SHORT / INST_OWNERSHIP_DELTA |
| Unusual Whales | ~$50-100 | 期权流 / dark pool（替代品已废） |
| Fiscal.ai | 不详 | daily_ratio 表从未导入 |
| AlphaVantage | $0-250 | NEWS_SENTIMENT / IV_SKEW / PUT_CALL_RATIO（数据从未积累） |

---

## 五、因子体系（Rust v25：71 因子）

> v25 = v21 减 5 个 Quiver 付费因子。当前在 `quant-engine/crates/factors/src/` 下，按大类分目录，每个 `.rs` 文件加 `us_` 前缀，对应 A 股的 `factors/src/a_share/`。

### Rust v25 Tier 权重（`strategy/src/rolling_ic.rs::icir_tier_weight`）

| Tier | 权重 | 因子 |
|------|------|------|
| **T0 super-strong** | **3.0** | MOM_12M, TSMOM, INDUSTRY_MOM, SUE_PEAD, EARNINGS_SURPRISE |
| T1 strong (\|ICIR\|≥0.3) | 2.0 | FREE_FLOAT_PCT, TURN_20D, PIOTROSKI_F, EV_TO_FCF, AMIHUD_ILLIQ, COMPOSITE_EQUITY_ISSUANCE, ROE_TTM, REVENUE_YOY |
| T2 default | 1.0 | 其他 |
| T3 weak | 0.5 | PRICE_52W_HIGH, VOLUME_RATIO 等 |
| Direction-flip | 0.3 | BP, INTANGIBLE_ADJ_BP, RD_INTENSITY, MOM_1M 等 |
| Noise | 0.0 | GEO_CONCENTRATION |
| **DISABLED** | — | NET_PROFIT_YOY (winner audit 显示 32% ≈ Losers 33% 不区分), EPS_REVISION (FMP date 是 forecast period 不是 publication date，misnamed), 5 个 Quiver 付费因子 (CONGRESS_NET_BUY / GOV_CONTRACT_FLOW / LOBBY_INTENSITY / DARK_POOL_SHORT / INST_OWNERSHIP_DELTA, 数据源已退订) |

### Rust 引擎 Category 权重（`config.toml`）

```toml
[category_weights]
value = 0.7        # 降权 (winner signature 显示反 value)
quality = 1.5
growth = 1.5       # 1.2 → 1.5 (winner audit)
momentum = 2.5     # 2.0 → 2.5 (winner audit, MOM_12M 是 #1 信号)
technical = 1.0
macro = 1.0
analyst = 1.2
sentiment = 1.0
```

### 已测试且被拒的方向（避免重复试）

| 测试 | 结果 |
|------|------|
| EV_TO_FCF 进 T0 (3.0) | α 退 0.22%/yr |
| PIOTROSKI_F 进 T0 (3.0) | α 退 1.70%/yr（过度防守） |
| AMIHUD_ILLIQ 进 T0 | 无效（universe min_dvol=$10M 已过滤） |
| Momentum 不 neutralize | α 退 0.55%/yr |
| ANALYST_DISP/QMJ_LEVERAGE 翻方向 | α 退 0.71%/yr |
| gross_leverage 1.5 | α 升但 Sharpe 跌 |
| max_long_weight 0.08 | α 退 1.2%（过分散） |
| max_long_weight 0.12 | α 退 0.5%（过集中） |

### 已归档的 Python 旧版因子体系（仅供参考）

> Python 因子代码已归档至 `legacy_python/stocks/services/factors/`，不再维护。
> 下面 Python-era 31 因子定义保留作算法描述参考；真实实现以 Rust `quant-engine/crates/factors/src/` 为准。

### 设计原则

- 两层打分：类内因子加权平均 → 类间大类加权求和
- 动态分母：缺失因子不补零，按有效因子等比缩减权重
- 防前视偏差（全因子已审计通过，零前视偏差）：
  - 财务数据：按 `filing_date <= date` 过滤（公告日，非报告期末），异常 filing_date 自动 +45 天缓冲
  - 价格数据：`trade_date <= date`，rolling 统计量一次性预计算
  - 分析师/情绪/另类数据：trailing lookback 窗口（14~365 天），不取未来数据
  - 宏观因子：30 天 lag，防止使用尚未发布的经济数据
  - EPS Revision：取 forward 一致预期（分析师当前共识），非实际未来盈利

### 因子清单（31 因子 × 7 大类）

#### Value 大类（4 因子）

| 因子 | 代码 | 计算公式 | 数据源 | 回看窗口 | 行业内 |ICIR| |
|------|------|---------|--------|---------|------------|
| **EP** | `value.EP` | TTM EPS / adj_close | FMP IS (季度) | 4Q TTM | 0.09 弱 |
| **BP** | `value.BP` | total_equity / market_cap | FMP BS (季度) | 最新季度 | **0.41** |
| **DIV_YIELD** | `value.DivYield` | 近12M 股息总额 / adj_close | FMP 分红数据 | 365 天 | 0.16 有效 |
| **BUYBACK_YIELD** | `accruals.BuybackYield` | 近4Q 回购金额 / market_cap | FMP CF (季度) | 4Q TTM | 0.23 有效 |

EP = Earnings-to-Price，传统盈利收益率。BP = Book-to-Price，账面价值比。两者在 2012-2023 截面 IC 为负（成长 > 价值时代），由滚动 IC 自动反转。DIV_YIELD 为股息率，BUYBACK_YIELD 为回购收益率（share_repurchased / market_cap）。

#### Quality 大类（15 因子，已迁移到 AlphaSignal 架构）

**架构：** 全部在 `stocks/services/factors/signals/quality/` 下，继承 `AlphaSignal` 基类，通过 `@register` 装饰器自动注册。每个因子携带元数据：`version / horizon / expected_icir / status / inherent_direction / ic_window_months / data_deps`。方向由 `inherent_direction` 锁定（+1=永不反转，-1=固有反向，0=滚动 IC 决定）。

**Legacy 5 个**（旧因子搬进 `signals/quality/legacy.py`，行为不变只加元数据）：

| 因子 | 类 | 计算公式 | 方向 | 回看 | 行业内 |ICIR| |
|------|----|--------|------|------|------------|
| **ROE_TTM** | `legacy.RoeTTM` | net_income / total_equity（最新季度）| +1 | 1Q | 0.07 弱 |
| **GROSS_MARGIN** | `legacy.GrossMargin` | gross_profit / revenue（最新季度）| +1 | 1Q | 0.09 弱 |
| **PROFIT_STB** | `legacy.ProfitStability` | -CV(近 8Q 净利润 YoY 增速) | +1 | 8Q | 0.12 弱 |
| **MARGIN_TREND** | `legacy.MarginTrend` | 最新 gross_margin - 上季度 gross_margin | +1 | 2Q | 0.14 弱 |
| **ACCRUALS** | `legacy.Accruals` | -(net_income - FCF) / total_assets | +1 | 4Q TTM | **0.17 有效** |

**新增 10 个**（Quality 补强）：

| 因子 | 类 | 计算公式 | 方向 | 回看 | 数据源 | 学术依据 |
|------|----|--------|------|------|--------|---------|
| **PIOTROSKI_F** | `piotroski.PiotroskiF` | 9 项财务体检 binary signals 求和（0-9） | +1 | 5Q | us_financial_data | Piotroski 2000 JAR |
| **ALTMAN_Z** | `altman.AltmanZ` | 1.2·WC/TA + 1.4·RE/TA + 3.3·EBIT/TA + 0.6·MV/TL + 1.0·S/TA | +1 | 1Q | us_financial_data + us_enterprise_value | Altman 1968 JF |
| **OHLSON_O** | `ohlson.OhlsonO` | Ohlson 9 输入 logit 公式（破产概率） | **-1** | 2Q | us_financial_data | Ohlson 1980 JAR |
| **BENEISH_M** | `beneish.BeneishM` | 8 ratios（DSRI/GMI/AQI/SGI/DEPI/SGAI/LVGI/TATA）线性组合 | **-1** | 5Q | us_financial_data | Beneish 1999 FAJ |
| **QMJ_LEVERAGE** | `qmj_safety.QmjLeverage` | total_debt / total_stockholders_equity | **-1** | 1Q | us_financial_data | AQR 2019 RFS |
| **QMJ_EARNINGS_VOL** | `qmj_safety.QmjEarningsVol` | std(近 20Q net_income) / \|mean\| | **-1** | 20Q | us_financial_data | AQR 2019 |
| **QMJ_ROE_VOL** | `qmj_safety.QmjRoeVol` | std(近 20Q ROE) | **-1** | 20Q | us_financial_data | AQR 2019 |
| **QMJ_NET_PAYOUT** | `qmj_payout.QmjNetPayout` | (TTM 分红 + 回购 − 发行) / market_cap | +1 | 4Q TTM | us_financial_data + us_enterprise_value | AQR 2019 |
| **CASH_CONV_CYCLE** | `ccc.CashConversionCycle` | DSO + DIO − DPO | **-1** | 1Q | us_key_metric | — |
| **EARNINGS_PERSISTENCE** | `persistence.EarningsPersistence` | AR(1) 系数 of 近 8Q EPS | +1 | 8Q | us_financial_data | Sloan 1996 |

**注：** 数据存取方式——新 Quality 因子直接查 Django ORM（`AlphaSignal.fetch_financial_latest / fetch_financial_history / fetch_key_metric_latest / pick_year_ago`），不经过 `preload_for_backtest` 的 parquet 缓存（后续批次再统一引入缓存层）。

2 个月验证（2024-11-01 → 2024-12-31）覆盖率：PIOTROSKI_F 70%、ALTMAN_Z 63%、OHLSON_O 66%、BENEISH_M 50%、QMJ 系列 63-65%、CASH_CONV_CYCLE 68%、EARNINGS_PERSISTENCE 64%。极值会在 `processor.winsorize_mad(n=5)` 环节自动剪掉。验证报告在 `output/signal_validation/*.md`。

#### Growth 大类（3 因子）

| 因子 | 代码 | 计算公式 | 数据源 | 回看窗口 | 行业内 |ICIR| |
|------|------|---------|--------|---------|------------|
| **NET_PROFIT_YOY** | `growth.NetProfitYoY` | 最近季度净利润 / 同期去年净利润 - 1 | FMP IS (季度) | 对比 Q-4 | 0.05 无 |
| **REVENUE_YOY** | `growth.RevenueYoY` | 最近季度收入 / 同期去年收入 - 1 | FMP IS (季度) | 对比 Q-4 | 0.10 弱 |
| **NET_PROFIT_CAGR_3Y** | `growth.NetProfitCAGR3Y` | (最近净利润 / 3年前净利润)^(1/3) - 1 | FMP IS (季度) | 12Q | 0.10 弱 |

成长因子衡量盈利和收入的历史增速。NET_PROFIT_YOY 行业内几乎无效（|ICIR|=0.05），信号主要来自行业间差异。

#### Momentum 大类（10 因子，2026-04-15 新增 6 个 AlphaSignal 进阶因子）

**Legacy 4 个**（旧架构，待迁移）：

| 因子 | 代码 | 计算公式 | 数据源 | 回看窗口 | 行业内 |ICIR| |
|------|------|---------|--------|---------|------------|
| **MOM_1M** | `momentum.Mom1M` | 近1个月收益率 | 日线价格 | 20 交易日 | 0.13 弱 |
| **MOM_3M** | `momentum.Mom3M` | 近3个月收益率 | 日线价格 | 60 交易日 | 0.07 弱 |
| **MOM_12M** | `momentum.Mom12M` | 近12个月收益率（跳过最近1月） | 月末价格 | 12M | 0.11 弱 |
| **REV_5D** | `momentum.Rev5D` | 近5日累计收益率（短期反转） | 日线价格 | 5 交易日 | **0.15 有效** |

**新增 6 个**（AlphaSignal 架构，`signals/momentum/us_*.py`）：

| 因子 | 类 | 计算公式 | 方向 | 回看 | 数据源 | 学术依据 |
|------|----|--------|------|------|--------|---------|
| **PRICE_52W_HIGH** | `us_high_52w.Price52WHigh` | 当前 adj_close / 过去 252 交易日 max(adj_close) | +1 | 380 天 | us_daily_price | George-Hwang 2004 JF |
| **RESIDUAL_MOM_FF3** | `us_residual_mom.ResidualMomFF3` | OLS: r_ex = α + β₁·MktRF + β₂·SMB + β₃·HML + ε，取 ε 累加（skip 最近 1 月） | +1 | 430 天 + FF5 缓存 | us_daily_price + ff5_daily.csv | Blitz-Huij-Martens 2011 JEF |
| **SUE_PEAD** | `us_sue_pead.SuePead` | (eps_actual − eps_estimated) / std(过去 8Q surprise)，仅在财报后 60 天内 | +1 | 8Q | us_earnings_surprise | Foster-Olsen-Shevlin 1984 + Bernard-Thomas 1989 |
| **INDUSTRY_MOM** | `us_industry_mom.IndustryMomentum` | 个股 12M 收益 − 所属 industry 中位数 12M 收益 | +1 | 380 天 | us_daily_price + us_industry_class | Moskowitz-Grinblatt 1999 JF |
| **FROG_IN_PAN** | `us_frog_in_pan.FrogInPan` | sign(R₁₂ₘ) × (%neg_days − %pos_days) × \|R₁₂ₘ\| | +1 | 380 天 | us_daily_price | Da-Gurun-Warachka 2014 RFS |
| **TSMOM** | `us_tsmom.Tsmom` | 12M 累计收益（方向由滚动 IC 决定） | **0** | 380 天 | us_daily_price | Moskowitz-Ooi-Pedersen 2012 JFE |

**关键设计要点：**
- **FF5 数据**：通过 `AlphaSignal.fetch_ff5_factors()` 从 `output/ff5_data/ff5_daily.csv` 加载（Kenneth French Data Library，1963-2026 日频，已缓存）
- **PEAD 事件窗口**：SUE_PEAD 只在最近一次财报公布后 60 天内给信号，超出窗口返 NaN（避免跨季混淆）
- **TSMOM 方向不锁**：与其他动量因子不同，TSMOM 在不同 Regime 可能反转（牛市趋势 vs 熊市反转），让滚动 IC 自动决定
- **数据存取**：全部 ORM 直查（无 preload/parquet 缓存），后续"统一缓存"批次再统一接入

**2 个月样本**（2024-12-31 截面，5 mega-cap）冒烟通过：
- PRICE_52W_HIGH：AAPL 0.97（接近高点）/ TSLA 0.84（远离）✓
- TSMOM：NVDA +168% / TSLA +60% / AAPL +28%（对得上 2024 实际涨幅）✓
- FROG_IN_PAN：5 票全负（mega-cap 都是"突破型"而非小步累积）✓
- SUE_PEAD：仅 NVDA 在 60 天 PEAD 窗口内
- RESIDUAL_MOM_FF3：NVDA +0.09（剔 FF3 后仍正残差）/ AAPL -0.06（被大盘解释完）✓

完整 2 个月全 universe 验证暂搁置（速度问题等"统一缓存"批次解决）。

#### Technical 大类（6 因子）

| 因子 | 代码 | 计算公式 | 数据源 | 回看窗口 | 行业内 |ICIR| |
|------|------|---------|--------|---------|------------|
| **TURN_20D** | `technical.Turn20D` | 近20日平均美元成交额 | 日线价格 | 20 交易日 | 0.06 弱 |
| **VOL_20D** | `technical.Vol20D` | 近20日收益率标准差 | 日线价格 | 20 交易日 | 0.09 弱 |
| **IVOL** | `technical.Ivol` | 对 S&P500 回归残差的标准差（取反） | 日线 + ^GSPC | 60 交易日 | 0.07 弱 |
| **SIZE** | `technical.Size` | log(market_cap) | us_stock_basic | 截面 | **0.21 有效** |
| **IV_SKEW** | `alphavantage.IvSkew` | ATM put IV - call IV 均值 | Alpha Vantage | 5 交易日 | 0.00 无数据 |
| **PUT_CALL_RATIO** | `alphavantage.PutCallRatio` | 看跌/看涨成交量比均值 | Alpha Vantage | 5 交易日 | 0.00 无数据 |

TURN_20D/VOL_20D/IVOL 为固有反转因子（高值=负信号，权重 -1.0）。IVOL 使用向量化 OLS 回归（numpy 矩阵运算，0.13s/3800 只）。IV_SKEW/PUT_CALL_RATIO 需每日增量采集积累数据。

#### Analyst 大类（5 因子）

| 因子 | 代码 | 计算公式 | 数据源 | 回看窗口 | 行业内 |ICIR| |
|------|------|---------|--------|---------|------------|
| **US_ANALYST_RATING** | `analyst.USAnalystRating` | 近120天分析师评级均值（5=Strong Buy → 1=Sell） | FMP v3/grade | 120 天 | **0.15 有效** |
| **US_ANALYST_COVERAGE** | `analyst.USAnalystCoverage` | log(1 + 近120天不同券商数) | FMP v3/grade | 120 天 | 0.09 弱 |
| **EARNINGS_SURPRISE** | `earnings.EarningsSurprise` | 近120天 (actual - estimated) / |estimated| 均值 | FMP bulk | 120 天 | 0.14 弱 |
| **EPS_REVISION** | `earnings.EpsRevision` | forward EPS 一致预期变化方向 | FMP analyst-estimates | 当前 vs 前期 | **0.43 最强** |
| **INSIDER_NET_BUY** | `insider.InsiderNetBuy` | 近90天内部人净买入金额 / market_cap | FMP v4/insider (filing_date) | 90 天 | 0.10 弱 |

**EPS_REVISION 是整个因子体系中行业内选股力最强的因子**（所有行业 ICIR > 0.29）。它衡量的是分析师对未来 EPS 预期的修正方向——被上调的股票系统性跑赢被下调的。INSIDER_NET_BUY 使用 SEC Form 4 的 filing_date（而非 transaction_date）过滤，防止前视偏差。

#### Sentiment 大类（4 因子）

| 因子 | 代码 | 计算公式 | 数据源 | 回看窗口 | 行业内 |ICIR| |
|------|------|---------|--------|---------|------------|
| **POLYMARKET_SENT** | `polymarket.PolymarketSent` | Polymarket 事件影响加权情绪（时间衰减） | Polymarket alerts | 14 天 | 无（2020+才有数据） |
| **LOBBY_INTENSITY** | `quiver.LobbyIntensity` | 近12月游说支出 / market_cap | Quiver API | 365 天 | 0.16 有效 |
| **GOV_CONTRACT** | `quiver.GovContract` | 近4季度政府合同金额 / TTM revenue | Quiver API | 4Q | 0.07 弱 |
| **NEWS_SENTIMENT** | `alphavantage.NewsSentiment` | 近14天 AI 新闻情绪加权均值 | Alpha Vantage | 14 天 | 0.00 无数据 |

LOBBY_INTENSITY 在 Technology 行业内 |ICIR|=0.36（显著），可能反映监管风险/政策敏感度。NEWS_SENTIMENT/POLYMARKET_SENT 历史数据不足，2020 年前因动态分母自动剥离。

**大类等权，因子内 ±1.0 等权。** 因子方向由 trailing 36M 滚动 IC 自动决定（不硬编码）。

**方向决策规则：**
- 固有反转（TURN_20D/VOL_20D/IVOL）：始终 -1.0，因子定义上高值=负信号
- 质量因子（ROE_TTM/GROSS_MARGIN/PROFIT_STB/MARGIN_TREND/ACCRUALS）：始终 +1.0，永不反转（做空优质资产长期自杀）
- 其他因子：每月调仓时计算过去 36 个月截面 Rank IC 均值，< -0.01 则反转，否则正向
- 冷启动期（前 12 个月无 IC 数据）：默认 +1.0

**替代数据时间覆盖说明：** POLYMARKET_SENT（2020+）、LOBBY_INTENSITY（1999+）、GOV_CONTRACT（2008+）、NEWS_SENTIMENT（2024+ 才有足够截面覆盖）、IV_SKEW/PUT_CALL_RATIO（仅当日快照）在早期回测中因动态分母机制自动剥离。2020 年以前的回测实质上由传统财务/量价因子驱动。

### 因子方向机制

**固有反转（硬编码 -1.0，因子定义决定）：**

| 因子 | 原大类 | 理由 |
|------|--------|------|
| TURN_20D | technical | 高换手 = 投机性强 = 负信号 |
| VOL_20D | technical | 高波动 = 风险溢价为负 |
| IVOL | technical | 特质波动率异象（低 IVOL 跑赢） |

**质量保护（锁定 +1.0，永不反转）：**

| 因子 | 原大类 | 理由 |
|------|--------|------|
| ROE_TTM, GROSS_MARGIN, PROFIT_STB, MARGIN_TREND, ACCRUALS | quality | 做空优质资产长期自杀，即使短期 IC 为负也不反转 |

**动态方向（分因子滚动 IC 窗口，每月调仓时自动决定）：**

| 因子类型 | 窗口 | 因子 |
|---------|------|------|
| 基本面 | 24-36M | EP, BP, DIV_YIELD, BUYBACK_YIELD, NET_PROFIT_YOY, REVENUE_YOY, NET_PROFIT_CAGR_3Y |
| 动量/技术 | 6-12M | MOM_1M(6), MOM_3M(9), MOM_12M(12), REV_5D(6), PRICE_DEV_60D(9), SIZE(24) |
| 分析师/盈利 | 12-18M | ANALYST_RATING(18), EPS_REVISION(12), EARNINGS_SURPRISE(18), INSIDER(12) |
| 情绪/另类 | 6M | NEWS_SENTIMENT, IV_SKEW, PUT_CALL_RATIO, POLYMARKET_SENT |

冷启动期（观测数 < 窗口的 1/3）默认 +1.0。

**IC 参考值（2012-2025 全样本，仅供参考，不用于硬编码决策）：**

| 因子 | IC Mean | ICIR | 全样本方向 | 说明 |
|------|---------|------|-----------|------|
| BP | -0.0620 | -0.69 | 负 | 成长 > 价值时期，滚动 IC 会自动捕捉 |
| SIZE | -0.0438 | -0.61 | 负 | 大盘溢价时期，未来可能反转 |
| DIV_YIELD | -0.0459 | -0.32 | 负 | 高息股跑输 |
| BUYBACK_YIELD | -0.0265 | -0.26 | 负 | 回购无效 |
| LOBBY_INTENSITY | -0.0349 | -0.47 | 负 | 理论支撑不足 |

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

`services/us_factors/processor.py`

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

## 六、多空选股策略

代码：`services/strategy/us_multi_factor.py`（USMultiFactorStrategy）

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

**组合构建（默认 MVO 优化器，`US_USE_OPTIMIZER=1`）：**

因子合成得分 μ̂ + Ledoit-Wolf 协方差矩阵 Σ → cvxpy Mean-Variance 优化：

```
max  μ̂'w − λ·w'Σw − γ·||w − w_prev||₁
s.t. Σ|w_i| ≤ 1.0        (总杠杆)
     Σw_i = 0.6           (净敞口)
     -0.05 ≤ w_i ≤ 0.15   (单股上下限)
     sector_gross ≤ 0.25   (行业 gross 上限)
```

- λ=`US_RISK_AVERSION`(1.0)：风险厌恶系数，控制集中度
- γ=`US_TURNOVER_PENALTY`(0.005)：换手惩罚，降低换手率
- 协方差：`USRiskModel`（`backtest/services/us_risk_model.py`），252D 日收益 + `sklearn.covariance.LedoitWolf`，最少 120 天历史，parquet 缓存
- 求解器：OSQP（首选）→ SCS（fallback）→ Top-N + Softmax（终极 fallback）
- 关键配置：`US_MAX_LONG_WEIGHT`(0.15)、`US_MAX_SHORT_WEIGHT`(0.05)、`US_MAX_SECTOR_GROSS`(0.25)

**Top-N + Softmax fallback（`US_USE_OPTIMIZER=0`）：**

得分 ≥ `US_MIN_SELECT_SCORE`(0.0) 的 Top-N 股票。
- 固定 `US_LONG_N=15`（不随 Regime 变化）。
- Softmax 权重分配，`tau=1.5`。

**空头（Short v5）：** 独立因子模型，不复用多头综合得分，**始终开启**。
- **独立 short_score**（4 因子 + 融券成本负向因子）：
  - EPS_REVISION（40%）：分析师下调
  - ACCRUALS（25%）：盈利质量差
  - EARNINGS_SURPRISE（20%）：财报 miss
  - INSIDER_NET_BUY（15%）：内部人卖出
  - BORROW_COST（-10%）：融券成本惩罚
- **INTERSECTION 选股**：short_score 前 30% ∩ EPS_REVISION worst 20%
- **候选池约束**：市值 ≥ $10B，分级借券费率（$50B+ → 0.3%，$10-50B → 1.5%）
- 等权，≤8 只（`US_SHORT_N`）
- **止损**：单只空头浮亏 ≥ 15% 逐日强制平仓
- **Regime 渐进调节**（非二元开关）：
  - 空头数量：strength=1.0→5 只，strength=0.0→10 只（`US_SHORT_N * (0.6 + 0.7*(1-s))`）
  - 净敞口：strength=1.0→60%，strength=0.0→20%（线性插值）
  - 始终有空头保护，无二元 gate（gate 回测验证失败：下行捕获 0.39→0.61）

**净敞口：** Regime 渐进（`US_NET_EXPOSURE=0.6`）
- 满牛（strength=1.0）：多头 80%，空头 -20%，净 60%
- 中性（strength=0.5）：多头 70%，空头 -30%，净 40%
- 满熊（strength=0.0）：多头 60%，空头 -40%，净 20%

**ML Blend（默认关闭）：** LightGBM 滚动训练代码已集成（`us_ml_scorer.py`），但回测验证 ML blend 严重拖累 alpha（开启 α=8.04%，关闭 α=15.63%，吃掉 7.6%）。原因：ML 在有限样本上学到的非线性关系是噪音，稀释了线性因子信号。`US_ML_SCORING_ENABLED` 默认 0，待 ML 模型优化后重评。

### 4.4 复合 Regime 检测（四维 + Credit Veto）

`services/strategy/us_regime.py`（USRegimeDetector）

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

## 六-B、Beta 策略（Regime 驱动仓位控制）

代码：`services/strategy/us_beta_strategy.py`（USBetaStrategy）

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

## 七、风控

代码：`services/risk/us_risk_manager.py`（USRiskManager）

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

## 六-C、Baseline / Alpha v2 实验策略

代码：`services/strategy/us_baseline_strategy.py`（USBaselineStrategy）

**用途**：Alpha v2 开发迭代框架。委托 `USMultiFactorStrategy` 进行 29 因子打分+选股，月频调仓。也用于 VQM 基线验证（历史，已完成）。

**当前配置**（Alpha v2 Step 3.5）：
- 因子打分：委托 USMultiFactorStrategy（31 因子 × 7 大类，两层类别评分）
- 选股：USMultiFactorStrategy._select_from_scores（MVO 优化器，fallback Top-15 long + Bottom-10 short Softmax）
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
python3 cli.py backtest --market us --strategy-type baseline --start 2015-01-01 --end 2023-12-31
```

---

## 六、回测引擎

代码：`services/strategy/us_backtest.py`（USBacktestEngine）

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
| 基准 | **S&P 500 (^GSPC)** | CSI 300 |
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

### 7.1 本地模拟盘

代码：`services/execution/us_paper_trader.py`（USPaperTrader）

4 张 DB 表：`us_paper_account`, `us_paper_position`, `us_paper_transaction`, `us_paper_nav`

- T+0 结算，支持做空（负 volume）
- 默认初始资金 $100,000
- `sync_position(target_weights)` 自动调仓到目标权重

### 7.2 Alpaca 模拟盘（实盘 API）

代码：`services/execution/alpaca_trader.py`（AlpacaTrader）

通过 Alpaca Markets API 连接真实模拟盘，使用 IEX 实时行情驱动撮合。

**配置**（`.env`）：
```
ALPACA_API_KEY=xxx
ALPACA_SECRET_KEY=xxx
ALPACA_PAPER=true
```

**CLI 命令**：
```bash
python3 cli.py paper status --market alpaca    # 查看账户 + 持仓
python3 cli.py paper trade --market alpaca     # 选股 + 提交订单
python3 cli.py paper reset --market alpaca     # 平仓 + 取消挂单
```

**API 端点**：
| 端点 | 方法 | 说明 |
|------|------|------|
| `/api/us/alpaca/account` | GET | 账户信息 |
| `/api/us/alpaca/positions` | GET | 持仓列表 |
| `/api/us/alpaca/orders` | GET | 订单（?status=open/closed） |
| `/api/us/alpaca/trade` | POST | 选股 + 调仓 |
| `/api/us/alpaca/reconcile` | POST | 对账（目标 vs 实际） |
| `/api/us/alpaca/reset` | POST | 平仓 + 取消挂单 |

**与本地模拟盘的区别**：
- 本地模拟盘用数据库中的历史收盘价模拟成交，Alpaca 用实时行情
- Alpaca 零佣金、不模拟滑点（但有真实 bid/ask spread）
- NAV 快照写入本地 `us_paper_nav`（account_id=-1），便于与本地模拟盘对比

---

## 八、数据库表

14 张美股相关表（`services/data/database.py`）：

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
python3 cli.py backtest --market us --strategy-type alpha  # Alpha 策略回测（默认）
python3 cli.py backtest --market us --strategy-type beta   # Beta 策略回测
python3 cli.py backtest --market us --start 2015-01-01     # 指定起始日期
python3 cli.py select --market us --date 2025-01-15        # 多空选股
python3 cli.py factor calc EP --market us                  # 单因子计算
python3 cli.py factor list --market us                     # 因子列表
python3 cli.py score AAPL --date 2025-01-15                # 单股得分
python3 cli.py paper status --market us                    # 模拟账户
python3 cli.py paper trade --market us                     # 执行交易
python3 cli.py data bulk-import --source fmp --target all --start-year 1995  # FMP 全量导入
python3 cli.py data bulk-import --source uw --target all   # Unusual Whales 全量
python3 cli.py data download --market us --target simfin --old-source  # SimFin 旧源
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
| `US_BENCHMARK_INDEX` | `^GSPC` | 回测基准（S&P 500） |
| `US_REGIME_INDEX` | `^GSPC` | Regime 检测基准（S&P 500） |
| `US_CATEGORY_WEIGHTS` | 全等权 1.0 | 大类权重（等权，不做 IC 引导优化） |
| `FMP_API_KEY` | — | FMP API Key（主数据源） |
| `UW_API_KEY` | — | Unusual Whales API Key |
| `FISCAL_API_KEY` | — | Fiscal.ai API Key |
| `FRED_API_KEY` | — | FRED API Key（宏观补充） |
| `SIMFIN_API_KEY` | — | SimFin API Key（旧源，`--old-source` 用） |

---

## 十二、Polymarket P&L 分析器（独立模块）

代码：`services/polymarket/polymarket_pnl_analyzer.py`

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
| 基准 | CSI 300 | **S&P 500** |
| 幸存者偏差 | 未修正 | **已修正**（含 227 只历史成分股） |
| FF5 回归 | 无 | **有**（Alpha v2: alpha 6.73%，t=2.20） |
| ML 增强 | 默认关闭 | LightGBM 代码已集成，回测验证拖累 alpha 7.6%，待优化后重启 |
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
| **Step 5** | Alpha v3: 31 因子 + 滚动 IC 动态方向 | ✅ IS α=6.66%(t=2.26), 熊市保护强(2022 +27.7%) |

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

### Step 5 结果：Alpha v3（31 因子 + 滚动 IC 动态方向）

**核心改进：**
- 因子扩展 23→31：+EPS_REVISION、EARNINGS_SURPRISE、INSIDER_NET_BUY、LOBBY_INTENSITY、GOV_CONTRACT、NEWS_SENTIMENT、IV_SKEW、PUT_CALL_RATIO
- 因子方向改为 trailing 36M 滚动 IC 动态决定（3 固有反转 + 5 质量保护 + 其余动态）
- 数据修复：季度财报（IS+BS+CF 三表合并）、roe/gross_margin 自动计算、adj_close 回退、IVOL 向量化、Insider filing_date 防前视偏差

**⚠️ 时间线定义（IS/OOS 严格互斥）：**
- **样本内 (IS)**：2012-01-01 ~ 2021-12-31
- **样本外 (OOS)**：2022-01-01 ~ 2026-03-31（含 2022 熊市，更严格）

**Alpha 策略逐年收益（基准 S&P 500）：**

| Year | 策略 | S&P 500 | 超额 | 回撤 | 区间 |
|------|------|---------|------|------|------|
| 2012 | 5.89% | 11.68% | -5.79% | -8.42% | IS |
| 2013 | 23.50% | 26.39% | -2.89% | -3.80% | IS |
| 2014 | 3.76% | 12.39% | -8.63% | -5.44% | IS |
| 2015 | 11.11% | -0.69% | **+11.80%** | -9.13% | IS |
| 2016 | 12.16% | 11.24% | +0.92% | -7.73% | IS |
| 2017 | 24.40% | 18.42% | **+5.98%** | -4.82% | IS |
| 2018 | 15.48% | -7.01% | **+22.49%** | -14.06% | IS |
| 2019 | 0.33% | 28.71% | -28.38% | -14.40% | IS |
| 2020 | 16.04% | 15.29% | +0.75% | -24.41% | IS |
| 2021 | 7.01% | 28.79% | -21.78% | -7.25% | IS |
| 2022 | 7.78% | -19.95% | **+27.73%** | -17.26% | OOS |
| 2023 | 3.36% | 24.73% | -21.37% | -7.84% | OOS |
| 2024 | 48.73% | 24.01% | **+24.72%** | -14.46% | OOS |
| 2025 | 108.25% | 16.65% | **+91.61%** ⚠️ | -32.10% | OOS |
| 2026 | -2.33% | -5.97% | +3.64% | -7.10% | OOS |

| 指标 | IS (2012-2021) | OOS (2022-2026) | 说明 |
|------|---------------|-----------------|------|
| FF5 Alpha | +6.66% (t=2.26**) | 受 2025 异常值影响，不具参考性 | 2025 +108% 扭曲了整体 FF5 回归 |
| Sharpe | 0.64 | ~0.88 | OOS 含 2025 风格红利 |
| β_mkt | 0.44 | ~0.40 | 一致稳定 |
| β_rmw | -0.21 | ~-0.60 ⚠️ | 2025 AI 泡沫期间深度负值 |
| 下行捕获 | 0.40 | ~0.35 | 一致稳定 |

**IS 结论：**
- FF5 Alpha=6.66%(t=2.26) 在 5% 水平显著
- 熊市保护强（2015 +11.8%, 2018 +22.5%），牛市跟不上（2019 -28.4%, 2021 -21.8%）— 半仓 L/S 天然代价
- β_mkt=0.44，真正的市场中性策略

**OOS 结论：**
- 2022 熊市 +27.73%（大幅跑赢），验证下行保护有效
- 2023 牛市 -21.37%（大幅跑输），L/S 半仓天然劣势
- 2025 +108.25% 是 AI 泡沫风格红利（β_rmw=-1.01），不可持续
- 下行捕获 ~0.35 稳定

### Step 5 样本外 β_rmw 解释

**现象**：样本外 β_rmw 深度负值（~-0.9），策略系统性做空高盈利公司，看似与 quality 大类（+1.0）矛盾。

**解释**：这不是 Bug，是 36M 滚动 IC 机制在 2024 年自动捕捉了 AI/科技牛市 Regime。动量因子（MOM_12M）、成长因子（REVENUE_YOY）及被动态反转的估值因子（BP→-1）在 2023-2024 年展现极强的 36M 历史 IC，这些因子的 Z-Score 绝对值远大于质量因子，在两层打分中"淹没"了质量分数（Feature Swamping）。系统因此疯狂买入"无利润但营收暴增、股价起飞、估值极贵"的 AI 概念股——在 FF5 归因中表现为深度负 β_rmw。

**结论**：35%+ 的样本外收益是"特定时代背景下的风格红利"（AI 狂暴牛市），而非万能选股 Alpha。滚动 IC 机制聪明地捕捉到了这个 Regime 并上了车，但在风格极端反转时（如科技泡沫破裂）将面临显著回撤风险。

**⚠️ 风险提示：**
1. **IS/OOS 严格互斥**：IS 2012-2021 / OOS 2022-2026（含 2022 熊市，更严格）
2. **替代数据时效隔离**：NEWS_SENTIMENT 2024 前每年仅数百条，WSB_SENTIMENT 已移除（仅 3 ticker），2020 年前回测实质由传统财务/量价因子驱动
3. **β_rmw 风格偏移风险**：策略在 AI 牛市中自动加大低质量成长股权重，一旦市场回归防御逻辑（高盈利、低估值），空头将被打爆
4. **因子方向完全动态**：不再硬编码反转，由 trailing 36M IC 每月自动调整

### 诚实评估与待办

**核心发现**：
- **行业内选股 alpha 确认存在**（P0 测试 2026-04-03），集中在 EPS_REVISION（行业内 ICIR=0.43，所有行业 >0.29）
- **FF5 Alpha 跨时代一致**：2000-2011 α=6.18%(t=2.19)，2012-2023 α=6.58%(t=2.20)
- **熊市保护是策略核心优势**：2002 +36%、2008 +32%、2022 +17% 超额
- **2025 +108% 是 AI 泡沫风格红利**（β_rmw=-1.01），不是常态表现
- **空头端 v5 重设计**（独立因子模型 + Regime 渐进 + 融券约束 + 止损），替代 v3/v4 的综合得分反面选股

**P0 — 行业内选股验证（已完成 2026-04-03）：**

行业内有选股力的因子（跨行业均 |ICIR| ≥ 0.15）：

| 因子 | 全市场 ICIR | 行业内 |ICIR| | 各行业一致性 |
|------|-----------|------------|------------|
| **EPS_REVISION** | **0.79** | **0.43** | 所有行业 ICIR > 0.29（最强） |
| BP | -0.70 | 0.41 | 行业内也强负 |
| BUYBACK_YIELD | -0.29 | 0.23 | |
| SIZE | -0.52 | 0.21 | |
| ACCRUALS | 0.34 | 0.17 | |
| US_ANALYST_RATING | 0.24 | 0.15 | |
| REV_5D | 0.03 | 0.15 | 全市场弱但行业内有效 |

无行业内选股力（行业内 |ICIR| < 0.05）：NET_PROFIT_YOY、IV_SKEW、PUT_CALL_RATIO、WSB_SENTIMENT、NEWS_SENTIMENT

EPS_REVISION 行业内 ICIR 明细：Basic Materials 0.59、Financial Services 0.75、Technology 0.55、Energy 0.46、Industrials 0.52、Healthcare 0.40、Real Estate 0.40、Communication Services 0.29、Consumer Cyclical 0.34、Consumer Defensive 0.32

**P1 — 策略架构决策（基于 P0 结论）：**
- ✅ 行业内有 alpha → 继续多空框架，聚焦 EPS_REVISION 提权
- Alpha v4 方向：EPS_REVISION 提权 + 行业内 L/S 优化 + β_rmw 约束
- 弱行业内因子（NET_PROFIT_YOY、IVOL、MOM_3M、TURN_20D 等）考虑降权或剪枝

**P0 — 因子优化（最高优先级，预计 2-3 周）：**
- **EPS_REVISION v2**：四维复合（修正方向 × 幅度 × 广度 × 加速度），数据在 FMP analyst-estimates，目标行业内 ICIR 0.43→0.55+
- **ACCRUALS v2**：拆分 working capital accruals（应收/应付/存货）和 long-term accruals（资本支出/收购），分别测 ICIR
- **BP v2**：Tech/Healthcare 做 R&D-adjusted book value（近 5 年研发资本化加回 equity）
- **REV_5D v2**：条件反转——UW 新闻过滤，无新闻加强反转，有新闻减弱
- **ROE_TTM v2**：从水平值改为近 4Q 变化趋势（线性回归斜率）
- 每个优化后用行业内 ICIR 框架验证，不显著则回滚

**P1 — 空头端重设计（v5，2026-04-08 实现）：**

v4 催化剂重建回测失败（UNION + 20 只 + always-on → α 从 6.66% 降至 3.01%），v5 从头重新设计：

- **独立空头因子模型**（不复用多头综合得分）：
  - EPS_REVISION（40%）：分析师下调 → 预期下修链
  - ACCRUALS（25%）：高应计 → 盈利质量差
  - EARNINGS_SURPRISE（20%）：财报 miss → 后续下修
  - INSIDER_NET_BUY（15%）：内部人净卖出
  - BORROW_COST（-10%）：融券成本作为负向因子自然惩罚
- **Regime 渐进调节**：牛市 5 只/净 60%，熊市 10 只/净 20%，始终有空头（无二元 gate）
- **INTERSECTION 选股**：short_score 前 30% ∩ EPS_REVISION worst 20%（双重确认）
- **融券约束**：市值 ≥ $10B，分级借券费率（$50B+ → 0.3%，$10-50B → 1.5%）
- **止损**：单只空头浮亏 ≥ 15% 强制平仓，不等月度调仓
- **等权**：≤8 只（熊市）/ ≤5 只（中性），不用 Softmax
- **对照基准**：个股空头须跑赢"做空 SPX 指数"baseline，否则不值得
- 待回测验证

**数据治理（已完成）：**
- **历史市值数据源**：`us_enterprise_value.market_capitalization`（季度精度，1983-至今，391k 行 / 5,652 ticker）
  - ~~us_key_metric.market_cap~~：FMP key-metrics 端点不返回该字段，列已删除
  - ~~us_historical_market_cap~~：FMP Ultimate plan 只有 ~90 天历史，表已删除
  - `get_market_cap()` 回退链：预加载 enterprise_value → SQL 查 enterprise_value → us_stock_basic 静态快照
- **upsert COALESCE 修复**：Key Metrics + Ratios 共享 us_key_metric 表，已修复多端点覆写 NULL 的 bug（`COALESCE(EXCLUDED.col, table.col)`）
- **FMP 端点字段映射修复**：earnings-surprise 切到 stable/earnings、company-profile 切到 stable/profile、eps-estimate 列名修正
- **Quiver 因子数据到位**：LOBBY_INTENSITY（223k 行 / 1999-2026）、GOV_CONTRACT（36k 行 / 2008-2026）

**P2 — 噪音因子清理（与 P0 并行，2-3 天）：**
- 直接移除：IV_SKEW、PUT_CALL_RATIO、NEWS_SENTIMENT、POLYMARKET_SENT（无数据/无覆盖，等积累 1-2 年后重评）
- 观察降权（0.5）：NET_PROFIT_YOY(ICIR 0.05)、TURN_20D(0.06)、US_ANALYST_COVERAGE(0.09)

**P3 — 信号权重分级（P0 完成后）：**
- ICIR > 0.3 → 权重 2.0，0.15-0.3 → 1.0，< 0.15 → 0.5
- 粗粒度分级（低自由度），IS/OOS 对比验证

**P0.5 — 工业级架构补强 ✅ Tier 1 已完成：**

~~当前 Top-N + Softmax + 行业硬 cap 15% 是启发式~~ → 已替换为 MVO 优化器。

- **Tier 1 ✅ 已完成：**
  - 风险模型：`backtest/services/us_risk_model.py` — 252D 日收益 + Ledoit-Wolf shrinkage → N×N 协方差矩阵 Σ，parquet 缓存
  - MVO 优化器：`backtest/services/us_optimizer.py` — cvxpy + OSQP，目标 `max μ̂'w − λ·w'Σw − γ·||w − w_prev||₁`，约束净敞口/杠杆/单股/行业，求解失败自动降级 Top-N
  - 配置：`US_USE_OPTIMIZER`(开关) / `US_RISK_AVERSION`(λ=1.0) / `US_TURNOVER_PENALTY`(γ=0.005) / `US_MAX_LONG_WEIGHT`(0.15) / `US_MAX_SHORT_WEIGHT`(0.05) / `US_MAX_SECTOR_GROSS`(0.25)
  - 待验证：跑 2012-2026 完整回测对比，期望换手率降 60%+，|β_rmw| < 0.5，Sharpe 不降
- **Tier 2（中期 1 月）：**
  - PCA 统计风险因子（前 20-30 主成分作 Barra 开源替代，B·F·B' + Δ 分解）
  - 多周期信号合成（日/周/月频分层，不同 horizon 不同 decay）
  - Alpha Bayesian shrinkage（θ 由历史 IC 估计，抑制极值持仓）
  - 交易成本/市场冲击模型（线性冲击 cost = spread/2 + κ·|Δw|/ADV，写入优化器）
- **Tier 3（长期）：**
  - Alpha Capture System（因子模块化 + 版本号 + 独立 IC 监控 + 灰度上线）
  - Barra USE5 / Axioma 对标自建模型
  - 真实执行层（VWAP/TWAP/IS 算法回测，IBKR/PB FIX 替换 Alpaca）
  - Barra-style P&L 归因（每日拆 factor return × β + specific return）

**P1 — 因子全面补强（17 大类，~70 个新因子）：**

按 ROI 排序补强当前 31 因子的盲区。完整清单见 memory。

- **T1（现有 FMP 数据立即可做，最高 ROI）：**
  - Quality 完整化：Piotroski F-Score / Beneish M-Score / Altman Z-Score / Ohlson O-Score / QMJ 完整四子项 / Earnings Persistence / CCC
  - Value 进阶：Asset Growth 反向（Cooper 异象）/ Net Operating Assets / Composite Equity Issuance / Intangible-Adjusted B/P / EV/EBIT / EV/FCF / Shareholder Yield
  - Momentum 进阶：Residual Momentum（剔 FF3 残差）/ 52-Week High Proximity / Industry Momentum / SUE/PEAD 漂移 / Frog-in-the-Pan / TSMOM
  - 防御：BAB（Frazzini-Pedersen）/ MAX 因子 / Downside Beta / Coskewness
  - 流动性：Amihud Illiquidity / Pastor-Stambaugh
  - 分析师进阶：Recommendation Δ / Analyst Dispersion / Days Since Earnings / Implied Earnings Growth
  - 空头侧补强：Short Interest + Days-to-Cover / 融券费率 / Hedge Fund Crowding / 13F Δ / Smart Money Index
- **T2（需补数据源，中等成本）：**
  - 期权：IV Skew / Put-Call Ratio / IV Rank / RNS / VRP / Option-Implied Beta
  - 微结构：Realized Vol/Skew/Kurt / OFI / Kyle's λ / VWAP Deviation
  - NLP：FinBERT News Sentiment / 10-K Loughran-McDonald / 10-K YoY 文本相似度 / Earnings Call Tone（FMP transcript 已可下载）/ WSB Mention
  - 另类：Google Trends SVI / Patent Count + Citations（Kogan 因子）/ Job Postings / App Downloads / Glassdoor
  - 宏观增强：VVIX / SKEW Index / TED/OIS Spread / Inflation Surprise / GS FCI / Macro PCA
- **T3（探索性）：**
  - ESG/治理：MSCI ESG / Carbon Intensity / Board Independence / G-Index
  - Crowding：HF Crowding Score / Factor Crowding / Pairwise Correlation Spike
  - ML 生成：Autoencoder 残差（Gu-Kelly-Xiu）/ XGBoost SHAP 交互因子 / BERT 10-K embedding / K-line CNN
  - 跨资产扩展：固收（Carry / CDS-Bond Basis）+ 加密（MVRV/NVT/Funding Rate）

**P3 — 方法论强化（与因子并行）：**
- Fama-MacBeth 截面回归测每个因子的截面溢价 + t 值
- 多重检验校正：T 阈值 ≥ 3.0（Harvey-Liu-Zhu 2016），不用 2.0
- Gram-Schmidt 因子正交化去多重共线
- Barra 风格因子分解：把当前 alpha 拆成风格暴露 + 残差 alpha
- 半衰期加权 IC（近期权重高）
- Regime-dependent 因子轮动（HMM / 宏观状态机）

**P4 — 回测鲁棒性（穿插进行）：**
- 换手率：MVO 优化器 turnover penalty（γ=0.005）已内置，待回测验证效果
- 滑点：T+1 VWAP + 10bps / 15bps，测 alpha 衰减幅度
- Regime：Credit Veto / 拥挤度参数 ±50% 扰动测试

**P5 — ML Blend 修复（因子优化之后）：**
- 当前 LightGBM 50% blend 拖累 alpha 7.6%（过拟合 + 稀释线性信号），已默认关闭
- 修复方向：降 blend 比例（10-20%）、增加正则化、延长训练窗口（24-36M）、ensemble rank 替代 raw score blend
- 验证标准：ML on 的 IS alpha 必须高于 ML off，否则不启用

**P6 — 工程与实盘（策略稳定后）：**
- 替代数据每日增量采集自动化
- IV_SKEW/PUT_CALL_RATIO 积累日频数据（1-2 年后重评）
- Alpaca 模拟盘接入已完成，实盘模拟 3-6 个月验证滑点

### 设计原则

- **每步独立可测**：每个阶段前后跑 FF5 回归对比，alpha 增量不显著则回滚
- **因子选择基于样本外验证**：不做 IC 引导权重优化（v1 已证明是数据窥探）
- **三层同步**：每步完成后同步 CLI / API / 前端 / 文档
