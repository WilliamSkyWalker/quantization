# A股量化投资系统 - 续接 Prompt

> 每次开启新会话时，将下方对应阶段的 Prompt 发送给 AI 即可无缝继续。
> 算法细节请参阅 `ALGORITHM.md`。

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

算法文档：请先阅读 ALGORITHM.md 了解完整算法设计（19 因子、5 大类评分、Z-score clip、中性化模式等）。
请先阅读项目现有代码再动手修改，不要凭空猜测已有实现。
```

---

## 当前进度

| 阶段 | 模块 | 状态 |
|------|------|------|
| Phase 1 数据层 | 配置 / ORM / 下载 / 清洗 / 更新 / income 补全 | ✅ 完成 |
| Phase 2 因子层 | 19 个因子（5 大类）+ 处理流水线 + IC/ICIR 评估 | ✅ 完成 |
| Phase 3 策略层 | 分类复合评分选股 + 回测引擎（一字板排队） | ✅ 完成 |
| Phase 4 风控层 | 个股/行业上限 + 回撤降仓 / 波动率目标管理 | ✅ 完成 |
| Phase 5 执行层 | 本地模拟盘 PaperTrader + BaseTrader 抽象 | ✅ 完成 |
| Phase 5 执行层 | 券商实盘对接 (QMT/Ptrade) | 📋 待实现 |
| Phase 6 监控层 | 绩效追踪 + HTML 报告生成 | ✅ 完成 |
| Phase 7 因子增强 | 防守型 + 质量增强 + 效率因子（7 个新因子） | ✅ 完成 |
| Phase 8 算法升级 | 4 新因子 + 分类评分 + Z-score clip + income 补全 | ✅ 完成 |
| Phase 8 舆情层 | 政策新闻采集流水线（11 源，9/11 可用） | ✅ 完成 |
| Phase 8 舆情层 | LLM 政策解读 + 舆情因子化 | 📋 待实现 |
| 远期规划 | 行业轮动数据深度接入 | 📋 备忘 |

---

## Phase 8 算法升级（已完成）摘要

详细算法见 `ALGORITHM.md`，核心改动：

1. **4 个新因子**：NET_PROFIT_YOY、REVENUE_YOY（成长）、RESIDUAL_MOM（残差动量）、VOL_PRICE_DIV（量价背离）
2. **分类复合评分**：19 因子分 5 大类（价值/质量/成长/动量/技术），类内动态分母 + 类间固定分母（4.5）
3. **核心财务准入过滤**：缺失全部 EP/BP/ROE_TTM/GROSS_MARGIN 的股票剔除
4. **Z-score clip ±3**：防止中性化后残差极端值主导得分
5. **中性化模式可配**：full（行业+市值）/ size_only（仅市值）/ none
6. **Income API 补全**：Tushare `fina_indicator` 丢失 revenue/net_profit，改用 `income` 接口补充
7. **波动率目标管理**：可选替代回撤缩仓（`USE_VOL_TARGETING=1`）
8. **一字板排队卖出**：跌停无法卖出的股票进入 pending_sells 队列，次日继续尝试

---

## CLI 命令速查

```bash
# 数据下载
python3 main.py download_all                       # 全量下载（股票列表+日线+沪深300指数）
python3 main.py download_index                     # 单独下载沪深300指数日线
python3 main.py download_extra                     # 一键下载财务+估值+行业
python3 main.py download_financial                  # 单独下载财务数据（含 income API 补全）
python3 main.py backfill_income                     # 回填已有记录中缺失的 revenue/net_profit
python3 main.py update_all                         # 增量更新全部

# 行业配置（种子数据）
python3 main.py seed_industry_config               # 初始化行业因子配置表
python3 main.py show_industry_config               # 查看行业因子权重配置

# 选股与回测
python3 main.py select 2025-12-31                  # 单日选股信号（全行业显示）
python3 main.py backtest 2025-01-01 2025-12-31     # 指定区间回测（基准=沪深300）

# 舆情抓取
python3 main.py download_sentiment                 # 全量抓取 11 个政府网站
python3 main.py download_sentiment --source=csrc   # 单源抓取
python3 main.py download_sentiment --tier=3        # 按层级抓取
python3 main.py update_sentiment                   # 增量更新
python3 main.py sentiment_status                   # 各来源文章数和最新日期

