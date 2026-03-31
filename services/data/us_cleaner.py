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
        "SELECT b.ticker, b.name, b.market_cap, b.ipo_date, "
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

    # 市值过滤（market_cap 为 NULL 时放行，稍后靠流动性过滤兜底）
    df["market_cap"] = pd.to_numeric(df["market_cap"], errors="coerce")
    df = df[(df["market_cap"] >= US_MIN_MARKET_CAP) | df["market_cap"].isna()]

    # 流动性过滤：查询最近20个交易日的平均成交额
    if not df.empty:
        tickers = df["ticker"].tolist()
        start_date = (date_dt - pd.Timedelta(days=40)).strftime("%Y-%m-%d")
        vol_sql = (
            "SELECT ticker, AVG(adj_close * volume) as avg_dollar_vol "
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
    if not df.empty:
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
