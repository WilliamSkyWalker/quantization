"""Data management API views."""
import logging

import pandas as pd
from rest_framework.decorators import api_view
from rest_framework.response import Response
from sqlalchemy.exc import OperationalError, ProgrammingError

from backend.services.data.database import DatabaseManager
from backend.tasks.manager import task_manager

logger = logging.getLogger(__name__)


def _get_db():
    db = DatabaseManager()
    db.init_tables()
    return db


@api_view(['GET'])
def data_status(request):
    """Get row counts and latest dates for all tables."""
    db = _get_db()
    tables = [
        ('stock_basic', '股票基本信息'),
        ('daily_price', '日线行情'),
        ('financial_data', '财务数据'),
        ('industry_class', '行业分类'),
        ('paper_account', '模拟盘账户'),
        ('paper_position', '模拟盘持仓'),
        ('paper_transaction', '模拟盘交易'),
        ('paper_nav', '模拟盘净值'),
        ('commodity_price', '商品期货价格'),
        ('macro_indicator', '宏观经济指标'),
        ('industry_factor_config', '行业因子配置'),
        ('policy_article', '政策文章'),
        ('policy_analysis', '舆情分析结果'),
        ('scrape_log', '抓取日志'),
        ('research_report', '券商研报'),
        # --- 美股 ---
        ('us_stock_basic', '🇺🇸 股票基本信息'),
        ('us_daily_price', '🇺🇸 日线行情'),
        ('us_financial_data', '🇺🇸 财务数据'),
        ('us_industry_class', '🇺🇸 行业分类'),
        ('us_index_daily', '🇺🇸 指数日线'),
        ('us_macro_indicator', '🇺🇸 宏观指标'),
        ('us_commodity_price', '🇺🇸 商品期货'),
        ('us_analyst_recommendation', '🇺🇸 分析师评级'),
        ('us_sec_filing', '🇺🇸 SEC公告'),
        ('us_corporate_action', '🇺🇸 公司行动'),
    ]
    # SQL to get the last updated_at for each table (when data was last written)
    latest_date_sql = {
        'stock_basic': "SELECT MAX(updated_at) FROM stock_basic",
        'daily_price': "SELECT MAX(updated_at) FROM daily_price",
        'financial_data': "SELECT MAX(updated_at) FROM financial_data",
        'industry_class': "SELECT MAX(updated_at) FROM industry_class",
        'commodity_price': "SELECT MAX(updated_at) FROM commodity_price",
        'macro_indicator': "SELECT MAX(updated_at) FROM macro_indicator",
        'paper_account': "SELECT MAX(updated_at) FROM paper_account",
        'paper_position': "SELECT MAX(updated_at) FROM paper_position",
        'paper_transaction': "SELECT MAX(created_at) FROM paper_transaction",
        'paper_nav': "SELECT MAX(created_at) FROM paper_nav",
        'industry_factor_config': "SELECT MAX(updated_at) FROM industry_factor_config",
        'policy_article': "SELECT MAX(scraped_at) FROM policy_article",
        'policy_analysis': "SELECT MAX(analyzed_at) FROM policy_analysis",
        'scrape_log': "SELECT MAX(finished_at) FROM scrape_log",
        'research_report': "SELECT MAX(updated_at) FROM research_report",
        # 美股
        'us_stock_basic': "SELECT MAX(updated_at) FROM us_stock_basic",
        'us_daily_price': "SELECT MAX(updated_at) FROM us_daily_price",
        'us_financial_data': "SELECT MAX(updated_at) FROM us_financial_data",
        'us_industry_class': "SELECT MAX(updated_at) FROM us_industry_class",
        'us_index_daily': "SELECT MAX(updated_at) FROM us_index_daily",
        'us_macro_indicator': "SELECT MAX(updated_at) FROM us_macro_indicator",
        'us_commodity_price': "SELECT MAX(updated_at) FROM us_commodity_price",
        'us_analyst_recommendation': "SELECT MAX(updated_at) FROM us_analyst_recommendation",
        'us_sec_filing': "SELECT MAX(updated_at) FROM us_sec_filing",
        'us_corporate_action': "SELECT MAX(updated_at) FROM us_corporate_action",
    }

    # SQL to get the latest business data date for each table
    data_date_sql = {
        'daily_price': "SELECT MAX(trade_date) FROM daily_price",
        'financial_data': "SELECT MAX(end_date) FROM financial_data",
        'commodity_price': "SELECT MAX(trade_date) FROM commodity_price",
        'macro_indicator': "SELECT MAX(report_date) FROM macro_indicator",
        'policy_article': "SELECT MAX(publish_date) FROM policy_article",
        'research_report': "SELECT MAX(report_date) FROM research_report",
        # 美股
        'us_daily_price': "SELECT MAX(trade_date) FROM us_daily_price",
        'us_financial_data': "SELECT MAX(date) FROM us_financial_data",
        'us_index_daily': "SELECT MAX(trade_date) FROM us_index_daily",
        'us_macro_indicator': "SELECT MAX(report_date) FROM us_macro_indicator",
        'us_commodity_price': "SELECT MAX(trade_date) FROM us_commodity_price",
        'us_analyst_recommendation': "SELECT MAX(date) FROM us_analyst_recommendation",
        'us_sec_filing': "SELECT MAX(filing_date) FROM us_sec_filing",
        'us_corporate_action': "SELECT MAX(date) FROM us_corporate_action",
    }

    # Get latest trade date separately for dashboard use
    latest_trade_date = None
    try:
        row = db.query("SELECT MAX(trade_date) FROM daily_price")
        val = row.iloc[0, 0] if not row.empty else None
        if val is not None and not (isinstance(val, float) and pd.isna(val)):
            latest_trade_date = str(val)[:10]
    except (OperationalError, ProgrammingError):
        pass

    result = []
    for table_name, label in tables:
        try:
            count = db.table_count(table_name)
            extra = {}
            if table_name in latest_date_sql:
                row = db.query(latest_date_sql[table_name])
                val = row.iloc[0, 0] if not row.empty else None
                if val is not None and not (isinstance(val, float) and pd.isna(val)):
                    extra['latest_date'] = str(val)[:19]  # YYYY-MM-DD HH:MM:SS
            if table_name in data_date_sql:
                row = db.query(data_date_sql[table_name])
                val = row.iloc[0, 0] if not row.empty else None
                if val is not None and not (isinstance(val, float) and pd.isna(val)):
                    extra['data_date'] = str(val)[:10]  # YYYY-MM-DD
            result.append({
                'table': table_name,
                'label': label,
                'count': count,
                **extra,
            })
        except (OperationalError, ProgrammingError) as e:
            logger.debug(f"表 {table_name} 查询失败: {e}")
            result.append({
                'table': table_name,
                'label': label,
                'count': 0,
                'error': '表不存在',
            })

    return Response({
        'tables': result,
        'latest_trade_date': latest_trade_date,
    })


