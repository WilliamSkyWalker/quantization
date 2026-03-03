"""
轻量回测引擎（持仓制，匹配模拟盘逻辑）

基于 pandas 实现的事件驱动回测框架：
    - 输入：每期选股信号（调仓日 → 目标持仓和权重）
    - 处理：T+1 开盘价执行、交易成本（佣金/印花税/滑点）、涨跌停限制、整手约束
    - 输出：净值曲线、绩效指标、交易记录

执行逻辑（与 PaperTrader 一致）：
    - T+1 开盘价为成交基准价
    - 买入：open * (1 + slippage) + 佣金（最低5元）
    - 卖出：open * (1 - slippage) - 佣金（最低5元） - 印花税
    - 100股整手约束
    - 现金追踪：先卖后买
    - 每日净值 = (现金 + 持仓市值) / 初始资金
"""

import logging
from typing import Optional, Callable

import numpy as np
import pandas as pd
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

from backend.services.config import (
    BUY_COMMISSION,
    SELL_COMMISSION,
    STAMP_TAX,
    SLIPPAGE,
    PAPER_INITIAL_CAPITAL,
    LOG_LEVEL,
    PROJECT_ROOT,
    ALLOWED_INDUSTRIES,
    INDUSTRY_INDEX_MAP,
)
from backend.services.data.database import DatabaseManager

logger = logging.getLogger(__name__)
logger.setLevel(LOG_LEVEL)

plt.rcParams["font.sans-serif"] = ["SimHei", "Arial Unicode MS", "DejaVu Sans"]
plt.rcParams["axes.unicode_minus"] = False

# A股最小交易单位
LOT_SIZE = 100
# 最低佣金（元）
MIN_COMMISSION = 5.0