# 模拟盘
python3 main.py trade                              # 执行当日交易
python3 main.py position                           # 查看持仓
python3 main.py paper_replay 2020-01-01 2024-12-31 --reset  # 回放历史交易
python3 main.py paper_nav                          # 查看净值历史
python3 main.py paper_transactions --last 50       # 查看交易记录
python3 main.py paper_reset --confirm              # 重置模拟账户
```

---

## 项目文件结构

```
quant_system/
├── config/settings.py               # 全局配置 + .env 加载
├── data/
│   ├── database.py                  # ORM表定义(8张表) + DatabaseManager + 自动列迁移
│   ├── downloader.py                # Tushare 数据下载
│   ├── cleaner.py                   # 数据清洗 + 股票池构建
│   ├── updater.py                   # 财务数据 + 行业分类 + income API 补全
│   └── seed_config.py               # 行业因子配置种子数据
├── factors/
│   ├── base.py                      # 因子基类 ABC + TTM/收盘价/总股本/TTM营收 工具方法
│   ├── value.py                     # EP, BP
│   ├── momentum.py                  # MOM_1M, MOM_3M, MOM_12M, REV_5D, RESIDUAL_MOM
│   ├── quality.py                   # ROE_TTM, GROSS_MARGIN, PROFIT_STB, MARGIN_TREND
│   ├── technical.py                 # TURN_20D, VOL_20D, PRICE_DEV_60D, SIZE, IND_MOM, VOL_PRICE_DIV
│   ├── growth.py                    # NET_PROFIT_YOY, REVENUE_YOY
│   ├── processor.py                 # 去极值 → 中性化(full/size_only/none) → Z-score → clip ±3
│   └── evaluation.py                # IC/ICIR/分层回测
├── strategy/
│   ├── multi_factor.py              # 分类复合评分 + Score 比例权重 + 换手惩罚
│   └── backtest.py                  # 回测引擎 + 一字板排队卖出
├── risk/
│   └── risk_manager.py              # 风控（个股/行业上限 + 回撤降仓/波动率目标）
├── execution/
│   ├── base_trader.py               # 交易执行器 ABC
│   ├── paper_trader.py              # 本地模拟盘
│   └── gm_trader.py                 # 掘金接口（已停服，保留参考）
├── monitor/
│   ├── performance.py               # 绩效追踪 + 归因
│   └── report.py                    # HTML 报告生成
├── sentiment/
│   ├── models.py                    # ORM: policy_article + scrape_log
│   ├── base_scraper.py              # HttpRateLimiter + BaseScraper ABC
│   ├── downloader.py                # SentimentDownloader 编排器
│   └── scrapers/                    # 11 个政府网站爬虫
├── tests/                           # 129个测试用例
├── main.py                          # CLI 入口
├── ALGORITHM.md                     # 完整算法设计文档
└── .env                             # 本地配置（不入库）
```

---

## 待实现续接 Prompt

### Phase 5：券商实盘对接（QMT / Ptrade）

```
继续 A 股量化投资系统 Phase 5 — 券商实盘对接。

本地模拟盘已完成，现需接入真实券商的模拟盘或实盘。
系统已预留 BaseTrader 抽象基类和 TRADER_TYPE 配置项。

请先读取 ALGORITHM.md 了解算法设计，再读取：
- quant_system/execution/base_trader.py（必须实现的接口）
- quant_system/execution/paper_trader.py（参考实现）
- quant_system/main.py（_create_trader 工厂函数）

接入新券商只需：
1. 新建 execution/qmt_trader.py — QMTTrader(BaseTrader)
2. 在 main.py _create_trader() 中添加 qmt 分支
3. .env 中设置 TRADER_TYPE=qmt + QMT 连接参数
```

### Phase 8-A：修复 JS 渲染网站爬虫

```
继续 A 股量化投资系统 Phase 8-A — 修复 miit/nfra 爬虫（JS 渲染网站）。

请先读取：
- quant_system/sentiment/base_scraper.py
- quant_system/sentiment/scrapers/miit.py
- quant_system/sentiment/scrapers/nfra.py

方案选择：找到后端 API（推荐）或使用 Playwright headless browser。
```

### Phase 8-B：LLM 政策解读 + 行业关联

```
继续 A 股量化投资系统 Phase 8-B — 用 LLM 对政策新闻做结构化解读。

