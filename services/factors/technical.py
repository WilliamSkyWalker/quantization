"""
技术因子

实现基于交易数据的技术因子：
    - TURN_20D: 过去 20 个交易日平均换手率（流动性代理变量）
    - VOL_20D: 过去 20 个交易日日收益率标准差（波动率）
    - PRICE_DEV_60D: 当前价格偏离 60 日均线的幅度
    - SIZE: 对数流通市值
    - IND_MOM: 所属行业近 20 日平均涨跌幅（行业动量）

换手率越高通常意味着流动性越好，但过高可能暗示投机行为。
在多因子模型中，换手率因子常被用作流动性控制变量。
"""

import numpy as np
import pandas as pd

from services.factors.base import FactorBase


class Turnover20DFactor(FactorBase):
    """过去 20 个交易日平均换手率。"""

    name = "TURN_20D"
    description = "过去20日平均换手率，流动性代理"

    def compute(self, date: str, universe: pd.DataFrame) -> pd.DataFrame:
        codes_set = set(universe["ts_code"].tolist())

        # 快速路径：预计算的 rolling 换手率
        day = self._get_rolling_for_date(date, codes_set)
        if day is not None and not day.empty and "turn_20d" in day.columns:
            df = day[["turn_20d"]].reset_index()
            df.columns = ["ts_code", "factor_value"]
            return df.dropna(subset=["factor_value"])

        # 回退到原始逻辑
        codes = list(codes_set)
        df_price = self.get_price_history(
            date, lookback_days=45, universe_codes=codes, columns=["turnover_rate"],
        )
        if df_price.empty:
            return pd.DataFrame(columns=["ts_code", "factor_value"])
        df_price["trade_date"] = pd.to_datetime(df_price["trade_date"])
        df_price = df_price.sort_values(["ts_code", "trade_date"])
        df_recent = df_price.groupby("ts_code").tail(20)
        df_avg = df_recent.groupby("ts_code")["turnover_rate"].mean().reset_index()
        df_avg.columns = ["ts_code", "factor_value"]
        return df_avg


class VolatilityFactor(FactorBase):
    """过去 20 个交易日日收益率的标准差（反向因子）。"""

    name = "VOL_20D"
    description = "过去20日收益率波动率，低波动更好（反向因子）"

    def compute(self, date: str, universe: pd.DataFrame) -> pd.DataFrame:
        codes_set = set(universe["ts_code"].tolist())

        # 快速路径
        day = self._get_rolling_for_date(date, codes_set)
        if day is not None and not day.empty and "vol_20d" in day.columns:
            df = day[["vol_20d"]].reset_index()
            df.columns = ["ts_code", "factor_value"]
            return df.dropna(subset=["factor_value"])

        # 回退
        codes = list(codes_set)
        df_price = self.get_price_history(
            date, lookback_days=45, universe_codes=codes, columns=["pct_chg"],
        )
        if df_price.empty:
            return pd.DataFrame(columns=["ts_code", "factor_value"])
        df_price["trade_date"] = pd.to_datetime(df_price["trade_date"])
        df_price = df_price.sort_values(["ts_code", "trade_date"])
        df_recent = df_price.groupby("ts_code").tail(20).copy()
        df_recent["ret"] = pd.to_numeric(df_recent["pct_chg"], errors="coerce") / 100
        df_vol = df_recent.groupby("ts_code")["ret"].std().reset_index()
        df_vol.columns = ["ts_code", "factor_value"]
        return df_vol


