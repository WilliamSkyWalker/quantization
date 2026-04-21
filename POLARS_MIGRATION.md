# Polars 迁移规范

## 总则
- `import pandas as pd` → `import polars as pl`
- `_static_cache` 和 `_date_cache` 全部存 `pl.DataFrame`
- 因子 `compute()` 返回 `pl.DataFrame` (columns: ["ticker", "factor_value"])
- numpy 保留，不动

## API 对照表

| pandas | polars |
|--------|--------|
| `pd.DataFrame({"a": [1,2]})` | `pl.DataFrame({"a": [1,2]})` |
| `df.empty` | `df.is_empty()` |
| `df.copy()` | `df.clone()` |
| `df[df["col"] > 0]` | `df.filter(pl.col("col") > 0)` |
| `df["col"]` | `df["col"]` 或 `df.get_column("col")` |
| `df[["a","b"]]` | `df.select(["a","b"])` |
| `df.rename(columns={"a":"b"})` | `df.rename({"a":"b"})` |
| `df.merge(other, on="key")` | `df.join(other, on="key", how="left")` |
| `df.drop_duplicates(subset=["k"], keep="last")` | `df.sort("date").unique(subset=["k"], keep="last")` |
| `df.sort_values("col")` | `df.sort("col")` |
| `df.sort_values("col", ascending=False)` | `df.sort("col", descending=True)` |
| `df.groupby("g")["v"].sum()` | `df.group_by("g").agg(pl.col("v").sum())` |
| `df.groupby("g").head(4)` | `df.group_by("g").head(4)` |
| `df.head(n)` | `df.head(n)` |
| `df.iterrows()` | `df.iter_rows(named=True)` |
| `df.values` | `df.to_numpy()` |
| `df["col"].values` | `df["col"].to_numpy()` |
| `pd.to_datetime(df["col"])` | `df.with_columns(pl.col("col").cast(pl.Date))` |
| `pd.to_numeric(df["col"], errors="coerce")` | `df.with_columns(pl.col("col").cast(pl.Float64, strict=False))` |
| `df["col"].fillna(val)` | `df.with_columns(pl.col("col").fill_null(val))` |
| `df["col"].notna()` | `pl.col("col").is_not_null()` |
| `df["col"].isna()` | `pl.col("col").is_null()` |
| `df.dropna(subset=["col"])` | `df.drop_nulls(subset=["col"])` |
| `df["col"].clip(lo, hi)` | `df.with_columns(pl.col("col").clip(lo, hi))` |
| `len(df)` | `df.height` 或 `len(df)` |
| `df.reset_index(drop=True)` | 不需要（polars 无 index） |
| `df.set_index(["a","b"])` | 不需要（用 filter/join 代替） |
| `df.xs(val, level="col")` | `df.filter(pl.col("col") == val)` |
| `pd.concat([a,b])` | `pl.concat([a,b])` |
| `df.assign(new_col=expr)` | `df.with_columns(expr.alias("new_col"))` |
| `df.to_parquet(path)` | `df.write_parquet(path)` |
| `pd.read_parquet(path)` | `pl.read_parquet(path)` |

## groupby + rolling（最大性能差异）

```python
# pandas (单线程, GIL)
g = df.groupby("ticker", sort=False)
df["ret"] = g["adj_close"].transform(lambda x: x.pct_change())
df["vol_20d"] = g["ret"].transform(lambda x: x.rolling(20, min_periods=10).std())

# polars (多线程, 无 GIL)
df = df.with_columns(
    pl.col("adj_close").pct_change().over("ticker").alias("ret"),
)
df = df.with_columns(
    pl.col("ret").rolling_std(20, min_periods=10).over("ticker").alias("vol_20d"),
)
```

## numpy 互操作

```python
# polars → numpy
arr = df["col"].to_numpy()

# numpy → polars column
df = df.with_columns(pl.Series("result", numpy_array))
```

## Django ORM 查询结果 → polars

```python
# 之前
df = pd.DataFrame(Model.objects.filter(...).values_list(*cols), columns=cols)

# 之后
rows = list(Model.objects.filter(...).values_list(*cols))
df = pl.DataFrame(rows, schema=cols, orient="row")
```

## parquet 读写

```python
# polars 读 parquet 比 pandas 快 5-10x
df = pl.read_parquet(path)

# 写
df.write_parquet(path)
```

## 注意事项

1. polars 没有 index，所有用 MultiIndex / xs() 的地方改用 filter
2. polars 是 lazy 优先，但我们用 eager mode（直接 pl.DataFrame）简化迁移
3. `df.to_pandas()` 可以在边界处临时转换（仅用于 matplotlib 绑图等无法避免的场景）
4. polars Series 和 numpy array 可以零拷贝互转
