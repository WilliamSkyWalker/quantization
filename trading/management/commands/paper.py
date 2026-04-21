"""模拟交易。Usage: python3 manage.py paper {status,trade,reset} --market us"""

from datetime import datetime

from django.core.management.base import BaseCommand
from rich.console import Console


class Command(BaseCommand):
    help = "模拟交易 (status/trade/reset)"

    def add_arguments(self, parser):
        parser.add_argument("action", choices=["status", "trade", "reset"])
        parser.add_argument("--market", default="us", choices=["cn", "us", "alpaca"])
        parser.add_argument("--date", default="", help="交易日期 (YYYY-MM-DD)")

    def handle(self, *args, **opts):
        import time
        console = Console()
        action = opts["action"]
        market = opts["market"]
        date = opts["date"] or datetime.now().strftime("%Y-%m-%d")

        if action == "status":
            self._status(console, market)
        elif action == "trade":
            self._trade(console, market, date)
        elif action == "reset":
            self._reset(console, market)

    def _status(self, console, market):
        if market == "alpaca":
            from trading.services.us_alpaca_trader import AlpacaTrader
            trader = AlpacaTrader(None)
            trader.connect()
            console.print(f"\n[cyan]Alpaca 模拟账户[/cyan]")
            console.print(trader.get_position_report())
            return

        if market == "us":
            from trading.services.us_paper_trader import USPaperTrader
            trader = USPaperTrader(None)
        else:
            from trading.services.a_paper_trader import PaperTrader
            trader = PaperTrader(None)
        trader.connect()

        info = trader.get_account_info()
        console.print(f"\n[cyan]{market.upper()} 模拟账户[/cyan]")
        for k, v in info.items():
            console.print(f"  {k}: {v:,.2f}" if isinstance(v, float) else f"  {k}: {v}")

        positions = trader.get_current_positions()
        if positions is not None and not positions.empty:
            console.print(f"\n[cyan]持仓 ({len(positions)} 只):[/cyan]")
            console.print(positions.to_string())
        else:
            console.print("\n  [dim]空仓[/dim]")

    def _trade(self, console, market, date):
        import time
        console.print(f"[cyan]执行 {market.upper()} 模拟交易: {date}[/cyan]")
        t0 = time.time()

        if market in ("us", "alpaca"):
            from backtest.services.us_strategy import USMultiFactorStrategy
            strategy = USMultiFactorStrategy(None)
            result = strategy.select_stocks(date)
            if result is None or result.is_empty():
                console.print("[yellow]无选股结果，跳过交易[/yellow]")
                return
            if market == "alpaca":
                from trading.services.us_alpaca_trader import AlpacaTrader
                trader = AlpacaTrader(None)
            else:
                from trading.services.us_paper_trader import USPaperTrader
                trader = USPaperTrader(None)
            trader.connect()
            n = trader.sync_position(result.select(["ticker", "weight"]).to_pandas())
            trader.update_nav()
        else:
            from backtest.services.a_strategy import MultiFactorStrategy
            from trading.services.a_paper_trader import PaperTrader
            strategy = MultiFactorStrategy(None)
            result = strategy.select_stocks(date)
            if result is None or result.empty:
                console.print("[yellow]无选股结果，跳过交易[/yellow]")
                return
            trader = PaperTrader(None)
            trader.connect()
            n = trader.sync_position(result[["ts_code", "weight"]])

        console.print(f"[green]交易完成: {n} 笔 ({time.time()-t0:.1f}s)[/green]")

    def _reset(self, console, market):
        if market == "alpaca":
            from trading.services.us_alpaca_trader import AlpacaTrader
            trader = AlpacaTrader(None)
        elif market == "us":
            from trading.services.us_paper_trader import USPaperTrader
            trader = USPaperTrader(None)
        else:
            from trading.services.a_paper_trader import PaperTrader
            trader = PaperTrader(None)
        trader.connect()
        if hasattr(trader, "reset"):
            trader.reset()
        else:
            trader.reset_account()
        console.print(f"[green]{market.upper()} 模拟账户已重置[/green]")
