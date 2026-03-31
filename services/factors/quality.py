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

from services.factors.base import FactorBase


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

        # 预加载数据可用时，从内存过滤
        bulk_fin = self._static_cache.get("_bulk_financial")
        if bulk_fin is not None and not bulk_fin.empty:
            date_ts = pd.to_datetime(date)
            df = bulk_fin[["ts_code", "ann_date", "end_date", "net_profit"]].copy()
            df = df[(df["ann_date"] <= date_ts) & df["net_profit"].notna()]
            if codes:
                df = df[df["ts_code"].isin(codes)]
            df = df.sort_values(["ts_code", "end_date"], ascending=[True, False])
        else:
            params: dict = {"date": date}
            sql = (
                "SELECT ts_code, ann_date, end_date, net_profit FROM financial_data "
                "WHERE ann_date <= :date AND net_profit IS NOT NULL"
            )
            if codes and len(codes) <= self._IN_CLAUSE_THRESHOLD:
                in_clause, in_params = self._build_in_clause(codes)
                sql += f" AND ts_code IN {in_clause}"
                params.update(in_params)
            sql += " ORDER BY ts_code, end_date DESC"
            df = self.db.query(sql, params=params)

        if df.empty:
            return pd.DataFrame(columns=["ts_code", "factor_value"])

        df["end_date"] = pd.to_datetime(df["end_date"])
        df["net_profit"] = pd.to_numeric(df["net_profit"], errors="coerce")

        # 去重并排序，生成组内排名
        df = df.drop_duplicates(subset=["ts_code", "end_date"])
        df = df.sort_values(["ts_code", "end_date"], ascending=[True, False])
        df["rank"] = df.groupby("ts_code").cumcount()

        # 取每只股票最近 4 个报告期
        top4 = df[df["rank"] < 4].copy()

        # 向量化构建上年同期 end_date
        top4["prev_end"] = pd.to_datetime(dict(
            year=top4["end_date"].dt.year - 1,
            month=top4["end_date"].dt.month,
            day=top4["end_date"].dt.day,
        ))

        # 构建 lookup：(ts_code, end_date) → net_profit
        lookup = df[["ts_code", "end_date", "net_profit"]].drop_duplicates(
            subset=["ts_code", "end_date"]
        )

        # merge 获取上年同期净利润
        merged = top4.merge(
            lookup.rename(columns={"end_date": "prev_end", "net_profit": "prev_np"}),
            on=["ts_code", "prev_end"],
            how="left",
        )

        # 计算同比增速
        merged["yoy"] = np.where(
            (merged["prev_np"].notna()) & (merged["prev_np"] != 0),
            merged["net_profit"] / merged["prev_np"] - 1,
            np.nan,
        )

        # 丢弃无效 yoy，按股票汇总统计
        valid = merged.dropna(subset=["yoy"])
        stats = valid.groupby("ts_code")["yoy"].agg(["mean", "std", "count"]).reset_index()

        # 至少 3 组同比增速才能算 CV
        stats = stats[stats["count"] >= 3].copy()

        # CV = std / |mean|
        stats["factor_value"] = np.where(
            stats["mean"].abs() > 1e-8,
            stats["std"] / stats["mean"].abs(),
            np.nan,
        )

        result = stats[["ts_code", "factor_value"]].dropna(subset=["factor_value"])
        if codes:
            result = result[result["ts_code"].isin(codes)]

        return result.reset_index(drop=True)


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

        # 预加载数据可用时，从内存过滤
        bulk_fin = self._static_cache.get("_bulk_financial")
        if bulk_fin is not None and not bulk_fin.empty:
            date_ts = pd.to_datetime(date)
            df = bulk_fin[["ts_code", "ann_date", "end_date", "gross_margin"]].copy()
            df = df[(df["ann_date"] <= date_ts) & df["gross_margin"].notna()]
            if codes:
                df = df[df["ts_code"].isin(codes)]
            df = df.sort_values(["ts_code", "end_date"], ascending=[True, False])
        else:
            params: dict = {"date": date}
            sql = (
                "SELECT ts_code, ann_date, end_date, gross_margin FROM financial_data "
                "WHERE ann_date <= :date AND gross_margin IS NOT NULL"
            )
            if codes and len(codes) <= self._IN_CLAUSE_THRESHOLD:
                in_clause, in_params = self._build_in_clause(codes)
                sql += f" AND ts_code IN {in_clause}"
                params.update(in_params)
            sql += " ORDER BY ts_code, end_date DESC"
            df = self.db.query(sql, params=params)

        if df.empty:
            return pd.DataFrame(columns=["ts_code", "factor_value"])

        df["end_date"] = pd.to_datetime(df["end_date"])
        df["gross_margin"] = pd.to_numeric(df["gross_margin"], errors="coerce")

        # 去重、排序、生成组内排名
        df = df.drop_duplicates(subset=["ts_code", "end_date"])
        df = df.sort_values(["ts_code", "end_date"], ascending=[True, False])
        df["rank"] = df.groupby("ts_code").cumcount()

        # 取 rank 0（当期）和 rank 1（上期）
        current = df[df["rank"] == 0][["ts_code", "gross_margin"]].rename(
            columns={"gross_margin": "current_margin"}
        )
        prev = df[df["rank"] == 1][["ts_code", "gross_margin"]].rename(
            columns={"gross_margin": "prev_margin"}
        )

        merged = current.merge(prev, on="ts_code", how="inner")
        merged["factor_value"] = np.where(
            merged["current_margin"].notna() & merged["prev_margin"].notna(),
            merged["current_margin"] - merged["prev_margin"],
            np.nan,
        )

        result = merged[["ts_code", "factor_value"]].dropna(subset=["factor_value"])
        if codes:
            result = result[result["ts_code"].isin(codes)]

        return result.reset_index(drop=True)
