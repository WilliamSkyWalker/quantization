# TODO

> 战术待办列表。战略上下文（baseline 数字、findings、设计决策）在 Claude memory 里。
> 完成的任务挪到底部"已完成"section，保留 ~30 天后归档到 doc/changelog/。

**当前 production baseline**：Rust v25 — α=13.28% (t=3.40), Sharpe 0.99, Down Capture -8.62%, Total +1294% / 14yr。
仅 FMP + FRED 数据，commit `207fd0c`。

---

## 🔴 P0 — 真 alpha 验证（v25 是否过拟合）

| 任务 | 工作量 | 状态 | 备注 |
|------|------|------|------|
| Walk-forward 验证（IS 2012-2018 / OOS 2019-2025）| 0.5 天 | ⏳ | winner-signature 调权可能用了未来信息，必须切样本验证 |
| 跨样本 1995-2011 回测 | 0.5 天 | ⏳ | 14 年训练 + 24 年测试，alpha 是否穿越多 regime |
| 行业内 long-short 测试（per GICS Sector）| 1 天 | ⏳ | 确认截面选股 alpha，不是 sector tilt 假象 |

## 🟠 P1 — 模拟盘接入

| 任务 | 工作量 | 状态 | 备注 |
|------|------|------|------|
| Rust signals export | 0.5 天 | ✅ | commit `831cab8` `--export-signals` 出 JSON |
| 美股 Alpaca REST 客户端（Phase 1）| 1-2 天 | ✅ | commit `656d86d` `quant alpaca {status,plan,run}` |
| 美股 IBKR TWS 客户端（Phase 2）| 1-2 周 | ⏳ | 真实借券费 + 流动性压力测试 |
| Alpaca 增强：fractional shares + limit order | 0.5 天 | ⏳ | 当前 floor 整股有 dollar 残差，market 单 open 时滑点大 |
| 每日 NAV 同步落 PG | 0.5 天 | ⏳ | Alpaca 仓位定时拉到本地 DB，方便对比 backtest |

## 🟡 P2 — 回测鲁棒性

| 任务 | 工作量 | 状态 | 备注 |
|------|------|------|------|
| 换手率控制 | 1 天 | ⏳ | 年化 ~400% 偏高，加持仓粘性 / buffer zone |
| 滑点敏感性测试 | 0.5 天 | ⏳ | T+0 close 太理想，测 T+1 VWAP / 10-15bps |
| Regime 参数扰动验证 | 0.5 天 | ⏳ | Credit Veto / 拥挤度阈值缺乏 robustness 测试 |
| 空头端重设计 | 1-2 天 | ⏳ | 当前 4 short 太集中，占 23% 收益过大 |

## 🟢 P3 — 策略扩展（探索性）

| 任务 | 工作量 | 状态 | 备注 |
|------|------|------|------|
| 多周期 momentum (3/6/12/24M) | 0.5 天 | ⏳ | 加 6M / 24M 看 robust |
| Layer 2 也改 sum | 0.5 天 | ⏳ | v10 只改了 Layer 1 |
| Quality × Momentum 交互项 | 1 天 | ⏳ | QMOM 经典组合，sum 制下乘积可能放大 |
| 行业 sleeve（Tech 单独配额）| 1-2 天 | ⏳ | 修 2019/2023 AI bull 缺口，有过拟合风险 |

## 🔵 P4 — 数据治理

| 任务 | 工作量 | 状态 | 备注 |
|------|------|------|------|
| EPS PIT snapshot 积累 | 中等 | ⏳ | 每日 cron 拉 FMP analyst-estimates 存历史快照 |
| FMP press / sec_filing / revenue_segment 下载 | 中等 | ⏳ | 表已存在，下载逻辑没写。NLP 时再做 |
| 政策爬虫 Python → Rust 迁移 | 1-2 周 | ⏳ | 优先级低于实盘 |

## 🟣 P0.5 — A 股舆情/事件/新闻驱动转型（当前主线，另一会话）

> 用户判断 A 股涨跌"财务驱动"权重有限（牛短熊长、情绪驱动强），要求把选股逻辑从财务驱动转向舆情/事件驱动。
> 已封存旧财务驱动选股逻辑，回测引擎接入新的空选股逻辑（新类，与旧逻辑并存）。

