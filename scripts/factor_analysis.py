#!/usr/bin/env python3
"""
全因子分析框架（IC 矩阵 / Fama-MacBeth / 因子衰减）。

架构：
    Phase 1: 多进程构建因子面板 (fork COW 共享缓存，不复制内存)
    Phase 2: 向量化前瞻收益矩阵（pivot + shift）
    Phase 3: IC / FM / Decay 纯 numpy（秒级）

用法:
    rm -f cache/*.parquet  # 首次需删旧缓存
    python3 scripts/factor_analysis.py --start 2012-01-01 --end 2025-12-31
    python3 scripts/factor_analysis.py --start 2012-01-01 --end 2025-12-31 --workers 6
"""

import argparse
import logging
import multiprocessing as mp
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

# Legacy 因子 imports（未迁移到 AlphaSignal registry 的因子）
from stocks.services.factors.us_value import EP, BP, DivYield  # noqa: E402
from stocks.services.factors.us_growth import NetProfitYoY, RevenueYoY, NetProfitCAGR3Y  # noqa: E402
from stocks.services.factors.us_momentum import Mom1M, Mom3M, Mom12M, Rev5D  # noqa: E402
from stocks.services.factors.us_technical import Turn20D, Vol20D, Ivol, Size  # noqa: E402
from stocks.services.factors.us_analyst import USAnalystRating, USAnalystCoverage  # noqa: E402
from stocks.services.factors.us_accruals import BuybackYield  # noqa: E402
from stocks.services.factors.us_earnings import EarningsSurprise, EpsRevision  # noqa: E402
from stocks.services.factors.us_insider import InsiderNetBuy  # noqa: E402
from stocks.services.factors.us_polymarket import PolymarketSent  # noqa: E402
from stocks.services.factors.us_quiver import LobbyIntensity, GovContract  # noqa: E402
from stocks.services.factors.us_alphavantage import NewsSentiment  # noqa: E402

logger = logging.getLogger("factor_analysis")
logger.setLevel(LOG_LEVEL)
logging.basicConfig(
    level=LOG_LEVEL, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)

HORIZONS = [21, 42, 63, 126, 252]  # 1M, 2M, 3M, 6M, 12M

# Legacy 因子名 → 类 映射（未迁移到 AlphaSignal registry）
_LEGACY_FACTOR_MAP: dict[str, type] = {
    "EP": EP, "BP": BP, "DIV_YIELD": DivYield, "BUYBACK_YIELD": BuybackYield,
    "NET_PROFIT_YOY": NetProfitYoY, "REVENUE_YOY": RevenueYoY,
    "NET_PROFIT_CAGR_3Y": NetProfitCAGR3Y,
    "MOM_1M": Mom1M, "MOM_3M": Mom3M, "MOM_12M": Mom12M, "REV_5D": Rev5D,
    "TURN_20D": Turn20D, "VOL_20D": Vol20D, "IVOL": Ivol, "SIZE": Size,
    "US_ANALYST_RATING": USAnalystRating, "US_ANALYST_COVERAGE": USAnalystCoverage,
    "EARNINGS_SURPRISE": EarningsSurprise, "EPS_REVISION": EpsRevision,
    "INSIDER_NET_BUY": InsiderNetBuy,
    "LOBBY_INTENSITY": LobbyIntensity, "GOV_CONTRACT": GovContract,
}


def _get_all_factors() -> dict[str, type]:
    """合并 AlphaSignal registry + legacy 因子，返回 {name: class}。"""
    combined = {n: c for n, c in get_registered().items() if c.status in ("live", "staging")}
    # Legacy 因子补入（不覆盖已在 registry 中的同名因子）
    for name, cls in _LEGACY_FACTOR_MAP.items():
        if name not in combined:
            combined[name] = cls
    return combined


# ======================================================================
# 多线程 worker 全局状态
# ======================================================================

_WORKER_UNIVERSES: dict[str, pd.DataFrame] = {}  # {date: universe_df}
_WORKER_FACTOR_NAMES: list[str] = []
_WORKER_ALL_FACTORS: dict[str, type] = {}  # {name: class} 合并后的完整因子表


