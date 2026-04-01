"""
美股 Regime 驱动 Beta 控制策略

核心理念：不做选股 alpha，只做择时和风控。
收益来自市场本身（beta），价值来自少亏（熊市保护）。

设计：
    Regime 检测 → 目标仓位 → 质量筛选等权持仓 → 现金管理

评价标准：
    Calmar 比率、最大回撤、下行捕获率、上行捕获率
    不追求 FF5 alpha 或超额收益

Usage:
    strategy = USBetaStrategy(db)
    signals = strategy.generate_signals("2015-01-01", "2025-12-31")
"""

import logging
import time

import numpy as np
import pandas as pd

from services.config import (
    US_REBALANCE_INTERVAL,
    LOG_LEVEL,
)
from services.data.database import DatabaseManager
from services.data.us_cleaner import get_us_clean_universe
from services.strategy.us_regime import USRegimeDetector
from services.us_factors.base import USFactorBase

logger = logging.getLogger(__name__)
logger.setLevel(LOG_LEVEL)

# 仓位映射参数
_EQUITY_MAX = 0.90   # 强牛最大仓位
_EQUITY_MIN = 0.10   # 强熊最小仓位
_N_HOLDINGS = 30     # 持仓股票数


class USBetaStrategy:
    """
    Regime-driven beta control strategy.

    不追求选股 alpha，通过 Regime 感知动态调整仓位实现：
    - 牛市：高仓位吃 beta
    - 熊市：低仓位 + 现金保护
    """

    def __init__(self, db: DatabaseManager, n_holdings: int = _N_HOLDINGS):
        self.db = db
        self.regime = USRegimeDetector(db)
        self.n_holdings = n_holdings

    def get_target_allocation(self, date: str) -> dict:
        """
        根据 Regime 决定目标仓位。

        Returns:
            {"equity_pct": float, "cash_pct": float, "regime": dict}
        """
        regime = self.regime.detect(date)
        strength = regime["strength"]

        # 线性映射 strength [0, 1] → equity [MIN, MAX]
        equity_pct = _EQUITY_MIN + (_EQUITY_MAX - _EQUITY_MIN) * strength
        equity_pct = max(_EQUITY_MIN, min(_EQUITY_MAX, equity_pct))

        return {
            "equity_pct": equity_pct,
            "cash_pct": 1.0 - equity_pct,
            "regime": regime,
        }

    def select_holdings(self, date: str) -> pd.DataFrame:
        """
        选择持仓股票（质量筛选，非 alpha 追求）。

        用 Gross Profitability 过滤掉最差的 50%，从剩余中等权选 N 只。
        目的：获取 beta 暴露的同时避免垃圾股暴雷。

        Returns:
            DataFrame[ticker, weight]
        """
        universe = get_us_clean_universe(self.db, date)
        if universe.empty:
            logger.debug(f"select_holdings: {date} 股票池为空，返回空 DataFrame")
            return pd.DataFrame(columns=["ticker", "weight"])

        # 质量筛选：Gross Profitability = gross_margin * revenue / total_assets
        # 用已有的 gross_margin 字段近似（不需要完整的因子计算）
        gp = self._compute_gross_profitability(date, universe)

        if gp.empty or len(gp) < self.n_holdings:
            # 回退到等权全池
            selected = universe.head(self.n_holdings)
        else:
            # 保留 GP 前 50%
            top_half = gp.nlargest(len(gp) // 2, "gp_score")
            selected = top_half.head(self.n_holdings)

        n = len(selected)
        if n == 0:
            logger.debug(f"select_holdings: {date} 筛选后无股票，返回空 DataFrame")
            return pd.DataFrame(columns=["ticker", "weight"])

        result = selected[["ticker"]].copy()
        result["weight"] = 1.0 / n
        return result

    def generate_signals(
        self, start_date: str, end_date: str,
        task_id: str = None,
    ) -> dict[str, pd.DataFrame]:
        """
        生成月频信号。

        Returns:
            {date_str: DataFrame[ticker, weight]}
            weight 已经乘以 equity_pct（即 weight 总和 = equity_pct）
        """
        # 获取交易日
        trade_dates = self._get_trade_dates(start_date, end_date)
        if not trade_dates:
            logger.debug(f"generate_signals: {start_date}~{end_date} 无交易日，返回空信号")
            return {}

        # 月频调仓日
        rebalance_dates = trade_dates[::US_REBALANCE_INTERVAL]
        if not rebalance_dates:
            rebalance_dates = [trade_dates[0]]

        logger.info(f"US Beta strategy: {start_date} ~ {end_date}, {len(rebalance_dates)} rebalance dates")

        # 预加载数据
        USFactorBase.clear_all_cache()
        USFactorBase.preload_for_backtest(self.db, start_date, end_date)
        USFactorBase.precompute_rolling_stats()

        signals = {}
        for i, date in enumerate(rebalance_dates):
            # Regime → 仓位
            alloc = self.get_target_allocation(date)
            equity_pct = alloc["equity_pct"]
            regime = alloc["regime"]

            # 选股（质量筛选等权）
            holdings = self.select_holdings(date)
            if holdings.empty:
                logger.debug(f"generate_signals: {date} 持仓为空，跳过该调仓日")
                continue

            # 乘以 equity_pct（余下自动变现金）
            holdings["weight"] = holdings["weight"] * equity_pct

            signals[date] = holdings

            logger.info(
                f"Beta signal: {date}, equity={equity_pct:.0%}, "
                f"{len(holdings)} stocks, "
                f"regime={regime['strength']:.2f} "
                f"(trend={regime.get('trend', 0):.2f}, "
                f"vol={regime.get('vol', 0):.2f}, "
                f"credit={regime.get('credit', 0):.2f})"
            )

        USFactorBase.clear_date_cache()
        logger.info(f"US Beta signals done: {len(signals)} periods")
        return signals

    def _compute_gross_profitability(self, date: str, universe: pd.DataFrame) -> pd.DataFrame:
        """简化版 Gross Profitability（不走完整因子流水线）。"""
        bulk_fin = USFactorBase._static_cache.get("_bulk_financial")
        if bulk_fin is None or bulk_fin.empty:
            logger.debug("_compute_gross_profitability: 财务数据缓存为空，返回空 DataFrame")
            return pd.DataFrame(columns=["ticker", "gp_score"])

        tickers = universe["ticker"].tolist()
        date_ts = pd.to_datetime(date)

        df = bulk_fin[bulk_fin["filing_date"] <= date_ts].copy()
        df = df[df["ticker"].isin(tickers)]
        df = df.sort_values("date", ascending=False).drop_duplicates("ticker")

        for c in ["revenue", "gross_margin", "total_assets"]:
            df[c] = pd.to_numeric(df[c], errors="coerce")

        df["gp"] = df["revenue"] * df["gross_margin"] / 100
        df["gp_score"] = np.where(df["total_assets"] > 0, df["gp"] / df["total_assets"], np.nan)

        return df[["ticker", "gp_score"]].dropna()

    def _get_trade_dates(self, start_date: str, end_date: str) -> list[str]:
        """从 us_index_daily 获取交易日。"""
        df = self.db.query(
            "SELECT DISTINCT trade_date FROM us_index_daily "
            "WHERE index_code = '^GSPC' AND trade_date >= :start AND trade_date <= :end "
            "ORDER BY trade_date",
            params={"start": start_date, "end": end_date},
        )
        if df.empty:
            logger.debug(f"_get_trade_dates: {start_date}~{end_date} 无交易日数据，返回空列表")
            return []
        return [d.strftime("%Y-%m-%d") for d in pd.to_datetime(df["trade_date"])]
