# CLAUDE.md

Claude Code 编码规范。**所有代码变更必须遵守，无例外。**

---

## 🔴 P0 — 绝对不可违反（违反 = 立即返工）

1. **先读后写** — 接到任务第一步是读代码，不是写代码。修改任何文件前必须完整阅读。修改函数前全局搜索所有调用方。
2. **不猜测** — 不假设变量类型、API 返回值、函数参数、列名。去看定义、查文档、调一次 API 确认。不确定就问用户。**[违反 5 次：2026-04-21 预加载列名 lobbying(year→date)/employee(date→filing_date)/esg(fiscal_year 当日期查)/revenue_segment(segment→segment_name)/dark_pool(short_volume→otc_short)]**
3. **严禁凭记忆写 API 调用** — 必须查文档或调一次 API 看真实返回。Tushare/AkShare 文档可能过时，以实际返回为准。
4. **严禁编造 import 路径** — 必须先确认项目目录结构和模块是否存在。
5. **API 返回什么就存什么** — 不做字段过滤、不只映射"觉得有用的"列。col_map 必须覆盖 API 返回的每一列。写 model 前调一次 API 对比列数。
6. **对标参考实现** — 新增模块必须完整读完参考实现（整个文件），1:1 复刻：类结构、多线程、断点续跑、增量逻辑、错误处理、日志格式。**[违反 1 次：2026-04-23 Rust FMP 下载器 40 个方法只给 daily_price 写了增量逻辑，其余 39 个没做，用户指出"你根本没做完"]**
7. **用户说"对齐/一致" = 逐层 diff** — 类结构 → 并发模型 → 断点 → 增量 → 错误处理 → 日志，写成表格给用户看。不是口头说"差不多"。
8. **禁止静默失败** — 每个 `return/continue/break` 前必须有 `logger`。数据跳过、空返回必须打日志。
9. **每次修改后验证** — `cargo build --release -p quant-cli` + 至少跑一个 backtest/factor smoke test + `SELECT COUNT(*)` 确认入库。不能只检查语法。**[违反 2 次：未清 import_progress 就跑 smoke test，财报 4 表假通；2026-04-23 MVO 优化器把全 universe 2700 只股票作为候选池，没验证就 commit，导致 1373% 虚假收益]**
10. **严禁重复犯同一个错误** — 被纠正一次，后续所有类似场景必须记住。**[违反 3 次：2026-04-21 多次给出前后矛盾的结论（"不需要删缓存"→"需要删缓存"→"不需要删"；"无法并行"→"可以并行"）；2026-04-22 已分析出 OBJC env var 需 os.execv 在进程启动前设置，动手时却选了 spawn 方案，导致每个 worker 重复加载 33M 行 daily price × 6 = 浪费内存+I/O]**
11. **禁止快速补丁** — 不做 `.empty` → `.is_empty()` 这种逐行替换式的 quick fix。必须完整迁移整个文件，grep 确认全项目零残留。半吊子修复比不修更差。
12. **大规模变更必须有验证门禁** — 涉及 >10 个文件的变更，commit 前必须：(1) `grep -rn "旧模式" | wc -l` 确认零残留；(2) 列出所有改动文件 vs 应改文件的 diff，确认无遗漏；(3) `cargo build --release` + 至少一个 smoke test (factors/backtest)。Agent 产出必须验证后再合并，不能盲信。**[违反 1 次：2026-04-21 polars 迁移只完成 25%，浪费 ~500K tokens]**

---

## 🟡 P1 — 写代码纪律

