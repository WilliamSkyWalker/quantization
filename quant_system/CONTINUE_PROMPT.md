ji xu# A股量化投资系统 - 续接 Prompt

> 每次开启新会话时，将下方对应阶段的 Prompt 发送给 AI 即可无缝继续。

---

## 通用上下文（每次都贴）

```
你是一位资深量化工程师，正在帮我搭建 A 股量化投资系统。

项目路径：/Users/daweilun/Documents/quantization/quant_system
数据库：本地 MySQL（库名 quant，pymysql 驱动）
数据源：AkShare（免费），辅助 Tushare
语言：Python 3.10+
核心依赖：pandas, numpy, akshare, sqlalchemy, pymysql, tqdm, lightgbm, cvxpy

开发规范：
- 每个函数必须有 docstring
- 使用 Python logging 模块输出日志
- 时间统一用 pd.Timestamp，时区 Asia/Shanghai
- 数据库操作用 SQLAlchemy ORM
- 因子计算必须做截面计算，禁止未来数据
- 财务数据必须用公告日期（ann_date），不用报告期
- 回测中涨跌停：当日涨停不可买入，当日跌停不可卖出
- 代码可直接运行，不留 TODO 占位，有完整异常处理

请先阅读项目现有代码再动手修改，不要凭空猜测已有实现。
```

---

## 当前进度

| 阶段 | 模块 | 状态 |
|------|------|------|
| Phase 1 数据层 | `config/settings.py` | ✅ 完成 |
| Phase 1 数据层 | `data/database.py` (ORM建表) | ✅ 完成 |
| Phase 1 数据层 | `data/downloader.py` (股票列表+日线) | ✅ 完成 |
| Phase 1 数据层 | `main.py` (命令行入口) | ✅ 完成 |
| Phase 1 数据层 | `data/cleaner.py` (数据清洗) | ✅ 完成 |
| Phase 1 数据层 | `data/updater.py` (财务+行业下载) | ✅ 完成 |
| Phase 1 数据层 | 每日增量更新脚本 (`update_all` 命令) | ✅ 完成 |
| Phase 2 因子层 | 价值/动量/质量/技术因子 | ✅ 完成 |
| Phase 2 因子层 | 因子处理(去极值/中性化/标准化) | ✅ 完成 |
| Phase 2 因子层 | 因子有效性评估(IC/ICIR/分层) | ✅ 完成 |
| Phase 3 策略层 | 多因子选股 + 回测引擎 | ✅ 完成 |
| Phase 4 风控层 | 风控模块 | ✅ 完成 |
| Phase 5 执行层 | 掘金模拟盘接入 | ⚠️ 已停服，保留作参考 |
| Phase 5 执行层 | `execution/base_trader.py` (交易抽象基类) | ✅ 完成 |
| Phase 5 执行层 | `execution/paper_trader.py` (本地模拟盘) | ✅ 完成 |
| Phase 5 执行层 | `data/database.py` 新增4张模拟盘表 | ✅ 完成 |
| Phase 5 执行层 | `main.py` CLI集成 + 工厂函数 | ✅ 完成 |
| Phase 5 执行层 | 券商实盘对接 (QMT/Ptrade) | 📋 待实现 |
| Phase 6 监控层 | 绩效追踪 + 报告生成 | ✅ 完成 |

---

## Phase 1 续接：数据清洗 + 财务/行业数据

```
继续 A 股量化投资系统 Phase 1 开发。

已完成：settings.py, database.py(MySQL ORM 4张表), downloader.py(股票列表+日线行情), main.py
请先读取以下文件了解现有实现：
- quant_system/config/settings.py
- quant_system/data/database.py
- quant_system/data/downloader.py
- quant_system/main.py

现在请完成：

1. data/cleaner.py — 数据清洗模块
   - 剔除ST、*ST股票（基于 stock_basic.is_st 字段）
   - 剔除上市不足180天的新股
   - 校验涨跌停标记（主板±10%，创业板/科创板±20%）
   - 处理缺失值和异常值（成交量为0的交易日标记）
   - 提供 get_clean_universe(date) 函数：返回某日可交易股票池

2. data/updater.py — 财务数据和行业分类下载
   - 用 AkShare 下载全市场财务数据（PE、PB、ROE、毛利率、营收、净利润、市值）
   - 用 AkShare 下载申万一级行业分类
   - 财务数据必须记录公告日期（ann_date），防止未来函数
   - 存入 database.py 已定义的 financial_data 和 industry_class 表

3. 更新 main.py 添加对应命令入口

要求：代码可直接运行，有完整异常处理和日志，下载时加限速和进度条。
```