@api_view(['POST'])
def data_download(request):
    """Start a data download task."""
    action = request.data.get('action', 'download_all')

    action_map = {
        'download_all': ('全量下载', _run_download_all),
        'download_extra': ('下载财务+估值+行业', _run_download_extra),
        'download_list': ('下载股票列表', _run_download_list),
        'download_daily': ('下载日线行情', _run_download_daily),
        'download_financial': ('下载财务数据', _run_download_financial),
        'download_valuation': ('下载估值快照', _run_download_valuation),
        'download_industry': ('下载行业分类', _run_download_industry),
        'download_index': ('下载指数数据', _run_download_index),
        'download_commodity': ('下载商品期货', _run_download_commodity),
        'download_macro': ('下载宏观数据', _run_download_macro),
        'download_reports': ('下载券商研报', _run_download_reports),
        'update_list': ('刷新股票列表', _run_update_list),
        'update_daily': ('增量更新日线', _run_update_daily),
        'update_financial': ('增量更新财务', _run_update_financial),
        'update_valuation': ('刷新估值快照', _run_update_valuation),
        'update_industry': ('刷新行业分类', _run_update_industry),
        'update_index': ('增量更新指数', _run_update_index),
        'update_commodity': ('增量更新商品期货', _run_update_commodity),
        'update_macro': ('增量更新宏观', _run_update_macro),
        'update_reports': ('刷新券商研报', _run_update_reports),
        'update_sentiment': ('增量更新舆情', _run_update_sentiment),
        'backfill_daily': ('补录日线行情', _run_backfill_daily),
        'backfill_financial': ('补录财务季度', _run_backfill_financial),
        'backfill_index': ('补录指数数据', _run_backfill_index),
        'backfill_commodity': ('补录商品期货', _run_backfill_commodity),
        'backfill_macro': ('补录宏观数据', _run_backfill_macro),
        'backfill_reports': ('补录券商研报', _run_backfill_reports),
        # --- 美股 ---
        'download_us_all': ('🇺🇸 全量下载', _run_download_us_all),
        'download_us_list': ('🇺🇸 下载股票列表', _run_download_us_list),
        'download_us_daily': ('🇺🇸 下载日线行情', _run_download_us_daily),
        'download_us_financial': ('🇺🇸 下载财务数据', _run_download_us_financial),
        'download_us_industry': ('🇺🇸 下载行业分类', _run_download_us_industry),
        'download_us_index': ('🇺🇸 下载指数数据', _run_download_us_index),
        'download_us_macro': ('🇺🇸 下载宏观数据', _run_download_us_macro),
        'download_us_commodity': ('🇺🇸 下载商品期货', _run_download_us_commodity),
        'download_us_analyst': ('🇺🇸 下载分析师评级', _run_download_us_analyst),
        'download_us_sec_filing': ('🇺🇸 下载SEC公告', _run_download_us_sec_filing),
        'download_us_corporate_action': ('🇺🇸 下载公司行动', _run_download_us_corporate_action),
        'update_us_daily': ('🇺🇸 增量更新日线', _run_update_us_daily),
        'update_us_financial': ('🇺🇸 增量更新财务', _run_update_us_financial),
        'update_us_index': ('🇺🇸 增量更新指数', _run_update_us_index),
        'update_us_macro': ('🇺🇸 增量更新宏观', _run_update_us_macro),
        'update_us_commodity': ('🇺🇸 增量更新商品', _run_update_us_commodity),
        'update_us_analyst': ('🇺🇸 增量更新评级', _run_update_us_analyst),
        'update_us_sec_filing': ('🇺🇸 增量更新SEC', _run_update_us_sec_filing),
        'update_us_corporate_action': ('🇺🇸 增量更新公司行动', _run_update_us_corporate_action),
    }

    if action not in action_map:
        return Response({'error': f'未知操作: {action}'}, status=400)

    name, func = action_map[action]
    task_id = task_manager.submit(name, func)
    return Response({'task_id': task_id, 'name': name})