class PriceDeviationFactor(FactorBase):
    """价格偏离 60 日均线幅度（反向因子）。"""

    name = "PRICE_DEV_60D"
    description = "价格偏离60日均线幅度，低偏离安全边际高（反向因子）"

    def compute(self, date: str, universe: pd.DataFrame) -> pd.DataFrame:
        codes_set = set(universe["ts_code"].tolist())

        # 快速路径：预计算的 MA60 和当日 adj_close
        day = self._get_rolling_for_date(date, codes_set)
        if day is not None and not day.empty and "ma60_adj" in day.columns:
            df = day[["adj_close", "ma60_adj"]].reset_index()
            df.columns = ["ts_code", "adj_close", "ma60"]
            df["factor_value"] = np.where(
                (df["ma60"].notna()) & (df["ma60"] > 0),
                (df["adj_close"] - df["ma60"]) / df["ma60"],
                np.nan,
            )
            return df[["ts_code", "factor_value"]].dropna(subset=["factor_value"])

        # 回退
        codes = list(codes_set)
        df_price = self.get_price_history(
            date, lookback_days=120, universe_codes=codes, columns=["close", "adj_factor"],
        )
        if df_price.empty:
            return pd.DataFrame(columns=["ts_code", "factor_value"])
        df_price["trade_date"] = pd.to_datetime(df_price["trade_date"])
        df_price["close"] = pd.to_numeric(df_price["close"], errors="coerce")
        df_price["adj_factor"] = pd.to_numeric(df_price["adj_factor"], errors="coerce").fillna(1.0)
        df_price["adj_close"] = df_price["close"] * df_price["adj_factor"]
        df_price = df_price.sort_values(["ts_code", "trade_date"])
        df_recent = df_price.groupby("ts_code").tail(60)
        df_ma60 = df_recent.groupby("ts_code")["adj_close"].mean().reset_index()
        df_ma60.columns = ["ts_code", "ma60"]
        df_latest = df_recent.groupby("ts_code").tail(1)[["ts_code", "adj_close"]].copy()
        df_dev = df_latest.merge(df_ma60, on="ts_code")
        df_dev["factor_value"] = np.where(
            (df_dev["ma60"].notna()) & (df_dev["ma60"] > 0),
            (df_dev["adj_close"] - df_dev["ma60"]) / df_dev["ma60"],
            np.nan,
        )
        return df_dev[["ts_code", "factor_value"]]


class SizeFactor(FactorBase):
    """
    对数流通市值因子。

    公式：ln(close × float_share × 10000)
    float_share 单位为万股，乘以 10000 转为股，再乘以 close 得到流通市值（元）。
    偏向中大盘股，降低与沪深300的跟踪偏离。
    """

    name = "SIZE"
    description = "对数流通市值，偏向中大盘"

    def compute(self, date: str, universe: pd.DataFrame) -> pd.DataFrame:
        codes = universe["ts_code"].tolist()

        # 获取收盘价
        df_close = self.get_close_on_date(date, codes)
        if df_close.empty:
            return pd.DataFrame(columns=["ts_code", "factor_value"])

        # 获取流通股本
        params: dict = {}
        sql = "SELECT ts_code, float_share FROM stock_basic WHERE float_share IS NOT NULL"
        if codes and len(codes) <= self._IN_CLAUSE_THRESHOLD:
            in_clause, in_params = self._build_in_clause(codes)
            sql += f" AND ts_code IN {in_clause}"
            params.update(in_params)
        df_share = self.db.query(sql, params=params)

        if df_share.empty:
            return pd.DataFrame(columns=["ts_code", "factor_value"])

        df = df_close.merge(df_share, on="ts_code", how="inner")
        df["close"] = pd.to_numeric(df["close"], errors="coerce")
        df["float_share"] = pd.to_numeric(df["float_share"], errors="coerce")

        # 流通市值（元）= close × float_share(万股) × 10000(股/万股)
        df["float_mv"] = df["close"] * df["float_share"] * 10000
        df["factor_value"] = np.where(
            df["float_mv"] > 0,
            np.log(df["float_mv"]),
            np.nan,
        )

        return df[["ts_code", "factor_value"]]


