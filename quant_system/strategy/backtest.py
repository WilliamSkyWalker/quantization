"""
轻量回测引擎

基于 pandas 实现的事件驱动回测框架：
    - 输入：每期选股信号（调仓日 → 目标持仓和权重）
    - 处理：交易成本、涨跌停限制、滑点
    - 输出：净值曲线、绩效指标、交易记录

交易成本假设：
    - 买入佣金：0.075%
    - 卖出佣金：0.075% + 印花税 0.1%
    - 滑点：0.1%

涨跌停规则：
    - 涨停不可买入
    - 跌停不可卖出
"""

import logging
from typing import Optional

import numpy as np
import pandas as pd
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

from config.settings import (
    BUY_COMMISSION,
    SELL_COMMISSION,
    STAMP_TAX,
    SLIPPAGE,
    LOG_LEVEL,
    PROJECT_ROOT,
)
from data.database import DatabaseManager

logger = logging.getLogger(__name__)
logger.setLevel(LOG_LEVEL)

plt.rcParams["font.sans-serif"] = ["SimHei", "Arial Unicode MS", "DejaVu Sans"]
plt.rcParams["axes.unicode_minus"] = False


class BacktestEngine:
    """
    回测引擎。

    用法:
        engine = BacktestEngine(db)
        result = engine.run(signals, start_date, end_date)
        engine.plot(result)
        print(engine.summary(result))
    """

    def __init__(
        self,
        db: DatabaseManager,
        buy_cost: float = BUY_COMMISSION + SLIPPAGE,
        sell_cost: float = SELL_COMMISSION + STAMP_TAX + SLIPPAGE,
        benchmark: str = "000300",
    ):
        """
        Args:
            db: DatabaseManager 实例。
            buy_cost: 买入总成本比例（佣金+滑点）。
            sell_cost: 卖出总成本比例（佣金+印花税+滑点）。
            benchmark: 基准指数代码（默认沪深300）。
        """
        self.db = db
        self.buy_cost = buy_cost
        self.sell_cost = sell_cost
        self.benchmark = benchmark

    def run(
        self,
        signals: dict[str, pd.DataFrame],
        start_date: str,
        end_date: str,
    ) -> dict:
        """
        执行回测。

        信号延迟执行（T+1）：
            - T 日收盘后产生信号
            - T+1 日开盘执行调仓，当日收益计入新组合

        Args:
            signals: {调仓日期: DataFrame[ts_code, weight]} 字典。
            start_date: 回测起始日期。
            end_date: 回测结束日期。

        Returns:
            回测结果字典，包含：
                - nav: 策略净值 Series（DatetimeIndex）
                - benchmark_nav: 基准净值 Series
                - trades: 交易记录 DataFrame
                - holdings: 每日持仓 DataFrame
                - turnover: 每期换手率 Series
        """
        logger.info(f"开始回测: {start_date} ~ {end_date}")

        # 获取全部交易日
        trade_dates = self._get_trade_dates(start_date, end_date)
        if trade_dates.empty:
            logger.error("无交易日数据")
            return {}

        # 排序信号日期
        signal_dates = sorted(signals.keys())

        # 初始化
        nav = pd.Series(dtype=float)
        portfolio_value = 1.0
        current_holdings = {}  # {ts_code: weight}
        pending_signal = None   # T日产生信号，T+1日执行
        trades = []
        turnover_list = []

        # 获取所有相关股票的日收益率
        all_codes = set()
        for df_sig in signals.values():
            all_codes.update(df_sig["ts_code"].tolist())

        price_cache = self._load_prices(list(all_codes), start_date, end_date)

        # 逐日循环
        signal_idx = 0

        for i, today in enumerate(trade_dates):
            today_str = today.strftime("%Y-%m-%d")

            # T+1 执行：如果昨天产生了待执行信号，今天开盘执行
            if pending_signal is not None:
                target_holdings = pending_signal

                # 用 T+1（今天）的涨跌停状态做限制
                target_holdings = self._apply_limit_constraints(
                    current_holdings, target_holdings, today_str
                )

                # 计算换手率和交易成本
                turnover, cost = self._calc_trade_cost(
                    current_holdings, target_holdings
                )
                portfolio_value *= (1 - cost)
                turnover_list.append({
                    "date": today_str,
                    "turnover": turnover,
                    "cost": cost,
                })

                # 记录交易
                for code in set(list(current_holdings.keys()) + list(target_holdings.keys())):
                    old_w = current_holdings.get(code, 0)
                    new_w = target_holdings.get(code, 0)
                    if old_w != new_w:
                        trades.append({
                            "date": today_str,
                            "ts_code": code,
                            "old_weight": old_w,
                            "new_weight": new_w,
                            "direction": "BUY" if new_w > old_w else "SELL",
                        })

                current_holdings = target_holdings
                pending_signal = None

            # 检查今天是否产生新信号（收盘后生效，明天执行）
            if signal_idx < len(signal_dates) and today_str >= signal_dates[signal_idx]:
                new_signal = signals[signal_dates[signal_idx]]
                pending_signal = dict(
                    zip(new_signal["ts_code"], new_signal["weight"])
                )
                signal_idx += 1

            # 计算今日组合收益（用旧持仓或刚换仓后的持仓）
            daily_return = 0.0
            for code, weight in current_holdings.items():
                ret = price_cache.get((code, today_str), 0.0)
                daily_return += weight * ret

            portfolio_value *= (1 + daily_return)
            nav[today] = portfolio_value

        # 获取基准净值
        benchmark_nav = self._get_benchmark_nav(start_date, end_date)

        result = {
            "nav": nav,
            "benchmark_nav": benchmark_nav,
            "trades": pd.DataFrame(trades),
            "turnover": pd.DataFrame(turnover_list),
        }

        logger.info(f"回测完成: 最终净值={portfolio_value:.4f}")
        return result

    def _get_trade_dates(self, start_date: str, end_date: str) -> pd.DatetimeIndex:
        """获取交易日序列。"""
        df = self.db.query(
            f"SELECT DISTINCT trade_date FROM daily_price "
            f"WHERE trade_date >= '{start_date}' "
            f"AND trade_date <= '{end_date}' "
            f"ORDER BY trade_date"
        )
        if df.empty:
            return pd.DatetimeIndex([])
        return pd.to_datetime(df["trade_date"])

    def _load_prices(
        self, codes: list[str], start_date: str, end_date: str
    ) -> dict[tuple, float]:
        """
        预加载日收益率缓存。

        Returns:
            字典 {(ts_code, date_str): daily_return}。
        """
        if not codes:
            return {}

        codes_str = "','".join(codes)
        df = self.db.query(
            f"SELECT ts_code, trade_date, pct_chg FROM daily_price "
            f"WHERE trade_date >= '{start_date}' "
            f"AND trade_date <= '{end_date}' "
            f"AND ts_code IN ('{codes_str}')"
        )

        cache = {}
        for _, row in df.iterrows():
            date_str = pd.to_datetime(row["trade_date"]).strftime("%Y-%m-%d")
            ret = row["pct_chg"] / 100.0 if pd.notna(row["pct_chg"]) else 0.0
            cache[(row["ts_code"], date_str)] = ret

        return cache

    def _apply_limit_constraints(
        self,
        current: dict[str, float],
        target: dict[str, float],
        date_str: str,
    ) -> dict[str, float]:
        """
        处理涨跌停限制。

        规则：
            - 涨停股不可买入：如果目标中有新买入的涨停股，保持原仓位
            - 跌停股不可卖出：如果要卖出的股票跌停，保持原仓位

        Args:
            current: 当前持仓。
            target: 目标持仓。
            date_str: 交易日期。

        Returns:
            调整后的目标持仓。
        """
        # 查询涨跌停状态
        all_codes = set(list(current.keys()) + list(target.keys()))
        if not all_codes:
            return target

        codes_str = "','".join(all_codes)
        df_limit = self.db.query(
            f"SELECT ts_code, is_limit_up, is_limit_down FROM daily_price "
            f"WHERE trade_date = '{date_str}' "
            f"AND ts_code IN ('{codes_str}')"
        )

        if df_limit.empty:
            return target

        limit_up = set(df_limit[df_limit["is_limit_up"] == 1]["ts_code"])
        limit_down = set(df_limit[df_limit["is_limit_down"] == 1]["ts_code"])

        adjusted = target.copy()
        blocked_weight = 0.0

        for code in list(adjusted.keys()):
            old_w = current.get(code, 0)
            new_w = adjusted.get(code, 0)

            # 涨停不可买入（新增或加仓）
            if code in limit_up and new_w > old_w:
                adjusted[code] = old_w
                blocked_weight += (new_w - old_w)
                logger.debug(f"{code} 涨停，不可买入")

            # 跌停不可卖出（减仓或清仓）
            if code in limit_down and new_w < old_w:
                adjusted[code] = old_w
                blocked_weight += (old_w - new_w)
                logger.debug(f"{code} 跌停，不可卖出")

        # 重新归一化权重
        total_w = sum(adjusted.values())
        if total_w > 0 and abs(total_w - 1.0) > 0.01:
            for code in adjusted:
                adjusted[code] /= total_w

        return adjusted

    def _calc_trade_cost(
        self,
        current: dict[str, float],
        target: dict[str, float],
    ) -> tuple[float, float]:
        """
        计算换手率和交易成本。

        Args:
            current: 当前持仓权重。
            target: 目标持仓权重。

        Returns:
            (换手率, 成本比例) 元组。
        """
        all_codes = set(list(current.keys()) + list(target.keys()))

        total_buy = 0.0
        total_sell = 0.0

        for code in all_codes:
            old_w = current.get(code, 0)
            new_w = target.get(code, 0)
            delta = new_w - old_w

            if delta > 0:
                total_buy += delta
            elif delta < 0:
                total_sell += abs(delta)

        turnover = (total_buy + total_sell) / 2  # 单边换手率
        cost = total_buy * self.buy_cost + total_sell * self.sell_cost

        return turnover, cost

    def _get_benchmark_nav(self, start_date: str, end_date: str) -> pd.Series:
        """
        获取基准指数（沪深300）净值。

        尝试从 daily_price 中查找指数代码。
        如果没有指数数据，返回空 Series。
        """
        # 基准使用全市场等权作为备选
        df = self.db.query(
            f"SELECT trade_date, AVG(pct_chg) as avg_ret FROM daily_price "
            f"WHERE trade_date >= '{start_date}' "
            f"AND trade_date <= '{end_date}' "
            f"GROUP BY trade_date "
            f"ORDER BY trade_date"
        )

        if df.empty:
            return pd.Series(dtype=float)

        df["trade_date"] = pd.to_datetime(df["trade_date"])
        df = df.set_index("trade_date")
        df["nav"] = (1 + df["avg_ret"] / 100).cumprod()

        return df["nav"]

    # ----------------------------------------------------------
    # 绩效指标
    # ----------------------------------------------------------

    @staticmethod
    def summary(result: dict) -> pd.DataFrame:
        """
        计算回测绩效指标。

        Args:
            result: run() 返回的回测结果。

        Returns:
            绩效摘要 DataFrame。
        """
        nav = result.get("nav")
        if nav is None or nav.empty:
            return pd.DataFrame()

        benchmark = result.get("benchmark_nav")

        # 日收益率
        daily_ret = nav.pct_change().dropna()
        n_days = len(daily_ret)
        n_years = n_days / 252

        # 总收益
        total_return = nav.iloc[-1] / nav.iloc[0] - 1

        # 年化收益
        annual_return = (1 + total_return) ** (1 / max(n_years, 0.01)) - 1

        # 年化波动率
        annual_vol = daily_ret.std() * np.sqrt(252)

        # 夏普比率（无风险利率假设 2%）
        rf = 0.02
        sharpe = (annual_return - rf) / annual_vol if annual_vol > 0 else np.nan

        # 最大回撤
        cummax = nav.cummax()
        drawdown = (nav - cummax) / cummax
        max_drawdown = drawdown.min()
        max_dd_date = drawdown.idxmin()

        # Calmar 比率
        calmar = annual_return / abs(max_drawdown) if max_drawdown != 0 else np.nan

        # 胜率（日）
        win_rate = (daily_ret > 0).mean()

        # 换手率
        turnover_df = result.get("turnover")
        avg_turnover = 0
        if turnover_df is not None and not turnover_df.empty:
            avg_turnover = turnover_df["turnover"].mean()

        metrics = {
            "总收益": f"{total_return:.2%}",
            "年化收益": f"{annual_return:.2%}",
            "年化波动率": f"{annual_vol:.2%}",
            "夏普比率": f"{sharpe:.2f}",
            "最大回撤": f"{max_drawdown:.2%}",
            "最大回撤日期": str(max_dd_date.date()) if pd.notna(max_dd_date) else "",
            "Calmar比率": f"{calmar:.2f}",
            "日胜率": f"{win_rate:.2%}",
            "平均换手率": f"{avg_turnover:.2%}",
            "交易天数": n_days,
        }

        # 基准对比
        if benchmark is not None and not benchmark.empty:
            bm_ret = benchmark.iloc[-1] / benchmark.iloc[0] - 1
            bm_annual = (1 + bm_ret) ** (1 / max(n_years, 0.01)) - 1
            excess = annual_return - bm_annual
            metrics["基准年化收益"] = f"{bm_annual:.2%}"
            metrics["超额年化收益"] = f"{excess:.2%}"

        return pd.DataFrame(
            list(metrics.items()), columns=["指标", "值"]
        )

    # ----------------------------------------------------------
    # 可视化
    # ----------------------------------------------------------

    @staticmethod
    def plot(result: dict, save_path: Optional[str] = None):
        """
        绘制回测净值曲线和回撤图。

        Args:
            result: run() 返回的回测结果。
            save_path: 图表保存路径。
        """
        nav = result.get("nav")
        if nav is None or nav.empty:
            return

        benchmark = result.get("benchmark_nav")

        fig, (ax1, ax2) = plt.subplots(
            2, 1, figsize=(14, 8), gridspec_kw={"height_ratios": [3, 1]}, sharex=True
        )

        # 净值曲线
        ax1.plot(nav.index, nav, label="Strategy", color="#e74c3c", linewidth=1.5)

        if benchmark is not None and not benchmark.empty:
            # 对齐到相同时间范围
            common_dates = nav.index.intersection(benchmark.index)
            if len(common_dates) > 0:
                bm_aligned = benchmark.loc[common_dates]
                # 归一化到同一起点
                bm_aligned = bm_aligned / bm_aligned.iloc[0] * nav.iloc[0]
                ax1.plot(common_dates, bm_aligned, label="Benchmark",
                         color="#3498db", linewidth=1.5, alpha=0.7)

        ax1.set_ylabel("NAV")
        ax1.set_title("Backtest Result")
        ax1.legend(loc="upper left")
        ax1.grid(True, alpha=0.3)

        # 回撤曲线
        cummax = nav.cummax()
        drawdown = (nav - cummax) / cummax
        ax2.fill_between(drawdown.index, 0, drawdown, color="#e74c3c", alpha=0.3)
        ax2.plot(drawdown.index, drawdown, color="#e74c3c", linewidth=0.8)
        ax2.set_ylabel("Drawdown")
        ax2.set_xlabel("Date")
        ax2.grid(True, alpha=0.3)

        plt.tight_layout()

        if save_path is None:
            output_dir = PROJECT_ROOT / "output"
            output_dir.mkdir(exist_ok=True)
            save_path = str(output_dir / "backtest_result.png")

        plt.savefig(save_path, dpi=150, bbox_inches="tight")
        plt.close()
        logger.info(f"回测图表已保存: {save_path}")
