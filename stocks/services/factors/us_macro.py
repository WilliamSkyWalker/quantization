"""
美股宏观因子: US_MACRO_CYCLE, US_MACRO_LIQD, US_MACRO_INFL, US_MACRO_EXTR

信号来源：us_macro_indicator 表（FRED 数据）
行业映射：GICS sector 敏感度系数
"""

import logging
from datetime import datetime, timedelta
from typing import Optional

import numpy as np
import polars as pl

from services.config import LOG_LEVEL
from stocks.services.factors.us_base import USFactorBase

logger = logging.getLogger(__name__)
logger.setLevel(LOG_LEVEL)

_EMPTY = pl.DataFrame(schema={"ticker": pl.Utf8, "factor_value": pl.Float64})

# FRED indicator codes (与 fred_downloader.py 的 FRED_SERIES_MAP 对应)
# 注意：us_macro_indicator.indicator_code 存储的是 FRED 原生 ID
_ISM_MFG = "MANEMP"           # Manufacturing Employment (ISM proxy)
_INDPRO = "INDPRO"            # Industrial Production
_FEDFUNDS = "FEDFUNDS"        # Federal Funds Rate
_DGS2 = "DGS2"               # 2-Year Treasury
_DGS10 = "DGS10"             # 10-Year Treasury
_M2 = "M2SL"                  # M2 Money Supply (mapped from US_M2)
_CPI = "CPIAUCSL"             # CPI
_CPILFESL = "CPILFESL"        # Core CPI
_PPI = "PPIACO"               # PPI (mapped from US_PPI)
_VIXCLS = "VIXCLS"            # VIX
_DTWEXBGS = "DTWEXBGS"        # USD Index
_UNRATE = "UNRATE"            # Unemployment Rate

# 宏观信号 Z-score 滚动窗口（月数）
_ZSCORE_WINDOW = 24


def _get_indicator_zscore(
    db, indicator_code: str, date: str, window: int = _ZSCORE_WINDOW,
    lag_days: int = 30,
) -> float:
    """获取指定宏观指标在 date 时的 Z-score。"""
    date_ts = datetime.strptime(date, "%Y-%m-%d") if isinstance(date, str) else date
    end_date = (date_ts - timedelta(days=lag_days)).strftime("%Y-%m-%d")
    start_date = (date_ts - timedelta(days=lag_days + window * 31 + 60)).strftime("%Y-%m-%d")

    sql = (
        "SELECT report_date, value FROM us_macro_indicator "
        "WHERE indicator_code = :code AND report_date >= :start AND report_date <= :end "
        "ORDER BY report_date"
    )
    df = db.query(sql, params={"code": indicator_code, "start": start_date, "end": end_date})
    if df.is_empty() or df.height < 6:
        logger.debug(f"_get_indicator_zscore: {indicator_code} 数据不足6条，返回0.0")
        return 0.0

    df = df.with_columns(pl.col("value").cast(pl.Float64, strict=False))
    vals = df.drop_nulls(subset=["value"])["value"].to_numpy()
    if len(vals) < 6:
        logger.debug(f"_get_indicator_zscore: {indicator_code} 有效值不足6条，返回0.0")
        return 0.0

    mean = np.mean(vals)
    std = np.std(vals)
    if std < 1e-10:
        logger.debug(f"_get_indicator_zscore: {indicator_code} 标准差为零，返回0.0")
        return 0.0
    return float((vals[-1] - mean) / std)


def _get_indicator_delta_zscore(
    db, indicator_code: str, date: str, delta_months: int = 3,
    window: int = _ZSCORE_WINDOW, lag_days: int = 30,
) -> float:
    """获取指标 N 月变化量的 Z-score。"""
    date_ts = datetime.strptime(date, "%Y-%m-%d") if isinstance(date, str) else date
    end_date = (date_ts - timedelta(days=lag_days)).strftime("%Y-%m-%d")
    start_date = (date_ts - timedelta(days=lag_days + (window + delta_months) * 31 + 60)).strftime("%Y-%m-%d")

    sql = (
        "SELECT report_date, value FROM us_macro_indicator "
        "WHERE indicator_code = :code AND report_date >= :start AND report_date <= :end "
        "ORDER BY report_date"
    )
    df = db.query(sql, params={"code": indicator_code, "start": start_date, "end": end_date})
    if df.is_empty() or df.height < delta_months + 6:
        logger.debug(f"_get_indicator_delta_zscore: {indicator_code} 数据不足，返回0.0")
        return 0.0

    df = df.with_columns(pl.col("value").cast(pl.Float64, strict=False))
    vals = df.drop_nulls(subset=["value"])["value"].to_numpy()
    if len(vals) < delta_months + 6:
        logger.debug(f"_get_indicator_delta_zscore: {indicator_code} 有效值不足，返回0.0")
        return 0.0

    # 计算差分序列
    deltas = vals[delta_months:] - vals[:-delta_months]
    mean = np.mean(deltas)
    std = np.std(deltas)
    if std < 1e-10:
        logger.debug(f"_get_indicator_delta_zscore: {indicator_code} 差分标准差为零，返回0.0")
        return 0.0
    return float((deltas[-1] - mean) / std)


# ================================================================
# GICS Sector 敏感度映射
# ================================================================
_CYCLE_SENSITIVITY = {
    "Industrials": 1.2, "Materials": 1.5, "Consumer Discretionary": 1.0,
    "Information Technology": 0.8, "Financials": 0.7, "Energy": 0.9,
    "Communication Services": 0.5, "Health Care": 0.2,
    "Consumer Staples": -0.2, "Utilities": -0.3, "Real Estate": 0.3,
}

