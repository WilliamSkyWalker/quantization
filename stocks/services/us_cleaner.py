"""
美股股票池清洗（polars 版本）

过滤条件：
    - is_actively_trading = 1
    - IPO 至少 N 天
    - 最低日均成交额
    - 最低市值
    - 排除当日无交易（停牌/退市）
"""

import logging

import polars as pl
import pandas as pd

from stocks.models import USStockBasic, USIndustryClass, USCompanyProfile, USDailyPrice
from services.config import (
    LOG_LEVEL,
    US_MIN_DAILY_VOLUME,
    US_MIN_MARKET_CAP,
    US_MIN_LISTING_DAYS,
)

logger = logging.getLogger(__name__)
logger.setLevel(LOG_LEVEL)

# 模块级缓存（静态数据，整个进程生命周期内不变）
_STATIC_CACHE: dict = {}


def _get_static_data() -> tuple[dict, dict, dict, set, set, dict]:
    """获取静态数据（行业/IPO/股票列表），首次查 DB 后缓存。"""
    if "_loaded" in _STATIC_CACHE:
        return (
            _STATIC_CACHE["industry_map"],
            _STATIC_CACHE["industry_detail"],
            _STATIC_CACHE["ipo_map"],
            _STATIC_CACHE["active_tickers"],
            _STATIC_CACHE["inactive_with_sector"],
            _STATIC_CACHE["name_map"],
        )

    industry_map = dict(USIndustryClass.objects.values_list("ticker", "sector"))
    industry_detail = dict(USIndustryClass.objects.values_list("ticker", "industry"))
    ipo_map = dict(USCompanyProfile.objects.values_list("ticker", "ipo_date"))

    active_tickers = set(
        USStockBasic.objects.filter(is_actively_trading=1)
        .values_list("ticker", flat=True)
    )
    inactive_with_sector = set(
        USStockBasic.objects.filter(is_actively_trading=0)
        .values_list("ticker", flat=True)
    ) & set(industry_map.keys())

    all_tickers = active_tickers | inactive_with_sector
    name_map = dict(
        USStockBasic.objects.filter(ticker__in=all_tickers)
        .values_list("ticker", "company_name")
    )

    _STATIC_CACHE["industry_map"] = industry_map
    _STATIC_CACHE["industry_detail"] = industry_detail
    _STATIC_CACHE["ipo_map"] = ipo_map
    _STATIC_CACHE["active_tickers"] = active_tickers
    _STATIC_CACHE["inactive_with_sector"] = inactive_with_sector
    _STATIC_CACHE["name_map"] = name_map
    _STATIC_CACHE["_loaded"] = True

    logger.info(
        f"US cleaner 静态数据已缓存: {len(industry_map)} 行业, "
        f"{len(ipo_map)} IPO, {len(all_tickers)} tickers"
    )
    return industry_map, industry_detail, ipo_map, active_tickers, inactive_with_sector, name_map


