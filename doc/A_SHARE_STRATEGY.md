# A股多因子量化选股系统 — 算法文档

> **⚠️ 2026-04-30 架构变更：A 股已迁移到 Rust。下方旧 Python 实现路径仅供算法/因子定义参考，
> 实际代码位置以 Rust workspace 为准：**
>
> | 旧 Python 路径 | 新 Rust 位置 |
> |---|---|
> | `stocks/models/a_stock.py` | `quant-engine/crates/db/src/models/a_stock.rs` |
> | `stocks/services/downloaders/a_tushare_*.py` | `quant-engine/crates/download/src/a_tushare.rs` |
> | `stocks/services/a_cleaner.py` | `quant-engine/crates/factors/src/a_share/universe.rs` |
> | `stocks/services/factors/a_*.py` | `quant-engine/crates/factors/src/a_share/factors.rs` |
> | `backtest/services/a_strategy.py / a_engine.py / a_regime.py` | `quant-engine/crates/strategy/src/a_strategy.rs` + `crates/backtest/src/a_engine.rs` |
> | `trading/services/a_paper_trader.py / a_risk.py / a_gm_trader.py` | `quant-engine/crates/trading/src/{paper,risk,broker}.rs` |
> | `python3 manage.py bulk_import --source tushare` | `quant --market cn download --source tushare --target all` |
> | `python3 manage.py backtest --market cn` | `quant --market cn factors --date YYYY-MM-DD` (回测命令进行中) |
> | `python3 manage.py paper --market cn` | `quant --market cn trade --account X --signals Y.json` |
>
> 因子设计、信号定义、universe 筛选规则、防前视逻辑等**算法层内容仍然适用**，下文描述以算法逻辑为主。
> Python 代码已归档至 `legacy_python/`，不再维护。

## 2026-10-02：Rust v2 研究基线

A 股 CLI 已接通 `a_strategy_v2` 的资金情绪回测；下文 v1 的财务选股参数说明不代表 v2 已实现。v1 保留归档，不作为 v2 回退。

- 固定使用三个有方向的因子：龙虎榜近 5 个交易日净买入比例、融资余额 20 日变化、融资买入额占成交额的近 5 日均值。方向均沿用现有 `+1` 假设，未用本轮收益选方向；上榜频率方向未确认，仍不参与评分。
- 因子只使用信号日前一交易日及更早的数据；收盘生成目标，下一交易日开盘成交。日历和基准预热历史保留到信号计算结束。
- 按 `rebalance_interval`（默认 10 个交易日）调仓，最多 20 只；截面只在可选股票池内标准化，按分数排序、代码打破并列。每个目标槽位等权，单股上限 12%、申万一级行业上限 20%，不足部分保留现金；未知行业共用一个上限。v2 只有 sentiment 类，`min_valid_categories=1`。
- 若启用 regime，滞后沪深 300 收盘低于 60 日均线时目标敞口降至 60%，否则为 100%。未接入 MVO、动态 IC、行业组约束、换手惩罚或通用 risk_controls；本轮不据收益调参。
- 开盘估值和限价检查只用开盘可见价格；停牌持仓以最近收盘估值。`adj_factor` 变化按份额调整近似红利再投资/拆股，不是精确的现金分红流水。佣金、印花税、滑点沿用配置中的固定假设，未按历史法规分段。
- 结果仍受当前 ST 标记（缺少历史状态）、历史退市样本覆盖、缺失行情及未建模市场冲击影响；属于研究基线，尚不构成经过样本外验证的策略。

```bash
cd quant-engine
./target/release/quant --market cn backtest --start 2021-01-01 --end 2026-09-30 --output ../output/a_share_v2_2021_20260930
```

输出目录包含 `nav.csv`、`rebalance_signals.json` 和 `summary.json`；没有有效信号或基准覆盖不足时返回非零状态。

本轮固定参数实测（2021-01-04 至 2026-09-30，1,393 个交易日）：累计收益 -42.20%，年化 -9.12%，最大回撤 58.87%，沪深 300 同期 -17.28%，累计落后 24.92 个百分点；140 次信号、4,860 笔成交。引擎现有 Sharpe 口径为 `(复合年化收益-2%)/年化日收益波动率`，结果 -0.53。该结果未调参，未证明因子方向有效。相关 43 项测试通过，另核对了导出净值、回撤、持仓数及目标敞口。

## 全市场资金行为指标实验（2026-10-02）

离线实验由 Rust `quant-research` crate 实现，统一入口为 `quant --market cn flow-research`。它独立于当前 v2 组合策略，不下单，不修改策略参数，也不依赖龙虎榜或融资标的资格。原 Python 研究程序及其测试脚本已删除；不再读取 pandas/pickle 中间产物。

```bash
cd quant-engine
cargo build --release -p quant-cli
# 校验并复用已有原始缓存，只下载缺失日期
./target/release/quant --market cn flow-research --stage fetch
# 读取缓存及 MySQL 行情，计算指标和检验统计
./target/release/quant --market cn flow-research --stage analyze
# 或一次执行两阶段
./target/release/quant --market cn flow-research --stage all --workers 0
```

默认读取区间为 2023-10-01 至 2026-09-30，缓存目录 `../cache/a_flow_research`。当前默认 `--entry-price open`，按买入日开盘价买入，输出到 `../output/a_flow_research_2024_20260930`。可显式指定 `--entry-price daily-range-two-thirds` 复现全天区间估算价 `low + (high-low)*2/3`，例如最低 9 元、最高 12 元时按 11 元计算；该口径输出到 `../output/a_flow_research_2024_20260930_range2of3`（路径相对上述工作目录）。不同价格口径不能覆盖同一冻结协议目录。`--workers 0` 按 CPU 数自动选择 Rayon 工作线程，最多 8 个。MySQL 读取使用最多 4 个连接，计算共享一次加载的数据，不向数据库写入。

