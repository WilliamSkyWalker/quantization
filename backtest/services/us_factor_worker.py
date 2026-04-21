"""
Spawn worker for parallel factor computation.

独立模块，不导入任何 Django model（避免 AppRegistryNotReady）。
所有 import 在函数内部完成，django.setup() 后才安全。
"""


def compute_factors_for_dates(args: tuple) -> list[tuple[str, dict]]:
    """spawn worker：独立进程计算因子值（不做评分/选股）。

    主进程已确保所有 parquet 缓存存在。
    worker 只从 parquet 读取，不查 DB。
    """
    import os
    import sys
    import time as _time

    worker_id, dates_chunk, preload_start, preload_end = args

    # Django setup（必须在任何 model import 之前）
    project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    if project_root not in sys.path:
        sys.path.insert(0, project_root)
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "core.settings")

    import django
    django.setup()

    t0 = _time.time()

    from stocks.services.factors.us_base import USFactorBase
    from stocks.services.us_cleaner import get_us_clean_universe
    from stocks.services.factors.us_registry import get_active as get_active_signals, AlphaSignal
    import stocks.services.factors.signals  # noqa: F401

    from stocks.services.factors.us_value import EP, BP, DivYield
    from stocks.services.factors.us_growth import NetProfitYoY, RevenueYoY, NetProfitCAGR3Y
    from stocks.services.factors.us_momentum import Mom1M, Mom3M, Mom12M, Rev5D
    from stocks.services.factors.us_technical import Turn20D, Vol20D, Ivol, Size
    from stocks.services.factors.us_analyst import USAnalystRating, USAnalystCoverage
    from stocks.services.factors.us_accruals import BuybackYield
    from stocks.services.factors.us_earnings import EarningsSurprise, EpsRevision
    from stocks.services.factors.us_insider import InsiderNetBuy
    from stocks.services.factors.us_quiver import LobbyIntensity, GovContract

    # 加载缓存（主进程已确保 parquet 存在，这里只读 parquet，不查 DB）
    # 用主进程的 preload 日期范围，确保 _find_cache 命中
    USFactorBase.preload_for_backtest(preload_start, preload_end)
    if not USFactorBase.load_precomputed_cache():
        USFactorBase.precompute_rolling_stats()
    AlphaSignal.preload_alpha_cache(preload_start, preload_end)

    # 构建因子实例
    alpha_signals = [cls() for cls in get_active_signals().values()]
    legacy_factors = [
        EP(), BP(), DivYield(), BuybackYield(),
        NetProfitYoY(), RevenueYoY(), NetProfitCAGR3Y(),
        Mom1M(), Mom3M(), Mom12M(), Rev5D(),
        Turn20D(), Vol20D(), Ivol(), Size(),
        USAnalystRating(), USAnalystCoverage(),
        EarningsSurprise(), EpsRevision(), InsiderNetBuy(),
        LobbyIntensity(), GovContract(),
    ]
    all_factors = alpha_signals + legacy_factors

    print(f"  Worker {worker_id}: 加载完成 {_time.time()-t0:.1f}s, {len(dates_chunk)} dates, "
          f"{len(all_factors)} factors", flush=True)

    results = []
    for date in dates_chunk:
        try:
            universe = get_us_clean_universe(date)
            if universe.empty:
                results.append((date, {}))
                continue

            factor_scores = {}
            for factor in all_factors:
                try:
                    df = factor.compute(date, universe)
                    if not df.empty:
                        factor_scores[factor.name] = df
                except Exception:
                    pass

            results.append((date, factor_scores))
            print(f"  Worker {worker_id} | {date}: {len(factor_scores)} factors", flush=True)
        except Exception as e:
            print(f"  Worker {worker_id} | {date}: ERROR {e}", flush=True)
            results.append((date, {}))

    return results
