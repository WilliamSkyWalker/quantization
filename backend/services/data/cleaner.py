"""
数据清洗模块

负责对原始数据进行清洗和过滤，生成可用于因子计算和回测的干净股票池。

清洗规则：
    1. 剔除 ST / *ST 股票
    2. 剔除上市不足 180 天的新股
    3. 剔除已退市股票
    4. 剔除当日停牌（成交量为 0）的股票
    5. 校验并修正涨跌停标记
    6. 剔除日均成交额低于阈值的股票（可选）

核心接口：
    get_clean_universe(db, date) -> 返回某日可交易股票池
"""

import logging
from datetime import date, timedelta
from typing import Optional

import pandas as pd

from backend.services.config import (
    IPO_FILTER_DAYS,
    MIN_DAILY_TURNOVER,
    MIN_MARKET_CAP,
    EXCLUDE_STAR_MARKET,
    ALLOWED_INDUSTRIES,
    LOG_LEVEL,
)
from backend.services.data.database import DatabaseManager

# ============================================================
# 日志配置
# ============================================================

logger = logging.getLogger(__name__)
logger.setLevel(LOG_LEVEL)


# ============================================================
# 涨跌停校验
# ============================================================

def verify_limit_flags(df: pd.DataFrame) -> pd.DataFrame:
    """
    校验并修正涨跌停标记。

    根据涨跌幅和板块类型重新判断涨跌停状态：
        - 主板：±10%（阈值 9.9%）
        - 创业板/科创板：±20%（阈值 19.9%）

    Args:
        df: 包含 ts_code, pct_chg, is_limit_up, is_limit_down 列的 DataFrame。

    Returns:
        修正后的 DataFrame（新增 is_limit_up_v, is_limit_down_v 列）。
    """
    if df.empty:
        return df

    df = df.copy()

    # 根据代码判断板块涨跌停幅度
    def _get_limit_threshold(ts_code: str) -> float:
        code = ts_code.split(".")[0]
        if code.startswith(("30", "68")):
            return 19.9  # 创业板/科创板 ±20%
        return 9.9  # 主板 ±10%

    df["_limit_thr"] = df["ts_code"].apply(_get_limit_threshold)

    df["is_limit_up_v"] = (df["pct_chg"] >= df["_limit_thr"]).astype(int)
    df["is_limit_down_v"] = (df["pct_chg"] <= -df["_limit_thr"]).astype(int)

    # 统计修正数量
    up_diff = (df["is_limit_up"] != df["is_limit_up_v"]).sum()
    down_diff = (df["is_limit_down"] != df["is_limit_down_v"]).sum()
    if up_diff > 0 or down_diff > 0:
        logger.info(f"涨跌停标记修正: 涨停 {up_diff} 条, 跌停 {down_diff} 条")

    df.drop(columns=["_limit_thr"], inplace=True)
    return df


# ============================================================
# 停牌标记
# ============================================================

def mark_suspended(df: pd.DataFrame) -> pd.DataFrame:
    """
    标记停牌日（成交量为 0 的交易日视为停牌）。

    Args:
        df: 包含 volume 列的日线行情 DataFrame。

    Returns:
        新增 is_suspended 列（1=停牌, 0=正常交易）的 DataFrame。
    """
    df = df.copy()
    df["is_suspended"] = ((df["volume"].isna()) | (df["volume"] == 0)).astype(int)

    suspended_count = df["is_suspended"].sum()
    if suspended_count > 0:
        logger.info(f"标记停牌: {suspended_count} 条记录")

    return df


# ============================================================
# 核心接口：获取干净股票池
# ============================================================

