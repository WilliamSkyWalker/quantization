"""Institutional Ownership Delta 因子

定义：
    INST_DELTA = number_of_13f_shares_change / number_of_13f_shares（最新季度）

    数据源：USInstitutionalHolder (FMP 13F summary)
    - ownership_percent_change 仅 1.8% 非 null（FMP API 不返回），弃用
    - number_of_13f_shares_change: 季度 13F 持股数变化（93.8% 覆盖率）
    - number_of_13f_shares: 总 13F 持股数（100% 覆盖率）

经济直觉：
    - 机构增持 → 聪明资金看好 → 正向信号
    - Gompers & Metrick (2001): 机构持仓变化有预测力
    - 正向因子

因子方向：+1
"""

import logging

import numpy as np
import pandas as pd

from services.config import LOG_LEVEL
from stocks.services.factors.us_registry import AlphaSignal, register

logger = logging.getLogger(__name__)
logger.setLevel(LOG_LEVEL)


@register
class InstOwnershipDelta(AlphaSignal):
    """Inst Ownership Delta — 机构 13F 持股数季度变化率。"""

    name = "INST_OWNERSHIP_DELTA"
    version = "v1"
    category = "ownership"
    horizon = "quarter"
    expected_icir = 0.08
    status = "staging"
    inherent_direction = +1
    data_deps = ["us_institutional_holder"]
    ic_window_months = 18

    def compute(self, date: str, universe: pd.DataFrame) -> pd.DataFrame:
        tickers = universe["ticker"].tolist()
        if not tickers:
            logger.debug("InstOwnershipDelta: 空 universe")
            return pd.DataFrame(columns=["ticker", "factor_value"])

        date_ts = pd.Timestamp(date)
        # 13F 季报有 ~45 天延迟，回看 200 天保证覆盖（需多取一期算 change）
        start_ts = date_ts - pd.Timedelta(days=400)

        # ---- 优先从预加载缓存获取 ----
        # 缓存列: ticker, date, investors_holding, number_of_13f_shares
        bulk = self._static_cache.get("_bulk_institutional")
        if bulk is not None and not bulk.empty:
            mask = (
                bulk["ticker"].isin(tickers)
                & (bulk["date"] >= start_ts)
                & (bulk["date"] <= date_ts)
            )
            df = bulk.loc[mask, ["ticker", "date", "number_of_13f_shares"]].copy()
            if df.empty:
                logger.debug(f"InstOwnershipDelta({date}): 缓存中无 13F 数据")
                return pd.DataFrame(columns=["ticker", "factor_value"])
            df["number_of_13f_shares"] = pd.to_numeric(df["number_of_13f_shares"], errors="coerce")
            df = df.dropna(subset=["number_of_13f_shares"])
            df = df.sort_values(["ticker", "date"], ascending=[True, False])
            df = df.drop_duplicates(subset=["ticker", "date"], keep="first")

            # 每只 ticker 取最近两期，计算 change
            df["rank"] = df.groupby("ticker").cumcount()
            latest = df[df["rank"] == 0][["ticker", "number_of_13f_shares"]].copy()
            latest.columns = ["ticker", "shares"]
            prev = df[df["rank"] == 1][["ticker", "number_of_13f_shares"]].copy()
            prev.columns = ["ticker", "shares_prev"]
            merged = latest.merge(prev, on="ticker", how="inner")
            if merged.empty:
                logger.warning(f"InstOwnershipDelta({date}): 缓存中不足两期数据")
                return pd.DataFrame(columns=["ticker", "factor_value"])
            denom = merged["shares_prev"].replace(0, np.nan)
            merged["factor_value"] = (merged["shares"] - merged["shares_prev"]) / denom
            out = merged[["ticker", "factor_value"]].copy()
            n_out = int(out["factor_value"].notna().sum())
            logger.info(f"InstOwnershipDelta({date}): {n_out} / {len(out)} 有值 (cache)")
            return out[["ticker", "factor_value"]]

        # ---- fallback ORM ----
        logger.debug(f"InstOwnershipDelta({date}): 缓存为空，fallback ORM")
        from stocks.models import USInstitutionalHolder

        start = (date_ts - pd.Timedelta(days=200)).date()
        qs = USInstitutionalHolder.objects.filter(
            ticker__in=tickers,
            date__gte=start,
            date__lte=date_ts.date(),
            number_of_13f_shares_change__isnull=False,
            number_of_13f_shares__isnull=False,
        ).values_list("ticker", "date", "number_of_13f_shares", "number_of_13f_shares_change")

        df = pd.DataFrame(list(qs), columns=["ticker", "date", "shares", "shares_change"])
        if df.empty:
            logger.debug(f"InstOwnershipDelta({date}): ORM 无 13F 数据")
            return pd.DataFrame(columns=["ticker", "factor_value"])

        df["date"] = pd.to_datetime(df["date"])
        df["shares"] = pd.to_numeric(df["shares"], errors="coerce")
        df["shares_change"] = pd.to_numeric(df["shares_change"], errors="coerce")

        # 每只股票取最近一条
        df = df.sort_values(["ticker", "date"], ascending=[True, False])
        df = df.drop_duplicates(subset=["ticker"], keep="first")

        # 变化率 = shares_change / shares
        denom = df["shares"].replace(0, np.nan)
        delta = df["shares_change"] / denom

        out = pd.DataFrame({
            "ticker": df["ticker"].values,
            "factor_value": delta.values,
        })
        n_out = int(out["factor_value"].notna().sum())
        logger.info(f"InstOwnershipDelta({date}): {n_out} / {len(out)} 有值")
        return out[["ticker", "factor_value"]]
