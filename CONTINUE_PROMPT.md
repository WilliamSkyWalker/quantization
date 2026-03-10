# A股量化投资系统 - 续接 Prompt

> 每次开启新会话时，将下方对应阶段的 Prompt 发送给 AI 即可无缝继续。
> 算法细节请参阅 `A_SHARE_STRATEGY.md`。

---

## 通用上下文（每次都贴）

```
你是一位资深量化工程师，正在帮我搭建 A 股量化投资系统。

项目路径：/Users/daweilun/Documents/quantization/backend
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

算法文档：请先阅读 A_SHARE_STRATEGY.md 了解完整算法设计（29 因子、7 大类评分、Z-score clip、中性化模式、Regime 切换等）。
请先阅读项目现有代码再动手修改，不要凭空猜测已有实现。
```

---

## 当前进度

| 阶段 | 模块 | 状态 |
|------|------|------|
| Phase 1 数据层 | 配置 / ORM / 下载 / 清洗 / 更新 / income 补全 | ✅ 完成 |
| Phase 2 因子层 | 29 个因子（7 大类）+ 处理流水线 + IC/ICIR 评估 | ✅ 完成 |
| Phase 3 策略层 | 分类复合评分选股 + 回测引擎（一字板排队） | ✅ 完成 |
| Phase 4 风控层 | 个股/行业上限 + 回撤降仓 / 波动率目标管理 | ✅ 完成 |
| Phase 5 执行层 | 本地模拟盘 PaperTrader + BaseTrader 抽象 | ✅ 完成 |
| Phase 5 执行层 | 券商实盘对接 (QMT/Ptrade) | 📋 待实现 |
| Phase 6 监控层 | 绩效追踪 + HTML 报告生成 | ✅ 完成 |
| Phase 7 因子增强 | 防守型 + 质量增强 + 效率因子（7 个新因子） | ✅ 完成 |
| Phase 8 算法升级 | 4 新因子 + 分类评分 + Z-score clip + income 补全 | ✅ 完成 |
| Phase 8 舆情层 | 政策新闻采集流水线（16 源：11 政府 + CCTV + 巨潮 + 3 Twitter） | ✅ 完成 |
| Phase 8 舆情层 | 美国政策 Twitter/X 采集（Trump/Vance/Rubio） | ✅ 完成 |
| Phase 8 舆情层 | LLM 政策解读 + 舆情因子化 + 券商研报因子 | ✅ 完成 |
| Phase 9 参数调优 | 大类/因子权重精调 + 风控参数放宽 + 环境变量可配 | ✅ 完成 |
| Phase 10 商品轮动 | 商品期货数据下载 + CMDTY_MOM 因子 + 行业联动 | ✅ 完成 |
| Phase 11 宏观因子 | 宏观经济数据接入 + 4 个宏观因子（MACRO_CYCLE/LIQD/INFL/EXTR） | ✅ 完成 |
| Phase 12 算法改进R2 | Regime切换 + Softmax权重 + 线性回撤 + Rank标准化 + CAGR因子 + 按大类中性化 + 权重再分配 | ✅ 完成 |
| Phase 13 Polymarket | 事件监控 + LLM 情感分析 + 回测引擎 | ✅ 完成 |
| Phase 14 美股数据 | S&P 500 + NASDAQ 100 数据接入（yfinance）+ 美股 P&L 回测 | ✅ 完成 |
| Phase 15 性能优化 | 因子计算向量化（TTM / 质量因子）+ SQL 查询优化 | ✅ 完成 |
| Phase 16 策略优化 | 降动量/升质量/渐进Regime/增持仓/波动率目标/换手惩罚（已被 Phase 21 进一步优化） | ✅ 完成 |
| Phase 17 财经媒体 | 东方财富/财联社/新浪财经快讯爬虫（AKShare），Tier 6 | ✅ 完成 |
| Phase 18 预测市场桥接 | Polymarket alert → 舆情管道（Tier 8），回测 alert 持久化 | ✅ 完成 |
| Phase 19 信号增强 | CMDTY_MOM 暴涨检测 + 舆情动态权重 + 市值过滤修复 + analyzer n_articles | ✅ 完成 |
| Phase 20 性能优化 | 预加载架构 + VOL_PRICE_DIV 向量化 + 舆情缓存/预加载 + 股票池缓存 | ✅ 完成 |
| Phase 21 回测优化 | 熊市Regime修复 + 行业集中度控制 + 大类权重调整 + 价值陷阱惩罚 + 趋势门槛过滤 | ✅ 完成 |
| 远期规划 | 行业轮动数据深度接入 | 📋 备忘 |

