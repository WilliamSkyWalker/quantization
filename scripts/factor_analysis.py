#!/usr/bin/env python3
"""
全因子分析框架（IC 矩阵 / Fama-MacBeth / 因子衰减）。

架构：
    Phase 1: 一次性构建因子面板 {date: DataFrame[ticker, f1, f2, ...]}
    Phase 2: 一次性构建前瞻收益矩阵（wide price pivot → shift）
    Phase 3: IC / FM / Decay 全在面板上做纯 numpy（秒级）

用法:
    # 删旧缓存后首次跑（~2 小时 preload）
    rm -f cache/*.parquet
    python3 scripts/factor_analysis.py --start 2012-01-01 --end 2025-12-31

    # 有缓存后重跑（<1 分钟 preload）
    python3 scripts/factor_analysis.py --start 2012-01-01 --end 2025-12-31

输出:
    output/factor_analysis/
    ├── ic_matrix_{start}_{end}.csv          — 每月每因子 IC
    ├── ic_summary_{start}_{end}.csv         — ICIR / mean_IC / t-stat 排名
    ├── fama_macbeth_{start}_{end}.csv       — FM 截面回归系数 + t-stat
    ├── decay_{start}_{end}.csv              — 多 horizon IC 衰减
    └── report_{start}_{end}.md              — 完整 markdown 报告
"""

import argparse
import logging
import os
import sys
import time
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "core.settings")

import django  # noqa: E402
django.setup()

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from scipy import stats as sp_stats  # noqa: E402

from services.config import LOG_LEVEL  # noqa: E402
from stocks.services.us_cleaner import get_us_clean_universe  # noqa: E402
from stocks.services.factors.us_base import USFactorBase  # noqa: E402
from stocks.services.factors.us_registry import AlphaSignal, get_registered  # noqa: E402
import stocks.services.factors.signals  # noqa: F401, E402

logger = logging.getLogger("factor_analysis")
logger.setLevel(LOG_LEVEL)
logging.basicConfig(
    level=LOG_LEVEL, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)

HORIZONS = [21, 42, 63, 126, 252]  # 1M, 2M, 3M, 6M, 12M


# ======================================================================
# Phase 1: 构建因子面板（一次性计算所有因子 × 所有日期）
# ======================================================================


def build_factor_panel(
    registry: dict, dates: list[str]
) -> dict[str, pd.DataFrame]:
    """对每个月末日期，计算所有因子的截面值。

    Returns:
        {date_str: DataFrame[ticker, factor1, factor2, ...]}
    """
    factor_names = sorted(registry.keys())
    panel = {}

    for i, date in enumerate(dates):
        t0 = time.time()
        universe = get_us_clean_universe(date)
        if universe.empty:
            logger.warning(f"[{i+1}/{len(dates)}] {date} universe 为空，跳过")
            continue

        tickers = universe["ticker"].tolist()
        result = pd.DataFrame({"ticker": tickers})

        n_ok = 0
        for fname in factor_names:
            cls = registry[fname]
            try:
                sig = cls()
                df = sig.compute(date, universe)
                if df.empty:
                    result[fname] = np.nan
                else:
                    result = result.merge(
                        df.rename(columns={"factor_value": fname}),
                        on="ticker", how="left",
                    )
                    n_ok += 1
            except Exception as e:
                logger.debug(f"  {fname} 报错: {e}")
                result[fname] = np.nan

        panel[date] = result
        dt = time.time() - t0
        logger.info(f"[{i+1}/{len(dates)}] {date}: {n_ok}/{len(factor_names)} factors, "
                     f"{len(tickers)} tickers, {dt:.1f}s")

    return panel


# ======================================================================
# Phase 2: 构建前瞻收益矩阵（向量化）
# ======================================================================


