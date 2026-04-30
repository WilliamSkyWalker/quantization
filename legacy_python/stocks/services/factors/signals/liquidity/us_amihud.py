"""Amihud Illiquidity 因子 (Amihud 2002, JFM)

定义：
    ILLIQ = (1/D) × Σ |R_d| / Volume_d    (过去 D 个交易日)

    D = 21 天（1 个月）

经济直觉：
    - ILLIQ 衡量"每单位成交量引起的价格变动"→ 高 ILLIQ = 流动性差
    - 非流动性溢价：流动性差的股票应有更高预期收益（Amihud 2002）
    - 但实证中高 ILLIQ 小票容易暴跌 → 反向更常见

    这里 inherent_direction = 0，让滚动 IC 决定方向。

注意：
    Volume 用美元交易量 = close × volume（单位对齐）。
    FMP 的 volume 是股数，需要 × close 转换。

因子方向：0（由滚动 IC 决定）
"""

import logging

import numpy as np
import pandas as pd

from services.config import LOG_LEVEL
from stocks.services.factors.us_registry import AlphaSignal, register

logger = logging.getLogger(__name__)
logger.setLevel(LOG_LEVEL)


@register
class AmihudIlliquidity(AlphaSignal):
    """Amihud Illiquidity — |R| / DollarVolume 月均（非流动性）。"""

    name = "AMIHUD_ILLIQ"
    version = "v1"
    category = "liquidity"
    horizon = "month"
    expected_icir = 0.08
    status = "staging"
    inherent_direction = 0  # 方向由滚动 IC 决定
    data_deps = ["us_daily_price"]
    ic_window_months = 12

    _LOOKBACK_DAYS = 35  # ~21 交易日 + buffer
    _MIN_DAYS = 15

    def compute(self, date: str, universe: pd.DataFrame) -> pd.DataFrame:
        tickers = universe["ticker"].tolist()
        if not tickers:
            logger.debug("AmihudIlliq: 空 universe")
            return pd.DataFrame(columns=["ticker", "factor_value"])

        # 从预加载缓存直接切片（避免 fetch_price_history 大切片）
        bulk_daily = self._static_cache.get("_bulk_daily")
        if bulk_daily is None or bulk_daily.empty:
            logger.warning(f"AmihudIlliq({date}): 无预加载数据")
            return pd.DataFrame(columns=["ticker", "factor_value"])

        date_ts = pd.to_datetime(date)
        start_ts = date_ts - pd.Timedelta(days=self._LOOKBACK_DAYS)
        ticker_set = set(tickers)

        mask = (
            bulk_daily["ticker"].isin(ticker_set)
            & (bulk_daily["trade_date"] >= start_ts)
            & (bulk_daily["trade_date"] <= date_ts)
        )
        hist = bulk_daily.loc[mask, ["ticker", "adj_close", "close", "volume"]].copy()

        if hist.empty:
            logger.warning(f"AmihudIlliq({date}): 无价格数据")
            return pd.DataFrame(columns=["ticker", "factor_value"])

        hist = hist.dropna(subset=["adj_close", "close", "volume"])
        hist["adj_close"] = hist["adj_close"].astype(float)
        hist["close"] = hist["close"].astype(float)
        hist["volume"] = hist["volume"].astype(float)

        # 向量化计算：按 ticker 分组算收益和成交量
        hist = hist.sort_values(["ticker", "adj_close"])  # 保持时间序排序
        hist["ret"] = hist.groupby("ticker")["adj_close"].pct_change()
        hist["dollar_vol"] = hist["close"] * hist["volume"]

        # 过滤有效行
        valid = hist.dropna(subset=["ret"]).copy()
        valid = valid[valid["dollar_vol"] > 0]

        # 向量化聚合
        valid["illiq_ratio"] = valid["ret"].abs() / valid["dollar_vol"]
        agg = valid.groupby("ticker").agg(
            illiq_mean=("illiq_ratio", "mean"),
            n_days=("illiq_ratio", "count"),
        ).reset_index()

        agg = agg[agg["n_days"] >= self._MIN_DAYS]
        agg = agg[agg["illiq_mean"] > 0]
        agg["factor_value"] = np.log(agg["illiq_mean"])

        out = agg[["ticker", "factor_value"]].copy()
        logger.info(f"AmihudIlliq({date}): {len(out)} 有值")
        return out