class VolPriceDivFactor(FactorBase):
    """
    量价背离因子：检测价格趋势与成交量趋势的方向不一致。

    计算逻辑：
        1. 20D 累计收益 → 价格趋势方向
        2. 20D 成交量 OLS 斜率 → 量能趋势方向
        3. divergence = |price_trend| × sign_mismatch
           当价格方向与量能方向不一致时 divergence > 0

    值越大 = 量价背离越严重 = 反转信号越强（正向因子）。
    """

    name = "VOL_PRICE_DIV"
    description = "量价背离，趋势背离检测（正向因子）"

    def compute(self, date: str, universe: pd.DataFrame) -> pd.DataFrame:
        codes = universe["ts_code"].tolist()

        df_price = self.get_price_history(
            date, lookback_days=45,
            universe_codes=codes,
            columns=["pct_chg", "volume"],
        )

        if df_price.empty:
            return pd.DataFrame(columns=["ts_code", "factor_value"])

        df_price["pct_chg"] = pd.to_numeric(df_price["pct_chg"], errors="coerce")
        df_price["volume"] = pd.to_numeric(df_price["volume"], errors="coerce")
        df_price["trade_date"] = pd.to_datetime(df_price["trade_date"])
        df_price = df_price.sort_values(["ts_code", "trade_date"])

        # 每只股票取最近 20 个交易日
        df_recent = df_price.groupby("ts_code").tail(20).copy()

        # 向量化计算量价背离（避免逐股票 apply）
        # 1. 价格趋势：20D 累计收益
        df_recent["_ret"] = 1 + df_recent["pct_chg"] / 100
        price_trend = df_recent.groupby("ts_code")["_ret"].prod() - 1

        # 2. 量能趋势 OLS 斜率：slope = cov(t, vol) / var(t)
        #    在每组内 t = 0,1,...,n-1，用组内排名作为 t
        df_recent["_rank"] = df_recent.groupby("ts_code").cumcount()
        grp_size = df_recent.groupby("ts_code")["_rank"].transform("count")
        # t_mean = (n-1)/2, var(t) = n*(n-1)/12 (for 0..n-1)
        t_mean = (grp_size - 1) / 2.0
        df_recent["_t_dev"] = df_recent["_rank"] - t_mean
        vol_mean = df_recent.groupby("ts_code")["volume"].transform("mean")
        df_recent["_vol_dev"] = df_recent["volume"] - vol_mean

        cov_tv = (df_recent["_t_dev"] * df_recent["_vol_dev"]).groupby(df_recent["ts_code"]).sum()
        var_t = (df_recent["_t_dev"] ** 2).groupby(df_recent["ts_code"]).sum()

        # 标准化斜率
        vol_mean_per_stock = df_recent.groupby("ts_code")["volume"].mean()
        n_per_stock = df_recent.groupby("ts_code").size()

        slope = cov_tv / var_t.replace(0, np.nan)
        norm_slope = slope / vol_mean_per_stock.replace(0, np.nan)

        # 方向不一致时产生正的背离信号
        sign_mismatch = np.sign(price_trend) != np.sign(norm_slope)
        divergence = np.where(sign_mismatch, price_trend.abs(), 0.0)
        # 数据不足（<10 个交易日）的标记为 NaN
        divergence = np.where(n_per_stock >= 10, divergence, np.nan)

        df_div = pd.DataFrame({
            "ts_code": price_trend.index,
            "factor_value": divergence,
        })

        return df_div


class IndustryMomentumFactor(FactorBase):
    """
    行业动量因子：所属行业近 20 个交易日的平均累计涨跌幅。

    计算逻辑：
        1. 取股票池所有股票的近 20 日 pct_chg
        2. 按行业分组，计算行业内所有股票的平均累计收益
        3. 将行业收益赋值给该行业的每只股票
    利用 A 股行业轮动效应，优先选择处于上升通道的行业。
    """

    name = "IND_MOM"
    description = "行业近20日平均涨跌幅，利用行业轮动效应"

    def compute(self, date: str, universe: pd.DataFrame) -> pd.DataFrame:
        codes = universe["ts_code"].tolist()
        codes_set = set(codes)

        try:
            df_industry = self.get_industry_map_cached()
        except Exception:
            return pd.DataFrame(columns=["ts_code", "factor_value"])
        if df_industry.empty:
            return pd.DataFrame(columns=["ts_code", "factor_value"])

        # 快速路径：使用预计算 20 日累计收益
        day = self._get_rolling_for_date(date, codes_set)
        if day is not None and not day.empty and "cum_ret_20d" in day.columns:
            df_cum_ret = day[["cum_ret_20d"]].reset_index()
            df_cum_ret.columns = ["ts_code", "cum_ret"]
            df_cum_ret = df_cum_ret.dropna(subset=["cum_ret"])
        else:
            # 回退到原始逻辑
            df_price = self.get_price_history(
                date, lookback_days=45, universe_codes=codes, columns=["pct_chg"],
            )
            if df_price.empty:
                return pd.DataFrame(columns=["ts_code", "factor_value"])
            df_price["trade_date"] = pd.to_datetime(df_price["trade_date"])
            df_price["pct_chg"] = pd.to_numeric(df_price["pct_chg"], errors="coerce")
            df_price = df_price.sort_values(["ts_code", "trade_date"])
            df_recent = df_price.groupby("ts_code").tail(20)
            df_cum_ret = (
                df_recent.groupby("ts_code")
                .apply(lambda g: (1 + g["pct_chg"] / 100).prod() - 1, include_groups=False)
                .reset_index()
            )
            df_cum_ret.columns = ["ts_code", "cum_ret"]

        df_merged = df_cum_ret.merge(df_industry, on="ts_code", how="left")
        df_merged = df_merged.dropna(subset=["industry_name"])
        df_ind_mom = df_merged.groupby("industry_name")["cum_ret"].mean().reset_index()
        df_ind_mom.columns = ["industry_name", "ind_mom"]
        df_result = df_merged[["ts_code", "industry_name"]].merge(
            df_ind_mom, on="industry_name", how="left"
        )
        df_result["factor_value"] = df_result["ind_mom"]
        return df_result[["ts_code", "factor_value"]]
