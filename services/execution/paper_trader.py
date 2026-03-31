"""
本地模拟盘交易执行器

替代已停服的掘金 GMTrader，使用本地数据库存储账户状态，
基于 daily_price 表的开盘价模拟成交（T+1执行）。

功能：
    1. 持久化账户状态（现金、持仓、交易记录、每日净值）
    2. 真实交易模拟（涨跌停限制、整手约束、滑点、佣金）
    3. 回放模式：回放历史区间逐日模拟
    4. 日常模式：每日运行一次执行当日信号

用法 (日常模式):
    trader = PaperTrader(db)
    trader.connect()
    trader.sync_position(target_weights_df)
    print(trader.get_position_report())

用法 (回放模式):
    trader = PaperTrader(db)
    trader.connect(initial_capital=1_000_000)
    trader.replay(signals, start_date="2023-01-01", end_date="2024-12-31")
"""

import logging
from datetime import date, datetime
from typing import Optional

import pandas as pd

from services.config import (
    BUY_COMMISSION,
    SELL_COMMISSION,
    STAMP_TAX,
    SLIPPAGE,
    PAPER_INITIAL_CAPITAL,
    PAPER_ACCOUNT_NAME,
    LOG_LEVEL,
)
from services.data.database import (
    DatabaseManager,
    PaperAccount,
    PaperPosition,
    PaperTransaction,
    PaperNav,
)
from services.execution.base_trader import BaseTrader

logger = logging.getLogger(__name__)
logger.setLevel(LOG_LEVEL)

# A 股最小交易单位
LOT_SIZE = 100
# 最低佣金（元）
MIN_COMMISSION = 5.0


