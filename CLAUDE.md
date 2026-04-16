# CLAUDE.md

Claude Code 编码规范。所有代码变更必须遵守。

## ⚠️ 强制检查清单（每次代码变更必须逐项完成）

**任何代码修改后，必须按顺序完成以下所有步骤，缺一不可：**

一、读代码 / 读上下文（Rules 1–20）

接到任务后，第一步永远是读相关代码，而不是写代码。 先用工具浏览项目结构，定位相关文件，理解现有架构后再动手。
修改任何文件之前，必须先完整阅读该文件。 不允许只看函数名就开始改，必须理解上下文、imports、依赖关系。
如果文件超过 500 行，至少通读你要修改的函数所在的上下 50 行。 确认前后的变量定义、状态管理和副作用。
修改函数之前，先全局搜索这个函数的所有调用方。 确认改动不会破坏其他地方。
绝对不要假设一个变量的类型、一个函数的参数、一个 API 的返回值。 去看定义，去看类型声明，去看实际调用。
遇到不认识的工具函数/自定义 hook/内部库，必须先找到其源码读懂再使用。 不允许根据函数名猜测行为。
修改 state 管理相关代码前，先画出当前的数据流。 搞清楚 state 从哪来、谁在读、谁在写、什么时候更新。
接手一个 bug 修复任务时，先读 bug 相关的完整代码路径（从入口到出口），再定位问题。 不允许看到报错就直接改报错那一行。
如果代码里有注释说明了设计意图或 workaround 原因，必须尊重并理解这些注释，不能无视直接重写。
修改配置文件（tsconfig、webpack、vite、package.json 等）前，先读懂现有配置的每一项含义。
对项目中已存在的代码模式（命名惯例、目录结构、错误处理方式）必须遵循。 不允许引入与现有代码风格矛盾的写法。
如果项目有 .eslintrc / .prettierrc / editorconfig，先读取并严格遵守。
在写任何 import 语句之前，先确认该模块在项目中确实存在并且路径正确。
修改数据库 schema / migration 前，先读完所有现有 migration 文件，理解当前 schema 全貌。
修改 API 接口前，先确认前后端的契约（类型定义、接口文档、实际请求/响应格式）。
在写测试之前，先看项目里已有的测试是怎么写的（测试框架、mock 方式、文件组织）。
不要在没看 package.json 的情况下安装新依赖。 先确认是否已有功能等价的库。
修改 CSS/样式前，先看组件的完整样式上下文，包括父组件传入的 class 和全局样式。
修改任何「看起来可以删」的代码前，先全局搜索确认它真的没被引用。
每次 edit 之后必须重新阅读修改后的文件相关部分，确认编辑结果符合预期，没有破坏缩进、没有遗漏闭合标签/括号。


二、查文档 / 查 API（Rules 21–40）

使用任何第三方库的 API 之前，必须查阅其官方文档。 不允许凭记忆写 API 调用。
当你对某个 API 的参数、返回值、行为有任何不确定时，必须查文档或查源码，绝不猜测。
版本敏感：使用任何库之前先确认项目实际安装的版本号，然后查对应版本的文档。 不同版本 API 可能完全不同。
如果项目有 README 或内部文档，在动手之前必须先读。
遇到报错信息时，先完整阅读错误堆栈，定位到具体文件和行号，再去看那行代码。 不允许看到错误关键词就猜原因。
使用命令行工具时，先 --help 或查文档确认参数格式，不要猜参数。
写正则表达式时，必须明确每个部分的含义并用测试用例验证。 不允许写一个看起来差不多的正则就提交。
使用数据库查询（SQL/ORM）时，先确认表结构、索引、外键关系。 不允许猜字段名或关联关系。
调用系统 API（文件系统、网络、进程）时，先确认在目标平台上的行为。 不同操作系统行为可能不同。
使用 CSS 属性时，如果不确定浏览器兼容性或具体行为，先查 MDN。
编写 TypeScript 类型时，先看项目已有的类型定义和惯例。 不允许重复定义已存在的类型。
使用环境变量前，先确认 .env 文件或部署配置中该变量确实存在。
写 Docker 相关配置时，先读懂现有的 Dockerfile 和 docker-compose.yml。
操作 Git 时（创建分支、rebase、merge），先确认当前分支状态和远程分支情况。
使用加密/安全相关 API 时，必须查官方文档确认正确用法。 安全代码不允许有任何猜测成分。
调用任何可能有副作用的操作（删除文件、修改数据库、发送请求）前，先确认操作范围和回滚方案。
如果项目使用了 monorepo（nx、turborepo、lerna），先理解项目间的依赖关系图。
使用框架的生命周期钩子（React useEffect、Vue onMounted 等）时，先确认触发时机和清理逻辑。
配置 CI/CD pipeline 时，先读懂现有 pipeline 的完整流程和依赖。
如果不确定某个函数是同步还是异步、某个操作是否会阻塞，去看源码或文档确认。不要猜。


