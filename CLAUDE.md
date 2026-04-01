# CLAUDE.md

Claude Code 编码规范。所有代码变更必须遵守。

## ⚠️ 强制检查清单（每次代码变更必须逐项完成）

**任何代码修改后，必须按顺序完成以下所有步骤，缺一不可：**

- [ ] **1. 系统性检查** — `grep` 找出所有受影响位置，逐一修改，`grep` 确认零残留。不凭记忆，不遗漏
- [ ] **2. 禁止静默失败** — 所有 `return`/`continue`/`break` 分支前必须有 `logger` 日志。数据跳过、空返回必须打日志
- [ ] **3. 真实数据测试** — 用实际 API + 真实数据库验证，`SELECT COUNT(*)` 确认数据入库。不能只 `import` 检查语法
- [ ] **4. 立即更新文档** — 检查并更新所有受影响的 MD 文件。不等用户提醒

> 这不是建议，是强制要求。违反任何一条都会导致用户重复返工。

---

## 编码规范

### 三层同步规则

系统有三个入口层调用同一套 service：**CLI** (`cli.py`)、**API** (`api/views/`)、**前端** (`frontend/src/`)。

- **新增/修改 service 方法** → 同步更新 CLI 命令 + API view + 前端页面
- **新增 API 端点** → 同步在 CLI 中添加对应命令、在前端 `api/index.ts` 中添加函数
- **业务逻辑只写在 `services/`** — CLI 和 API views 只做参数解析 + 调用 service + 格式化输出
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
- 数据库批量写入统一使用 `_fast_bulk_upsert`（`INSERT ... ON DUPLICATE KEY UPDATE`），禁止逐条 ORM
- Matplotlib 必须在导入 `pyplot` 前调用 `matplotlib.use("Agg")`
- 因子计算始终为截面（同一日期，全部股票）
- MySQL 列名 `open` 是保留字，原生 SQL 需用反引号转义
- A股因子在 `services/factors/`，美股因子在 `services/us_factors/`（独立，不共享基类）
- A股配置无前缀（`MAX_HOLDINGS`），美股配置带 `US_` 前缀（`US_MAX_HOLDINGS`）
- 使用 `python3` 而非 `python`

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