---

## Phase 8 算法升级（已完成）摘要

详细算法见 `A_SHARE_STRATEGY.md`，核心改动：

1. **4 个新因子**：NET_PROFIT_YOY、REVENUE_YOY（成长）、RESIDUAL_MOM（残差动量）、VOL_PRICE_DIV（量价背离）
2. **分类复合评分**：29 因子分 7 大类（价值/质量/成长/动量/技术/宏观/舆情），类内动态分母 + 类间动态分母（权重再分配）
3. **核心财务准入过滤**：缺失全部 EP/BP/ROE_TTM/GROSS_MARGIN 的股票剔除
4. **Z-score clip ±3**：防止中性化后残差极端值主导得分
5. **中性化模式可配**：full（行业+市值）/ size_only（仅市值）/ none
6. **Income API 补全**：Tushare `fina_indicator` 丢失 revenue/net_profit，改用 `income` 接口补充
7. **波动率目标管理**：可选替代回撤缩仓（`USE_VOL_TARGETING=1`）
8. **一字板排队卖出**：跌停无法卖出的股票进入 pending_sells 队列，次日继续尝试

---

## API 命令速查

所有操作通过 backend API 端点执行（后端需先启动: `./start.sh`）：

```bash
# 数据管理
curl -X POST http://localhost:8000/api/data/update              # 增量更新全部
curl -X POST http://localhost:8000/api/data/download-all        # 全量下载
curl -X POST http://localhost:8000/api/data/download-extra      # 财务+估值+行业
curl -X POST http://localhost:8000/api/data/backfill-income     # 回填利润表

# 选股与回测
curl -X POST http://localhost:8000/api/strategy/select \
  -H 'Content-Type: application/json' -d '{"date":"2025-12-31"}'
curl -X POST http://localhost:8000/api/strategy/backtest \
  -H 'Content-Type: application/json' -d '{"start_date":"2025-01-01","end_date":"2025-12-31"}'

# 舆情抓取
curl -X POST http://localhost:8000/api/sentiment/download       # 全量抓取 14 个来源
curl -X GET  http://localhost:8000/api/sentiment/status          # 各来源文章数

# 模拟盘
curl -X POST http://localhost:8000/api/paper/trade              # 执行当日交易
curl -X GET  http://localhost:8000/api/paper/position            # 查看持仓

# 报告
curl -X POST http://localhost:8000/api/report/generate \
  -H 'Content-Type: application/json' -d '{"start_date":"2020-01-01","end_date":"2024-12-31"}'

# 测试
cd backend && python -m pytest
```

---

## 项目文件结构