def get_clean_universe(
    db: DatabaseManager,
    target_date: str,
    min_turnover: Optional[float] = None,
    lookback_days: int = 20,
    skip_industry_filter: bool = False,
) -> pd.DataFrame:
    """
    获取某一日的可交易股票池（干净宇宙）。

    过滤规则（依次执行）：
        1. 剔除已退市股票
        2. 剔除 ST / *ST
        3. 剔除上市不足 IPO_FILTER_DAYS 天的新股
        4. 剔除当日停牌（成交量=0）
        5. 剔除当日涨停（不可买入）—— 可选标记，不从池中移除
        6. 剔除近 N 日日均成交额低于阈值的股票（可选）

    Args:
        db: DatabaseManager 实例。
        target_date: 目标日期，格式 YYYY-MM-DD 或 YYYYMMDD。
        min_turnover: 日均成交额下限（元），默认使用 settings 中的配置。
                      传入 0 或 None 则不过滤。
        lookback_days: 计算日均成交额的回看天数，默认 20 个交易日。

    Returns:
        DataFrame，包含以下列：
            - ts_code: 股票代码
            - name: 股票名称
            - market: 板块
            - list_date: 上市日期
            - industry_name: 行业（如有）
            - is_limit_up: 当日是否涨停
            - is_limit_down: 当日是否跌停
            - avg_amount: 近 N 日日均成交额
    """
    # 标准化日期格式
    target_date_str = pd.to_datetime(target_date).strftime("%Y-%m-%d")
    target_dt = pd.to_datetime(target_date).date()

    if min_turnover is None:
        min_turnover = MIN_DAILY_TURNOVER

    logger.info(f"构建股票池: 目标日期={target_date_str}")

    # ----------------------------------------------------------
    # 1. 获取基本信息，过滤退市、ST、新股
    # ----------------------------------------------------------
    df_basic = db.query(
        "SELECT ts_code, name, market, list_date, delist_date, is_st "
        "FROM stock_basic"
    )

    initial_count = len(df_basic)

    # 排除已退市
    target_ts = pd.Timestamp(target_dt)
    df_basic = df_basic[
        (df_basic["delist_date"].isna()) |
        (pd.to_datetime(df_basic["delist_date"]) > target_ts)
    ]
    after_delist = len(df_basic)

    # 排除 ST
    df_basic = df_basic[df_basic["is_st"] == 0]
    after_st = len(df_basic)

    # 排除科创板（688 开头）
    if EXCLUDE_STAR_MARKET:
        df_basic = df_basic[~df_basic["ts_code"].str.startswith("68")]
        after_star = len(df_basic)
    else:
        after_star = after_st

    # 排除上市不足 N 天的新股
    ipo_cutoff = target_dt - timedelta(days=IPO_FILTER_DAYS)
    ipo_cutoff_ts = pd.Timestamp(ipo_cutoff)
    df_basic = df_basic[
        pd.to_datetime(df_basic["list_date"]).notna() &
        (pd.to_datetime(df_basic["list_date"]) <= ipo_cutoff_ts)
    ]
    after_ipo = len(df_basic)

    star_msg = f" -> 科创板{after_star}" if EXCLUDE_STAR_MARKET else ""
    logger.info(
        f"基本面过滤: {initial_count} -> 退市{after_delist} -> "
        f"ST{after_st}{star_msg} -> 新股{after_ipo}"
    )

    if df_basic.empty:
        logger.warning("过滤后股票池为空")
        return pd.DataFrame()

    stock_codes = df_basic["ts_code"].tolist()

    # ----------------------------------------------------------
    # 2. 获取当日行情，过滤停牌
    # ----------------------------------------------------------
    codes_str = "','".join(stock_codes)
    df_daily = db.query(
        f"SELECT ts_code, volume, amount, pct_chg, is_limit_up, is_limit_down "
        f"FROM daily_price "
        f"WHERE trade_date = '{target_date_str}' "
        f"AND ts_code IN ('{codes_str}')"
    )

    if df_daily.empty:
        logger.warning(f"{target_date_str} 无行情数据（数据尚未下载或非交易日，请先执行增量更新）")
        return pd.DataFrame()

    # 过滤停牌
    df_daily = df_daily[(df_daily["volume"].notna()) & (df_daily["volume"] > 0)]
    after_suspend = len(df_daily)
    logger.info(f"停牌过滤: {after_suspend} 只可交易")

    # ----------------------------------------------------------
    # 3. 流动性过滤（日均成交额）
    # ----------------------------------------------------------
    if min_turnover and min_turnover > 0:
        # 向前取 lookback_days 个交易日的平均成交额
        lookback_start = (target_dt - timedelta(days=lookback_days * 2)).strftime("%Y-%m-%d")
        df_amount = db.query(
            f"SELECT ts_code, trade_date, amount "
            f"FROM daily_price "
            f"WHERE trade_date >= '{lookback_start}' "
            f"AND trade_date <= '{target_date_str}' "
            f"AND ts_code IN ('{codes_str}')"
        )

        if not df_amount.empty:
            # 取每只股票最近 lookback_days 个交易日
            df_amount["trade_date"] = pd.to_datetime(df_amount["trade_date"])
            df_amount = df_amount.sort_values(["ts_code", "trade_date"])
            df_avg = (
                df_amount.groupby("ts_code")
                .apply(lambda x: x.tail(lookback_days)["amount"].mean(), include_groups=False)
                .reset_index(name="avg_amount")
            )

            # 过滤低流动性
            liquid_codes = df_avg[df_avg["avg_amount"] >= min_turnover]["ts_code"].tolist()
            df_daily = df_daily[df_daily["ts_code"].isin(liquid_codes)]

            # 合并平均成交额
            df_daily = df_daily.merge(df_avg, on="ts_code", how="left")
        else:
            df_daily["avg_amount"] = None

        after_liquidity = len(df_daily)
        logger.info(f"流动性过滤(>={min_turnover/1e4:.0f}万): {after_liquidity} 只")
    else:
        df_daily["avg_amount"] = None

    # ----------------------------------------------------------
    # 4. 市值过滤（剔除微盘股）
    # ----------------------------------------------------------
    if MIN_MARKET_CAP and MIN_MARKET_CAP > 0:
        codes_str_mv = "','".join(df_daily["ts_code"].tolist())
        df_mv = db.query(
            f"SELECT d.ts_code, d.`close`, s.total_share "
            f"FROM daily_price d "
            f"INNER JOIN stock_basic s ON d.ts_code = s.ts_code "
            f"WHERE d.trade_date = '{target_date_str}' "
            f"AND d.ts_code IN ('{codes_str_mv}')"
        )
        if not df_mv.empty and "total_share" in df_mv.columns:
            # total_share 单位为万股，close 单位为元，total_mv = close * total_share * 10000（元）
            df_mv["total_mv"] = df_mv["close"] * df_mv["total_share"] * 10000
            large_codes = df_mv[df_mv["total_mv"] >= MIN_MARKET_CAP]["ts_code"].tolist()
            before_mv = len(df_daily)
            df_daily = df_daily[df_daily["ts_code"].isin(large_codes)]
            logger.info(f"市值过滤(>={MIN_MARKET_CAP/1e8:.0f}亿): {before_mv} -> {len(df_daily)} 只")

    # ----------------------------------------------------------
    # 5. 合并基本信息
    # ----------------------------------------------------------
    df_result = df_daily.merge(
        df_basic[["ts_code", "name", "market", "list_date"]],
        on="ts_code",
        how="left",
    )

    # 尝试合并行业信息
    try:
        df_industry = db.get_industry_map()
        if not df_industry.empty:
            df_result = df_result.merge(df_industry, on="ts_code", how="left")
        else:
            df_result["industry_name"] = None
    except Exception:
        df_result["industry_name"] = None

    # 行业白名单过滤（同时检查 L1 和 L2 行业名）
    if ALLOWED_INDUSTRIES and "industry_name" in df_result.columns and not skip_industry_filter:
        before_ind = len(df_result)
        l1_match = df_result["industry_name"].isin(ALLOWED_INDUSTRIES)
        if "l2_industry_name" in df_result.columns:
            l2_match = df_result["l2_industry_name"].isin(ALLOWED_INDUSTRIES)
        else:
            l2_match = False
        df_result = df_result[l1_match | l2_match]
        logger.info(
            f"行业白名单过滤({','.join(ALLOWED_INDUSTRIES)}): "
            f"{before_ind} -> {len(df_result)} 只"
        )

    # 整理输出列
    output_cols = [
        "ts_code", "name", "market", "list_date", "industry_name",
        "l2_industry_name", "is_limit_up", "is_limit_down", "avg_amount",
    ]
    df_result = df_result[[c for c in output_cols if c in df_result.columns]]

    logger.info(f"最终股票池: {len(df_result)} 只")
    return df_result