def _compute_single_date(date: str) -> tuple[str, pd.DataFrame | None]:
    """Worker 函数：计算一个日期的全部因子截面。

    通过 fork COW 继承 _static_cache（~700MB）和 _WORKER_UNIVERSES，不复制内存。
    """
    import traceback as _tb

    try:
        universe = _WORKER_UNIVERSES.get(date)
        if universe is None or universe.empty:
            return date, None

        all_factors = _WORKER_ALL_FACTORS
        tickers = universe["ticker"].tolist()
        result = pd.DataFrame({"ticker": tickers})

        n_ok = 0
        n_fail = 0
        for fname in _WORKER_FACTOR_NAMES:
            cls = all_factors.get(fname)
            if cls is None:
                result[fname] = np.nan
                continue
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
                result[fname] = np.nan
                n_fail += 1

        print(f"  {date}: {n_ok}/{len(_WORKER_FACTOR_NAMES)} ok, {n_fail} fail, {len(tickers)} tickers",
              flush=True)
        return date, result

    except Exception as e:
        print(f"  {date}: WORKER ERROR: {e}\n{_tb.format_exc()}", flush=True)
        return date, None


# ======================================================================
# Phase 1: 构建因子面板
# ======================================================================


def _spawn_worker(args: tuple) -> list[tuple[str, pd.DataFrame | None]]:
    """fork worker: 通过 COW 继承主进程的 _static_cache，不需要重新加载数据。"""
    worker_id, dates_chunk, factor_names, universe_dict_path, full_start, full_end = args

    import time as _time
    t_start = _time.time()

    # fork 后关闭从主进程继承的死 DB 连接
    from django.db import connections
    connections.close_all()

    print(f"  Worker {worker_id}: ready {_time.time()-t_start:.1f}s, "
          f"{len(dates_chunk)} dates", flush=True)

    # 3. 加载 universe（主进程预算好存到 pickle）
    import pickle
    with open(universe_dict_path, "rb") as f:
        universes = pickle.load(f)

    # 4. 构建因子注册表
    import stocks.services.factors.signals  # noqa: F401 — 触发 @register
    all_factors = _get_all_factors()

    # 5. 计算因子（带计时，找慢因子）
    results = []
    slow_factors: dict[str, float] = {}  # {factor_name: total_seconds}

    for date in dates_chunk:
        universe = universes.get(date)
        if universe is None or universe.empty:
            results.append((date, None))
            continue

        tickers = universe["ticker"].tolist()
        result = pd.DataFrame({"ticker": tickers})
        n_ok = 0
        date_t0 = _time.time()

        for fname in factor_names:
            cls = all_factors.get(fname)
            if cls is None:
                result[fname] = np.nan
                continue
            try:
                ft0 = _time.time()
                sig = cls()
                df = sig.compute(date, universe)
                elapsed_f = _time.time() - ft0
                slow_factors[fname] = slow_factors.get(fname, 0.0) + elapsed_f

                if df.empty:
                    result[fname] = np.nan
                else:
                    result = result.merge(
                        df.rename(columns={"factor_value": fname}),
                        on="ticker", how="left",
                    )
                    n_ok += 1
            except Exception:
                result[fname] = np.nan

        dt = _time.time() - date_t0
        dates_done = len(results) + 1
        print(f"  Worker {worker_id} | {date}: {n_ok}/{len(factor_names)} factors, "
              f"{len(tickers)} tickers, {dt:.1f}s [{dates_done}/{len(dates_chunk)}]", flush=True)
        results.append((date, result))

        # 每 5 个日期打印阶段性慢因子 Top 10
        if dates_done % 5 == 0 or dates_done == len(dates_chunk):
            top_slow = sorted(slow_factors.items(), key=lambda x: x[1], reverse=True)[:10]
            print(f"  Worker {worker_id} 慢因子 Top 10 (after {dates_done} dates):", flush=True)
            for fname, total_s in top_slow:
                avg_s = total_s / dates_done
                print(f"    {fname:35s} total={total_s:6.1f}s  avg={avg_s:.2f}s/date", flush=True)

    return results


