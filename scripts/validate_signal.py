#!/usr/bin/env python3
"""
单因子截面验证器（轻量，不跑回测）。

用法:
    python3 scripts/validate_signal.py --signal PIOTROSKI_F --start 2024-11-01 --end 2024-12-31
    python3 scripts/validate_signal.py --signal PIOTROSKI_F --start 2024-11-01 --end 2024-12-31 --peers quality

流程:
    1. preload_for_backtest + precompute_rolling_stats
    2. 在 [start, end] 每个月末计算一次截面
    3. 输出：覆盖率 / 分布 / Top-Bottom 10 / 与同类因子截面相关性
    4. 报告写 output/signal_validation/{name}_{version}_{start}_{end}.md

验收标准（人肉审核）:
    - 覆盖率 > 60%（fundamentals 类可能偏低，视数据而定）
    - 分布无全 0 / 全 NaN / 全常数
    - Top/Bottom ticker 看起来业务上合理
    - 与同类因子相关性 |ρ| < 0.95（避免完全冗余）
"""

import argparse
import logging
import os
import sys
from pathlib import Path

# 项目根目录
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

# 必须在 import django 前设置
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "core.settings")

import django  # noqa: E402

django.setup()

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from services.config import LOG_LEVEL  # noqa: E402
from stocks.services.us_cleaner import get_us_clean_universe  # noqa: E402
from stocks.services.factors.us_base import USFactorBase  # noqa: E402
from stocks.services.factors.us_registry import get_registered  # noqa: E402
import stocks.services.factors.signals  # noqa: F401, E402  触发自动注册

logger = logging.getLogger("validate_signal")
logger.setLevel(LOG_LEVEL)
logging.basicConfig(
    level=LOG_LEVEL, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)


def _month_end_dates(start: str, end: str) -> list[str]:
    """返回 [start, end] 范围内所有月末日期（YYYY-MM-DD 字符串）。"""
    start_ts = pd.Timestamp(start)
    end_ts = pd.Timestamp(end)
    month_ends = pd.date_range(start_ts, end_ts, freq="ME")
    return [d.strftime("%Y-%m-%d") for d in month_ends]


def _compute_cross_section(
    signal_cls, date: str
) -> pd.DataFrame:
    """用 signal_cls 在 date 计算一次截面，返回 DataFrame[ticker, factor_value]。"""
    universe = get_us_clean_universe(date)
    if universe.empty:
        logger.warning(f"{date} universe 为空，跳过")
        return pd.DataFrame(columns=["ticker", "factor_value"])

    instance = signal_cls()
    try:
        df = instance.compute(date, universe)
    except Exception as e:
        logger.error(f"{signal_cls.name}.compute({date}) 报错: {e}", exc_info=True)
        return pd.DataFrame(columns=["ticker", "factor_value"])

    if df is None or df.empty:
        logger.warning(f"{signal_cls.name}.compute({date}) 返回空")
        return pd.DataFrame(columns=["ticker", "factor_value"])

    if list(df.columns)[:2] != ["ticker", "factor_value"]:
        logger.error(
            f"{signal_cls.name} 返回列不规范: {list(df.columns)}，期望 [ticker, factor_value]"
        )
        return pd.DataFrame(columns=["ticker", "factor_value"])

    return df[["ticker", "factor_value"]].copy()


def _summarize_cross_section(df: pd.DataFrame, universe_size: int) -> dict:
    """汇总一个截面：count / 覆盖率 / 分布 / top / bottom。"""
    if df.empty:
        return {
            "n": 0,
            "coverage": 0.0,
            "mean": np.nan,
            "std": np.nan,
            "p_min": np.nan,
            "p25": np.nan,
            "p50": np.nan,
            "p75": np.nan,
            "p_max": np.nan,
            "n_zero": 0,
            "n_unique": 0,
            "top10": [],
            "bottom10": [],
        }
    vals = pd.to_numeric(df["factor_value"], errors="coerce").dropna()
    n = len(vals)
    sorted_df = df.assign(factor_value=pd.to_numeric(df["factor_value"], errors="coerce")).dropna(
        subset=["factor_value"]
    ).sort_values("factor_value", ascending=False)
    return {
        "n": n,
        "coverage": n / universe_size if universe_size else 0.0,
        "mean": float(vals.mean()) if n else np.nan,
        "std": float(vals.std()) if n > 1 else np.nan,
        "p_min": float(vals.min()) if n else np.nan,
        "p25": float(vals.quantile(0.25)) if n else np.nan,
        "p50": float(vals.quantile(0.50)) if n else np.nan,
        "p75": float(vals.quantile(0.75)) if n else np.nan,
        "p_max": float(vals.max()) if n else np.nan,
        "n_zero": int((vals == 0).sum()),
        "n_unique": int(vals.nunique()),
        "top10": list(sorted_df.head(10)[["ticker", "factor_value"]].itertuples(index=False, name=None)),
        "bottom10": list(sorted_df.tail(10)[["ticker", "factor_value"]].itertuples(index=False, name=None)),
    }


