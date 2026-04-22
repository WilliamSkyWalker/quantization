"""
Fork worker for parallel factor computation.

fork 模式：子进程通过 COW 继承主进程的 _static_cache（~6GB），
不需要重新加载数据，不需要 Django setup。

注意：主进程 fork 前必须：
1. 完成所有预加载（串行，无线程）
2. 关闭所有 DB 连接（connections.close_all()）
"""

import logging
import time as _time

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


def compute_factors_for_dates(args: tuple) -> list[tuple[str, dict]]:
    """fork worker：计算一批日期的因子值。

    通过 fork COW 继承主进程的 _static_cache，不复制内存。
    返回 [(date, {factor_name: DataFrame[ticker, factor_value]}), ...]
    """
    from django.db import connections
    connections.close_all()  # fork 后关闭从主进程继承的死连接

    worker_id, dates_chunk, preload_start, preload_end = args
    t0 = _time.time()

    from stocks.services.factors.us_base import USFactorBase
    from stocks.services.us_cleaner import get_us_clean_universe
    from stocks.services.factors.us_registry import get_active as get_active_signals
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

    # 构建因子实例（轻量，不加载数据）
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

    print(f"  Worker {worker_id}: ready {_time.time()-t0:.1f}s, {len(dates_chunk)} dates, "
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

    connections.close_all()
    return results
