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

from backend.services.factors.base import FactorBase


class Turnover20DFactor(FactorBase):
    """
    过去 20 个交易日平均换手率。

    使用日线行情中的 turnover_rate 字段，取最近 20 个交易日的均值。
    """

    name = "TURN_20D"
    description = "过去20日平均换手率，流动性代理"

    def compute(self, date: str, universe: pd.DataFrame) -> pd.DataFrame:
        codes = universe["ts_code"].tolist()

        # 取约30个自然日确保覆盖20个交易日
        df_price = self.get_price_history(
            date, lookback_days=45,
            universe_codes=codes,
            columns=["turnover_rate"],
        )

        if df_price.empty:
            return pd.DataFrame(columns=["ts_code", "factor_value"])

        # 每只股票取最近20个交易日
        df_price["trade_date"] = pd.to_datetime(df_price["trade_date"])
        df_price = df_price.sort_values(["ts_code", "trade_date"])

        df_recent = (
            df_price.groupby("ts_code")
            .tail(20)
        )

        df_avg = (
            df_recent.groupby("ts_code")["turnover_rate"]
            .mean()
            .reset_index()
        )
        df_avg.columns = ["ts_code", "factor_value"]

        return df_avg


class VolatilityFactor(FactorBase):
    """
    过去 20 个交易日日收益率的标准差。

    低波动股票在下行市场中更具防御性。
    作为反向因子使用（低波动更好）。
    """

    name = "VOL_20D"
    description = "过去20日收益率波动率，低波动更好（反向因子）"

    def compute(self, date: str, universe: pd.DataFrame) -> pd.DataFrame:
        codes = universe["ts_code"].tolist()

        df_price = self.get_price_history(
            date, lookback_days=45,
            universe_codes=codes,
            columns=["pct_chg"],
        )

        if df_price.empty:
            return pd.DataFrame(columns=["ts_code", "factor_value"])

        df_price["trade_date"] = pd.to_datetime(df_price["trade_date"])
        df_price = df_price.sort_values(["ts_code", "trade_date"])

        df_recent = (
            df_price.groupby("ts_code")
            .tail(20)
        )

        # pct_chg 是百分比形式，除以 100 转为小数再算 std
        df_recent = df_recent.copy()
        df_recent["ret"] = pd.to_numeric(df_recent["pct_chg"], errors="coerce") / 100

        df_vol = (
            df_recent.groupby("ts_code")["ret"]
            .std()
            .reset_index()
        )
        df_vol.columns = ["ts_code", "factor_value"]

        return df_vol