def build_forward_return_matrix(
    start: str, end: str, dates: list[str], horizons: list[int] = None,
) -> dict[int, pd.DataFrame]:
    """用全量价格矩阵一次性计算所有日期 × 所有 horizon 的前瞻收益。

    Returns:
        {horizon_days: DataFrame[trade_date(index), ticker(columns)] = forward_return}
    """
    if horizons is None:
        horizons = HORIZONS
    max_h = max(horizons)

    logger.info(f"Building forward return matrix (max horizon={max_h}d)...")
    t0 = time.time()

    # 从缓存读全量价格
    cache = AlphaSignal._static_cache.get("_alpha_daily")
    if cache is None or cache.empty:
        logger.warning("_alpha_daily 缓存为空，无法构建前瞻收益")
        return {}

    # Pivot 成 wide format (trade_date × ticker)
    price_wide = cache.pivot_table(
        index="trade_date", columns="ticker", values="adj_close", aggfunc="last"
    )
    price_wide = price_wide.sort_index()

    result = {}
    for h in horizons:
        # 前瞻收益 = price[t+h] / price[t] - 1
        fwd = price_wide.shift(-h) / price_wide - 1.0
        result[h] = fwd
        logger.info(f"  horizon {h}d: {fwd.shape}")

    logger.info(f"Forward return matrix done: {time.time() - t0:.1f}s")
    return result


def get_forward_returns_for_date(
    fwd_matrices: dict[int, pd.DataFrame],
    date: str,
    tickers: list[str],
    horizon: int,
) -> pd.Series:
    """从预计算的前瞻矩阵取某日某 horizon 的收益。

    Returns:
        Series[ticker] = return (NaN for missing)
    """
    mat = fwd_matrices.get(horizon)
    if mat is None:
        return pd.Series(dtype=float)

    date_ts = pd.Timestamp(date)
    # 找最近的交易日（月末可能不是交易日）
    valid_dates = mat.index[mat.index >= date_ts]
    if valid_dates.empty:
        # 往前找
        valid_dates = mat.index[mat.index <= date_ts]
        if valid_dates.empty:
            return pd.Series(dtype=float)
        actual_date = valid_dates[-1]
    else:
        actual_date = valid_dates[0]

    row = mat.loc[actual_date]
    # 只取 tickers 中有的
    common = [t for t in tickers if t in row.index]
    return row[common]


# ======================================================================
# Phase 3a: IC 矩阵
# ======================================================================


