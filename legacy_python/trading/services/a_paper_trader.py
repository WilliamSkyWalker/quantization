"""
A 股本地模拟盘交易执行器（Django ORM 版）

迁自 services/execution/paper_trader.py，持久化到 PostgreSQL 的 paper_*
表（APaperAccount / APaperPosition / APaperTransaction / APaperNav）。

关键实现：
    - T+1 回放模式（信号日收盘产生 → 次交易日开盘执行）
    - 开盘价成交 + 涨跌停/一字板阻断 + 跌停排队
    - 佣金 + 印花税 + 滑点建模
    - adj_factor 除权除息自动调仓
"""

import logging
from datetime import date, datetime
from typing import Optional

import pandas as pd
from django.db import transaction
from django.db.models import Max

from services.config import (
    BUY_COMMISSION,
    LOG_LEVEL,
    PAPER_ACCOUNT_NAME,
    PAPER_INITIAL_CAPITAL,
    SELL_COMMISSION,
    SLIPPAGE,
    STAMP_TAX,
)
from stocks.models import (
    ADailyPrice,
    APaperAccount,
    APaperNav,
    APaperPosition,
    APaperTransaction,
)
from trading.services.base_trader import BaseTrader

logger = logging.getLogger(__name__)
logger.setLevel(LOG_LEVEL)

# A 股最小交易单位
LOT_SIZE = 100
# 最低佣金（元）
MIN_COMMISSION = 5.0