@api_view(['POST'])
def data_update(request):
    """Start incremental data update."""
    task_id = task_manager.submit('增量更新', _run_update_all)
    return Response({'task_id': task_id, 'name': '增量更新'})


@api_view(['POST'])
def data_backfill_income(request):
    """Start income backfill task."""
    task_id = task_manager.submit('回填利润表', _run_backfill_income)
    return Response({'task_id': task_id, 'name': '回填利润表'})


@api_view(['GET'])
def task_status(request, task_id):
    """Get status of a background task."""
    status = task_manager.get_status(task_id)
    if not status:
        return Response({'error': '任务不存在'}, status=404)
    return Response(status)


@api_view(['GET'])
def task_list(request):
    """Get all recent tasks."""
    return Response(task_manager.get_all_tasks())


@api_view(['POST'])
def task_cancel(request, task_id):
    """Cancel a running or pending task."""
    ok = task_manager.cancel(task_id)
    if ok:
        return Response({'message': f'任务 {task_id} 已取消'})
    status = task_manager.get_status(task_id)
    if not status:
        return Response({'error': '任务不存在'}, status=404)
    return Response({'error': f'任务状态为 {status["status"]}，无法取消'}, status=400)


# --- Task runner functions ---
# Each receives task_id as first argument for progress updates

def _run_download_all(task_id):
    from backend.services.data.downloader import TushareDownloader
    db = _get_db()
    dl = TushareDownloader(db)

    task_manager.update_progress(task_id, 10, '下载股票列表...')
    df = dl.download_stock_list()

    task_manager.update_progress(task_id, 30, f'股票列表完成({len(df)}只)，下载日线行情...')
    count = dl.download_daily_prices()

    task_manager.update_progress(task_id, 80, f'日线完成({count}只)，下载沪深300指数...')
    idx = dl.download_index_daily('000300.SH')

    return {'stocks': len(df), 'daily': count, 'index': idx}


def _run_download_extra(task_id):
    from backend.services.data.updater import FinancialUpdater
    db = _get_db()
    updater = FinancialUpdater(db)

    task_manager.update_progress(task_id, 10, '下载财务数据...')
    fin = updater.download_financial_data()

    task_manager.update_progress(task_id, 50, '下载估值快照...')
    val = updater.download_valuation_snapshot()

    task_manager.update_progress(task_id, 80, '下载行业分类...')
    ind = updater.download_industry_classification()

    return {'financial': fin, 'valuation': val, 'industry': ind}


def _run_download_list(task_id):
    from backend.services.data.downloader import TushareDownloader
    db = _get_db()
    dl = TushareDownloader(db)
    task_manager.update_progress(task_id, 30, '下载股票列表...')
    df = dl.download_stock_list()
    return {'count': len(df)}


def _run_download_daily(task_id):
    from backend.services.data.downloader import TushareDownloader
    db = _get_db()
    dl = TushareDownloader(db)
    task_manager.update_progress(task_id, 10, '下载日线行情...')
    count = dl.download_daily_prices()
    return {'count': count}


def _run_download_financial(task_id):
    from backend.services.data.updater import FinancialUpdater
    db = _get_db()
    updater = FinancialUpdater(db)
    task_manager.update_progress(task_id, 20, '下载财务数据...')
    count = updater.download_financial_data()
    return {'count': count}


def _run_download_valuation(task_id):
    from backend.services.data.updater import FinancialUpdater
    db = _get_db()
    updater = FinancialUpdater(db)
    task_manager.update_progress(task_id, 30, '下载估值快照...')
    count = updater.download_valuation_snapshot()
    return {'count': count}


def _run_download_industry(task_id):
    from backend.services.data.updater import FinancialUpdater
    db = _get_db()
    updater = FinancialUpdater(db)
    task_manager.update_progress(task_id, 30, '下载行业分类...')
    count = updater.download_industry_classification()
    return {'count': count}


def _run_download_index(task_id):
    from backend.services.data.downloader import TushareDownloader
    from backend.services.config import INDUSTRY_INDEX_MAP
    db = _get_db()
    dl = TushareDownloader(db)
    task_manager.update_progress(task_id, 20, '下载沪深300指数...')
    count = dl.download_index_daily('000300.SH')
    if INDUSTRY_INDEX_MAP:
        for i, (name, code) in enumerate(INDUSTRY_INDEX_MAP.items()):
            pct = 20 + int(70 * (i + 1) / len(INDUSTRY_INDEX_MAP))
            task_manager.update_progress(task_id, pct, f'下载行业指数 {name}...')
            dl.download_index_daily(code)
    return {'count': count}


def _run_download_commodity(task_id):
    from backend.services.data.commodity_downloader import CommodityDownloader
    db = _get_db()
    dl = CommodityDownloader(db)
    task_manager.update_progress(task_id, 20, '下载商品期货数据...')
    count = dl.download_commodity_prices()
    return {'count': count}


def _run_download_macro(task_id):
    from backend.services.data.macro_downloader import MacroDownloader
    db = _get_db()
    dl = MacroDownloader(db)
    task_manager.update_progress(task_id, 20, '下载宏观经济数据...')
    results = dl.download_all()
    return {'total': sum(results.values()), 'detail': {k: v for k, v in results.items()}}