| 任务 | 工作量 | 状态 | 备注 |
|------|------|------|------|
| 龙虎榜/两融情绪数据下载（top_list/top_inst/margin/margin_detail/moneyflow_hsgt）| — | ✅ | 2020-2026 全量回补完成 |
| 情绪因子实现+IC/FM验证（LHB/margin 4 个因子）| — | ✅ | **负面结论**：仅 LHB_APPEARANCE_FREQ_20D 显著但方向与预设相反（需 direction=-1）；MARGIN_BAL_CHG_20D 方向也反且FM控制后不显著；其余2个纯噪声。情绪代理指标路线基本走不通 |
| 结构化事件数据下载器（forecast/express/stk_holdertrade/repurchase/share_float）| 1 天 | ✅ | 2015-2026全量回补完成：forecast 12.7万行/express 2.7万行/holdertrade 17.2万行/repurchase 5.8万行/share_float 419万行（共5表）。news/anns接口无权限保持不可用 |
| 事件驱动因子实现（PEAD/股东增减持/回购强度/解禁压力）| — | ⏳ | 依赖上一项回补完成（已完成），下一步实现因子并重新跑IC/FM验证，不能凭学术假设方向直接上线 |
| **个股新闻原文抓取管道（东财新闻聚合接口）**| 0.5 天 | ✅ | 案例分析（合力科技/比亚迪）证实新闻/题材驱动能解释结构化数据解释不了的核心异动。技术方案：Python独立脚本（不进Rust workspace）+ MySQL新表`a_news_raw`/`a_news_fetch_state`。已探测接口限制：单关键词硬上限~1000条、不支持日期区间参数、需用公司全称（代码搜索误召回严重）、深市A/B后缀股需去尾字母fallback。**全市场5212只股票 backfill 已完成，150万条入库**，脚本位于`scripts/news_fetch_pipeline.py`。财联社官方私有API（带签名保护）已确认不碰——技术保护措施规避红线 |
| **宏观新闻/政策利率抓取（新闻联播 + 央行RRR/LPR）**| 0.3 天 | ⚠️ | 东财搜索接口对宏观关键词（"商务部公告"等）是模糊匹配非精确检索，验证发现结果经常文不对题，不可靠。改用：①AkShare `news_cctv`（新闻联播官方文字稿，2016-02-03至今全文，公开无认证）②AkShare `macro_china_reserve_requirement_ratio`/`macro_china_lpr`（结构化PIT数据，公布/生效日期+调整前后数值，已验证准确）。新表`a_macro_news_raw`/`a_macro_rate_history`。脚本`scripts/macro_news_fetch.py`。RRR(1732条)/LPR完整。**已知缺口：`a_macro_news_raw` 2021-10-25~2026-08-27约5年数据缺失（backfill被WSL重启中断），待重新执行`--mode backfill`补齐** |
| 工信部/商务部政策公告原文抓取 | 待定 | ✅ | `scripts/gov_policy_fetch.py`，backfill 已完成 5390 条（MIIT 3075 + MOFCOM 2315，覆盖至2026-08-27） |
| **三个新闻脚本 DB 配置 bug 修复** | — | ✅ | 2026-09-01 发现 `load_db_config()` 凭记忆假设 `env.json` 顶层有 `mysql`/`database` 键，实际结构是 `{"ENV":..,"quant":{env:{...}}}`，导致三个脚本静默连接失败退化为 root 空密码，**三条管道此前从未真正跑通增量抓取**。已修复三个脚本并验证连接成功，尚未 commit |
| 新闻定时增量抓取上 cron（个股新闻 + 宏观新闻联播/利率 + 政策公告）| 0.5 天 | ✅ | 2026-09-01 配置完成：个股新闻每天09:00/13:00/16:00（含周末）、宏观新闻每天07:30、政策公告每天08:00，均为`--mode incremental` |
| 新闻文本情感打分（NLP）| 待定 | ⏳ | 抓取管道验证后再决定：词典法 vs 金融BERT微调，是否需要追加付费Tushare news/anns（~2000元/年）或Finlight（$99+/月，自带实体+情感标签） |
| 动态估值分位数 regime overlay（替代硬编码4000点）| 中等 | ⏳ | 已用DB数据核实"4000点魔咒"是经验区间非铁律（2025-11已破4000，2026年新高4242）。待舆情/事件工作完成后排期，扩展现有`detect_a_regime`框架 |
| 因子类别权重从财务驱动转向舆情驱动（config.toml category_weights）| 小 | ⏳ | 依赖事件/新闻因子验证出正向IC，否则只是把权重压到空因子上 |

