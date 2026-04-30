"""Backtest app URL configuration — A 股选股 + 回测 + 报告生成。"""
from django.urls import path

from backtest.views import a_report, a_strategy

urlpatterns = [
    # === A 股选股 ===
    path('universe', a_strategy.universe),
    path('select', a_strategy.select_stocks),
    path('select/history', a_strategy.select_history),
    path('select/history/<str:date>', a_strategy.select_history_date),
    path('factors', a_strategy.factor_detail),

    # === A 股回测 ===
    path('backtest/run', a_strategy.backtest_run),
    path('backtest/history', a_strategy.backtest_history),
    path('backtest/history/<int:pk>', a_strategy.backtest_history_detail),

    # === 报告生成 ===
    path('report/generate', a_report.generate_report),
]