---

## Phase 2 续接：因子计算

```
继续 A 股量化投资系统 Phase 2 — 因子层开发。

请先读取以下文件了解现有实现：
- quant_system/config/settings.py
- quant_system/data/database.py
- quant_system/data/downloader.py
- quant_system/data/cleaner.py

现在请完成 factors/ 目录下所有模块：

1. factors/base.py — 因子基类
   - 定义因子计算的统一接口：compute(date) -> DataFrame[ts_code, factor_value]
   - 截面计算，每月末更新

2. factors/value.py — 价值因子
   - EP（市盈率倒数 = 1/PE_TTM）
   - BP（市净率倒数 = 1/PB）

3. factors/momentum.py — 动量因子
   - MOM_1M（过去1个月收益率）
   - MOM_3M（过去3个月收益率）
   - MOM_12M（过去12个月收益率，剔除最近1个月）

4. factors/quality.py — 质量因子
   - ROE_TTM
   - 毛利率

5. factors/technical.py — 技术因子
   - 过去20日平均换手率（流动性代理）

6. factors/processor.py — 因子处理流水线
   - 去极值：MAD法（中位数绝对偏差）
   - 行业市值中性化：对行业哑变量和 ln(市值) 做截面回归取残差
   - Z-Score 标准化

7. 因子有效性评估脚本（可放 notebooks/ 或单独文件）
   - IC（因子值与下期收益率的截面相关系数）
   - ICIR（IC均值/IC标准差）
   - 因子分层回测（按因子值分5组，画净值曲线）

注意：所有因子计算必须是截面计算，禁止用到未来数据。财务数据要用公告日期匹配。
```

---

## Phase 3 续接：策略与回测

```
继续 A 股量化投资系统 Phase 3 — 策略层开发。

请先读取 factors/ 目录下所有文件了解因子实现，再开发：

1. strategy/multi_factor.py — 多因子打分选股模型
   - 等权合成多因子得分
   - 选股范围：沪深300成分股
   - 月频调仓（每月最后一个交易日）
   - 持仓 20~30 只

2. strategy/backtest.py — 轻量回测引擎（基于 pandas）
   - 输入：每期选股结果 + 权重
   - 处理：交易成本（佣金0.15%双边 + 印花税0.1%卖出 + 滑点0.1%）
   - T+1信号执行：T日收盘产生信号，T+1日执行，使用T+1日涨跌停约束
   - 涨跌停处理：涨停不可买入，跌停不可卖出
   - 输出：净值曲线、年化收益、最大回撤、夏普比率、换手率
   - 基准：沪深300指数

要求：回测结果可视化（matplotlib 画净值曲线对比图）。
```

---

## Phase 4 续接：风控

```
继续 A 股量化投资系统 Phase 4 — 风控模块。

请先读取 strategy/ 目录了解策略实现，再开发：

1. risk/risk_manager.py
   - 个股持仓上限：5%
   - 单行业暴露：不超过30%
   - 最大回撤触发降仓：回撤超15%仓位降至50%
   - 流动性过滤：剔除日均成交额 < 5000万的股票
   - 提供 adjust_weights(weights_df, date) 接口，输入原始权重，输出风控后权重
```

---

## Phase 5 续接：券商实盘对接（QMT / Ptrade）

