"""
美股股票池清洗

过滤条件：
    - is_actively_trading = 1
    - IPO 至少 N 天
    - 最低日均成交额
    - 最低市值
    - 排除当日无交易（停牌/退市）
"""

import logging

import pandas as pd
from django.db.models import Avg, F, Q

from stocks.models import USStockBasic, USIndustryClass, USCompanyProfile, USDailyPrice
from services.config import (
    LOG_LEVEL,
    US_MIN_DAILY_VOLUME,
    US_MIN_MARKET_CAP,
    US_MIN_LISTING_DAYS,
)

logger = logging.getLogger(__name__)
logger.setLevel(LOG_LEVEL)


def get_us_clean_universe(date: str, **kwargs) -> pd.DataFrame:
    """
    获取指定日期的美股可交易股票池。

    Returns:
        DataFrame[ticker, name, sector, industry, market_cap]
    """
    date_dt = pd.to_datetime(date)
    cutoff_date = (date_dt - pd.Timedelta(days=US_MIN_LISTING_DAYS)).strftime("%Y-%m-%d")

    # 1. 行业数据（ticker → sector/industry）
    industry_map = dict(
        USIndustryClass.objects.values_list("ticker", "sector")
    )
    industry_detail = dict(
        USIndustryClass.objects.values_list("ticker", "industry")
    )

    # 2. IPO 日期（ticker → ipo_date）
    ipo_map = dict(
        USCompanyProfile.objects.values_list("ticker", "ipo_date")
    )

    # 3. 基本筛选：活跃 OR (非活跃但有行业数据)
    active_tickers = set(
        USStockBasic.objects.filter(is_actively_trading=1)
        .values_list("ticker", flat=True)
    )
    inactive_with_sector = set(
        USStockBasic.objects.filter(is_actively_trading=0)
        .values_list("ticker", flat=True)
    ) & set(industry_map.keys())

    all_tickers = active_tickers | inactive_with_sector

    # 4. IPO 日期过滤
    filtered = []
    name_map = dict(
        USStockBasic.objects.filter(ticker__in=all_tickers)
        .values_list("ticker", "company_name")
    )
    for ticker in all_tickers:
        ipo = ipo_map.get(ticker)
        if ipo is not None and str(ipo) > cutoff_date:
            continue
        filtered.append({
            "ticker": ticker,
            "name": name_map.get(ticker, ""),
            "sector": industry_map.get(ticker),
            "industry": industry_detail.get(ticker),
        })

    df = pd.DataFrame(filtered)
    if df.empty:
        logger.warning(f"US 股票池为空 (date={date})")
        return df

    initial = len(df)

    # 5. 历史市值过滤（优先用预加载缓存）
    from stocks.services.factors.us_base import USFactorBase
    bulk_mktcap = USFactorBase._static_cache.get("_bulk_mktcap")
    if bulk_mktcap is not None and not bulk_mktcap.empty:
        valid = bulk_mktcap[bulk_mktcap["date"] <= date_dt]
        if not valid.empty:
            hist_mktcap = (
                valid.sort_values("date")
                .drop_duplicates(subset=["ticker"], keep="last")
                [["ticker", "market_cap"]]
            )
            df = df.merge(hist_mktcap, on="ticker", how="left")
        else:
            df["market_cap"] = float("nan")
    else:
        # 非回测模式：静态快照
        static = pd.DataFrame(
            USStockBasic.objects.filter(market_cap__isnull=False)
            .values("ticker", "market_cap")
        )
        if not static.empty:
            static["market_cap"] = pd.to_numeric(static["market_cap"], errors="coerce")
            df = df.merge(static[["ticker", "market_cap"]], on="ticker", how="left")
        else:
            df["market_cap"] = float("nan")

    df["market_cap"] = pd.to_numeric(df["market_cap"], errors="coerce")
    df = df[(df["market_cap"] >= US_MIN_MARKET_CAP) | df["market_cap"].isna()]

    # 6. 流动性过滤
    if not df.empty:
        bulk_daily = USFactorBase._static_cache.get("_bulk_daily")

        if bulk_daily is not None and not bulk_daily.empty:
            # 内存计算（快速路径）
            start_dt = date_dt - pd.Timedelta(days=40)
            mask = (bulk_daily["trade_date"] >= start_dt) & (bulk_daily["trade_date"] <= date_dt)
            recent = bulk_daily[mask]
            if not recent.empty:
                close = pd.to_numeric(recent["close"], errors="coerce")
                volume = pd.to_numeric(recent["volume"], errors="coerce")
                dvol = recent.assign(dollar_vol=close * volume)
                vol_df = dvol.groupby("ticker")["dollar_vol"].mean().reset_index()
                vol_df.columns = ["ticker", "avg_dollar_vol"]
                liquid = vol_df[vol_df["avg_dollar_vol"] >= US_MIN_DAILY_VOLUME]["ticker"]
                df = df[df["ticker"].isin(liquid)]

                day_mask = bulk_daily["trade_date"] == date_dt
                traded = bulk_daily[day_mask & (pd.to_numeric(bulk_daily["volume"], errors="coerce") > 0)]["ticker"]
                df = df[df["ticker"].isin(traded)]
            else:
                logger.debug(f"get_us_clean_universe: 内存缓存中 {date} 附近无数据，跳过流动性过滤")
        else:
            # Django ORM 回退
            start_date = (date_dt - pd.Timedelta(days=40)).strftime("%Y-%m-%d")
            vol_qs = (
                USDailyPrice.objects.filter(
                    trade_date__gte=start_date,
                    trade_date__lte=date,
                )
                .values("ticker")
                .annotate(avg_dollar_vol=Avg(F("close") * F("volume")))
            )
            liquid = {
                row["ticker"]
                for row in vol_qs
                if row["avg_dollar_vol"] and row["avg_dollar_vol"] >= US_MIN_DAILY_VOLUME
            }
            df = df[df["ticker"].isin(liquid)]

            # 排除当日无交易
            traded = set(
                USDailyPrice.objects.filter(trade_date=date, volume__gt=0)
                .values_list("ticker", flat=True)
                .distinct()
            )
            df = df[df["ticker"].isin(traded)]

    logger.info(f"US 股票池: {initial} → {len(df)} (date={date})")
    return df[["ticker", "name", "sector", "industry", "market_cap"]].reset_index(drop=True)