def build_factor_panel(
    registry: dict, dates: list[str], n_workers: int = 1,
    start_date: str = "", end_date: str = "",
) -> dict[str, pd.DataFrame]:
    """计算所有因子 × 所有日期的面板。

    n_workers=1: 单进程
    n_workers>1: multiprocessing spawn（真多核并行，无 GIL）
    """
    global _WORKER_UNIVERSES, _WORKER_FACTOR_NAMES, _WORKER_ALL_FACTORS

    factor_names = sorted(registry.keys())
    _WORKER_FACTOR_NAMES = factor_names
    _WORKER_ALL_FACTORS = registry

    # 预先计算所有 universe（串行，首次后走缓存）
    logger.info(f"Pre-computing universes for {len(dates)} dates...")
    t0 = time.time()
    for date in dates:
        _WORKER_UNIVERSES[date] = get_us_clean_universe(date)
    logger.info(f"Universes done: {time.time() - t0:.1f}s")

    if n_workers <= 1:
        panel = {}
        for i, date in enumerate(dates):
            t0 = time.time()
            date, result = _compute_single_date(date)
            if result is not None:
                panel[date] = result
            dt = time.time() - t0
            logger.info(f"[{i+1}/{len(dates)}] {date}: {dt:.1f}s")
        return panel

    # === multiprocessing fork（COW 共享缓存，不复制内存） ===
    logger.info(f"Launching {n_workers} fork workers (~{_mem_mb():.0f}MB cache, {len(dates)} dates)...")

    # 存 universe 到临时 pickle（供 worker 读取）
    import pickle, tempfile
    universe_path = Path(tempfile.mktemp(suffix=".pkl"))
    with open(universe_path, "wb") as f:
        pickle.dump(_WORKER_UNIVERSES, f)

    # 分割日期（连续块，不交错，便于数据子集优化）
    chunk_size = (len(dates) + n_workers - 1) // n_workers
    chunks = [dates[i:i+chunk_size] for i in range(0, len(dates), chunk_size)]
    worker_args = [
        (i, chunk, factor_names, str(universe_path), start_date, end_date)
        for i, chunk in enumerate(chunks) if chunk
    ]

    # fork 多进程（COW 共享 _static_cache，preload 已串行化无线程残留）
    from django.db import connections
    connections.close_all()
    ctx = mp.get_context("fork")
    t_start = time.time()
    with ctx.Pool(processes=len(worker_args)) as pool:
        all_results = pool.map(_spawn_worker, worker_args)

    # 清理临时文件
    universe_path.unlink(missing_ok=True)

    # 合并结果
    panel = {}
    for batch in all_results:
        for date, result in batch:
            if result is not None:
                panel[date] = result

    logger.info(f"Spawn workers done: {len(panel)} dates, {time.time() - t_start:.1f}s")
    return panel


def _mem_mb() -> float:
    """当前进程 RSS (MB)。"""
    try:
        import resource
        return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024 / 1024
    except Exception:
        return 0.0


# ======================================================================
# Phase 2: 前瞻收益矩阵
# ======================================================================


def build_forward_return_matrix(
    start: str, end: str, dates: list[str], horizons: list[int] = None,
) -> dict[int, pd.DataFrame]:
    """全量价格 pivot → shift 向量化计算前瞻收益。"""
    if horizons is None:
        horizons = HORIZONS

    logger.info(f"Building forward return matrix...")
    t0 = time.time()

    cache = AlphaSignal._static_cache.get("_alpha_daily")
    if cache is None or cache.empty:
        logger.warning("_alpha_daily 缓存为空")
        return {}

    price_wide = cache.pivot_table(
        index="trade_date", columns="ticker", values="adj_close", aggfunc="last"
    )
    price_wide = price_wide.sort_index()

    result = {}
    for h in horizons:
        fwd = price_wide.shift(-h) / price_wide - 1.0
        result[h] = fwd

    logger.info(f"Forward returns done: {len(horizons)} horizons, {time.time() - t0:.1f}s")
    return result


def get_fwd_for_date(
    fwd_matrices: dict[int, pd.DataFrame], date: str,
    tickers: list[str], horizon: int,
) -> pd.Series:
    """从前瞻矩阵取某日某 horizon 收益。"""
    mat = fwd_matrices.get(horizon)
    if mat is None:
        return pd.Series(dtype=float)
    date_ts = pd.Timestamp(date)
    idx = mat.index
    valid = idx[idx >= date_ts]
    if valid.empty:
        valid = idx[idx <= date_ts]
        if valid.empty:
            return pd.Series(dtype=float)
        actual = valid[-1]
    else:
        actual = valid[0]
    row = mat.loc[actual]
    common = [t for t in tickers if t in row.index]
    return row[common]