```
继续 A 股量化投资系统 Phase 5 — 券商实盘对接。

本地模拟盘已完成，现需接入真实券商的模拟盘或实盘。
系统已预留 BaseTrader 抽象基类和 TRADER_TYPE 配置项，接入新券商零改动策略层。

已完成的执行层架构：
- execution/base_trader.py — 交易执行器 ABC（connect, sync_position, get_positions 等）
- execution/paper_trader.py — PaperTrader(BaseTrader) 本地模拟盘，129个测试全通过
- execution/gm_trader.py — 旧掘金接口（已停服，保留作参考）
- config/settings.py — TRADER_TYPE 配置项（paper/qmt/ptrade）
- main.py — _create_trader() 工厂函数，根据 TRADER_TYPE 自动选择实现

本地模拟盘功能：
- 4张持久化表：paper_account, paper_position, paper_transaction, paper_nav
- T+1信号执行：T日收盘后产生信号，T+1日开盘价±滑点成交（消除前视偏差）
- 交易模拟：开盘价±滑点、佣金(万7.5,最低5元)+印花税、100股整手、涨跌停限制
- 回放模式：python3 main.py paper_replay 2020-01-01 2024-12-31 --reset
- 日常模式：python3 main.py trade（自动使用 TRADER_TYPE 对应的执行器）

接入新券商只需：
1. 新建 execution/qmt_trader.py — QMTTrader(BaseTrader)
   - 实现 BaseTrader 的全部抽象方法
   - 通过 xtquant SDK 连接 Mini QMT
   - 处理 QMT 的股票代码格式转换
2. 在 main.py _create_trader() 中添加 qmt 分支
3. .env 中设置 TRADER_TYPE=qmt + QMT 连接参数

请先读取以下文件了解现有接口：
- quant_system/execution/base_trader.py（必须实现的接口）
- quant_system/execution/paper_trader.py（参考实现）
- quant_system/main.py（_create_trader 工厂函数）
```

---

## Phase 6 续接：监控与报告

```
继续 A 股量化投资系统 Phase 6 — 监控与报告。

请先读取整个项目了解全部实现，再开发：

1. monitor/performance.py — 每日绩效追踪
   - 对比沪深300基准
   - 计算超额收益、信息比率
   - 归因分析（行业归因、因子归因）

2. monitor/report.py — 月度报告自动生成
   - 输出 HTML 报告
   - 包含：净值曲线、持仓明细、因子IC监控、风险指标、调仓记录
```

---

## Debug 用 Prompt

```
A股量化系统遇到问题，请帮我排查。

项目路径：/Users/daweilun/Documents/quantization/quant_system
数据库：本地 MySQL（库名 quant）

问题描述：
[在这里描述错误信息和复现步骤]

请先读取相关代码文件，定位问题根因，然后给出修复方案并直接修改代码。
```

---

## 已完成优化

- [x] 股票列表下载优化：批量获取上市日期（~2秒 vs 原来 ~42分钟）
- [x] 日线行情多线程并发下载（DOWNLOAD_WORKERS 可配，默认8线程，.env 中可调）
- [x] DB批量写入：攒批50只后一次性 bulk_insert，减少DB往返
- [x] 增量更新多线程并发 + 批量 bulk_upsert（MySQL ON DUPLICATE KEY UPDATE）
- [x] 估值快照批量 CASE WHEN UPDATE（替代逐行 UPDATE）
- [x] 完整单元测试覆盖：129个测试用例全部通过（SQLite in-memory，不依赖MySQL）
- [x] DatabaseManager 兼容 SQLite/MySQL（连接池参数自动适配）
- [x] 修复 datetime64 vs date 类型比较问题（cleaner.py）
- [x] 修复风控降仓后归一化失效问题（risk_manager.py）
- [x] 本地模拟盘 PaperTrader 替代已停服的掘金量化（execution/paper_trader.py）
- [x] BaseTrader 抽象基类，预留 QMT/Ptrade 券商切换接口（execution/base_trader.py）
- [x] 模拟盘持久化：4张MySQL表（paper_account/position/transaction/nav）
- [x] 模拟盘交易模拟：开盘价±滑点、佣金(万7.5,最低5元)+印花税、100股整手、涨跌停限制
- [x] 回放模式：历史区间逐日模拟交易，含除权除息处理（adj_factor检测）
- [x] TRADER_TYPE 工厂模式：main.py 通过配置项自动选择 paper/qmt/ptrade 执行器
- [x] T+1信号执行修复（消除前视偏差）：BacktestEngine 和 PaperTrader 均已修复
  - BacktestEngine: pending_signal 模式，T日信号存储，T+1日用T+1涨跌停约束执行
  - PaperTrader: replay() 同样 pending_signal 延迟执行，_execute_rebalance() 使用开盘价成交