def compute_ic_from_panel(
    panel: dict[str, pd.DataFrame],
    fwd_matrices: dict[int, pd.DataFrame],
    factor_names: list[str],
    horizon: int = 21,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """从预计算面板 + 前瞻收益算 IC。

    Returns:
        (ic_matrix: DataFrame[date, f1, f2, ...],
         ic_summary: DataFrame[factor, mean_ic, std_ic, icir, t_stat, pct_pos, n_months])
    """
    logger.info(f"Computing IC matrix (horizon={horizon}d)...")
    t0 = time.time()

    ic_records = []
    for date, factor_df in panel.items():
        tickers = factor_df["ticker"].tolist()
        fwd_ret = get_forward_returns_for_date(fwd_matrices, date, tickers, horizon)
        if fwd_ret.empty or fwd_ret.notna().sum() < 50:
            continue

        row = {"date": date}
        for fname in factor_names:
            if fname not in factor_df.columns:
                row[fname] = np.nan
                continue
            vals = factor_df.set_index("ticker")[fname]
            # 对齐
            common = vals.index.intersection(fwd_ret.index)
            v = vals.loc[common].astype(float)
            r = fwd_ret.loc[common].astype(float)
            valid = v.notna() & r.notna()
            if valid.sum() < 30:
                row[fname] = np.nan
                continue
            ic, _ = sp_stats.spearmanr(v[valid], r[valid])
            row[fname] = ic
        ic_records.append(row)

    ic_matrix = pd.DataFrame(ic_records)
    logger.info(f"IC matrix: {ic_matrix.shape}, {time.time() - t0:.1f}s")

    # 汇总
    rows = []
    for fname in factor_names:
        if fname not in ic_matrix.columns:
            continue
        ics = ic_matrix[fname].dropna()
        n = len(ics)
        if n < 3:
            rows.append({"factor": fname, "n_months": n,
                          "mean_ic": np.nan, "std_ic": np.nan,
                          "icir": np.nan, "t_stat": np.nan, "pct_pos": np.nan})
            continue
        mean_ic = ics.mean()
        std_ic = ics.std()
        icir = mean_ic / std_ic if std_ic > 1e-10 else np.nan
        t_stat = mean_ic / (std_ic / np.sqrt(n)) if std_ic > 1e-10 else np.nan
        pct_pos = (ics > 0).mean()
        rows.append({
            "factor": fname, "n_months": n,
            "mean_ic": mean_ic, "std_ic": std_ic,
            "icir": icir, "t_stat": t_stat, "pct_pos": pct_pos,
        })
    ic_summary = pd.DataFrame(rows).sort_values("icir", ascending=False, key=abs)
    return ic_matrix, ic_summary


# ======================================================================
# Phase 3b: Fama-MacBeth
# ======================================================================


def fama_macbeth_from_panel(
    panel: dict[str, pd.DataFrame],
    fwd_matrices: dict[int, pd.DataFrame],
    factor_names: list[str],
    horizon: int = 21,
) -> pd.DataFrame:
    """Fama-MacBeth on pre-computed panel.

    Returns:
        DataFrame[factor, mean_gamma, std_gamma, t_stat, n_months]
    """
    logger.info(f"Running Fama-MacBeth (horizon={horizon}d)...")
    t0 = time.time()

    gamma_records = []
    for date, factor_df in panel.items():
        tickers = factor_df["ticker"].tolist()
        fwd_ret = get_forward_returns_for_date(fwd_matrices, date, tickers, horizon)
        if fwd_ret.empty or fwd_ret.notna().sum() < 100:
            continue

        # 构建 X: z-score 标准化因子矩阵
        avail_factors = []
        X_cols = []
        for fname in factor_names:
            if fname not in factor_df.columns:
                continue
            col = factor_df.set_index("ticker")[fname].astype(float)
            std = col.std()
            if std > 1e-10:
                col = (col - col.mean()) / std
                X_cols.append(col.rename(fname))
                avail_factors.append(fname)

        if len(avail_factors) < 5:
            continue

        X_df = pd.concat(X_cols, axis=1)
        # 对齐 y
        common = X_df.index.intersection(fwd_ret.index)
        y = fwd_ret.loc[common].astype(float)
        X = X_df.loc[common]
        valid = y.notna() & X.notna().all(axis=1)
        y = y[valid].values
        X = X[valid].fillna(0).values

        if len(y) < 100:
            continue

        X = np.column_stack([np.ones(len(X)), X])
        try:
            beta, *_ = np.linalg.lstsq(X, y, rcond=None)
        except np.linalg.LinAlgError:
            continue

        gamma = {"_intercept": beta[0]}
        for j, fname in enumerate(avail_factors):
            gamma[fname] = beta[j + 1]
        gamma_records.append(gamma)

    if not gamma_records:
        return pd.DataFrame()

    gamma_df = pd.DataFrame(gamma_records)

    rows = []
    for col in gamma_df.columns:
        vals = gamma_df[col].dropna()
        n = len(vals)
        if n < 3:
            continue
        mean_g = vals.mean()
        std_g = vals.std()
        t_stat = mean_g / (std_g / np.sqrt(n)) if std_g > 1e-10 else np.nan
        rows.append({
            "factor": col, "mean_gamma": mean_g,
            "std_gamma": std_g, "t_stat": t_stat, "n_months": n,
        })

    result = pd.DataFrame(rows).sort_values("t_stat", ascending=False, key=abs)
    logger.info(f"Fama-MacBeth done: {time.time() - t0:.1f}s")
    return result


# ======================================================================
# Phase 3c: 因子衰减
# ======================================================================


def factor_decay_from_panel(
    panel: dict[str, pd.DataFrame],
    fwd_matrices: dict[int, pd.DataFrame],
    factor_names: list[str],
    horizons: list[int] = None,
) -> pd.DataFrame:
    """多 horizon IC 衰减。

    Returns:
        DataFrame[factor, ic_21d, icir_21d, ic_42d, icir_42d, ...]
    """
    if horizons is None:
        horizons = HORIZONS
    logger.info(f"Computing factor decay ({len(horizons)} horizons)...")
    t0 = time.time()

    # {fname: {h: [ic_values]}}
    ic_store = {f: {h: [] for h in horizons} for f in factor_names}

    for date, factor_df in panel.items():
        tickers = factor_df["ticker"].tolist()
        for h in horizons:
            fwd_ret = get_forward_returns_for_date(fwd_matrices, date, tickers, h)
            if fwd_ret.empty or fwd_ret.notna().sum() < 50:
                continue
            for fname in factor_names:
                if fname not in factor_df.columns:
                    continue
                vals = factor_df.set_index("ticker")[fname]
                common = vals.index.intersection(fwd_ret.index)
                v = vals.loc[common].astype(float)
                r = fwd_ret.loc[common].astype(float)
                valid = v.notna() & r.notna()
                if valid.sum() < 30:
                    continue
                ic, _ = sp_stats.spearmanr(v[valid], r[valid])
                ic_store[fname][h].append(ic)

    rows = []
    for fname in factor_names:
        row = {"factor": fname}
        for h in horizons:
            ics = ic_store[fname][h]
            row[f"ic_{h}d"] = np.mean(ics) if ics else np.nan
            row[f"icir_{h}d"] = (
                np.mean(ics) / np.std(ics)
                if len(ics) > 2 and np.std(ics) > 1e-10
                else np.nan
            )
        rows.append(row)

    logger.info(f"Decay done: {time.time() - t0:.1f}s")
    return pd.DataFrame(rows)


# ======================================================================
# Report
# ======================================================================


def generate_report(
    ic_summary: pd.DataFrame,
    fm_result: pd.DataFrame,
    decay_df: pd.DataFrame,
    start: str, end: str, n_dates: int,
) -> str:
    md = []
    md.append(f"# 全因子分析报告 ({start} → {end})")
    md.append(f"\n> {n_dates} 个月末截面，59 个 AlphaSignal 因子\n")

    # IC Summary Top 30
    md.append("## 1. IC 排名 (|ICIR| 排序，Top 30)")
    md.append("")
    md.append("| Rank | Factor | Mean IC | Std IC | ICIR | t-stat | % Positive | N |")
    md.append("|------|--------|--------:|-------:|-----:|-------:|-----------:|--:|")
    for i, (_, r) in enumerate(ic_summary.head(30).iterrows(), 1):
        md.append(
            f"| {i} | {r['factor']} | {r['mean_ic']:+.4f} | {r['std_ic']:.4f} "
            f"| {r['icir']:+.3f} | {r['t_stat']:+.2f} | {r['pct_pos']:.0%} | {int(r['n_months'])} |"
        )
    md.append("")

    # Fama-MacBeth Top 30
    if not fm_result.empty:
        md.append("## 2. Fama-MacBeth 截面回归 (|t-stat| 排序，Top 30)")
        md.append("")
        md.append("| Rank | Factor | Mean γ | Std γ | t-stat | N |")
        md.append("|------|--------|-------:|------:|-------:|--:|")
        fm_top = fm_result[fm_result["factor"] != "_intercept"].head(30)
        for i, (_, r) in enumerate(fm_top.iterrows(), 1):
            md.append(
                f"| {i} | {r['factor']} | {r['mean_gamma']:+.6f} | {r['std_gamma']:.6f} "
                f"| {r['t_stat']:+.2f} | {int(r['n_months'])} |"
            )
        md.append("")
        md.append("> Harvey-Liu-Zhu (2016) 阈值: |t| > 3.0 才有统计显著性（多重检验校正）。")
        md.append("")

    # Decay Top 30
    if not decay_df.empty:
        md.append("## 3. 因子衰减 (Mean IC at Different Horizons)")
        md.append("")
        md.append("| Factor | IC 1M | IC 2M | IC 3M | IC 6M | IC 12M | ICIR 1M |")
        md.append("|--------|------:|------:|------:|------:|-------:|--------:|")
        decay_sorted = decay_df.copy()
        decay_sorted["_sort"] = decay_sorted.get("icir_21d", pd.Series(dtype=float)).abs()
        decay_sorted = decay_sorted.sort_values("_sort", ascending=False).head(30)
        for _, r in decay_sorted.iterrows():
            md.append(
                f"| {r['factor']} "
                f"| {r.get('ic_21d', np.nan):+.4f} "
                f"| {r.get('ic_42d', np.nan):+.4f} "
                f"| {r.get('ic_63d', np.nan):+.4f} "
                f"| {r.get('ic_126d', np.nan):+.4f} "
                f"| {r.get('ic_252d', np.nan):+.4f} "
                f"| {r.get('icir_21d', np.nan):+.3f} |"
            )
        md.append("")
        md.append("> IC 随 horizon 递减 = 信号衰减正常；IC 在 6-12M 仍显著 = 慢因子（Value/Quality）。")
        md.append("")

    md.append("---")
    md.append(f"\n*Generated by `scripts/factor_analysis.py`*")
    return "\n".join(md)


# ======================================================================
# Main
# ======================================================================


def main():
    parser = argparse.ArgumentParser(description="全因子分析（IC / Fama-MacBeth / 衰减）")
    parser.add_argument("--start", required=True, help="开始日期 YYYY-MM-DD")
    parser.add_argument("--end", required=True, help="结束日期 YYYY-MM-DD")
    parser.add_argument("--skip-fm", action="store_true", help="跳过 Fama-MacBeth")
    parser.add_argument("--skip-decay", action="store_true", help="跳过衰减分析")
    args = parser.parse_args()

    out_dir = _PROJECT_ROOT / "output" / "factor_analysis"
    out_dir.mkdir(parents=True, exist_ok=True)
    suffix = f"{args.start}_{args.end}"

    # ── Step 0: Preload ──
    logger.info(f"=== Factor Analysis: {args.start} → {args.end} ===")
    logger.info("Step 0: Preload data...")
    t_total = time.time()
    USFactorBase.clear_all_cache()
    USFactorBase.preload_for_backtest(args.start, args.end)
    USFactorBase.precompute_rolling_stats()
    logger.info(f"Preload done: {time.time() - t_total:.1f}s")

    registry = {n: c for n, c in get_registered().items() if c.status in ("live", "staging")}
    factor_names = sorted(registry.keys())
    logger.info(f"Active factors: {len(factor_names)}")

    dates = [d.strftime("%Y-%m-%d") for d in pd.date_range(args.start, args.end, freq="ME")]
    logger.info(f"Month-end dates: {len(dates)} ({dates[0]} → {dates[-1]})")

    # ── Phase 1: 因子面板（最耗时） ──
    logger.info("=" * 60)
    logger.info("Phase 1: Building factor panel (compute once, use thrice)...")
    t0 = time.time()
    panel = build_factor_panel(registry, dates)
    t_panel = time.time() - t0
    logger.info(f"Factor panel done: {len(panel)} dates, {t_panel:.1f}s "
                f"(avg {t_panel / max(len(panel), 1):.1f}s/date)")

    # 存面板到 parquet（便于复查）
    panel_rows = []
    for date, df in panel.items():
        df_copy = df.copy()
        df_copy.insert(0, "date", date)
        panel_rows.append(df_copy)
    if panel_rows:
        panel_all = pd.concat(panel_rows, ignore_index=True)
        panel_path = out_dir / f"factor_panel_{suffix}.parquet"
        panel_all.to_parquet(panel_path, index=False)
        logger.info(f"Factor panel saved: {panel_path} ({len(panel_all)} rows)")

    # ── Phase 2: 前瞻收益矩阵 ──
    logger.info("=" * 60)
    logger.info("Phase 2: Building forward return matrix...")
    fwd_matrices = build_forward_return_matrix(args.start, args.end, dates)

    # ── Phase 3a: IC ──
    logger.info("=" * 60)
    logger.info("Phase 3a: IC matrix...")
    ic_matrix, ic_summary = compute_ic_from_panel(panel, fwd_matrices, factor_names, horizon=21)
    ic_matrix.to_csv(out_dir / f"ic_matrix_{suffix}.csv", index=False)
    ic_summary.to_csv(out_dir / f"ic_summary_{suffix}.csv", index=False)
    logger.info(f"Top 5 by |ICIR|:")
    for _, r in ic_summary.head(5).iterrows():
        logger.info(f"  {r['factor']:30s} ICIR={r['icir']:+.3f}  IC={r['mean_ic']:+.4f}  t={r['t_stat']:+.2f}")

    # ── Phase 3b: Fama-MacBeth ──
    fm_result = pd.DataFrame()
    if not args.skip_fm:
        logger.info("=" * 60)
        logger.info("Phase 3b: Fama-MacBeth...")
        fm_result = fama_macbeth_from_panel(panel, fwd_matrices, factor_names, horizon=21)
        if not fm_result.empty:
            fm_result.to_csv(out_dir / f"fama_macbeth_{suffix}.csv", index=False)
            fm_top = fm_result[fm_result["factor"] != "_intercept"].head(5)
            logger.info(f"FM Top 5:")
            for _, r in fm_top.iterrows():
                logger.info(f"  {r['factor']:30s} γ={r['mean_gamma']:+.6f}  t={r['t_stat']:+.2f}")

    # ── Phase 3c: Decay ──
    decay_df = pd.DataFrame()
    if not args.skip_decay:
        logger.info("=" * 60)
        logger.info("Phase 3c: Decay...")
        decay_df = factor_decay_from_panel(panel, fwd_matrices, factor_names)
        if not decay_df.empty:
            decay_df.to_csv(out_dir / f"decay_{suffix}.csv", index=False)

    # ── Report ──
    logger.info("=" * 60)
    report = generate_report(ic_summary, fm_result, decay_df, args.start, args.end, len(dates))
    report_path = out_dir / f"report_{suffix}.md"
    report_path.write_text(report, encoding="utf-8")
    logger.info(f"Report: {report_path}")

    t_elapsed = time.time() - t_total
    # 终端摘要
    print("\n" + "=" * 70)
    print(f"FACTOR ANALYSIS: {args.start} → {args.end} ({len(dates)} months, {t_elapsed/60:.0f} min)")
    print("=" * 70)
    print(f"\nTop 10 by |ICIR|:")
    for i, (_, r) in enumerate(ic_summary.head(10).iterrows(), 1):
        star = "***" if abs(r["t_stat"]) > 3.0 else "**" if abs(r["t_stat"]) > 2.0 else ""
        print(f"  {i:2d}. {r['factor']:30s} ICIR={r['icir']:+.3f}  IC={r['mean_ic']:+.4f}  t={r['t_stat']:+.2f} {star}")
    print(f"\nTotal time: {t_elapsed/60:.0f} min")
    print(f"Outputs: {out_dir}/")


if __name__ == "__main__":
    main()
