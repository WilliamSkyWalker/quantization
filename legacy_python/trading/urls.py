"""Trading app URL configuration — A 股 paper trading API。"""
from django.urls import path

from trading.views import a_trading

urlpatterns = [
    path('paper/account', a_trading.paper_account),
    path('paper/positions', a_trading.paper_positions),
    path('paper/nav', a_trading.paper_nav),
    path('paper/transactions', a_trading.paper_transactions),
    path('paper/trade', a_trading.paper_trade),
    path('paper/replay', a_trading.paper_replay),
    path('paper/reset', a_trading.paper_reset),
]