def _format_markdown(
    cls,
    start: str,
    end: str,
    per_date: list[tuple[str, dict]],
    peer_corr: dict[str, dict[str, float]],
) -> str:
    """生成 markdown 报告。"""
    md = []
    md.append(f"# 因子验证报告：{cls.name} ({cls.version})")
    md.append("")
    md.append(f"- **category**: {cls.category}")
    md.append(f"- **horizon**: {cls.horizon}")
    md.append(f"- **status**: {cls.status}")
    md.append(f"- **inherent_direction**: {cls.inherent_direction:+d}")
    md.append(f"- **expected_icir**: {cls.expected_icir}")
    md.append(f"- **ic_window_months**: {cls.ic_window_months}")
    md.append(f"- **data_deps**: {', '.join(cls.data_deps) if cls.data_deps else '—'}")
    md.append(f"- **验证区间**: {start} → {end}（{len(per_date)} 个月末截面）")
    md.append("")

    md.append("## 截面统计")
    md.append("")
    md.append(
        "| Date | N | Coverage | Mean | Std | Min | P25 | P50 | P75 | Max | #Zero | #Unique |"
    )
    md.append(
        "|------|---|---------:|-----:|----:|----:|----:|----:|----:|----:|------:|--------:|"
    )
    for date, s in per_date:
        md.append(
            f"| {date} | {s['n']} | {s['coverage']:.1%} "
            f"| {s['mean']:.4f} | {s['std']:.4f} "
            f"| {s['p_min']:.4f} | {s['p25']:.4f} | {s['p50']:.4f} "
            f"| {s['p75']:.4f} | {s['p_max']:.4f} "
            f"| {s['n_zero']} | {s['n_unique']} |"
        )
    md.append("")

    # Top / Bottom 基于最后一个截面
    if per_date:
        last_date, last_s = per_date[-1]
        md.append(f"## Top 10 / Bottom 10（截面 = {last_date}）")
        md.append("")
        md.append("**Top 10（因子值最大）**")
        md.append("")
        md.append("| Rank | Ticker | Value |")
        md.append("|------|--------|------:|")
        for i, (ticker, v) in enumerate(last_s["top10"], 1):
            md.append(f"| {i} | {ticker} | {v:.4f} |")
        md.append("")
        md.append("**Bottom 10（因子值最小）**")
        md.append("")
        md.append("| Rank | Ticker | Value |")
        md.append("|------|--------|------:|")
        for i, (ticker, v) in enumerate(last_s["bottom10"], 1):
            md.append(f"| {i} | {ticker} | {v:.4f} |")
        md.append("")

    # Peer correlation
    if peer_corr:
        md.append("## 与同类因子的截面相关性")
        md.append("")
        md.append(f"*（基于最后一个截面 {per_date[-1][0] if per_date else '—'}，Spearman 相关系数）*")
        md.append("")
        md.append("| Peer Factor | ρ (Spearman) | N 重叠 |")
        md.append("|-------------|-------------:|-------:|")
        for peer, info in peer_corr.items():
            md.append(f"| {peer} | {info['rho']:.3f} | {info['n']} |")
        md.append("")
        md.append("**判读**：|ρ| > 0.95 → 完全冗余，考虑剪掉；0.7-0.95 → 高度相关，保留但监控；<0.7 → 信号独立性好。")
        md.append("")

    md.append("## 结论")
    md.append("")
    md.append("（人肉审核 coverage / 分布 / Top-Bottom ticker 合理性 / peer 相关性后填写）")
    md.append("")
    return "\n".join(md)


