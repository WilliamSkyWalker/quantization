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
| 美股 Alpaca REST 客户端（Phase 1）| 1-2 天 | ⏳ | `quant-trading` 加美股部分，`quant --market us trade` 命令 |
| 美股 IBKR TWS 客户端（Phase 2）| 1-2 周 | ⏳ | 真实借券费 + 流动性压力测试 |
| Rust signals export（`--output-signals signals.csv`）| 0.5 天 | ⏳ | 让外部程序/手动操作能读 |

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

### 2026-04-30

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