```
backend/
├── core/                            # Django 配置（settings, urls, asgi, wsgi）
├── api/
│   ├── urls.py                      # API 路由定义
│   └── views/                       # DRF 视图（data, trading, sentiment, report 等）
├── services/                        # 核心业务逻辑（原 quant_system/ 迁移）
│   ├── config/settings.py           # 全局配置 + .env 加载
│   ├── data/
│   │   ├── database.py              # ORM表定义 + DatabaseManager + 自动列迁移
│   │   ├── downloader.py            # Tushare 数据下载
│   │   ├── cleaner.py               # 数据清洗 + 股票池构建
│   │   ├── updater.py               # 财务数据 + 行业分类 + income API 补全
│   │   ├── akshare_downloader.py    # AKShare 数据下载（券商研报等）
│   │   ├── fmp_downloader.py        # 美股数据下载（S&P 500 + NASDAQ 100, yfinance）
│   │   ├── commodity_downloader.py  # 商品期货数据下载
│   │   ├── macro_downloader.py      # 宏观经济数据下载（8 个 Tushare API）
│   │   └── seed_config.py           # 行业因子配置种子数据
│   ├── factors/
│   │   ├── base.py                  # 因子基类 ABC + 向量化 TTM/收盘价/总股本 + preload_for_backtest
│   │   ├── value.py                 # EP, BP
│   │   ├── momentum.py              # MOM_1M, MOM_3M, MOM_12M, REV_5D, IND_MOM, RESIDUAL_MOM
│   │   ├── quality.py               # ROE_TTM, GROSS_MARGIN, PROFIT_STB, MARGIN_TREND（向量化）
│   │   ├── technical.py             # TURN_20D, VOL_20D, PRICE_DEV_60D, SIZE, VOL_PRICE_DIV（向量化）
│   │   ├── growth.py                # NET_PROFIT_YOY, REVENUE_YOY, NET_PROFIT_CAGR_3Y
│   │   ├── commodity.py             # CMDTY_MOM（商品轮动）
│   │   ├── macro.py                 # MACRO_CYCLE, MACRO_LIQD, MACRO_INFL, MACRO_EXTR（宏观因子）
│   │   ├── sentiment.py             # POLICY_SENT, POLICY_INTENSITY（舆情因子，共享缓存）
│   │   ├── research.py              # ANALYST_RATING, ANALYST_COVERAGE（券商研报因子）
│   │   ├── processor.py             # 去极值 → 中性化(full/size_only/none) → Z-score → clip ±3
│   │   └── evaluation.py            # IC/ICIR/分层回测
│   ├── strategy/
│   │   ├── multi_factor.py          # 分类复合评分 + Score 比例权重 + 换手惩罚
│   │   └── backtest.py              # 回测引擎 + 一字板排队卖出
│   ├── risk/
│   │   └── risk_manager.py          # 风控（个股/行业上限 + 回撤降仓/波动率目标）
│   ├── execution/
│   │   ├── base_trader.py           # 交易执行器 ABC
│   │   ├── paper_trader.py          # 本地模拟盘
│   │   └── gm_trader.py            # 掘金接口（已停服，保留参考）
│   ├── monitor/
│   │   ├── performance.py           # 绩效追踪 + 归因
│   │   └── report.py                # HTML 报告生成
│   ├── sentiment/
│   │   ├── models.py                # ORM: policy_article + policy_analysis + scrape_log
│   │   ├── base_scraper.py          # HttpRateLimiter + BaseScraper ABC
│   │   ├── downloader.py            # SentimentDownloader 编排器
│   │   ├── analyzer.py              # 两层分析调度（keyword + LLM）
│   │   ├── keyword_analyzer.py      # 关键词规则分析层
│   │   ├── llm_analyzer.py          # LLM 增强分析层（Anthropic/OpenAI）
│   │   └── scrapers/                # 16 个爬虫（11 政府 + CCTV + 巨潮 + 3 Twitter）
│   └── polymarket/
│       ├── models.py                # ORM: polymarket_event + polymarket_alert 等
│       ├── monitor.py               # Polymarket 实时监控
│       ├── alert_manager.py         # 告警管理
│       ├── event_analyzer.py        # LLM 事件分析（受影响 ticker/方向/置信度）
│       ├── backtester.py            # Polymarket 回测引擎
│       ├── history.py               # 历史数据下载
│       └── us_stock_backtester.py   # 美股 P&L 回测（告警 → 股价 → 收益统计）
├── tests/
├── manage.py                        # Django CLI 入口
├── requirements.txt
└── .env                             # 本地配置（不入库）
frontend/                            # Vue 3 + Naive UI 仪表盘（数据操作/文章列表/数据表状态 三个顶级 Tab）
start.sh                             # 一键启动 + crontab（cron 通过 curl 调用 API）
A_SHARE_STRATEGY.md                         # 完整算法设计文档
```

---

## Phase 9 参数调优（已完成）摘要

基于回测结果对因子权重和风控参数进行了系统性调优（已被 Phase 16 进一步优化）。

---

## Phase 16 策略优化（已完成）摘要

针对策略在 2025 年 5 月前持续跑输沪深 300 的问题，进行全面优化：

1. **大类权重重新平衡**：
   - 质量 1.0→1.2（提升防守）、动量 1.3→0.8（大幅降低趋势依赖）
   - 成长 1.2→1.0（避免熊市高估值陷阱）、技术 0.5→0.7（加强防守信号）