`moneyflow` 使用 Tushare 官方个股资金流接口，读取原始 `net_mf_amount`；其单位万元，与行情成交额的千元统一换成元。完整原始响应逐日原子保存为 `moneyflow_YYYYMMDD.json.gz`。下载采用 Tokio 有界并发，最多 4 个请求共享 120 次/分钟限速，环境配置更低时服从较低额度；重试同样经过限速，限流响应触发共享冷却。返回空数据或疑似达到 6000 行截断上限时明确失败，不静默继续。该接口的净流入口径不可用各类大小单金额简单相减替代，见 [官方定义](https://tushare.pro/document/2?doc_id=170)。已下载的 726 个交易日、3,739,562 行原始 gzip 缓存继续复用；下载完成不代表指标有效性已通过验证。

固定研究方案：2023 年末预热、2024 年研究、2025 年验证、2026 年截至 9 月复核。2026 年已用于之前的策略研究，不属于完全未见的样本外区间。观察日 t 的指标，t+2 交易日进入、再持有 h 个交易日至开盘退出；主期限 h=5，1/10/20 日仅作诊断。买入日可采用开盘价或上述全天区间估算价，均用当日复权因子换算；标签不得跨入下一个研究年份。全天高低价仅作为事后成交价假设，不能作为盘中可见信号，这不是分钟级成交回测。指标计算和选股资格不读取买入日数据；成本仍统一扣原有 0.45%，不额外叠加滑点。高低价缺失、倒置、非正数或无成交时，估算收益缺失，不回退为开盘价。

| 维度 | 实验指标 |
|---|---|
| 活跃度 | 3 日成交额均值 / 此前 20 日成交额中位数；5 日换手率均值 / 此前 20 日均值 |
| 买卖压力 | 1 日及 5 日净流入 / 同期成交额 |
| 持续性 | 5 日净流入天数比例；最大单日正流入 / 5 日正流入合计 |
| 价格反应 | 5 日相对行业收益；5 日收盘位置均值 `(close-low)/(high-low)` |
| 风险 | 20 日日收益波动；20 日 `abs(return)/成交额` 均值 |

行业按观察日有效成员计算，未来行业收益采用剔除自身的同行等权平均，剔除后其他同行有效样本至少 10 个。指标缺失保留 NA，不补零；数据有效的沪深股票需至少 60 条历史行情，不设市值或股价下限，不用当前 ST 标记筛历史。单独报告近 20 日成交额中位数至少 500 万元的容量分组。

各价格口径的输出目录包含预先固定的 `protocol.json`、每日覆盖率、逐指标逐期限逐年 Rank IC、两端分组收益、月度均值 bootstrap 区间、指标相关矩阵、规模/流动性/价格暴露和全市场最新指标表。并列因子值不强行按股票代码拆组。收益使用复权买入估算价和卖出开盘价近似总收益，双边比例成本固定 0.45%；高低组收益差只是统计诊断，不是可实施的多空收益。

`run_config.json` 冻结本次请求的日期范围和缓存路径；修改这些参数须指定新的 `--output` 目录。输出目录有运行锁，只有 `run_state.json` 中 `status` 为 `complete` 才表示本次分析完整结束；其他状态下的文件不能当作完整结果使用。

这是重叠持有期的因子事件检验，不是组合净值回测。停牌/退市后的缺失标签、涨跌停无法成交、历史 ST、市场冲击仍可能造成偏差；同时报告标签覆盖率。置信区间未做多重检验校正，不能把某一个期限的显著性直接当作可交易 alpha。实验不自动翻转因子方向或拟合组合权重。

### 分钟线入场数据层

后续研究采用日线形成候选池、分钟线检验入场时机的分层方式。分钟数据通过 Rust `quant --market cn minutes` 按股票和时间段获取，不全市场导入 MySQL；默认仅内存，历史回测片段可选择 gzip 缓存，详见 [分钟数据说明](DATA_SOURCES.md#分钟线按需查询不入库)。查询层已经与策略层分开：取到行情不代表可以直接成交，未完成分钟、过期行情及数据发布时间需要在信号层处理。当前 v2 的固定周期调仓和前述日线研究尚未改成分钟级交易策略。

### 首轮 Rust 实测结果（开盘价口径）

2026-10-02 完成：726 个交易日、5,325 只沪深股票，读取 3,725,439 行行情和 3,739,562 行原始资金流；2024 年起共 666 个观察日，输出 240 组年度汇总。8 个 Rayon 线程下，程序耗时约 39 秒（外部计时约 40 秒），其中数据读取与整理约 32 秒、指标和统计约 6 秒；峰值内存约 1.16 GiB。主要耗时已在 MySQL 读取，指标本身约 0.3 秒。

下表是全样本、5 日持有期的日均行业相对 Rank IC，正值表示指标较高的股票后续相对收益通常较高，尚未转成组合交易规则：

| 指标 | 2024 研究 | 2025 验证 | 2026 截至 9 月复核 |
|---|---:|---:|---:|
| 成交额活跃度 | -0.0423 | -0.0469 | -0.0222 |
| 换手率活跃度 | -0.0360 | -0.0441 | -0.0162 |
| 1 日净流入强度 | -0.0012 | 0.0069 | 0.0003 |
| 5 日净流入强度 | -0.0010 | 0.0102 | -0.0030 |
| 5 日流入天数比例 | 0.0042 | 0.0121 | 0.0017 |
| 5 日流入集中度 | -0.0013 | -0.0070 | -0.0009 |
| 5 日相对行业涨幅 | -0.0390 | -0.0358 | 0.0036 |
| 5 日收盘位置 | -0.0168 | -0.0092 | -0.0039 |
| 20 日波动率 | -0.0611 | -0.0702 | -0.0639 |
| 20 日非流动性 | 0.0128 | 0.0440 | 0.0515 |

净流入强度的 IC 接近零，且年度方向不稳定，本轮不支持直接采用“净流入越多越好”的选股规则。活跃度与波动率的排序关系较一致，但多数年度的高低组收益差区间跨零，不能据此断言可获利。成交额与换手活跃度的日均相关性约 0.881，不能当作两个独立信号重复加权；非流动性与规模、成交额的秩相关约 -0.804、-0.905，需先区分规模与流动性暴露，不能直接解释成独立资金 alpha。本轮没有根据这些结果翻转因子方向或拟合权重。

验证：12 项研究测试及 16 项下载测试通过；1 项需专用数据库的旧下载集成测试未运行。另完成 Rust 单日真实下载（2026-09-30，5,572 行）、全部缓存校验、release 构建和完整区间分析；用同一输出目录更改日期范围会明确失败。完整结果、覆盖率、源码快照及运行清单位于 `output/a_flow_research_2024_20260930/`，耗时日志为 `logs/a_flow_research_rust.log`。

## 低价小市值上涨案例（2026-10-05）

运行 `cd quant-engine` 后执行 `./target/release/quant --market cn low-price-cases`。这是日线历史案例复盘，与默认开盘价买入的因子事件研究及 v2 组合策略独立。

固定筛选：2024 年起，窗口起点名义收盘价不超过 5 元、流通市值不超过 50 亿元，随后 5 个交易日复权收盘涨幅达到 20%；要求起点满足现有历史上市资格，并有连续 20 天背景数据。同股识别后冷却 60 个交易日，避免重叠窗口重复计数。比较上涨前 20 日、上涨 5 日、识别后 20 日的量价和资金流。后续收益、峰值与最大回撤均使用复权收盘价，缺失或尾部不足 20 日保留空值，不是可成交的策略收益。

输出 `output/a_low_price_cases_20260930/`：`protocol.json` 固定口径，`cases.json` 保存全部事件和逐年统计，`showcase_daily.json` 保存展示案例逐日明细，`report.html` 展示走势图。展示从 2026 年后续数据完整的事件中选上涨 5 日涨幅最大的 6 只、后 20 日表现最差和最好的各 3 只，股票去重；展示选择明确使用了事后信息。统计基于全体筛中事件，不能据此估计提前选股的成功率。未筛历史 ST、股票名称取当前元数据，退市数据缺口仍可能影响样本；资金净流入不等同于机构吸筹或上涨原因。

本轮实际运行约 40.5 秒，覆盖 5,325 只股票，共识别 2,139 段事件（2024/2025/2026 分别 1,566/370/203 段），输出 12 个案例。2026 年上涨窗口成交额相对前 20 日均值的中位数为 2.17 倍；上涨前 5 日净流入为正占 22.7%，上涨 5 日合计净流入为正占 46.8%。后 20 日数据完整的 176 段中，收盘价高于识别日的占 42.0%，收益中位数 -2.17%。这些是筛中上涨事件的描述统计，未匹配非上涨对照，不能推导资金流的预测有效性。

### 上涨催化与公告时间核对（2026-10-05）

本轮核对 12 个展示案例。下表区分公司已披露事件、同期市场题材和无法确认的解释；“事件存在且时间相邻”仍不是因果检验证明。公告落款日期不等于盘前可获得时间，未取得精确发布时间的记录不得直接生成当日开盘信号。识别后的公告仅用于解释后续路径或反证，不能回填为启动信号。

| 股票／研究窗口（2026年） | 可核实的信息与时间 | 研究判断及边界 |
|---|---|---|
| 节能铁汉 300197，5/26—6/2 | [5/29 重组提示公告](https://money.finance.sina.com.cn/corp/view/vCB_AllBulletinDetail.php?id=12364103&stockid=300197)提出资产出售与购买；[当晚20:33报道](https://finance.sina.com.cn/stock/zqgd/2026-05-29/doc-inhzqptm7566168.shtml)确认盘后已公开。 | 明确公司事件先于6/1启动；可归入重组预期案例。不能将6/2才明确的收购标的回填到5/29。重组当时仍未确定方案，不等于资产注入已完成。 |
| 华电辽能 600396，3/5—3/12 | [3/5同期政策报道](https://finance.eastmoney.com/a/202603053663074274.html)涉及算电协同；[3/27公司公告](https://epaper.stcn.com/pic/202603/27/60ff19579f7a1cb68e0dfbdb0988e2c9.pdf)明确不涉及算电协同项目。 | 政策题材与上涨时间相邻，后续存在板块共振；不能认定公司有相关项目或新增订单。3/27澄清属于事后核验，不能用于3/6决策。 |
| 中嘉博创 000889，4/2—4/10 | [4/13异动公告](https://money.finance.sina.com.cn/corp/view/vCB_AllBulletinDetail.php?id=12081909&stockid=000889)称经营环境无重大变化；[同期报道](https://www.eeo.com.cn/2026/0409/832757.shtml)将算力、5G消息列作可能驱动。 | 题材候选，未确认窗口内新增重大订单。报道中的“近期摘帽”有时间错位：[2025半年报](https://static.cninfo.com.cn/finalpage/2025-08-26/1224569423.PDF)确认摘帽在2025/6/4，不能算作2026/4的新事件。 |
| ST豆神 300010，8/19—8/26 | [8/28异动公告](https://money.finance.sina.com.cn/corp/view/vCB_AllBulletinDetail.php?id=12563483&stockid=300010)对应8/25—27上涨，未披露其他重大未公开信息，说明同期半年报仍亏损；[8/5诉讼进展报道](https://m.sohu.com/a/1059195337_122014422/)记录更早的撤诉事项。 | 已核实风险修复背景，但未确认8/19—26直接新增催化；半年报披露晚于本轮识别日，不能用作提前触发。AI教育、算力等解释保持待核实。 |
| 三峡新材 600293，5/29—6/5 | [6/5财联社快讯](https://api3.cls.cn/share/article/2391434?app=&os=web&sv=831)将其列入玻璃基板概念上涨股；[6/6公司公告](https://money.finance.sina.com.cn/corp/view/vCB_AllBulletinDetail.php?id=12376159&stockid=600293)称基本面无重大变化。 | 有同期题材联动证据，但不能据此确认公司已形成先进封装玻璃基板业务。[7/10项目公告目录](https://money.finance.sina.com.cn/corp/go.php/vCB_AllBulletin/stockid/600293.phtml?ftype=lsgg)中的新项目晚于启动，不能回填。 |
| 贤丰控股 002141，6/11—6/18 | [6/18交易所公告](https://disc.static.szse.cn/download/disc/disk03/finalpage/2026-06-18/f60aa58f-5b6a-4654-8570-fdf29ab8b962.PDF)核实经营环境未发生重大变化；[6/17同花顺报道](https://yuanchuang.10jqka.com.cn/20260617/c677523295.shtml)列出覆铜板涨价、PCB上游等题材。 | 覆铜板景气题材候选；报道注明AI生成，相关涨价幅度和具体订单未作为独立已核实事实。不能把4月发布的年报当成6月的新公告。 |
| 珈伟新能 300317，7/13—7/20 | [7/17同期市场报道](https://m.stnn.cc/detail/6a59a8e1c39b3f0d86b97279.html)记录多只电力股同步上涨；[7/20公司公告](https://vip.stock.finance.sina.com.cn/corp/view/vCB_AllBulletinDetail.php?id=12454083&stockid=300317)称经营正常、无重大未披露事项。 | 板块联动证据较清楚，未确认独立公司新利好。8月半年报不属于7月启动前信息。 |
| ST金鸿 000669，2/6—2/13 | [2/10专项自查公告](https://money.finance.sina.com.cn/corp/view/vCB_AllBulletinDetail.php?id=11958408&stockid=000669)确认被债权人申请预重整；[3/18风险公告](https://money.finance.sina.com.cn/corp/view/vCB_AllBulletinDetail.php?id=12000697&stockid=000669)回溯2/10披露与2/24庭外重组进展。 | 窗口内存在明确重整事件；申请、法院同意庭外重组、正式受理重整必须区分，不能提前视为重整成功。 |
| *ST元道 301139，7/22—7/29 | [7/28原始公告](https://static.cninfo.com.cn/finalpage/2026-07-28/1225444973.PDF)确认异动，同时提示可能重大违法强制退市。 | 风险背景明确，尚未确认正面公司催化；不能将暴跌后的反弹当成经营反转。原行情报告名称“元道退”为当前元数据，该窗口历史名称为“*ST元道”。 |
| *ST清越 688496，7/21—7/28 | [7/28原始公告](https://static.cninfo.com.cn/finalpage/2026-07-28/1225443080.PDF)记录截至7/27连续15个交易日低于1元，并提示可能重大违法强制退市。 | 归为退市风险背景下反弹案例，不把回到1元以上等同于全部退市风险解除；未确认正面经营催化。 |
| *ST万方 000638，2/9—2/24 | [2/25异动公告](https://file.finance.sina.com.cn/211.154.219.97%3A9494/MRGG/CNSESZ_STOCK/2026/2026-2/2026-02-25/11969579.PDF)明确2/10起连续涨停并提示情绪过热、退市风险。 | 未找到足以确认直接启动原因的正面公告；自动生成报道中的“治理优化”等叙事不作为已核实催化。 |
| 华谊兄弟 300027，2/3—2/10 | [2/10证券时报同期报道](https://finance.eastmoney.com/a/202602103646480976.html)记录春节档预售与AI视频题材下影视股集体上涨；[2/9公司公告](https://static.cninfo.com.cn/finalpage/2026-02-09/1224972589.PDF)证券简称为华谊兄弟。 | 板块题材证据较清楚，不意味着公司直接获得AI订单。原行情报告使用当前名称“ST华谊”，不能据此将2月样本认定为当时ST股。 |

对后续研究的影响：按公司事件、板块题材、退市风险背景、未确认催化分别打标签，先比较同类案例的时序；暂不把这些人工事后标签直接当成已验证因子。既有资金流字段与媒体“主力资金净流入”不是同一口径，不能将它们混用来证明机构吸筹。

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

每 10 个交易日 + 自适应调仓的多因子打分选股策略，核心流程：

```
每 10 个交易日调仓(T日) + 偏离度触发的额外调仓日
  → 构建可交易股票池（含核心财务准入过滤）
  → 计算 30 个因子（动量/偏离度使用前复权价格）
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
| 6 | 市值过滤 | JOIN `stock_basic` 获取 `total_share`（万股），市值 = `total_share × close × 10000`（元），过滤极小市值 |
| 7 | 流动性过滤 | 近 20 个交易日日均成交额 ≥ `MIN_DAILY_TURNOVER`（默认 5000 万元） |
| 8 | **核心财务准入** | EP/BP/ROE_TTM/GROSS_MARGIN 至少一项非空，否则剔除 |

涨停/跌停标记：主板 ±10%（阈值 9.9%），创业板/科创板 ±20%（阈值 19.9%）。涨停股不可买入但保留在池中。

---

## 3. 因子体系

共 39 个因子，分 7 大类（10 个宏观/舆情因子为 stub，暂未接入数据）。

> **2026-08-28 更新**：新增 10 个因子（PIOTROSKI_F, FREE_FLOAT_PCT, AMIHUD_ILLIQ, ACCRUALS, BAB_BETA, REVENUE_ACCELERATION, GROSS_MARGIN_CHG, RSI_14, MAX_RET, PRICE_52W_HIGH），基于因子分析 IC 验证。

### 3.1 价值因子（value）

| 因子 | 名称 | 公式 | 方向 |
|------|------|------|------|
| **EP** | 市盈率倒数 | TTM净利润 / (收盘价 × 总股本 × 10000) | 越高越好 |
| **BP** | 市净率倒数 | 每股净资产(BPS) / 收盘价 | 越高越好 |
| **DIV_YIELD** | 股息率 | 近12个月股息率（dv_ttm from daily_price） | 越高越好 |

- EP 使用滚动 4 季度 TTM 净利润，净利润 ≤ 0 返回 NaN
- BP 使用最新报告期 BPS，BPS ≤ 0 返回 NaN
- DIV_YIELD 仅使用 daily_price.dv_ttm，无数据时返回 NaN（不做 PE 回退近似）
- 数据防未来函数：仅使用 `ann_date ≤ 选股日` 的报告

### 3.2 质量因子（quality）

| 因子 | 名称 | 公式 | 方向 |
|------|------|------|------|
| **ROE_TTM** | 净资产收益率 | 直接读取 `financial_data.roe_ttm` | 越高越好 |
| **GROSS_MARGIN** | 毛利率 | 直接读取 `financial_data.gross_margin` | 越高越好 |
| **PROFIT_STB** | 盈利稳定性 | std(同比增长率) / \|mean(同比增长率)\| | **越低越好（反向）** |
| **MARGIN_TREND** | 毛利率趋势 | 当期毛利率 - 上期毛利率 | 越高越好 |
| **PIOTROSKI_F** | Piotroski F 评分 | 6 项财务健康指标得分之和（见下） | 越高越好 |
| **ACCRUALS** | 应计利润变化 | -(本期 ocf_to_profit - 上期 ocf_to_profit) | **越低越好（反向）** |

- PROFIT_STB 使用最近 4+ 个报告期的净利润同比增速的变异系数（CV），需 ≥ 3 组有效同比数据
- **PIOTROSKI_F**（2026-08-28 新增）：简化版 6 分制评分，每项 +1 分：① ROA > 0 ② 经营现金流 > 0（ocf_to_profit > 0）③ 流动比率 > 1 ④ 毛利率环比改善 ⑤ 资产负债率同比下降 ⑥ 资产周转率同比上升。数据不足时返回 NaN
- **ACCRUALS**（2026-08-28 新增）：反向因子，衡量盈利质量。经营现金流占比下降（应计利润增加）→ 值更高 → 盈利质量更差。数据不足时返回 NaN

### 3.3 成长因子（growth）

| 因子 | 名称 | 公式 | 方向 |
|------|------|------|------|
| **NET_PROFIT_YOY** | 净利润同比 | TTM净利润(当期) / TTM净利润(去年同期) - 1 | 越高越好 |
| **REVENUE_YOY** | 营收同比 | TTM营收(当期) / TTM营收(去年同期) - 1 | 越高越好 |
| **NET_PROFIT_CAGR_3Y** | 3年复合增长率 | (TTM净利润(当期) / TTM净利润(3年前))^(1/3) - 1 | 越高越好 |
| **REVENUE_ACCELERATION** | 营收加速度 | 当期 q_sales_yoy - 上期 q_sales_yoy | 越高越好 |
| **GROSS_MARGIN_CHG** | 毛利率变化 | 当期 gross_margin - 4 季度前 gross_margin | 越高越好 |

- 分母 ≤ 0 → NaN（避免负利润增速误导）
- CAGR 要求当期和 3 年前 TTM 净利润均 > 0，IPO < 3 年的股票自动 NaN
- revenue/net_profit 数据来自 `fina_indicator` + `income` 接口合并
- **REVENUE_ACCELERATION**（2026-08-28 新增）：衡量营收增速的边际变化（二阶导数），正値表示增速加快。需要连续两季度 q_sales_yoy 数据，不足时返回 NaN
- **GROSS_MARGIN_CHG**（2026-08-28 新增）：毛利率 4 季度变化量，反映盈利能力趋势。需要当期和 4 季度前 gross_margin 数据，不足时返回 NaN

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
| **PRICE_52W_HIGH** | 52周新高比 | AdjClose / max(AdjClose, 380D) | 380 交易日 | 越高越好 |

- **前复权价格**: MOM_1M/3M/12M 使用 `adj_close = close × adj_factor` 计算跨期收益率，避免除权除息产生虚假信号。`adj_factor` 为 NULL 时 fillna(1.0) 保持向后兼容。
- MOM_12M 跳过最近 1 个月，避免短期反转污染
- RESIDUAL_MOM 剥离了行业 beta，捕捉个股 alpha
- **PRICE_52W_HIGH**（2026-08-28 新增）：当前前复权收盘价除以 380 个交易日内最高前复权收盘价，衡量距离 52 周高点的距离。值越接近 1.0 表示越接近新高。数据不足时返回 NaN
- CMDTY_MOM 通过两层映射（L2 优先 → L1 回退）将商品价格动量传导到对应行业股票。无映射行业（如银行、计算机）返回 NaN，由动态分母机制正确处理。同行业多商品按 OI（持仓量）加权平均。数据来源：Tushare `fut_mapping` + `fut_daily`。
- **CMDTY_MOM 暴涨检测**：基于历史滚动动量分布计算 z-score（`COMMODITY_SURGE_LOOKBACK=500` 交易日窗口），当 z ≥ `COMMODITY_SURGE_ZSCORE`（默认 2.0）时触发非线性放大，放大倍率 = `1 + (COMMODITY_SURGE_MULTIPLIER - 1) × min((z - threshold) / threshold, 1.0)`，最大 `COMMODITY_SURGE_MULTIPLIER`（默认 1.5x）。用于捕捉黄金、原油等商品暴涨对相关行业的超额影响。

### 3.5 技术因子（technical）

| 因子 | 名称 | 公式 | 回溯期 | 方向 |
|------|------|------|--------|------|
| **TURN_20D** | 20 日平均换手率 | mean(turnover_rate, 20D) | 20 交易日 | **反向** |
| **VOL_20D** | 20 日波动率 | std(日收益率, 20D) | 20 交易日 | **反向** |
| **PRICE_DEV_60D** | 60 日均线偏离 | (AdjClose - MA60_adj) / MA60_adj | 60 交易日 | **反向** |
| **SIZE** | 市值 | ln(收盘价 × 流通股本 × 10000) | 当日 | 越高越好（偏大盘） |
| **VOL_PRICE_DIV** | 量价背离 | 趋势背离检测（见下） | 20 交易日 | 越高越好 |
| **FREE_FLOAT_PCT** | 自由流通比例 | free_share / total_share | 当日 | 越高越好 |
| **AMIHUD_ILLIQ** | Amihud 非流动性 | mean(\|pct_chg\| / (close × amount), 21D) | 21 交易日 | **反向** |
| **BAB_BETA** | Beta（Betting Against Beta） | OLS β(r_i, r_CSI300), 252D | 252 交易日 | **反向** |
| **RSI_14** | 14 日 RSI | 标准 Wilder RSI(14) | 14 交易日 | **反向** |
| **MAX_RET** | 最大日收益 | mean(top-5 daily returns, 35D) | 35 交易日 | **反向** |

反向因子 = 值越低越好，权重为负数。TURN_20D、VOL_20D、PRICE_DEV_60D、PROFIT_STB、ACCRUALS、AMIHUD_ILLIQ、BAB_BETA、RSI_14、MAX_RET 为反向因子。

**PRICE_DEV_60D** 使用前复权价格 `adj_close = close × adj_factor` 计算 MA60 和偏离度，避免除权除息导致均线失真。

**VOL_PRICE_DIV 趋势背离公式**（正向因子，高值 = 背离 = 反转信号强，向量化实现）：
1. 20D 累计收益 `prod(1 + pct_chg/100) - 1` → 价格趋势方向
2. 20D 成交量 OLS 斜率（向量化 `cov(t, vol) / var(t)`，标准化除以均量） → 量能趋势方向
3. 当价格方向与量能方向不一致时，divergence = |price_trend|；否则 = 0
4. 量增价跌 / 量缩价升 → 高值 → 反转信号
5. 数据不足（< 10 个交易日）→ NaN

**新增技术因子**（2026-08-28）：

- **FREE_FLOAT_PCT**：自由流通股本 / 总股本，衡量股票流动性。数据来自 `stock_basic.free_share / total_share`，缺失时返回 NaN
- **AMIHUD_ILLIQ**（反向）：Amihud (2002) 非流动性指标，`mean(|pct_chg/100| / (close × amount × 1000), 21D)`。值越大表示流动性越差。使用 21 个交易日窗口，不足时返回 NaN
- **BAB_BETA**（反向）：Frazzini-Pedersen "Betting Against Beta" 因子，个股日收益率对 CSI 300 日收益率做 OLS 回归的 β 系数（252 日窗口，最少 120 日）。低 beta 股票长期跑赢高 beta 股票（低杠杆效应）。数据不足时返回 NaN
- **RSI_14**（反向）：Wilder 相对强弱指标，14 日窗口。RSI 过高表示超买，作为反向因子使用。数据不足时返回 NaN
- **MAX_RET**（反向）：35 个交易日内最高的 5 个日收益率的均值（Han et al. 2021）。高 MAX_RET 股票往往未来收益较低（彩票偏好效应）。不足 5 个交易日时返回 NaN

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
- **get_daily_score() 返回值**: 包含 `n_articles` 列（各行业在窗口期内的文章计数），供策略层判断信号质量和触发动态权重调整
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

动量因子中 IND_MOM/CMDTY_MOM 本质是行业级信号，full 中性化的 OLS 行业哑变量回归会将行业效应完全回归掉，导致行业轮动信号归零。宏观/舆情因子同理（行业 beta / 行业级情感映射）。因此默认按大类覆盖：

| 大类 | 默认中性化模式 | 原因 |
|------|--------------|------|
| momentum | `size_only` | IND_MOM/CMDTY_MOM 行业信号需保留 |
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

### 5.1 大类合成评分

39 个因子分为 7 个大类，评分分两层：

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
| **value** | EP, BP, DIV_YIELD | 0.7 | 11.5% |
| **quality** | ROE_TTM, GROSS_MARGIN, PROFIT_STB, MARGIN_TREND, PIOTROSKI_F, ACCRUALS | 1.3 | 21.3% |
| **growth** | NET_PROFIT_YOY, REVENUE_YOY, NET_PROFIT_CAGR_3Y, REVENUE_ACCELERATION, GROSS_MARGIN_CHG | 1.0 | 16.4% |
| **momentum** | MOM_1M, MOM_3M, MOM_12M, REV_5D, IND_MOM, RESIDUAL_MOM, CMDTY_MOM, PRICE_52W_HIGH | 0.9 | 14.8% |
| **technical** | TURN_20D, VOL_20D, PRICE_DEV_60D, SIZE, VOL_PRICE_DIV, FREE_FLOAT_PCT, AMIHUD_ILLIQ, BAB_BETA, RSI_14, MAX_RET | 0.7 | 11.5% |
| **macro** | MACRO_CYCLE, MACRO_LIQD, MACRO_INFL, MACRO_EXTR | 0.6 | — |
| **sentiment** | POLICY_SENT, POLICY_INTENSITY, ANALYST_RATING, ANALYST_COVERAGE | 0.6 | — |

设计目的（Phase 21 优化）：质量主导（1.3）最高权重防守；价值降权（1.0→0.7）避免价值陷阱（地产等低估值结构性下行行业）；动量提升（0.8→0.9）增强趋势跟踪过滤能力；成长/技术/宏观/舆情不变。

### 5.3 因子级权重（类内）

| 因子 | 权重 | 说明 |
|------|------|------|
| EP, BP | 1.0 | 价值基准 |
| DIV_YIELD | 0.8 | 股息率（高分红偏好） |
| MOM_1M | 0.6 | 1月动量（降权，噪音大） |
| MOM_3M | 0.8 | 3月动量（适度降权） |
| MOM_12M | 1.0 | 12-1月动量 |
| ROE_TTM, GROSS_MARGIN | 1.0 | 质量基准 |
| TURN_20D | **-0.5** | 反向，回避高换手（降低惩罚） |
| VOL_20D | **-0.6** | 反向，加强低波偏好 |
| PRICE_DEV_60D | **-0.4** | 反向，加强超跌保护 |
| REV_5D | 0.7 | 短期反转信号（提高权重） |
| PROFIT_STB | **-0.5** | 反向，偏好稳定 |
| MARGIN_TREND | 0.4 | 毛利趋势改善 |
| SIZE | 0.3 | 偏中大盘 |
| IND_MOM | 0.8 | 行业轮动 |
| NET_PROFIT_YOY | 1.0 | 成长性 |
| REVENUE_YOY | 0.8 | 营收增长 |
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
| PIOTROSKI_F | 0.8 | 财务健康综合评分（质量补充） |
| ACCRUALS | **-0.5** | 反向，盈利质量（低应计 = 高质量） |
| REVENUE_ACCELERATION | 0.6 | 营收增速边际变化 |
| GROSS_MARGIN_CHG | 0.5 | 毛利率趋势变化 |
| PRICE_52W_HIGH | 0.7 | 趋势强度（接近新高） |
| FREE_FLOAT_PCT | 0.3 | 流动性辅助信号 |
| AMIHUD_ILLIQ | **-0.5** | 反向，非流动性惩罚 |
| BAB_BETA | **-0.6** | 反向，低 beta 偏好（BAB 效应） |
| RSI_14 | **-0.4** | 反向，超买惩罚 |
| MAX_RET | **-0.3** | 反向，彩票偏好惩罚 |

权重回退链：`DB行业配置 → __DEFAULT__ 配置 → 代码硬编码权重`

### 5.4 选股规则

依次执行（顺序与代码一致）：

1. **核心财务准入过滤**：EP/BP/ROE_TTM/GROSS_MARGIN 全部缺失的股票剔除
2. 剔除综合得分为 NaN 的股票（因子全缺失）
3. **价值陷阱惩罚**：value 大类得分 > 0 且 quality 大类得分 < -0.5 时，value 得分 × penalty（penalty = clip(1.5 + quality, 0.3, 1.0)），质量越差惩罚越重
4. **趋势门槛过滤**：MOM_12M < -1.0（底部 ~16%）的股票得分乘以衰减系数（penalty = clip(1.0 + 0.3×MOM_12M, 0.3, 0.7)），防止买入持续下跌股
5. **排除涨停股**（不可买入）
6. 换手惩罚加分（若 `TURNOVER_PENALTY_LAMBDA > 0`，已持仓股 +λ）
7. 按综合得分降序排列
8. 过滤 `score < MIN_SELECT_SCORE`（默认 0）
9. 取前 `MAX_HOLDINGS` 只（默认 20）
10. 允许空仓（无股票达标时持现金）

### 5.5 财务数据时效性衰减

> 方法: `multi_factor.py::_apply_financial_staleness_decay()`

财务数据发布后随时间推移信息价值递减。对依赖 `financial_data` 的 9 个因子（EP, BP, DIV_YIELD, ROE_TTM, GROSS_MARGIN, PROFIT_STB, MARGIN_TREND, NET_PROFIT_YOY, REVENUE_YOY）施加时效性衰减：

| 报告期距今 | 衰减系数 | 说明 |
|-----------|---------|------|
| ≤ 3 个月 | 100% | 最新季报，完全信任 |
| 3-6 个月 | 50% | 一季度前的数据，减半 |
| 6-9 个月 | 25% | 半年前的数据，大幅衰减 |
| > 9 个月 | 设为 -1.0 | **负面信号**：延迟发布季报或不发季报视为负面 |

- 以 `financial_data.end_date`（报告期）为基准计算时效（非 `ann_date`），避免早发布的公司被误判
- 衰减作用于标准化后的因子 Z-score（乘以衰减系数），保持因子间可比性
- 每只股票独立计算（不同股票可能使用不同季度的财报）
- NET_PROFIT_CAGR_3Y 不受影响（长期指标本身就是多年数据）

### 5.6 缺失因子惩罚

> 方法: `multi_factor.py::_apply_missing_factor_penalty()`

当股票缺失过多因子时，动态分母机制可能导致得分虚高。缺失因子惩罚机制线性压缩这些股票的最终得分：

```
missing_ratio = 缺失因子数 / 总因子数
if missing_ratio > MISSING_FACTOR_THRESHOLD (默认 0.20):
    penalty = 1.0 - (missing_ratio - threshold) / (1.0 - threshold) × MAX_PENALTY
    final_score × = penalty
```

- `MISSING_FACTOR_THRESHOLD = 0.20`：缺失 ≤ 20% 因子不惩罚
- `MISSING_FACTOR_MAX_PENALTY = 0.5`：最大惩罚 50%（100% 因子缺失时）
- 惩罚作用于最终综合得分（在大类合成之后）

### 5.7 仓位分配 — Softmax 权重

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

### 5.8 舆情动态权重提升（可选）

> 方法: `multi_factor.py::_adjust_sentiment_weight()`

当某些行业在窗口期内文章数量异常集中时（z-score > `SENTIMENT_SURGE_ZSCORE`），自动提升 sentiment 大类权重，放大集中报道行业的舆情信号：

```
行业文章分布 z-score = (n_articles_i - mean) / std
if z > SENTIMENT_SURGE_ZSCORE:
    sentiment_weight *= SENTIMENT_SURGE_MULTIPLIER
```

**默认禁用**（`SENTIMENT_SURGE_MULTIPLIER=1.0`），原因：当前数据源（CCTV 等政府新闻）行业区分度不足，启用后可能放大噪音。待接入更多行业细分数据源后可开启。

配置参数：
- `SENTIMENT_SURGE_MULTIPLIER`（默认 1.0，即禁用；建议开启值 1.3~1.5）
- `SENTIMENT_SURGE_ZSCORE`（默认 1.5，触发阈值）

### 5.9 换手惩罚（可选）

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
原始选股结果 → 流动性过滤 → 个股上限 → 行业上限 → 关联行业组上限 → 归一化 → 回撤缩仓/波动率目标
```

### 7.2 限制规则

| 规则 | 参数 | 说明 |
|------|------|------|
| 个股上限 | `MAX_SINGLE_WEIGHT = 12%` | 超限部分按比例分配给其他持仓，迭代至收敛（最多 10 轮） |
| 行业上限 | `MAX_INDUSTRY_WEIGHT = 20%` | 超限行业内所有股票等比例缩减 |
| 关联行业组上限 | `MAX_INDUSTRY_GROUP_WEIGHT = 30%` | 同一产业链（如地产链=房地产+建筑装饰+建筑材料）合计不超过上限 |
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

### 8.1 检测逻辑（渐进式切换）

```
deviation = (close(CSI300) - MA60(CSI300)) / MA60(CSI300)

deviation ≥ +5%  → strength = 1.0（纯牛）
deviation ≤ -5%  → strength = 0.0（纯熊）
中间              → strength = 线性插值 [0, 1]

每个大类权重 = bull_weight × strength + bear_weight × (1 - strength)
```

数据不足时回退到 bull（strength=1.0）。渐进式切换避免了 MA 附近的频繁二元跳变（whipsaw）。

### 8.2 熊市大类权重覆盖

| 大类 | 牛市权重 | 熊市权重 | 说明 |
|------|---------|---------|------|
| momentum | 0.9 | 0.6 | 保留趋势过滤能力（避免关闭动量安全阀） |
| quality | 1.3 | 1.5 | 提高质量防御 |
| growth | 1.0 | 0.8 | 适度保留成长信号 |
| value | 0.7 | 0.6 | 降低价值暴露（避免熊市价值陷阱） |
| technical | 0.7 | 1.0 | 提升防守因子信号 |
| macro | 0.6 | 0.6 | 不变 |
| sentiment | 0.6 | 0.6 | 不变 |

- 可通过 `REGIME_BEAR_OVERRIDES` 环境变量自定义（JSON 格式）
- `REGIME_ENABLED=0` 完全关闭 regime 切换
- MA 窗口 60 日（原 120 日），更快响应市场变化
- ±5% 过渡带实现渐进式权重调整，避免频繁切换

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
- **调仓频率**: 每 `REBALANCE_INTERVAL`（默认 10）个交易日 + 偏离度触发的自适应调仓
- **自适应调仓**: 每隔 `REBALANCE_CHECK_INTERVAL`（默认5）个交易日检查偏离度，Top-N 中新股票占比 ≥ `REBALANCE_DEVIATION_THRESHOLD`（默认40%）时触发额外调仓，两次调仓最短间隔 `REBALANCE_MIN_INTERVAL`（默认5）个交易日
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

### 9.9 回测性能优化

> 文件: `factors/base.py`, `factors/sentiment.py`, `factors/technical.py`, `strategy/multi_factor.py`

回测信号生成采用 **预加载 + 缓存** 架构，将单日因子计算从 ~5s 降至 ~2.1s：

**预加载阶段**（`FactorBase.preload_for_backtest()`，一次性）：
- `financial_data` 全量加载到内存（~191K 行）
- `daily_price` 按回测区间 +400 天加载（~2.6M 行）
- `policy_analysis` JOIN `policy_article` 按回测区间 +30 天加载（~6K 行）
- 预加载后，`get_price_history`/`get_latest_financial`/`get_close_on_date` 等自动从内存过滤

**因子级优化**：

| 优化 | 文件 | 效果 |
|------|------|------|
| VOL_PRICE_DIV 向量化 | `technical.py` | 1.0s → 0.05s（消除 `groupby.apply` 循环） |
| 舆情因子缓存 | `sentiment.py` | POLICY_INTENSITY 0.68s → 0.05s（`_get_sentiment_data` 共享缓存） |
| 舆情数据预加载 | `analyzer.py` | POLICY_SENT 0.97s → 0.015s（`_get_policy_analysis_fast` 内存过滤） |
| 舆情因子 dict 查找 | `sentiment.py` | O(n²) DataFrame 过滤 → O(1) dict 查找 |
| 股票池缓存 | `multi_factor.py` | `get_clean_universe` 结果按日期缓存在 `_date_cache` |

**信号生成流程**（`generate_signals()`）：
```
preload_for_backtest()（一次性 ~15s）
  → 逐日 _compute_scores_for_date()（~2.1s/日）
    → get_clean_universe()（~0.12s，缓存后）
    → 29 个因子 compute()（~1.9s，全部从内存过滤）
    → 因子处理 + 合成评分
  → 逐日 _select_from_scores()（<0.01s/日）
    → 换手惩罚 + Top-N + Softmax 权重
```

**基准数据**（1 年回测，25 个调仓日）：
- 总耗时：~68s（预加载 15s + 计算 53s）
- 单日平均：2.1s（含股票池构建、29 因子计算、因子处理、评分合成）

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
| MAX_HOLDINGS | 20 | MAX_HOLDINGS |
| MIN_SELECT_SCORE | 0.0 | MIN_SELECT_SCORE |
| REBALANCE_INTERVAL | 10 交易日 | REBALANCE_INTERVAL |
| REBALANCE_DEVIATION_THRESHOLD | 0.4 | REBALANCE_DEVIATION_THRESHOLD |
| REBALANCE_CHECK_INTERVAL | 5 | REBALANCE_CHECK_INTERVAL |
| REBALANCE_MIN_INTERVAL | 5 | REBALANCE_MIN_INTERVAL |
| TURNOVER_PENALTY_LAMBDA | 0.15 | TURNOVER_PENALTY_LAMBDA |
| NEUTRALIZE_MODE | full | NEUTRALIZE_MODE |
| NONLINEAR_SIZE | 0（关闭） | NONLINEAR_SIZE |
| MIN_VALID_CATEGORIES | 4 | MIN_VALID_CATEGORIES |
| CATEGORY_NEUTRALIZE_OVERRIDES | {"momentum":"size_only","macro":"size_only","sentiment":"size_only"} | CATEGORY_NEUTRALIZE_OVERRIDES |
| STANDARDIZE_MODE | zscore | STANDARDIZE_MODE |
| MISSING_FACTOR_THRESHOLD | 0.20 | MISSING_FACTOR_THRESHOLD |
| MISSING_FACTOR_MAX_PENALTY | 0.5 | MISSING_FACTOR_MAX_PENALTY |
| WEIGHT_TEMPERATURE | 2.0 | WEIGHT_TEMPERATURE |
| REGIME_ENABLED | 1（开启） | REGIME_ENABLED |
| REGIME_MA_WINDOW | 60 | REGIME_MA_WINDOW |
| REGIME_INDEX_CODE | 000300.SH | REGIME_INDEX_CODE |
| REGIME_BEAR_OVERRIDES | {"momentum":0.6,"quality":1.5,"growth":0.8,"value":0.6,"technical":1.0} | REGIME_BEAR_OVERRIDES |

### 风控

| 参数 | 默认值 | 环境变量 |
|------|--------|---------|
| MAX_SINGLE_WEIGHT | 0.12 | MAX_SINGLE_WEIGHT |
| MAX_INDUSTRY_WEIGHT | 0.20 | MAX_INDUSTRY_WEIGHT |
| MAX_INDUSTRY_GROUP_WEIGHT | 0.30 | MAX_INDUSTRY_GROUP_WEIGHT |
| DD_START_THRESHOLD | 0.10 | DD_START_THRESHOLD |
| DD_MAX_THRESHOLD | 0.25 | DD_MAX_THRESHOLD |
| DD_MIN_POSITION | 0.50 | DD_MIN_POSITION |
| MIN_DAILY_TURNOVER | 5000 万 | — |
| USE_VOL_TARGETING | 1（开启） | USE_VOL_TARGETING |
| TARGET_VOL | 0.18 | TARGET_VOL |
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
| PAPER_INITIAL_CAPITAL | 500,000 元 | PAPER_INITIAL_CAPITAL |
| BACKTEST_INITIAL_CAPITAL | 500,000 元 | BACKTEST_INITIAL_CAPITAL |
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

### 商品因子

| 参数 | 默认值 | 环境变量 |
|------|--------|---------|
| COMMODITY_SURGE_ZSCORE | 2.0 | COMMODITY_SURGE_ZSCORE |
| COMMODITY_SURGE_MULTIPLIER | 1.5 | COMMODITY_SURGE_MULTIPLIER |
| COMMODITY_SURGE_LOOKBACK | 500（交易日） | COMMODITY_SURGE_LOOKBACK |

### 舆情因子

| 参数 | 默认值 | 环境变量 |
|------|--------|---------|
| SENTIMENT_LOOKBACK_DAYS | 7 | SENTIMENT_LOOKBACK_DAYS |
| SENTIMENT_DECAY | 0.3 | SENTIMENT_DECAY |
| SENTIMENT_LLM_THRESHOLD | 0.5 | SENTIMENT_LLM_THRESHOLD |
| SENTIMENT_SURGE_MULTIPLIER | 1.0（禁用） | SENTIMENT_SURGE_MULTIPLIER |
| SENTIMENT_SURGE_ZSCORE | 1.5 | SENTIMENT_SURGE_ZSCORE |
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

### 11.3 财经媒体层（Tier 6）

通过 AKShare 接口采集 3 家主流财经媒体快讯，用于捕捉 AI 革命、黄金飙升等市场热点：

| 来源 | AKShare 接口 | 说明 |
|------|-------------|------|
| eastmoney | `stock_info_global_em()` | 东方财富全球财经快讯 |
| cls | `stock_info_global_cls(symbol='全部')` | 财联社快讯 |
| sina | `stock_info_global_sina()` | 新浪财经全球快讯 |

**架构设计：**
- 遵循 CCTV 爬虫的 AKShare 模式：继承 `BaseScraper`，重写 `scrape_pages()`/`scrape()`
- `fetch_content=False`，`list_urls=[]`（纯 API 接口，无 HTML 解析）
- 按天分批 yield，`max_pages` 复用为回看天数
- 东方财富 URL 来自 API 返回的链接列；财联社/新浪 URL 基于内容 hash 生成
- 财联社标题列可能为空，取内容前 100 字做标题；新浪无标题列，取内容前 50 字
- `TIER_WEIGHTS[6] = 0.6`，与舆情大类权重一致

### 11.4 预测市场层（Tier 8）

将 Polymarket 预测市场的 Spike 告警桥接到舆情因子管道，无需重新跑 LLM 分析。

**数据流：**
```
polymarket_alert (实时监控 + 回测引擎产出，含 LLM 分析结果)
  → PolymarketScraper.scrape_pages() 读取 alert
  → policy_article (source="polymarket", tier=8)
  → policy_analysis (直接从 alert 的 llm_sentiment/industries/stocks 注入，analysis_type="llm")
  → get_daily_score() / get_daily_stock_score() 自动拾取
  → POLICY_SENT / POLICY_INTENSITY 因子
```

**关键设计：**
- `PolymarketScraper` 继承 `BaseScraper`，`fetch_content=False`，`list_urls=[]`
- 从 `polymarket_alert` 表读取有 `llm_summary` 和 `llm_sentiment` 的 alert
- 转换为 `policy_article` 格式，附带 `_analysis` 元数据
- `SentimentDownloader` 识别 `_analysis` 元数据，写入 article 后立即注入 `policy_analysis`
- `SKIP_ANALYSIS_SOURCES = {"polymarket"}`，analyzer 跳过已自带分析的文章
- 回测引擎 `_replay_market()` 生成的 alert 同步持久化到 `polymarket_alert` 表
- `TIER_WEIGHTS[8] = 0.8`（金融预测市场信号质量高）
- `max_pages` 复用为回看天数

---

## 12. 因子分析验证（2026-08-28）

> 分析区间：2021-01-01 ~ 2026-08-29，66 个月度截面，前复权 21 日前瞻收益
> 方法：Spearman Rank IC + Fama-MacBeth 回归
> 显著性标准：Harvey-Liu-Zhu (2016) \|t\| > 3.0

### 12.1 Fama-MacBeth 回归结果

| 因子 | 大类 | mean γ | t-stat | 显著性 |
|------|------|--------|--------|--------|
| **REV_5D** | momentum | -0.00459 | -3.42 | *** |
| **VOL_PRICE_DIV** | technical | -0.00178 | -3.23 | *** |
| **TURN_20D** | technical | -0.00424 | -3.13 | *** |
| **SIZE** | technical | -0.00844 | -3.12 | *** |
| AMIHUD_ILLIQ | technical | 0.00418 | 2.54 | ** |
| MOM_12M | momentum | 0.12938 | 2.21 | * |
| RESIDUAL_MOM | momentum | -0.12769 | -2.17 | * |
| BAB_BETA | technical | 0.00315 | 1.77 | |
| REVENUE_ACCELERATION | growth | 0.00055 | 1.64 | |
| REVENUE_YOY | growth | 0.00063 | 1.52 | |
| PRICE_52W_HIGH | momentum | 0.00172 | 1.42 | |
| BP | value | 0.00095 | 0.93 | |
| MOM_3M | momentum | -0.00180 | -0.80 | |
| PIOTROSKI_F | quality | -0.00035 | -0.75 | |
| MOM_1M | momentum | -0.00125 | -0.60 | |
| RSI_14 | technical | 0.00071 | 0.56 | |
| GROSS_MARGIN_CHG | growth | 0.00011 | 0.50 | |
| MARGIN_TREND | quality | 0.00011 | 0.50 | |
| VOL_20D | technical | -0.00057 | -0.36 | |
| DIV_YIELD | value | 0.00020 | 0.36 | |
| NET_PROFIT_CAGR_3Y | growth | -0.00008 | -0.26 | |
| MAX_RET | technical | -0.00025 | -0.18 | |
| PRICE_DEV_60D | technical | 0.00014 | 0.04 | |
| EP | value | -0.00003 | -0.03 | |
| IND_MOM | momentum | 0.00002 | 0.03 | |
| ROE_TTM | quality | 0.00001 | 0.02 | |
| GROSS_MARGIN | quality | -0.00001 | -0.01 | |

> \* |t|>1.96, \*\* |t|>2.58, \*\*\* |t|>3.0 (Harvey-Liu-Zhu)

### 12.2 IC 摘要（Spearman Rank IC）

| 因子 | 均值 IC | ICIR | IC>0 占比 |
|------|---------|------|----------|
| REV_5D | -0.0393 | -0.42 | 36.4% |
| VOL_PRICE_DIV | -0.0354 | -0.40 | 33.3% |
| TURN_20D | -0.0339 | -0.39 | 34.8% |
| SIZE | -0.0288 | -0.38 | 36.4% |
| AMIHUD_ILLIQ | 0.0298 | 0.31 | 59.1% |
| BAB_BETA | 0.0214 | 0.22 | 56.1% |
| PRICE_52W_HIGH | 0.0177 | 0.17 | 53.0% |
| REVENUE_ACCELERATION | 0.0075 | 0.20 | 54.5% |

### 12.3 关键发现

1. **A 股反转效应显著**：REV_5D（|t|=3.42）和 TURN_20D（|t|=3.13）是最强信号，过去赢家未来表现差。MOM_1M/3M/12M 的 IC 均为负值，印证 A 股短期反转 > 动量的市场特征
2. **流动性/低波因子有效**：AMIHUD_ILLIQ（|t|=2.54）和 BAB_BETA（|t|=1.77）在 A 股有显著溢价，低流动性/低 beta 股票跑赢
3. **价值因子失效**：EP（t=-0.03）、BP（t=0.93）、DIV_YIELD（t=0.36）均不显著，传统价值投资在 A 股 2021-2026 区间无明显 alpha
4. **质量/成长因子分化**：ROE_TTM/GROSS_MARGIN 不显著，但 REVENUE_ACCELERATION（t=1.64）有边际预测力
5. **新增 10 因子中 4 个有统计意义**：AMIHUD_ILLIQ（**|t|=2.54**）最显著，BAB_BETA/PRICE_52W_HIGH/REVENUE_ACCELERATION 方向正确但未达 3.0 阈值

### 12.4 前瞻误差审计

对 12 个显著因子（|t| > 1.96）逐一审计前瞻偏差风险：

| 因子 | 数据类型 | 前瞻风险 | 结论 |
|------|---------|---------|------|
| REV_5D | 价格 | 无 — 使用 T-5 日已实现价格 | 安全 |
| VOL_PRICE_DIV | 价格+成交量 | 无 — 使用 T-20 日已实现数据 | 安全 |
| TURN_20D | 换手率 | 无 — 使用 T-20 日已实现换手率 | 安全 |
| SIZE | 价格+股本 | 无 — 使用 T 日数据 | 安全 |
| AMIHUD_ILLIQ | 价格+成交额 | 无 — 使用 T-21 日已实现数据 | 安全 |
| MOM_12M | 价格 | 无 — 使用 T-1M 到 T-12M 已实现价格 | 安全 |
| RESIDUAL_MOM | 价格 | 无 — 使用 T-20 日已实现数据 | 安全 |
| BAB_BETA | 价格 | 无 — 使用 T-252 日已实现收益率 | 安全 |
| REVENUE_ACCELERATION | 财务 | 潜在风险* — 依赖 fina_indicator，无 f_ann_date | 需关注 |
| BP | 财务 | 潜在风险* — 依赖 BPS，无 f_ann_date | 需关注 |
| PRICE_52W_HIGH | 价格 | 无 — 使用 T-380 日已实现价格 | 安全 |
| PIOTROSKI_F | 财务 | 潜在风险* — 依赖多期财务数据 | 需关注 |

> \* **结构性前瞻偏差**：财务数据表使用 UPSERT 语义，历史报告期数据可能被后续修正覆盖（point-in-time vs restated）。且缺少 `f_ann_date`（首次发布日期）和 `report_type` 字段，无法精确判断信息可获得时点。
>
> **影响范围**：仅影响 3 个财务因子（REVENUE_ACCELERATION, BP, PIOTROSKI_F），其中 BP 不显著（t=0.93），REVENUE_ACCELERATION 和 PIOTROSKI_F 也不显著（t=1.64, -0.75）。**所有统计显著的因子（|t|>3.0）均为纯价格因子，无前瞻偏差风险。**
>
> **修复方案**：需 Tushare 数据源增加 `f_ann_date` 字段 + 改用 append-only 存储 + 按 `report_type` 过滤首次披露数据。

### Cover 两资产再平衡探索（已封存，2026-10-08）

用户已决定停止该方向开发，不纳入生产策略。以下源码、命令和结果仅为历史研究档案，不属于后续开发计划。

新增独立 `quant --market cn universal-backtest`，用已有价格和交易日历比较通用组合、固定比例再平衡与买入持有，含交易费用和整手约束。方法、复现命令与执行近似见 [通用组合实验](UNIVERSAL_PORTFOLIO.md)。不改变现有选股策略。

`quant --market cn pair-validation` 扩展为历史两年筛选、次年验证的滚动研究，固定股票池与筛选规则，保留所有候选对、100 组随机对照、费用敏感性和理想基准。主检验区间为 2022–2025；详细协议和结果见同一实验文档。

本轮固定协议未通过：每日等权、通用组合累计净收益分别为 3.70%、4.18%，同组持有为 5.35%，两种策略均只有两年正超额，随机配对比较接近中位数。该结果不支持将这套配对再平衡规则纳入生产策略；报告保留一组选中组合的年末报价风险。

### 12.5 回测性能对比

| 指标 | 旧版（29 因子） | 新版（39 因子） |
|------|----------------|----------------|
| 总收益 | -28.40% | **+13.41%** |
| 年化收益 | — | 正值 |
| 最大回撤 | — | 改善 |

> 新增因子（尤其 AMIHUD_ILLIQ、BAB_BETA、REV_5D 的反向信号）显著提升了选股质量，回测从亏损转为盈利。