class PaperTrader(BaseTrader):
    """A 股模拟盘执行器。"""

    def __init__(
        self,
        db=None,
        account_name: str = PAPER_ACCOUNT_NAME,
        buy_commission: float = BUY_COMMISSION,
        sell_commission: float = SELL_COMMISSION,
        stamp_tax: float = STAMP_TAX,
        slippage: float = SLIPPAGE,
    ):
        # db 参数保留兼容性，内部全部用 Django ORM
        self.db = db
        self.account_name = account_name
        self.buy_commission = buy_commission
        self.sell_commission = sell_commission
        self.stamp_tax = stamp_tax
        self.slippage = slippage
        self.connected = False
        self._initial_capital = 0.0

    # ----------------------------------------------------------
    # 连接
    # ----------------------------------------------------------

    def connect(self, initial_capital: float = PAPER_INITIAL_CAPITAL, **kwargs):
        """加载或创建账户。"""
        account = APaperAccount.objects.filter(account_name=self.account_name).first()
        if account:
            self._initial_capital = account.initial_capital
            logger.info(
                f"加载模拟账户 [{self.account_name}]: "
                f"总资产={account.total_assets:,.2f}, 现金={account.cash:,.2f}"
            )
        else:
            APaperAccount.objects.create(
                account_name=self.account_name,
                initial_capital=initial_capital,
                cash=initial_capital,
                total_assets=initial_capital,
            )
            self._initial_capital = initial_capital
            logger.info(
                f"创建模拟账户 [{self.account_name}]: 初始资金={initial_capital:,.2f}"
            )
        self.connected = True

    def _ensure_connected(self):
        if not self.connected:
            raise ConnectionError("请先调用 connect() 初始化模拟账户")

    # ----------------------------------------------------------
    # 查询
    # ----------------------------------------------------------

    def get_account_info(self) -> dict:
        self._ensure_connected()
        account = APaperAccount.objects.filter(account_name=self.account_name).first()
        if not account:
            logger.warning("get_account_info: 账户不存在")
            return {}
        return {
            "total_assets": float(account.total_assets),
            "available_cash": float(account.cash),
            "market_value": float(account.total_assets) - float(account.cash),
            "pnl": float(account.total_assets) - float(account.initial_capital),
        }

    def get_current_positions(self) -> pd.DataFrame:
        self._ensure_connected()
        rows = list(APaperPosition.objects.filter(account_name=self.account_name).values(
            "ts_code", "volume", "market_value", "cost_basis",
        ))
        if not rows:
            return pd.DataFrame(columns=["ts_code", "volume", "market_value", "cost"])
        df = pd.DataFrame(rows)
        df = df.rename(columns={"cost_basis": "cost"})
        return df

    # ----------------------------------------------------------
    # 同步持仓
    # ----------------------------------------------------------

    def sync_position(self, target_weights: pd.DataFrame, **kwargs) -> dict:
        self._ensure_connected()
        trade_date = kwargs.get("trade_date")
        if trade_date is None:
            latest = ADailyPrice.objects.aggregate(m=Max("trade_date"))["m"]
            trade_date = latest.strftime("%Y-%m-%d") if latest else None
        if trade_date is None:
            logger.error("无法获取交易日期")
            return {"success": 0, "failed": 0, "skipped": 0}
        return self._execute_rebalance(str(trade_date), target_weights)

    def order_target_percent(self, ts_code: str, target_percent: float) -> bool:
        self._ensure_connected()
        target_weights = pd.DataFrame({
            "ts_code": [ts_code], "weight": [target_percent],
        })
        result = self.sync_position(target_weights)
        return result.get("failed", 0) == 0

    # ----------------------------------------------------------
    # 核心调仓逻辑
    # ----------------------------------------------------------

    @transaction.atomic
    def _execute_rebalance(
        self, trade_date: str, target_weights: pd.DataFrame
    ) -> dict:
        """执行一次调仓。"""
        account = APaperAccount.objects.select_for_update().get(account_name=self.account_name)

        positions = list(APaperPosition.objects.filter(account_name=self.account_name))
        pos_map = {p.ts_code: p for p in positions}

        target = dict(zip(target_weights["ts_code"], target_weights["weight"]))
        all_codes = set(list(pos_map.keys()) + list(target.keys()))
        if not all_codes:
            return {"success": 0, "failed": 0, "skipped": 0}

        price_info = self._get_price_info(trade_date, list(all_codes))

        # 计算市值（开盘价估算）
        market_value = 0.0
        for code, pos in pos_map.items():
            info = price_info.get(code)
            if info:
                open_px = info.get("open") or info["close"]
                market_value += pos.volume * open_px
            else:
                market_value += pos.market_value or 0

        total_value = float(account.cash) + market_value

        sell_orders = []
        buy_orders = []

        for code in all_codes:
            current_vol = pos_map[code].volume if code in pos_map else 0
            target_w = target.get(code, 0)
            info = price_info.get(code)
            if not info:
                logger.debug(f"_execute_rebalance: {code} 无行情，跳过")
                continue
            open_px = info.get("open") or info["close"]
            if open_px <= 0:
                logger.debug(f"_execute_rebalance: {code} 开盘价无效")
                continue
            target_vol = self._round_to_lot(target_w * total_value / open_px)
            delta = target_vol - current_vol
            if abs(delta) < LOT_SIZE:
                logger.debug(f"_execute_rebalance: {code} 变动({delta})<一手，跳过")
                continue
            if delta < 0:
                sell_orders.append((code, abs(delta), info))
            else:
                buy_orders.append((code, delta, target_w, info))

        results = {"success": 0, "failed": 0, "skipped": 0}
        failed_sells = {}
        trade_dt = self._parse_date(trade_date)

        # 先卖
        for code, volume, info in sell_orders:
            one_char_up, one_char_down = self._is_one_char_limit(info)
            if one_char_down or info.get("is_limit_down"):
                self._log_blocked_trade(trade_dt, code, "SELL", volume,
                                        info.get("open") or info["close"],
                                        "limit_down_blocked")
                failed_sells[code] = volume
                results["failed"] += 1
                continue
            self._execute_sell(account, pos_map, trade_dt, code, volume,
                               info.get("open") or info["close"])
            results["success"] += 1

        # 后买（权重降序）
        buy_orders.sort(key=lambda x: x[2], reverse=True)
        for code, volume, weight, info in buy_orders:
            one_char_up, _ = self._is_one_char_limit(info)
            if info.get("is_limit_up") or one_char_up:
                self._log_blocked_trade(trade_dt, code, "BUY", volume,
                                        info.get("open") or info["close"],
                                        "limit_up_blocked")
                results["failed"] += 1
                continue
            filled = self._execute_buy(account, pos_map, trade_dt, code, volume,
                                       info.get("open") or info["close"])
            if filled > 0:
                results["success"] += 1
            elif filled == 0:
                results["failed"] += 1

        account.save()

        results["failed_sells"] = failed_sells
        logger.info(
            f"调仓完成 [{trade_date}]: 成功={results['success']}, "
            f"失败={results['failed']}, 跳过={results['skipped']}"
        )
        return results

    # ----------------------------------------------------------
    # 买卖执行
    # ----------------------------------------------------------

    def _execute_sell(self, account, pos_map, trade_date, ts_code, volume, base_price):
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

        account.cash = float(account.cash) + amount - fees["total_cost"]

        pos.volume -= actual_vol
        if pos.volume <= 0:
            pos.delete()
            del pos_map[ts_code]
        else:
            pos.current_price = base_price
            pos.market_value = pos.volume * base_price
            pos.save()

        APaperTransaction.objects.create(
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
        )

    def _execute_buy(self, account, pos_map, trade_date, ts_code, volume, base_price) -> int:
        exec_price = round(base_price * (1 + self.slippage), 2)
        cost_per_share = exec_price * (1 + self.buy_commission)
        max_affordable = self._round_to_lot(float(account.cash) / cost_per_share)
        actual_vol = min(volume, max_affordable)
        actual_vol = self._round_to_lot(actual_vol)
        if actual_vol < LOT_SIZE:
            self._log_blocked_trade(trade_date, ts_code, "BUY", volume, base_price,
                                    "insufficient_cash")
            return 0
        amount = actual_vol * exec_price
        fees = self._calc_fees(amount, "BUY")

        account.cash = float(account.cash) - (amount + fees["total_cost"])

        pos = pos_map.get(ts_code)
        if pos:
            old_cost_total = pos.volume * (pos.cost_basis or 0)
            new_cost_total = amount + fees["total_cost"]
            pos.cost_basis = (old_cost_total + new_cost_total) / (pos.volume + actual_vol)
            pos.volume += actual_vol
            pos.current_price = base_price
            pos.market_value = pos.volume * base_price
            pos.save()
        else:
            new_pos = APaperPosition.objects.create(
                account_name=self.account_name,
                ts_code=ts_code,
                volume=actual_vol,
                cost_basis=(amount + fees["total_cost"]) / actual_vol,
                current_price=base_price,
                market_value=actual_vol * base_price,
            )
            pos_map[ts_code] = new_pos

        reason = "rebalance" if actual_vol >= volume else "rebalance_partial"
        APaperTransaction.objects.create(
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
        )
        return actual_vol

    def _log_blocked_trade(self, trade_date, ts_code, direction, volume, price, reason):
        APaperTransaction.objects.create(
            account_name=self.account_name,
            trade_date=trade_date,
            ts_code=ts_code,
            direction=direction,
            target_volume=volume,
            filled_volume=0,
            price=price,
            amount=0,
            commission=0, stamp_tax=0, slippage_cost=0, total_cost=0,
            reason=reason,
        )
        logger.debug(f"{ts_code} {direction} 阻断: {reason}")

    # ----------------------------------------------------------
    # 回放
    # ----------------------------------------------------------

    def replay(
        self,
        signals: dict[str, pd.DataFrame],
        start_date: str,
        end_date: str,
    ):
        """历史信号回放（T+1 执行）。"""
        self._ensure_connected()

        trade_dates = self._get_trade_dates(start_date, end_date)
        if not trade_dates:
            logger.warning("无交易日数据")
            return

        signal_dates = sorted(signals.keys())
        signal_idx = 0
        prev_adj_factors = {}
        pending_signal = None
        pending_sells = {}

        logger.info(
            f"开始回放: {start_date}~{end_date}, "
            f"{len(trade_dates)} 个交易日, {len(signal_dates)} 次调仓"
        )

        for trade_date in trade_dates:
            date_str = trade_date if isinstance(trade_date, str) else str(trade_date)

            # 除权除息处理
            prev_adj_factors = self._apply_corporate_actions(date_str, prev_adj_factors)

            # 排队卖单优先
            if pending_sells:
                pending_sells = self._process_pending_sells(date_str, pending_sells)

            # T+1 执行信号
            if pending_signal is not None:
                result = self._execute_rebalance(date_str, pending_signal)
                new_failed = result.get("failed_sells", {})
                pending_sells.update(new_failed)
                pending_signal = None

            # 今日新信号（明天执行）
            if signal_idx < len(signal_dates) and date_str >= signal_dates[signal_idx]:
                pending_signal = signals[signal_dates[signal_idx]]
                signal_idx += 1

            # 更新持仓市值 + 净值
            self._update_daily_nav(date_str)

        logger.info(f"回放完成: {len(trade_dates)} 个交易日")

    @transaction.atomic
    def _process_pending_sells(self, trade_date: str, pending_sells: dict) -> dict:
        """处理排队卖单。"""
        account = APaperAccount.objects.select_for_update().get(account_name=self.account_name)
        positions = list(APaperPosition.objects.filter(account_name=self.account_name))
        pos_map = {p.ts_code: p for p in positions}

        codes = list(pending_sells.keys())
        price_info = self._get_price_info(trade_date, codes)
        resolved = []
        trade_dt = self._parse_date(trade_date)

        for code, vol in pending_sells.items():
            info = price_info.get(code)
            if not info:
                continue
            one_char_up, one_char_down = self._is_one_char_limit(info)
            if one_char_down or info.get("is_limit_down"):
                continue
            self._execute_sell(account, pos_map, trade_dt, code, vol,
                               info.get("open") or info["close"])
            resolved.append(code)

        for code in resolved:
            del pending_sells[code]
        account.save()
        return pending_sells

    @transaction.atomic
    def _apply_corporate_actions(self, trade_date: str, prev_adj_factors: dict) -> dict:
        """除权除息（检测 adj_factor 变化）。"""
        positions = list(APaperPosition.objects.select_for_update().filter(account_name=self.account_name))
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
                    pos.cost_basis = (pos.cost_basis or 0) / adj_ratio
                pos.save()
                logger.info(
                    f"{code} 除权除息: adj_ratio={adj_ratio:.4f}, "
                    f"股数 {old_vol} -> {pos.volume}"
                )
        return current_adj

    @transaction.atomic
    def _update_daily_nav(self, trade_date: str):
        """更新持仓市值 + 记录净值。"""
        account = APaperAccount.objects.select_for_update().get(account_name=self.account_name)
        positions = list(APaperPosition.objects.select_for_update().filter(account_name=self.account_name))

        codes = [p.ts_code for p in positions]
        price_info = self._get_price_info(trade_date, codes) if codes else {}

        market_value = 0.0
        for pos in positions:
            info = price_info.get(pos.ts_code)
            if info:
                pos.current_price = info["close"]
                pos.market_value = pos.volume * info["close"]
                pos.save()
            market_value += pos.market_value or 0

        prev_assets = float(account.total_assets)
        account.total_assets = float(account.cash) + market_value
        account.save()

        trade_dt = self._parse_date(trade_date)
        nav_value = float(account.total_assets) / float(account.initial_capital)
        daily_pnl = float(account.total_assets) - prev_assets
        daily_return = daily_pnl / prev_assets if prev_assets > 0 else 0

        APaperNav.objects.update_or_create(
            account_name=self.account_name,
            trade_date=trade_dt,
            defaults={
                "cash": float(account.cash),
                "market_value": market_value,
                "total_assets": float(account.total_assets),
                "nav": nav_value,
                "daily_pnl": daily_pnl,
                "daily_return": daily_return,
                "n_holdings": len(positions),
            },
        )

    # ----------------------------------------------------------
    # 对账 + 报告
    # ----------------------------------------------------------

    def reconcile(self, target_weights: pd.DataFrame) -> pd.DataFrame:
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
                "ts_code": code, "target_weight": tgt, "actual_weight": act,
                "diff": act - tgt,
            })
        df = pd.DataFrame(records)
        if not df.empty:
            df = df.sort_values("diff", key=abs, ascending=False)
        return df

    def get_position_report(self) -> str:
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
    # 查询
    # ----------------------------------------------------------

    def get_nav_series(self) -> pd.Series:
        self._ensure_connected()
        rows = list(
            APaperNav.objects.filter(account_name=self.account_name)
            .order_by("trade_date").values("trade_date", "nav")
        )
        if not rows:
            return pd.Series(dtype=float)
        df = pd.DataFrame(rows)
        df["trade_date"] = pd.to_datetime(df["trade_date"])
        return df.set_index("trade_date")["nav"]

    def get_transactions(
        self, trade_date: Optional[str] = None, last_n: int = 50,
    ) -> pd.DataFrame:
        self._ensure_connected()
        q = APaperTransaction.objects.filter(account_name=self.account_name)
        if trade_date:
            q = q.filter(trade_date=self._parse_date(trade_date))
        q = q.order_by("-id")[:last_n]
        rows = list(q.values(
            "trade_date", "ts_code", "direction", "filled_volume",
            "price", "amount", "total_cost", "reason",
        ))
        return pd.DataFrame(rows) if rows else pd.DataFrame()

    def get_nav_history(self, last_n: int = 20) -> pd.DataFrame:
        self._ensure_connected()
        rows = list(
            APaperNav.objects.filter(account_name=self.account_name)
            .order_by("-trade_date").values(
                "trade_date", "cash", "market_value", "total_assets", "nav",
                "daily_pnl", "daily_return", "n_holdings",
            )[:last_n]
        )
        return pd.DataFrame(rows) if rows else pd.DataFrame()

    # ----------------------------------------------------------
    # 账户管理
    # ----------------------------------------------------------

    @transaction.atomic
    def reset_account(self):
        self._ensure_connected()
        APaperPosition.objects.filter(account_name=self.account_name).delete()
        APaperTransaction.objects.filter(account_name=self.account_name).delete()
        APaperNav.objects.filter(account_name=self.account_name).delete()
        account = APaperAccount.objects.select_for_update().get(account_name=self.account_name)
        account.initial_capital = PAPER_INITIAL_CAPITAL
        account.cash = PAPER_INITIAL_CAPITAL
        account.total_assets = PAPER_INITIAL_CAPITAL
        account.save()
        logger.info(f"模拟账户 [{self.account_name}] 已重置")

    # ----------------------------------------------------------
    # 辅助
    # ----------------------------------------------------------

    def _get_price_info(self, trade_date: str, codes: list[str]) -> dict:
        if not codes:
            return {}
        d = self._parse_date(trade_date)
        rows = ADailyPrice.objects.filter(
            ts_code__in=codes, trade_date=d,
        ).values("ts_code", "open", "close", "high", "low",
                 "is_limit_up", "is_limit_down", "adj_factor")
        result = {}
        for row in rows:
            close_px = row["close"]
            result[row["ts_code"]] = {
                "open": row.get("open") or close_px,
                "close": close_px,
                "high": row.get("high") or close_px,
                "low": row.get("low") or close_px,
                "is_limit_up": row.get("is_limit_up") == 1,
                "is_limit_down": row.get("is_limit_down") == 1,
                "adj_factor": row.get("adj_factor"),
            }
        return result

    def _get_adj_factors(self, trade_date: str, codes: list[str]) -> dict:
        if not codes:
            return {}
        d = self._parse_date(trade_date)
        return {
            r["ts_code"]: r["adj_factor"]
            for r in ADailyPrice.objects.filter(
                ts_code__in=codes, trade_date=d,
            ).values("ts_code", "adj_factor")
        }

    def _get_trade_dates(self, start_date: str, end_date: str) -> list[str]:
        s = self._parse_date(start_date)
        e = self._parse_date(end_date)
        dates = list(
            ADailyPrice.objects.filter(trade_date__gte=s, trade_date__lte=e)
            .values_list("trade_date", flat=True).distinct().order_by("trade_date")
        )
        return [d.strftime("%Y-%m-%d") for d in dates]

    def _calc_fees(self, amount: float, direction: str) -> dict:
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
        o, h, l, c = info["open"], info["high"], info["low"], info["close"]
        if o == h == l == c:
            return (info["is_limit_up"], info["is_limit_down"])
        return (False, False)

    @staticmethod
    def _round_to_lot(volume: float) -> int:
        return int(volume // LOT_SIZE) * LOT_SIZE

    @staticmethod
    def _parse_date(date_str) -> date:
        if isinstance(date_str, date):
            return date_str
        return datetime.strptime(str(date_str)[:10], "%Y-%m-%d").date()
