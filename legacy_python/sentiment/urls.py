"""Sentiment app URL configuration — 政策舆情 + Polymarket。"""
from django.urls import path

from sentiment.views import polymarket, sentiment

urlpatterns = [
    # === 舆情 ===
    path('sentiment/status', sentiment.sentiment_status),
    path('sentiment/articles', sentiment.sentiment_articles),
    path('sentiment/download', sentiment.sentiment_download),
    path('sentiment/analyze', sentiment.sentiment_analyze),
    path('sentiment/analysis-stats', sentiment.sentiment_analysis_stats),
    path('sentiment/download-and-analyze', sentiment.sentiment_download_and_analyze),
    path('sentiment/backfill-analyze', sentiment.sentiment_backfill_analyze),
    path('sentiment/backfill-content', sentiment.sentiment_backfill_content),
    path('sentiment/backfill-llm', sentiment.sentiment_backfill_llm),

    # === Polymarket Monitor ===
    path('polymarket/monitor/start', polymarket.monitor_start),
    path('polymarket/monitor/stop', polymarket.monitor_stop),
    path('polymarket/status', polymarket.monitor_status),
    path('polymarket/alerts', polymarket.alert_list),
    path('polymarket/alerts/<int:alert_id>/read', polymarket.alert_mark_read),
    path('polymarket/mock-alert', polymarket.mock_alert),
    path('polymarket/mock-alert/delete', polymarket.delete_mock_alerts),

    # === Polymarket Backtest ===
    path('polymarket/backtest/discover', polymarket.backtest_discover),
    path('polymarket/backtest/download', polymarket.backtest_download),
    path('polymarket/backtest/markets', polymarket.backtest_markets),
    path('polymarket/backtest/price-series/<str:condition_id>', polymarket.backtest_price_series),
    path('polymarket/backtest/backfill-categories', polymarket.backtest_backfill_categories),
    path('polymarket/backtest/run', polymarket.backtest_run),
    path('polymarket/backtest/result', polymarket.backtest_result),
    path('polymarket/impact', polymarket.impact_overview),
    path('polymarket/backtest/us-stock-pnl', polymarket.us_stock_pnl),
    path('polymarket/backtest/us-stock-pnl-from-db', polymarket.us_stock_pnl_from_db),
    path('polymarket/backtest/a-share-pnl-from-db', polymarket.a_share_pnl_from_db),
]