def _run_update_list(task_id):
    from backend.services.data.downloader import TushareDownloader
    db = _get_db()
    dl = TushareDownloader(db)
    task_manager.update_progress(task_id, 30, '刷新股票列表...')
    df = dl.download_stock_list()
    return {'count': len(df)}


def _run_update_daily(task_id):
    from backend.services.data.downloader import TushareDownloader
    db = _get_db()
    dl = TushareDownloader(db)
    task_manager.update_progress(task_id, 10, '增量更新日线行情...')
    count = dl.update_daily_prices()
    return {'count': count}


def _run_update_financial(task_id):
    from backend.services.data.updater import FinancialUpdater
    db = _get_db()
    updater = FinancialUpdater(db)
    task_manager.update_progress(task_id, 20, '增量更新财务数据...')
    count = updater.update_financial_data()
    return {'count': count}


def _run_update_valuation(task_id):
    from backend.services.data.updater import FinancialUpdater
    db = _get_db()
    updater = FinancialUpdater(db)
    task_manager.update_progress(task_id, 30, '刷新估值快照...')
    count = updater.download_valuation_snapshot()
    return {'count': count}


def _run_update_industry(task_id):
    from backend.services.data.updater import FinancialUpdater
    db = _get_db()
    updater = FinancialUpdater(db)
    task_manager.update_progress(task_id, 30, '刷新行业分类...')
    count = updater.download_industry_classification()
    return {'count': count}


def _run_update_index(task_id):
    from backend.services.data.downloader import TushareDownloader
    from backend.services.config import INDUSTRY_INDEX_MAP
    db = _get_db()
    dl = TushareDownloader(db)
    task_manager.update_progress(task_id, 20, '增量更新沪深300指数...')
    count = dl.update_index_daily('000300.SH')
    if INDUSTRY_INDEX_MAP:
        for i, (name, code) in enumerate(INDUSTRY_INDEX_MAP.items()):
            pct = 20 + int(70 * (i + 1) / len(INDUSTRY_INDEX_MAP))
            task_manager.update_progress(task_id, pct, f'增量更新行业指数 {name}...')
            dl.update_index_daily(code)
    return {'count': count}


def _run_update_commodity(task_id):
    from backend.services.data.commodity_downloader import CommodityDownloader
    db = _get_db()
    dl = CommodityDownloader(db)
    task_manager.update_progress(task_id, 20, '增量更新商品期货...')
    count = dl.update_commodity_prices()
    return {'count': count}


def _run_update_macro(task_id):
    from backend.services.data.macro_downloader import MacroDownloader
    db = _get_db()
    dl = MacroDownloader(db)
    task_manager.update_progress(task_id, 20, '增量更新宏观数据...')
    results = dl.update()
    return {'total': sum(results.values()) if isinstance(results, dict) else results}


def _run_update_reports(task_id):
    from datetime import datetime, timedelta
    from backend.services.data.akshare_downloader import AKShareDownloader
    db = _get_db()
    dl = AKShareDownloader(db)
    begin = (datetime.now() - timedelta(days=90)).strftime("%Y-%m-%d")
    task_manager.update_progress(task_id, 20, f'增量刷新券商研报 ({begin} 起)...')
    count = dl.download_research_reports(begin_time=begin)
    return {'count': count}


def _run_update_sentiment(task_id):
    db = _get_db()
    task_manager.update_progress(task_id, 10, '增量抓取舆情...')
    sent_new = 0
    try:
        from backend.services.sentiment.downloader import SentimentDownloader
        sent_dl = SentimentDownloader(db)
        sent_results = sent_dl.download_all()
        sent_new = sum(r['new'] for r in sent_results.values())
    except Exception as e:
        logger.warning(f'舆情抓取跳过 ({type(e).__name__}): {e}')

    task_manager.update_progress(task_id, 60, '舆情分析...')
    analysis_result = {}
    try:
        from backend.services.sentiment.analyzer import SentimentAnalyzer
        analyzer = SentimentAnalyzer(db)
        analysis_result = analyzer.analyze_pending(max_articles=1000)
    except Exception as e:
        logger.warning(f'舆情分析跳过 ({type(e).__name__}): {e}')

    return {'new_articles': sent_new, **analysis_result}