请先读取 ALGORITHM.md 了解算法设计，再读取：
- quant_system/sentiment/models.py
- quant_system/data/database.py

需完成：
1. sentiment/models.py 新增 policy_analysis 表
2. sentiment/analyzer.py — LLM 政策解读器（OpenAI 兼容 API）
3. data/database.py 新增查询方法
4. main.py 新增 analyze_sentiment 命令
```

### Phase 8-C：舆情因子化

```
继续 A 股量化投资系统 Phase 8-C — 将政策舆情转化为量化因子。

前置条件：Phase 8-B LLM 分析层完成。

请先读取 ALGORITHM.md 和 factors/base.py 了解因子框架。

需完成：
1. factors/sentiment.py — POLICY_SENT + POLICY_INTENSITY
2. 在 multi_factor.py 注册新因子
3. 防未来函数验证：只使用 publish_date <= date 的文章
```

### 行业数据深度接入（远期）

```
现有行业数据仅用于中性化，尚未作为独立选股信号。
可扩展：行业景气度因子、行业拥挤度、行业轮动模型。
数据源：Tushare 申万行业指数 index_daily。
```

---

## Debug 用 Prompt

```
A股量化系统遇到问题，请帮我排查。

项目路径：/Users/daweilun/Documents/quantization/quant_system
数据库：本地 MySQL（库名 quant）
算法文档：ALGORITHM.md

问题描述：
[在这里描述错误信息和复现步骤]

请先读取相关代码文件，定位问题根因，然后给出修复方案并直接修改代码。
```

---

## 已完成优化

- [x] 股票列表下载优化：批量获取上市日期（~2秒 vs 原来 ~42分钟）
- [x] 日线行情多线程并发下载（DOWNLOAD_WORKERS 可配，默认8线程）
- [x] DB批量写入 + 增量更新多线程并发 + 批量 bulk_upsert
- [x] 估值快照批量 CASE WHEN UPDATE
- [x] 完整单元测试覆盖：129个测试用例全部通过（SQLite in-memory）
- [x] DatabaseManager 兼容 SQLite/MySQL（连接池参数自动适配）
- [x] 修复 datetime64 vs date 类型比较问题（cleaner.py）
- [x] 修复风控降仓后归一化失效问题（risk_manager.py）
- [x] 本地模拟盘 PaperTrader 替代已停服的掘金量化
- [x] BaseTrader 抽象基类，预留 QMT/Ptrade 券商切换接口
- [x] T+1信号执行修复（消除前视偏差）
- [x] 修复流动性过滤单位不匹配（DB amount 列为千元）
- [x] 修复增量更新时区报错
- [x] 修复估值快照更新仅匹配2条（改用 INNER JOIN 子查询）
- [x] 本地计算 PE_TTM/PB/市值，消除前视偏差（value.py + base.py）
- [x] 修复回测短区间空仓问题（自动回溯前月调仓日）
- [x] 沪深300指数作为回测基准
- [x] 排除科创板选股（EXCLUDE_STAR_MARKET 可配）
- [x] Phase 7 因子增强：7 个新因子（防守/质量增强/效率）
- [x] Phase 8 算法升级：4 新因子 + 分类复合评分 + Z-score clip ±3
- [x] Income API 补全：修复 Tushare fina_indicator 丢失 revenue/net_profit
- [x] 核心财务准入过滤：缺失全部核心财务指标的票剔除
- [x] 中性化模式可配（full/size_only/none）+ 非线性市值项
- [x] 一字板排队卖出机制（pending_sells）
- [x] 波动率目标管理（可选替代回撤缩仓）

## 已知待优化项

- [ ] 首次全量下载无断点续传，bulk_insert 重跑可能重复
- [ ] 前复权价格会随分红送股变化，需定期全量刷新
- [ ] MySQL 保留字 `open` 作列名，raw SQL 需加反引号
- [ ] 行业分类下载仍为串行（板块数 ~90，速度尚可）
- [ ] 券商实盘对接：QMT（华泰/国金）或 Ptrade（恒生）
- [ ] 模拟盘净值图表可视化（paper_nav --plot）
- [ ] miit/nfra 爬虫需 headless browser（JS 渲染网站）
- [ ] LLM 政策解读 + 舆情因子化（Phase 8-B/C）