# ============================================================
# 批量 Universe 预计算（回测向量化优化 Tier 1）
# ============================================================

def preload_clean_universes(
    db: DatabaseManager,
    dates: list[str],
    bulk_daily: pd.DataFrame,
    min_turnover: float = 0,
) -> dict[str, pd.DataFrame]:
    """
    批量预计算所有调仓日的 clean universe，避免逐日 SQL 查询。

    使用预加载的 bulk_daily 内存数据替代逐日 DB 查询，
    将 167 次 × 3-5 条 SQL 降为 1 次 stock_basic 查询 + 内存过滤。

    Args:
        db: DatabaseManager 实例。
        dates: 调仓日期列表。
        bulk_daily: 预加载的 daily_price DataFrame（含 trade_date, ts_code 等列）。

    Returns:
        {date_str: universe_df} 字典，格式与 get_clean_universe() 一致。
    """
    import time
    t0 = time.time()

    if bulk_daily is None or bulk_daily.empty:
        return {}

    # 1. 查 stock_basic 一次
    df_basic = db.query(
        "SELECT ts_code, name, market, list_date, delist_date, is_st, total_share "
        "FROM stock_basic"
    )
    if df_basic.empty:
        return {}

    # 静态过滤（ST、科创板）
    df_basic = df_basic[df_basic["is_st"] == 0]
    if EXCLUDE_STAR_MARKET:
        df_basic = df_basic[~df_basic["ts_code"].str.startswith("68")]
    df_basic["list_date_ts"] = pd.to_datetime(df_basic["list_date"])
    df_basic["delist_date_ts"] = pd.to_datetime(df_basic["delist_date"])
    df_basic["total_share"] = pd.to_numeric(df_basic["total_share"], errors="coerce")

    # 2. 行业映射一次
    try:
        df_industry = db.get_industry_map()
    except Exception:
        df_industry = pd.DataFrame()

    # 3. 预计算 rolling 20 日均成交额
    need_cols = ["ts_code", "trade_date", "volume", "amount", "pct_chg", "close"]
    bd = bulk_daily[[c for c in need_cols if c in bulk_daily.columns]].copy()
    for col in ["volume", "amount", "close", "pct_chg"]:
        if col in bd.columns:
            bd[col] = pd.to_numeric(bd[col], errors="coerce")

    # 涨跌停标记：优先使用预计算列，否则从 pct_chg 推断
    if "is_limit_up" in bulk_daily.columns:
        bd["is_limit_up"] = bulk_daily["is_limit_up"]
        bd["is_limit_down"] = bulk_daily["is_limit_down"]
    else:
        pct = bd["pct_chg"]
        bd["is_limit_up"] = (pct >= 9.8).astype(int)
        bd["is_limit_down"] = (pct <= -9.8).astype(int)
    bd = bd.sort_values(["ts_code", "trade_date"])

    if min_turnover > 0:
        bd["avg_amount_20d"] = bd.groupby("ts_code")["amount"].transform(
            lambda x: x.rolling(20, min_periods=10).mean()
        )
    else:
        bd["avg_amount_20d"] = 0

    # 4. 按日期建索引加速查找
    bd["trade_date"] = pd.to_datetime(bd["trade_date"])
    bd_by_date = {dt: grp for dt, grp in bd.groupby("trade_date")}

    # 5. 逐日期构建 universe
    result = {}
    for date_str in dates:
        date_ts = pd.to_datetime(date_str)

        # IPO 过滤
        ipo_cutoff = date_ts - pd.Timedelta(days=IPO_FILTER_DAYS)
        valid = df_basic[
            ((df_basic["delist_date_ts"].isna()) | (df_basic["delist_date_ts"] > date_ts)) &
            (df_basic["list_date_ts"].notna()) & (df_basic["list_date_ts"] <= ipo_cutoff)
        ]
        valid_codes = set(valid["ts_code"].tolist())

        # 当日行情
        day = bd_by_date.get(date_ts)
        if day is None or day.empty:
            continue

        day = day[day["ts_code"].isin(valid_codes)].copy()

        # 停牌过滤
        day = day[(day["volume"].notna()) & (day["volume"] > 0)]

        # 流动性过滤
        if min_turnover > 0:
            day = day[day["avg_amount_20d"] >= min_turnover]

        # 市值过滤
        if MIN_MARKET_CAP and MIN_MARKET_CAP > 0:
            day = day.merge(
                valid[["ts_code", "total_share"]], on="ts_code", how="left"
            )
            day["total_mv"] = day["close"] * day["total_share"] * 10000
            day = day[day["total_mv"] >= MIN_MARKET_CAP]

        if day.empty:
            continue

        # 合并基本信息
        day = day.merge(
            valid[["ts_code", "name", "market", "list_date"]], on="ts_code", how="left"
        )
        if not df_industry.empty:
            day = day.merge(df_industry, on="ts_code", how="left")
        else:
            day["industry_name"] = None

        # 行业白名单
        if ALLOWED_INDUSTRIES and "industry_name" in day.columns:
            l1_match = day["industry_name"].isin(ALLOWED_INDUSTRIES)
            l2_match = day["l2_industry_name"].isin(ALLOWED_INDUSTRIES) if "l2_industry_name" in day.columns else False
            day = day[l1_match | l2_match]

        output_cols = [
            "ts_code", "name", "market", "list_date", "industry_name",
            "l2_industry_name", "is_limit_up", "is_limit_down", "avg_amount_20d",
        ]
        day = day[[c for c in output_cols if c in day.columns]]
        # 重命名 avg_amount_20d → avg_amount 以匹配原函数输出格式
        if "avg_amount_20d" in day.columns:
            day = day.rename(columns={"avg_amount_20d": "avg_amount"})

        result[date_str] = day

    logger.info(f"批量预计算 universe: {len(result)}/{len(dates)} 个日期, {time.time()-t0:.1f}s")
    return result