三、写代码纪律（Rules 41–60）

一次只做一件事。 不允许在一个 commit 里同时重构代码结构和添加新功能。
写代码之前先说清楚你的方案和理由。 在动手前用自然语言描述：我准备做什么、为什么这样做、有哪些风险。
如果有多种实现方案，列出来让我选择。 不允许默默选了你认为最好的就开始写。
新增代码必须与项目现有风格一致。 包括缩进、命名、注释风格、文件组织方式。
不允许写 magic number。 所有常量必须有命名，且命名要能说明含义。
不允许在没有错误处理的情况下写异步代码。 每个 async/await、Promise、网络请求都必须有 error handling。
不允许静默吞掉错误（empty catch block）。 至少要 log。
不允许复制粘贴代码再微调。 如果两段代码高度相似，必须抽象成可复用的函数/组件。
新增函数必须有明确的输入输出类型声明（如果项目用 TypeScript）。 不允许用 any 除非有充分理由并注释说明。
不允许在修复一个 bug 的过程中顺手改一堆无关的代码。 保持改动最小化和可审查性。
条件分支要覆盖所有 case。 switch 要有 default，if-else 要考虑边界情况。
不允许硬编码环境相关的值（URL、路径、密钥）。 必须用环境变量或配置文件。
写代码时考虑可测试性。 函数应该是纯函数或者依赖可注入。不要把逻辑和 IO 耦合在一起。
每写完一个功能模块，立刻运行相关测试确认没有 break 任何东西。
不允许注释掉代码并留在文件里。 要么删掉，要么说明为什么暂时保留。
新加的工具函数 / util 必须放在项目约定的目录下。 不允许随意在功能文件里塞 helper。
不允许在 PR 里引入未使用的 import、变量或函数。
所有对外暴露的函数/组件必须有 JSDoc 或注释说明用途、参数、返回值。
写 CSS 时遵循项目已有的方案（CSS Modules / Tailwind / styled-components 等），不引入新方案。
涉及并发/竞态条件的代码必须明确说明同步策略。 比如 debounce、mutex、乐观锁等。


四、验证与测试（Rules 61–75）

每次修改后必须运行项目的 lint 和 type check。 不允许交付有 lint error 或 type error 的代码。
如果项目有已有的测试套件，修改代码后必须运行相关测试。
写新功能时，至少写一个 happy path 测试和一个 edge case 测试。
修复 bug 时，先写一个能复现这个 bug 的测试用例，再修复，再确认测试通过。
不允许交付编译不通过的代码。 每次修改后都要确认 build 能过。
UI 相关的修改必须实际跑一下看效果。 不允许只看代码觉得「应该没问题」。
涉及数据处理的代码，用真实数据或接近真实的数据跑一遍。 不允许只用完美的测试数据。
性能敏感的代码必须做基准测试。 不允许说「这个应该很快」。
修改权限/认证相关代码后，必须测试正常用户和异常用户两种场景。
修改表单验证逻辑后，必须测试所有验证规则，包括边界值。
发现测试失败时，先确认是你的改动导致的还是测试本身的问题，不要上来就改测试让它通过。
如果你的代码依赖外部服务（API、数据库），写测试时必须 mock 这些依赖，并且 mock 行为要符合真实行为。
不允许交付时说「我没法运行测试」然后跳过。 如果环境有问题，先解决环境问题。
修改公共组件 / 共享模块后，检查所有使用它的地方是否仍然正常。
数值计算相关代码必须验证精度问题（浮点数、四舍五入、货币计算）。


