"""API URL configuration."""
from django.urls import path

from .views import data, strategy, trading, sentiment, config, report, polymarket, stock, watchlist

urlpatterns = [
    # Data management
    path('data/status', data.data_status),
    path('data/download', data.data_download),
    path('data/update', data.data_update),
    path('data/backfill-income', data.data_backfill_income),
    path('data/research-reports', data.research_reports),
    path('data/download-reports', data.data_download_reports),
    path('data/browse', data.data_browse),

    # Tasks
    path('tasks/', data.task_list),
    path('tasks/<str:task_id>', data.task_status),
    path('tasks/<str:task_id>/cancel', data.task_cancel),

    # Stock pool & selection
    path('universe', strategy.universe),
    path('select', strategy.select_stocks),
    path('select/history', strategy.select_history),
    path('select/history/<str:date>', strategy.select_history_date),
    path('factors', strategy.factor_detail),

    # Backtest
    path('backtest/run', strategy.backtest_run),
    path('backtest/history', strategy.backtest_history),
    path('backtest/history/<int:pk>', strategy.backtest_history_detail),

    # Paper trading
    path('paper/account', trading.paper_account),
    path('paper/positions', trading.paper_positions),
    path('paper/nav', trading.paper_nav),
    path('paper/transactions', trading.paper_transactions),
    path('paper/trade', trading.paper_trade),
    path('paper/replay', trading.paper_replay),
    path('paper/reset', trading.paper_reset),

    # Sentiment
    path('sentiment/status', sentiment.sentiment_status),
    path('sentiment/articles', sentiment.sentiment_articles),
    path('sentiment/download', sentiment.sentiment_download),
    path('sentiment/analyze', sentiment.sentiment_analyze),
    path('sentiment/analysis-stats', sentiment.sentiment_analysis_stats),
    path('sentiment/download-and-analyze', sentiment.sentiment_download_and_analyze),
    path('sentiment/backfill-analyze', sentiment.sentiment_backfill_analyze),
    path('sentiment/backfill-content', sentiment.sentiment_backfill_content),
    path('sentiment/backfill-llm', sentiment.sentiment_backfill_llm),

    # Config
    path('config/settings', config.get_settings),
    path('config/settings/update', config.update_settings),
    path('config/industry-factors', config.get_industry_factors),
    path('config/industry-factors/update', config.update_industry_factors),
    path('config/industries', config.get_all_industries),
    path('config/init', config.init_database),

    # Watchlist
    path('watchlist', watchlist.get_list),
    path('watchlist/add', watchlist.add),
    path('watchlist/<str:ts_code>/remove', watchlist.remove),
    path('watchlist/<str:ts_code>/check', watchlist.check),

    # Stock detail
    path('stock/search', stock.search),
    path('stock/<str:ts_code>/profile', stock.profile),
    path('stock/<str:ts_code>/kline', stock.kline),
    path('stock/<str:ts_code>/reports', stock.reports),
    path('stock/<str:ts_code>/news', stock.news),

    # Report
    path('report/generate', report.generate_report),

    # Polymarket - Monitor
    path('polymarket/monitor/start', polymarket.monitor_start),
    path('polymarket/monitor/stop', polymarket.monitor_stop),
    path('polymarket/status', polymarket.monitor_status),
    path('polymarket/alerts', polymarket.alert_list),
    path('polymarket/alerts/<int:alert_id>/read', polymarket.alert_mark_read),
    path('polymarket/mock-alert', polymarket.mock_alert),
    path('polymarket/mock-alert/delete', polymarket.delete_mock_alerts),

    # Polymarket - Backtest
    path('polymarket/backtest/discover', polymarket.backtest_discover),
    path('polymarket/backtest/download', polymarket.backtest_download),
    path('polymarket/backtest/markets', polymarket.backtest_markets),
    path('polymarket/backtest/price-series/<str:condition_id>', polymarket.backtest_price_series),
    path('polymarket/backtest/backfill-categories', polymarket.backtest_backfill_categories),
    path('polymarket/backtest/run', polymarket.backtest_run),
    path('polymarket/backtest/result', polymarket.backtest_result),

    # Polymarket - Impact Analysis
    path('polymarket/impact', polymarket.impact_overview),

    # Polymarket - US Stock P&L
    path('polymarket/backtest/us-stock-pnl', polymarket.us_stock_pnl),
    path('polymarket/backtest/us-stock-pnl-from-db', polymarket.us_stock_pnl_from_db),

    # Polymarket - A-Share P&L
    path('polymarket/backtest/a-share-pnl-from-db', polymarket.a_share_pnl_from_db),
]
