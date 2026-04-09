"""
美股股票池清洗

过滤条件：
    - is_active = 1
    - IPO 至少 N 天
    - 最低日均成交额
    - 最低市值
    - 排除当日无交易（停牌/退市）
"""

import logging
from datetime import datetime

import pandas as pd

from services.config import (
    LOG_LEVEL,
    US_MIN_DAILY_VOLUME,
    US_MIN_MARKET_CAP,
    US_MIN_LISTING_DAYS,
)
from services.data.database import DatabaseManager

logger = logging.getLogger(__name__)
logger.setLevel(LOG_LEVEL)


def get_us_clean_universe(db: DatabaseManager, date: str) -> pd.DataFrame:
    """
    获取指定日期的美股可交易股票池。

    Returns:
        DataFrame[ticker, name, sector, industry, market_cap]
    """
    date_dt = pd.to_datetime(date)
    cutoff_date = (date_dt - pd.Timedelta(days=US_MIN_LISTING_DAYS)).strftime("%Y-%m-%d")

    # 基本筛选：
    # - is_active=1（当前成分股）：直接入选
    # - is_active=0（历史成分股）：需有行业数据才入选（无 sector 的退市股无法中性化）
    sql = (
        "SELECT b.ticker, b.name, b.ipo_date, "
        "       c.sector, c.industry "
        "FROM us_stock_basic b "
        "LEFT JOIN us_industry_class c ON b.ticker = c.ticker "
        "WHERE (b.is_active = 1 OR (b.is_active = 0 AND c.sector IS NOT NULL)) "
        "AND (b.ipo_date IS NULL OR b.ipo_date <= :cutoff)"
    )
    df = db.query(sql, params={"cutoff": cutoff_date})

    if df.empty:
        logger.warning(f"US 股票池为空 (date={date})")
        return df

    initial = len(df)

    # 历史市值过滤（使用 us_key_metric 历史数据，消除前瞻偏差）
    from services.us_factors.base import USFactorBase
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
        # 回退：非回测模式用静态快照
        static_mktcap = db.query(
            "SELECT ticker, market_cap FROM us_stock_basic WHERE market_cap IS NOT NULL"
        )
        if not static_mktcap.empty:
            static_mktcap["market_cap"] = pd.to_numeric(static_mktcap["market_cap"], errors="coerce")
            df = df.merge(static_mktcap[["ticker", "market_cap"]], on="ticker", how="left")
        else:
            df["market_cap"] = float("nan")

    df["market_cap"] = pd.to_numeric(df["market_cap"], errors="coerce")
    df = df[(df["market_cap"] >= US_MIN_MARKET_CAP) | df["market_cap"].isna()]

    # 流动性过滤：优先从预加载缓存计算，回退到 SQL
    if not df.empty:
        from services.us_factors.base import USFactorBase
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

                # 排除当日无交易
                day_mask = bulk_daily["trade_date"] == date_dt
                traded = bulk_daily[day_mask & (pd.to_numeric(bulk_daily["volume"], errors="coerce") > 0)]["ticker"]
                df = df[df["ticker"].isin(traded)]
            else:
                logger.debug(f"get_us_clean_universe: 内存缓存中 {date} 附近无数据，跳过流动性过滤")
        else:
            # SQL 回退（无预加载时）
            start_date = (date_dt - pd.Timedelta(days=40)).strftime("%Y-%m-%d")
            vol_sql = (
                "SELECT ticker, AVG(close * volume) as avg_dollar_vol "
                "FROM us_daily_price "
                "WHERE trade_date >= :start AND trade_date <= :end "
                "GROUP BY ticker"
            )
            vol_df = db.query(vol_sql, params={"start": start_date, "end": date})
            if not vol_df.empty:
                vol_df["avg_dollar_vol"] = pd.to_numeric(vol_df["avg_dollar_vol"], errors="coerce")
                liquid = vol_df[vol_df["avg_dollar_vol"] >= US_MIN_DAILY_VOLUME]["ticker"]
                df = df[df["ticker"].isin(liquid)]

            # 排除当日无交易的股票
            traded_sql = (
                "SELECT DISTINCT ticker FROM us_daily_price "
                "WHERE trade_date = :date AND volume > 0"
            )
            traded = db.query(traded_sql, params={"date": date})
            if not traded.empty:
                df = df[df["ticker"].isin(traded["ticker"])]

    # 注意：低覆盖度股票池筛选已移除（样本外验证显示可能有前视偏差）

    logger.info(f"US 股票池: {initial} → {len(df)} (date={date})")
    return df[["ticker", "name", "sector", "industry", "market_cap"]].reset_index(drop=True)
