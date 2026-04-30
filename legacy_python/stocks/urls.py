"""Stocks app URL configuration — A 股 + 美股选股 API。"""
from django.urls import path

from stocks.views import a_config, a_data, a_stock, a_watchlist, us_strategy

urlpatterns = [
    # === A 股数据状态 / 任务调度 ===
    path('data/status', a_data.data_status),
    path('data/download', a_data.data_download),
    path('data/update', a_data.data_update),
    path('data/backfill-income', a_data.data_backfill_income),
    path('data/research-reports', a_data.research_reports),
    path('data/download-reports', a_data.data_download_reports),
    path('data/browse', a_data.data_browse),
    path('tasks/', a_data.task_list),
    path('tasks/<str:task_id>', a_data.task_status),
    path('tasks/<str:task_id>/cancel', a_data.task_cancel),

    # === A 股配置 ===
    path('config/settings', a_config.get_settings),
    path('config/settings/update', a_config.update_settings),
    path('config/industry-factors', a_config.get_industry_factors),
    path('config/industry-factors/update', a_config.update_industry_factors),
    path('config/industries', a_config.get_all_industries),
    path('config/init', a_config.init_database),

    # === A 股自选股 ===
    path('watchlist', a_watchlist.get_list),
    path('watchlist/add', a_watchlist.add),
    path('watchlist/<str:ts_code>/remove', a_watchlist.remove),
    path('watchlist/<str:ts_code>/check', a_watchlist.check),

    # === A 股股票详情 ===
    path('stock/search', a_stock.search),
    path('stock/<str:ts_code>/profile', a_stock.profile),
    path('stock/<str:ts_code>/kline', a_stock.kline),
    path('stock/<str:ts_code>/reports', a_stock.reports),
    path('stock/<str:ts_code>/news', a_stock.news),

    # === 美股策略（选股 + 模拟盘 + Alpaca） ===
    path('us/universe', us_strategy.universe),
    path('us/select', us_strategy.select_stocks),
    path('us/backtest/run', us_strategy.backtest_run),
    path('us/paper/account', us_strategy.paper_account),
    path('us/paper/positions', us_strategy.paper_positions),
    path('us/paper/nav', us_strategy.paper_nav),
    path('us/paper/trade', us_strategy.paper_trade),
    path('us/paper/reset', us_strategy.paper_reset),
    path('us/alpaca/account', us_strategy.alpaca_account),
    path('us/alpaca/positions', us_strategy.alpaca_positions),
    path('us/alpaca/orders', us_strategy.alpaca_orders),
    path('us/alpaca/trade', us_strategy.alpaca_trade),
    path('us/alpaca/reconcile', us_strategy.alpaca_reconcile),
    path('us/alpaca/reset', us_strategy.alpaca_reset),
]