# ======================================================================
# Phase 3a: IC
# ======================================================================


def compute_ic_from_panel(
    panel: dict, fwd_matrices: dict, factor_names: list, horizon: int = 21,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    logger.info(f"Computing IC (horizon={horizon}d)...")
    t0 = time.time()
    ic_records = []

    for date, factor_df in sorted(panel.items()):
        tickers = factor_df["ticker"].tolist()
        fwd_ret = get_fwd_for_date(fwd_matrices, date, tickers, horizon)
        if fwd_ret.empty or fwd_ret.notna().sum() < 50:
            continue

        row = {"date": date}
        idx = factor_df.set_index("ticker")
        for fname in factor_names:
            if fname not in idx.columns:
                row[fname] = np.nan
                continue
            v = idx[fname]
            common = v.index.intersection(fwd_ret.index)
            v2, r2 = v.loc[common].astype(float), fwd_ret.loc[common].astype(float)
            valid = v2.notna() & r2.notna()
            if valid.sum() < 30:
                row[fname] = np.nan
                continue
            ic, _ = sp_stats.spearmanr(v2[valid], r2[valid])
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
            rows.append({"factor": fname, "n_months": n, "mean_ic": np.nan,
                          "std_ic": np.nan, "icir": np.nan, "t_stat": np.nan, "pct_pos": np.nan})
            continue
        m, s = ics.mean(), ics.std()
        rows.append({
            "factor": fname, "n_months": n, "mean_ic": m, "std_ic": s,
            "icir": m / s if s > 1e-10 else np.nan,
            "t_stat": m / (s / np.sqrt(n)) if s > 1e-10 else np.nan,
            "pct_pos": (ics > 0).mean(),
        })
    ic_summary = pd.DataFrame(rows).sort_values("icir", ascending=False, key=abs)
    return ic_matrix, ic_summary


# ======================================================================
# Phase 3b: Fama-MacBeth
# ======================================================================


def fama_macbeth_from_panel(
    panel: dict, fwd_matrices: dict, factor_names: list, horizon: int = 21,
) -> pd.DataFrame:
    logger.info(f"Fama-MacBeth (horizon={horizon}d)...")
    t0 = time.time()
    gamma_records = []

    for date, factor_df in sorted(panel.items()):
        tickers = factor_df["ticker"].tolist()
        fwd_ret = get_fwd_for_date(fwd_matrices, date, tickers, horizon)
        if fwd_ret.empty or fwd_ret.notna().sum() < 100:
            continue

        idx = factor_df.set_index("ticker")
        avail, X_cols = [], []
        for fname in factor_names:
            if fname not in idx.columns:
                continue
            col = idx[fname].astype(float)
            s = col.std()
            if s > 1e-10:
                X_cols.append(((col - col.mean()) / s).rename(fname))
                avail.append(fname)
        if len(avail) < 5:
            continue

        X_df = pd.concat(X_cols, axis=1)
        common = X_df.index.intersection(fwd_ret.index)
        y = fwd_ret.loc[common].astype(float)
        X = X_df.loc[common]
        valid = y.notna() & X.notna().all(axis=1)
        y, X = y[valid].values, X[valid].fillna(0).values
        if len(y) < 100:
            continue

        X = np.column_stack([np.ones(len(X)), X])
        try:
            beta, *_ = np.linalg.lstsq(X, y, rcond=None)
        except np.linalg.LinAlgError:
            continue

        gamma = {"_intercept": beta[0]}
        for j, f in enumerate(avail):
            gamma[f] = beta[j + 1]
        gamma_records.append(gamma)

    if not gamma_records:
        return pd.DataFrame()

    gdf = pd.DataFrame(gamma_records)
    rows = []
    for col in gdf.columns:
        vals = gdf[col].dropna()
        n = len(vals)
        if n < 3:
            continue
        m, s = vals.mean(), vals.std()
        rows.append({
            "factor": col, "mean_gamma": m, "std_gamma": s,
            "t_stat": m / (s / np.sqrt(n)) if s > 1e-10 else np.nan, "n_months": n,
        })
    result = pd.DataFrame(rows).sort_values("t_stat", ascending=False, key=abs)
    logger.info(f"Fama-MacBeth done: {time.time() - t0:.1f}s")
    return result


# ======================================================================
# Phase 3c: Decay
# ======================================================================


def factor_decay_from_panel(
    panel: dict, fwd_matrices: dict, factor_names: list, horizons: list = None,
) -> pd.DataFrame:
    if horizons is None:
        horizons = HORIZONS
    logger.info(f"Factor decay ({len(horizons)} horizons)...")
    t0 = time.time()

    ic_store = {f: {h: [] for h in horizons} for f in factor_names}

    for date, factor_df in sorted(panel.items()):
        tickers = factor_df["ticker"].tolist()
        idx = factor_df.set_index("ticker")
        for h in horizons:
            fwd_ret = get_fwd_for_date(fwd_matrices, date, tickers, h)
            if fwd_ret.empty or fwd_ret.notna().sum() < 50:
                continue
            for fname in factor_names:
                if fname not in idx.columns:
                    continue
                v = idx[fname]
                common = v.index.intersection(fwd_ret.index)
                v2, r2 = v.loc[common].astype(float), fwd_ret.loc[common].astype(float)
                valid = v2.notna() & r2.notna()
                if valid.sum() < 30:
                    continue
                ic, _ = sp_stats.spearmanr(v2[valid], r2[valid])
                ic_store[fname][h].append(ic)

    rows = []
    for fname in factor_names:
        row = {"factor": fname}
        for h in horizons:
            ics = ic_store[fname][h]
            row[f"ic_{h}d"] = np.mean(ics) if ics else np.nan
            row[f"icir_{h}d"] = (
                np.mean(ics) / np.std(ics) if len(ics) > 2 and np.std(ics) > 1e-10 else np.nan
            )
        rows.append(row)
    logger.info(f"Decay done: {time.time() - t0:.1f}s")
    return pd.DataFrame(rows)


# ======================================================================
# Report
# ======================================================================


def generate_report(ic_summary, fm_result, decay_df, start, end, n_dates) -> str:
    md = [f"# 全因子分析报告 ({start} → {end})",
          f"\n> {n_dates} 个月末截面，{len(ic_summary)} 个因子\n"]

    md.append("## 1. IC 排名 (|ICIR| 排序，Top 30)\n")
    md.append("| Rank | Factor | Mean IC | Std IC | ICIR | t-stat | %Pos | N |")
    md.append("|------|--------|--------:|-------:|-----:|-------:|-----:|--:|")
    for i, (_, r) in enumerate(ic_summary.head(30).iterrows(), 1):
        md.append(
            f"| {i} | {r['factor']} | {r['mean_ic']:+.4f} | {r['std_ic']:.4f} "
            f"| {r['icir']:+.3f} | {r['t_stat']:+.2f} | {r['pct_pos']:.0%} | {int(r['n_months'])} |"
        )
    md.append("")

    if not fm_result.empty:
        md.append("## 2. Fama-MacBeth (|t-stat| 排序，Top 30)\n")
        md.append("| Rank | Factor | Mean γ | t-stat | N |")
        md.append("|------|--------|-------:|-------:|--:|")
        for i, (_, r) in enumerate(fm_result[fm_result["factor"] != "_intercept"].head(30).iterrows(), 1):
            md.append(f"| {i} | {r['factor']} | {r['mean_gamma']:+.6f} | {r['t_stat']:+.2f} | {int(r['n_months'])} |")
        md.append("\n> Harvey-Liu-Zhu (2016): |t| > 3.0 才有统计显著性。\n")

    if not decay_df.empty:
        md.append("## 3. 因子衰减 (Top 30 by |ICIR 1M|)\n")
        md.append("| Factor | IC 1M | IC 2M | IC 3M | IC 6M | IC 12M | ICIR 1M |")
        md.append("|--------|------:|------:|------:|------:|-------:|--------:|")
        ds = decay_df.copy()
        ds["_s"] = ds.get("icir_21d", pd.Series(dtype=float)).abs()
        for _, r in ds.sort_values("_s", ascending=False).head(30).iterrows():
            md.append(
                f"| {r['factor']} | {r.get('ic_21d',np.nan):+.4f} | {r.get('ic_42d',np.nan):+.4f} "
                f"| {r.get('ic_63d',np.nan):+.4f} | {r.get('ic_126d',np.nan):+.4f} "
                f"| {r.get('ic_252d',np.nan):+.4f} | {r.get('icir_21d',np.nan):+.3f} |"
            )
        md.append("")

    md.append("---\n*Generated by `scripts/factor_analysis.py`*")
    return "\n".join(md)


# ======================================================================
# Main
# ======================================================================


def _split_panel_by_year(panel: dict) -> dict[str, dict]:
    """将面板按年份分组。返回 {year_str: {date: df}}。"""
    yearly = {}
    for date, df in panel.items():
        year = date[:4]
        yearly.setdefault(year, {})[date] = df
    return yearly


def _analyze_period(
    label: str, sub_panel: dict, fwd_matrices: dict, factor_names: list,
    skip_fm: bool = False, skip_decay: bool = False,
) -> dict:
    """对一个时间段（某年或全量）计算 IC / FM / Decay。"""
    result = {"label": label, "n_dates": len(sub_panel)}

    _, ic_summary = compute_ic_from_panel(sub_panel, fwd_matrices, factor_names)
    result["ic_summary"] = ic_summary

    if not skip_fm:
        result["fm"] = fama_macbeth_from_panel(sub_panel, fwd_matrices, factor_names)
    else:
        result["fm"] = pd.DataFrame()

    if not skip_decay:
        result["decay"] = factor_decay_from_panel(sub_panel, fwd_matrices, factor_names)
    else:
        result["decay"] = pd.DataFrame()

    return result


def generate_yearly_report(
    yearly_results: list[dict], total_result: dict, factor_names: list,
    start: str, end: str,
) -> str:
    """生成逐年 + 总计报告。"""
    md = [f"# 逐年因子分析报告 ({start} → {end})",
          f"\n> 共 {total_result['n_dates']} 个月末截面，{len(factor_names)} 个因子\n"]

    # ── 逐年 IC 热力图（每年 Top 20 因子的 ICIR） ──
    md.append("## 1. 逐年 ICIR 矩阵 (Top 30 by 全期 |ICIR|)\n")

    total_ic = total_result["ic_summary"]
    top_factors = total_ic.head(30)["factor"].tolist()

    # 表头
    years = [r["label"] for r in yearly_results]
    md.append("| Factor | " + " | ".join(years) + " | **Total** |")
    md.append("|--------|" + "|".join(["-------:" for _ in years]) + "|--------:|")

    # 构造 {year: {factor: icir}} 查找表
    yearly_icir = {}
    for r in yearly_results:
        ic = r["ic_summary"]
        yearly_icir[r["label"]] = dict(zip(ic["factor"], ic["icir"]))
    total_icir = dict(zip(total_ic["factor"], total_ic["icir"]))

    for fname in top_factors:
        cells = []
        for yr in years:
            v = yearly_icir.get(yr, {}).get(fname, np.nan)
            if pd.isna(v):
                cells.append("-")
            else:
                cells.append(f"{v:+.2f}")
        total_v = total_icir.get(fname, np.nan)
        total_s = f"**{total_v:+.2f}**" if not pd.isna(total_v) else "-"
        md.append(f"| {fname} | " + " | ".join(cells) + f" | {total_s} |")
    md.append("")

    # ── 全期 IC 排名 ──
    md.append("## 2. 全期 IC 排名 (|ICIR| Top 30)\n")
    md.append("| Rank | Factor | Mean IC | Std IC | ICIR | t-stat | %Pos | N |")
    md.append("|------|--------|--------:|-------:|-----:|-------:|-----:|--:|")
    for i, (_, r) in enumerate(total_ic.head(30).iterrows(), 1):
        star = " ***" if abs(r.get("t_stat", 0)) > 3.0 else " **" if abs(r.get("t_stat", 0)) > 2.0 else ""
        md.append(
            f"| {i} | {r['factor']} | {r['mean_ic']:+.4f} | {r['std_ic']:.4f} "
            f"| {r['icir']:+.3f} | {r['t_stat']:+.2f}{star} | {r['pct_pos']:.0%} | {int(r['n_months'])} |"
        )
    md.append("")

    # ── 全期 Fama-MacBeth ──
    fm = total_result.get("fm", pd.DataFrame())
    if not fm.empty:
        md.append("## 3. 全期 Fama-MacBeth (|t-stat| Top 30)\n")
        md.append("| Rank | Factor | Mean γ | t-stat | N |")
        md.append("|------|--------|-------:|-------:|--:|")
        for i, (_, r) in enumerate(fm[fm["factor"] != "_intercept"].head(30).iterrows(), 1):
            md.append(f"| {i} | {r['factor']} | {r['mean_gamma']:+.6f} | {r['t_stat']:+.2f} | {int(r['n_months'])} |")
        md.append("\n> Harvey-Liu-Zhu (2016): |t| > 3.0 才有统计显著性。\n")

    # ── 全期因子衰减 ──
    decay = total_result.get("decay", pd.DataFrame())
    if not decay.empty:
        md.append("## 4. 全期因子衰减 (Top 30 by |ICIR 1M|)\n")
        md.append("| Factor | IC 1M | IC 2M | IC 3M | IC 6M | IC 12M | ICIR 1M |")
        md.append("|--------|------:|------:|------:|------:|-------:|--------:|")
        ds = decay.copy()
        ds["_s"] = ds.get("icir_21d", pd.Series(dtype=float)).abs()
        for _, r in ds.sort_values("_s", ascending=False).head(30).iterrows():
            md.append(
                f"| {r['factor']} | {r.get('ic_21d',np.nan):+.4f} | {r.get('ic_42d',np.nan):+.4f} "
                f"| {r.get('ic_63d',np.nan):+.4f} | {r.get('ic_126d',np.nan):+.4f} "
                f"| {r.get('ic_252d',np.nan):+.4f} | {r.get('icir_21d',np.nan):+.3f} |"
            )
        md.append("")

    # ── 逐年 Top 5 ──
    md.append("## 5. 逐年 Top 5 因子\n")
    for r in yearly_results:
        ic = r["ic_summary"]
        md.append(f"### {r['label']} ({r['n_dates']} months)\n")
        md.append("| Rank | Factor | ICIR | Mean IC | t-stat |")
        md.append("|------|--------|-----:|--------:|-------:|")
        for i, (_, row) in enumerate(ic.head(5).iterrows(), 1):
            md.append(f"| {i} | {row['factor']} | {row['icir']:+.3f} | {row['mean_ic']:+.4f} | {row['t_stat']:+.2f} |")
        md.append("")

    md.append("---\n*Generated by `scripts/factor_analysis.py`*")
    return "\n".join(md)


def main():
    parser = argparse.ArgumentParser(description="全因子分析（IC / Fama-MacBeth / 衰减）")
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--workers", type=int, default=1,
                        help="并发 worker 数 (>1 用 fork COW 共享内存)")
    parser.add_argument("--skip-fm", action="store_true")
    parser.add_argument("--skip-decay", action="store_true")
    args = parser.parse_args()

    out_dir = _PROJECT_ROOT / "output" / "factor_analysis"
    out_dir.mkdir(parents=True, exist_ok=True)
    suffix = f"{args.start}_{args.end}"

    # ── Preload ──
    logger.info(f"=== Factor Analysis: {args.start} → {args.end}, workers={args.workers} ===")
    t_total = time.time()
    USFactorBase.clear_all_cache()
    USFactorBase.preload_for_backtest(args.start, args.end)
    USFactorBase.precompute_rolling_stats()
    logger.info(f"Preload done: {time.time() - t_total:.1f}s, RSS={_mem_mb():.0f}MB")

    registry = _get_all_factors()
    factor_names = sorted(registry.keys())
    # 用实际交易日（每月最后交易日），不用日历月末（可能落在周末/假日）
    from stocks.models import USIndexDaily
    trade_dates = list(
        USIndexDaily.objects.filter(
            index_code="^GSPC",
            trade_date__gte=args.start,
            trade_date__lte=args.end,
        ).order_by("trade_date").values_list("trade_date", flat=True)
    )
    if not trade_dates:
        logger.error("无交易日数据")
        return
    td_series = pd.Series(trade_dates)
    td_series.index = pd.to_datetime(td_series)
    # 每月最后一个交易日
    dates = [d.strftime("%Y-%m-%d") for d in td_series.groupby(td_series.index.to_period("M")).last()]
    logger.info(f"Factors: {len(factor_names)}, Dates: {len(dates)}")

    # ── Phase 1: 因子面板（一次性计算全量） ──
    logger.info("=" * 60)
    logger.info("Phase 1: Factor panel...")
    t0 = time.time()
    panel = build_factor_panel(registry, dates, n_workers=args.workers,
                               start_date=args.start, end_date=args.end)
    t_panel = time.time() - t0
    logger.info(f"Panel: {len(panel)} dates, {t_panel:.0f}s ({t_panel/max(len(panel),1):.1f}s/date), RSS={_mem_mb():.0f}MB")

    # 存面板
    rows_all = []
    for date, df in sorted(panel.items()):
        c = df.copy()
        c.insert(0, "date", date)
        rows_all.append(c)
    if rows_all:
        pa = pd.concat(rows_all, ignore_index=True)
        pa.to_parquet(out_dir / f"factor_panel_{suffix}.parquet", index=False)
        logger.info(f"Panel saved: {len(pa)} rows")

    # ── Phase 2: 前瞻收益（一次性计算） ──
    logger.info("=" * 60)
    fwd_matrices = build_forward_return_matrix(args.start, args.end, dates)

    # ── Phase 3: 逐年分析 + 全期汇总 ──
    logger.info("=" * 60)
    logger.info("Phase 3: Yearly + total analysis...")

    # 3a. 逐年
    yearly_panels = _split_panel_by_year(panel)
    yearly_results = []
    for year in sorted(yearly_panels.keys()):
        sub = yearly_panels[year]
        logger.info(f"  Analyzing {year} ({len(sub)} dates)...")
        r = _analyze_period(year, sub, fwd_matrices, factor_names,
                            skip_fm=True, skip_decay=True)
        yearly_results.append(r)
        # 存逐年 IC
        r["ic_summary"].to_csv(out_dir / f"ic_summary_{year}.csv", index=False)

    # 3b. 全期
    logger.info(f"  Analyzing TOTAL ({len(panel)} dates)...")
    total_result = _analyze_period("TOTAL", panel, fwd_matrices, factor_names,
                                   skip_fm=args.skip_fm, skip_decay=args.skip_decay)

    # 存全期结果
    total_result["ic_summary"].to_csv(out_dir / f"ic_summary_{suffix}.csv", index=False)
    if not total_result["fm"].empty:
        total_result["fm"].to_csv(out_dir / f"fama_macbeth_{suffix}.csv", index=False)
    if not total_result["decay"].empty:
        total_result["decay"].to_csv(out_dir / f"decay_{suffix}.csv", index=False)

    # ── Report ──
    report = generate_yearly_report(yearly_results, total_result, factor_names,
                                     args.start, args.end)
    (out_dir / f"report_{suffix}.md").write_text(report, encoding="utf-8")

    # ── Console summary ──
    t_elapsed = time.time() - t_total
    ic_total = total_result["ic_summary"]

    print("\n" + "=" * 70)
    print(f"FACTOR ANALYSIS: {args.start} → {args.end} ({len(dates)} months, {t_elapsed/60:.0f} min)")
    print("=" * 70)

    # 逐年概览
    print(f"\n{'Year':>6s} | {'#1 Factor':>30s} | {'ICIR':>6s} | {'#Dates':>6s}")
    print("-" * 60)
    for r in yearly_results:
        ic = r["ic_summary"]
        if not ic.empty:
            top = ic.iloc[0]
            print(f"{r['label']:>6s} | {top['factor']:>30s} | {top['icir']:+.3f} | {r['n_dates']:>6d}")
    print("-" * 60)

    print(f"\nTop 10 by |ICIR| (全期):")
    for i, (_, r) in enumerate(ic_total.head(10).iterrows(), 1):
        star = "***" if abs(r.get("t_stat", 0)) > 3.0 else "**" if abs(r.get("t_stat", 0)) > 2.0 else ""
        print(f"  {i:2d}. {r['factor']:30s} ICIR={r['icir']:+.3f}  IC={r['mean_ic']:+.4f}  t={r['t_stat']:+.2f} {star}")

    print(f"\nTotal: {t_elapsed/60:.0f} min, RSS={_mem_mb():.0f}MB")
    print(f"Outputs: {out_dir}/")


if __name__ == "__main__":
    main()