_LIQD_SENSITIVITY = {
    "Real Estate": 1.0, "Financials": 0.8, "Consumer Discretionary": 0.6,
    "Information Technology": 0.5, "Industrials": 0.4, "Materials": 0.3,
    "Communication Services": 0.3, "Energy": 0.2,
    "Health Care": -0.2, "Consumer Staples": -0.3, "Utilities": 0.5,
}

_INFL_SENSITIVITY = {
    "Energy": 1.0, "Materials": 0.8, "Utilities": 0.5,
    "Real Estate": 0.3, "Industrials": 0.2, "Financials": 0.1,
    "Consumer Staples": -0.2, "Health Care": -0.3,
    "Consumer Discretionary": -0.5, "Information Technology": -0.4,
    "Communication Services": -0.3,
}

_EXTR_SENSITIVITY = {
    "Materials": 0.8, "Industrials": 0.6, "Energy": 0.5,
    "Information Technology": 0.4, "Consumer Discretionary": 0.3,
    "Communication Services": 0.3, "Health Care": 0.2,
    "Financials": 0.1, "Consumer Staples": -0.1,
    "Utilities": -0.2, "Real Estate": -0.1,
}


class _USMacroFactorBase(USFactorBase):
    """宏观因子基类：信号 x 行业敏感度 -> 个股因子值。"""

    _sensitivity_map: dict = {}

    def _compute_signal(self, date: str) -> float:
        raise NotImplementedError

    def compute(self, date: str, universe: pl.DataFrame) -> pl.DataFrame:
        signal = self._compute_signal(date)
        ind_map = self.get_industry_map_cached()

        df = universe.select("ticker").join(
            ind_map.select(["ticker", "sector"]),
            on="ticker",
            how="left",
        )

        # Map sector to sensitivity using replace_strict
        df = df.with_columns(
            pl.col("sector")
            .replace_strict(_sensitivity_map_to_use(self._sensitivity_map), default=0.0)
            .cast(pl.Float64)
            .alias("sensitivity")
        )
        df = df.with_columns(
            (pl.lit(signal) * pl.col("sensitivity")).alias("factor_value")
        )
        return df.select(["ticker", "factor_value"])


def _sensitivity_map_to_use(m: dict) -> dict:
    """Ensure all values are float for polars replace_strict."""
    return {k: float(v) for k, v in m.items()}


class USMacroCycle(_USMacroFactorBase):
    """US Macro Cycle: z(MANEMP) + z(INDPRO)"""
    name = "US_MACRO_CYCLE"
    description = "美国经济周期因子"
    _sensitivity_map = _CYCLE_SENSITIVITY

    def _compute_signal(self, date: str) -> float:
        cache_key = ("macro_cycle_signal", date)
        cached = self._date_cache.get(cache_key)
        if cached is not None:
            return cached

        z_mfg = _get_indicator_zscore(self.db, _ISM_MFG, date)
        z_ip = _get_indicator_zscore(self.db, _INDPRO, date)
        signal = 0.6 * z_mfg + 0.4 * z_ip
        self._date_cache[cache_key] = signal
        return signal


class USMacroLiqd(_USMacroFactorBase):
    """US Macro Liquidity: z(M2_YoY) - z(Δ3M FEDFUNDS) - z(Δ3M DGS2)"""
    name = "US_MACRO_LIQD"
    description = "美国流动性因子"
    _sensitivity_map = _LIQD_SENSITIVITY

    def _compute_signal(self, date: str) -> float:
        cache_key = ("macro_liqd_signal", date)
        cached = self._date_cache.get(cache_key)
        if cached is not None:
            return cached

        z_m2 = _get_indicator_zscore(self.db, _M2, date)
        z_ff = _get_indicator_delta_zscore(self.db, _FEDFUNDS, date, delta_months=3)
        z_2y = _get_indicator_delta_zscore(self.db, _DGS2, date, delta_months=3)
        signal = 0.4 * z_m2 - 0.3 * z_ff - 0.3 * z_2y
        self._date_cache[cache_key] = signal
        return signal


class USMacroInfl(_USMacroFactorBase):
    """US Macro Inflation: z(CPI) - z(PPI) spread + z(Core CPI)"""
    name = "US_MACRO_INFL"
    description = "美国通胀因子"
    _sensitivity_map = _INFL_SENSITIVITY

    def _compute_signal(self, date: str) -> float:
        cache_key = ("macro_infl_signal", date)
        cached = self._date_cache.get(cache_key)
        if cached is not None:
            return cached

        z_cpi = _get_indicator_zscore(self.db, _CPI, date)
        z_ppi = _get_indicator_zscore(self.db, _PPI, date)
        z_core = _get_indicator_zscore(self.db, _CPILFESL, date)
        # 正通胀利好能源/材料，不利消费/科技
        signal = 0.4 * (z_cpi - z_ppi) + 0.3 * z_core + 0.3 * z_ppi
        self._date_cache[cache_key] = signal
        return signal


class USMacroExtr(_USMacroFactorBase):
    """US Macro External: -z(USD) + z(1/VIX)"""
    name = "US_MACRO_EXTR"
    description = "美国外部环境因子 (弱美元+低波动利好)"
    _sensitivity_map = _EXTR_SENSITIVITY

    def _compute_signal(self, date: str) -> float:
        cache_key = ("macro_extr_signal", date)
        cached = self._date_cache.get(cache_key)
        if cached is not None:
            return cached

        z_usd = _get_indicator_zscore(self.db, _DTWEXBGS, date)
        z_vix = _get_indicator_zscore(self.db, _VIXCLS, date)
        # 弱美元利好出口型；低 VIX 利好风险资产
        signal = -0.5 * z_usd - 0.5 * z_vix
        self._date_cache[cache_key] = signal
        return signal