class PaperTrader(BaseTrader):
    """
    本地模拟盘交易执行器。

    所有状态持久化到 MySQL/SQLite，通过 DatabaseManager 读写。
    """

    def __init__(
        self,
        db: DatabaseManager,
        account_name: str = PAPER_ACCOUNT_NAME,
        buy_commission: float = BUY_COMMISSION,
        sell_commission: float = SELL_COMMISSION,
        stamp_tax: float = STAMP_TAX,
        slippage: float = SLIPPAGE,
    ):
        self.db = db
        self.account_name = account_name
        self.buy_commission = buy_commission
        self.sell_commission = sell_commission
        self.stamp_tax = stamp_tax
        self.slippage = slippage
        self.connected = False
        self._initial_capital = 0.0

    # ----------------------------------------------------------
    # 连接 / 初始化
    # ----------------------------------------------------------

    def connect(self, initial_capital: float = PAPER_INITIAL_CAPITAL, **kwargs):
        """
        加载或创建模拟账户。

        如果账户已存在则直接加载，不存在则创建新账户。

        Args:
            initial_capital: 初始资金（仅新建账户时生效）。
        """
        session = self.db.get_session()
        try:
            account = session.query(PaperAccount).filter_by(
                account_name=self.account_name
            ).first()

            if account:
                self._initial_capital = account.initial_capital
                logger.info(
                    f"加载模拟账户 [{self.account_name}]: "
                    f"总资产={account.total_assets:,.2f}, "
                    f"现金={account.cash:,.2f}"
                )
            else:
                account = PaperAccount(
                    account_name=self.account_name,
                    initial_capital=initial_capital,
                    cash=initial_capital,
                    total_assets=initial_capital,
                )
                session.add(account)
                session.commit()
                self._initial_capital = initial_capital
                logger.info(
                    f"创建模拟账户 [{self.account_name}]: "
                    f"初始资金={initial_capital:,.2f}"
                )

            self.connected = True
        finally:
            session.close()

    def _ensure_connected(self):
        """确保已连接。"""
        if not self.connected:
            raise ConnectionError("请先调用 connect() 初始化模拟账户")

    # ----------------------------------------------------------
    # 账户与持仓查询
    # ----------------------------------------------------------

    def get_account_info(self) -> dict:
        """获取账户信息。"""
        self._ensure_connected()
        session = self.db.get_session()
        try:
            account = session.query(PaperAccount).filter_by(
                account_name=self.account_name
            ).first()
            if not account:
                return {}
            return {
                "total_assets": account.total_assets,
                "available_cash": account.cash,
                "market_value": account.total_assets - account.cash,
                "pnl": account.total_assets - account.initial_capital,
            }
        finally:
            session.close()

    def get_current_positions(self) -> pd.DataFrame:
        """获取当前持仓 DataFrame[ts_code, volume, market_value, cost]。"""
        self._ensure_connected()
        session = self.db.get_session()
        try:
            positions = session.query(PaperPosition).filter_by(
                account_name=self.account_name
            ).all()
            if not positions:
                return pd.DataFrame(
                    columns=["ts_code", "volume", "market_value", "cost"]
                )
            records = []
            for pos in positions:
                records.append({
                    "ts_code": pos.ts_code,
                    "volume": pos.volume,
                    "market_value": pos.market_value or 0,
                    "cost": pos.cost_basis,
                })
            return pd.DataFrame(records)
        finally:
            session.close()

    # ----------------------------------------------------------
    # 交易执行
    # ----------------------------------------------------------

    def sync_position(self, target_weights: pd.DataFrame, **kwargs) -> dict:
        """
        将持仓同步到目标权重。

        Args:
            target_weights: DataFrame[ts_code, weight]。
            trade_date: 交易日期（可选，默认使用数据库最新交易日）。

        Returns:
            {success, failed, skipped}。
        """
        self._ensure_connected()

        trade_date = kwargs.get("trade_date")
        if trade_date is None:
            trade_date = self.db.get_latest_trade_date()

        if trade_date is None:
            logger.error("无法获取交易日期")
            return {"success": 0, "failed": 0, "skipped": 0}

        return self._execute_rebalance(trade_date, target_weights)

    def order_target_percent(self, ts_code: str, target_percent: float) -> bool:
        """按目标比例下单（单股）。"""
        self._ensure_connected()
        target_weights = pd.DataFrame({
            "ts_code": [ts_code],
            "weight": [target_percent],
        })
        result = self.sync_position(target_weights)
        return result.get("failed", 0) == 0

    # ----------------------------------------------------------
    # 核心交易逻辑
    # ----------------------------------------------------------

    def _execute_rebalance(
        self, trade_date: str, target_weights: pd.DataFrame
    ) -> dict:
        """
        执行一次调仓。

        Args:
            trade_date: 交易日期字符串。
            target_weights: DataFrame[ts_code, weight]。

        Returns:
            {success, failed, skipped}。
        """
        session = self.db.get_session()
        try:
            account = session.query(PaperAccount).filter_by(
                account_name=self.account_name
            ).first()

            # 获取当前持仓
            positions = session.query(PaperPosition).filter_by(
                account_name=self.account_name
            ).all()
            pos_map = {p.ts_code: p for p in positions}

            # 获取所有相关股票的行情
            target = dict(zip(target_weights["ts_code"], target_weights["weight"]))
            all_codes = set(list(pos_map.keys()) + list(target.keys()))

            if not all_codes:
                return {"success": 0, "failed": 0, "skipped": 0}

            price_info = self._get_price_info(trade_date, list(all_codes))

            # 计算当前总资产（用开盘价估算）
            market_value = 0.0
            for code, pos in pos_map.items():
                info = price_info.get(code)
                if info:
                    open_px = info.get("open") or info["close"]
                    market_value += pos.volume * open_px
                else:
                    market_value += pos.market_value or 0

            total_value = account.cash + market_value

            # 计算目标股数和交易方向（用开盘价）
            sell_orders = []
            buy_orders = []

            for code in all_codes:
                current_vol = pos_map[code].volume if code in pos_map else 0
                target_w = target.get(code, 0)
                info = price_info.get(code)

                if not info:
                    continue
                open_px = info.get("open") or info["close"]
                if open_px <= 0:
                    continue

                target_vol = self._round_to_lot(
                    target_w * total_value / open_px
                )
                delta = target_vol - current_vol

                if abs(delta) < LOT_SIZE:
                    continue

                if delta < 0:
                    sell_orders.append((code, abs(delta), info))
                else:
                    buy_orders.append((code, delta, target_w, info))

            results = {"success": 0, "failed": 0, "skipped": 0}
            failed_sells = {}  # {ts_code: volume} 跌停/一字板无法卖出的订单
            trade_dt = self._parse_date(trade_date)

            # 先卖后买（用开盘价作为成交基准价）
            for code, volume, info in sell_orders:
                one_char_up, one_char_down = self._is_one_char_limit(info)

                if one_char_down or info.get("is_limit_down"):
                    self._log_blocked_trade(
                        session, trade_dt, code, "SELL", volume,
                        info.get("open") or info["close"],
                        "limit_down_blocked"
                    )
                    failed_sells[code] = volume
                    results["failed"] += 1
                    continue

                self._execute_sell(
                    session, account, pos_map, trade_dt,
                    code, volume, info.get("open") or info["close"]
                )
                results["success"] += 1

            # 按权重降序买入（优先买入权重大的）
            buy_orders.sort(key=lambda x: x[2], reverse=True)

            for code, volume, weight, info in buy_orders:
                one_char_up, _ = self._is_one_char_limit(info)

                if info.get("is_limit_up") or one_char_up:
                    self._log_blocked_trade(
                        session, trade_dt, code, "BUY", volume,
                        info.get("open") or info["close"],
                        "limit_up_blocked"
                    )
                    results["failed"] += 1
                    continue

                filled = self._execute_buy(
                    session, account, pos_map, trade_dt,
                    code, volume, info.get("open") or info["close"]
                )
                if filled > 0:
                    results["success"] += 1
                elif filled == 0:
                    results["failed"] += 1

            session.commit()
            results["failed_sells"] = failed_sells

            logger.info(
                f"调仓完成 [{trade_date}]: "
                f"成功={results['success']}, "
                f"失败={results['failed']}, "
                f"跳过={results['skipped']}"
            )
            return results

        except Exception as e:
            session.rollback()
            logger.error(f"调仓失败: {e}")
            raise
        finally:
            session.close()

    def _execute_sell(
        self, session, account, pos_map, trade_date, ts_code, volume, base_price
    ):
        """执行卖出。"""
        pos = pos_map.get(ts_code)
        if not pos:
            return

        actual_vol = min(volume, pos.volume)
        actual_vol = self._round_to_lot(actual_vol)
        if actual_vol <= 0:
            return

        exec_price = round(base_price * (1 - self.slippage), 2)
        amount = actual_vol * exec_price
        fees = self._calc_fees(amount, "SELL")

        # 更新现金
        account.cash += amount - fees["total_cost"]

        # 更新持仓
        pos.volume -= actual_vol
        if pos.volume <= 0:
            session.delete(pos)
            del pos_map[ts_code]
        else:
            pos.current_price = base_price
            pos.market_value = pos.volume * base_price

        # 记录交易
        session.add(PaperTransaction(
            account_name=self.account_name,
            trade_date=trade_date,
            ts_code=ts_code,
            direction="SELL",
            target_volume=volume,
            filled_volume=actual_vol,
            price=exec_price,
            amount=amount,
            commission=fees["commission"],
            stamp_tax=fees["stamp_tax"],
            slippage_cost=round(actual_vol * base_price * self.slippage, 2),
            total_cost=fees["total_cost"],
            reason="rebalance",
        ))

    def _execute_buy(
        self, session, account, pos_map, trade_date, ts_code, volume, base_price
    ) -> int:
        """
        执行买入。

        Returns:
            实际成交股数。
        """
        exec_price = round(base_price * (1 + self.slippage), 2)
        cost_per_share = exec_price * (1 + self.buy_commission)

        # 检查资金是否充足，不足则计算最大可买
        max_affordable = self._round_to_lot(account.cash / cost_per_share)
        actual_vol = min(volume, max_affordable)
        actual_vol = self._round_to_lot(actual_vol)

        if actual_vol < LOT_SIZE:
            self._log_blocked_trade(
                session, trade_date, ts_code, "BUY", volume, base_price,
                "insufficient_cash"
            )
            return 0

        amount = actual_vol * exec_price
        fees = self._calc_fees(amount, "BUY")

        # 更新现金
        account.cash -= (amount + fees["total_cost"])

        # 更新持仓
        pos = pos_map.get(ts_code)
        if pos:
            # 加仓：重新计算成本
            old_cost_total = pos.volume * pos.cost_basis
            new_cost_total = amount + fees["total_cost"]
            pos.cost_basis = (old_cost_total + new_cost_total) / (pos.volume + actual_vol)
            pos.volume += actual_vol
            pos.current_price = base_price
            pos.market_value = pos.volume * base_price
        else:
            new_pos = PaperPosition(
                account_name=self.account_name,
                ts_code=ts_code,
                volume=actual_vol,
                cost_basis=(amount + fees["total_cost"]) / actual_vol,
                current_price=base_price,
                market_value=actual_vol * base_price,
            )
            session.add(new_pos)
            pos_map[ts_code] = new_pos

        # 记录交易
        reason = "rebalance"
        if actual_vol < volume:
            reason = "rebalance_partial"

        session.add(PaperTransaction(
            account_name=self.account_name,
            trade_date=trade_date,
            ts_code=ts_code,
            direction="BUY",
            target_volume=volume,
            filled_volume=actual_vol,
            price=exec_price,
            amount=amount,
            commission=fees["commission"],
            stamp_tax=0,
            slippage_cost=round(actual_vol * base_price * self.slippage, 2),
            total_cost=fees["total_cost"],
            reason=reason,
        ))

        return actual_vol

    def _log_blocked_trade(
        self, session, trade_date, ts_code, direction, volume, price, reason
    ):
        """记录被阻断的交易。"""
        session.add(PaperTransaction(
            account_name=self.account_name,
            trade_date=trade_date,
            ts_code=ts_code,
            direction=direction,
            target_volume=volume,
            filled_volume=0,
            price=price,
            amount=0,
            commission=0,
            stamp_tax=0,
            slippage_cost=0,
            total_cost=0,
            reason=reason,
        ))
        logger.debug(f"{ts_code} {direction} 被阻断: {reason}")

    # ----------------------------------------------------------
    # 回放模式
    # ----------------------------------------------------------

    def replay(
        self,
        signals: dict[str, pd.DataFrame],
        start_date: str,
        end_date: str,
    ):
        """
        回放历史交易。

        信号延迟执行（T+1）：
            - T 日收盘后产生信号（存为 pending_signal）
            - T+1 日开盘执行调仓，使用 T+1 日开盘价成交

        Args:
            signals: {调仓日期: DataFrame[ts_code, weight]} 字典。
            start_date: 起始日期。
            end_date: 结束日期。
        """
        self._ensure_connected()

        trade_dates = self._get_trade_dates(start_date, end_date)
        if not trade_dates:
            logger.warning("无交易日数据")
            return

        signal_dates = sorted(signals.keys())
        signal_idx = 0
        prev_adj_factors = {}
        pending_signal = None  # T日产生信号，T+1日执行
        pending_sells = {}     # {ts_code: volume} 跌停排队卖单

        logger.info(
            f"开始回放: {start_date} ~ {end_date}, "
            f"{len(trade_dates)} 个交易日, "
            f"{len(signal_dates)} 次调仓"
        )

        for trade_date in trade_dates:
            date_str = trade_date if isinstance(trade_date, str) else str(trade_date)

            # 检测除权除息
            prev_adj_factors = self._apply_corporate_actions(
                date_str, prev_adj_factors
            )

            # === 优先处理排队卖单 ===
            if pending_sells:
                session = self.db.get_session()
                try:
                    account = session.query(PaperAccount).filter_by(
                        account_name=self.account_name
                    ).first()
                    positions = session.query(PaperPosition).filter_by(
                        account_name=self.account_name
                    ).all()
                    pos_map = {p.ts_code: p for p in positions}

                    codes = list(pending_sells.keys())
                    price_info = self._get_price_info(date_str, codes)
                    resolved = []

                    for code, vol in pending_sells.items():
                        info = price_info.get(code)
                        if not info:
                            continue  # 停牌，继续排队

                        one_char_up, one_char_down = self._is_one_char_limit(info)
                        if one_char_down or info.get("is_limit_down"):
                            continue  # 仍然跌停，继续排队

                        # 可以卖出
                        trade_dt = self._parse_date(date_str)
                        self._execute_sell(
                            session, account, pos_map, trade_dt,
                            code, vol, info.get("open") or info["close"]
                        )
                        resolved.append(code)
                        logger.debug(f"{code} 排队卖单成功执行")

                    for code in resolved:
                        del pending_sells[code]

                    session.commit()
                finally:
                    session.close()

            # T+1 执行：如果昨天产生了待执行信号，今天开盘执行
            if pending_signal is not None:
                result = self._execute_rebalance(date_str, pending_signal)
                # 收集新的失败卖单加入排队
                new_failed = result.get("failed_sells", {})
                pending_sells.update(new_failed)
                pending_signal = None

            # 检查今天是否产生新信号（收盘后生效，明天执行）
            if signal_idx < len(signal_dates) and date_str >= signal_dates[signal_idx]:
                pending_signal = signals[signal_dates[signal_idx]]
                signal_idx += 1

            # 每日更新持仓市值和净值
            self._update_daily_nav(date_str)

        logger.info(f"回放完成: {len(trade_dates)} 个交易日")

    def _apply_corporate_actions(
        self, trade_date: str, prev_adj_factors: dict
    ) -> dict:
        """
        检测并处理除权除息（通过 adj_factor 变化）。

        Returns:
            更新后的 adj_factor 字典。
        """
        session = self.db.get_session()
        try:
            positions = session.query(PaperPosition).filter_by(
                account_name=self.account_name
            ).all()

            if not positions:
                return {}

            codes = [p.ts_code for p in positions]
            current_adj = self._get_adj_factors(trade_date, codes)

            for pos in positions:
                code = pos.ts_code
                curr = current_adj.get(code)
                prev = prev_adj_factors.get(code)

                if curr is not None and prev is not None and abs(curr - prev) > 1e-6:
                    adj_ratio = curr / prev
                    old_vol = pos.volume
                    pos.volume = self._round_to_lot(pos.volume * adj_ratio)
                    if pos.volume > 0:
                        pos.cost_basis = pos.cost_basis / adj_ratio
                    logger.info(
                        f"{code} 除权除息: adj_ratio={adj_ratio:.4f}, "
                        f"股数 {old_vol} -> {pos.volume}"
                    )

            session.commit()
            return current_adj

        finally:
            session.close()

    def _update_daily_nav(self, trade_date: str):
        """更新持仓市值并记录每日净值快照。"""
        session = self.db.get_session()
        try:
            account = session.query(PaperAccount).filter_by(
                account_name=self.account_name
            ).first()

            positions = session.query(PaperPosition).filter_by(
                account_name=self.account_name
            ).all()

            # 获取收盘价
            codes = [p.ts_code for p in positions]
            if codes:
                price_info = self._get_price_info(trade_date, codes)
            else:
                price_info = {}

            # 更新持仓市值
            market_value = 0.0
            for pos in positions:
                info = price_info.get(pos.ts_code)
                if info:
                    pos.current_price = info["close"]
                    pos.market_value = pos.volume * info["close"]
                market_value += pos.market_value or 0

            # 更新账户
            prev_assets = account.total_assets
            account.total_assets = account.cash + market_value

            # 记录净值
            trade_dt = self._parse_date(trade_date)
            nav_value = account.total_assets / account.initial_capital
            daily_pnl = account.total_assets - prev_assets
            daily_return = daily_pnl / prev_assets if prev_assets > 0 else 0

            # 检查是否已有记录
            existing = session.query(PaperNav).filter_by(
                account_name=self.account_name,
                trade_date=trade_dt,
            ).first()

            if existing:
                existing.cash = account.cash
                existing.market_value = market_value
                existing.total_assets = account.total_assets
                existing.nav = nav_value
                existing.daily_pnl = daily_pnl
                existing.daily_return = daily_return
                existing.n_holdings = len(positions)
            else:
                session.add(PaperNav(
                    account_name=self.account_name,
                    trade_date=trade_dt,
                    cash=account.cash,
                    market_value=market_value,
                    total_assets=account.total_assets,
                    nav=nav_value,
                    daily_pnl=daily_pnl,
                    daily_return=daily_return,
                    n_holdings=len(positions),
                ))

            session.commit()

        finally:
            session.close()

    # ----------------------------------------------------------
    # 对账与报告
    # ----------------------------------------------------------

    def reconcile(self, target_weights: pd.DataFrame) -> pd.DataFrame:
        """对账：比较目标权重和实际持仓差异。"""
        self._ensure_connected()
        account = self.get_account_info()
        if not account:
            return pd.DataFrame()

        total_assets = account["total_assets"]
        current = self.get_current_positions()

        target = dict(zip(target_weights["ts_code"], target_weights["weight"]))
        actual = {}
        if not current.empty:
            for _, row in current.iterrows():
                actual[row["ts_code"]] = row["market_value"] / total_assets

        all_codes = set(list(target.keys()) + list(actual.keys()))
        records = []
        for code in sorted(all_codes):
            tgt = target.get(code, 0)
            act = actual.get(code, 0)
            records.append({
                "ts_code": code,
                "target_weight": tgt,
                "actual_weight": act,
                "diff": act - tgt,
            })

        df = pd.DataFrame(records)
        if not df.empty:
            df = df.sort_values("diff", key=abs, ascending=False)

        return df

    def get_position_report(self) -> str:
        """生成持仓报告文本。"""
        self._ensure_connected()
        account = self.get_account_info()
        positions = self.get_current_positions()

        lines = ["=" * 50, "    模拟盘持仓报告", "=" * 50]

        if account:
            lines.append(f"  总资产: {account.get('total_assets', 0):,.2f}")
            lines.append(f"  可用资金: {account.get('available_cash', 0):,.2f}")
            lines.append(f"  持仓市值: {account.get('market_value', 0):,.2f}")
            lines.append(f"  盈亏: {account.get('pnl', 0):,.2f}")

        lines.append("-" * 50)

        if positions.empty:
            lines.append("  无持仓")
        else:
            lines.append(f"  持仓 {len(positions)} 只股票:")
            for _, row in positions.iterrows():
                lines.append(
                    f"    {row['ts_code']:12s} "
                    f"持仓{row['volume']:.0f}股 "
                    f"市值{row['market_value']:,.2f}"
                )

        lines.append("=" * 50)
        return "\n".join(lines)

    # ----------------------------------------------------------
    # 净值与交易记录
    # ----------------------------------------------------------

    def get_nav_series(self) -> pd.Series:
        """获取净值时间序列。"""
        self._ensure_connected()
        df = self.db.query(
            f"SELECT trade_date, nav FROM paper_nav "
            f"WHERE account_name = '{self.account_name}' "
            f"ORDER BY trade_date"
        )
        if df.empty:
            return pd.Series(dtype=float)
        df["trade_date"] = pd.to_datetime(df["trade_date"])
        return df.set_index("trade_date")["nav"]

    def get_transactions(
        self,
        trade_date: Optional[str] = None,
        last_n: int = 50,
    ) -> pd.DataFrame:
        """
        获取交易记录。

        Args:
            trade_date: 指定日期（可选）。
            last_n: 最近 N 条记录。

        Returns:
            交易记录 DataFrame。
        """
        self._ensure_connected()
        sql = (
            f"SELECT trade_date, ts_code, direction, filled_volume, "
            f"price, amount, total_cost, reason "
            f"FROM paper_transaction "
            f"WHERE account_name = '{self.account_name}'"
        )
        if trade_date:
            sql += f" AND trade_date = '{trade_date}'"
        sql += f" ORDER BY id DESC LIMIT {last_n}"
        return self.db.query(sql)

    def get_nav_history(self, last_n: int = 20) -> pd.DataFrame:
        """获取最近 N 天的净值记录。"""
        self._ensure_connected()
        return self.db.query(
            f"SELECT trade_date, cash, market_value, total_assets, nav, "
            f"daily_pnl, daily_return, n_holdings "
            f"FROM paper_nav "
            f"WHERE account_name = '{self.account_name}' "
            f"ORDER BY trade_date DESC LIMIT {last_n}"
        )

    # ----------------------------------------------------------
    # 账户管理
    # ----------------------------------------------------------

    def reset_account(self):
        """重置模拟账户：清空持仓、交易、净值，现金回到初始。"""
        self._ensure_connected()
        session = self.db.get_session()
        try:
            # 清空持仓
            session.query(PaperPosition).filter_by(
                account_name=self.account_name
            ).delete()
            # 清空交易记录
            session.query(PaperTransaction).filter_by(
                account_name=self.account_name
            ).delete()
            # 清空净值
            session.query(PaperNav).filter_by(
                account_name=self.account_name
            ).delete()
            # 重置账户
            account = session.query(PaperAccount).filter_by(
                account_name=self.account_name
            ).first()
            if account:
                account.initial_capital = PAPER_INITIAL_CAPITAL
                account.cash = PAPER_INITIAL_CAPITAL
                account.total_assets = PAPER_INITIAL_CAPITAL

            session.commit()
            logger.info(f"模拟账户 [{self.account_name}] 已重置")
        finally:
            session.close()

    # ----------------------------------------------------------
    # 辅助方法
    # ----------------------------------------------------------

    def _get_price_info(self, trade_date: str, codes: list[str]) -> dict:
        """
        获取指定日期的行情信息。

        Returns:
            {ts_code: {open, close, high, low, is_limit_up, is_limit_down, adj_factor}}。
        """
        if not codes:
            return {}

        codes_str = "','".join(codes)
        df = self.db.query(
            f"SELECT ts_code, `open`, `close`, `high`, `low`, "
            f"is_limit_up, is_limit_down, adj_factor "
            f"FROM daily_price "
            f"WHERE trade_date = '{trade_date}' "
            f"AND ts_code IN ('{codes_str}')"
        )

        result = {}
        for _, row in df.iterrows():
            close_px = row["close"]
            result[row["ts_code"]] = {
                "open": row.get("open", close_px),
                "close": close_px,
                "high": row.get("high", close_px),
                "low": row.get("low", close_px),
                "is_limit_up": row.get("is_limit_up", 0) == 1,
                "is_limit_down": row.get("is_limit_down", 0) == 1,
                "adj_factor": row.get("adj_factor"),
            }
        return result

    def _get_adj_factors(self, trade_date: str, codes: list[str]) -> dict:
        """获取复权因子。"""
        if not codes:
            return {}
        codes_str = "','".join(codes)
        df = self.db.query(
            f"SELECT ts_code, adj_factor FROM daily_price "
            f"WHERE trade_date = '{trade_date}' "
            f"AND ts_code IN ('{codes_str}')"
        )
        return dict(zip(df["ts_code"], df["adj_factor"]))

    def _get_trade_dates(self, start_date: str, end_date: str) -> list[str]:
        """获取交易日序列（字符串列表）。"""
        df = self.db.query(
            f"SELECT DISTINCT trade_date FROM daily_price "
            f"WHERE trade_date >= '{start_date}' "
            f"AND trade_date <= '{end_date}' "
            f"ORDER BY trade_date"
        )
        if df.empty:
            return []
        return [str(d) for d in pd.to_datetime(df["trade_date"]).dt.date]

    def _calc_fees(self, amount: float, direction: str) -> dict:
        """
        计算交易费用。

        Returns:
            {commission, stamp_tax, total_cost}。
        """
        if direction == "BUY":
            commission = max(amount * self.buy_commission, MIN_COMMISSION)
            stamp_tax = 0.0
        else:
            commission = max(amount * self.sell_commission, MIN_COMMISSION)
            stamp_tax = amount * self.stamp_tax

        total_cost = commission + stamp_tax
        return {
            "commission": round(commission, 2),
            "stamp_tax": round(stamp_tax, 2),
            "total_cost": round(total_cost, 2),
        }

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

    @staticmethod
    def _round_to_lot(volume: float) -> int:
        """向下取整到 100 股整手。"""
        return int(volume // LOT_SIZE) * LOT_SIZE

    @staticmethod
    def _parse_date(date_str: str) -> date:
        """将日期字符串转为 date 对象。"""
        if isinstance(date_str, date):
            return date_str
        return datetime.strptime(str(date_str)[:10], "%Y-%m-%d").date()