class BacktestEngine:
    """
    回测引擎（持仓制，匹配模拟盘）。

    用法:
        engine = BacktestEngine(db)
        result = engine.run(signals, start_date, end_date)
        engine.plot(result)
        print(engine.summary(result))
    """

    def __init__(
        self,
        db: DatabaseManager,
        buy_commission: float = BUY_COMMISSION,
        sell_commission: float = SELL_COMMISSION,
        stamp_tax: float = STAMP_TAX,
        slippage: float = SLIPPAGE,
        initial_capital: float = PAPER_INITIAL_CAPITAL,
        benchmark: str = "000300",
        **kwargs,
    ):
        self.db = db
        self.benchmark = benchmark
        self.initial_capital = initial_capital

        # Backward compat: accept old buy_cost/sell_cost
        if 'buy_cost' in kwargs:
            buy_commission = kwargs['buy_cost']
            slippage = 0
        if 'sell_cost' in kwargs:
            sell_commission = kwargs['sell_cost']
            stamp_tax = 0

        self.buy_commission = buy_commission
        self.sell_commission = sell_commission
        self.stamp_tax = stamp_tax
        self.slippage = slippage

    def run(
        self,
        signals: dict[str, pd.DataFrame],
        start_date: str,
        end_date: str,
        cancel_check: Optional[Callable[[], bool]] = None,
    ) -> dict:
        """
        执行回测（持仓制，匹配模拟盘逻辑）。

        Args:
            signals: {调仓日期: DataFrame[ts_code, weight]} 字典。
            start_date: 回测起始日期。
            end_date: 回测结束日期。

        Returns:
            回测结果字典，包含：
                - nav: 策略净值 Series（DatetimeIndex）
                - benchmark_nav: 基准净值 Series
                - trades: 交易记录 DataFrame
                - turnover: 每期换手率 Series
        """
        logger.info(f"开始回测: {start_date} ~ {end_date}")

        def ensure_not_cancelled():
            if cancel_check and cancel_check():
                raise RuntimeError('回测已取消')

        # 获取全部交易日（延伸几天确保最后一期信号能 T+1 执行）
        extended_end = (pd.to_datetime(end_date) + pd.Timedelta(days=10)).strftime("%Y-%m-%d")
        trade_dates = self._get_trade_dates(start_date, extended_end)
        if trade_dates.empty:
            logger.error("无交易日数据")
            return {}

        # 排序信号日期
        signal_dates = sorted(signals.keys())

        # 预加载所有相关股票的价格数据
        all_codes = set()
        for df_sig in signals.values():
            ensure_not_cancelled()
            all_codes.update(df_sig["ts_code"].tolist())
        price_cache = self._load_prices(list(all_codes), start_date, extended_end)

        # 初始化持仓状态
        cash = float(self.initial_capital)
        positions = {}       # {ts_code: int_shares}
        last_close = {}      # {ts_code: float} 停牌时用最近收盘价
        pending_signal = None
        pending_sells = {}   # {ts_code: int_shares} 一字板排队卖单
        prev_adj_factors = {}  # {ts_code: float} 前日复权因子，用于检测除权除息
        signal_idx = 0

        nav = pd.Series(dtype=float)
        trades = []
        turnover_list = []

        for i, today in enumerate(trade_dates):
            ensure_not_cancelled()

            today_str = today.strftime("%Y-%m-%d")
            day_turnover_amount = 0.0

            # === 除权除息检测：adj_factor 变化时调整持仓股数 ===
            if positions and prev_adj_factors:
                for code in list(positions.keys()):
                    info = price_cache.get((code, today_str))
                    if not info:
                        continue
                    curr_adj = info.get('adj_factor')
                    prev_adj = prev_adj_factors.get(code)
                    if curr_adj is not None and prev_adj is not None and abs(curr_adj - prev_adj) > 1e-6:
                        adj_ratio = curr_adj / prev_adj
                        old_vol = positions[code]
                        new_vol = self._round_to_lot(old_vol * adj_ratio)
                        if new_vol > 0:
                            positions[code] = new_vol
                            logger.debug(
                                f"{code} 除权除息: adj_ratio={adj_ratio:.4f}, "
                                f"股数 {old_vol} -> {new_vol}"
                            )
                        else:
                            del positions[code]

            # === 优先处理排队卖单（每个交易日开头，不论是否有信号）===
            if pending_sells:
                resolved = []
                for code, vol in pending_sells.items():
                    info = price_cache.get((code, today_str))
                    if not info:
                        continue  # 停牌，继续排队

                    one_char_up, one_char_down = self._is_one_char_limit(info)
                    if one_char_down:
                        # 一字跌停，仍不可卖，继续排队
                        continue
                    if info['is_limit_down'] and not one_char_down:
                        # 普通跌停（非一字板），也不可卖
                        continue

                    # 可以卖出
                    open_px = info['open']
                    exec_price = round(open_px * (1 - self.slippage), 2)
                    actual_vol = min(vol, positions.get(code, 0))
                    actual_vol = self._round_to_lot(actual_vol)
                    if actual_vol <= 0:
                        resolved.append(code)
                        continue

                    amount = actual_vol * exec_price
                    fees = self._calc_fees(amount, "SELL")
                    cash += amount - fees
                    day_turnover_amount += amount

                    positions[code] = positions.get(code, 0) - actual_vol
                    if positions[code] <= 0:
                        del positions[code]

                    trades.append({
                        'date': today_str,
                        'ts_code': code,
                        'direction': 'SELL',
                        'volume': actual_vol,
                        'price': exec_price,
                        'amount': amount,
                        'fees': fees,
                    })
                    resolved.append(code)
                    logger.debug(f"{code} 排队卖单成功执行")

                for code in resolved:
                    del pending_sells[code]

            # === T+1 执行：如果昨天产生了待执行信号，今天开盘执行 ===
            if pending_signal is not None:
                target_weights = pending_signal  # {ts_code: weight}

                # 用开盘价估算总资产
                total_value = cash
                for code, shares in positions.items():
                    info = price_cache.get((code, today_str))
                    if info:
                        open_px = info['open']
                    else:
                        open_px = last_close.get(code, 0)
                    total_value += shares * open_px

                # 计算目标股数
                sell_orders = []   # (code, delta_shares, info)
                buy_orders = []    # (code, delta_shares, target_weight, info)

                all_involved = set(list(positions.keys()) + list(target_weights.keys()))
                # 排除已在排队中的卖单
                all_involved -= set(pending_sells.keys())

                for code in all_involved:
                    current_vol = positions.get(code, 0)
                    target_w = target_weights.get(code, 0)

                    info = price_cache.get((code, today_str))
                    if not info:
                        # 停牌：维持原仓位
                        continue

                    open_px = info['open']
                    if open_px <= 0:
                        continue

                    target_vol = self._round_to_lot(target_w * total_value / open_px)
                    delta = target_vol - current_vol

                    if abs(delta) < LOT_SIZE:
                        continue

                    if delta < 0:
                        sell_orders.append((code, abs(delta), info))
                    else:
                        buy_orders.append((code, delta, target_w, info))

                # 先卖后买
                for code, volume, info in sell_orders:
                    one_char_up, one_char_down = self._is_one_char_limit(info)

                    if one_char_down or info['is_limit_down']:
                        # 跌停（含一字跌停）→ 加入排队
                        pending_sells[code] = volume
                        logger.debug(f"{code} 跌停，加入卖单排队")
                        continue

                    open_px = info['open']
                    exec_price = round(open_px * (1 - self.slippage), 2)
                    actual_vol = min(volume, positions.get(code, 0))
                    actual_vol = self._round_to_lot(actual_vol)
                    if actual_vol <= 0:
                        continue

                    amount = actual_vol * exec_price
                    fees = self._calc_fees(amount, "SELL")
                    cash += amount - fees
                    day_turnover_amount += amount

                    positions[code] = positions.get(code, 0) - actual_vol
                    if positions[code] <= 0:
                        del positions[code]

                    trades.append({
                        'date': today_str,
                        'ts_code': code,
                        'direction': 'SELL',
                        'volume': actual_vol,
                        'price': exec_price,
                        'amount': amount,
                        'fees': fees,
                    })

                # 按权重降序买入
                buy_orders.sort(key=lambda x: x[2], reverse=True)

                for code, volume, weight, info in buy_orders:
                    one_char_up, _ = self._is_one_char_limit(info)

                    if info['is_limit_up'] or one_char_up:
                        logger.debug(f"{code} 涨停，不可买入")
                        continue

                    open_px = info['open']
                    exec_price = round(open_px * (1 + self.slippage), 2)

                    # 检查资金是否充足
                    cost_per_share = exec_price * (1 + self.buy_commission)
                    max_affordable = self._round_to_lot(cash / cost_per_share)
                    actual_vol = min(volume, max_affordable)
                    actual_vol = self._round_to_lot(actual_vol)

                    if actual_vol < LOT_SIZE:
                        logger.debug(f"{code} 资金不足，跳过买入")
                        continue

                    amount = actual_vol * exec_price
                    fees = self._calc_fees(amount, "BUY")
                    cash -= (amount + fees)
                    day_turnover_amount += amount

                    positions[code] = positions.get(code, 0) + actual_vol

                    trades.append({
                        'date': today_str,
                        'ts_code': code,
                        'direction': 'BUY',
                        'volume': actual_vol,
                        'price': exec_price,
                        'amount': amount,
                        'fees': fees,
                    })

                # 记录换手率
                turnover = day_turnover_amount / total_value if total_value > 0 else 0
                turnover_list.append({
                    'date': today_str,
                    'turnover': turnover / 2,  # 单边换手率
                })

                pending_signal = None

            # === 检查今天是否产生新信号（收盘后生效，明天执行）===
            if signal_idx < len(signal_dates) and today_str >= signal_dates[signal_idx]:
                new_signal = signals[signal_dates[signal_idx]]
                pending_signal = dict(
                    zip(new_signal["ts_code"], new_signal["weight"])
                )
                signal_idx += 1

            # === 每日净值 = (现金 + 持仓市值) / 初始资金 ===
            market_value = 0.0
            for code, shares in positions.items():
                info = price_cache.get((code, today_str))
                if info:
                    close_px = info['close']
                    last_close[code] = close_px
                else:
                    close_px = last_close.get(code, 0)
                market_value += shares * close_px

            nav[today] = (cash + market_value) / self.initial_capital

            # === 更新 prev_adj_factors（用于下一日除权除息检测）===
            for code in positions:
                info = price_cache.get((code, today_str))
                if info and info.get('adj_factor') is not None:
                    prev_adj_factors[code] = info['adj_factor']

        # 获取基准净值
        benchmark_nav = self._get_benchmark_nav(start_date, extended_end)

        # 获取行业指数净值（仅在行业白名单开启时）
        industry_benchmarks = self._get_industry_benchmark_navs(start_date, extended_end)

        result = {
            "nav": nav,
            "benchmark_nav": benchmark_nav,
            "industry_benchmarks": industry_benchmarks,
            "trades": pd.DataFrame(trades),
            "turnover": pd.DataFrame(turnover_list),
        }

        final_nav = nav.iloc[-1] if not nav.empty else 0
        logger.info(f"回测完成: 最终净值={final_nav:.4f}")
        return result

    # ----------------------------------------------------------
    # 辅助方法
    # ----------------------------------------------------------

    def _calc_fees(self, amount: float, direction: str) -> float:
        """
        计算交易费用（匹配 PaperTrader 逻辑）。

        Returns:
            总费用（佣金 + 印花税）。
        """
        if direction == "BUY":
            commission = max(amount * self.buy_commission, MIN_COMMISSION)
            return commission
        else:
            commission = max(amount * self.sell_commission, MIN_COMMISSION)
            stamp = amount * self.stamp_tax
            return commission + stamp

    @staticmethod
    def _round_to_lot(volume: float) -> int:
        """向下取整到 100 股整手。"""
        return int(volume // LOT_SIZE) * LOT_SIZE

    def _get_trade_dates(self, start_date: str, end_date: str) -> pd.DatetimeIndex:
        """获取交易日序列。"""
        df = self.db.query(
            "SELECT DISTINCT trade_date FROM daily_price "
            "WHERE trade_date >= :start_date "
            "AND trade_date <= :end_date "
            "ORDER BY trade_date",
            params={"start_date": start_date, "end_date": end_date},
        )
        if df.empty:
            return pd.DatetimeIndex([])
        return pd.to_datetime(df["trade_date"])

    def _load_prices(
        self, codes: list[str], start_date: str, end_date: str
    ) -> dict[tuple, dict]:
        """
        预加载价格缓存（open, close, 涨跌停状态, adj_factor）。

        Returns:
            字典 {(ts_code, date_str): {open, close, high, low, is_limit_up, is_limit_down, adj_factor}}。
        """
        if not codes:
            return {}

        from backend.services.factors.base import FactorBase
        params: dict = {"start_date": start_date, "end_date": end_date}
        in_clause, in_params = FactorBase._build_in_clause(codes)
        params.update(in_params)

        df = self.db.query(
            "SELECT ts_code, trade_date, `open`, `close`, `high`, `low`, "
            "is_limit_up, is_limit_down, adj_factor "
            "FROM daily_price "
            "WHERE trade_date >= :start_date "
            "AND trade_date <= :end_date "
            f"AND ts_code IN {in_clause}",
            params=params,
        )

        if df.empty:
            return {}

        df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.strftime("%Y-%m-%d")
        df["open"] = df["open"].fillna(df["close"])
        df["high"] = df["high"].fillna(df["close"])
        df["low"] = df["low"].fillna(df["close"])

        cache = {}
        for row in df.itertuples(index=False):
            adj = getattr(row, 'adj_factor', None)
            if adj is not None:
                try:
                    adj = float(adj)
                except (TypeError, ValueError):
                    adj = None
            cache[(row.ts_code, row.trade_date)] = {
                'open': row.open,
                'close': row.close,
                'high': row.high,
                'low': row.low,
                'is_limit_up': getattr(row, 'is_limit_up', 0) == 1,
                'is_limit_down': getattr(row, 'is_limit_down', 0) == 1,
                'adj_factor': adj,
            }

        return cache

    @staticmethod
    def _is_one_char_limit(info: dict) -> tuple[bool, bool]:
        """
        判断是否为一字板（开盘=最高=最低=收盘）。

        Returns:
            (is_one_char_limit_up, is_one_char_limit_down)
        """
        o, h, l, c = info['open'], info['high'], info['low'], info['close']
        if o == h == l == c:
            return (info['is_limit_up'], info['is_limit_down'])
        return (False, False)

    def _get_benchmark_nav(self, start_date: str, end_date: str) -> pd.Series:
        """
        获取基准指数净值。

        优先从 daily_price 表查询沪深300指数（000300.SH）的日线数据。
        若无指数数据则回退到全市场等权平均。

        Args:
            start_date: 起始日期。
            end_date: 结束日期。

        Returns:
            基准净值 Series（DatetimeIndex）。
        """
        # 优先使用沪深300指数真实数据
        benchmark_code = self.benchmark  # 默认 "000300"
        # 尝试匹配 daily_price 中的指数代码格式
        for code in [f"{benchmark_code}.SH", f"{benchmark_code}.SZ", benchmark_code]:
            df = self.db.query(
                "SELECT trade_date, pct_chg FROM daily_price "
                "WHERE ts_code = :code "
                "AND trade_date >= :start_date "
                "AND trade_date <= :end_date "
                "ORDER BY trade_date",
                params={"code": code, "start_date": start_date, "end_date": end_date},
            )
            if not df.empty:
                logger.info(f"基准: {code} ({len(df)} 个交易日)")
                df["trade_date"] = pd.to_datetime(df["trade_date"])
                df = df.set_index("trade_date")
                df["nav"] = (1 + df["pct_chg"] / 100).cumprod()
                return df["nav"]

        # 回退：全市场等权平均
        logger.warning("未找到沪深300指数数据，回退到全市场等权平均（建议运行 download_index）")
        df = self.db.query(
            "SELECT trade_date, AVG(pct_chg) as avg_ret FROM daily_price "
            "WHERE trade_date >= :start_date "
            "AND trade_date <= :end_date "
            "GROUP BY trade_date "
            "ORDER BY trade_date",
            params={"start_date": start_date, "end_date": end_date},
        )

        if df.empty:
            return pd.Series(dtype=float)

        df["trade_date"] = pd.to_datetime(df["trade_date"])
        df = df.set_index("trade_date")
        df["nav"] = (1 + df["avg_ret"] / 100).cumprod()

        return df["nav"]

    def _get_industry_benchmark_navs(
        self, start_date: str, end_date: str
    ) -> dict[str, pd.Series]:
        """
        获取行业指数净值（仅在行业白名单开启时）。

        遍历 ALLOWED_INDUSTRIES，在 INDUSTRY_INDEX_MAP 中查对应指数代码，
        从 daily_price 表查 pct_chg 计算累计净值。

        Returns:
            {行业名: nav_series} 字典，无数据的行业会被跳过。
        """
        if not ALLOWED_INDUSTRIES or not INDUSTRY_INDEX_MAP:
            return {}

        result = {}
        for industry in ALLOWED_INDUSTRIES:
            index_code = INDUSTRY_INDEX_MAP.get(industry)
            if not index_code:
                logger.debug(f"行业 {industry} 无对应指数代码，跳过")
                continue

            df = self.db.query(
                "SELECT trade_date, pct_chg FROM daily_price "
                "WHERE ts_code = :index_code "
                "AND trade_date >= :start_date "
                "AND trade_date <= :end_date "
                "ORDER BY trade_date",
                params={"index_code": index_code, "start_date": start_date, "end_date": end_date},
            )
            if df.empty:
                logger.warning(f"行业指数 {industry}({index_code}) 无数据，请运行 download_index")
                continue

            df["trade_date"] = pd.to_datetime(df["trade_date"])
            df = df.set_index("trade_date")
            df["nav"] = (1 + df["pct_chg"] / 100).cumprod()
            result[industry] = df["nav"]
            logger.info(f"行业指数: {industry}({index_code}) {len(df)} 个交易日")

        return result

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

        # 行业指数对比
        industry_benchmarks = result.get("industry_benchmarks", {})
        for ind_name, ind_nav in industry_benchmarks.items():
            if ind_nav is None or ind_nav.empty:
                continue
            ind_ret = ind_nav.iloc[-1] / ind_nav.iloc[0] - 1
            ind_annual = (1 + ind_ret) ** (1 / max(n_years, 0.01)) - 1
            ind_excess = annual_return - ind_annual
            metrics[f"{ind_name}指数年化"] = f"{ind_annual:.2%}"
            metrics[f"超额({ind_name})"] = f"{ind_excess:.2%}"

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

        # 行业指数对比线
        industry_benchmarks = result.get("industry_benchmarks", {})
        industry_colors = ["#2ecc71", "#9b59b6", "#f39c12", "#1abc9c", "#e67e22"]
        for idx, (ind_name, ind_nav) in enumerate(industry_benchmarks.items()):
            if ind_nav is None or ind_nav.empty:
                continue
            common = nav.index.intersection(ind_nav.index)
            if len(common) > 0:
                aligned = ind_nav.loc[common]
                aligned = aligned / aligned.iloc[0] * nav.iloc[0]
                color = industry_colors[idx % len(industry_colors)]
                ax1.plot(common, aligned, label=ind_name,
                         color=color, linewidth=1.2, linestyle="--", alpha=0.8)

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