# ============================================================
# 批量清洗工具
# ============================================================

def batch_clean_daily_data(db: DatabaseManager, ts_code: str) -> pd.DataFrame:
    """
    对单只股票的全部日线数据做清洗，返回清洗后的 DataFrame。

    清洗内容：
        - 标记停牌日
        - 校验涨跌停标记
        - 填充缺失值（前向填充收盘价，成交量缺失填0）

    Args:
        db: DatabaseManager 实例。
        ts_code: 股票代码。

    Returns:
        清洗后的日线行情 DataFrame。
    """
    df = db.get_daily_price(ts_code=ts_code)
    if df.empty:
        return df

    # 标记停牌
    df = mark_suspended(df)

    # 校验涨跌停
    df = verify_limit_flags(df)

    # 用校验后的值覆盖原始标记
    if "is_limit_up_v" in df.columns:
        df["is_limit_up"] = df["is_limit_up_v"]
        df["is_limit_down"] = df["is_limit_down_v"]
        df.drop(columns=["is_limit_up_v", "is_limit_down_v"], inplace=True)

    # 缺失值处理
    df["close"] = df["close"].ffill()
    df["volume"] = df["volume"].fillna(0)
    df["amount"] = df["amount"].fillna(0)

    return df


# ============================================================
# 命令行入口
# ============================================================

if __name__ == "__main__":
    import sys

    logging.basicConfig(
        level=LOG_LEVEL,
        format="%(asctime)s | %(name)-20s | %(levelname)-7s | %(message)s",
    )

    db = DatabaseManager()
    db.init_tables()

    # 默认查看最新交易日的股票池
    target = sys.argv[1] if len(sys.argv) > 1 else None
    if target is None:
        target = db.get_latest_trade_date()
        if target is None:
            print("数据库中无行情数据，请先下载日线行情")
            sys.exit(1)

    print(f"=== 构建 {target} 股票池 ===")
    df = get_clean_universe(db, target)
    if df.empty:
        print("股票池为空")
    else:
        print(f"\n可交易股票: {len(df)} 只")
        print(f"涨停股票: {df['is_limit_up'].sum()} 只（池中保留但不可买入）")
        print(f"跌停股票: {df['is_limit_down'].sum()} 只（池中保留但不可卖出）")
        print(f"\n前10只:")
        print(df.head(10).to_string(index=False))
