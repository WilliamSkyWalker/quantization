"""
US stock paper trader

Simulates US stock trading with:
    - T+0 settlement (can buy and sell same day)
    - Lot size = 1 share (no 100-share constraint)
    - Zero commission, zero stamp tax, slippage only (default 5bps)
    - Prices from us_daily_price.adj_close

Usage:
    trader = USPaperTrader(db)
    trader.connect(initial_capital=100_000)
    trader.sync_position(target_weights_df)
    print(trader.get_account_info())
"""

import logging
from datetime import date, datetime
from typing import Optional

import pandas as pd

from services.config import US_INITIAL_CAPITAL, US_SLIPPAGE, LOG_LEVEL
from services.data.database import (
    DatabaseManager,
    USPaperAccount,
    USPaperPosition,
    USPaperTransaction,
    USPaperNav,
)

logger = logging.getLogger(__name__)
logger.setLevel(LOG_LEVEL)


class USPaperTrader:
    """
    US stock paper trading executor.

    All state is persisted to MySQL via DatabaseManager.
    """

    def __init__(
        self,
        db: DatabaseManager,
        account_name: str = "default",
        slippage: float = US_SLIPPAGE,
    ):
        self.db = db
        self.account_name = account_name
        self.slippage = slippage
        self.connected = False
        self._account_id: Optional[int] = None
        self._initial_capital = 0.0

    # ----------------------------------------------------------
    # Connect / Initialize
    # ----------------------------------------------------------

    def connect(self, initial_capital: Optional[float] = None):
        """
        Load or create a paper trading account.

        If the account already exists, load it. Otherwise create a new one.

        Args:
            initial_capital: Starting capital (only used when creating a new account).
        """
        if initial_capital is None:
            initial_capital = US_INITIAL_CAPITAL

        session = self.db.get_session()
        try:
            account = (
                session.query(USPaperAccount)
                .filter_by(account_name=self.account_name)
                .first()
            )

            if account:
                self._account_id = account.id
                self._initial_capital = account.initial_capital
                logger.info(
                    f"Loaded US paper account [{self.account_name}]: "
                    f"total_assets={account.total_assets:,.2f}, "
                    f"cash={account.cash:,.2f}"
                )
            else:
                account = USPaperAccount(
                    account_name=self.account_name,
                    initial_capital=initial_capital,
                    cash=initial_capital,
                    total_assets=initial_capital,
                )
                session.add(account)
                session.commit()
                self._account_id = account.id
                self._initial_capital = initial_capital
                logger.info(
                    f"Created US paper account [{self.account_name}]: "
                    f"initial_capital={initial_capital:,.2f}"
                )

            self.connected = True
        finally:
            session.close()

    def _ensure_connected(self):
        """Ensure account is connected."""
        if not self.connected:
            raise ConnectionError("Call connect() first to initialize the account")

    # ----------------------------------------------------------
    # Account & Position Queries
    # ----------------------------------------------------------

    def get_account_info(self) -> dict:
        """Get current account status."""
        self._ensure_connected()
        session = self.db.get_session()
        try:
            account = (
                session.query(USPaperAccount)
                .filter_by(account_name=self.account_name)
                .first()
            )
            if not account:
                logger.warning("get_account_info: 未找到美股模拟账户")
                return {}
            return {
                "total_assets": account.total_assets,
                "available_cash": account.cash,
                "market_value": account.total_assets - account.cash,
                "pnl": account.total_assets - account.initial_capital,
                "initial_capital": account.initial_capital,
            }
        finally:
            session.close()

    def get_current_positions(self) -> pd.DataFrame:
        """Get current holdings DataFrame[ticker, volume, market_value, cost_basis]."""
        self._ensure_connected()
        session = self.db.get_session()
        try:
            positions = (
                session.query(USPaperPosition)
                .filter_by(account_id=self._account_id)
                .all()
            )
            if not positions:
                logger.debug("get_current_positions: 当前无美股持仓")
                return pd.DataFrame(
                    columns=["ticker", "volume", "market_value", "cost_basis"]
                )
            records = []
            for pos in positions:
                records.append(
                    {
                        "ticker": pos.ticker,
                        "volume": pos.volume,
                        "market_value": pos.market_value or 0,
                        "cost_basis": pos.cost_basis,
                    }
                )
            return pd.DataFrame(records)
        finally:
            session.close()

    def get_nav_history(self) -> pd.DataFrame:
        """Get NAV time series."""
        self._ensure_connected()
        df = self.db.query(
            f"SELECT nav_date, nav, total_assets "
            f"FROM us_paper_nav "
            f"WHERE account_id = {self._account_id} "
            f"ORDER BY nav_date"
        )
        return df

    # ----------------------------------------------------------
    # Trade Execution
    # ----------------------------------------------------------

    def sync_position(self, target_weights: pd.DataFrame, **kwargs) -> dict:
        """
        Rebalance holdings to target weights.

        Args:
            target_weights: DataFrame[ticker, weight].
            trade_date: Optional trade date string (default: today).

        Returns:
            {success, failed, skipped}.
        """
        self._ensure_connected()

        trade_date = kwargs.get("trade_date")
        if trade_date is None:
            trade_date = date.today().strftime("%Y-%m-%d")

        return self._execute_rebalance(trade_date, target_weights)

    def _execute_rebalance(
        self, trade_date: str, target_weights: pd.DataFrame
    ) -> dict:
        """
        Execute a single rebalance.

        Args:
            trade_date: Trade date string.
            target_weights: DataFrame[ticker, weight].

        Returns:
            {success, failed, skipped}.
        """
        session = self.db.get_session()
        try:
            account = (
                session.query(USPaperAccount)
                .filter_by(id=self._account_id)
                .first()
            )

            # Current positions
            positions = (
                session.query(USPaperPosition)
                .filter_by(account_id=self._account_id)
                .all()
            )
            pos_map = {p.ticker: p for p in positions}

            # Target weights
            target = dict(zip(target_weights["ticker"], target_weights["weight"]))
            all_tickers = set(list(pos_map.keys()) + list(target.keys()))

            if not all_tickers:
                logger.debug("_execute_rebalance: 无需调仓的股票")
                return {"success": 0, "failed": 0, "skipped": 0}

            # Get latest prices
            prices = self._get_latest_prices(list(all_tickers))

            # Calculate total asset value using current prices
            market_value = 0.0
            for ticker, pos in pos_map.items():
                px = prices.get(ticker)
                if px:
                    market_value += pos.volume * px
                else:
                    market_value += pos.market_value or 0
            total_value = account.cash + market_value

            # Build sell and buy order lists
            sell_orders = []
            buy_orders = []

            for ticker in all_tickers:
                current_vol = pos_map[ticker].volume if ticker in pos_map else 0
                target_w = target.get(ticker, 0)
                px = prices.get(ticker)

                if not px or px <= 0:
                    logger.debug(f"_execute_rebalance: {ticker} 无有效价格，跳过")
                    continue

                target_vol = int(target_w * total_value / px)
                delta = target_vol - current_vol

                if delta == 0:
                    logger.debug(f"_execute_rebalance: {ticker} 目标股数与当前相同，跳过")
                    continue

                if delta < 0:
                    sell_orders.append((ticker, abs(delta), px))
                else:
                    buy_orders.append((ticker, delta, target_w, px))

            results = {"success": 0, "failed": 0, "skipped": 0}
            trade_dt = self._parse_date(trade_date)

            # Sell first, then buy
            for ticker, volume, px in sell_orders:
                self._execute_sell(session, account, pos_map, trade_dt, ticker, volume, px)
                results["success"] += 1

            # Buy in descending weight order (prioritize larger weights)
            buy_orders.sort(key=lambda x: x[2], reverse=True)

            for ticker, volume, weight, px in buy_orders:
                filled = self._execute_buy(
                    session, account, pos_map, trade_dt, ticker, volume, px
                )
                if filled > 0:
                    results["success"] += 1
                else:
                    results["failed"] += 1

            session.commit()

            logger.info(
                f"US rebalance done [{trade_date}]: "
                f"success={results['success']}, "
                f"failed={results['failed']}, "
                f"skipped={results['skipped']}"
            )
            return results

        except Exception as e:
            session.rollback()
            logger.error(f"US rebalance failed: {e}")
            raise
        finally:
            session.close()

    def _execute_sell(
        self, session, account, pos_map, trade_date, ticker, volume, base_price
    ):
        """Execute a sell order."""
        pos = pos_map.get(ticker)
        if not pos:
            logger.debug(f"_execute_sell: {ticker} 无持仓，跳过卖出")
            return

        actual_vol = min(volume, pos.volume)
        if actual_vol <= 0:
            logger.debug(f"_execute_sell: {ticker} 卖出量为0，跳过")
            return

        exec_price = round(base_price * (1 - self.slippage), 4)
        amount = actual_vol * exec_price

        # Update cash (no commission/tax for US, only slippage)
        account.cash += amount

        # Update position
        pos.volume -= actual_vol
        if pos.volume <= 0:
            session.delete(pos)
            del pos_map[ticker]
        else:
            pos.market_value = pos.volume * base_price

        # Record transaction
        session.add(
            USPaperTransaction(
                account_id=self._account_id,
                ticker=ticker,
                direction="SELL",
                volume=actual_vol,
                price=exec_price,
                amount=amount,
                fees=0,
                trade_date=trade_date,
            )
        )

    def _execute_buy(
        self, session, account, pos_map, trade_date, ticker, volume, base_price
    ) -> int:
        """
        Execute a buy order.

        Returns:
            Number of shares actually filled.
        """
        exec_price = round(base_price * (1 + self.slippage), 4)

        # Check if we have enough cash; if not, buy what we can
        max_affordable = int(account.cash / exec_price) if exec_price > 0 else 0
        actual_vol = min(volume, max_affordable)

        if actual_vol <= 0:
            logger.debug(f"{ticker} BUY blocked: insufficient cash")
            return 0

        amount = actual_vol * exec_price

        # Update cash
        account.cash -= amount

        # Update position
        pos = pos_map.get(ticker)
        if pos:
            old_cost_total = pos.volume * pos.cost_basis
            new_cost_total = amount
            pos.cost_basis = (old_cost_total + new_cost_total) / (pos.volume + actual_vol)
            pos.volume += actual_vol
            pos.market_value = pos.volume * base_price
        else:
            new_pos = USPaperPosition(
                account_id=self._account_id,
                ticker=ticker,
                volume=actual_vol,
                cost_basis=exec_price,
                market_value=actual_vol * base_price,
            )
            session.add(new_pos)
            pos_map[ticker] = new_pos

        # Record transaction
        session.add(
            USPaperTransaction(
                account_id=self._account_id,
                ticker=ticker,
                direction="BUY",
                volume=actual_vol,
                price=exec_price,
                amount=amount,
                fees=0,
                trade_date=trade_date,
            )
        )

        return actual_vol

    # ----------------------------------------------------------
    # Daily NAV Update
    # ----------------------------------------------------------

    def update_nav(self, nav_date: Optional[str] = None):
        """Update position market values and record daily NAV snapshot."""
        self._ensure_connected()
        session = self.db.get_session()
        try:
            account = (
                session.query(USPaperAccount)
                .filter_by(id=self._account_id)
                .first()
            )

            positions = (
                session.query(USPaperPosition)
                .filter_by(account_id=self._account_id)
                .all()
            )

            tickers = [p.ticker for p in positions]
            prices = self._get_latest_prices(tickers) if tickers else {}

            market_value = 0.0
            for pos in positions:
                px = prices.get(pos.ticker)
                if px:
                    pos.market_value = pos.volume * px
                market_value += pos.market_value or 0

            account.total_assets = account.cash + market_value

            # Record NAV
            dt = self._parse_date(nav_date) if nav_date else date.today()
            nav_value = account.total_assets / account.initial_capital if account.initial_capital > 0 else 1.0

            existing = (
                session.query(USPaperNav)
                .filter_by(account_id=self._account_id, nav_date=dt)
                .first()
            )

            if existing:
                existing.nav = nav_value
                existing.total_assets = account.total_assets
            else:
                session.add(
                    USPaperNav(
                        account_id=self._account_id,
                        nav_date=dt,
                        nav=nav_value,
                        total_assets=account.total_assets,
                    )
                )

            session.commit()
            logger.info(
                f"US NAV updated [{dt}]: total_assets={account.total_assets:,.2f}, nav={nav_value:.4f}"
            )

        finally:
            session.close()

    # ----------------------------------------------------------
    # Reset
    # ----------------------------------------------------------

    def reset(self):
        """Reset account: clear positions, transactions, NAV; restore initial capital."""
        self._ensure_connected()
        session = self.db.get_session()
        try:
            session.query(USPaperPosition).filter_by(
                account_id=self._account_id
            ).delete()
            session.query(USPaperTransaction).filter_by(
                account_id=self._account_id
            ).delete()
            session.query(USPaperNav).filter_by(
                account_id=self._account_id
            ).delete()

            account = (
                session.query(USPaperAccount)
                .filter_by(id=self._account_id)
                .first()
            )
            if account:
                account.cash = account.initial_capital
                account.total_assets = account.initial_capital

            session.commit()
            logger.info(f"US paper account [{self.account_name}] has been reset")
        finally:
            session.close()

    # ----------------------------------------------------------
    # Helpers
    # ----------------------------------------------------------

    def _get_latest_prices(self, tickers: list[str]) -> dict[str, float]:
        """Get latest adj_close for each ticker from us_daily_price."""
        if not tickers:
            logger.debug("_get_latest_prices: 无ticker，返回空字典")
            return {}

        tickers_str = "','".join(tickers)
        df = self.db.query(
            f"SELECT ticker, COALESCE(adj_close, close) as adj_close FROM us_daily_price "
            f"WHERE (ticker, trade_date) IN ("
            f"  SELECT ticker, MAX(trade_date) FROM us_daily_price "
            f"  WHERE ticker IN ('{tickers_str}') "
            f"  GROUP BY ticker"
            f")"
        )

        if df.empty:
            logger.debug("_get_latest_prices: 查询无结果，返回空字典")
            return {}

        return dict(zip(df["ticker"], df["adj_close"]))

    @staticmethod
    def _parse_date(date_str) -> date:
        """Convert date string to date object."""
        if isinstance(date_str, date):
            return date_str
        return datetime.strptime(str(date_str)[:10], "%Y-%m-%d").date()