2. **因子权重精调**：
   - MOM_1M 1.0→0.6、MOM_3M 1.0→0.8（降低短期动量噪音）
   - VOL_20D -0.3→-0.6、PRICE_DEV_60D -0.15→-0.4（加强低波/超跌保护）
   - REV_5D 0.4→0.7（加强短期反转捕捉）、TURN_20D -1.0→-0.5（降低换手惩罚）
3. **渐进式 Regime 切换**：
   - MA 窗口 120→60（更快响应）
   - ±5% 过渡带线性插值（避免二元 whipsaw）
   - 扩大熊市覆盖范围：新增 growth:0.6, value:1.3, technical:1.0; momentum 0.6→0.3
4. **持仓分散化**：MAX_HOLDINGS 10→15（降低个股风险）
5. **换手惩罚**：TURNOVER_PENALTY_LAMBDA 0→0.15（减少无效调仓）
6. **波动率目标管理**：USE_VOL_TARGETING=1, TARGET_VOL=0.18（替代回撤降仓，更主动）
7. **舆情因子提权**：sentiment 大类 0.4→0.6
8. **修复行业轮动信号**：momentum 大类改为 size_only 中性化（IND_MOM/CMDTY_MOM 行业信号不再被回归掉）
9. **商品回看窗口**：COMMODITY_MOM_LOOKBACK 20→60（捕捉黄金等中期趋势）
10. **行业关键词大幅扩展**：计算机新增 AI/LLM 热词（ChatGPT/GPT/生成式AI/AI芯片等）；有色金属新增贵金属热词（黄金/金价/央行购金等）；电子新增 AI 硬件供应链（HBM/光模块/CoWoS 等）
11. **情感关键词扩展**：新增市场热点正面词（爆发/崛起/飙升/新高/风口等）和负面词（暴跌/泡沫/制裁/封锁等）

---

## Phase 19 信号增强（已完成）摘要

1. **CMDTY_MOM 商品暴涨检测**：`commodity.py` 新增 z-score 暴涨放大机制。基于历史滚动动量分布（`COMMODITY_SURGE_LOOKBACK=500` 交易日窗口）计算 z-score，z ≥ `COMMODITY_SURGE_ZSCORE`（默认 2.0）时触发非线性放大，最大 `COMMODITY_SURGE_MULTIPLIER=1.5x`。用于捕捉黄金、原油等商品暴涨对相关行业的超额影响。
2. **舆情动态权重提升**：`multi_factor.py` 新增 `_adjust_sentiment_weight()` 方法，当行业文章数量异常集中时（z > `SENTIMENT_SURGE_ZSCORE`）自动提升 sentiment 大类权重。默认禁用（`SENTIMENT_SURGE_MULTIPLIER=1.0`），因当前数据源（CCTV 等政府新闻）行业区分度不足。
3. **市值过滤修复**：`cleaner.py` 市值过滤 SQL 改为 JOIN `stock_basic` 获取 `total_share`，修正单位换算（万股 × close × 10000 = 元）。
4. **analyzer.py get_daily_score() 增强**：返回值新增 `n_articles` 列（行业文章计数），供策略层判断信号质量。
5. **新增配置参数**（`config.py`）：`COMMODITY_SURGE_ZSCORE`(2.0)、`COMMODITY_SURGE_MULTIPLIER`(1.5)、`COMMODITY_SURGE_LOOKBACK`(500)、`SENTIMENT_SURGE_MULTIPLIER`(1.0, 禁用)、`SENTIMENT_SURGE_ZSCORE`(1.5)。

## Phase 21 回测优化（已完成）摘要

针对 2018-2026 回测跑输沪深300的问题（总收益 -39% vs 沪深300 +16%），进行系统性诊断和优化：

**问题诊断**：行业归因显示房地产（占比 16.6%，收益 -51.66%）+ 建筑装饰/建筑材料合计占比 ~42%，EP/BP 因子持续给地产链高分（价值陷阱），熊市时 Regime 反向加码价值权重（1.0→1.3）更加剧了问题。

