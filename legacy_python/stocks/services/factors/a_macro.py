"""
宏观经济因子（MACRO_CYCLE, MACRO_LIQD, MACRO_INFL, MACRO_EXTR）

利用宏观经济指标的 trailing Z-score 驱动行业轮动：
    - 计算宏观指标的标准化分数（24 月窗口）
    - 通过行业敏感度系数映射到个股
    - 防未来数据泄露：按 MACRO_PUBLICATION_LAG 延迟取值

设计模式复用 CMDTY_MOM：外部数据 → 行业映射 → 个股因子值。
"""

import logging
from abc import abstractmethod

import numpy as np
import pandas as pd

from services.config import (
    MACRO_ZSCORE_WINDOW,
    MACRO_PUBLICATION_LAG,
    MACRO_CYCLE_SENSITIVITY,
    MACRO_LIQD_SENSITIVITY,
    MACRO_INFL_SENSITIVITY,
    MACRO_EXTR_SENSITIVITY,
    LOG_LEVEL,
)
from stocks.services.factors.a_base import FactorBase

logger = logging.getLogger(__name__)
logger.setLevel(LOG_LEVEL)


class _MacroFactorBase(FactorBase):
    """
    宏观因子公共基类。

    子类实现 _compute_signal(date) 返回标量信号值，
    基类负责行业敏感度映射和个股赋值。
    """

    # 子类覆盖
    _sensitivity_map: dict[str, float] = {}

    def compute(self, date: str, universe: pd.DataFrame) -> pd.DataFrame:
        codes = universe["ts_code"].tolist()
        result = pd.DataFrame({"ts_code": codes, "factor_value": np.nan})

        # 1. 获取行业映射
        industry_df = self.get_industry_map_cached()
        if industry_df.empty:
            logger.warning(f"{self.name}: 无行业分类数据")
            return result

        # 2. 计算宏观信号（标量）
        signal = self._compute_signal(date)
        if signal is None or np.isnan(signal):
            logger.warning(f"{self.name}: 信号计算失败")
            return result

        # 3. 映射到个股: factor_value = signal × sensitivity
        stock_industry = industry_df[
            industry_df["ts_code"].isin(codes)
        ][["ts_code", "industry_name"]].copy()

        if stock_industry.empty:
            logger.debug(f"{self.name}: 股票池无行业匹配数据，返回空")
            return result

        def _map(row):
            ind = row.get("industry_name")
            if pd.notna(ind) and ind in self._sensitivity_map:
                return signal * self._sensitivity_map[ind]
            return np.nan

        stock_industry["factor_value"] = stock_industry.apply(_map, axis=1)

        result = result[["ts_code"]].merge(
            stock_industry[["ts_code", "factor_value"]],
            on="ts_code",
            how="left",
        )
        result["factor_value"] = result["factor_value"].astype(float)

        valid = result["factor_value"].notna().sum()
        logger.debug(f"{self.name}: signal={signal:.3f}, {valid}/{len(result)} 只有效值")
        return result[["ts_code", "factor_value"]]

    @abstractmethod
    def _compute_signal(self, date: str) -> float | None:
        """计算宏观信号（标量值），子类实现。"""
        raise NotImplementedError

    # ----------------------------------------------------------
    # 通用工具
    # ----------------------------------------------------------

    def _get_indicator_history(
        self, indicator_code: str, date: str, months: int | None = None,
    ) -> pd.Series:
        """
        获取宏观指标历史序列，已应用发布延迟。

        Args:
            indicator_code: 指标代码。
            date: 计算日期 YYYY-MM-DD。
            months: 回看月数，默认 MACRO_ZSCORE_WINDOW + 6（留足窗口余量）。

        Returns:
            pd.Series(index=report_date, values=value)，升序。
        """
        lag_days = MACRO_PUBLICATION_LAG.get(indicator_code, 0)
        effective_date = (
            pd.to_datetime(date) - pd.Timedelta(days=lag_days)
        ).strftime("%Y-%m-%d")

        if months is None:
            months = MACRO_ZSCORE_WINDOW + 6

        df = self.db.get_macro_indicator_history(indicator_code, effective_date, months)
        if df.empty:
            logger.debug(f"_get_indicator_history: 指标 {indicator_code} 无数据")
            return pd.Series(dtype=float)

        df["report_date"] = pd.to_datetime(df["report_date"])
        return df.set_index("report_date")["value"].sort_index()

    def _trailing_zscore(self, series: pd.Series, window: int | None = None) -> float | None:
        """
        计算序列最新值的 trailing Z-score。

        Args:
            series: 时间序列。
            window: 窗口大小（月数），默认 MACRO_ZSCORE_WINDOW。

        Returns:
            Z-score 浮点数，数据不足返回 None。
        """
        if window is None:
            window = MACRO_ZSCORE_WINDOW

        if len(series) < max(3, window // 2):
            logger.debug(f"_trailing_zscore: 数据不足({len(series)}<{max(3, window // 2)})，返回 None")
            return None

        # 取最近 window 个观测值
        tail = series.iloc[-window:] if len(series) >= window else series
        mean = tail.mean()
        std = tail.std()
        if std == 0 or np.isnan(std):
            logger.debug("_trailing_zscore: 标准差为0或NaN，返回 0.0")
            return 0.0

        latest = series.iloc[-1]
        z = (latest - mean) / std
        # 截断 ±3
        return float(np.clip(z, -3.0, 3.0))

    def _delta_zscore(
        self, indicator_code: str, date: str, delta_months: int = 3,
    ) -> float | None:
        """
        计算指标 N 月变化量的 trailing Z-score。

        用于利率类指标：z(Δ3M SHIBOR_3M) 等。

        Args:
            indicator_code: 指标代码。
            date: 计算日期。
            delta_months: 差分月数。

        Returns:
            Z-score，数据不足返回 None。
        """
        series = self._get_indicator_history(indicator_code, date)
        if len(series) < delta_months + 3:
            logger.debug(f"_delta_zscore: 指标 {indicator_code} 数据不足({len(series)}<{delta_months + 3})，返回 None")
            return None

        # 对日频数据按月重采样取末值
        if len(series) > MACRO_ZSCORE_WINDOW * 25:
            series = series.resample("ME").last().dropna()

        # 计算 delta
        delta = series.diff(delta_months).dropna()
        if delta.empty:
            logger.debug(f"_delta_zscore: 指标 {indicator_code} 差分后为空，返回 None")
            return None

        return self._trailing_zscore(delta)


# ============================================================
# 4 个宏观因子
# ============================================================

class MacroCycleFactor(_MacroFactorBase):
    """
    经济周期因子 (MACRO_CYCLE)

    信号: 0.5×z(PMI-50) + 0.3×z(PPI_YOY) + 0.2×z(PMI_NEW_ORDER-50)
    PMI 不可用时退化: 0.6×z(PPI_YOY) + 0.4×z(PPI_MP_YOY)
    """

    name = "MACRO_CYCLE"
    description = "经济周期因子，PMI/PPI驱动周期-防御轮动"
    _sensitivity_map = MACRO_CYCLE_SENSITIVITY

    def _compute_signal(self, date: str) -> float | None:
        # 尝试 PMI 版本
        pmi_series = self._get_indicator_history("PMI_MFG", date)
        ppi_series = self._get_indicator_history("PPI_YOY", date)

        if len(pmi_series) >= 6:
            # PMI 以 50 为荣枯线，减去 50 后标准化
            pmi_centered = pmi_series - 50.0
            z_pmi = self._trailing_zscore(pmi_centered)

            z_ppi = self._trailing_zscore(ppi_series) if len(ppi_series) >= 6 else None

            pmi_no_series = self._get_indicator_history("PMI_NEW_ORDER", date)
            z_pmi_no = None
            if len(pmi_no_series) >= 6:
                z_pmi_no = self._trailing_zscore(pmi_no_series - 50.0)

            if z_pmi is not None:
                signal = 0.5 * z_pmi
                if z_ppi is not None:
                    signal += 0.3 * z_ppi
                if z_pmi_no is not None:
                    signal += 0.2 * z_pmi_no
                return signal

        # 退化版本: PPI only
        if len(ppi_series) >= 6:
            z_ppi = self._trailing_zscore(ppi_series)
            ppi_mp_series = self._get_indicator_history("PPI_MP_YOY", date)
            z_ppi_mp = self._trailing_zscore(ppi_mp_series) if len(ppi_mp_series) >= 6 else None

            if z_ppi is not None:
                signal = 0.6 * z_ppi
                if z_ppi_mp is not None:
                    signal += 0.4 * z_ppi_mp
                return signal

        logger.debug("MACRO_CYCLE._compute_signal: PMI 和 PPI 数据均不足，返回 None")
        return None


class MacroLiquidityFactor(_MacroFactorBase):
    """
    流动性因子 (MACRO_LIQD)

    信号: 0.3×z(M1_M2_SPREAD) + 0.3×z(M2_YOY) + 0.2×(-z(Δ3M SHIBOR_3M)) + 0.2×(-z(Δ3M LPR_1Y))
    """

    name = "MACRO_LIQD"
    description = "流动性因子，货币宽松驱动地产/金融/成长轮动"
    _sensitivity_map = MACRO_LIQD_SENSITIVITY

    def _compute_signal(self, date: str) -> float | None:
        components = []
        weights = []

        # M1-M2 剪刀差
        spread = self._get_indicator_history("M1_M2_SPREAD", date)
        z_spread = self._trailing_zscore(spread) if len(spread) >= 6 else None
        if z_spread is not None:
            components.append(z_spread)
            weights.append(0.3)

        # M2 同比
        m2 = self._get_indicator_history("M2_YOY", date)
        z_m2 = self._trailing_zscore(m2) if len(m2) >= 6 else None
        if z_m2 is not None:
            components.append(z_m2)
            weights.append(0.3)

        # -z(Δ3M SHIBOR) — 利率下降=流动性宽松
        z_d_shibor = self._delta_zscore("SHIBOR_3M", date, delta_months=3)
        if z_d_shibor is not None:
            components.append(-z_d_shibor)
            weights.append(0.2)

        # -z(Δ3M LPR)
        z_d_lpr = self._delta_zscore("LPR_1Y", date, delta_months=3)
        if z_d_lpr is not None:
            components.append(-z_d_lpr)
            weights.append(0.2)

        if not components:
            logger.debug("MACRO_LIQD._compute_signal: 无可用流动性指标，返回 None")
            return None

        # 加权求和（按实际可用权重归一化）
        total_w = sum(weights)
        return sum(c * w for c, w in zip(components, weights)) / total_w if total_w > 0 else None


class MacroInflationFactor(_MacroFactorBase):
    """
    通胀结构因子 (MACRO_INFL)

    信号: 0.5×z(CPI_YOY - PPI_YOY) + 0.3×z(CPI_YOY) + 0.2×(-z(PPI_YOY))
    """

    name = "MACRO_INFL"
    description = "通胀结构因子，CPI-PPI剪刀差驱动消费-周期轮动"
    _sensitivity_map = MACRO_INFL_SENSITIVITY

    def _compute_signal(self, date: str) -> float | None:
        cpi = self._get_indicator_history("CPI_YOY", date)
        ppi = self._get_indicator_history("PPI_YOY", date)

        if len(cpi) < 6 or len(ppi) < 6:
            logger.debug(f"MACRO_INFL._compute_signal: CPI({len(cpi)})或PPI({len(ppi)})数据不足，返回 None")
            return None

        z_cpi = self._trailing_zscore(cpi)
        z_ppi = self._trailing_zscore(ppi)

        if z_cpi is None or z_ppi is None:
            logger.debug("MACRO_INFL._compute_signal: CPI 或 PPI z-score 计算失败，返回 None")
            return None

        # CPI-PPI 剪刀差（对齐日期后相减）
        aligned = pd.DataFrame({"cpi": cpi, "ppi": ppi}).dropna()
        if len(aligned) < 6:
            logger.debug(f"MACRO_INFL._compute_signal: CPI-PPI 对齐后数据不足({len(aligned)}<6)，返回 None")
            return None

        spread = aligned["cpi"] - aligned["ppi"]
        z_spread = self._trailing_zscore(spread)

        if z_spread is None:
            logger.debug("MACRO_INFL._compute_signal: CPI-PPI 剪刀差 z-score 计算失败，返回 None")
            return None

        return 0.5 * z_spread + 0.3 * z_cpi + 0.2 * (-z_ppi)


class MacroExternalFactor(_MacroFactorBase):
    """
    外部风险因子 (MACRO_EXTR)

    信号: 0.6×(-z(UST_10Y)) + 0.4×z(UST_2Y10Y)
    """

    name = "MACRO_EXTR"
    description = "外部风险因子，美债利率驱动成长-金融轮动"
    _sensitivity_map = MACRO_EXTR_SENSITIVITY

    def _compute_signal(self, date: str) -> float | None:
        components = []
        weights = []

        # -z(UST_10Y) — 美债利率下降利好成长
        ust10 = self._get_indicator_history("UST_10Y", date)
        if len(ust10) >= 6:
            # 对日频数据按月重采样取末值
            if len(ust10) > MACRO_ZSCORE_WINDOW * 25:
                ust10 = ust10.resample("ME").last().dropna()
            z_ust10 = self._trailing_zscore(ust10)
            if z_ust10 is not None:
                components.append(-z_ust10)
                weights.append(0.6)

        # z(UST_2Y10Y) — 期限利差走阔=经济预期改善
        ust_spread = self._get_indicator_history("UST_2Y10Y", date)
        if len(ust_spread) >= 6:
            if len(ust_spread) > MACRO_ZSCORE_WINDOW * 25:
                ust_spread = ust_spread.resample("ME").last().dropna()
            z_spread = self._trailing_zscore(ust_spread)
            if z_spread is not None:
                components.append(z_spread)
                weights.append(0.4)

        if not components:
            logger.debug("MACRO_EXTR._compute_signal: 无可用美债指标，返回 None")
            return None

        total_w = sum(weights)
        return sum(c * w for c, w in zip(components, weights)) / total_w if total_w > 0 else None