def _run_update_all(task_id):
    from backend.services.data.downloader import TushareDownloader
    from backend.services.data.updater import FinancialUpdater
    from backend.services.config import INDUSTRY_INDEX_MAP
    db = _get_db()
    dl = TushareDownloader(db)
    updater = FinancialUpdater(db)

    task_manager.update_progress(task_id, 5, '步骤1/10: 刷新股票列表...')
    df = dl.download_stock_list()

    task_manager.update_progress(task_id, 12, '步骤2/10: 增量更新日线行情...')
    daily = dl.update_daily_prices()

    task_manager.update_progress(task_id, 24, '步骤3/10: 更新沪深300指数...')
    dl.update_index_daily('000300.SH')
    if INDUSTRY_INDEX_MAP:
        for name, code in INDUSTRY_INDEX_MAP.items():
            dl.update_index_daily(code)

    task_manager.update_progress(task_id, 33, '步骤4/10: 更新商品期货...')
    try:
        from backend.services.data.commodity_downloader import CommodityDownloader
        CommodityDownloader(db).update_commodity_prices()
    except Exception as e:
        logger.warning(f'商品期货跳过 ({type(e).__name__}): {e}')

    task_manager.update_progress(task_id, 42, '步骤5/10: 更新宏观数据...')
    try:
        from backend.services.data.macro_downloader import MacroDownloader
        MacroDownloader(db).update()
    except Exception as e:
        logger.warning(f'宏观数据跳过 ({type(e).__name__}): {e}')

    task_manager.update_progress(task_id, 52, '步骤6/10: 更新财务数据...')
    updater.update_financial_data()

    task_manager.update_progress(task_id, 62, '步骤7/10: 刷新估值快照...')
    updater.download_valuation_snapshot()

    task_manager.update_progress(task_id, 72, '步骤8/10: 刷新行业分类...')
    updater.download_industry_classification()

    task_manager.update_progress(task_id, 78, '步骤9/12: 下载券商研报...')
    try:
        from backend.services.data.akshare_downloader import AKShareDownloader
        ak_dl = AKShareDownloader(db)
        ak_dl.download_research_reports()
    except Exception as e:
        logger.warning(f'券商研报跳过 ({type(e).__name__}): {e}')

    task_manager.update_progress(task_id, 82, '步骤10/12: 抓取舆情...')
    sent_new = 0
    try:
        from backend.services.sentiment.downloader import SentimentDownloader
        sent_dl = SentimentDownloader(db)
        sent_results = sent_dl.download_all(max_pages=2, incremental=True)
        sent_new = sum(r['new'] for r in sent_results.values())
        logger.info(f'舆情抓取完成: {sent_new} 篇新文章')
    except Exception as e:
        logger.warning(f'舆情抓取跳过 ({type(e).__name__}): {e}')

    task_manager.update_progress(task_id, 90, '步骤11/12: 舆情分析...')
    try:
        from backend.services.sentiment.analyzer import SentimentAnalyzer
        analyzer = SentimentAnalyzer(db)
        analysis_result = analyzer.analyze_pending(max_articles=1000)
        logger.info(f'舆情分析完成: keyword={analysis_result["keyword_analyzed"]}, llm={analysis_result["llm_analyzed"]}')
    except Exception as e:
        logger.warning(f'舆情分析跳过 ({type(e).__name__}): {e}')

    return {'stocks': len(df), 'daily': daily, 'sentiment_new': sent_new}


def _run_backfill_income(task_id):
    from backend.services.data.updater import FinancialUpdater
    db = _get_db()
    updater = FinancialUpdater(db)
    task_manager.update_progress(task_id, 20, '回填利润表数据...')
    count = updater.backfill_income()
    return {'count': count}


def _run_backfill_daily(task_id):
    from backend.services.data.downloader import TushareDownloader
    db = _get_db()
    dl = TushareDownloader(db)
    task_manager.update_progress(task_id, 10, '检测缺失交易日...')
    result = dl.backfill_daily_prices()
    return result


def _run_backfill_financial(task_id):
    from backend.services.data.updater import FinancialUpdater
    db = _get_db()
    updater = FinancialUpdater(db)
    task_manager.update_progress(task_id, 10, '检测缺失财务季度...')
    result = updater.backfill_financial_quarters()
    return result


def _run_backfill_index(task_id):
    from backend.services.data.downloader import TushareDownloader
    db = _get_db()
    dl = TushareDownloader(db)
    task_manager.update_progress(task_id, 10, '检测缺失指数数据...')
    result = dl.backfill_index_daily()
    return result


def _run_backfill_commodity(task_id):
    from backend.services.data.commodity_downloader import CommodityDownloader
    db = _get_db()
    dl = CommodityDownloader(db)
    task_manager.update_progress(task_id, 10, '补录商品期货数据...')
    result = dl.backfill_commodity_prices()
    return result


def _run_backfill_macro(task_id):
    from backend.services.data.macro_downloader import MacroDownloader
    db = _get_db()
    dl = MacroDownloader(db)
    task_manager.update_progress(task_id, 10, '补录宏观数据...')
    result = dl.backfill()
    return result


def _run_backfill_reports(task_id):
    from backend.services.data.akshare_downloader import AKShareDownloader
    db = _get_db()
    dl = AKShareDownloader(db)
    task_manager.update_progress(task_id, 10, '强制全量下载券商研报...')
    count = dl.download_research_reports(force=True)
    return {'count': count}


def _run_download_reports(task_id, force=False):
    from backend.services.data.akshare_downloader import AKShareDownloader
    db = _get_db()
    dl = AKShareDownloader(db)
    task_manager.update_progress(task_id, 20, '下载券商研报...')
    count = dl.download_research_reports(force=force)
    return {'count': count}


@api_view(['POST'])
def data_download_reports(request):
    """Start research report download task. Pass force=true to skip early termination."""
    force = str(request.data.get('force', '')).lower() in ('true', '1')
    task_id = task_manager.submit('下载券商研报', _run_download_reports, force)
    return Response({'task_id': task_id, 'name': '下载券商研报'})


