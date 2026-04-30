"""
策略层 Crowding 监控（金融基础 skill 第 10 节）

可用数据有限的情况下的 4 个 NAV-only 指标 + 1 个跨指数相关性指标：

1. 60D rolling kurtosis  → 阈值 10
2. 1D rolling autocorrelation (60D 窗口) → 阈值 +0.1
3. 252D rolling Sharpe 历史分位（5 年滚动）→ 阈值 95%
4. Strategy 对 NASDAQ vs S&P500 的相对相关性差 → tech crowding 代理
5. (复合 alert) 任 ≥3 项命中 → 拥挤度高

Limitations: 没有 13F 持仓 / SI / thematic ETF AUM 等数据，无法做完整的 7 指标体系。
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass, field
from datetime import date as Date
from typing import Iterable

import numpy as np
import pandas as pd
from scipy import stats

logger = logging.getLogger(__name__)

TRADING_DAYS_PER_YEAR = 252


@dataclass
class CrowdingThresholds:
    kurtosis_60d: float = 10.0
    autocorr_60d: float = 0.10
    sharpe_252d_percentile: float = 0.95  # 95% 历史分位
    nasdaq_spy_corr_diff: float = 0.20  # NASDAQ corr − SPY corr > 0.20 视为 tech-crowded


@dataclass
class CrowdingPoint:
    date: str
    kurtosis_60d: float
    autocorr_60d: float
    sharpe_252d: float
    sharpe_252d_pct_rank: float
    corr_nasdaq_60d: float
    corr_spy_60d: float
    nasdaq_minus_spy_corr: float
    n_alerts: int
    alert_kurtosis: bool
    alert_autocorr: bool
    alert_sharpe_extreme: bool
    alert_tech_skew: bool


def nav_to_returns_with_dates(nav_json: str | list) -> pd.Series:
    if isinstance(nav_json, str):
        nav_json = json.loads(nav_json)
    if not nav_json:
        return pd.Series(dtype=float)
    df = pd.DataFrame(nav_json)
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values("date").reset_index(drop=True)
    df["nav"] = df["nav"].astype(float)
    df["ret"] = df["nav"].pct_change()
    return pd.Series(df["ret"].values[1:], index=df["date"].values[1:], name="ret")


def rolling_kurtosis(returns: pd.Series, window: int = 60) -> pd.Series:
    return returns.rolling(window).apply(
        lambda x: stats.kurtosis(x, fisher=False, bias=False), raw=True
    )


def rolling_autocorr(returns: pd.Series, lag: int = 1, window: int = 60) -> pd.Series:
    def _ac(x: np.ndarray) -> float:
        if len(x) < lag + 2:
            return float("nan")
        a = x[lag:]
        b = x[:-lag]
        if a.std(ddof=1) == 0 or b.std(ddof=1) == 0:
            return float("nan")
        return float(np.corrcoef(a, b)[0, 1])

    return returns.rolling(window).apply(_ac, raw=True)


def rolling_sharpe(returns: pd.Series, window: int = 252) -> pd.Series:
    def _sr(x: np.ndarray) -> float:
        if x.std(ddof=1) == 0:
            return float("nan")
        return float(x.mean() / x.std(ddof=1) * math.sqrt(TRADING_DAYS_PER_YEAR))

    return returns.rolling(window).apply(_sr, raw=True)


def rolling_percentile_rank(series: pd.Series, history_window: int = 1260) -> pd.Series:
    """每个时点回看 history_window 天，计算当前值在历史中的分位数。"""

    def _rank(x: np.ndarray) -> float:
        if len(x) < 2 or np.isnan(x[-1]):
            return float("nan")
        valid = x[~np.isnan(x)]
        if len(valid) < 2:
            return float("nan")
        return float((valid <= x[-1]).sum() / len(valid))

    return series.rolling(history_window, min_periods=60).apply(_rank, raw=True)


def rolling_correlation(
    a: pd.Series, b: pd.Series, window: int = 60
) -> pd.Series:
    aligned = pd.concat([a.rename("a"), b.rename("b")], axis=1).dropna()
    return aligned["a"].rolling(window).corr(aligned["b"])


def fetch_index_returns(index_code: str, start: str, end: str) -> pd.Series:
    """从 us_index_daily 读 close → returns。"""
    from stocks.models import USIndexDaily

    qs = (
        USIndexDaily.objects.filter(
            index_code=index_code,
            trade_date__gte=start,
            trade_date__lte=end,
        )
        .order_by("trade_date")
        .values_list("trade_date", "close")
    )
    rows = list(qs)
    if not rows:
        return pd.Series(dtype=float)
    dates = [pd.Timestamp(r[0]) for r in rows]
    closes = np.array([float(r[1]) for r in rows])
    rets = np.diff(closes) / closes[:-1]
    return pd.Series(rets, index=dates[1:], name=index_code)


def compute_crowding_timeseries(
    nav_json: str | list,
    thresholds: CrowdingThresholds | None = None,
) -> pd.DataFrame:
    """主入口：返回每日 crowding 指标时序 + alert flags。"""
    if thresholds is None:
        thresholds = CrowdingThresholds()

    strat_ret = nav_to_returns_with_dates(nav_json)
    if strat_ret.empty:
        return pd.DataFrame()

    start = strat_ret.index.min().strftime("%Y-%m-%d")
    end = strat_ret.index.max().strftime("%Y-%m-%d")

    nasdaq_ret = fetch_index_returns("^IXIC", start, end)
    spy_ret = fetch_index_returns("^GSPC", start, end)

    df = pd.DataFrame(index=strat_ret.index)
    df["kurtosis_60d"] = rolling_kurtosis(strat_ret, 60)
    df["autocorr_60d"] = rolling_autocorr(strat_ret, lag=1, window=60)
    df["sharpe_252d"] = rolling_sharpe(strat_ret, 252)
    df["sharpe_252d_pct_rank"] = rolling_percentile_rank(df["sharpe_252d"], 1260)
    df["corr_nasdaq_60d"] = rolling_correlation(strat_ret, nasdaq_ret, 60)
    df["corr_spy_60d"] = rolling_correlation(strat_ret, spy_ret, 60)
    df["nasdaq_minus_spy_corr"] = df["corr_nasdaq_60d"] - df["corr_spy_60d"]

    df["alert_kurtosis"] = df["kurtosis_60d"] > thresholds.kurtosis_60d
    df["alert_autocorr"] = df["autocorr_60d"] > thresholds.autocorr_60d
    df["alert_sharpe_extreme"] = df["sharpe_252d_pct_rank"] > thresholds.sharpe_252d_percentile
    df["alert_tech_skew"] = df["nasdaq_minus_spy_corr"] > thresholds.nasdaq_spy_corr_diff
    df["n_alerts"] = (
        df["alert_kurtosis"].astype(int)
        + df["alert_autocorr"].astype(int)
        + df["alert_sharpe_extreme"].astype(int)
        + df["alert_tech_skew"].astype(int)
    )
    df["crowding_high"] = df["n_alerts"] >= 3
    return df


def summarize_crowding(df: pd.DataFrame) -> dict:
    """整理一份摘要：最近一天 / 历史 alerts 比例 / top 5 拥挤期。"""
    if df.empty:
        return {}
    valid = df.dropna(subset=["sharpe_252d_pct_rank"])
    if valid.empty:
        return {"empty": True}
    last = valid.iloc[-1]
    high_periods = valid[valid["crowding_high"]]
    alert_pct = (valid["n_alerts"] >= 3).mean()
    top_kurtosis = valid.nlargest(5, "kurtosis_60d")[["kurtosis_60d", "n_alerts"]]
    top_sharpe = valid.nlargest(5, "sharpe_252d_pct_rank")[
        ["sharpe_252d", "sharpe_252d_pct_rank", "n_alerts"]
    ]

    return {
        "n_obs_with_full_metrics": int(len(valid)),
        "last_date": last.name.strftime("%Y-%m-%d"),
        "last_kurtosis_60d": float(last["kurtosis_60d"]),
        "last_autocorr_60d": float(last["autocorr_60d"]),
        "last_sharpe_252d": float(last["sharpe_252d"]),
        "last_sharpe_252d_pct_rank": float(last["sharpe_252d_pct_rank"]),
        "last_nasdaq_minus_spy_corr": float(last["nasdaq_minus_spy_corr"]),
        "last_n_alerts": int(last["n_alerts"]),
        "last_crowding_high": bool(last["crowding_high"]),
        "high_crowding_pct_of_history": float(alert_pct),
        "n_high_crowding_days": int(len(high_periods)),
        "top_kurtosis_dates": top_kurtosis.reset_index().rename(
            columns={"index": "date"}
        ).to_dict("records"),
        "top_sharpe_dates": top_sharpe.reset_index().rename(
            columns={"index": "date"}
        ).to_dict("records"),
    }