## ⚪ P5 — 工程收尾

| 任务 | 工作量 | 状态 | 备注 |
|------|------|------|------|
| A 股掘金实盘接入 | 1-2 周 | ⏳ | 另一会话排期 |
| HTTP API 重写（Axum/Warp）| 1 周 | ⏳ | 前端归档后暂缓 |
| Rust `quant export-parquet` 命令 | 0.5 天 | ⏳ | 当前用 Python 脚本，避免双栈 |

## ⚫ 暂缓 / 已废弃

- Polymarket LLM 回填（POLYMARKET_SENT 因子值全 0）
- AlphaVantage NEWS_SENTIMENT 数据积累（已退订 AV）
- Earnings Transcript 下载（NLP sentiment 时再考虑）
- ~~A 股 Django ORM 迁移~~（2026-04-30 放弃）
- ~~付费分析师数据源接入（Refinitiv/Bloomberg）~~（暂缓）

---

## ✅ 已完成（最近 30 天）

### 2026-04-30 / 05-01

- ✅ Alpaca REST 美股 paper trading 客户端（`quant alpaca` 全套命令）— commit `656d86d`
- ✅ Rust signals export（`backtest --export-signals` 出 JSON）— commit `831cab8`
- ✅ Rust 引擎 8 bug 修复（floor / sign-flip / cover-starvation / margin-call / post-hoc-gross-scaling）— commit `cc7b69a`
- ✅ Layer 1 sum scoring + EPS_REVISION 移除 — commit `29e9d88`
- ✅ Winner-signature 调权（MOM_12M/SUE_PEAD/EARNINGS_SURPRISE 进 T0）— commit `acc4014`
- ✅ EV_TO_FCF/EV_TO_EBIT 方向修正 — commit `71cf204`
- ✅ Layer 1 sum + FF5 alpha 验证 α=8.24% (t=2.47) → v21 α=13.53% → v25 α=13.28%
- ✅ gross_leverage 1.0 → 1.2（v21 Sharpe 突破 1.0）— commit `e61b243`
- ✅ 5 个 Quiver 付费因子禁用，仅 FMP+FRED 数据（v25）— commit `207fd0c`
- ✅ 慢 SQL 阈值 1s → 2s — commit `13f4825`
- ✅ FMP 并发 15 → 30 — commit `7f9077d`
- ✅ Python 全部归档到 `legacy_python/` + 前端一并归档 — commit `8b97ce0`, `a62122f`
- ✅ Rust 美股代码加 us_ 前缀（factors / backtest engine / 部分 strategy）— commit `3c5f62d`, `eb58dbb`
- ✅ 全面文档更新到 v25 baseline — commit `e171eb8`

### 2026-04-29 之前

- ✅ 全量数据重写+重新导入（美股 39 模型 + A 股 20 模型，无字段过滤）
- ✅ finance-basics skill v5（18 节，1013 行）
- ✅ Tier 1 风险模型 + MVO 优化器（Ledoit-Wolf + cvxpy/OSQP）
- ✅ A 股 Rust 迁移 ~85% 完成（factors / cleaner / trading PaperBroker）

---

## 操作提示

- 添加新任务：在对应 priority 表格底部加一行
- 状态标记：⏳ 待开始 / 🔄 进行中 / ✅ 完成 / ❌ 放弃
- 完成的任务移到"已完成"section
- 战略 findings / 设计决策 → 写到 Claude memory（`project_*.md`），不要塞这里
- 每周或每个 milestone 后清理一次（30+ 天前的已完成 → 归档到 `doc/changelog/`）
