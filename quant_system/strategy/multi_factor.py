"""
多因子打分选股模型

实现等权合成多因子得分的选股策略：
    1. 在每个调仓日，对股票池内所有股票计算各因子值
    2. 对每个因子做处理（去极值、中性化、标准化）
    3. 等权合成为综合得分
    4. 选取得分最高的 N 只股票，等权分配

调仓规则：
    - 频率：月频，每月最后一个交易日
    - 选股范围：可配置（默认全市场可交易股票）
    - 持仓数量：20~30 只
    - 权重分配：等权
"""

import logging
from typing import Optional

import pandas as pd
import numpy as np

from config.settings import (
    MIN_HOLDINGS,
    MAX_HOLDINGS,
    LOG_LEVEL,
)
from data.database import DatabaseManager
from data.cleaner import get_clean_universe
from factors.value import EPFactor, BPFactor
from factors.momentum import MOM1MFactor, MOM3MFactor, MOM12MFactor
from factors.quality import ROEFactor, GrossMarginFactor
from factors.technical import Turnover20DFactor
from factors.processor import process_factor

logger = logging.getLogger(__name__)
logger.setLevel(LOG_LEVEL)


class MultiFactorStrategy:
    """
    多因子选股策略。

    用法:
        db = DatabaseManager()
        strategy = MultiFactorStrategy(db)
        result = strategy.select_stocks("2024-12-31")
        signals = strategy.generate_signals("2023-01-01", "2024-12-31")
    """

    def __init__(
        self,
        db: DatabaseManager,
        n_holdings: int = MAX_HOLDINGS,
        factor_weights: Optional[dict[str, float]] = None,
    ):
        """
        Args:
            db: DatabaseManager 实例。
            n_holdings: 持仓数量。
            factor_weights: 因子权重字典 {因子名: 权重}。
                           为 None 则等权。权重越大越重要。
        """
        self.db = db
        self.n_holdings = n_holdings

        # 初始化因子实例
        self.factors = [
            EPFactor(db),
            BPFactor(db),
            MOM1MFactor(db),
            MOM3MFactor(db),
            MOM12MFactor(db),
            ROEFactor(db),
            GrossMarginFactor(db),
            Turnover20DFactor(db),
        ]

        # 因子权重（默认等权）
        if factor_weights is None:
            self.factor_weights = {f.name: 1.0 for f in self.factors}
        else:
            self.factor_weights = factor_weights

        # 换手率因子取反（低换手率更好，作为反向因子）
        if "TURN_20D" in self.factor_weights:
            self.factor_weights["TURN_20D"] = -abs(self.factor_weights["TURN_20D"])

    def select_stocks(self, date: str) -> pd.DataFrame:
        """
        在指定日期进行选股。

        流程：
            1. 构建当日可交易股票池
            2. 计算各因子值
            3. 因子处理（去极值 + 标准化）
            4. 等权合成综合得分
            5. 选取得分最高的 N 只股票

        Args:
            date: 选股日期，格式 YYYY-MM-DD。

        Returns:
            选中的股票 DataFrame，包含 ts_code, score, weight 列。
        """
        logger.info(f"选股: {date}")

        # 1. 构建股票池
        universe = get_clean_universe(self.db, date, min_turnover=0)
        if universe.empty:
            logger.warning(f"{date} 股票池为空")
            return pd.DataFrame()

        logger.info(f"股票池: {len(universe)} 只")

        # 2. 计算各因子值
        factor_scores = {}
        for factor in self.factors:
            try:
                df_factor = factor.compute(date, universe)
                if not df_factor.empty:
                    factor_scores[factor.name] = df_factor
                    valid_count = df_factor["factor_value"].notna().sum()
                    logger.debug(f"  {factor.name}: {valid_count} 个有效值")
            except Exception as e:
                logger.warning(f"因子 {factor.name} 计算失败: {e}")

        if not factor_scores:
            logger.warning(f"{date} 所有因子计算失败")
            return pd.DataFrame()

        # 3. 因子处理 + 合成
        # 获取行业和市值数据（用于中性化）
        industry_df = None
        mktcap_df = None
        try:
            industry_df = self.db.get_industry_map()
        except Exception:
            pass
        try:
            # 本地计算市值：close × total_share（万股）→ 总市值（万元）
            # 取当日收盘价
            lookback = (pd.to_datetime(date) - pd.Timedelta(days=10)).strftime("%Y-%m-%d")
            df_close = self.db.query(
                f"SELECT ts_code, trade_date, close FROM daily_price "
                f"WHERE trade_date >= '{lookback}' AND trade_date <= '{date}' "
                f"ORDER BY ts_code, trade_date DESC"
            )
            if not df_close.empty:
                df_close = df_close.drop_duplicates(subset=["ts_code"], keep="first")
            # 取总股本
            df_share = self.db.query(
                "SELECT ts_code, total_share FROM stock_basic WHERE total_share IS NOT NULL"
            )
            if not df_close.empty and not df_share.empty:
                mktcap_df = df_close[["ts_code", "close"]].merge(
                    df_share, on="ts_code", how="inner"
                )
                # total_mv 单位：万元（close × total_share(万股) × 10000(股) / 10000(→万元) = close × total_share）
                mktcap_df["total_mv"] = mktcap_df["close"] * mktcap_df["total_share"]
                mktcap_df = mktcap_df[["ts_code", "total_mv"]]
        except Exception:
            pass

        # 处理并合并所有因子
        all_codes = universe["ts_code"].tolist()
        composite = pd.DataFrame({"ts_code": all_codes})

        for fname, df_raw in factor_scores.items():
            processed = process_factor(
                df_raw,
                industry_df=industry_df,
                mktcap_df=mktcap_df,
                do_neutralize=(industry_df is not None and mktcap_df is not None),
            )
            # 合并该因子得分
            weight = self.factor_weights.get(fname, 1.0)
            processed = processed.rename(columns={"factor_value": fname})
            processed[fname] = processed[fname] * weight
            composite = composite.merge(processed, on="ts_code", how="left")

        # 4. 等权合成：各因子得分取均值
        factor_cols = [c for c in composite.columns if c != "ts_code"]
        composite["score"] = composite[factor_cols].mean(axis=1)

        # 过滤掉因子值全缺失的股票
        composite = composite.dropna(subset=["score"])

        # 排除涨停股（不可买入）
        limit_up_codes = universe[universe["is_limit_up"] == 1]["ts_code"].tolist()
        composite = composite[~composite["ts_code"].isin(limit_up_codes)]

        # 5. 选取得分最高的 N 只
        composite = composite.sort_values("score", ascending=False)
        selected = composite.head(self.n_holdings).copy()

        # 等权分配
        if len(selected) > 0:
            selected["weight"] = 1.0 / len(selected)

        logger.info(
            f"选股完成: {len(selected)} 只, "
            f"得分范围 [{selected['score'].min():.3f}, {selected['score'].max():.3f}]"
        )

        return selected[["ts_code", "score", "weight"]]

    def get_rebalance_dates(self, start_date: str, end_date: str) -> list[str]:
        """
        获取调仓日期列表（每月最后一个交易日）。

        Args:
            start_date: 起始日期。
            end_date: 结束日期。

        Returns:
            调仓日期列表。
        """
        df = self.db.query(
            f"SELECT DISTINCT trade_date FROM daily_price "
            f"WHERE trade_date >= '{start_date}' "
            f"AND trade_date <= '{end_date}' "
            f"ORDER BY trade_date"
        )

        if df.empty:
            return []

        df["trade_date"] = pd.to_datetime(df["trade_date"])
        df["year_month"] = df["trade_date"].dt.to_period("M")

        # 每月最后一个交易日
        month_end = df.groupby("year_month")["trade_date"].max()
        dates = [d.strftime("%Y-%m-%d") for d in month_end]

        return dates

    def generate_signals(
        self, start_date: str, end_date: str
    ) -> dict[str, pd.DataFrame]:
        """
        生成回测区间内所有调仓日的选股信号。

        Args:
            start_date: 回测起始日期。
            end_date: 回测结束日期。

        Returns:
            字典 {调仓日期: 选股结果 DataFrame}。
        """
        rebalance_dates = self.get_rebalance_dates(start_date, end_date)
        logger.info(f"回测区间: {start_date} ~ {end_date}, {len(rebalance_dates)} 个调仓日")

        signals = {}
        for dt in rebalance_dates:
            try:
                result = self.select_stocks(dt)
                if not result.empty:
                    signals[dt] = result
            except Exception as e:
                logger.warning(f"{dt} 选股失败: {e}")

        logger.info(f"信号生成完成: {len(signals)} 期有效信号")
        return signals