- [x] 修复流动性过滤单位不匹配：DB amount 列为千元，MIN_DAILY_TURNOVER 为元，比较前乘1000（risk_manager.py）
- [x] 修复增量更新时区报错：pd.to_datetime 返回 tz-naive 与 Timestamp.now(tz=) 的 tz-aware 比较失败（downloader.py）
- [x] 修复估值快照更新仅匹配2条：改用 INNER JOIN 子查询匹配每只股票各自最新报告期，而非全局 MAX(end_date)（database.py）
- [x] 修复估值快照取当天未收盘数据为空：回退尝试最近5个交易日直到找到有数据的日期（updater.py）

## 模拟盘 CLI 命令

```bash
python3 main.py trade                              # 执行当日交易（使用TRADER_TYPE对应的执行器）
python3 main.py position                           # 查看持仓
python3 main.py paper_replay 2020-01-01 2024-12-31 --reset  # 回放历史交易
python3 main.py paper_nav                          # 查看净值历史
python3 main.py paper_transactions --last 50       # 查看交易记录
python3 main.py paper_reset --confirm              # 重置模拟账户
```

## 项目文件结构

```
quant_system/
├── config/settings.py               # 全局配置 + .env 加载
├── data/
│   ├── database.py                  # ORM表定义(8张表) + DatabaseManager
│   ├── downloader.py                # Tushare 数据下载
│   ├── cleaner.py                   # 数据清洗 + 股票池构建
│   └── updater.py                   # 财务数据 + 行业分类更新
├── factors/
│   ├── base.py                      # 因子基类 ABC
│   ├── value.py                     # EP, BP 因子
│   ├── momentum.py                  # MOM_1M, MOM_3M, MOM_12M
│   ├── quality.py                   # ROE, GrossMargin
│   ├── technical.py                 # Turnover_20D
│   ├── processor.py                 # 去极值/中性化/标准化
│   └── evaluation.py                # IC/ICIR/分层回测
├── strategy/
│   ├── multi_factor.py              # 多因子等权选股
│   └── backtest.py                  # 回测引擎
├── risk/
│   └── risk_manager.py              # 风控（个股/行业上限/回撤降仓）
├── execution/
│   ├── base_trader.py               # 交易执行器 ABC
│   ├── paper_trader.py              # 本地模拟盘（替代掘金）
│   └── gm_trader.py                 # 掘金接口（已停服，保留参考）
├── monitor/
│   ├── performance.py               # 绩效追踪 + 归因
│   └── report.py                    # HTML 报告生成
├── tests/                           # 129个测试用例
├── main.py                          # CLI 入口
└── .env                             # 本地配置（不入库）
```

## 已知待优化项

- [ ] 首次全量下载无断点续传，bulk_insert 重跑可能重复
- [ ] 前复权价格会随分红送股变化，需定期全量刷新
- [ ] MySQL 保留字 `open` 作列名，raw SQL 需加反引号
- [x] ~~updater.py 估值快照更新仍用逐只 UPDATE~~ 已改为批量 CASE WHEN + INNER JOIN 子查询
- [ ] 行业分类下载仍为串行（板块数 ~90，速度尚可）
- [ ] 券商实盘对接：QMT（华泰/国金）或 Ptrade（恒生）
- [ ] 模拟盘净值图表可视化（paper_nav --plot）
