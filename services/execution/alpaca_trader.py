"""
Alpaca Markets paper/live trading executor

Connects to Alpaca API for real paper trading with live market data.
Implements the same interface as USPaperTrader for seamless switching.

Usage:
    trader = AlpacaTrader(db)
    trader.connect()
    trader.sync_position(target_weights_df)
    print(trader.get_account_info())

Config (.env):
    ALPACA_API_KEY=xxx
    ALPACA_SECRET_KEY=xxx
    ALPACA_PAPER=true
"""

import logging
import time
from datetime import date, datetime
from typing import Optional

import pandas as pd

from services.config import LOG_LEVEL

logger = logging.getLogger(__name__)
logger.setLevel(LOG_LEVEL)

# Alpaca API 限流：200 次/分钟，保守间隔
_ORDER_DELAY_SEC = 0.35


def _check_alpaca():
    try:
        import alpaca
        return alpaca
    except ImportError:
        raise ImportError("alpaca-py 未安装，请运行: pip install alpaca-py")


class AlpacaTrader:
    """
    Alpaca Markets trading executor.

    Supports both paper and live trading via API.
    State is persisted to local MySQL (NAV snapshots) + Alpaca cloud (positions/orders).
    """

    def __init__(
        self,
        db,
        api_key: str = None,
        secret_key: str = None,
        paper: bool = True,
    ):
        from services.config import (
            ALPACA_API_KEY,
            ALPACA_SECRET_KEY,
            ALPACA_PAPER,
        )

        self.db = db
        self._api_key = api_key or ALPACA_API_KEY
        self._secret_key = secret_key or ALPACA_SECRET_KEY
        self._paper = paper if api_key else ALPACA_PAPER
        self._client = None
        self.connected = False

    # ----------------------------------------------------------
    # Connect
    # ----------------------------------------------------------

    def connect(self, **kwargs):
        """Initialize Alpaca TradingClient."""
        _check_alpaca()
        from alpaca.trading.client import TradingClient

        if not self._api_key or not self._secret_key:
            raise ValueError(
                "Alpaca API keys not configured. "
                "Set ALPACA_API_KEY and ALPACA_SECRET_KEY in .env"
            )

        self._client = TradingClient(
            self._api_key,
            self._secret_key,
            paper=self._paper,
        )

        # Verify connection
        account = self._client.get_account()
        mode = "Paper" if self._paper else "Live"
        logger.info(
            f"Alpaca {mode} connected: "
            f"equity=${float(account.equity):,.2f}, "
            f"cash=${float(account.cash):,.2f}, "
            f"buying_power=${float(account.buying_power):,.2f}"
        )
        self.connected = True

    def _ensure_connected(self):
        if not self.connected or self._client is None:
            raise ConnectionError("Call connect() first")

    # ----------------------------------------------------------
    # Account & Position Queries
    # ----------------------------------------------------------

    def get_account_info(self) -> dict:
        """Get Alpaca account status."""
        self._ensure_connected()
        account = self._client.get_account()
        equity = float(account.equity)
        cash = float(account.cash)
        last_equity = float(account.last_equity)
        return {
            "total_assets": equity,
            "available_cash": cash,
            "market_value": equity - cash,
            "buying_power": float(account.buying_power),
            "pnl": equity - last_equity,
            "initial_capital": last_equity,
            "pdt_flag": account.pattern_day_trader,
        }

    def get_current_positions(self) -> pd.DataFrame:
        """Get current Alpaca positions as DataFrame."""
        self._ensure_connected()
        positions = self._client.get_all_positions()
        if not positions:
            logger.debug("get_current_positions: Alpaca 无持仓")
            return pd.DataFrame(
                columns=["ticker", "volume", "market_value", "cost_basis"]
            )
        records = []
        for pos in positions:
            records.append({
                "ticker": pos.symbol,
                "volume": int(pos.qty),
                "market_value": float(pos.market_value),
                "cost_basis": float(pos.avg_entry_price),
                "unrealized_pnl": float(pos.unrealized_pl),
                "side": pos.side.value,
            })
        return pd.DataFrame(records)

    def get_nav_history(self) -> pd.DataFrame:
        """Get local NAV history (synced from Alpaca snapshots)."""
        from services.data.database import USPaperNav

        session = self.db.get_session()
        try:
            navs = (
                session.query(USPaperNav)
                .filter(USPaperNav.account_id == -1)  # Alpaca account uses id=-1
                .order_by(USPaperNav.nav_date)
                .all()
            )
            if not navs:
                logger.debug("get_nav_history: Alpaca NAV 记录为空")
                return pd.DataFrame(columns=["nav_date", "nav", "total_assets"])
            records = [
                {
                    "nav_date": n.nav_date,
                    "nav": n.nav,
                    "total_assets": n.total_assets,
                }
                for n in navs
            ]
            return pd.DataFrame(records)
        finally:
            session.close()

    # ----------------------------------------------------------
    # Order Execution
    # ----------------------------------------------------------

    def sync_position(self, target_weights: pd.DataFrame, **kwargs) -> dict:
        """
        Rebalance Alpaca holdings to target weights.

        Args:
            target_weights: DataFrame[ticker, weight].

        Returns:
            {success, failed, skipped}.
        """
        self._ensure_connected()
        from alpaca.trading.requests import MarketOrderRequest
        from alpaca.trading.enums import OrderSide, TimeInForce

        # 1. Get account equity for weight-to-shares conversion
        account = self._client.get_account()
        equity = float(account.equity)

        # 2. Current positions
        current_positions = self._client.get_all_positions()
        pos_map = {p.symbol: p for p in current_positions}

        # 3. Build target
        target = dict(zip(target_weights["ticker"], target_weights["weight"]))
        all_tickers = set(list(pos_map.keys()) + list(target.keys()))

        if not all_tickers:
            logger.debug("sync_position: 无需调仓")
            return {"success": 0, "failed": 0, "skipped": 0}

        # 4. Calculate deltas
        sell_orders = []
        buy_orders = []

        for ticker in all_tickers:
            current_qty = int(pos_map[ticker].qty) if ticker in pos_map else 0
            current_price = (
                float(pos_map[ticker].current_price)
                if ticker in pos_map
                else None
            )
            target_w = target.get(ticker, 0)
            target_value = target_w * equity

            if current_price and current_price > 0:
                target_qty = int(target_value / current_price)
            elif target_w > 0:
                # No current price available — use notional order later
                logger.warning(
                    f"sync_position: {ticker} 无当前价格，使用 notional 下单"
                )
                target_qty = current_qty  # will handle below
                if target_w > 0 and current_qty == 0:
                    buy_orders.append(
                        (ticker, 0, target_w, None, target_value)
                    )
                    continue
            else:
                target_qty = 0

            delta = target_qty - current_qty

            if delta == 0:
                logger.debug(f"sync_position: {ticker} 无需调仓")
                continue

            if delta < 0:
                sell_orders.append((ticker, abs(delta)))
            else:
                buy_orders.append(
                    (ticker, delta, target_w, current_price, None)
                )

        results = {"success": 0, "failed": 0, "skipped": 0}

        # 5. Sell first (free up buying power)
        for ticker, qty in sell_orders:
            try:
                if qty >= (int(pos_map[ticker].qty) if ticker in pos_map else 0):
                    # Close entire position
                    self._client.close_position(ticker)
                    logger.info(f"SELL {ticker}: close all")
                else:
                    self._client.submit_order(
                        order_data=MarketOrderRequest(
                            symbol=ticker,
                            qty=qty,
                            side=OrderSide.SELL,
                            time_in_force=TimeInForce.DAY,
                        )
                    )
                    logger.info(f"SELL {ticker}: {qty} shares")
                results["success"] += 1
                time.sleep(_ORDER_DELAY_SEC)
            except Exception as e:
                logger.warning(f"SELL {ticker} failed: {e}")
                results["failed"] += 1

        # 6. Buy (in descending weight order)
        buy_orders.sort(key=lambda x: x[2], reverse=True)

        for ticker, qty, weight, price, notional in buy_orders:
            try:
                if notional and not qty:
                    # Use notional order when no price available
                    self._client.submit_order(
                        order_data=MarketOrderRequest(
                            symbol=ticker,
                            notional=round(notional, 2),
                            side=OrderSide.BUY,
                            time_in_force=TimeInForce.DAY,
                        )
                    )
                    logger.info(f"BUY {ticker}: ${notional:,.2f} notional")
                elif qty > 0:
                    self._client.submit_order(
                        order_data=MarketOrderRequest(
                            symbol=ticker,
                            qty=qty,
                            side=OrderSide.BUY,
                            time_in_force=TimeInForce.DAY,
                        )
                    )
                    logger.info(f"BUY {ticker}: {qty} shares")
                else:
                    logger.debug(f"BUY {ticker}: 0 shares, skipped")
                    results["skipped"] += 1
                    continue
                results["success"] += 1
                time.sleep(_ORDER_DELAY_SEC)
            except Exception as e:
                logger.warning(f"BUY {ticker} failed: {e}")
                results["failed"] += 1

        logger.info(
            f"Alpaca rebalance done: "
            f"success={results['success']}, "
            f"failed={results['failed']}, "
            f"skipped={results['skipped']}"
        )
        return results

    def order_target_percent(self, ticker: str, target_percent: float) -> bool:
        """
        Adjust a single position to target percent of equity.

        Returns:
            Whether the order was submitted successfully.
        """
        self._ensure_connected()
        from alpaca.trading.requests import MarketOrderRequest
        from alpaca.trading.enums import OrderSide, TimeInForce

        account = self._client.get_account()
        equity = float(account.equity)
        target_value = target_percent * equity

        # Current position
        try:
            pos = self._client.get_open_position(ticker)
            current_value = float(pos.market_value)
        except Exception:
            current_value = 0.0

        delta_value = target_value - current_value

        if abs(delta_value) < 10:  # < $10 change, skip
            logger.debug(
                f"order_target_percent: {ticker} 差额 ${delta_value:.2f} 太小，跳过"
            )
            return True

        try:
            if delta_value < 0:
                # Sell
                if target_percent == 0:
                    self._client.close_position(ticker)
                    logger.info(f"order_target_percent: {ticker} close all")
                else:
                    self._client.submit_order(
                        order_data=MarketOrderRequest(
                            symbol=ticker,
                            notional=round(abs(delta_value), 2),
                            side=OrderSide.SELL,
                            time_in_force=TimeInForce.DAY,
                        )
                    )
                    logger.info(
                        f"order_target_percent: SELL {ticker} ${abs(delta_value):,.2f}"
                    )
            else:
                # Buy
                self._client.submit_order(
                    order_data=MarketOrderRequest(
                        symbol=ticker,
                        notional=round(delta_value, 2),
                        side=OrderSide.BUY,
                        time_in_force=TimeInForce.DAY,
                    )
                )
                logger.info(
                    f"order_target_percent: BUY {ticker} ${delta_value:,.2f}"
                )
            return True
        except Exception as e:
            logger.warning(f"order_target_percent: {ticker} failed: {e}")
            return False

    # ----------------------------------------------------------
    # Reconciliation
    # ----------------------------------------------------------

    def reconcile(self, target_weights: pd.DataFrame) -> pd.DataFrame:
        """Compare target weights vs actual Alpaca positions."""
        self._ensure_connected()

        account = self._client.get_account()
        equity = float(account.equity)

        positions = self._client.get_all_positions()
        actual = {p.symbol: float(p.market_value) / equity for p in positions}

        target = dict(zip(target_weights["ticker"], target_weights["weight"]))

        all_tickers = set(list(actual.keys()) + list(target.keys()))
        records = []
        for ticker in sorted(all_tickers):
            tw = target.get(ticker, 0)
            aw = actual.get(ticker, 0)
            records.append({
                "ticker": ticker,
                "target_weight": round(tw, 4),
                "actual_weight": round(aw, 4),
                "diff": round(aw - tw, 4),
            })

        return pd.DataFrame(records)

    # ----------------------------------------------------------
    # Orders & Transactions
    # ----------------------------------------------------------

    def get_open_orders(self) -> pd.DataFrame:
        """Get all open orders from Alpaca."""
        self._ensure_connected()
        from alpaca.trading.requests import GetOrdersRequest
        from alpaca.trading.enums import QueryOrderStatus

        request = GetOrdersRequest(status=QueryOrderStatus.OPEN)
        orders = self._client.get_orders(filter=request)

        if not orders:
            logger.debug("get_open_orders: 无未成交订单")
            return pd.DataFrame(
                columns=["id", "ticker", "side", "qty", "type", "status", "submitted_at"]
            )

        records = []
        for o in orders:
            records.append({
                "id": str(o.id),
                "ticker": o.symbol,
                "side": o.side.value,
                "qty": str(o.qty),
                "type": o.order_type.value,
                "status": o.status.value,
                "submitted_at": str(o.submitted_at),
            })
        return pd.DataFrame(records)

    def get_closed_orders(self, limit: int = 50) -> pd.DataFrame:
        """Get recent closed orders from Alpaca."""
        self._ensure_connected()
        from alpaca.trading.requests import GetOrdersRequest
        from alpaca.trading.enums import QueryOrderStatus

        request = GetOrdersRequest(
            status=QueryOrderStatus.CLOSED, limit=limit
        )
        orders = self._client.get_orders(filter=request)

        if not orders:
            logger.debug("get_closed_orders: 无已成交订单")
            return pd.DataFrame()

        records = []
        for o in orders:
            records.append({
                "id": str(o.id),
                "ticker": o.symbol,
                "side": o.side.value,
                "qty": str(o.qty),
                "filled_qty": str(o.filled_qty),
                "filled_avg_price": str(o.filled_avg_price) if o.filled_avg_price else None,
                "type": o.order_type.value,
                "status": o.status.value,
                "filled_at": str(o.filled_at) if o.filled_at else None,
            })
        return pd.DataFrame(records)

    def cancel_all_orders(self):
        """Cancel all open orders."""
        self._ensure_connected()
        cancelled = self._client.cancel_orders()
        logger.info(f"Cancelled {len(cancelled)} open orders")
        return len(cancelled)

    # ----------------------------------------------------------
    # NAV Snapshot (local DB)
    # ----------------------------------------------------------

    def update_nav(self, nav_date: Optional[str] = None):
        """Record daily NAV snapshot to local database."""
        self._ensure_connected()
        from services.data.database import USPaperNav

        account = self._client.get_account()
        equity = float(account.equity)
        last_equity = float(account.last_equity)
        nav_value = equity / last_equity if last_equity > 0 else 1.0

        dt = (
            datetime.strptime(str(nav_date)[:10], "%Y-%m-%d").date()
            if nav_date
            else date.today()
        )

        session = self.db.get_session()
        try:
            existing = (
                session.query(USPaperNav)
                .filter_by(account_id=-1, nav_date=dt)
                .first()
            )
            if existing:
                existing.nav = nav_value
                existing.total_assets = equity
            else:
                session.add(
                    USPaperNav(
                        account_id=-1,
                        nav_date=dt,
                        nav=nav_value,
                        total_assets=equity,
                    )
                )
            session.commit()
            logger.info(
                f"Alpaca NAV snapshot [{dt}]: equity=${equity:,.2f}, nav={nav_value:.4f}"
            )
        finally:
            session.close()

    # ----------------------------------------------------------
    # Close All / Reset
    # ----------------------------------------------------------

    def close_all_positions(self):
        """Close all positions and cancel all open orders."""
        self._ensure_connected()
        self._client.cancel_orders()
        self._client.close_all_positions(cancel_orders=True)
        logger.info("Alpaca: all positions closed, all orders cancelled")

    def reset(self):
        """
        Reset Alpaca paper account.

        Note: Alpaca paper accounts cannot be reset via API.
        This closes all positions and cancels orders.
        """
        self.close_all_positions()
        logger.info(
            "Alpaca paper reset: positions closed. "
            "To reset capital, create a new paper account at https://app.alpaca.markets/"
        )

    # ----------------------------------------------------------
    # Position Report
    # ----------------------------------------------------------

    def get_position_report(self) -> str:
        """Generate a formatted position report."""
        self._ensure_connected()
        account = self._client.get_account()
        positions = self._client.get_all_positions()

        lines = [
            f"=== Alpaca {'Paper' if self._paper else 'Live'} Account ===",
            f"Equity:       ${float(account.equity):>12,.2f}",
            f"Cash:         ${float(account.cash):>12,.2f}",
            f"Buying Power: ${float(account.buying_power):>12,.2f}",
            f"Day P&L:      ${float(account.equity) - float(account.last_equity):>12,.2f}",
            "",
            f"Positions ({len(positions)}):",
        ]

        if positions:
            lines.append(
                f"{'Symbol':<8} {'Qty':>6} {'AvgCost':>10} {'MktVal':>12} {'P&L':>10} {'P&L%':>8}"
            )
            lines.append("-" * 60)
            for p in sorted(positions, key=lambda x: float(x.market_value), reverse=True):
                pnl_pct = float(p.unrealized_plpc) * 100
                lines.append(
                    f"{p.symbol:<8} {int(p.qty):>6} "
                    f"${float(p.avg_entry_price):>9,.2f} "
                    f"${float(p.market_value):>11,.2f} "
                    f"${float(p.unrealized_pl):>9,.2f} "
                    f"{pnl_pct:>7.1f}%"
                )
        else:
            lines.append("  (empty)")

        return "\n".join(lines)