class PriceDeviationFactor(FactorBase):
    """
    当前价格偏离 60 日均线的幅度。

    公式：(close - MA60) / MA60
    偏离越低说明股价越接近均线支撑位，安全边际更高。
    作为反向因子使用（低偏离更好）。
    """

    name = "PRICE_DEV_60D"
    description = "价格偏离60日均线幅度，低偏离安全边际高（反向因子）"

    def compute(self, date: str, universe: pd.DataFrame) -> pd.DataFrame:
        codes = universe["ts_code"].tolist()

        df_price = self.get_price_history(
            date, lookback_days=120,
            universe_codes=codes,
            columns=["close", "adj_factor"],
        )

        if df_price.empty:
            return pd.DataFrame(columns=["ts_code", "factor_value"])

        df_price["trade_date"] = pd.to_datetime(df_price["trade_date"])
        df_price["close"] = pd.to_numeric(df_price["close"], errors="coerce")
        # 前复权价格
        df_price["adj_factor"] = pd.to_numeric(df_price["adj_factor"], errors="coerce").fillna(1.0)
        df_price["adj_close"] = df_price["close"] * df_price["adj_factor"]
        df_price = df_price.sort_values(["ts_code", "trade_date"])

        # 每只股票取最近 60 个交易日
        df_recent = (
            df_price.groupby("ts_code")
            .tail(60)
        )

        # MA60（使用前复权价格）
        df_ma60 = (
            df_recent.groupby("ts_code")["adj_close"]
            .mean()
            .reset_index()
        )
        df_ma60.columns = ["ts_code", "ma60"]

        # 最新前复权收盘价（每组最后一条）
        df_latest = (
            df_recent.groupby("ts_code")
            .tail(1)[["ts_code", "adj_close"]]
            .copy()
        )

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
        if codes:
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

        df_price["trade_date"] = pd.to_datetime(df_price["trade_date"])
        df_price["pct_chg"] = pd.to_numeric(df_price["pct_chg"], errors="coerce")
        df_price["volume"] = pd.to_numeric(df_price["volume"], errors="coerce")
        df_price = df_price.sort_values(["ts_code", "trade_date"])

        # 每只股票取最近 20 个交易日
        df_recent = df_price.groupby("ts_code").tail(20).copy()

        def _divergence(g):
            g = g.dropna(subset=["pct_chg", "volume"])
            if len(g) < 10:
                return np.nan
            # 价格趋势：20D 累计收益
            price_trend = (1 + g["pct_chg"] / 100).prod() - 1
            # 量能趋势：OLS 斜率（volume ~ t）
            t = np.arange(len(g), dtype=float)
            vol = g["volume"].values.astype(float)
            t_mean = t.mean()
            vol_mean = vol.mean()
            if vol_mean == 0:
                return np.nan
            slope = ((t - t_mean) * (vol - vol_mean)).sum() / ((t - t_mean) ** 2).sum()
            # 标准化斜率（除以均值使不同股票可比）
            norm_slope = slope / vol_mean
            # 方向不一致时产生正的背离信号
            if np.sign(price_trend) != np.sign(norm_slope):
                return abs(price_trend)
            else:
                return 0.0

        df_div = (
            df_recent.groupby("ts_code")
            .apply(_divergence, include_groups=False)
            .reset_index()
        )
        df_div.columns = ["ts_code", "factor_value"]

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

        # 获取行业映射
        try:
            df_industry = self.get_industry_map_cached()
        except Exception:
            return pd.DataFrame(columns=["ts_code", "factor_value"])

        if df_industry.empty:
            return pd.DataFrame(columns=["ts_code", "factor_value"])

        # 取近 20 个交易日的 pct_chg
        df_price = self.get_price_history(
            date, lookback_days=45,
            universe_codes=codes,
            columns=["pct_chg"],
        )

        if df_price.empty:
            return pd.DataFrame(columns=["ts_code", "factor_value"])

        df_price["trade_date"] = pd.to_datetime(df_price["trade_date"])
        df_price["pct_chg"] = pd.to_numeric(df_price["pct_chg"], errors="coerce")
        df_price = df_price.sort_values(["ts_code", "trade_date"])

        # 每只股票取最近 20 个交易日
        df_recent = (
            df_price.groupby("ts_code")
            .tail(20)
        )

        # 计算每只股票的 20 日累计收益率
        df_cum_ret = (
            df_recent.groupby("ts_code")
            .apply(lambda g: (1 + g["pct_chg"] / 100).prod() - 1, include_groups=False)
            .reset_index()
        )
        df_cum_ret.columns = ["ts_code", "cum_ret"]

        # 合并行业信息
        df_merged = df_cum_ret.merge(df_industry, on="ts_code", how="left")
        df_merged = df_merged.dropna(subset=["industry_name"])

        # 按行业计算平均累计收益
        df_ind_mom = (
            df_merged.groupby("industry_name")["cum_ret"]
            .mean()
            .reset_index()
        )
        df_ind_mom.columns = ["industry_name", "ind_mom"]

        # 将行业动量赋值给每只股票
        df_result = df_merged[["ts_code", "industry_name"]].merge(
            df_ind_mom, on="industry_name", how="left"
        )
        df_result["factor_value"] = df_result["ind_mom"]

        return df_result[["ts_code", "factor_value"]]
