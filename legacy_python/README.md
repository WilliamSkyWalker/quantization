# Legacy Python Code (DEPRECATED, 2026-04-30)

**全部 Python 代码已归档到此目录。后续不再维护。生产策略迁移到 Rust：[`/quant-engine/`](../quant-engine/)。**

## 为什么归档

1. **Bug 严重**：2026-04-30 审计发现 Python `backtest` 引擎含 stop_cover 不限 cash + 候选集累积 + initial_capital=$100K 等 bug，与已修复的 Rust 引擎差距大
2. **Rust 已成熟**：Rust v25 baseline 在 2012-2025 14 年回测达 α=13.28% (t=3.40) / Sharpe 0.99 / Down Capture -8.62%（机构级 alpha）
3. **数据栈精简**：Rust 仅依赖 FMP + FRED 两个数据源，节约 ~$600+/年
4. **维护成本**：双栈维护成本高，专注 Rust

## 目录结构（归档前 Django MVT）

```
legacy_python/
├── backtest/      回测引擎 + 策略 (Alpha v3 / Beta / Baseline)
├── core/          Django settings + URLs
├── sentiment/     舆情爬虫 + Polymarket
├── services/      config.py + database.py
├── stocks/        models / downloaders / factors / management commands
├── tasks/         Celery 任务
├── trading/       paper trader + Alpaca + risk
├── scripts/       Python 工具脚本（DDL 生成、回填等）
├── manage.py      Django 入口
├── requirements.txt
├── start.sh       开发启动脚本
└── test_cli.py    测试入口
```

## 替代方案

| 旧 Python 功能 | 新 Rust 实现 |
|---------------|------------|
| `python3 manage.py bulk_import` 数据下载 | `quant download --source fmp --target all` |
| `python3 manage.py data_update` 增量更新 | `quant download --source fmp --target all --incremental` |
| `python3 manage.py backtest` 回测 | `quant backtest --start ... --end ...` |
| `python3 manage.py factor_analysis` 因子分析 | `quant analyze --start ... --end ...` |
| `python3 manage.py paper --market alpaca` 模拟交易 | **待迁移**（Rust 暂无 trading crate 美股部分） |
| 前端 API（Django views） | **待迁移**（Rust 需起 Axum/Warp HTTP 服务） |

## 待迁移的 Python 功能（如需保留）

1. **Alpaca 模拟交易** (`trading/services/us_alpaca_trader.py`)
   - Rust 端尚未实现美股 paper trading（只有 A 股 `quant-engine/crates/trading/`）
   - 短期可仍跑 Python `paper --market alpaca`，但 strategy signal 应来自 Rust
   - 桥接方案：Rust `backtest` → 输出 signals.csv → Python paper trader 读取执行

2. **前端 API**（`stocks/views/` + `backtest/views/` + Django REST）
   - 前端 `frontend/` 仍依赖 Django HTTP API
   - 长期需用 Rust 框架（Axum）重写后端 API
   - 短期：保留 Django 仅作为前端 backend（不再开发）

3. **舆情爬虫**（`sentiment/scrapers/`）
   - 中国政府网站 + Twitter 爬虫 11+ 个
   - 当前 strategy 不用 sentiment 因子（POLYMARKET_SENT 全 0）
   - 暂不迁移

## Python 引擎已知 bug（不再修复）

- `us_engine.py:463`: stop_cover 无 `cash >= 0` 限制 → cash 可负 → NAV 可负 → sign-flip 雪崩
- `us_engine.py:264, 294`: `total_value <= 0` 时 `int(target_w * TV / px)` 翻转所有 target_shares 方向
- `us_strategy.py:986`: 候选集 `prev_holdings` 无界累积（14 年累积到 1843 名）
- `services/config.py:638`: `US_INITIAL_CAPITAL = 100000` 太小，1000+ 持仓被 floor() 截零
- `us_optimizer.py`: cvxpy 约束正确（与 Rust 不同），无 post-hoc gross scaling bug ✓

详见 commit `cc7b69a` (Rust 8 bug 修复) 和 `29e9d88` (Layer 1 sum scoring) 提交说明。

## 复活路线（如果某天需要重启 Python）

```bash
cd legacy_python
pip install -r requirements.txt
# 注意：1) us_engine.py 仍有 bug 未修
#       2) us_optimizer.py 没问题但用旧因子权重
#       3) signal source 应改读 Rust 输出
python3 manage.py backtest --market us --start 2020-01-01 --end 2025-12-31
```
