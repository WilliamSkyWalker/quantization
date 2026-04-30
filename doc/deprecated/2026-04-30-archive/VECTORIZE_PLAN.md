# 因子计算向量化优化计划

## 目标

回测时间从 ~10 分钟降至 ~2 分钟（6 年回测、167 次调仓）。

## 当前瓶颈分析

单日因子计算 ~3.2s，167 次 = ~530s（9 分钟）：

| 环节 | 耗时/日 | 占比 |
|------|---------|------|
| Universe 构建（3-5 次 SQL） | 0.3-0.9s | 20% |
| 舆情因子（POLICY_SENT） | 0.5-1.0s | 20% |
| 动量因子（IND_MOM, REV_5D, RESIDUAL_MOM 等） | ~1.0s | 30% |
| 财务因子（EP, ROE_TTM 等） | ~0.5s | 15% |
| 其他因子 + 中性化 | ~0.5s | 15% |

数据已通过 `preload_for_backtest()` 加载到内存，但每个日期仍重复：过滤、排序、去重、rolling 计算。

---

## 四层优化

### Tier 1: Universe 批量预计算（省 ~80s）

**问题：** `get_clean_universe()` 每次执行 3-5 条 SQL（stock_basic、daily_price 当日行情、流动性回看、市值）。

**方案：** 新增 `preload_clean_universes(db, dates)`:
- `stock_basic` 查一次，内存中按日期过滤（退市日、上市天数）
- `daily_price` 用 `trade_date IN (...)` 一次查所有调仓日的行情
- 流动性用已预加载的 `_bulk_daily` 内存计算
- 返回 `dict[date_str, DataFrame]`

**改动文件：**
- `services/data/cleaner.py` — 新增 `preload_clean_universes()`
- `services/strategy/multi_factor.py` — `generate_signals()` 中调用，`_compute_scores_for_date()` 优先使用缓存

**风险：** 低。现有 `get_clean_universe()` 不变，实时选股路径不受影响。

---

### Tier 2: 财务因子快照去重（省 ~120s）

**问题：** 财务数据季度更新，但 167 次调仓每次都重新过滤 200K 行 `financial_data`（`ann_date <= date` + sort + drop_duplicates）。

**方案：** 按公告日边界分组，相同财务快照的调仓日共享计算结果：
- 从 `_bulk_financial` 提取所有 `ann_date`
- 将 167 个调仓日映射到 ~10 个唯一快照（每季报一个边界）
- 预计算每个快照的 `get_latest_financial()` / TTM 结果
- `get_latest_financial()` / `get_ttm_net_profit()` / `get_ttm_revenue()` 优先查快照缓存

**改动文件：**
- `services/factors/base.py` — 新增 `precompute_financial_snapshots(dates)`，修改缓存查找方法
- 各财务因子文件无需改动（透明使用缓存）

**风险：** 中。需正确处理不同股票的 `ann_date` 不同的情况（快照以调仓日为 key，每只股票取各自的最新公告数据）。

---

### Tier 3: 价格因子 Rolling 预计算（省 ~160s）

**问题：** 动量/技术因子每次调用 `get_price_history()` 过滤内存 DataFrame + 计算 rolling 统计。167 次重复。

**方案：** 新增 `compute_batch(dates, universes)` 接口，用 `groupby('ts_code').rolling()` 一次算完整个回测区间：

```python
# FactorBase 默认实现（向后兼容）
def compute_batch(self, dates, universes):
    return {d: self.compute(d, universes[d]) for d in dates}
```

需要 override 的因子：
- **动量类：** MOM_1M/3M/12M → 预计算月末复权价，向量化除法算收益率
- **REV_5D / RESIDUAL_MOM：** `rolling(5/20)` 一次算完
- **技术类：** TURN_20D → `rolling(20).mean()`，VOL_20D → `rolling(20).std()`，PRICE_DEV_60D → `rolling(60).mean()` 后算偏离
- **IND_MOM：** 复用个股 rolling 收益，按行业聚合
- **VOL_PRICE_DIV：** 预计算 rolling 价格趋势和成交量斜率

**改动文件：**
- `services/factors/base.py` — 新增 `compute_batch()` 默认方法
- `services/factors/momentum.py` — override 6 个动量因子
- `services/factors/technical.py` — override 5 个技术因子
- `services/strategy/multi_factor.py` — 新增 `_compute_all_factors_batch()` 集成

**风险：** 高。Rolling window 边界处理（新股不足 lookback 天数产生 NaN）需与逐日计算结果严格一致。内存增加约 ~500MB。

---

### Tier 4: 中性化缓存（省 ~30s）

**问题：** `process_factor()` 为 30 因子 × 167 日 = 5010 次 OLS 回归，行业 dummy matrix 重复构建。

**方案：**
- 缓存 `pd.get_dummies()` 结果（按行业集合 hash）
- 预计算所有调仓日的市值 DataFrame

**改动文件：**
- `services/factors/processor.py` — 缓存 dummy matrix
- `services/strategy/multi_factor.py` — 预计算市值

**风险：** 低。纯内部优化。

---

## 预期效果

| 优化层级 | 节省时间 | 累计耗时 |
|----------|---------|---------|
| 当前 | — | ~530s (9min) |
| Tier 1: Universe 预加载 | -80s | ~450s |
| Tier 2: 财务快照去重 | -120s | ~330s |
| Tier 3: 价格 Rolling 预计算 | -160s | ~170s |
| Tier 4: 中性化缓存 | -30s | ~140s (2.3min) |

## 实施顺序

1. **Tier 1**（1-2 天）：最安全，确定有效
2. **Tier 2**（1-2 天）：中等复杂度，高收益
3. **Tier 3**（2-3 天）：最复杂，最大收益
4. **Tier 4**（0.5 天）：锦上添花

## 验证方法

1. 改动前跑一次固定区间（2023-01-01~2023-06-30）的回测，保存全量 signals 作为基准
2. 每完成一层，对比 3 个代表性日期的因子值，允许浮点误差 < 1e-10
3. 确认实时选股路径（`select_stocks(date)`）不受影响

## 向后兼容

- 所有优化仅在 `generate_signals()` 回测路径触发
- 单日 `compute(date, universe)` 接口保持不变
- 实时交易 `select_stocks()` / `score_all_stocks()` 不受任何影响