@api_view(['GET'])
def research_reports(request):
    """Get research reports with pagination and filtering."""
    db = _get_db()
    ts_code = request.query_params.get('ts_code')
    institution = request.query_params.get('institution')
    start_date = request.query_params.get('start_date')
    end_date = request.query_params.get('end_date')
    page = int(request.query_params.get('page', 1))
    page_size = int(request.query_params.get('page_size', 20))
    offset = (page - 1) * page_size

    conditions = []
    params = {}
    if ts_code:
        conditions.append("ts_code = :ts_code")
        params['ts_code'] = ts_code
    if institution:
        conditions.append("institution LIKE :institution")
        params['institution'] = f'%{institution}%'
    if start_date:
        conditions.append("report_date >= :start_date")
        params['start_date'] = start_date
    if end_date:
        conditions.append("report_date <= :end_date")
        params['end_date'] = end_date

    where = ''
    if conditions:
        where = 'WHERE ' + ' AND '.join(conditions)

    try:
        total_df = db.query(
            f"SELECT COUNT(*) as cnt FROM research_report {where}",
            params=params,
        )
        total = int(total_df['cnt'].iloc[0])

        df = db.query(
            f"SELECT ts_code, stock_name, institution, analyst, title, "
            f"rating, rating_score, report_date "
            f"FROM research_report {where} "
            f"ORDER BY report_date DESC LIMIT :limit OFFSET :offset",
            params={**params, 'limit': page_size, 'offset': offset},
        )
    except (OperationalError, ProgrammingError):
        return Response({'reports': [], 'total': 0, 'page': page, 'page_size': page_size})

    reports = df.to_dict('records') if not df.empty else []
    for r in reports:
        if r.get('report_date'):
            r['report_date'] = str(r['report_date'])

    return Response({
        'reports': reports,
        'total': total,
        'page': page,
        'page_size': page_size,
    })


# ------------------------------------------------------------------
# 美股 task runners
# ------------------------------------------------------------------

def _run_download_us_all(task_id):
    from backend.services.data.fmp_downloader import FMPDownloader
    from backend.services.data.fred_downloader import FREDDownloader
    db = _get_db()
    fmp = FMPDownloader(db)
    fred = FREDDownloader(db)

    task_manager.update_progress(task_id, 5, '🇺🇸 下载股票列表...')
    fmp.download_stock_list()
    task_manager.update_progress(task_id, 15, '🇺🇸 下载日线行情...')
    fmp.download_daily_prices()
    task_manager.update_progress(task_id, 40, '🇺🇸 下载财务数据...')
    fmp.download_financial_data()
    task_manager.update_progress(task_id, 55, '🇺🇸 下载行业分类...')
    fmp.download_industry_class()
    task_manager.update_progress(task_id, 60, '🇺🇸 下载指数数据...')
    fmp.download_index_daily()
    task_manager.update_progress(task_id, 65, '🇺🇸 下载宏观数据 (FRED)...')
    fred.download_all()
    task_manager.update_progress(task_id, 75, '🇺🇸 下载商品期货...')
    fmp.download_commodity_prices()
    task_manager.update_progress(task_id, 80, '🇺🇸 下载分析师评级...')
    fmp.download_analyst_recommendations()
    task_manager.update_progress(task_id, 90, '🇺🇸 下载SEC公告...')
    fmp.download_sec_filings()
    task_manager.update_progress(task_id, 95, '🇺🇸 下载公司行动...')
    fmp.download_corporate_actions()
    return {'status': 'ok'}


def _run_download_us_list(task_id):
    from backend.services.data.fmp_downloader import FMPDownloader
    db = _get_db()
    dl = FMPDownloader(db)
    task_manager.update_progress(task_id, 30, '🇺🇸 下载股票列表...')
    count = dl.download_stock_list()
    return {'count': count}


def _run_download_us_daily(task_id):
    from backend.services.data.fmp_downloader import FMPDownloader
    db = _get_db()
    dl = FMPDownloader(db)
    task_manager.update_progress(task_id, 10, '🇺🇸 下载日线行情...')
    count = dl.download_daily_prices()
    return {'count': count}


def _run_download_us_financial(task_id):
    from backend.services.data.fmp_downloader import FMPDownloader
    db = _get_db()
    dl = FMPDownloader(db)
    task_manager.update_progress(task_id, 10, '🇺🇸 下载财务数据...')
    count = dl.download_financial_data()
    return {'count': count}


def _run_download_us_industry(task_id):
    from backend.services.data.fmp_downloader import FMPDownloader
    db = _get_db()
    dl = FMPDownloader(db)
    task_manager.update_progress(task_id, 30, '🇺🇸 下载行业分类...')
    count = dl.download_industry_class()
    return {'count': count}


def _run_download_us_index(task_id):
    from backend.services.data.fmp_downloader import FMPDownloader
    db = _get_db()
    dl = FMPDownloader(db)
    task_manager.update_progress(task_id, 20, '🇺🇸 下载指数数据...')
    count = dl.download_index_daily()
    return {'count': count}


def _run_download_us_macro(task_id):
    from backend.services.data.fred_downloader import FREDDownloader
    db = _get_db()
    dl = FREDDownloader(db)
    task_manager.update_progress(task_id, 20, '🇺🇸 下载宏观数据 (FRED)...')
    results = dl.download_all()
    return {'total': sum(results.values()), 'detail': results}


def _run_download_us_commodity(task_id):
    from backend.services.data.fmp_downloader import FMPDownloader
    db = _get_db()
    dl = FMPDownloader(db)
    task_manager.update_progress(task_id, 20, '🇺🇸 下载商品期货...')
    count = dl.download_commodity_prices()
    return {'count': count}