五、沟通与思维方式（Rules 76–90）

不确定就问，不要猜。 如果需求不清晰，先提出具体的澄清问题。
主动暴露风险。 如果你发现当前方案有潜在问题（性能、安全、兼容性），立即说出来。
如果你发现任务比预期复杂得多，立刻说明，并提出分步实施方案。 不要默默尝试一步到位。
每次回复中明确说明你做了什么改动、为什么这样改、还有什么没做完。
如果你犯了错误，直接承认并修复，不要试图掩盖或绕开。
不允许在回复里说「这应该可以工作」。 要么验证了确认能工作，要么明确说「我无法验证，需要你确认以下几点...」。
解释技术方案时，先说结论/做法，再说原因。 不要写长篇大论的背景铺垫让我找不到重点。
如果我给了你一个方向但你认为有更好的方案，直接提出对比分析，但最终决定权在我。
涉及架构级别的变更（引入新框架、改变数据流、更换状态管理方案），必须先讨论而非直接实施。
遇到自己不擅长的领域（如安全、性能优化、特定框架的深入用法），坦诚说明局限性。
回复要有结构：改了什么 → 为什么改 → 如何验证 → 还有什么要注意的。 不允许只甩一段代码什么都不说。
当我指出你的代码有问题时，先认真理解我说的问题，再回应。 不要在没理解问题的情况下就开始改。
如果一个任务需要多步完成，在开始前给出完整计划，每一步做完后汇报进展。
如果你在搜索/阅读代码后发现你之前的假设是错的，明确说明你的认知更新。
不允许为了显得高效而跳过必要步骤。 宁可慢一点交付正确的代码，也不要快速交付需要反复修改的垃圾。


六、严禁事项（Rules 91–100）

严禁凭记忆写 API 调用。 你的训练数据可能过时，版本可能不对，参数可能记错。必须查文档。
严禁在没看过原始代码的情况下写 str_replace 的 old_str。 必须先 view 文件确认原始内容的精确文本。
严禁编造不存在的 npm 包、Python 库、API endpoint。 如果不确定是否存在，先搜索确认。
严禁在生成代码时编造 import 路径。 必须先确认项目目录结构和模块导出方式。
严禁使用已经废弃（deprecated）的 API。 必须确认你用的 API 在当前版本仍然可用。
严禁在没有理解业务逻辑的情况下重构代码。 重构只改结构不改行为，你必须先理解行为才能保证不改变它。
严禁一次性生成超过 200 行代码而不分段验证。 大段代码必须逐步构建、逐步验证。
严禁跳过错误信息中的关键细节。 报错信息的每一行都可能是线索，不要只看第一行就下结论。
严禁「我改完了你试试」这种甩锅心态。 你有义务在交付前做尽可能充分的验证。
严禁重复犯同一个错误。 如果我纠正了你一次，你必须在后续所有类似场景中记住这个教训。

> 这不是建议，是强制要求。违反任何一条都会导致用户重复返工。

---

## 编码规范

### 项目结构（Django MVT — A 股/美股按 app 对齐 + `a_`/`us_` 前缀区分）

**核心规则：**
- 同一业务模块在 **同名 app** 下并列
- A 股逻辑文件用 `a_xxx.py`，美股逻辑文件用 `us_xxx.py`，跨市场通用代码无前缀
- CLI / management commands **共用**，通过 `--market {cn,us}` 或 `--source {fmp,quiver,tushare,akshare}` 分发
- URL 路由分散到各 app 的 `urls.py`，`core/urls.py` 统一 include 到 `/api/` 前缀

