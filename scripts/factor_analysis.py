#!/usr/bin/env python3
"""
全因子分析框架（IC 矩阵 / Fama-MacBeth / 因子衰减）。

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

# ======================================================================
# 1. 数据准备
# ======================================================================


def get_month_end_dates(start: str, end: str) -> list[str]:
    """返回 [start, end] 范围内所有月末交易日。"""
    dates = pd.date_range(start, end, freq="ME")
    return [d.strftime("%Y-%m-%d") for d in dates]


def compute_forward_returns(
    date: str, tickers: list[str], horizons: list[int] = None
) -> pd.DataFrame:
    """计算 date 起各 horizon（交易日）的前瞻收益率。

    Returns:
        DataFrame[ticker, fwd_1m, fwd_2m, fwd_3m, fwd_6m, fwd_12m]
    """
    if horizons is None:
        horizons = [21, 42, 63, 126, 252]  # 1M, 2M, 3M, 6M, 12M
    max_horizon = max(horizons) + 30  # buffer

    hist = AlphaSignal.fetch_price_history(
        # 取 date 之后的数据（forward-looking）
        # 需要用一个技巧：从 date 开始往后看
        date=_forward_date(date, max_horizon),
        tickers=tickers,
        lookback_days=max_horizon + 10,
        columns=["adj_close"],
    )
    if hist.empty:
        return pd.DataFrame(columns=["ticker"] + [f"fwd_{h}d" for h in horizons])

    date_ts = pd.Timestamp(date)
    # 只保留 date 当天及之后
    hist = hist[hist["trade_date"] >= date_ts]
    if hist.empty:
        return pd.DataFrame(columns=["ticker"] + [f"fwd_{h}d" for h in horizons])

    # Pivot wide
    wide = hist.pivot(index="trade_date", columns="ticker", values="adj_close")
    wide = wide.sort_index()

    if len(wide) < 2:
        return pd.DataFrame(columns=["ticker"] + [f"fwd_{h}d" for h in horizons])

    # 基准价格 = date 当天或之后最近的交易日
    base_prices = wide.iloc[0]  # 第一行

    result = pd.DataFrame({"ticker": wide.columns})
    for h in horizons:
        if h < len(wide):
            fwd_prices = wide.iloc[h]
            fwd_ret = (fwd_prices / base_prices) - 1.0
            result[f"fwd_{h}d"] = fwd_ret.values
        else:
            result[f"fwd_{h}d"] = np.nan

    return result


def _forward_date(date: str, days: int) -> str:
    """date + days 个日历日。"""
    return (pd.Timestamp(date) + pd.Timedelta(days=int(days * 1.5))).strftime("%Y-%m-%d")


# ======================================================================
# 2. IC 矩阵
# ======================================================================


def compute_ic_matrix(
    factor_registry: dict,
    dates: list[str],
    horizon_days: int = 21,
) -> pd.DataFrame:
    """计算所有因子在每个月末的截面 Spearman IC。

    Returns:
        DataFrame[date, factor1_ic, factor2_ic, ...]
    """
    factor_names = sorted(factor_registry.keys())
    ic_records = []

    for i, date in enumerate(dates):
        t0 = time.time()
        # forward return (1M)
        universe = get_us_clean_universe(date)
        if universe.empty:
            logger.warning(f"[{i+1}/{len(dates)}] {date} universe 为空，跳过")
            continue

        tickers = universe["ticker"].tolist()
        fwd = compute_forward_returns(date, tickers, horizons=[horizon_days])
        fwd_col = f"fwd_{horizon_days}d"
        if fwd.empty or fwd_col not in fwd.columns:
            logger.warning(f"[{i+1}/{len(dates)}] {date} 无前瞻收益，跳过")
            continue
        fwd = fwd[["ticker", fwd_col]].dropna()
        if len(fwd) < 50:
            logger.warning(f"[{i+1}/{len(dates)}] {date} 前瞻收益覆盖不足 ({len(fwd)})")
            continue

        row = {"date": date}

        # 逐因子计算 IC
        for fname in factor_names:
            cls = factor_registry[fname]
            try:
                sig = cls()
                df = sig.compute(date, universe)
                if df.empty:
                    row[fname] = np.nan
                    continue
                merged = df.merge(fwd, on="ticker", how="inner").dropna()
                if len(merged) < 30:
                    row[fname] = np.nan
                    continue
                ic, _ = sp_stats.spearmanr(merged["factor_value"], merged[fwd_col])
                row[fname] = ic
            except Exception as e:
                logger.warning(f"  {fname} 报错: {e}")
                row[fname] = np.nan

        ic_records.append(row)
        dt = time.time() - t0
        # 简要日志
        n_valid = sum(1 for f in factor_names if not np.isnan(row.get(f, np.nan)))
        logger.info(f"[{i+1}/{len(dates)}] {date}: {n_valid}/{len(factor_names)} factors, {dt:.1f}s")

    return pd.DataFrame(ic_records)


def summarize_ic(ic_matrix: pd.DataFrame, factor_names: list[str]) -> pd.DataFrame:
    """从 IC 矩阵汇总：mean_IC, std_IC, ICIR, t_stat, pct_positive。"""
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
            "factor": fname,
            "n_months": n,
            "mean_ic": mean_ic,
            "std_ic": std_ic,
            "icir": icir,
            "t_stat": t_stat,
            "pct_pos": pct_pos,
        })
    df = pd.DataFrame(rows)
    df = df.sort_values("icir", ascending=False, key=abs)
    return df


# ======================================================================
# 3. Fama-MacBeth 截面回归
# ======================================================================


def fama_macbeth(
    factor_registry: dict,
    dates: list[str],
    horizon_days: int = 21,
) -> pd.DataFrame:
    """Fama-MacBeth 两步法：每月截面回归 → 系数时间序列 → t 检验。

    Step 1: 每月 OLS: R_{i,t+1} = γ₀ + Σ γ_k · F_{i,k,t} + ε_{i,t}
    Step 2: γ_k 时间序列 → mean(γ_k), t-stat(γ_k)

    Returns:
        DataFrame[factor, mean_gamma, std_gamma, t_stat, n_months]
    """
    factor_names = sorted(factor_registry.keys())
    gamma_records = []  # list of dicts: {factor1: γ, factor2: γ, ...}

    for i, date in enumerate(dates):
        t0 = time.time()
        universe = get_us_clean_universe(date)
        if universe.empty:
            continue

        tickers = universe["ticker"].tolist()
        fwd = compute_forward_returns(date, tickers, horizons=[horizon_days])
        fwd_col = f"fwd_{horizon_days}d"
        if fwd.empty or fwd_col not in fwd.columns:
            continue
        fwd = fwd[["ticker", fwd_col]].dropna()

        # 计算所有因子截面值
        factor_df = pd.DataFrame({"ticker": tickers})
        computed_factors = []
        for fname in factor_names:
            cls = factor_registry[fname]
            try:
                sig = cls()
                df = sig.compute(date, universe)
                if df.empty:
                    continue
                # z-score 标准化（截面内）
                vals = pd.to_numeric(df["factor_value"], errors="coerce")
                mean_v = vals.mean()
                std_v = vals.std()
                if std_v > 1e-10:
                    df = df.copy()
                    df["factor_value"] = (vals - mean_v) / std_v
                    factor_df = factor_df.merge(
                        df[["ticker", "factor_value"]].rename(columns={"factor_value": fname}),
                        on="ticker", how="left",
                    )
                    computed_factors.append(fname)
            except Exception:
                pass

        if len(computed_factors) < 5:
            continue

        # merge forward return
        merged = factor_df.merge(fwd, on="ticker", how="inner").dropna(subset=[fwd_col])
        if len(merged) < 100:
            continue

        # 截面 OLS
        y = merged[fwd_col].values
        X = merged[computed_factors].fillna(0).values
        X = np.column_stack([np.ones(len(X)), X])  # intercept

        try:
            beta, *_ = np.linalg.lstsq(X, y, rcond=None)
        except np.linalg.LinAlgError:
            continue

        gamma = {"_intercept": beta[0]}
        for j, fname in enumerate(computed_factors):
            gamma[fname] = beta[j + 1]
        gamma_records.append(gamma)

        dt = time.time() - t0
        if (i + 1) % 12 == 0:
            logger.info(f"FM [{i+1}/{len(dates)}] {date}: {len(computed_factors)} factors, {dt:.1f}s")

    if not gamma_records:
        return pd.DataFrame()

    gamma_df = pd.DataFrame(gamma_records)

    # Step 2: 时间序列汇总
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
            "factor": col,
            "mean_gamma": mean_g,
            "std_gamma": std_g,
            "t_stat": t_stat,
            "n_months": n,
        })
    result = pd.DataFrame(rows)
    result = result.sort_values("t_stat", ascending=False, key=abs)
    return result


# ======================================================================
# 4. 因子衰减分析
# ======================================================================


def factor_decay(
    factor_registry: dict,
    dates: list[str],
    horizons: list[int] = None,
) -> pd.DataFrame:
    """对每个因子，计算不同前瞻 horizon 的平均 IC（衰减曲线）。

    Returns:
        DataFrame[factor, ic_1m, ic_2m, ic_3m, ic_6m, ic_12m]
    """
    if horizons is None:
        horizons = [21, 42, 63, 126, 252]

    factor_names = sorted(factor_registry.keys())
    # 存储: {fname: {horizon: [ic_values]}}
    ic_by_factor_horizon = {f: {h: [] for h in horizons} for f in factor_names}

    for i, date in enumerate(dates):
        t0 = time.time()
        universe = get_us_clean_universe(date)
        if universe.empty:
            continue

        tickers = universe["ticker"].tolist()
        fwd = compute_forward_returns(date, tickers, horizons=horizons)
        if fwd.empty:
            continue

        for fname in factor_names:
            cls = factor_registry[fname]
            try:
                sig = cls()
                df = sig.compute(date, universe)
                if df.empty:
                    continue
                for h in horizons:
                    fwd_col = f"fwd_{h}d"
                    if fwd_col not in fwd.columns:
                        continue
                    merged = df.merge(fwd[["ticker", fwd_col]], on="ticker", how="inner").dropna()
                    if len(merged) < 30:
                        continue
                    ic, _ = sp_stats.spearmanr(merged["factor_value"], merged[fwd_col])
                    ic_by_factor_horizon[fname][h].append(ic)
            except Exception:
                pass

        dt = time.time() - t0
        if (i + 1) % 12 == 0:
            logger.info(f"Decay [{i+1}/{len(dates)}] {date}: {dt:.1f}s")

    # 汇总
    rows = []
    for fname in factor_names:
        row = {"factor": fname}
        for h in horizons:
            ics = ic_by_factor_horizon[fname][h]
            row[f"ic_{h}d"] = np.mean(ics) if ics else np.nan
            row[f"icir_{h}d"] = (np.mean(ics) / np.std(ics)) if len(ics) > 2 and np.std(ics) > 1e-10 else np.nan
        rows.append(row)
    return pd.DataFrame(rows)


# ======================================================================
# 5. Markdown 报告
# ======================================================================


def generate_report(
    ic_summary: pd.DataFrame,
    fm_result: pd.DataFrame,
    decay_df: pd.DataFrame,
    start: str,
    end: str,
    n_dates: int,
) -> str:
    md = []
    md.append(f"# 全因子分析报告 ({start} → {end})")
    md.append(f"\n> {n_dates} 个月末截面，59 个 AlphaSignal 因子\n")

    # IC Summary Top 20
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

    # Fama-MacBeth Top 20
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

    # Decay
    if not decay_df.empty:
        md.append("## 3. 因子衰减 (Mean IC at Different Horizons)")
        md.append("")
        md.append("| Factor | IC 1M | IC 2M | IC 3M | IC 6M | IC 12M | ICIR 1M |")
        md.append("|--------|------:|------:|------:|------:|-------:|--------:|")
        # 按 1M ICIR 排序
        decay_sorted = decay_df.copy()
        decay_sorted["_sort"] = decay_sorted["icir_21d"].abs()
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
# 6. Main
# ======================================================================


def main():
    parser = argparse.ArgumentParser(description="全因子分析（IC / Fama-MacBeth / 衰减）")
    parser.add_argument("--start", required=True, help="开始日期 YYYY-MM-DD")
    parser.add_argument("--end", required=True, help="结束日期 YYYY-MM-DD")
    parser.add_argument("--skip-fm", action="store_true", help="跳过 Fama-MacBeth（最慢）")
    parser.add_argument("--skip-decay", action="store_true", help="跳过衰减分析")
    args = parser.parse_args()

    # 1. Preload
    logger.info(f"=== Factor Analysis: {args.start} → {args.end} ===")
    logger.info("Step 0: Preload data...")
    t0 = time.time()
    USFactorBase.clear_all_cache()
    USFactorBase.preload_for_backtest(args.start, args.end)
    USFactorBase.precompute_rolling_stats()
    logger.info(f"Preload done: {time.time() - t0:.1f}s")

    # 因子注册表（只 active）
    registry = {n: c for n, c in get_registered().items() if c.status in ("live", "staging")}
    factor_names = sorted(registry.keys())
    logger.info(f"Active factors: {len(factor_names)}")

    dates = get_month_end_dates(args.start, args.end)
    logger.info(f"Month-end dates: {len(dates)} ({dates[0]} → {dates[-1]})")

    out_dir = _PROJECT_ROOT / "output" / "factor_analysis"
    out_dir.mkdir(parents=True, exist_ok=True)
    suffix = f"{args.start}_{args.end}"

    # 2. IC Matrix
    logger.info("=" * 60)
    logger.info("Step 1: Computing IC matrix...")
    t0 = time.time()
    ic_matrix = compute_ic_matrix(registry, dates, horizon_days=21)
    logger.info(f"IC matrix done: {time.time() - t0:.1f}s, shape={ic_matrix.shape}")

    ic_matrix.to_csv(out_dir / f"ic_matrix_{suffix}.csv", index=False)

    ic_summary = summarize_ic(ic_matrix, factor_names)
    ic_summary.to_csv(out_dir / f"ic_summary_{suffix}.csv", index=False)
    logger.info(f"IC summary saved. Top 5 by |ICIR|:")
    for _, r in ic_summary.head(5).iterrows():
        logger.info(f"  {r['factor']:30s} ICIR={r['icir']:+.3f}  mean_IC={r['mean_ic']:+.4f}  t={r['t_stat']:+.2f}")

    # 3. Fama-MacBeth
    fm_result = pd.DataFrame()
    if not args.skip_fm:
        logger.info("=" * 60)
        logger.info("Step 2: Fama-MacBeth cross-sectional regression...")
        t0 = time.time()
        fm_result = fama_macbeth(registry, dates, horizon_days=21)
        logger.info(f"Fama-MacBeth done: {time.time() - t0:.1f}s")
        if not fm_result.empty:
            fm_result.to_csv(out_dir / f"fama_macbeth_{suffix}.csv", index=False)
            fm_top = fm_result[fm_result["factor"] != "_intercept"].head(5)
            logger.info(f"FM Top 5 by |t-stat|:")
            for _, r in fm_top.iterrows():
                logger.info(f"  {r['factor']:30s} γ={r['mean_gamma']:+.6f}  t={r['t_stat']:+.2f}")

    # 4. Decay
    decay_df = pd.DataFrame()
    if not args.skip_decay:
        logger.info("=" * 60)
        logger.info("Step 3: Factor decay analysis...")
        t0 = time.time()
        decay_df = factor_decay(registry, dates, horizons=[21, 42, 63, 126, 252])
        logger.info(f"Decay done: {time.time() - t0:.1f}s")
        if not decay_df.empty:
            decay_df.to_csv(out_dir / f"decay_{suffix}.csv", index=False)

    # 5. Report
    logger.info("=" * 60)
    logger.info("Generating report...")
    report = generate_report(ic_summary, fm_result, decay_df, args.start, args.end, len(dates))
    report_path = out_dir / f"report_{suffix}.md"
    report_path.write_text(report, encoding="utf-8")
    logger.info(f"Report: {report_path}")

    # 终端摘要
    print("\n" + "=" * 70)
    print(f"FACTOR ANALYSIS: {args.start} → {args.end} ({len(dates)} months)")
    print("=" * 70)
    print(f"\nTop 10 Factors by |ICIR|:")
    for i, (_, r) in enumerate(ic_summary.head(10).iterrows(), 1):
        star = "***" if abs(r["t_stat"]) > 3.0 else "**" if abs(r["t_stat"]) > 2.0 else ""
        print(f"  {i:2d}. {r['factor']:30s} ICIR={r['icir']:+.3f}  IC={r['mean_ic']:+.4f}  t={r['t_stat']:+.2f} {star}")
    print(f"\nOutputs: {out_dir}/")


if __name__ == "__main__":
    main()
