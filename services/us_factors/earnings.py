"""美股盈利因子: EARNINGS_SURPRISE, EPS_REVISION"""

import logging

import numpy as np
import pandas as pd

from services.config import LOG_LEVEL
from services.us_factors.base import USFactorBase

logger = logging.getLogger(__name__)
logger.setLevel(LOG_LEVEL)


class EarningsSurprise(USFactorBase):
    """
    Earnings Surprise: 最近一次盈利惊喜百分比。

    正向因子：surprise_pct > 0 → 实际 EPS 超预期 → 看多。
    Post-earnings announcement drift (PEAD) 是最强截面异象之一。

    数据来源: FMP API → us_earnings_surprise 表
    """
    name = "EARNINGS_SURPRISE"
    description = "盈利惊喜百分比 (actual - estimated) / |estimated|"

    # 回看窗口：只取最近 120 天内公布的最近一次 earnings
    _LOOKBACK_DAYS = 120

    def compute(self, date: str, universe: pd.DataFrame) -> pd.DataFrame:
        tickers = universe["ticker"].tolist()
        date_ts = pd.to_datetime(date)
        start_ts = date_ts - pd.Timedelta(days=self._LOOKBACK_DAYS)

        # 从预加载数据获取
        bulk = self._static_cache.get("_bulk_earnings_surprise")
        if bulk is not None and not bulk.empty:
            mask = (bulk["date"] >= start_ts) & (bulk["date"] <= date_ts)
            df = bulk[mask].copy()
        else:
            df = self.db.query(
                "SELECT ticker, date, surprise_pct FROM us_earnings_surprise "
                "WHERE date >= :start AND date <= :end",
                params={"start": start_ts.strftime("%Y-%m-%d"), "end": date},
            )

        if df.empty:
            return pd.DataFrame(columns=["ticker", "factor_value"])

        if tickers:
            df = df[df["ticker"].isin(tickers)]

        df = df.dropna(subset=["surprise_pct"])
        if df.empty:
            return pd.DataFrame(columns=["ticker", "factor_value"])

        # 取每只股票最近一次的 surprise_pct
        df = df.sort_values("date", ascending=False).drop_duplicates(
            subset=["ticker"], keep="first"
        )

        result = df[["ticker", "surprise_pct"]].copy()
        result.columns = ["ticker", "factor_value"]
        return result


class EpsRevision(USFactorBase):
    """
    EPS Revision: 当前 EPS 共识预期 vs 90 天前的预期变化方向。

    正向因子：分析师上调 EPS → 看多。
    需要 us_eps_estimate 表中有同一 ticker 不同时间点的快照。

    FMP analyst-estimates 端点返回的是按 fiscal period 归集的共识快照，
    取下一财报期（next quarter）的 eps_avg 差值衡量修正方向。
    """
    name = "EPS_REVISION"
    description = "EPS 共识预期修正 (当前 vs 90天前)"

    _REVISION_WINDOW_DAYS = 90

    def compute(self, date: str, universe: pd.DataFrame) -> pd.DataFrame:
        tickers = universe["ticker"].tolist()
        date_ts = pd.to_datetime(date)

        # 从预加载数据获取
        bulk = self._static_cache.get("_bulk_eps_estimate")
        if bulk is not None and not bulk.empty:
            df = bulk.copy()
        else:
            df = self.db.query(
                "SELECT ticker, date, eps_avg, num_analysts FROM us_eps_estimate",
            )

        if df.empty:
            return pd.DataFrame(columns=["ticker", "factor_value"])

        df["date"] = pd.to_datetime(df["date"])
        if tickers:
            df = df[df["ticker"].isin(tickers)]

        if df.empty:
            return pd.DataFrame(columns=["ticker", "factor_value"])

        # 对每只股票，取距离当前日期最近的两个未来财报期的预期
        # 作为 "当前共识"，与更远财报期对比衡量修正
        # FMP 返回的 date 字段是 fiscal period end date（非快照日期），
        # 所以我们取 date > 当前日期 的最近一期（下一季财报预期）的 eps_avg
        future_mask = df["date"] > date_ts
        past_mask = (df["date"] <= date_ts) & (df["date"] >= date_ts - pd.Timedelta(days=self._REVISION_WINDOW_DAYS))

        # 下一季预期（forward EPS consensus）
        df_future = df[future_mask].sort_values("date").drop_duplicates(
            subset=["ticker"], keep="first"
        )
        # 最近已公布季度的预期（回看窗口内）
        df_past = df[past_mask].sort_values("date", ascending=False).drop_duplicates(
            subset=["ticker"], keep="first"
        )

        if df_future.empty:
            return pd.DataFrame(columns=["ticker", "factor_value"])

        # Forward EPS 作为 proxy：EPS_avg 越高 → 分析师预期越乐观
        # 简化处理：直接用 forward EPS / past EPS - 1 作为 revision proxy
        merged = df_future[["ticker", "eps_avg"]].merge(
            df_past[["ticker", "eps_avg"]],
            on="ticker", suffixes=("_fwd", "_past"),
        )

        if merged.empty or "eps_avg_past" not in merged.columns:
            # fallback: 直接用 forward EPS avg 的 cross-section rank
            result = df_future[["ticker", "eps_avg"]].copy()
            result.columns = ["ticker", "factor_value"]
            return result

        # Revision = (forward - past) / |past|
        merged["factor_value"] = np.where(
            merged["eps_avg_past"].abs() > 0.01,
            (merged["eps_avg_fwd"] - merged["eps_avg_past"]) / merged["eps_avg_past"].abs(),
            0.0,
        )

        return merged[["ticker", "factor_value"]]