```
quantization/
├── stocks/         # 股票域（US + CN 共用 app）
│   ├── models/        us_stock.py + a_stock.py（managed=False）
│   ├── services/
│   │   ├── downloaders/    a_bulk.py（CN，AShareBulkDownloader 类，含 Tushare + AkShare 全端点）
│   │   │                   us_bulk.py / us_fmp.py / us_fred.py / us_edgar.py 等（US）
│   │   ├── factors/        a_base/a_value/a_quality/...（CN 13 文件）
│   │   │                   us_base/us_value/us_quality/...（US）+ signals/（AlphaSignal 架构）
│   │   ├── upsert.py       UpsertManager（跨市场通用）
│   │   ├── a_cleaner.py    A 股股票池清洗
│   │   └── us_cleaner.py   美股股票池清洗
│   ├── views/         a_stock.py / a_config.py / a_data.py / a_watchlist.py（CN）
│   │                  us_strategy.py（US）
│   ├── urls.py        /api/{data,tasks,config,watchlist,stock,us}/*
│   └── management/commands/  bulk_import.py / data_update.py（统一入口）
├── backtest/       # 回测域
│   ├── services/      a_engine.py / a_strategy.py / a_regime.py（CN）
│   │                  us_engine.py / us_strategy.py / us_regime.py / us_ff5.py / us_saver.py 等（US）
│   ├── views/         a_strategy.py / a_report.py（CN）
│   ├── urls.py        /api/{universe,select,factors,backtest,report}/*
│   └── models/        result.py
├── trading/        # 交易域
│   ├── services/      a_paper_trader.py / a_risk.py / a_gm_trader.py（CN）
│   │                  us_paper_trader.py / us_alpaca_trader.py（US）
│   │                  base_trader.py（通用接口）
│   │                  monitor/performance.py + monitor/report.py（绩效，通用）
│   ├── views/         a_trading.py（CN）
│   └── urls.py        /api/paper/*
├── sentiment/      # 情绪域：polymarket + scrapers + views
│   └── urls.py        /api/{sentiment,polymarket}/*
├── services/       # 仅 config.py + database.py 兼容 stub
├── core/           # Django settings + 根 URL（include 4 个 app 的 urls）
├── scripts/        # generate_ashare_ddl.py / migrate_ashare_schema.sql
└── cli.py          # 残留旧 typer 命令
```

每个 app 结构：`models/ services/ views/ management/commands/ urls.py apps.py`。

### 三层同步规则

系统有三个入口层调用业务逻辑：**Django management commands** (`python3 manage.py <cmd>`)、**API** (`api/urls.py` + 各 app 的 `views/`)、**前端** (`frontend/src/`)。

- **新增业务逻辑** → 放在对应 app 的 `services/` 下，CLI / API / 前端三处同步
- **核心命令已迁移**：`python3 manage.py backtest / data_update / bulk_import`
- **A 股下载入口**（对齐美股 `AShareBulkDownloader` 类，多线程 + 断点续跑）：
  - `python3 manage.py bulk_import --source tushare --target {stock-list,prices,income,balancesheet,cashflow,fina-indicator,industry,index,commodity,macro,trade-cal,all} [--start-date YYYYMMDD] [--clean]`
  - `python3 manage.py bulk_import --source akshare --target {research-report,insider,all} [--clean]`
  - `python3 manage.py data_update --market cn`（增量更新，调 `AShareBulkDownloader(incremental=True)`）
  - DDL：`scripts/generate_ashare_ddl.py` 自动生成 → `scripts/migrate_ashare_schema.sql`（全 21 张 `a_*` 表）
- **其他 cli.py 命令**（select/factor/paper/db/polymarket）按需逐步迁移到 management commands
- **前端修改后必须 `pnpm build`** — Django 只提供 dist 静态文件

### 操作授权规则

- **禁止擅自执行数据导入/外部 API 调用/写数据库操作** — 只给出命令，等用户确认后再执行
- 涉及的命令包括但不限于：`data bulk-import`、`data download`、`data update`、`paper trade`、`paper reset`
- **禁止并行调用同一外部 API** — FMP/UW/Fiscal 等有限流，必须逐个执行
- **不确定的事说"我不确定"，不要编** — 不知道标准就问用户，给选项让用户选，不要自己编造结论

