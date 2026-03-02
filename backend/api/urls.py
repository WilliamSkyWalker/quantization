"""API URL configuration."""
from django.urls import path

from .views import data, strategy, trading, sentiment, config, report

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
    path('select/<str:task_id>', strategy.select_result),
    path('factors', strategy.factor_detail),

    # Backtest
    path('backtest/run', strategy.backtest_run),
    path('backtest/result/<str:task_id>', strategy.backtest_result),

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

    # Config
    path('config/settings', config.get_settings),
    path('config/settings/update', config.update_settings),
    path('config/industry-factors', config.get_industry_factors),
    path('config/industry-factors/update', config.update_industry_factors),
    path('config/industries', config.get_all_industries),
    path('config/init', config.init_database),

    # Report
    path('report/generate', report.generate_report),
]