def _run_download_us_analyst(task_id):
    from backend.services.data.fmp_downloader import FMPDownloader
    db = _get_db()
    dl = FMPDownloader(db)
    task_manager.update_progress(task_id, 10, '🇺🇸 下载分析师评级...')
    count = dl.download_analyst_recommendations()
    return {'count': count}


def _run_download_us_sec_filing(task_id):
    from backend.services.data.fmp_downloader import FMPDownloader
    db = _get_db()
    dl = FMPDownloader(db)
    task_manager.update_progress(task_id, 10, '🇺🇸 下载SEC公告...')
    count = dl.download_sec_filings()
    return {'count': count}


def _run_download_us_corporate_action(task_id):
    from backend.services.data.fmp_downloader import FMPDownloader
    db = _get_db()
    dl = FMPDownloader(db)
    task_manager.update_progress(task_id, 10, '🇺🇸 下载公司行动...')
    count = dl.download_corporate_actions()
    return {'count': count}


def _run_update_us_daily(task_id):
    from backend.services.data.fmp_downloader import FMPDownloader
    db = _get_db()
    dl = FMPDownloader(db)
    task_manager.update_progress(task_id, 10, '🇺🇸 增量更新日线...')
    count = dl.update_daily_prices()
    return {'count': count}


def _run_update_us_financial(task_id):
    from backend.services.data.fmp_downloader import FMPDownloader
    db = _get_db()
    dl = FMPDownloader(db)
    task_manager.update_progress(task_id, 10, '🇺🇸 增量更新财务...')
    count = dl.update_financial_data()
    return {'count': count}


def _run_update_us_index(task_id):
    from backend.services.data.fmp_downloader import FMPDownloader
    db = _get_db()
    dl = FMPDownloader(db)
    task_manager.update_progress(task_id, 20, '🇺🇸 增量更新指数...')
    count = dl.update_index_daily()
    return {'count': count}


def _run_update_us_macro(task_id):
    from backend.services.data.fred_downloader import FREDDownloader
    db = _get_db()
    dl = FREDDownloader(db)
    task_manager.update_progress(task_id, 20, '🇺🇸 增量更新宏观...')
    results = dl.update()
    return {'total': sum(results.values()) if isinstance(results, dict) else results}


def _run_update_us_commodity(task_id):
    from backend.services.data.fmp_downloader import FMPDownloader
    db = _get_db()
    dl = FMPDownloader(db)
    task_manager.update_progress(task_id, 20, '🇺🇸 增量更新商品...')
    count = dl.update_commodity_prices()
    return {'count': count}


def _run_update_us_analyst(task_id):
    from backend.services.data.fmp_downloader import FMPDownloader
    db = _get_db()
    dl = FMPDownloader(db)
    task_manager.update_progress(task_id, 10, '🇺🇸 增量更新评级...')
    count = dl.update_analyst_recommendations()
    return {'count': count}


def _run_update_us_sec_filing(task_id):
    from backend.services.data.fmp_downloader import FMPDownloader
    db = _get_db()
    dl = FMPDownloader(db)
    task_manager.update_progress(task_id, 10, '🇺🇸 增量更新SEC...')
    count = dl.update_sec_filings()
    return {'count': count}


def _run_update_us_corporate_action(task_id):
    from backend.services.data.fmp_downloader import FMPDownloader
    db = _get_db()
    dl = FMPDownloader(db)
    task_manager.update_progress(task_id, 10, '🇺🇸 增量更新公司行动...')
    count = dl.update_corporate_actions()
    return {'count': count}