### 通用规范

- 所有模块使用 `logging.getLogger(__name__)`，日志级别取 `config.settings.LOG_LEVEL`
- **禁止静默失败** — 每个 `return 0`/`return []`/`continue`/`break` 前必须有 `logger.debug/warning`
- **所有修改必须系统性检查** — `grep` 找全、逐一改、`grep` 确认零残留
- **所有修改必须测试** — 用实际数据运行验证，涉及 DB 写入的必须 `SELECT COUNT(*)` 确认入库
- **不用 pytest** — 测试用端到端自动化脚本（真实 API + 真实 DB），不用 mock
- **数据库读写必须使用 Django ORM**，禁止手写 raw SQL。模型定义在 `stocks/models/`、`backtest/models/` 等 app 下（managed=False），写入用 `stocks.services.upsert.UpsertManager`（查询+分流 create/update），查询用 `Model.objects.filter().values_list()`。回测热路径数据通过 parquet 缓存（`cache/` 目录）加速
- **FMP 数据导入禁止 rename 列名** — DB 列名必须和 `_camel_to_snake()` 转换结果完全一致，不允许手动起别名（如 `roe`/`rd_expenses`）。唯一例外是 `date→trade_date`（避免和 unique key 冲突）
- **A 股 Tushare 数据导入保留所有字段** — `_download_endpoint` 不指定 `fields=`，让 Tushare 返回端点全字段。列名 = Tushare API 原名（snake_case），财报拆分四张表（income/balance/cashflow/indicator），不再合表
- **禁止使用 Agent 子进程** — 所有工作在主会话中完成
- Matplotlib 必须在导入 `pyplot` 前调用 `matplotlib.use("Agg")`
- 因子计算始终为截面（同一日期，全部股票）
- MySQL 列名 `open` 是保留字，原生 SQL 需用反引号转义
- **A 股因子放 `stocks/services/factors/a_*.py`**（与美股 `factors/us_*.py` 同目录，前缀区分；不共享基类）。A 股因子 base (`a_base.py`) 提供旧字段别名（`net_profit/roe_ttm/gross_margin` 等）保持子类兼容
- A股配置无前缀（`MAX_HOLDINGS`），美股配置带 `US_` 前缀（`US_MAX_HOLDINGS`）
- 使用 `python3` 而非 `python`

### 数据治理重写待办
- [x] FMP 端点字段映射修复（earnings/profile/key-metrics COALESCE upsert）
- [x] 历史市值切换到 us_enterprise_value.market_capitalization（删 us_historical_market_cap）
- [x] Quiver lobbying / gov_contract 接入
- [x] Polymarket model 扩展（52 列）+ history.py 异步写入
- [x] ETF 预标记机制（_premark_etfs_no_data）
- [ ] 端到端测试 — 用 AAPL 测试每个端点写入
- [ ] 删除旧的 `fmp_downloader.py`（yfinance 版）或标记废弃
- [ ] UW/Fiscal/AV 下载逻辑迁移

### 文档同步规则

代码变动后立即同步更新（不是事后补）：

| 文档 | 内容 | 何时更新 |
|------|------|---------|
| `README.md` | 项目概述、架构、因子表、绩效、CLI 命令 | 架构/因子/数据源变更时 |
| `doc/US_SHARE_STRATEGY.md` | 美股策略算法详细文档 | 策略/因子/数据源变更时 |
| `doc/A_SHARE_STRATEGY.md` | A股策略算法文档 | A股策略变更时 |
| `doc/DATA_SOURCES.md` | 数据源详细文档 | 数据源/表结构变更时 |
| `doc/old/PollyMarket_STRATEGY.md` | Polymarket 策略文档（归档） | 涉及美股数据时 |
| `doc/old/POLYMARKET_PNL_ANALYSIS.md` | P&L 分析文档（归档） | 涉及数据源时 |