**五项优化**：
1. **熊市 Regime 权重修复**：value 1.3→0.6（避免熊市价值陷阱）、momentum 0.3→0.6（保留趋势过滤）、growth 0.6→0.8
2. **行业集中度控制**：`MAX_INDUSTRY_WEIGHT` 30%→20%；新增 `MAX_INDUSTRY_GROUP_WEIGHT=30%` + `INDUSTRY_GROUPS`（地产链/金融/TMT 组合上限）
3. **大类权重重新平衡**：value 1.0→0.7、quality 1.2→1.3、momentum 0.8→0.9
4. **价值陷阱惩罚**：value 得分 > 0 且 quality 得分 < -0.5 时，压缩 value 得分（penalty = clip(1.5 + quality, 0.3, 1.0)）
5. **趋势门槛过滤**：MOM_12M < -1.0 的股票得分乘以衰减系数（clip(1.0 + 0.3×MOM_12M, 0.3, 0.7)），防止买入持续下跌股

**回测效果（2018-01-01 ~ 2026-03-06）**：
- 总收益：-38.98% → **+5.28%**（改善 44 个百分点）
- 年化收益：-6.09% → **+0.66%**
- 超额年化：-7.78% → **-1.03%**
- 最大回撤：-69.03% → **-55.69%**
- 2023 超额：-14.19% → -3.61%；2024-2026 连续正超额（+6.69%、+11.00%、+16.78%）

---

## Phase 20 性能优化（已完成）摘要

回测信号生成性能优化，单日因子计算 ~5s → ~2.1s，1 年回测 ~68s（25 个调仓日）：

1. **预加载架构扩展**：`FactorBase.preload_for_backtest()` 新增 `policy_analysis` JOIN `policy_article` 预加载（~6K 行），`SentimentAnalyzer._get_policy_analysis_fast()` 优先使用内存数据（0.97s → 0.015s/日）。
2. **VOL_PRICE_DIV 向量化**：`technical.py` 消除 `groupby.apply` 逐股票 Python 循环，改为向量化 `cov(t, vol) / var(t)` 计算 OLS 斜率（1.0s → 0.05s）。
3. **舆情因子缓存**：`sentiment.py` 新增 `_get_sentiment_data()` 模块函数，POLICY_SENT 和 POLICY_INTENSITY 共享 `FactorBase._date_cache` 中的 `get_daily_score`/`get_daily_stock_score` 结果，消除重复 DB 查询（0.68s → 0.05s）。
4. **舆情因子 dict 查找**：`sentiment.py` 两个因子的行业→个股映射从 O(n²) DataFrame 过滤改为 O(1) dict 查找。
5. **股票池缓存**：`multi_factor.py` 中 `get_clean_universe` 结果按日期缓存在 `_date_cache`，避免同日期重复构建。
6. **ThreadPoolExecutor 无效验证**：Python GIL 限制下，CPU-bound 的 pandas/numpy 因子计算无法受益于多线程（实测 0.90x，反而更慢）。

---

## 待实现续接 Prompt

### Phase 5：券商实盘对接（QMT / Ptrade）

```
继续 A 股量化投资系统 Phase 5 — 券商实盘对接。

本地模拟盘已完成，现需接入真实券商的模拟盘或实盘。
系统已预留 BaseTrader 抽象基类和 TRADER_TYPE 配置项。

请先读取 A_SHARE_STRATEGY.md 了解算法设计，再读取：
- backend/services/execution/base_trader.py（必须实现的接口）
- backend/services/execution/paper_trader.py（参考实现）
- backend/api/views/trading.py（API 视图 + _create_trader 工厂逻辑）

接入新券商只需：
1. 新建 execution/qmt_trader.py — QMTTrader(BaseTrader)
2. 在 main.py _create_trader() 中添加 qmt 分支
3. .env 中设置 TRADER_TYPE=qmt + QMT 连接参数
```

### Phase 8-A：修复 JS 渲染网站爬虫

**已探查到两站后端 API，无需 headless browser。**

#### MIIT（工信部）API