# ------------------------------------------------------------------
# Generic data browse
# ------------------------------------------------------------------
_BROWSE_TABLES = {
    'stock_basic': {
        'label': '股票基本信息',
        'order': 'ts_code ASC',
        'columns': 'ts_code, name, market, list_date, delist_date, is_st, total_share, float_share',
    },
    'daily_price': {
        'label': '日线行情',
        'order': 'trade_date DESC, ts_code ASC',
        'columns': 'ts_code, trade_date, `open`, high, low, `close`, volume, amount, pct_chg, turnover_rate',
    },
    'financial_data': {
        'label': '财务数据',
        'order': 'ann_date DESC, ts_code ASC',
        'columns': 'ts_code, ann_date, end_date, revenue, net_profit, pe_ttm, pb, roe_ttm, gross_margin, bps, total_mv',
    },
    'industry_class': {
        'label': '行业分类',
        'order': 'ts_code ASC',
        'columns': 'ts_code, industry_name, l2_industry_name',
    },
    'commodity_price': {
        'label': '商品期货价格',
        'order': 'trade_date DESC, commodity_code ASC',
        'columns': 'commodity_code, ts_code, trade_date, `open`, high, low, `close`, settle, volume, amount, oi',
    },
    'macro_indicator': {
        'label': '宏观经济指标',
        'order': 'report_date DESC, indicator_code ASC',
        'columns': 'indicator_code, report_date, value',
    },
    'policy_article': {
        'label': '政策文章',
        'order': 'publish_date DESC',
        'columns': 'source, tier, title, category, publish_date, url, scraped_at',
    },
    'policy_analysis': {
        'label': '舆情分析结果',
        'order': 'analyzed_at DESC',
        'columns': '*',
    },
    'research_report': {
        'label': '券商研报',
        'order': 'report_date DESC',
        'columns': 'ts_code, stock_name, institution, analyst, title, rating, rating_score, report_date',
    },
    'paper_account': {
        'label': '模拟盘账户',
        'order': 'id DESC',
        'columns': '*',
    },
    'paper_position': {
        'label': '模拟盘持仓',
        'order': 'ts_code ASC',
        'columns': '*',
    },
    'paper_transaction': {
        'label': '模拟盘交易',
        'order': 'created_at DESC',
        'columns': '*',
    },
    'paper_nav': {
        'label': '模拟盘净值',
        'order': 'trade_date DESC',
        'columns': '*',
    },
    # --- 美股 ---
    'us_stock_basic': {
        'label': '🇺🇸 股票基本信息',
        'order': 'ticker ASC',
        'columns': 'ticker, name, exchange, sector, industry, ipo_date, market_cap, country, is_active',
    },
    'us_daily_price': {
        'label': '🇺🇸 日线行情',
        'order': 'trade_date DESC, ticker ASC',
        'columns': 'ticker, trade_date, `open`, high, low, `close`, adj_close, volume, change_pct',
    },
    'us_financial_data': {
        'label': '🇺🇸 财务数据',
        'order': 'date DESC, ticker ASC',
        'columns': 'ticker, period, date, filing_date, revenue, net_income, eps, gross_margin, operating_margin, roe, pe_ratio, pb_ratio',
    },
    'us_industry_class': {
        'label': '🇺🇸 行业分类',
        'order': 'ticker ASC',
        'columns': 'ticker, sector, industry, sub_industry',
    },
    'us_index_daily': {
        'label': '🇺🇸 指数日线',
        'order': 'trade_date DESC, index_code ASC',
        'columns': 'index_code, trade_date, `open`, high, low, `close`, volume',
    },
    'us_macro_indicator': {
        'label': '🇺🇸 宏观指标',
        'order': 'report_date DESC, indicator_code ASC',
        'columns': 'indicator_code, report_date, value',
    },
    'us_commodity_price': {
        'label': '🇺🇸 商品期货',
        'order': 'trade_date DESC, symbol ASC',
        'columns': 'symbol, trade_date, `open`, high, low, `close`, volume',
    },
    'us_analyst_recommendation': {
        'label': '🇺🇸 分析师评级',
        'order': 'date DESC, ticker ASC',
        'columns': 'ticker, date, analyst_company, analyst_name, rating, price_target',
    },
    'us_sec_filing': {
        'label': '🇺🇸 SEC公告',
        'order': 'filing_date DESC, ticker ASC',
        'columns': 'ticker, filing_date, type, title, url',
    },
    'us_corporate_action': {
        'label': '🇺🇸 公司行动',
        'order': 'date DESC, ticker ASC',
        'columns': 'ticker, date, action_type, label, value',
    },
}


@api_view(['GET'])
def data_browse(request):
    """Generic paginated data browser for any registered table."""
    table = request.query_params.get('table', '')
    if table not in _BROWSE_TABLES:
        return Response({'error': f'不支持浏览的表: {table}', 'tables': list(_BROWSE_TABLES.keys())}, status=400)

    spec = _BROWSE_TABLES[table]
    page = int(request.query_params.get('page', 1))
    page_size = min(int(request.query_params.get('page_size', 50)), 200)
    keyword = request.query_params.get('keyword', '').strip()
    offset = (page - 1) * page_size

    db = _get_db()

    # Optional keyword filter (search across text columns)
    where = ''
    params: dict = {}
    if keyword:
        # Simple keyword search: LIKE on first text column
        text_cols = [c.strip().strip('`') for c in spec['columns'].split(',') if c.strip() != '*']
        like_parts = []
        for col in text_cols[:3]:  # search first 3 columns
            like_parts.append(f"`{col}` LIKE :kw")
        if like_parts:
            where = 'WHERE (' + ' OR '.join(like_parts) + ')'
            params['kw'] = f'%{keyword}%'

    try:
        total_df = db.query(f"SELECT COUNT(*) as cnt FROM `{table}` {where}", params=params)
        total = int(total_df['cnt'].iloc[0])

        cols = spec['columns']
        df = db.query(
            f"SELECT {cols} FROM `{table}` {where} ORDER BY {spec['order']} LIMIT :lim OFFSET :off",
            params={**params, 'lim': page_size, 'off': offset},
        )
    except (OperationalError, ProgrammingError) as e:
        return Response({'error': str(e), 'rows': [], 'total': 0})

    # Convert to JSON-safe format (NaN, NaT, None → None; everything else → str)
    def _safe(v):
        if v is None:
            return None
        if isinstance(v, float) and (pd.isna(v) or v != v):
            return None
        try:
            if pd.isna(v):
                return None
        except (TypeError, ValueError):
            pass
        return str(v)

    rows = []
    if not df.empty:
        for _, row in df.iterrows():
            rows.append({col: _safe(row[col]) for col in df.columns})

    return Response({
        'table': table,
        'label': spec['label'],
        'columns': [c.strip().strip('`') for c in (df.columns.tolist() if not df.empty else spec['columns'].split(','))],
        'rows': rows,
        'total': total,
        'page': page,
        'page_size': page_size,
    })