def main():
    parser = argparse.ArgumentParser(description="单因子截面验证")
    parser.add_argument("--signal", required=True, help="因子 name（大写，例 PIOTROSKI_F）")
    parser.add_argument("--start", required=True, help="开始日期 YYYY-MM-DD")
    parser.add_argument("--end", required=True, help="结束日期 YYYY-MM-DD")
    parser.add_argument(
        "--peers",
        default="",
        help="Peer category（例 quality），会算与该类所有其他 active 因子的截面相关性",
    )
    args = parser.parse_args()

    registered = get_registered()
    if args.signal not in registered:
        logger.error(f"因子 {args.signal} 未注册。可用: {sorted(registered.keys())}")
        sys.exit(1)
    cls = registered[args.signal]
    logger.info(f"Validating {cls.name} ({cls.version}) on {args.start} → {args.end}")

    # 1. Preload（按整个验证区间预加载）
    logger.info("Preloading data (this may take ~30s for 2-month window)...")
    USFactorBase.clear_all_cache()
    USFactorBase.preload_for_backtest(args.start, args.end)
    USFactorBase.precompute_rolling_stats()

    # 2. 逐月末计算
    dates = _month_end_dates(args.start, args.end)
    if not dates:
        logger.error(f"区间 {args.start}→{args.end} 没有月末日期")
        sys.exit(1)

    per_date = []
    last_cross_section = None
    for date in dates:
        logger.info(f"Computing {cls.name} on {date}")
        univ = get_us_clean_universe(date)
        df = _compute_cross_section(cls, date)
        summary = _summarize_cross_section(df, len(univ))
        per_date.append((date, summary))
        logger.info(
            f"  {date}: n={summary['n']}, coverage={summary['coverage']:.1%}, "
            f"mean={summary['mean']:.4f}, std={summary['std']:.4f}"
        )
        last_cross_section = df

    # 3. Peer correlation（基于最后一个截面）
    peer_corr = {}
    if args.peers and last_cross_section is not None and not last_cross_section.empty:
        last_date = dates[-1]
        peers = [
            c for c in registered.values()
            if c.category == args.peers and c.name != cls.name and c.status in ("live", "staging")
        ]
        for peer_cls in peers:
            try:
                peer_df = _compute_cross_section(peer_cls, last_date)
            except Exception as e:
                logger.warning(f"peer {peer_cls.name} 计算失败: {e}")
                continue
            if peer_df.empty:
                continue
            merged = last_cross_section.merge(
                peer_df.rename(columns={"factor_value": "peer_value"}), on="ticker", how="inner"
            )
            merged = merged.dropna(subset=["factor_value", "peer_value"])
            if len(merged) < 10:
                peer_corr[peer_cls.name] = {"rho": float("nan"), "n": len(merged)}
                continue
            rho = merged["factor_value"].rank().corr(merged["peer_value"].rank())
            peer_corr[peer_cls.name] = {"rho": float(rho), "n": len(merged)}

    # 4. 写 markdown 报告
    out_dir = _PROJECT_ROOT / "output" / "signal_validation"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{cls.name}_{cls.version}_{args.start}_{args.end}.md"
    md = _format_markdown(cls, args.start, args.end, per_date, peer_corr)
    out_path.write_text(md, encoding="utf-8")
    logger.info(f"Report written: {out_path}")

    # 5. 终端总结
    print("\n" + "=" * 70)
    print(f"{cls.name} ({cls.version})  {args.start} → {args.end}")
    print("=" * 70)
    for date, s in per_date:
        print(
            f"  {date}  n={s['n']:5d}  cov={s['coverage']:5.1%}  "
            f"mean={s['mean']:+.4f}  std={s['std']:.4f}  "
            f"range=[{s['p_min']:+.3f}, {s['p_max']:+.3f}]  "
            f"#unique={s['n_unique']}"
        )
    if peer_corr:
        print("\n  Peer correlation (last cross-section):")
        for peer, info in peer_corr.items():
            print(f"    {peer:30s}  ρ={info['rho']:+.3f}  (n={info['n']})")
    print(f"\n  Report: {out_path}")


if __name__ == "__main__":
    main()
