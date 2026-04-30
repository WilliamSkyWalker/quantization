r"""Standardized Unexpected Earnings (SUE) + PEAD drift

    SUE = (EPS_actual − EPS_estimated) / std(surprise over past 8Q)

PEAD (Post-Earnings Announcement Drift, Ball & Brown 1968, Bernard & Thomas 1989):
    财报公布后，正惊喜组的股价会继续漂移 3-60 个交易日，
    这是最经典、最稳定的学术异象之一。

SUE 改良版（Foster-Olsen-Shevlin 1984）：除以历史 surprise 标准差而非 \|mean\|
避免均值近零爆炸，聚焦"新息相对历史噪音"的强度。

实现：
    1. 取每只股票最近 8 季的 EPS surprise（actual − estimated）
    2. 计算 SUE = 最新 surprise / std(过去 8Q surprise)
    3. 限制只在"财报公布后 60 天内"给出信号（事件驱动）
    4. 超过 60 天则信号置 NaN（避免跨季信号混淆）

方向：+1（高 SUE = 盈利超预期 → 未来继续漂移上涨）。
"""

import logging

import numpy as np
import pandas as pd

from services.config import LOG_LEVEL
from stocks.services.factors.us_registry import AlphaSignal, register

logger = logging.getLogger(__name__)
logger.setLevel(LOG_LEVEL)


@register
class SuePead(AlphaSignal):
    """SUE + PEAD drift：标准化盈利惊喜，60 天事件窗口。"""

    name = "SUE_PEAD"
    version = "v1"
    category = "momentum"
    horizon = "month"
    expected_icir = 0.15
    status = "staging"
    inherent_direction = +1
    data_deps = ["us_earnings_surprise"]
    ic_window_months = 12

    _LOOKBACK_QUARTERS = 8
    _PEAD_WINDOW_DAYS = 60  # 超过 60 天不给信号（事件衰减）

    def compute(self, date: str, universe: pd.DataFrame) -> pd.DataFrame:
        tickers = universe["ticker"].tolist()
        if not tickers:
            logger.debug("SuePead: 空 universe")
            return pd.DataFrame(columns=["ticker", "factor_value"])

        date_ts = pd.Timestamp(date)
        # 拉 8 季数据（~720 天足够）
        start_ts = date_ts - pd.Timedelta(days=800)

        # 优先走缓存
        cache = self._static_cache.get("_bulk_earnings_surprise")
        if cache is not None and not cache.empty:
            mask = (
                cache["ticker"].isin(tickers)
                & (cache["date"] >= start_ts)
                & (cache["date"] <= date_ts)
                & cache["eps_actual"].notna()
                & cache["eps_estimated"].notna()
            )
            df = cache[mask][["ticker", "date", "eps_actual", "eps_estimated"]].copy()
            df.columns = ["ticker", "date", "actual", "estimated"]
        else:
            # ORM fallback
            from stocks.models import USEarningsSurprise

            qs = USEarningsSurprise.objects.filter(
                ticker__in=tickers,
                date__gte=start_ts.date(),
                date__lte=date_ts.date(),
                eps_actual__isnull=False,
                eps_estimated__isnull=False,
            ).order_by("ticker", "-date").values_list(
                "ticker", "date", "eps_actual", "eps_estimated"
            )
            df = pd.DataFrame(list(qs), columns=["ticker", "date", "actual", "estimated"])
            if not df.empty:
                df["date"] = pd.to_datetime(df["date"])

        if df.empty:
            logger.warning(f"SuePead({date}): 无 earnings surprise 数据")
            return pd.DataFrame(columns=["ticker", "factor_value"])

        df["actual"] = pd.to_numeric(df["actual"], errors="coerce")
        df["estimated"] = pd.to_numeric(df["estimated"], errors="coerce")
        df["surprise"] = df["actual"] - df["estimated"]

        rows = []
        for ticker, grp in df.groupby("ticker", sort=False):
            grp = grp.sort_values("date", ascending=False).head(self._LOOKBACK_QUARTERS)
            if len(grp) < 4:  # 至少 4Q 才算 std
                continue

            latest = grp.iloc[0]
            days_since = (date_ts - latest["date"]).days
            if days_since < 0 or days_since > self._PEAD_WINDOW_DAYS:
                continue  # 事件窗口外，无 PEAD 信号

            history = grp.iloc[1:]  # 历史 surprise（不含最新）
            hist_vals = history["surprise"].dropna().values
            if len(hist_vals) < 3:
                continue
            std = np.std(hist_vals, ddof=1)
            if std < 1e-6 or np.isnan(std):
                continue

            sue = latest["surprise"] / std
            if np.isnan(sue):
                continue
            rows.append({"ticker": ticker, "factor_value": float(sue)})

        if not rows:
            logger.debug(f"SuePead({date}): 无事件窗口内的 ticker")
            return pd.DataFrame(columns=["ticker", "factor_value"])
        out = pd.DataFrame(rows)
        logger.info(f"SuePead({date}): {len(out)} 有值 (60 天 PEAD 窗口内)")
        return out