- **端点**: `https://www.miit.gov.cn/api-gateway/jpaas-publish-server/front/page/build/unit`
- **方法**: GET，返回 `{"code":"200","data":{"html":"..."}}`，data.html 为服务端渲染 HTML
- **核心参数**:
  - `parseType=buildstatic`
  - `webId=8d828e408d90447786ddbe128d495e9e`（固定）
  - `tplSetId=209741b2109044b5b7695700b2bec37e`（装备一司模板，其他栏目需从页面提取）
  - `pageType=column`
  - `tagId=当前栏目_list`
  - `pageId=28ac65269a12494f81b5a832bce5f51c`（装备一司/文件发布，每个栏目不同）
- **分页**: 返回 HTML 底部 `<div class="pagination" count="484" pageNo="1" rows="24">`
- **HTML 结构**: `ul > li.cf > a.fl[href][title] + span.fr（日期）`
- **注意**: 不同栏目的 `tplSetId` / `pageId` 不同，需从对应栏目 HTML 页面的 `<script>` 标签 `queryData` 属性中提取

#### NFRA（金融监管总局）API

- **端点**: `https://www.nfra.gov.cn/cbircweb/DocInfo/SelectDocByItemIdAndChild`
- **方法**: GET，返回标准 JSON `{"rptCode":200,"data":{"total":N,"rows":[...]}}`
- **参数**: `itemId={分类ID}&pageSize={每页条数}&pageIndex={页码}`
- **关键 itemId**:
  - `915` — 监管动态（~4803 篇，最活跃）
  - `916` — 政策解读
  - `925` — 公告通知
  - `926` — 政策法规
- **返回字段**: `docId, docTitle, docSubtitle, publishDate, docFileUrl, pdfFileUrl`
- **文章详情**: `GET /cbircweb/DocInfo/SelectByDocId?docId={docId}` 含 `docClob`（全文 HTML）
- **备用 CDN**: `https://www.nfra.gov.cn/cn/static/data/DocInfo/SelectDocByItemIdAndChild/data_itemId=915,pageIndex=1,pageSize=18.json`
- **无需特殊 Headers**，标准 User-Agent 即可

```
继续 A 股量化投资系统 Phase 8-A — 用后端 API 修复 miit/nfra 爬虫。

请先读取：
- backend/services/sentiment/base_scraper.py
- backend/services/sentiment/scrapers/miit.py
- backend/services/sentiment/scrapers/nfra.py

两站后端 API 已探查完毕（见上方详情），改造方案：

1. miit.py — 改为调用 /api-gateway/jpaas-publish-server/front/page/build/unit，
   从返回 JSON 的 data.html 解析 li>a[title]+span 提取标题/链接/日期。
   可在 BaseScraper 中新增 fetch_json() 或直接在 MiitScraper 中 override fetch_page。
2. nfra.py — 改为调用 /cbircweb/DocInfo/SelectDocByItemIdAndChild JSON API，
   直接从 rows 数组提取 docTitle/publishDate/docId，
   文章 URL 拼接 https://www.nfra.gov.cn/cn/view/pages/ItemDetail.html?docId={docId}。
   重写 scrape() 方法遍历多个 itemId + 分页。
3. 两个爬虫的 list_urls 属性不再需要（改用 API 参数），
   但保持 BaseScraper 接口兼容（source/tier/scrape 方法签名不变）。
4. 运行 python3 main.py download_sentiment --source=miit 和 --source=nfra 验证。
```

### Phase 8-B：LLM 政策解读 + 行业关联

