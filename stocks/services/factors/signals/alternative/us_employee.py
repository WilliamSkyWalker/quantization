"""Employee Growth 因子

定义：
    EMPLOYEE_GROWTH = (employees_now - employees_yoy) / employees_yoy

    数据源：USEmployeeCount (10-K filings, annual)

经济直觉：
    - 员工增长反映公司扩张 / 萎缩
    - 方向不定：快速扩张可能是好信号（增长），也可能是坏信号（过度招聘）
    - Belo-Lin-Bazdresch (2014): 劳动力雇佣有负溢价（类似 asset growth）
    - 设 direction=0 让 IC 决定

因子方向：0
"""

import logging

import numpy as np
import pandas as pd

from services.config import LOG_LEVEL
from stocks.services.factors.us_registry import AlphaSignal, register

logger = logging.getLogger(__name__)
logger.setLevel(LOG_LEVEL)


@register
class EmployeeGrowth(AlphaSignal):
    """Employee Growth — 员工数 YoY 增速。"""

    name = "EMPLOYEE_GROWTH"
    version = "v1"
    category = "alternative"
    horizon = "quarter"
    expected_icir = 0.06
    status = "staging"
    inherent_direction = 0
    data_deps = ["us_employee_count"]
    ic_window_months = 24

    def compute(self, date: str, universe: pd.DataFrame) -> pd.DataFrame:
        tickers = universe["ticker"].tolist()
        if not tickers:
            logger.debug("EmployeeGrowth: 空 universe")
            return pd.DataFrame(columns=["ticker", "factor_value"])

        date_ts = pd.Timestamp(date)
        # 10-K 年报，回看 3 年保证有 2 个数据点
        start = (date_ts - pd.DateOffset(years=3)).date()

        df = pd.DataFrame(columns=["ticker", "date", "emp"])

        # 优先从预加载缓存获取
        bulk = self._static_cache.get("_bulk_employee")
        if bulk is not None and not bulk.empty:
            mask = (
                bulk["ticker"].isin(tickers)
                & (bulk["date"] >= pd.Timestamp(start))
                & (bulk["date"] <= date_ts)
                & bulk["employee_count"].notna()
                & (bulk["employee_count"] > 0)
            )
            filtered = bulk[mask]
            if not filtered.empty:
                df = filtered[["ticker", "date", "employee_count"]].copy()
                df.columns = ["ticker", "date", "emp"]
                logger.debug(f"EmployeeGrowth({date}): 缓存命中 {len(df)} 条")
            else:
                logger.debug(f"EmployeeGrowth({date}): 缓存中无匹配数据")
        else:
            # fallback ORM
            from stocks.models import USEmployeeCount

            qs = USEmployeeCount.objects.filter(
                ticker__in=tickers,
                period_of_report__gte=start,
                period_of_report__lte=date_ts.date(),
                employee_count__isnull=False,
                employee_count__gt=0,
            ).values_list("ticker", "period_of_report", "employee_count")
            df = pd.DataFrame(list(qs), columns=["ticker", "date", "emp"])
            logger.debug(f"EmployeeGrowth({date}): ORM fallback {len(df)} 条")
        if df.empty:
            logger.warning(f"EmployeeGrowth({date}): 无员工数据")
            return pd.DataFrame(columns=["ticker", "factor_value"])

        df["date"] = pd.to_datetime(df["date"])
        df["emp"] = pd.to_numeric(df["emp"], errors="coerce")
        df = df.sort_values(["ticker", "date"], ascending=[True, False])

        rows = []
        for ticker, grp in df.groupby("ticker", sort=False):
            if len(grp) < 2:
                continue
            now = grp.iloc[0]["emp"]
            prev = grp.iloc[1]["emp"]
            if pd.isna(prev) or prev <= 0:
                continue
            growth = (now - prev) / prev
            rows.append({"ticker": ticker, "factor_value": float(growth)})

        if not rows:
            logger.warning(f"EmployeeGrowth({date}): 无足够数据的 ticker")
            return pd.DataFrame(columns=["ticker", "factor_value"])

        out = pd.DataFrame(rows)
        n_out = int(out["factor_value"].notna().sum())
        logger.info(f"EmployeeGrowth({date}): {n_out} / {len(out)} 有值")
        return out
