"""Price Target Ratio 因子（混合方案，无前瞻偏差）

定义：
    PTR = consensus_target / current_price

数据来源（按日期分段）：
    ≥ 2021: FMP v4/price-target 的 per-analyst 历史目标价
            → 取截面日前 12 个月内的 analyst targets → median
    < 2021: us_eps_estimate 的 consensus EPS × 行业 forward PE
            → Forward EP 作为 implied target 的代理

经济直觉：
    - PTR > 1 → 分析师/市场认为当前价格被低估
    - PTR < 1 → 认为当前价格被高估

因子方向：+1
"""

import logging

import numpy as np
import pandas as pd

from services.config import LOG_LEVEL
from stocks.services.factors.us_registry import AlphaSignal, register

logger = logging.getLogger(__name__)
logger.setLevel(LOG_LEVEL)

# per-analyst 目标价数据最早覆盖日期
_PT_DETAIL_CUTOVER = pd.Timestamp("2021-01-01")
_PT_LOOKBACK_DAYS = 365  # 取最近 12 个月内的 analyst targets


@register
class PriceTargetRatio(AlphaSignal):
    """Price Target Ratio — 混合方案（无前瞻偏差）。"""

    name = "PRICE_TARGET_RATIO"
    version = "v2"
    category = "analyst"
    horizon = "month"
    expected_icir = 0.30
    status = "staging"
    inherent_direction = +1
    data_deps = ["us_price_target_detail", "us_eps_estimate", "us_daily_price"]
    ic_window_months = 12

    def compute(self, date: str, universe: pd.DataFrame) -> pd.DataFrame:
        tickers = universe["ticker"].tolist()
        if not tickers:
            return pd.DataFrame(columns=["ticker", "factor_value"])

        date_ts = pd.Timestamp(date)

        if date_ts >= _PT_DETAIL_CUTOVER:
            result = self._compute_from_pt_detail(date, date_ts, tickers)
            if result is not None and not result.empty:
                return result
            logger.info(f"PriceTargetRatio({date}): PT detail 无数据，降级到 Forward EP")

        return self._compute_forward_ep(date, date_ts, tickers)

    # ------------------------------------------------------------------
    # 方案 B: 真实 per-analyst price target（≥ 2021）
    # ------------------------------------------------------------------

    def _compute_from_pt_detail(
        self, date: str, date_ts: pd.Timestamp, tickers: list[str],
    ) -> pd.DataFrame | None:
        """从 us_price_target_detail 取截面日前 12M 的 analyst targets → median。"""
        from stocks.models import USPriceTargetDetail

        start_ts = date_ts - pd.Timedelta(days=_PT_LOOKBACK_DAYS)

        qs = USPriceTargetDetail.objects.filter(
            ticker__in=tickers,
            published_date__gte=start_ts,
            published_date__lte=date_ts,
            price_target__gt=0,
        ).values_list("ticker", "price_target")

        df = pd.DataFrame(list(qs), columns=["ticker", "price_target"])
        if df.empty or len(df) < 50:
            return None

        df["price_target"] = pd.to_numeric(df["price_target"], errors="coerce")

        # 每个 ticker 取最近 12M 的 median target
        consensus = df.groupby("ticker")["price_target"].median().reset_index()
        consensus.columns = ["ticker", "target"]

        # 取当前价格
        price = self._get_price(date, consensus["ticker"].tolist())
        if price.empty:
            return None

        merged = consensus.merge(price, on="ticker", how="inner")
        p = merged["close"].replace(0, np.nan)
        merged["factor_value"] = merged["target"] / p

        out = merged[["ticker", "factor_value"]].dropna()
        logger.info(f"PriceTargetRatio({date}): {len(out)} 有值 (PT detail, {len(df)} records)")
        return out

    # ------------------------------------------------------------------
    # 方案 A: Forward EP 代理（< 2021 或 PT detail 无数据时降级）
    # ------------------------------------------------------------------

    def _compute_forward_ep(
        self, date: str, date_ts: pd.Timestamp, tickers: list[str],
    ) -> pd.DataFrame:
        """Forward EP = estimated_eps_avg / current_price（point-in-time）。"""
        # 取截面日可见的最新 EPS 估计
        bulk_ee = self._static_cache.get("_bulk_eps_estimate")
        if bulk_ee is not None and not bulk_ee.empty:
            mask = (bulk_ee["date"] <= date_ts) & bulk_ee["ticker"].isin(tickers)
            df = bulk_ee[mask].copy()
        else:
            from stocks.models import USEpsEstimate
            start = (date_ts - pd.Timedelta(days=400)).strftime("%Y-%m-%d")
            qs = USEpsEstimate.objects.filter(
                ticker__in=tickers,
                date__gte=start,
                date__lte=date,
            ).values_list("ticker", "date", "estimated_eps_avg")
            df = pd.DataFrame(list(qs), columns=["ticker", "date", "estimated_eps_avg"])

        if df.empty:
            logger.warning(f"PriceTargetRatio({date}): 无 EPS 估计数据")
            return pd.DataFrame(columns=["ticker", "factor_value"])

        df["date"] = pd.to_datetime(df["date"])
        df["estimated_eps_avg"] = pd.to_numeric(df["estimated_eps_avg"], errors="coerce")

        # 每个 ticker 取最新的 EPS 估计
        latest = (
            df.sort_values("date", ascending=False)
            .drop_duplicates(subset=["ticker"], keep="first")
            [["ticker", "estimated_eps_avg"]]
        )
        latest = latest[latest["estimated_eps_avg"].notna() & (latest["estimated_eps_avg"] > 0)]

        if latest.empty:
            logger.warning(f"PriceTargetRatio({date}): 无有效 EPS 估计")
            return pd.DataFrame(columns=["ticker", "factor_value"])

        # Forward EP = EPS / Price（和 PTR 同方向：越高越被低估）
        price = self._get_price(date, latest["ticker"].tolist())
        if price.empty:
            return pd.DataFrame(columns=["ticker", "factor_value"])

        merged = latest.merge(price, on="ticker", how="inner")
        p = merged["close"].replace(0, np.nan)
        merged["factor_value"] = merged["estimated_eps_avg"] / p

        out = merged[["ticker", "factor_value"]].dropna()
        logger.info(f"PriceTargetRatio({date}): {len(out)} 有值 (Forward EP)")
        return out

    # ------------------------------------------------------------------
    # 共用：取截面日价格
    # ------------------------------------------------------------------

    def _get_price(self, date: str, tickers: list[str]) -> pd.DataFrame:
        """取截面日最近收盘价（优先走缓存）。"""
        date_ts = pd.Timestamp(date)

        bulk_daily = self._static_cache.get("_bulk_daily")
        if bulk_daily is not None and not bulk_daily.empty:
            mask = (
                (bulk_daily["trade_date"] <= date_ts)
                & (bulk_daily["trade_date"] >= date_ts - pd.Timedelta(days=10))
                & bulk_daily["ticker"].isin(tickers)
            )
            df = bulk_daily[mask].copy()
            if not df.empty:
                df = df.sort_values(["ticker", "trade_date"], ascending=[True, False])
                df = df.drop_duplicates(subset=["ticker"], keep="first")
                df["close"] = pd.to_numeric(df["close"], errors="coerce")
                return df[["ticker", "close"]].reset_index(drop=True)

        # ORM fallback
        from stocks.models import USDailyPrice
        start = (date_ts - pd.Timedelta(days=10)).date()
        qs = USDailyPrice.objects.filter(
            ticker__in=tickers,
            trade_date__gte=start,
            trade_date__lte=date_ts.date(),
        ).values_list("ticker", "trade_date", "close")
        df = pd.DataFrame(list(qs), columns=["ticker", "trade_date", "close"])
        if df.empty:
            return pd.DataFrame(columns=["ticker", "close"])
        df = df.sort_values(["ticker", "trade_date"], ascending=[True, False])
        df = df.drop_duplicates(subset=["ticker"], keep="first")
        df["close"] = pd.to_numeric(df["close"], errors="coerce")
        return df[["ticker", "close"]].reset_index(drop=True)
