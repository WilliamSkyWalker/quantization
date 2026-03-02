"""
质量因子

实现基于财务质量的因子：
    - ROE_TTM: 净资产收益率（滚动12个月）
    - GROSS_MARGIN: 销售毛利率
    - PROFIT_STB: 净利润同比增速变异系数（利润稳定性）
    - MARGIN_TREND: 毛利率环比变化（利润率改善趋势）

使用公告日期获取最新可用的财务数据，防止未来函数。
"""

import numpy as np
import pandas as pd

from backend.services.factors.base import FactorBase


class ROEFactor(FactorBase):
    """
    ROE_TTM 因子。

    取截止计算日已公告的最新一期 ROE_TTM 值。
    ROE 越高表示盈利能力越强。
    """

    name = "ROE_TTM"
    description = "净资产收益率TTM，越高盈利能力越强"

    def compute(self, date: str, universe: pd.DataFrame) -> pd.DataFrame:
        codes = universe["ts_code"].tolist()

        df_fin = self.get_latest_financial(date, ["roe_ttm"], codes)

        if df_fin.empty:
            return pd.DataFrame(columns=["ts_code", "factor_value"])

        df_fin["factor_value"] = pd.to_numeric(df_fin["roe_ttm"], errors="coerce")

        return df_fin[["ts_code", "factor_value"]]


class GrossMarginFactor(FactorBase):
    """
    毛利率因子。

    取截止计算日已公告的最新一期销售毛利率。
    毛利率越高表示产品定价能力和竞争优势越强。
    """

    name = "GROSS_MARGIN"
    description = "销售毛利率，越高竞争优势越强"

    def compute(self, date: str, universe: pd.DataFrame) -> pd.DataFrame:
        codes = universe["ts_code"].tolist()

        df_fin = self.get_latest_financial(date, ["gross_margin"], codes)

        if df_fin.empty:
            return pd.DataFrame(columns=["ts_code", "factor_value"])

        df_fin["factor_value"] = pd.to_numeric(
            df_fin["gross_margin"], errors="coerce"
        )

        return df_fin[["ts_code", "factor_value"]]


class ProfitStabilityFactor(FactorBase):
    """
    净利润同比增速的变异系数（Coefficient of Variation）。

    取截止到计算日已公告的最近 4+ 个报告期的净利润，
    计算 3 组同比增速（每组 = 当期 / 同比上年同期 - 1），
    因子值 = std(yoy_growth) / |mean(yoy_growth)|。

    CV 越小表示利润增长越稳定，穿越风格切换能力越强。
    作为反向因子使用（低 CV 更好）。
    """

    name = "PROFIT_STB"
    description = "净利润增速变异系数，越小利润越稳定（反向因子）"

    def compute(self, date: str, universe: pd.DataFrame) -> pd.DataFrame:
        codes = universe["ts_code"].tolist()

        # 查询截止到 date 已公告的所有净利润数据
        params: dict = {"date": date}
        sql = (
            "SELECT ts_code, ann_date, end_date, net_profit FROM financial_data "
            "WHERE ann_date <= :date AND net_profit IS NOT NULL"
        )
        if codes:
            in_clause, in_params = self._build_in_clause(codes)
            sql += f" AND ts_code IN {in_clause}"
            params.update(in_params)
        sql += " ORDER BY ts_code, end_date DESC"

        df = self.db.query(sql, params=params)

        if df.empty:
            return pd.DataFrame(columns=["ts_code", "factor_value"])

        df["end_date"] = pd.to_datetime(df["end_date"])
        df["net_profit"] = pd.to_numeric(df["net_profit"], errors="coerce")

        results = []
        for ts_code, grp in df.groupby("ts_code"):
            grp = grp.drop_duplicates(subset=["end_date"]).sort_values(
                "end_date", ascending=False
            )

            # 需要至少 7 个报告期（最近 4 期 + 各自同比上年同期共 4 期，可能有重叠）
            if len(grp) < 4:
                continue

            # 取最近 4 个报告期，尝试匹配各自的同比上年同期
            yoy_growths = []
            for _, row in grp.head(4).iterrows():
                end_dt = row["end_date"]
                prev_end = pd.Timestamp(
                    year=end_dt.year - 1, month=end_dt.month, day=end_dt.day
                )
                prev = grp[grp["end_date"] == prev_end]
                if not prev.empty:
                    prev_np = prev.iloc[0]["net_profit"]
                    if prev_np != 0 and pd.notna(prev_np):
                        yoy = row["net_profit"] / prev_np - 1
                        yoy_growths.append(yoy)

            # 至少需要 3 组同比增速才能算 CV
            if len(yoy_growths) >= 3:
                arr = np.array(yoy_growths)
                mean_val = np.mean(arr)
                std_val = np.std(arr, ddof=1)
                if abs(mean_val) > 1e-8:
                    cv = std_val / abs(mean_val)
                    results.append({"ts_code": ts_code, "factor_value": cv})

        if not results:
            return pd.DataFrame(columns=["ts_code", "factor_value"])

        return pd.DataFrame(results)


class MarginTrendFactor(FactorBase):
    """
    毛利率环比变化趋势。

    公式：当期毛利率 - 上一期毛利率（取最近已公告的两个报告期）。
    正值表示利润率在改善，负值表示恶化。
    作为正向因子使用（利润率改善更好）。
    """

    name = "MARGIN_TREND"
    description = "毛利率环比变化，利润率改善信号"

    def compute(self, date: str, universe: pd.DataFrame) -> pd.DataFrame:
        codes = universe["ts_code"].tolist()

        # 查询截止到 date 已公告的毛利率数据
        params: dict = {"date": date}
        sql = (
            "SELECT ts_code, ann_date, end_date, gross_margin FROM financial_data "
            "WHERE ann_date <= :date AND gross_margin IS NOT NULL"
        )
        if codes:
            in_clause, in_params = self._build_in_clause(codes)
            sql += f" AND ts_code IN {in_clause}"
            params.update(in_params)
        sql += " ORDER BY ts_code, end_date DESC"

        df = self.db.query(sql, params=params)

        if df.empty:
            return pd.DataFrame(columns=["ts_code", "factor_value"])

        df["end_date"] = pd.to_datetime(df["end_date"])
        df["gross_margin"] = pd.to_numeric(df["gross_margin"], errors="coerce")

        results = []
        for ts_code, grp in df.groupby("ts_code"):
            grp = grp.drop_duplicates(subset=["end_date"]).sort_values(
                "end_date", ascending=False
            )

            if len(grp) < 2:
                continue

            current_margin = grp.iloc[0]["gross_margin"]
            prev_margin = grp.iloc[1]["gross_margin"]

            if pd.notna(current_margin) and pd.notna(prev_margin):
                results.append({
                    "ts_code": ts_code,
                    "factor_value": current_margin - prev_margin,
                })

        if not results:
            return pd.DataFrame(columns=["ts_code", "factor_value"])

        return pd.DataFrame(results)