```
继续 A 股量化投资系统 Phase 8-B — 用 LLM 对政策新闻做结构化解读。

请先读取 A_SHARE_STRATEGY.md 了解算法设计，再读取：
- backend/services/sentiment/models.py
- backend/services/data/database.py

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

请先读取 A_SHARE_STRATEGY.md 和 factors/base.py 了解因子框架。

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

项目路径：/Users/daweilun/Documents/quantization/backend
数据库：本地 MySQL（库名 quant）
算法文档：A_SHARE_STRATEGY.md
核心业务逻辑：backend/services/

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
- [x] Phase 9 参数调优：大类权重差异化（成长 1.2 / 动量 1.3）+ 因子权重精调（已被 Phase 16 覆盖）
- [x] Phase 16 策略优化：降动量(0.8)/升质量(1.2)/渐进Regime(60MA±5%)/增持仓(15)/波动率目标(18%)/换手惩罚(0.15)/舆情提权(0.6)
- [x] 风控参数放宽：回撤阈值 0.25 + 降仓比例 0.70（减少频繁降仓）
- [x] MAX_SINGLE_WEIGHT / MAX_INDUSTRY_WEIGHT 改为环境变量可配
- [x] Twitter/X 美国政策推文采集（Trump/Vance/Rubio），Tier 5，twikit 免费方案 + 独立限速器
- [x] Phase 11 宏观因子：8 个 Tushare 宏观 API → 14 个指标序列 → 4 个因子（MACRO_CYCLE/LIQD/INFL/EXTR）
- [x] 宏观因子防未来数据泄露（MACRO_PUBLICATION_LAG 配置各指标发布延迟）
- [x] PMI 积分不足时优雅退化（PMI → PPI only 版本）
- [x] 前端数据管理页重构：移除"舆情监控"Tab，数据源概览移入"数据操作"Tab（舆情数据下方），文章列表提升为顶级 Tab
- [x] Phase 8 LLM 政策解读：关键词底层 + LLM 增强层两层分析，policy_analysis 表存储
- [x] Phase 8 舆情因子化：POLICY_SENT + POLICY_INTENSITY 映射到个股，归入 sentiment 大类
- [x] 券商研报因子：ANALYST_RATING（共识评级）+ ANALYST_COVERAGE（覆盖度），AKShare 数据源
- [x] CCTV 新闻联播 + 巨潮公告爬虫接入（16 源完整覆盖）
- [x] Phase 17 财经媒体爬虫：东方财富/财联社/新浪财经（AKShare），Tier 6，19 源完整覆盖
- [x] Phase 13 Polymarket 事件监控：实时价格监控 + LLM 情感/ticker 分析 + 告警系统 + 回测引擎
- [x] Phase 14 美股数据：S&P 500 + NASDAQ 100 成分股（Wikipedia 抓取）+ yfinance 日线行情
- [x] 美股 P&L 回测引擎：Polymarket 告警 → 美股建仓 → 持有 N 天 → 胜率/收益/夏普统计
- [x] 因子计算向量化优化：TTM 计算 O(N) 循环 → 2 次 merge，ProfitStability/MarginTrend 向量化
- [x] SQL 查询优化：IN 条件阈值（>2000 股票跳过 IN 过滤），SelectionResult.by_industry 升级 MEDIUMTEXT
- [x] 选股 API 按行业结果精简展示列（减少 JSON 体积）
- [x] Phase 19 信号增强：CMDTY_MOM 暴涨 z-score 放大（1.5x）+ 舆情动态权重（默认禁用）+ 市值过滤 JOIN stock_basic 修复 + analyzer n_articles 列
- [x] Phase 20 性能优化：预加载架构（financial+daily+policy_analysis）+ VOL_PRICE_DIV 向量化（1.0s→0.05s）+ 舆情因子缓存/预加载（0.97s→0.015s）+ 股票池缓存 + dict 查找优化（单日 5s→2.1s，1 年回测 ~68s）
- [x] Phase 21 回测优化：熊市Regime修复（value 1.3→0.6, momentum 0.3→0.6）+ 行业集中度（MAX_INDUSTRY_WEIGHT 30→20%, 关联行业组上限 30%）+ 大类权重（value 1.0→0.7, quality 1.2→1.3, momentum 0.8→0.9）+ 价值陷阱惩罚 + 趋势门槛过滤（MOM_12M<-1.0 惩罚），总收益 -39%→+5%

## 已知待优化项

- [x] 首次全量下载无断点续传 → 已添加股票级失败重试 + 增量更新自动回填历史不完整数据
- [ ] 前复权价格会随分红送股变化，需定期全量刷新
- [ ] MySQL 保留字 `open` 作列名，raw SQL 需加反引号
- [ ] 行业分类下载仍为串行（板块数 ~90，速度尚可）
- [ ] 券商实盘对接：QMT（华泰/国金）或 Ptrade（恒生）
- [ ] 模拟盘净值图表可视化（paper_nav --plot）
- [ ] miit/nfra 爬虫改用后端 API（已探查到端点，待编码实现）