def get_us_clean_universe(date: str, **kwargs) -> pl.DataFrame:
    """
    获取指定日期的美股可交易股票池。

    Returns:
        pl.DataFrame[ticker, name, sector, industry, market_cap]
    """
    date_dt = pd.to_datetime(date)
    date_val = date_dt.date()
    cutoff_date = (date_dt - pd.Timedelta(days=US_MIN_LISTING_DAYS)).strftime("%Y-%m-%d")

    # 1-3. 静态数据（首次查 DB，后续走缓存）
    industry_map, industry_detail, ipo_map, active_tickers, inactive_with_sector, name_map = _get_static_data()

    all_tickers = active_tickers | inactive_with_sector

    # 4. IPO 日期过滤
    filtered = []
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

    if not filtered:
        logger.warning(f"US 股票池为空 (date={date})")
        return pl.DataFrame(schema={"ticker": pl.Utf8, "name": pl.Utf8,
                                     "sector": pl.Utf8, "industry": pl.Utf8,
                                     "market_cap": pl.Float64})

    df = pl.DataFrame(filtered)
    initial = df.height

    # 5. 历史市值过滤（优先用预加载缓存）
    from stocks.services.factors.us_base import USFactorBase
    bulk_mktcap = USFactorBase._static_cache.get("_bulk_mktcap")
    if bulk_mktcap is not None and isinstance(bulk_mktcap, pl.DataFrame) and not bulk_mktcap.is_empty():
        valid = bulk_mktcap.filter(pl.col("date").cast(pl.Date) <= date_val)
        if not valid.is_empty():
            hist_mktcap = (
                valid.sort("date")
                .unique(subset=["ticker"], keep="last")
                .select(["ticker", "market_cap"])
            )
            df = df.join(hist_mktcap, on="ticker", how="left")
        else:
            df = df.with_columns(pl.lit(None).cast(pl.Float64).alias("market_cap"))
    else:
        # 非回测模式：静态快照
        rows = list(
            USStockBasic.objects.filter(market_cap__isnull=False)
            .values_list("ticker", "market_cap")
        )
        if rows:
            static = pl.DataFrame(rows, schema=["ticker", "market_cap"], orient="row")
            static = static.with_columns(pl.col("market_cap").cast(pl.Float64, strict=False))
            df = df.join(static, on="ticker", how="left")
        else:
            df = df.with_columns(pl.lit(None).cast(pl.Float64).alias("market_cap"))

    if "market_cap" in df.columns:
        df = df.with_columns(pl.col("market_cap").cast(pl.Float64, strict=False))
        df = df.filter(
            (pl.col("market_cap") >= US_MIN_MARKET_CAP) | pl.col("market_cap").is_null()
        )

    # 6. 流动性过滤
    if df.height > 0:
        bulk_daily = USFactorBase._static_cache.get("_bulk_daily")

        if bulk_daily is not None and isinstance(bulk_daily, pl.DataFrame) and not bulk_daily.is_empty():
            start_val = (date_dt - pd.Timedelta(days=40)).date()
            recent = bulk_daily.filter(
                (pl.col("trade_date").cast(pl.Date) >= start_val)
                & (pl.col("trade_date").cast(pl.Date) <= date_val)
            )
            if not recent.is_empty():
                # 日均成交额
                dvol = recent.with_columns(
                    (pl.col("close").cast(pl.Float64, strict=False)
                     * pl.col("volume").cast(pl.Float64, strict=False)).alias("dollar_vol")
                )
                vol_df = dvol.group_by("ticker").agg(
                    pl.col("dollar_vol").mean().alias("avg_dollar_vol")
                )
                liquid = vol_df.filter(pl.col("avg_dollar_vol") >= US_MIN_DAILY_VOLUME)
                df = df.filter(pl.col("ticker").is_in(liquid["ticker"]))

                # 当日有交易
                day_data = bulk_daily.filter(pl.col("trade_date").cast(pl.Date) == date_val)
                day_data = day_data.with_columns(pl.col("volume").cast(pl.Float64, strict=False))
                traded = day_data.filter(pl.col("volume") > 0)
                df = df.filter(pl.col("ticker").is_in(traded["ticker"]))
        else:
            # Django ORM 回退（非回测模式）
            from django.db.models import Avg, F
            start_date = (date_dt - pd.Timedelta(days=40)).strftime("%Y-%m-%d")
            vol_qs = (
                USDailyPrice.objects.filter(
                    trade_date__gte=start_date, trade_date__lte=date,
                ).values("ticker")
                .annotate(avg_dollar_vol=Avg(F("close") * F("volume")))
            )
            liquid = {
                row["ticker"] for row in vol_qs
                if row["avg_dollar_vol"] and row["avg_dollar_vol"] >= US_MIN_DAILY_VOLUME
            }
            df = df.filter(pl.col("ticker").is_in(list(liquid)))

            traded = set(
                USDailyPrice.objects.filter(trade_date=date, volume__gt=0)
                .values_list("ticker", flat=True).distinct()
            )
            df = df.filter(pl.col("ticker").is_in(list(traded)))

    logger.info(f"US 股票池: {initial} → {df.height} (date={date})")
    return df.select(["ticker", "name", "sector", "industry", "market_cap"])