11. **遵循现有模式** — 命名惯例、目录结构、错误处理方式必须和项目一致。不引入矛盾写法。
12. **先说方案再动手** — 多种方案列出让用户选。涉及架构变更必须先讨论。**[违反 3 次：2026-04-21 polars 全切未确认就开始改；2026-04-22 串行评分加速未设计方案就开始改代码；2026-04-22 fork→spawn 架构变更未确认就改了 3 个文件，结果方案本身就是错的]**
13. **一次只做一件事** — 不在一个 commit 里同时重构 + 加功能。
14. **不写 magic number** — 用类常量或 config。
15. **不静默吞错误** — 至少 logger.warning。
16. **不硬编码环境值** — URL、路径、密钥用环境变量。Rust CLI 必须自动加载 `.env` 文件（dotenvy），不能要求用户手动 `source .env && export`。**[违反 1 次：2026-04-24 用户跑 `quant download` 报 "Database not configured"，因为没读 .env]**
17. **float 用 `_safe_float`** — 防空字符串/None/NaN。日期用 `_date_col_to_date`。datetime 用 timezone aware。
18. **DataFrame 去重在 upsert 前做** — `drop_duplicates(subset=unique_keys, keep="last")`。
19. **每个方法都用 ThreadPoolExecutor** — 除非数据量 < 20 条。每个方法都有 tqdm + logger.info 汇总。
20. **每个 per-ticker 方法都用 `_mark_done` 断点** — 无论有无数据都标记完成。
21. **系统性 grep 检查** — 改完后 grep 找全受影响位置，逐一修改，grep 确认零残留。
22. **立即更新文档** — 改完代码后检查 CLAUDE.md / README / doc/*.md，不等用户提醒。
23. **字符串截断 `[:N]`** — 防超长字符串炸 DB varchar。
24. **所有 API 调用经过 rate limiter** — 不绕过。
25. **写完一个方法立即对比参考实现** — 不等全写完再对比。**[违反 1 次：industry unique key 改了没验证实际效果]**

---

## 🟢 P2 — 沟通与验证

26. **不确定就问** — 需求不清先提问。不猜测用户意图。
27. **主动暴露风险** — 发现方案有问题立即说出。
28. **犯错直接承认** — 不掩盖不绕开。
29. **不说"应该可以工作"** — 要么验证了确认能工作，要么明确说无法验证。
30. **宁可慢一点交付正确的代码，也不要快速交付需要反复修改的垃圾。**
31. **回复结构：改了什么 → 为什么 → 如何验证 → 注意事项。**
32. **用户指出问题时先理解问题再改** — 不在没理解的情况下开始改。
33. **每个端点单独 smoke test** — 不只测 target all。用真实 API、查 DB、检查新字段。
34. **全量导入前先跑 1 天 / 1 ticker** — 不直接跑全量。
35. **发现 bug 先查根因** — 不是"加 try/except"或"加 if col in df"，而是修正映射/逻辑。

---

## 项目特定规则

### 项目结构（Rust 单栈 — `a_`/`us_` 前缀区分）

> **2026-04-30 重大变更**：Python 全部代码归档到 `legacy_python/`，不再使用。
> 生产策略 = Rust `quant-engine/` workspace（9 crates）。

- 同一业务在 **同 module** 下：A 股 `a_xxx.rs` / `a_share/`，美股 `us_xxx.rs`，通用无前缀
- CLI 统一入口 `quant`（`quant-cli` crate）：`quant --market {us,cn} <command>`

```
quantization/
├── quant-engine/                 ← Rust 生产代码
│   └── crates/
│       ├── core/                  TickerId / Config / Date / FactorResult
│       ├── data/                  parquet 加载 + DataCache + Universe filter
│       ├── factors/               美股 71 因子 + A 股 因子（a_share/）
│       ├── strategy/              scoring + MVO (Clarabel) + rolling_ic + regime
│       ├── backtest/              T+0 引擎 + FF5 + margin call + a_engine (A 股)
│       ├── download/              FMP / FRED / Tushare 下载器
│       ├── trading/               A 股 PaperBroker + 掘金实盘 + RiskChecker
│       ├── db/                    PostgreSQL pool (sqlx) + 模型 + queries
│       └── cli/                   `quant` CLI 入口（clap）
│   ├── config.toml                production 配置（含 v25 baseline）
│   └── scripts/                   DDL 自动生成 (gen_a_financial_rows.py)
├── cache/                         parquet 数据缓存（gitignore）
├── doc/                           策略文档
├── output/                        回测输出（gitignore）
├── logs/                          运行时日志（gitignore）
├── scripts/                       SQL migration（PostgreSQL DDL）
└── legacy_python/                 ← 已废弃 Python + React 前端（不再使用）
```

### 数据导入（Rust CLI）

- **PostgreSQL via sqlx**（quant-db crate）— 不再用 Django ORM
- **FMP** — Ultimate plan，~$300/月。列名 snake_case，全字段保留
- **Tushare** — 全字段，财报拆 4 表
- **FRED** — 永久免费，12 个宏观指标
- **已废弃数据源**：Quiver / UW / Fiscal.ai / AlphaVantage（v25 不依赖）
- **CLI 命令**：
  ```bash
  cd quant-engine
  cargo build --release -p quant-cli
  ./target/release/quant download --source fmp --target all
  ./target/release/quant download --source tushare --target all --start-year 2015
  ./target/release/quant download --source fred --target all
  ```
- **DDL**：`python3 quant-engine/scripts/gen_a_financial_rows.py`（保留 Python tooling）

### 操作授权

- **禁止擅自执行数据导入/外部 API 调用/写 DB** — 给出命令等用户确认
- **禁止并行调用同一外部 API** — 有限流
- **不确定的事说"我不确定"** — 不编造结论

### 其他

- `logging.getLogger(__name__)` + `LOG_LEVEL`
- `python3` not `python`
- `matplotlib.use("Agg")` 在 import pyplot 前
- 因子计算 = 截面（同日期全股票）
- A 股配置无前缀，美股 `US_` 前缀
- A/US 因子不共享基类

### 文档同步

代码变动后**立即**更新：

| 文档 | 何时更新 |
|------|---------|
| `README.md` | 架构/因子/数据源/CLI 变更 |
| `doc/A_SHARE_STRATEGY.md` | A 股策略变更 |
| `doc/US_SHARE_STRATEGY.md` | 美股策略变更 |
| `doc/DATA_SOURCES.md` | 数据源/表结构变更 |
